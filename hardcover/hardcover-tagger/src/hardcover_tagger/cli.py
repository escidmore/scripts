"""CLI entry point for hardcover-tagger."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from typing import Any

from hardcover_tagger.client import GraphQLClient, GraphQLError
from hardcover_tagger.inventory import update_inventory
from hardcover_tagger.operations import (
    Book,
    ListResult,
    fetch_all_lists,
    process_lists,
    resolve_book,
    resolve_user_id,
    retry_failed,
)
from hardcover_tagger.rate_limiter import RateLimiter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hardcover-tagger",
        description="Add a book to multiple Hardcover lists.",
    )
    parser.add_argument("slug", help="Hardcover book slug")
    parser.add_argument(
        "--existing",
        nargs="+",
        default=[],
        metavar="LIST",
        help="Names of existing lists to add the book to",
    )
    parser.add_argument(
        "--new",
        nargs="+",
        default=[],
        metavar="LIST",
        help="Names of new lists to create and add the book to",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON (for machine consumption)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages (errors still go to stderr)",
    )
    return parser


def log(msg: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


def format_results_text(book: Book, results: list[ListResult], *, dry_run: bool) -> str:
    lines: list[str] = []
    prefix = "[DRY RUN] " if dry_run else ""
    lines.append(f"{prefix}Results for: {book.title} ({book.slug})")
    lines.append("")

    succeeded = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    if succeeded:
        lines.append(f"  Added to {len(succeeded)} list(s):")
        for r in succeeded:
            tag = " (new)" if r.created else ""
            lines.append(f"    + {r.name}{tag}")

    if failed:
        lines.append(f"  Failed for {len(failed)} list(s):")
        for r in failed:
            lines.append(f"    x {r.name}: {r.error}")

    return "\n".join(lines)


def format_results_json(book: Book, results: list[ListResult], *, dry_run: bool) -> str:
    output: dict[str, Any] = {
        "book": {"id": book.id, "title": book.title, "slug": book.slug},
        "dry_run": dry_run,
        "results": [
            {
                "name": r.name,
                "success": r.success,
                "created": r.created,
                "error": r.error or None,
            }
            for r in results
        ],
        "summary": {
            "total": len(results),
            "succeeded": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
        },
    }
    return json.dumps(output, indent=2)


def run(args: argparse.Namespace) -> int:
    """Main execution logic. Returns exit code."""
    api_key = os.environ.get("HARDCOVER_API_KEY", "")
    if not api_key:
        print("Error: HARDCOVER_API_KEY is not set", file=sys.stderr)
        return 1

    if not args.existing and not args.new:
        print(
            "Error: provide at least one --existing or --new list",
            file=sys.stderr,
        )
        return 1

    quiet = args.quiet or args.json_output
    rate_limiter = RateLimiter()
    client = GraphQLClient(api_key=api_key, rate_limiter=rate_limiter)

    # Resolve user ID
    log("Resolving user ID...", quiet=quiet)
    try:
        user_id = resolve_user_id(client)
    except GraphQLError as exc:
        print(f"Error resolving user: {exc}", file=sys.stderr)
        return 1

    # Resolve book
    log(f"Resolving book: {args.slug}...", quiet=quiet)
    try:
        book = resolve_book(client, args.slug)
    except GraphQLError as exc:
        print(f"Error resolving book: {exc}", file=sys.stderr)
        return 1

    if book is None:
        print(f"Error: no book found for slug '{args.slug}'", file=sys.stderr)
        return 1

    log(f"Found: {book.title} (id={book.id})", quiet=quiet)

    # Fetch all user lists
    log("Fetching user lists...", quiet=quiet)
    try:
        user_lists = fetch_all_lists(client, user_id)
    except GraphQLError as exc:
        print(f"Error fetching lists: {exc}", file=sys.stderr)
        return 1

    log(f"Found {len(user_lists)} existing lists", quiet=quiet)

    # Process all lists
    if args.dry_run:
        log("[DRY RUN] Simulating...", quiet=quiet)

    results = process_lists(
        client,
        book,
        args.existing,
        args.new,
        user_lists,
        dry_run=args.dry_run,
    )

    # Retry failures once (skip in dry-run)
    failed = [r for r in results if not r.success]
    if failed and not args.dry_run:
        log(f"Retrying {len(failed)} failed operation(s)...", quiet=quiet)
        # Re-fetch lists in case new ones were created
        with contextlib.suppress(GraphQLError):
            user_lists = fetch_all_lists(client, user_id)
        retried = retry_failed(client, book, failed, user_lists)
        # Replace failed results with retry results
        succeeded_names = {r.name for r in results if r.success}
        results = [r for r in results if r.success] + [
            r for r in retried if r.name not in succeeded_names
        ]

    # Output results
    if args.json_output:
        print(format_results_json(book, results, dry_run=args.dry_run))
    else:
        print(format_results_text(book, results, dry_run=args.dry_run))

    # Update inventory with successful list names (skip in dry-run)
    if not args.dry_run:
        successful_names = [r.name for r in results if r.success]
        if successful_names:
            added = update_inventory(successful_names)
            if added > 0:
                log(
                    f"Added {added} new name(s) to inventory",
                    quiet=quiet,
                )

    # Exit code: 0 if all succeeded, 1 if any failed
    any_failed = any(not r.success for r in results)
    return 1 if any_failed else 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
