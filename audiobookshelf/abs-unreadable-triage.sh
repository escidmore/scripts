#!/usr/bin/env bash
set -euo pipefail

# ---------- CONFIG ----------
INPUT_LIST=""
OUT_DIR="/tmp/abs-triage-$(date +%Y%m%d-%H%M%S)"
OP_TIMEOUT=120
KEEP_SALVAGE=0

# ---------- FUNCTIONS ----------
log() { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $(basename "$0") -i FILE [OPTIONS]

Read-only triage for files previously identified as ffprobe-unreadable.

For each input path, this script:
  1) Re-checks readability with ffprobe
  2) If unreadable, attempts non-destructive salvage remux with ffmpeg (-c copy)
  3) Categorizes each file into report lists

Required:
  -i, --input FILE         Text file containing one absolute file path per line

Options:
  -o, --out-dir DIR        Output report directory (default: timestamped /tmp dir)
  -t, --timeout SEC        Timeout per ffprobe/ffmpeg operation (default: $OP_TIMEOUT)
  -k, --keep-salvage       Keep successful salvage outputs under out-dir/salvage/
  -h, --help               Show this help

Outputs:
  - triage.tsv                     reason<TAB>path<TAB>detail
  - recoverable-by-remux.txt       unreadable originals that remux successfully
  - unrecoverable-replace.txt      unreadable originals that remux fails on
  - currently-readable.txt         now readable by ffprobe
  - missing-files.txt              paths that no longer exist
  - summary.txt                    aggregate counts

Notes:
  - Originals are never modified.
  - Salvage tests write to temporary files in out-dir and are deleted unless --keep-salvage is set.

EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--input) INPUT_LIST="$2"; shift 2 ;;
        -o|--out-dir) OUT_DIR="$2"; shift 2 ;;
        -t|--timeout) OP_TIMEOUT="$2"; shift 2 ;;
        -k|--keep-salvage) KEEP_SALVAGE=1; shift ;;
        -h|--help) usage ;;
        *) fail "Unknown option: $1" ;;
    esac
done

[[ -n "$INPUT_LIST" ]] || fail "--input is required"
[[ -f "$INPUT_LIST" ]] || fail "Input file not found: $INPUT_LIST"
[[ "$OP_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail "--timeout must be a positive integer"

command -v ffprobe >/dev/null 2>&1 || fail "ffprobe is required"
command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg is required"

mkdir -p "$OUT_DIR"
mkdir -p "$OUT_DIR/salvage"

TRIAGE="$OUT_DIR/triage.tsv"
RECOVERABLE="$OUT_DIR/recoverable-by-remux.txt"
UNRECOVERABLE="$OUT_DIR/unrecoverable-replace.txt"
READABLE="$OUT_DIR/currently-readable.txt"
MISSING="$OUT_DIR/missing-files.txt"
SUMMARY="$OUT_DIR/summary.txt"

: > "$TRIAGE"
: > "$RECOVERABLE"
: > "$UNRECOVERABLE"
: > "$READABLE"
: > "$MISSING"
: > "$SUMMARY"

record() {
    local reason="$1"
    local path="$2"
    local detail="$3"

    printf "%s\t%s\t%s\n" "$reason" "$path" "$detail" >> "$TRIAGE"
    case "$reason" in
        recoverable-by-remux) printf "%s\n" "$path" >> "$RECOVERABLE" ;;
        unrecoverable-replace) printf "%s\n" "$path" >> "$UNRECOVERABLE" ;;
        currently-readable) printf "%s\n" "$path" >> "$READABLE" ;;
        missing-file) printf "%s\n" "$path" >> "$MISSING" ;;
        *) : ;;
    esac
}

sanitize_detail() {
    printf "%s" "$1" | tr '\n' ' ' | tr '\t' ' ' | sed 's/[[:space:]]\+/ /g'
}

process_one() {
    local f="$1"
    [[ -n "$f" ]] || return 0

    if [[ ! -e "$f" ]]; then
        record "missing-file" "$f" "path does not exist"
        return 0
    fi

    local probe_out=""
    local rc=0
    probe_out=$(timeout "$OP_TIMEOUT" ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 -- "$f" 2>&1) || rc=$?
    if [[ $rc -eq 0 ]]; then
        if awk -v d="$probe_out" 'BEGIN{exit !(d>0)}'; then
            record "currently-readable" "$f" "duration=$probe_out"
            return 0
        fi
    fi

    local ext="${f##*.}"
    ext="${ext,,}"
    local base
    base=$(basename "$f")
    local tmp_out="$OUT_DIR/salvage/${base}.salvage.$$.$RANDOM.$ext"

    local salvage_out=""
    rc=0
    salvage_out=$(timeout "$OP_TIMEOUT" ffmpeg -nostdin -v error -y -i "$f" -map 0:a -c:a copy -vn -sn -dn "$tmp_out" 2>&1) || rc=$?

    if [[ $rc -eq 0 ]] && [[ -s "$tmp_out" ]]; then
        local new_size
        new_size=$(stat -c '%s' -- "$tmp_out" 2>/dev/null || echo 0)
        record "recoverable-by-remux" "$f" "salvage_size=$new_size"
        if [[ $KEEP_SALVAGE -eq 0 ]]; then
            rm -f -- "$tmp_out"
        fi
        return 0
    fi

    rm -f -- "$tmp_out" 2>/dev/null || true
    if [[ $rc -eq 124 ]]; then
        record "unrecoverable-replace" "$f" "ffmpeg timeout ${OP_TIMEOUT}s"
    else
        salvage_out=$(sanitize_detail "$salvage_out")
        [[ -n "$salvage_out" ]] || salvage_out="ffmpeg remux failed"
        record "unrecoverable-replace" "$f" "$salvage_out"
    fi
}

log "Input list: $INPUT_LIST"
log "Output dir: $OUT_DIR"
log "Timeout per operation: ${OP_TIMEOUT}s"

while IFS= read -r f || [[ -n "$f" ]]; do
    process_one "$f"
done < "$INPUT_LIST"

total_input=$(wc -l < "$INPUT_LIST")
total_rows=$(wc -l < "$TRIAGE")
recoverable_count=$(wc -l < "$RECOVERABLE")
unrecoverable_count=$(wc -l < "$UNRECOVERABLE")
readable_count=$(wc -l < "$READABLE")
missing_count=$(wc -l < "$MISSING")

{
    printf "input_list=%s\n" "$INPUT_LIST"
    printf "out_dir=%s\n" "$OUT_DIR"
    printf "timeout_seconds=%s\n" "$OP_TIMEOUT"
    printf "total_input_paths=%s\n" "$total_input"
    printf "total_triage_rows=%s\n" "$total_rows"
    printf "recoverable_by_remux=%s\n" "$recoverable_count"
    printf "unrecoverable_replace=%s\n" "$unrecoverable_count"
    printf "currently_readable=%s\n" "$readable_count"
    printf "missing_files=%s\n" "$missing_count"
} > "$SUMMARY"

log "Triage complete."
log "  Recoverable by remux: $recoverable_count"
log "  Unrecoverable replace: $unrecoverable_count"
log "  Currently readable: $readable_count"
log "  Missing files: $missing_count"
log "See: $SUMMARY"
