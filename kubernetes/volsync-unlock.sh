#!/bin/bash

# volsync-unlock.sh - Safely unlock stuck restic repositories from volsync pods
set -euo pipefail

# Configuration
PASSWORD_FILE="$HOME/.secrets/wasabi-restic"  # Adjust path as needed
WASABI_BASE="/mnt/wasabi"
DRY_RUN=${1:-"false"}  # Pass "true" as first argument for dry run

# Color output for better visibility - using printf for better compatibility
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    printf "${BLUE}[INFO]${NC} %s\n" "$1"
}

log_warn() {
    printf "${YELLOW}[WARN]${NC} %s\n" "$1"
}

log_error() {
    printf "${RED}[ERROR]${NC} %s\n" "$1"
}

log_success() {
    printf "${GREEN}[SUCCESS]${NC} %s\n" "$1"
}

# Check prerequisites
check_prerequisites() {
    log_info "Checking prerequisites..."
    
    if ! command -v kubectl >/dev/null 2>&1; then
        log_error "kubectl not found. Please install kubectl."
        exit 1
    fi
    
    if ! command -v restic >/dev/null 2>&1; then
        log_error "restic not found. Please install restic."
        exit 1
    fi
    
    if [[ ! -f "$PASSWORD_FILE" ]]; then
        log_error "Password file not found at $PASSWORD_FILE"
        exit 1
    fi
    
    if [[ ! -d "$WASABI_BASE" ]]; then
        log_error "Wasabi mount point not found at $WASABI_BASE"
        exit 1
    fi
    
    # Check if we can use sudo (needed for accessing root-owned restic repos)
    if ! sudo -n true 2>/dev/null; then
        log_warn "This script requires passwordless sudo access for restic operations"
        log_info "Continuing anyway - you may be prompted for sudo password"
    fi
    
    log_success "Prerequisites check passed"
}

# Export restic password
setup_restic_password() {
    log_info "Setting up restic password..."
    export RESTIC_PASSWORD=$(cat "$PASSWORD_FILE")
    if [[ -z "$RESTIC_PASSWORD" ]]; then
        log_error "Password file is empty"
        exit 1
    fi
    log_success "Restic password loaded"
}

# Get failed volsync pods and extract unique repository names
get_failed_repositories() {
    # Get failed pods, extract names, strip random suffixes, and get unique entries
    # Note: This function outputs data to stdout for capture, logging goes to stderr
    
    local failed_repos
    failed_repos=$(timeout 30 kubectl get pods -A --no-headers | \
        grep volsync | \
        grep -E "(Error|CrashLoopBackOff|Failed)" | \
        awk '{print $2}' | \
        sed 's/-[a-z0-9]\{5\}$//' | \
        sort | uniq)
    
    if [[ $? -ne 0 || -z "$failed_repos" ]]; then
        return 1
    fi
    
    # Output the repository names to stdout for capture
    echo "$failed_repos"
}

# Parse repository name to construct restic path
# Expected format: volsync-src-APP-VOLUME-TYPE
parse_repo_path() {
    local repo_name="$1"
    
    # Remove volsync-src- prefix
    local clean_name="${repo_name#volsync-src-}"
    
    # Split from the RIGHT side to handle app names with hyphens
    # This splits on the last two hyphens: APP-VOLUME-TYPE
    if [[ "$clean_name" =~ ^(.+)-([^-]+)-([^-]+)$ ]]; then
        local full_match="${BASH_REMATCH[0]}"
        local type="${BASH_REMATCH[3]}"
        local volume="${BASH_REMATCH[2]}"
        
        # Remove -VOLUME-TYPE from the end to get APP
        local app="${clean_name%-${volume}-${type}}"
        
        # Construct the expected restic repository path
        local repo_path="$WASABI_BASE/$app/volsync/$volume-volsync-$type"
        
        if [[ "$DRY_RUN" == "true" ]]; then
            printf "[DEBUG] Parsing: clean_name='%s' -> app='%s', volume='%s', type='%s'\n" "$clean_name" "$app" "$volume" "$type" >&2
        fi
        
        echo "$repo_path"
    else
        log_error "Could not parse repository name: $repo_name"
        log_error "Clean name after prefix removal: $clean_name"
        return 1
    fi
}

