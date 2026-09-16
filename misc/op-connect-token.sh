#!/usr/bin/env bash
# Extract the bare 1Password Connect JWT from ~/.secrets/op-connect.
#
# The user keeps that file as a pseudo-shell-export snippet (the same form
# 1Password's admin UI shows on token issue):
#     export OP_CONNECT_HOST = "op.samesies.gay"
#     export OP_CONNECT_TOKEN = "<jwt>"
# It is not a valid `source`-able script (spaces around `=`), so we extract
# the token by splitting on `"`. Stdout: the bare JWT, no trailing newline.
set -euo pipefail

token_file="${OP_CONNECT_TOKEN_FILE:-$HOME/.secrets/op-connect}"

if [ ! -r "$token_file" ]; then
  exit 0
fi

awk -F'"' '/OP_CONNECT_TOKEN/ {printf "%s", $2; exit}' "$token_file"
