#!/usr/bin/env bash
set -euo pipefail

##############################################################################
# CONFIG – change only if your paths differ
##############################################################################
WWW_USER="www-data"
WWW_GROUP="www-data"
WEB_SERVER="nginx"              # systemd service name for your web server

CORE_BASE="/var/www"            # Where mediawiki-x.y.z dirs live
SYMLINK="${CORE_BASE}/wiki"     # Symlink all vhosts use
SITES_DIR="/var/www/sites"      # Each site: sites/<host>/{LocalSettings.php,images,…}
##############################################################################

err()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf '!! %s\n' "$*" >&2; }
info() { printf -- '-- %s\n' "$*"; }

# Preflight for required tools
for tool in curl tar jq git composer; do
  command -v "$tool" >/dev/null 2>&1 || err "Missing required tool: $tool"
done

########################## helper for permissions ############################
fix_site_perms() {
  local site="$1"
  local dir="${SITES_DIR}/${site}"
  # Only chown expected writable areas; avoid clobbering strange mounts
  chown -R "$WWW_USER:$WWW_GROUP" "$dir/images" "$dir/cache" "$dir/extensions" "$dir/skins" 2>/dev/null || true
  # Ensure directory/file modes are sane for web use
  find "$dir/images" "$dir/cache" "$dir/extensions" "$dir/skins" -type d -exec chmod 755 {} \; 2>/dev/null || true
  find "$dir/images" "$dir/cache" "$dir/extensions" "$dir/skins" -type f -exec chmod 644 {} \; 2>/dev/null || true
}

