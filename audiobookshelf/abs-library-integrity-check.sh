#!/usr/bin/env bash
set -euo pipefail

# ---------- CONFIG ----------
ROOT="/mnt/cephfs/audiobooks/audiobooks"
TARGET_GLOB='.*\.m(4[ab]|p3)$'
PARALLEL=8
OP_TIMEOUT=20
OUT_DIR="/tmp/abs-integrity-$(date +%Y%m%d-%H%M%S)"

# ---------- FUNCTIONS ----------
log() { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Read-only integrity scan for audiobook files.

Checks each matching audio file for:
  - zero-byte size
  - stat timeout/failure (metadata read issues)
  - ffprobe unreadable input (header/container parse failures)

Options:
  -r, --root PATH          Library root to scan (default: $ROOT)
  -g, --target-glob REGEX  POSIX-extended find -iregex (default: $TARGET_GLOB)
  -p, --parallel N         Parallel workers (default: $PARALLEL)
  -t, --timeout SEC        Per-op timeout seconds (default: $OP_TIMEOUT)
  -o, --out-dir DIR        Report directory (default: timestamped /tmp dir)
  -h, --help               Show this help

Output files in out-dir:
  - problems.tsv           reason<TAB>path<TAB>detail
  - zero-byte.txt
  - stat-failures.txt
  - ffprobe-unreadable.txt
  - summary.txt

EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -r|--root) ROOT="$2"; shift 2 ;;
        -g|--target-glob) TARGET_GLOB="$2"; shift 2 ;;
        -p|--parallel) PARALLEL="$2"; shift 2 ;;
        -t|--timeout) OP_TIMEOUT="$2"; shift 2 ;;
        -o|--out-dir) OUT_DIR="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) fail "Unknown option: $1" ;;
    esac
done

[[ -d "$ROOT" ]] || fail "Root directory does not exist: $ROOT"
[[ "$PARALLEL" =~ ^[1-9][0-9]*$ ]] || fail "--parallel must be a positive integer"
[[ "$OP_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail "--timeout must be a positive integer"

mkdir -p "$OUT_DIR"

PROBLEMS="$OUT_DIR/problems.tsv"
ZERO_BYTE="$OUT_DIR/zero-byte.txt"
STAT_FAIL="$OUT_DIR/stat-failures.txt"
FFPROBE_FAIL="$OUT_DIR/ffprobe-unreadable.txt"
SUMMARY="$OUT_DIR/summary.txt"
LOCKFILE="$OUT_DIR/.write.lock"

: > "$PROBLEMS"
: > "$ZERO_BYTE"
: > "$STAT_FAIL"
: > "$FFPROBE_FAIL"
: > "$SUMMARY"
: > "$LOCKFILE"

log_problem() {
    local reason="$1"
    local path="$2"
    local detail="$3"
    {
        flock 200
        printf "%s\t%s\t%s\n" "$reason" "$path" "$detail" >> "$PROBLEMS"
        case "$reason" in
            zero-byte) printf "%s\n" "$path" >> "$ZERO_BYTE" ;;
            stat-failed|stat-timeout) printf "%s\n" "$path" >> "$STAT_FAIL" ;;
            ffprobe-unreadable|ffprobe-timeout) printf "%s\n" "$path" >> "$FFPROBE_FAIL" ;;
            *) : ;;
        esac
    } 200>>"$LOCKFILE"
}

check_one() {
    local f="$1"

    local size
    local stat_output
    local rc=0
    stat_output=$(timeout "$OP_TIMEOUT" stat -c '%s' -- "$f" 2>&1) || rc=$?
    if [[ $rc -ne 0 ]]; then
        if [[ $rc -eq 124 ]]; then
            log_problem "stat-timeout" "$f" "timeout=${OP_TIMEOUT}s"
        else
            log_problem "stat-failed" "$f" "$stat_output"
        fi
        return 0
    fi

    size="$stat_output"
    if [[ "$size" =~ ^[0-9]+$ ]] && [[ "$size" -eq 0 ]]; then
        log_problem "zero-byte" "$f" "size=0"
        return 0
    fi

    local probe_output
    rc=0
    probe_output=$(timeout "$OP_TIMEOUT" ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 -- "$f" 2>&1) || rc=$?
    if [[ $rc -ne 0 ]]; then
        if [[ $rc -eq 124 ]]; then
            log_problem "ffprobe-timeout" "$f" "timeout=${OP_TIMEOUT}s"
        else
            probe_output=$(printf "%s" "$probe_output" | tr '\n' ' ' | tr '\t' ' ')
            log_problem "ffprobe-unreadable" "$f" "$probe_output"
        fi
        return 0
    fi

    if ! awk -v d="$probe_output" 'BEGIN{exit !(d>0)}'; then
        log_problem "ffprobe-no-duration" "$f" "duration=$probe_output"
    fi
}

export OP_TIMEOUT PROBLEMS ZERO_BYTE STAT_FAIL FFPROBE_FAIL SUMMARY LOCKFILE
export -f log_problem check_one

log "Scanning root: $ROOT"
log "Target regex: $TARGET_GLOB"
log "Parallel workers: $PARALLEL"
log "Per-op timeout: ${OP_TIMEOUT}s"
log "Writing reports to: $OUT_DIR"

find "$ROOT" -type f -regextype posix-extended -iregex "$TARGET_GLOB" -print0 |
  xargs -0 -P "$PARALLEL" -I{} bash -c 'check_one "$@"' _ {}

total_files=$(find "$ROOT" -type f -regextype posix-extended -iregex "$TARGET_GLOB" | wc -l)
total_problems=$(wc -l < "$PROBLEMS")
zero_count=$(wc -l < "$ZERO_BYTE")
stat_fail_count=$(wc -l < "$STAT_FAIL")
ffprobe_fail_count=$(wc -l < "$FFPROBE_FAIL")

{
    printf "root=%s\n" "$ROOT"
    printf "target_glob=%s\n" "$TARGET_GLOB"
    printf "parallel=%s\n" "$PARALLEL"
    printf "timeout_seconds=%s\n" "$OP_TIMEOUT"
    printf "total_files_scanned=%s\n" "$total_files"
    printf "total_problem_rows=%s\n" "$total_problems"
    printf "zero_byte_files=%s\n" "$zero_count"
    printf "stat_failures=%s\n" "$stat_fail_count"
    printf "ffprobe_unreadable_or_timeout=%s\n" "$ffprobe_fail_count"
} > "$SUMMARY"

log "Scan complete."
log "Total files scanned: $total_files"
log "Problem rows: $total_problems"
log "  Zero-byte: $zero_count"
log "  Stat failures/timeouts: $stat_fail_count"
log "  ffprobe unreadable/timeouts: $ffprobe_fail_count"
log "See: $SUMMARY"
