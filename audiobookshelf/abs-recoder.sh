#!/usr/bin/env bash
set -euo pipefail

# ---------- CONFIG ----------
SRC_ROOT="/mnt/cephfs/audiobooks/audiobooks"   # no trailing slash
DEST_ROOT="/tmp/audiobooks"                    # where new encodes go (same subpaths)
TARGET_GLOB='.*\.m(4b|p3)$'                    # what to search for
PARALLEL=16                                    # xargs -P value
DRY_RUN=0                                      # 1=dry-run (no replace), 0=replace originals

# Niceness (CPU) and I/O priority
NICE="${NICE:-nice -n 19}"          # lowest CPU priority
IONICE="${IONICE:-ionice -c2 -n7}"  # best-effort, lowest IO priority (use -c3 for truly idle)
export NICE IONICE

# When the input extension != .m4b (e.g., .mp3), what to do with the original after install?
ON_EXT_CHANGE="delete"                         # keep | rename_old | delete
#   keep        -> leave original (e.g., .mp3) in place
#   rename_old  -> move original to "<name>.old"
#   delete      -> remove original file (DANGEROUS)

# Audio encode settings
AUDIO_BITRATE="48k"
AUDIO_CHANNELS=1

# Duration acceptance threshold (e.g., 0.98 = new must be >=98% of old)
DUR_RATIO_MIN="0.98"

# Fast probe (speeds up startup on clean files)
FF_FAST="-analyzeduration 5M -probesize 5M"

SAVINGS_FILE="$(mktemp)"
export SRC_ROOT DEST_ROOT TARGET_GLOB PARALLEL DRY_RUN AUDIO_BITRATE AUDIO_CHANNELS DUR_RATIO_MIN FF_FAST SAVINGS_FILE ON_EXT_CHANGE

FAILED_LOG="/tmp/abs-recoder-failed.log"
> "$FAILED_LOG"  # clear it at start
export FAILED_LOG

