#!/usr/bin/env bash
set -euo pipefail

# ---------- CONFIG ----------
NAMESPACE="media"                              # namespace where audiobookshelf lives
APP_LABEL="app.kubernetes.io/name=audiobookshelf"  # label selector for the pod
DRY_RUN=0                                      # 1=dry-run, 0=actual sync

# Paths inside the pod
OPENAUDIBLE_SRC="/mnt-old/media/audible/"      # OpenAudible downloads (source)
AUDIOBOOKSHELF_AUDIBLE="/mnt/audible/"         # where to sync OpenAudible content
AUDIOBOOKSHELF_BOOKS="/mnt/audiobooks/"        # audiobookshelf library
NAS_BACKUP="/mnt-old/media/audiobooks/"        # backup destination on NAS

# ---------- FUNCTIONS ----------
log() { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

# Count files by category from rsync output
count_files() {
    local output="$1"
    local audio=0 images=0 metadata=0 other=0

    # Count by extension patterns (case-insensitive)
    audio=$(echo "$output" | grep -ciE '\.(mp3|m4b|m4a|flac|opus|ogg|aac)$' || true)
    images=$(echo "$output" | grep -ciE '\.(jpg|jpeg|png|gif|webp|bmp|tiff?)$' || true)
    metadata=$(echo "$output" | grep -ciE '\.json$' || true)

    # Count other files (excluding directories and summary lines)
    local total_files
    total_files=$(echo "$output" | grep -cvE '^(sending|sent|total|$|.*/$)' || true)
    other=$((total_files - audio - images - metadata))
    [[ "$other" -lt 0 ]] && other=0

    echo "$audio $images $metadata $other"
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Sync audiobooks between OpenAudible and Audiobookshelf storage.

Options:
    -n, --dry-run     Show what would be synced without making changes
    -h, --help        Show this help message

Syncs:
    1. OpenAudible downloads → Audiobookshelf audible folder
    2. Audiobookshelf library → NAS backup

EOF
    exit 0
}

# ---------- PARSE ARGS ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage ;;
        *) fail "Unknown option: $1" ;;
    esac
done

# ---------- MAIN ----------
log "Finding audiobookshelf pod..."
POD=$(kubectl get pods -n "$NAMESPACE" -l "$APP_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) \
    || fail "Could not find audiobookshelf pod in namespace '$NAMESPACE'"

[[ -z "$POD" ]] && fail "No pod found matching label '$APP_LABEL'"
log "Found pod: $POD"

# Build rsync flags
RSYNC_FLAGS="-avzu"
[[ "$DRY_RUN" -eq 1 ]] && RSYNC_FLAGS="$RSYNC_FLAGS --dry-run"

# Execute commands in pod
run_in_pod() {
    kubectl exec -n "$NAMESPACE" "$POD" -- sh -c "$1"
}

log "Ensuring rsync is installed..."
run_in_pod "command -v rsync >/dev/null 2>&1 || apk add --no-cache rsync"

log "Setting permissions on /mnt..."
run_in_pod "chown -R 568:root /mnt && chmod -R 777 /mnt"

if [[ "$DRY_RUN" -eq 1 ]]; then
    log "DRY-RUN MODE - showing what would be synced"
fi

# Initialize totals
total_audio=0 total_images=0 total_metadata=0 total_other=0

print_summary() {
    local label="$1" audio="$2" images="$3" metadata="$4" other="$5"
    local total=$((audio + images + metadata + other))
    log "  [$label] Audio: $audio | Images: $images | Metadata: $metadata | Other: $other | Total: $total"
}

log "Syncing OpenAudible → Audiobookshelf..."
output1=$(run_in_pod "rsync $RSYNC_FLAGS '$OPENAUDIBLE_SRC' '$AUDIOBOOKSHELF_AUDIBLE'" 2>&1) || true
echo "$output1"
read -r a1 i1 m1 o1 <<< "$(count_files "$output1")"
print_summary "OpenAudible → ABS" "$a1" "$i1" "$m1" "$o1"
total_audio=$((total_audio + a1))
total_images=$((total_images + i1))
total_metadata=$((total_metadata + m1))
total_other=$((total_other + o1))

log "Syncing Audiobookshelf → NAS backup..."
output2=$(run_in_pod "rsync $RSYNC_FLAGS '$AUDIOBOOKSHELF_BOOKS' '$NAS_BACKUP'" 2>&1) || true
echo "$output2"
read -r a2 i2 m2 o2 <<< "$(count_files "$output2")"
print_summary "ABS → NAS backup" "$a2" "$i2" "$m2" "$o2"
total_audio=$((total_audio + a2))
total_images=$((total_images + i2))
total_metadata=$((total_metadata + m2))
total_other=$((total_other + o2))

# Print summary
total_all=$((total_audio + total_images + total_metadata + total_other))
log "─────────────────────────────────────────"
if [[ "$DRY_RUN" -eq 1 ]]; then
    log "Summary (would sync):"
else
    log "Summary (synced):"
fi
log "  Audio files:    $total_audio"
log "  Image files:    $total_images"
log "  Metadata files: $total_metadata"
log "  Other files:    $total_other"
log "  ─────────────────"
log "  Total:          $total_all"
log "─────────────────────────────────────────"
log "Sync complete!"
