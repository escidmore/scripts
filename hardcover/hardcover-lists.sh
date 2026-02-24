#!/usr/bin/env bash
set -euo pipefail

OUTPUT_FILE="/mnt/c/Users/eve/Downloads/hardcover-lists.txt"
API_URL="https://api.hardcover.app/v1/graphql"
USER_ID=22672
PAGE_SIZE=100

if [[ -z "${HARDCOVER_API_KEY:-}" ]]; then
  echo "Error: HARDCOVER_API_KEY is not set" >&2
  exit 1
fi

tmpfile=$(mktemp)
trap 'rm -f "$tmpfile"' EXIT

offset=0

while true; do
  payload=$(jq -n \
    --argjson uid "$USER_ID" \
    --argjson limit "$PAGE_SIZE" \
    --argjson offset "$offset" \
    '{
      "query": "query GetUserLists { lists(where: {user_id: {_eq: \($uid)}}, order_by: {updated_at: desc}, limit: \($limit), offset: \($offset)) { name } }"
    }')

  response=$(curl -sf \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${HARDCOVER_API_KEY}" \
    -d "$payload" \
    "$API_URL")

  if echo "$response" | jq -e '.errors' > /dev/null 2>&1; then
    echo "API error: $(echo "$response" | jq -r '.errors[].message')" >&2
    exit 1
  fi

  count=$(echo "$response" | jq '.data.lists | length')

  if [[ "$count" -eq 0 ]]; then
    break
  fi

  echo "$response" | jq -r '.data.lists[].name' >> "$tmpfile"
  echo "Fetched $count lists (offset $offset)..." >&2

  offset=$((offset + PAGE_SIZE))

  if [[ "$count" -lt "$PAGE_SIZE" ]]; then
    break
  fi
done

sort -u "$tmpfile" > "$OUTPUT_FILE"
echo "Done. $(wc -l < "$OUTPUT_FILE") unique lists written to $OUTPUT_FILE" >&2
