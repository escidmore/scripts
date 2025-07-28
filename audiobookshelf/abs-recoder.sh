#!/usr/bin/env bash
set -euo pipefail

# ---------- CONFIG ----------
SRC_ROOT="/mnt/cephfs/audiobooks/audiobooks"   # no trailing slash
DEST_ROOT="/tmp/audiobooks"                    # where new encodes go (same subpaths)
TARGET_GLOB='.*\.m(4[ab]|p3)$'                 # what to search for
PARALLEL=16                                    # xargs -P value
DRY_RUN=0                                      # 1=dry-run (no replace), 0=replace originals
DEBUG=0                                        # toggle debugging
FAST_PROBE=0                                   # 1 = use -analyzeduration/-probesize
STRICT_ERRORS=0                                # 1 = add -xerror (strict), 0 = omit (more tolerant)

# Niceness (CPU) and I/O priority
NICE="${NICE:-nice -n 19}"          # lowest CPU priority
IONICE="${IONICE:-ionice -c2 -n7}"  # best-effort, lowest IO priority (use -c3 for truly idle)
export NICE IONICE

# When the input extension != .m4b (e.g., .mp3), what to do with the original after install?
ON_EXT_CHANGE="delete"                         # keep | rename_old | delete

# Audio encode settings
AUDIO_BITRATE="48k"
AUDIO_CHANNELS=1

# Duration acceptance threshold (e.g., 0.98 = new must be >=98% of old)
DUR_RATIO_MIN="0.98"

# Fast probe toggle (corrected logic)
if [[ "${FAST_PROBE:-0}" -eq 1 ]]; then
  FF_FAST="-analyzeduration 2M -probesize 2M"
else
  FF_FAST=""
fi

SAVINGS_FILE="$(mktemp)"
export SRC_ROOT DEST_ROOT TARGET_GLOB PARALLEL DRY_RUN AUDIO_BITRATE AUDIO_CHANNELS DUR_RATIO_MIN FF_FAST SAVINGS_FILE ON_EXT_CHANGE DEBUG STRICT_ERRORS

# xtrace log only when DEBUG=1
if [[ "${DEBUG:-0}" -eq 1 ]]; then
  : > /tmp/abs-recoder-cmds.log
fi

FAILED_LOG="/tmp/abs-recoder-failed.log"
> "$FAILED_LOG"
export FAILED_LOG