# ---------- HELPERS ----------
get_bytes()    { stat -c %s -- "$1"; }
get_duration() { ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$1" 2>/dev/null || echo 0; }
fmt_mb()       { awk "BEGIN{printf(\"%.1f\", $1/1048576)}"; }

# Log a failure with a reason and return code
fail() {
  # usage: fail "reason" "file" [code]
  local reason="$1"; local f="$2"; local code="${3:-1}"
  printf "[FAILED: %s] %s\n" "$reason" "$f" >> "$FAILED_LOG"
  return "$code"
}
export -f fail

process_one() {
  file="$1"

  # Skip if already Opus
  if ffprobe -v error -select_streams a:0 -show_entries stream=codec_name \
             -of default=nw=1:nk=1 "$file" | grep -qi "^opus$"; then
    return 0
  fi

  # Validate source duration
  old_dur=$(get_duration "$file")
  awk -v d="$old_dur" 'BEGIN{exit !(d>0)}' || fail "no duration (unreadable source)" "$file" 10 || return $?

  old_bytes=$(get_bytes "$file") || fail "stat old bytes failed" "$file" 1 || return $?

  # Paths / naming
  rel="${file#"$SRC_ROOT"/}"                          # path relative to SRC_ROOT
  rel_noext="${rel%.*}"                               # strip extension
  ext_lc="$(echo "${file##*.}" | tr '[:upper:]' '[:lower:]')"

  # We ALWAYS output .m4b (MP4 container)
  out="$DEST_ROOT/$rel_noext.m4b"
  outdir=$(dirname "$out")
  mkdir -p "$outdir" || fail "mkdir outdir failed" "$outdir" 20 || return $?

  # Mapping: always copy audio, chapters, and global metadata
  # For MP3 sources, also try to carry over cover art (attached_pic) -> MP4 covr
  KEEP_ART=0
  if [[ "$ext_lc" == "mp3" ]]; then
    KEEP_ART=1
  fi

  # Build ffmpeg options safely
  # (no arrays here to keep it POSIX-ish inside xargs bash -c environments)
  map_opts=( -map 0:a -map_chapters 0 -map_metadata 0 )
  v_opts=()
  if [[ "$KEEP_ART" -eq 1 ]]; then
    # Copy attached picture if present; keep it flagged as cover
    map_opts=( -map 0:a -map 0:v? -map_chapters 0 -map_metadata 0 )
    v_opts=( -c:v copy -disposition:v:0 attached_pic )
  fi

  # Transcode to a temp file under /tmp, then finalize
  tmp_out="$out.enc.$$"
  if $NICE $IONICE ffmpeg -v error -xerror -nostdin $FF_FAST -i "$file" \
            "${map_opts[@]}" \
            -c:a libopus -ac "$AUDIO_CHANNELS" -b:a "$AUDIO_BITRATE" \
            -vbr on -application audio -compression_level 10 \
            "${v_opts[@]}" \
            -movflags use_metadata_tags \
            -f mp4 "$tmp_out" >/dev/null 2>&1; then

    new_dur=$(get_duration "$tmp_out")
    # Require duration parity
    if ! awk -v o="$old_dur" -v n="$new_dur" -v r="$DUR_RATIO_MIN" 'BEGIN{exit !(o>0 && n/o>=r)}'; then
      rm -f -- "$tmp_out"
      fail "duration mismatch (old=${old_dur}s new=${new_dur}s)" "$file" 12 || return $?
    fi

    # Move tmp to final /tmp path
    mkdir -p "$outdir" || { rm -f -- "$tmp_out"; fail "mkdir outdir failed (finalize)" "$outdir" 21 || return $?; }
    $IONICE mv -f -- "$tmp_out" "$out" || { rm -f -- "$tmp_out"; fail "move tmp_out to out failed" "$file" 22 || return $?; }

    new_bytes=$(get_bytes "$out") || fail "stat new bytes failed" "$out" 2 || return $?

    # Report per-file sizes
    old_mb=$(fmt_mb "$old_bytes")
    new_mb=$(fmt_mb "$new_bytes")
    saved_mb=$(fmt_mb "$((old_bytes - new_bytes))")
    printf "Old: %sMB  New: %sMB  Saved: %sMB  %s\n" "$old_mb" "$new_mb" "$saved_mb" "$file"

    printf "%s %s\n" "$old_bytes" "$new_bytes" >> "$SAVINGS_FILE"

    # Install into source (if not dry-run)
    if [ "${DRY_RUN:-0}" -eq 0 ]; then
      if [[ "$ext_lc" == "m4b" ]]; then
        # Same extension: atomic in-place replace
        tmp_src="$file.new.$$"
        if $IONICE cp -p -- "$out" "$tmp_src"; then
          $IONICE mv -f -- "$tmp_src" "$file" || { rm -f -- "$tmp_src"; fail "atomic replace (mv) failed" "$file" 4 || return $?; }
        else
          rm -f -- "$tmp_src"
          fail "atomic replace (cp) failed" "$file" 4 || return $?
        fi
      else
        # Different extension (e.g., mp3 -> m4b): install new alongside old
        dst="$SRC_ROOT/$rel_noext.m4b"
        dstdir=$(dirname "$dst")
        mkdir -p "$dstdir" || fail "mkdir dst dir failed" "$dstdir" 23 || return $?
        tmp_dst="$dst.new.$$"
        if $IONICE cp -p -- "$out" "$tmp_dst"; then
          $IONICE mv -f -- "$tmp_dst" "$dst" || { rm -f -- "$tmp_dst"; fail "place new (.m4b) failed" "$dst" 24 || return $?; }
        else
          $IONICE rm -f -- "$tmp_dst"
          fail "copy new (.m4b) to dst failed" "$dst" 24 || return $?
        fi

        # Optional original handling when extension changed
        case "$ON_EXT_CHANGE" in
          keep)        : ;;
          rename_old)  $IONICE mv -f -- "$file" "${file}.old" || fail "rename old failed" "$file" 25 || return $? ;;
          delete)      rm -f -- "$file" || fail "delete old failed" "$file" 26 || return $? ;;
          *)           : ;;
        esac
      fi
    fi
  else
    rm -f -- "$tmp_out" 2>/dev/null || true
    fail "ffmpeg transcode failed" "$file" 5 || return $?
  fi
}

export -f process_one fmt_mb get_duration get_bytes fail

# ---------- RUN ----------
find "$SRC_ROOT" -type f -regextype posix-extended -iregex "$TARGET_GLOB" -print0 | \
  $NICE $IONICE xargs -0 -P "$PARALLEL" -I{} bash -c 'process_one "$@"' _ {}

# ---------- TOTALS ----------
awk '
  { old += $1; new += $2 }
  END {
    saved = old - new
    pct = (old > 0) ? (saved * 100.0 / old) : 0
    printf("\nTOTAL  Old: %.2fGB  New: %.2fGB  Saved: %.2fGB  (%.1f%%)\n",
           old/1073741824, new/1073741824, saved/1073741824, pct)
  }
' "$SAVINGS_FILE" || true

# ---------- FAILURES SUMMARY ----------
if [[ -s "$FAILED_LOG" ]]; then
  echo -e "\nFailures:"
  cat "$FAILED_LOG"
  echo -e "\nFailure summary (by reason):"
  awk -F'[]] ' '{r=$1; sub(/^\[FAILED: /,"",r); c[r]++} END{for (k in c) printf("  %-35s %d\n", k, c[k])}' "$FAILED_LOG"
else
  echo -e "\nNo failures recorded."
fi

rm -f "$SAVINGS_FILE"
