#!/usr/bin/env python3
"""Backfill Hardcover author tags across all books in the Obsidian vault."""

import os
import re
import sys
import time
from pathlib import Path

# Add hardcover_tagger library to path if needed
lib_path = "/Users/host/.local/share/uv/tools/hardcover-tagger/lib/python3.14/site-packages"
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

from hardcover_tagger.client import GraphQLClient, GraphQLError
from hardcover_tagger.operations import resolve_tagging_setup, process_lists
from hardcover_tagger.rate_limiter import RateLimiter
from hardcover_tagger.inventory import update_inventory

VAULT_DIR = Path("/Users/host/Obsidian/Notes/Books")

def parse_frontmatter(content: str) -> dict | None:
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end == -1:
        return None
    fm_text = content[3:end]
    data = {}
    current_key = None
    in_list = False
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if re.match(r"^[A-Za-z0-9_-]+:\s*", line):
            parts = line.split(":", 1)
            current_key = parts[0].strip()
            val = parts[1].strip()
            if val == "":
                data[current_key] = []
                in_list = True
            else:
                data[current_key] = val
                in_list = False
        elif in_list and line.strip().startswith("- "):
            item = line.strip()[2:].strip()
            if current_key in data and isinstance(data[current_key], list):
                data[current_key].append(item)
    return data

def load_author_tag_map() -> dict[str, dict]:
    author_map = {}
    for path in VAULT_DIR.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if "author-metadata" in text:
            name_match = re.search(r'name:\s*"(.*?)"', text)
            author_name = name_match.group(1) if name_match else path.stem
            tags_match = re.search(r"## Hardcover Author Tags\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
            tags = []
            if tags_match:
                tags = re.findall(r"- `([^`]+)`", tags_match.group(1))
            author_map[author_name.lower()] = {
                "name": author_name,
                "tags": tags
            }
    return author_map

def find_books_needing_tags(author_map: dict) -> list[dict]:
    books = []
    for path in VAULT_DIR.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if "author-metadata" in text:
            continue
        fm = parse_frontmatter(text)
        if not fm:
            continue
        url = fm.get("url", "")
        slug_match = re.search(r"hardcover\.app/books/([^/]+)", url)
        if not slug_match:
            continue
        slug = slug_match.group(1)

        authors = fm.get("authors", [])
        if isinstance(authors, str):
            authors = [authors]
        clean_authors = []
        for a in authors:
            m = re.match(r'\[\[(.*?)(?:\|.*?)?\]\]', a.strip('"\''))
            clean_authors.append(m.group(1) if m else a.strip('"\''))

        lists = fm.get("lists", [])
        if isinstance(lists, str):
            lists = [lists]
        clean_lists = set()
        for l in lists:
            m = re.match(r'\[\[(.*?)(?:\|.*?)?\]\]', l.strip('"\''))
            clean_lists.add(m.group(1) if m else l.strip('"\''))

        needed_tags = set()
        matched_authors = []
        for a in clean_authors:
            norm_a = a.lower().strip()
            if norm_a in author_map and author_map[norm_a]["tags"]:
                matched_authors.append(author_map[norm_a]["name"])
                needed_tags.update(author_map[norm_a]["tags"])

        if matched_authors and needed_tags:
            books.append({
                "slug": slug,
                "title": fm.get("title", path.stem),
                "authors": matched_authors,
                "tags": sorted(needed_tags),
                "path": str(path)
            })
    return books

def main():
    dry_run = "--dry-run" in sys.argv
    api_key = os.environ.get("HARDCOVER_API_KEY", "")
    if not api_key:
        print("ERROR: HARDCOVER_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    print("Scanning Obsidian vault for author metadata and books...")
    author_map = load_author_tag_map()
    print(f"Loaded {len(author_map)} authors.")

    books = find_books_needing_tags(author_map)
    # Deduplicate books by slug
    dedup_books = {}
    for b in books:
        if b["slug"] not in dedup_books:
            dedup_books[b["slug"]] = b
    unique_books = list(dedup_books.values())
    print(f"Found {len(unique_books)} unique books to process.")

    # Rate limiter: 45 requests per minute to stay safely below 60/min limit
    rate_limiter = RateLimiter(requests_per_minute=45)
    client = GraphQLClient(api_key=api_key, rate_limiter=rate_limiter)

    total = len(unique_books)
    succeeded = 0
    failed = 0
    start_time = time.time()

    for idx, b in enumerate(unique_books, start=1):
        slug = b["slug"]
        tags = b["tags"]
        title = b["title"]
        authors_str = ", ".join(b["authors"])

        try:
            setup = resolve_tagging_setup(client, slug, tags)
            if not setup.book:
                print(f"[{idx}/{total}] SKIP '{title}' ({slug}): book not found on Hardcover", flush=True)
                continue

            results = process_lists(
                client,
                setup.book,
                existing_names=tags,
                new_names=[],
                user_lists=setup.user_lists,
                dry_run=dry_run,
            )

            added_names = [r.name for r in results if r.success]
            if not dry_run and added_names:
                update_inventory(added_names)

            errs = [r for r in results if not r.success]
            if errs:
                failed += 1
                print(f"[{idx}/{total}] PARTIAL '{title}' ({slug}): {len(added_names)} added, {len(errs)} failed: {[e.error for e in errs]}", flush=True)
            else:
                succeeded += 1
                print(f"[{idx}/{total}] OK '{title}' ({slug}) by {authors_str} -> {len(tags)} tag(s)", flush=True)

        except Exception as e:
            failed += 1
            print(f"[{idx}/{total}] ERROR '{title}' ({slug}): {e}", flush=True)

    elapsed = time.time() - start_time
    print(f"\nDone! Processed {total} books in {elapsed/60:.1f} mins. Succeeded: {succeeded}, Failed/Partial: {failed}")

if __name__ == "__main__":
    main()