# Unlock a single repository
unlock_repository() {
    local repo_name="$1"
    local repo_path
    
    repo_path=$(parse_repo_path "$repo_name")
    if [[ $? -ne 0 ]]; then
        log_error "Failed to parse repository path for: $repo_name"
        return 1
    fi
    
    # Use sudo to check if repository path exists (since it's root-owned)
    if sudo test -d "$repo_path"; then
        log_info "Repository path exists: $repo_path"
    else
        log_warn "Repository path does not exist: $repo_path"
        return 1
    fi
    
    log_info "Unlocking repository: $repo_path"
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_warn "[DRY RUN] Would run: sudo RESTIC_PASSWORD='***' restic -r $repo_path unlock"
        return 0
    fi
    
    # Use sudo with environment variable passing for restic
    if sudo RESTIC_PASSWORD="$RESTIC_PASSWORD" restic -r "$repo_path" unlock; then
        log_success "Successfully unlocked: $repo_path"
        return 0
    else
        log_error "Failed to unlock: $repo_path"
        return 1
    fi
}

# Main execution
main() {
    log_info "Starting volsync repository unlock process..."
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_warn "Running in DRY RUN mode - no actual unlock commands will be executed"
    fi
    
    check_prerequisites
    setup_restic_password
    
    log_info "Finding failed volsync pods..."
    
    # First, let's see what volsync pods exist at all
    local all_volsync_pods
    all_volsync_pods=$(kubectl get pods -A --no-headers | grep volsync | wc -l)
    log_info "Total volsync pods found: $all_volsync_pods"
    
    # Show all volsync pods for debugging
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "All volsync pods:"
        kubectl get pods -A --no-headers | grep volsync | awk '{printf "  %s/%s - %s\n", $1, $2, $4}'
    fi
    
    local failed_repos
    failed_repos=$(get_failed_repositories)
    local get_repos_exit_code=$?
    
    if [[ $get_repos_exit_code -ne 0 || -z "$failed_repos" ]]; then
        log_warn "No failed volsync pods found"
        log_info "Pod states found:"
        kubectl get pods -A --no-headers | grep volsync | awk '{print $4}' | sort | uniq -c | sed 's/^/  /'
        log_info "No repositories to unlock. Exiting."
        exit 0
    fi
    
    log_info "Found failed repositories:"
    echo "$failed_repos" | sed 's/^/  - /'
    
    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "Debug - failed_repos content:"
        echo "$failed_repos" | while IFS= read -r line; do
            echo "  '$line'"
        done
    fi
    
    local success_count=0
    local failure_count=0
    
    log_info "Processing repositories..."
    
    # Convert to array and process
    local repos_array
    readarray -t repos_array <<< "$failed_repos"
    
    for repo_name in "${repos_array[@]}"; do
        [[ -z "$repo_name" ]] && continue
        
        log_info "Processing repository: $repo_name"
        
        # Temporarily disable exit on error for individual repository processing
        set +e
        unlock_repository "$repo_name"
        local unlock_result=$?
        set -e
        
        if [[ $unlock_result -eq 0 ]]; then
            success_count=$((success_count + 1))
        else
            failure_count=$((failure_count + 1))
        fi
    done
    
    log_info "Operation completed:"
    log_success "  Successfully unlocked: $success_count repositories"
    if [[ $failure_count -gt 0 ]]; then
        log_error "  Failed to unlock: $failure_count repositories"
    fi
    
    # Clean up restic cache if we successfully unlocked any repositories
    if [[ $success_count -gt 0 && "$DRY_RUN" != "true" ]]; then
        log_info "Cleaning up restic cache..."
        if sudo restic cache --cleanup; then
            log_success "Restic cache cleanup completed"
        else
            log_warn "Restic cache cleanup failed (non-critical)"
        fi
    fi
    
    if [[ $failure_count -gt 0 ]]; then
        exit 1
    fi
}

# Show usage if help requested
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    echo "Usage: $0 [dry_run]"
    echo "  dry_run: Pass 'true' to see what would be done without executing unlock commands"
    echo ""
    echo "Configuration:"
    echo "  PASSWORD_FILE: $PASSWORD_FILE"
    echo "  WASABI_BASE: $WASABI_BASE"
    exit 0
fi

main "$@"