# ---------- HELPERS ----------
get_bytes()    { stat -c %s -- "$1"; }
get_duration() { ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$1" 2>/dev/null || echo 0; }
fmt_mb()       { awk "BEGIN{printf(\"%.1f\", $1/1048576)}"; }

# Log a failure with a reason and return code
fail() {
  local reason="$1"; local f="$2"; local code="${3:-1}"
  printf "[FAILED: %s] %s\n" "$reason" "$f" | tee -a "$FAILED_LOG"
  return "$code"
}
export -f fail

process_one() {
  file="$1"

  # Skip if already Opus
  if ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of default=nw=1:nk=1 "$file" | grep -qi "^opus$"; then
    return 0
  fi

  # Validate source duration
  old_dur=$(get_duration "$file")
  awk -v d="$old_dur" 'BEGIN{exit !(d>0)}' || fail "no duration (unreadable source)" "$file" 10 || return $?

  old_bytes=$(get_bytes "$file") || fail "stat old bytes failed" "$file" 1 || return $?

  # Default per-file channel config
  AUDIO_CHANNELS_THIS="$AUDIO_CHANNELS"

  # Override to stereo for certain audiobooks if global is mono
  if [[ "$AUDIO_CHANNELS" -eq 1 ]]; then
    base_name="$(basename "$file")"
    if [[ "$base_name" =~ [Dd]ramatized ]]; then
      AUDIO_CHANNELS_THIS=2
      echo "  → Forcing stereo due to filename match: $base_name"
    else
      pub="$(ffprobe -v error -show_entries format_tags=publisher \
              -of default=nk=1:nw=1 "$file" 2>/dev/null || echo "")"
      if echo "$pub" | grep -iq "GraphicAudio"; then
        AUDIO_CHANNELS_THIS=2
        echo "  → Forcing stereo due to publisher metadata: $pub"
      fi
    fi
  fi

  # Paths / naming
  rel="${file#"$SRC_ROOT"/}"
  rel_noext="${rel%.*}"
  ext_lc="$(echo "${file##*.}" | tr '[:upper:]' '[:lower:]')"

  # We ALWAYS output .m4b (MP4 container)
  out="$DEST_ROOT/$rel_noext.m4b"
  outdir=$(dirname "$out")
  mkdir -p "$outdir" || fail "mkdir outdir failed" "$outdir" 20 || return $?

  # Detect cover image (video stream)
  has_cover="$(ffprobe -v error -select_streams v -show_entries stream=index -of csv=p=0 "$file" | head -n1 || true)"

  # Build ffmpeg maps/opts safely
  map_opts=( -map 0:a -map_chapters 0 -map_metadata 0 )
  v_opts=()
  if [[ -n "$has_cover" && "$ext_lc" == "mp3" ]]; then
    # Only include cover/disposition if a video stream exists
    map_opts=( -map 0:a -map 0:v:0 -map_chapters 0 -map_metadata 0 )
    v_opts=( -c:v copy -disposition:v:0 attached_pic )
  fi

  # Strict errors toggle
  xerr_opts=()
  [[ "${STRICT_ERRORS:-0}" -eq 1 ]] && xerr_opts=( -xerror )

  # Transcode to a temp file under /tmp, then finalize
  tmp_out="$out.enc.$$"
  printf "Encoding %s\n" "$file"
  [ -e "$tmp_out" ] && rm -f -- "$tmp_out"

  errlog="/tmp/recode_ffmpeg.$$.stderr"; : > "$errlog"

  # Helper to run encode (allows retry with extra args)
  run_encode() {
    local -a extra=( "$@" )
    if [[ "${DEBUG:-0}" -eq 1 ]]; then
      PS4='+ $(date "+%F %T") [$$] '
      {
        set -x
        $NICE $IONICE ffmpeg -y -v error "${xerr_opts[@]}" -nostdin ${FF_FAST:-} -i "$file" \
          "${map_opts[@]}" \
          -c:a libopus -ac "$AUDIO_CHANNELS" -b:a "$AUDIO_BITRATE" -vbr on -application audio -compression_level 10 \
          "${v_opts[@]}" \
          -movflags use_metadata_tags \
          "${extra[@]}" \
          -f mp4 "$tmp_out"
        rc=$?
        set +x
      } >/dev/null 2>>/tmp/abs-recoder-cmds.log 2>>"$errlog"
    else
      $NICE $IONICE ffmpeg -y -v error "${xerr_opts[@]}" -nostdin ${FF_FAST:-} -i "$file" \
        "${map_opts[@]}" \
        -c:a libopus -ac "$AUDIO_CHANNELS" -b:a "$AUDIO_BITRATE" -vbr on -application audio -compression_level 10 \
        "${v_opts[@]}" \
        -movflags use_metadata_tags \
        "${extra[@]}" \
        -f mp4 "$tmp_out" \
        >/dev/null 2>>"$errlog"
      rc=$?
    fi
    return "$rc"
  }

  # First try (normal)
  run_encode
  rc=$?

  # Retry once if muxer complains about non-monotonic DTS: normalize timestamps
  if [[ $rc -ne 0 ]] && grep -q "Non-monotonic DTS" "$errlog"; then
    : > "$errlog"
    run_encode -fflags +genpts -af aresample=async=1:first_pts=0 -avoid_negative_ts make_zero -muxpreload 0 -muxdelay 0
    rc=$?
  fi

  if [ $rc -eq 0 ]; then
    new_dur=$(get_duration "$tmp_out")
    # Require duration parity
    if ! awk -v o="$old_dur" -v n="$new_dur" -v r="$DUR_RATIO_MIN" 'BEGIN{exit !(o>0 && n/o>=r)}'; then
      rm -f -- "$tmp_out"
      tail -n 40 "$errlog" || true
      fail "duration mismatch (old=${old_dur}s new=${new_dur}s)" "$file" 12 || return $?
    fi

    # Move tmp to final /tmp path
    mkdir -p "$outdir" || { rm -f -- "$tmp_out"; fail "mkdir outdir failed (finalize)" "$outdir" 21 || return $?
    }
    $IONICE mv -f -- "$tmp_out" "$out" || { rm -f -- "$tmp_out"; fail "move tmp_out to out failed" "$file" 22 || return $?
    }

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
        # Save original attrs
        attrref="$(mktemp)"; cp -p --attributes-only -- "$file" "$attrref" 2>/dev/null || true
        if $IONICE cp -p -- "$out" "$tmp_src"; then
          if $IONICE mv -f -- "$tmp_src" "$file"; then
            chown --reference="$attrref" "$file" 2>/dev/null || true
            chmod --reference="$attrref" "$file" 2>/dev/null || true
            touch -r "$attrref" "$file" 2>/dev/null || true
          else
            rm -f -- "$tmp_src" "$attrref"
            fail "atomic replace (mv) failed" "$file" 4 || return $?
          fi
        else
          rm -f -- "$tmp_src" "$attrref"
          fail "atomic replace (cp) failed" "$file" 4 || return $?
        fi
        rm -f -- "$attrref"
      else
        # Different extension (e.g., mp3 -> m4b): install new alongside old
        dst="$SRC_ROOT/$rel_noext.m4b"
        dstdir=$(dirname "$dst")
        mkdir -p "$dstdir" || fail "mkdir dst dir failed" "$dstdir" 23 || return $?
        tmp_dst="$dst.new.$$"
        if $IONICE cp -p -- "$out" "$tmp_dst"; then
          $IONICE mv -f -- "$tmp_dst" "$dst" || { rm -f -- "$tmp_dst"; fail "place new (.m4b) failed" "$dst" 24 || return $?
          }
        else
          $IONICE rm -f -- "$tmp_dst"
          fail "copy new (.m4b) to dst failed" "$dst" 24 || return $?
        fi
        # Ensure new file matches original's owner/permissions
        chown --reference="$file" "$dst" 2>/dev/null || true
        chmod --reference="$file" "$dst" 2>/dev/null || true

        case "$ON_EXT_CHANGE" in
          keep)        : ;;
          rename_old)  $IONICE mv -f -- "$file" "${file}.old" || fail "rename old failed" "$file" 25 || return $? ;;
          delete)      rm -f -- "$file" || fail "delete old failed" "$file" 26 || return $? ;;
          *)           : ;;
        esac
      fi
    fi
  else
    # Show last errors for quick triage
    echo "ffmpeg stderr (tail) for: $file"
    tail -n 60 "$errlog" || true
    if [[ "${DEBUG:-0}" -eq 1 ]]; then
      { printf 'RUN:'; printf ' %q' $NICE $IONICE ffmpeg -v error "${xerr_opts[@]}" -nostdin ${FF_FAST:-} -i "$file" \
          "${map_opts[@]}" -c:a libopus -ac "$AUDIO_CHANNELS" -b:a "$AUDIO_BITRATE" \
          -vbr on -application audio -compression_level 10 "${v_opts[@]}" \
          -movflags use_metadata_tags -f mp4 "$tmp_out"; printf '\n'; } >> /tmp/abs-recoder-cmds.log
    fi
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
