# hardcover-tagger

Add a book to multiple [Hardcover](https://hardcover.app) lists in one command.

Given a book slug and a set of list names (existing and/or new), it resolves the book, creates any missing lists, adds the book to all of them, and updates the local list inventory.

## Install

```bash
uv tool install /path/to/scripts/hardcover/hardcover-tagger
```

Requires `HARDCOVER_API_KEY` in your environment.

## Usage

```bash
# Add to existing lists
hardcover-tagger <slug> --existing "Authors: PoC" "Genre: Fantasy"

# Create new lists and add
hardcover-tagger <slug> --new "Genre: Cyberpunk" "Themes: Displacement"

# Mix existing and new
hardcover-tagger the-obake-code \
  --existing "Authors: Indigenous" "Setting: Space" \
  --new "Authors: Kānaka Maoli" "Genre: Heist"

# Preview without making changes
hardcover-tagger <slug> --existing "Some List" --dry-run

# Machine-readable output
hardcover-tagger <slug> --existing "Some List" --json --quiet
```

## Options

| Flag | Description |
|------|-------------|
| `--existing LIST [LIST ...]` | Names of existing lists to add the book to |
| `--new LIST [LIST ...]` | Names of new lists to create and add the book to |
| `--dry-run` | Show what would happen without making changes |
| `--json` | Output results as JSON |
| `--quiet` | Suppress progress messages (errors still go to stderr) |

## Behavior

- Resolves the authenticated user from the API token (no hardcoded user ID)
- Resolves the book by slug and confirms its identity
- Batch-fetches all user lists in one paginated call, then matches `--existing` names locally
- Creates new lists sequentially (Hardcover has a position conflict on concurrent mutations)
- Adds the book to each list sequentially
- Retries failed operations once before reporting
- Updates `hardcover-lists.txt` with any new list names (atomic write, sorted, deduplicated)
- Exit code 0 if all lists succeeded, 1 if any failed

## Rate Limiting

Token bucket: bursts up to 30 requests, then refills at 1 req/sec to stay under Hardcover's 60 req/min ceiling. The rate limiter is shared across all operations including retries.

## Related

- `hardcover-lists.sh` — full refresh of the list inventory from the API
