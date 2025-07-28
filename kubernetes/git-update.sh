#!/bin/bash

set -euo pipefail

# Step 1: Show what’s changed
echo "📂 Git status:"
git status -s

read -p "🔍 View full staged diff? [y/N] " show_diff
if [[ "$show_diff" =~ ^[Yy]$ ]]; then
  echo "📜 Staged diff:"
  git diff --cached || true
fi

# Step 2: Determine commit message
if [[ $# -gt 0 ]]; then
  # Commit message passed via CLI
  msg="$*"
else
  echo "📝 No commit message provided — opening \$EDITOR..."

  tmp_msg=$(mktemp)
  trap 'rm -f "$tmp_msg"' EXIT

  {
    echo ""
    echo "# Write your commit message above. Lines starting with # will be ignored."
    echo "#"
    git diff --cached --name-status | sed 's/^/# /'
  } > "$tmp_msg"

  ${EDITOR:-vim} "$tmp_msg"

  # Read actual message, skipping comments and blank lines
  msg=$(grep -vE '^\s*#' "$tmp_msg" | sed '/^\s*$/d')

  if [[ -z "$msg" ]]; then
    echo "❌ Empty commit message. Aborting."
    exit 1
  fi
fi

# Step 3: Stage everything and commit
echo "📦 Staging all changes..."
git add .

echo "✅ Committing with message:"
echo "$msg"
git commit -m "$msg"

# Step 4: Push and reconcile
echo "⤴️  Pushing to origin..."
git push

echo "🔁 Reconciling Flux Git source..."
flux reconcile source git cluster

echo "🔁 Reconciling Flux system kustomization..."
flux reconcile kustomization flux -n flux-system --with-source

echo "✅ All done!"