############################ php-fpm auto-detect #############################
detect_php_fpm() {
  local bins=()
  IFS=$'\n' read -r -d '' -a bins < <(command -v -a php-fpm* 2>/dev/null | sort -u && printf '\0' || true)
  if (( ${#bins[@]} == 0 )); then
    for b in /usr/sbin/php-fpm* /usr/bin/php-fpm*; do [[ -x "$b" ]] && bins+=("$b"); done
  fi
  (( ${#bins[@]} == 0 )) && err "No php-fpm binary found"

  if (( ${#bins[@]} == 1 )); then
    PHP_FPM_BIN="${bins[0]}"
  else
    PHP_FPM_BIN="$(printf '%s\n' "${bins[@]}" | sort -V | tail -n1)"
  fi

  local base svc1 svc2
  base="$(basename "$PHP_FPM_BIN")"
  svc1="$base"
  svc2="${base/php-fpm/php}-fpm"
  if systemctl list-unit-files | grep -q "^${svc1}\.service"; then
    PHP_FPM_SERVICE="$svc1"
  elif systemctl list-unit-files | grep -q "^${svc2}\.service"; then
    PHP_FPM_SERVICE="$svc2"
  elif systemctl list-unit-files | grep -q "^php-fpm\.service"; then
    PHP_FPM_SERVICE="php-fpm"
  else
    PHP_FPM_SERVICE="$(systemctl list-unit-files | awk -F. '/^php[0-9]+\.[0-9]+-fpm\.service$/ {print $1}' | sort -V | tail -n1)"
    [[ -z "$PHP_FPM_SERVICE" ]] && PHP_FPM_SERVICE="php8.2-fpm"
  fi
}
detect_php_fpm
info "Detected PHP-FPM: binary=$PHP_FPM_BIN, service=$PHP_FPM_SERVICE"

############################ current version #################################
current_version="0.0.0"
if [[ -L "$SYMLINK" ]]; then
  if [[ "$(readlink -f "$SYMLINK")" =~ mediawiki-([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    current_version="${BASH_REMATCH[1]}"
  fi
fi
printf 'Current MediaWiki core: %s\n' "$current_version"

############################ prompt & validate ###############################
read -rp "Enter new MediaWiki version (e.g. 1.44.0): " MWVERSION
[[ "$MWVERSION" =~ ^1\.[0-9]{2}\.[0-9]+$ ]] || err "Version must match 1.xx.x"

vernum() { awk -F. '{printf "%03d%03d%03d\n",$1,$2,$3}' <<<"$1"; }
if [[ "$current_version" != "0.0.0" ]]; then
  [[ "$(vernum "$MWVERSION")" -le "$(vernum "$current_version")" ]] && \
    err "New version ($MWVERSION) is not newer than current ($current_version)"
fi

MWVSHORT="$(cut -d. -f1-2 <<<"$MWVERSION")"           # 1.44
MWVSHORTUS="${MWVSHORT/./_}"                          # 1_44
REL_BRANCH="REL${MWVSHORTUS}"                         # REL1_44
TARBALL="mediawiki-${MWVERSION}.tar.gz"
URL="https://releases.wikimedia.org/mediawiki/${MWVSHORT}/${TARBALL}"

################################ backups #####################################
if command -v /root/.local/bin/borgmatic >/dev/null 2>&1; then
  echo
  read -rp "Run borgmatic backup now before upgrade? (Y/n): " do_borg
  do_borg="${do_borg:-Y}"
  if [[ "${do_borg,,}" == "y" ]]; then
    info "Starting borgmatic backup (this may take a while)…"
    /root/.local/bin/borgmatic --verbosity 1 --stats || err "borgmatic backup failed; aborting upgrade"
    info "borgmatic backup completed"
  else
    warn "Skipping borgmatic backup at user request"
  fi
else
  warn "borgmatic not found at /root/.local/bin/borgmatic"
  read -rp "Continue upgrade without a fresh backup? (y/N): " do_upgrade
  if [[ "${do_upgrade,,}" != "y" ]]; then
    err "Quitting at user request."
  fi
fi

############################ download & extract ##############################
cd "$CORE_BASE"
info "Downloading $URL"
curl -fLS --retry 3 -o "$TARBALL" "$URL" || err "Download failed: $URL"
[[ -s "$TARBALL" ]] || err "Downloaded file empty"
if command -v file >/dev/null 2>&1; then
  file "$TARBALL" | grep -qi gzip || err "Downloaded file is not gzip"
fi
info "Extracting $TARBALL"
tar xzf "$TARBALL"
rm -f "$TARBALL"
NEW_CORE="${CORE_BASE}/mediawiki-${MWVERSION}"
[[ -d "$NEW_CORE" ]] || err "Extraction did not produce $NEW_CORE"

# perms
chown -R root:root "$NEW_CORE"
find "$NEW_CORE" -type d -exec chmod 755 {} \;
find "$NEW_CORE" -type f -exec chmod 644 {} \;

############################ composer merge-plugin ###########################
cp -a "$NEW_CORE/composer.json" "$NEW_CORE/composer.json.bak.$(date +%F-%H%M%S)"
jq '
  .extra = (.extra // {}) |
  .extra["merge-plugin"] = {
    "include": ["/var/www/sites/*/composer.local.json"],
    "recurse": false,
    "merge-dev": false,
    "replace": false,
    "ignore-duplicates": true
  }' "$NEW_CORE/composer.json" > "$NEW_CORE/composer.json.tmp"
mv "$NEW_CORE/composer.json.tmp" "$NEW_CORE/composer.json"
chown "$WWW_USER:$WWW_GROUP" "$NEW_CORE/composer.json"

############################ switch symlink & composer #######################
info "Linking $SYMLINK -> $NEW_CORE"
ln -sfn "$NEW_CORE" "$SYMLINK"
cd "$SYMLINK"
info "composer install (no-dev) in $SYMLINK"
if ! sudo -u "$WWW_USER" composer install --no-dev --no-progress --prefer-dist --no-interaction; then
  err "composer install failed in $SYMLINK"
fi
if ! sudo -u "$WWW_USER" composer dump-autoload -o; then
  warn "composer dump-autoload reported issues"
fi
# Optional check
if ! sudo -u "$WWW_USER" composer validate; then
  warn "composer.json warnings (expected with pinned versions)"
fi

############################ overlay extension sync #########################
sync_overlay_extensions_for_site() {
  local site="$1" rel="$2"
  local dir="${SITES_DIR}/${site}/extensions"
  mkdir -p "$dir"
  shopt -s nullglob
  for path in "$dir"/*; do
    [[ -d "$path" ]] || continue
    local ext="$(basename "$path")"
    local url="https://github.com/wikimedia/mediawiki-extensions-${ext}"
    info "[$site] syncing $ext -> $rel"
    if [[ -d "$path/.git" ]]; then
      sudo -u "$WWW_USER" git -C "$path" remote get-url origin >/dev/null 2>&1 || git -C "$path" remote add origin "$url" || warn "could not set remote for $ext"
      if ! sudo -u "$WWW_USER" git -C "$path" fetch --all --prune; then warn "fetch failed for $ext ($site)"; continue; fi
      if sudo -u "$WWW_USER" git -C "$path" rev-parse --verify -q "origin/$rel" >/dev/null; then
        sudo -u "$WWW_USER" git -C "$path" checkout -q "$rel" || git -C "$path" checkout -q -t "origin/$rel" || warn "checkout $rel failed for $ext"
      elif sudo -u "$WWW_USER" git -C "$path" rev-parse --verify -q origin/master >/dev/null; then
        warn "$ext: $rel not found, using master"
        sudo -u "$WWW_USER" git -C "$path" checkout -q master || warn "checkout master failed for $ext"
      elif sudo -u "$WWW_USER" git -C "$path" rev-parse --verify -q origin/main >/dev/null; then
        warn "$ext: $rel not found, using main"
        sudo -u "$WWW_USER" git -C "$path" checkout -q main || warn "checkout main failed for $ext"
      else
        warn "$ext: no $rel/main/master branch found; skipping checkout"
      fi
      if ! sudo -u "$WWW_USER" git -C "$path" pull --ff-only; then
        warn "sudo -u "$WWW_USER" git pull failed for $ext ($site)"
      fi
    else
      warn "$ext in $site is not a git repo; skipping"
    fi
    if [[ -f "$path/composer.json" ]]; then
      if ! ( cd "$path" && sudo -u "$WWW_USER" composer install --no-dev --no-progress --prefer-dist --no-interaction ); then
        warn "composer install failed for $ext ($site)"
      fi
    fi
  done
  shopt -u nullglob
}

# Sync overlays for all detected sites
shopt -s nullglob
all_sites=( "$(basename -a "$SITES_DIR"/* 2>/dev/null || true)" )
for site in "${all_sites[@]}"; do
  if [[ -f "$SITES_DIR/$site/LocalSettings.php" ]]; then
    sync_overlay_extensions_for_site "$site" "$REL_BRANCH"
    fix_site_perms "$site"
  fi
done
shopt -u nullglob

############################ reload services ################################
systemctl reload "$PHP_FPM_SERVICE" || warn "Failed to reload $PHP_FPM_SERVICE"
systemctl reload "$WEB_SERVER"      || warn "Failed to reload $WEB_SERVER"

############################ optional DB updates ############################
echo
read -rp "Run database updater now? (y/N): " resp
[[ "${resp,,}" != "y" ]] && { echo "All done (core upgrade only)."; exit 0; }

shopt -s nullglob
sites=( "$SITES_DIR"/*/LocalSettings.php )
(( ${#sites[@]} == 0 )) && err "No LocalSettings.php found under $SITES_DIR"

echo "Detected sites:"
i=1; declare -A idx
for f in "${sites[@]}"; do
  site="$(basename "$(dirname "$f")")"
  printf '  %s) %s\n' "$i" "$site"
  idx[$i]="$f"; ((i++))
done
echo "  a) All"

read -rp "Choose site number(s) (comma/space-separated) or 'a': " choice
choice="${choice//,/ }"

run_updater() {
  local conf="$1"
  local site
  site="$(basename "$(dirname "$conf")")"
  info "Updating DB for $site"
  if ! sudo -u "$WWW_USER" php "$SYMLINK/maintenance/run.php" update --conf "$conf" --quick --doshared; then
    warn "DB update failed for $site"
  fi
  if ! sudo -u "$WWW_USER" php "$SYMLINK/maintenance/run.php" runJobs --conf "$conf" --maxjobs 300; then
    warn "runJobs failed for $site"
  fi
  fix_site_perms "$site"
}

if [[ "$choice" == "a" ]]; then
  for f in "${sites[@]}"; do run_updater "$f"; done
else
  for sel in $choice; do
    [[ -n "${idx[$sel]:-}" ]] && run_updater "${idx[$sel]}" || warn "Invalid selection: $sel"
  done
fi

echo "Upgrade complete: MediaWiki $MWVERSION live."