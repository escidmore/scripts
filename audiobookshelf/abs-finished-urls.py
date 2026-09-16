#!/usr/bin/env python3
"""Find tracking-site URLs for recently finished Audiobookshelf books."""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


DEFAULT_FIRECRAWL_API_URL = "https://firecrawl.samesies.gay/v1"
DEFAULT_ABS_BASE_URL = "https://abs.samesies.gay"
DEFAULT_ABS_LIBRARY_ID = "98216a96-0651-43e3-b6a5-44e192b53e03"
DEFAULT_AUDIBLE_API_URL = "https://api.audible.com"
HARDCOVER_API_URL = "https://api.hardcover.app/v1/graphql"
SUPPORTED_SITES = {"amazon", "goodreads", "hardcover", "storygraph"}
NETWORK_SITES = {"goodreads", "hardcover", "storygraph"}
SUPPORTED_STATUSES = {"error", "high", "low", "medium", "none", "search"}
PROVIDER_BY_SITE = {
    "amazon": "amazon",
    "goodreads": "firecrawl",
    "hardcover": "hardcover",
    "storygraph": "firecrawl",
}
STORYGRAPH_BOOK_RE = re.compile(
    r"https://app\.thestorygraph\.com/books/[0-9a-fA-F-]+"
)
GOODREADS_BOOK_RE = re.compile(
    r"https://www\.goodreads\.com/book/show/\d+(?:[-\w.]*)?"
)
MIN_TITLE_AUTHOR_SCORE = 7
HARDCOVER_DURATION_TOLERANCE_SECONDS = 300
HARDCOVER_REQUESTS_PER_MINUTE = 30
GOODREADS_EXPORT_HEADERS = [
    "Book Id",
    "Title",
    "Author",
    "Author l-f",
    "Additional Authors",
    "ISBN",
    "ISBN13",
    "My Rating",
    "Average Rating",
    "Publisher",
    "Binding",
    "Number of Pages",
    "Year Published",
    "Original Publication Year",
    "Date Read",
    "Date Added",
    "Bookshelves",
    "Bookshelves with positions",
    "Exclusive Shelf",
    "My Review",
    "Spoiler",
    "Private Notes",
    "Read Count",
    "Owned Copies",
]
HARDCOVER_CSV_HEADERS = [
    "Title",
    "Author",
    "Series",
    "Status",
    "Privacy",
    "Hardcover Book ID",
    "Hardcover Edition ID",
    "ISBN 10",
    "ISBN 13",
    "ASIN",
    "Media",
    "Country Code",
    "Language Code",
    "Binding",
    "Pages",
    "Duration in Seconds",
    "Publish Date",
    "Publisher",
    "Genres",
    "Moods",
    "Tags",
    "Content Warnings",
    "Lists",
    "Date Added",
    "Date Started",
    "Date Finished",
    "Rating",
    "Review",
    "Review Contains Spoilers",
    "Sponsored Review",
    "Review Date",
    "Review URL",
    "Review Media URL",
    "Private Notes",
    "Owned",
    "Compilation",
    "Review Slate",
]


@dataclass(frozen=True)
class Book:
    title: str
    author: str
    isbn: str | None
    asin: str | None
    abs_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    subtitle: str | None = None
    narrator: str | None = None
    publisher: str | None = None
    published_date: str | None = None
    published_year: str | None = None
    language: str | None = None
    duration_seconds: int | None = None
    series_name: str | None = None
    cover_path: str | None = None


@dataclass
class SiteResult:
    url: str | None
    confidence: str
    evidence: str
    error: str | None = None


@dataclass(frozen=True)
class HardcoverPlan:
    action: str
    confidence: str
    evidence: str
    hardcover_url: str | None = None
    book_id: int | None = None
    edition_id: int | None = None
    alternatives: tuple[str, ...] = ()
    cover_needed: bool = False
    hardcover_duration_seconds: int | None = None
    hardcover_language: str | None = None
    error: str | None = None


@dataclass
class LookupConfig:
    sites: list[str]
    sleep_seconds: float
    timeout_seconds: float
    goodreads_pages: int
    use_firecrawl_search: bool
    quiet: bool
    storygraph_url: str | None = None


class LookupErrorWithDetail(RuntimeError):
    """Raised when an external lookup fails with a useful message."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderUnavailableError(LookupErrorWithDetail):
    """Raised when retrying a provider during this run cannot succeed."""


class JsonCache:
    def __init__(self, path: Path, enabled: bool, refresh: bool = False) -> None:
        self.path = path
        self.enabled = enabled
        self.refresh = refresh
        self.data: dict[str, dict[str, Any]] = {}
        if enabled:
            self.data = self._load()

    def get(self, site: str, book: Book) -> SiteResult | None:
        if not self.enabled or self.refresh:
            return None
        key = cache_key(site, book)
        record = self.data.get(key)
        if not record:
            return None
        try:
            result = SiteResult(**record)
        except TypeError:
            del self.data[key]
            return None
        if not result.url or result.error or result.confidence == "low":
            del self.data[key]
            return None
        return result

    def put(self, site: str, book: Book, result: SiteResult) -> None:
        key = cache_key(site, book)
        if (
            self.enabled
            and result.url
            and not result.error
            and result.confidence != "low"
        ):
            self.data[key] = asdict(result)
        elif self.enabled:
            self.data.pop(key, None)

    def save(self) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            tmp_path.write_text(json.dumps(self.data, indent=2, sort_keys=True))
            tmp_path.replace(self.path)
        except OSError as error:
            raise LookupErrorWithDetail(f"failed to write cache {self.path}: {error}")

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            content = self.path.read_text()
            parsed = json.loads(content)
        except (OSError, json.JSONDecodeError) as error:
            raise LookupErrorWithDetail(f"failed to read cache {self.path}: {error}")
        if not isinstance(parsed, dict):
            raise LookupErrorWithDetail(f"cache {self.path} is not a JSON object")
        return parsed


class ApiClients:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.firecrawl_url = normalize_firecrawl_api_url(
            os.environ.get("FIRECRAWL_API_URL", DEFAULT_FIRECRAWL_API_URL)
        )
        self.firecrawl_key = os.environ.get("FIRECRAWL_API_KEY")
        self.hardcover_key = os.environ.get("HARDCOVER_API_KEY")
        self._hardcover_request_interval = 60.0 / HARDCOVER_REQUESTS_PER_MINUTE
        self._hardcover_next_request = 0.0
        self.abs_url = os.environ.get("ABS_BASE_URL", DEFAULT_ABS_BASE_URL)
        self.abs_library_id = os.environ.get("ABS_LIBRARY_ID", DEFAULT_ABS_LIBRARY_ID)
        self.abs_key = os.environ.get("ABS_API_KEY")
        self.audible_url = os.environ.get("AUDIBLE_API_URL", DEFAULT_AUDIBLE_API_URL)

    def firecrawl_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        payload = {"query": query, "limit": limit}
        response = post_json(
            self.firecrawl_endpoint("search"),
            payload,
            firecrawl_headers(self.firecrawl_key),
            self.timeout_seconds,
        )
        return firecrawl_search_results(response)

    def firecrawl_scrape(self, url: str) -> dict[str, Any]:
        payload = {
            "url": url,
            "formats": ["markdown", "links"],
            "onlyMainContent": False,
            "waitFor": 5000,
            "removeBase64Images": True,
        }
        response = post_json(
            self.firecrawl_endpoint("scrape"),
            payload,
            firecrawl_headers(self.firecrawl_key),
            self.timeout_seconds,
        )
        data = response.get("data", response)
        if not isinstance(data, dict):
            raise LookupErrorWithDetail(f"unexpected scrape response for {url}")
        return data

    def firecrawl_map(self, url: str, search: str | None = None) -> list[str]:
        payload = {"url": url, "limit": 20}
        if search:
            payload["search"] = search
        response = post_json(
            self.firecrawl_endpoint("map"),
            payload,
            firecrawl_headers(self.firecrawl_key),
            self.timeout_seconds,
        )
        data = response.get("links", response.get("data", []))
        if isinstance(data, list):
            return [str(item) for item in data]
        if isinstance(data, dict) and isinstance(data.get("links"), list):
            return [str(item) for item in data["links"]]
        return []

    def firecrawl_endpoint(self, method: str) -> str:
        return f"{self.firecrawl_url.rstrip('/')}/{method}"

    def audible_product(self, asin: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "response_groups": (
                    "contributors,media,product_details,product_extended_attrs,"
                    "series,category_ladders,sample"
                ),
                "image_sizes": "1215,900,500",
            }
        )
        response = self._audible_json(
            f"{self.audible_url.rstrip('/')}/1.0/catalog/products/"
            f"{urllib.parse.quote(asin, safe='')}?{query}"
        )
        product = response.get("product")
        if not isinstance(product, dict):
            raise LookupErrorWithDetail(f"Audible returned no product for {asin}")
        return product

    def audible_chapter_info(self, asin: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"response_groups": "chapter_info", "chapter_titles_type": "Flat"}
        )
        response = self._audible_json(
            f"{self.audible_url.rstrip('/')}/1.0/content/"
            f"{urllib.parse.quote(asin, safe='')}/metadata?{query}"
        )
        metadata = response.get("content_metadata") or {}
        chapter_info = metadata.get("chapter_info")
        if not isinstance(chapter_info, dict):
            raise LookupErrorWithDetail(
                f"Audible returned no chapter metadata for {asin}"
            )
        return chapter_info

    def _audible_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                content = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            content = error.read().decode("utf-8", errors="replace")
            raise LookupErrorWithDetail(
                f"HTTP {error.code} from {url}: {summarize_http_body(content)}",
                error.code,
            )
        except (urllib.error.URLError, TimeoutError) as error:
            raise LookupErrorWithDetail(f"request failed for {url}: {error}")
        try:
            value = json.loads(content)
        except json.JSONDecodeError as error:
            raise LookupErrorWithDetail(f"invalid JSON response from {url}: {error}")
        if not isinstance(value, dict):
            raise LookupErrorWithDetail(f"unexpected Audible response from {url}")
        return value

    def hardcover_query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.hardcover_key:
            raise LookupErrorWithDetail("HARDCOVER_API_KEY is not set")
        response = post_json(
            HARDCOVER_API_URL,
            {"query": query, "variables": variables},
            bearer_headers(self.hardcover_key),
            self.timeout_seconds,
            self._pace_hardcover_request,
        )
        errors = response.get("errors")
        if errors:
            messages = "; ".join(error.get("message", str(error)) for error in errors)
            raise LookupErrorWithDetail(f"Hardcover GraphQL error: {messages}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise LookupErrorWithDetail("Hardcover GraphQL response did not include data")
        return data

    def _pace_hardcover_request(self) -> None:
        now = time.monotonic()
        if now < self._hardcover_next_request:
            time.sleep(self._hardcover_next_request - now)
            now = time.monotonic()
        self._hardcover_next_request = max(now, self._hardcover_next_request) + (
            self._hardcover_request_interval
        )

    def audiobookshelf_json(self, path: str, query: dict[str, str] | None = None) -> Any:
        if not self.abs_key:
            raise LookupErrorWithDetail("ABS_API_KEY is not set")
        url = self.abs_endpoint(path, query)
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.abs_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                content = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            content = error.read().decode("utf-8", errors="replace")
            raise LookupErrorWithDetail(
                f"HTTP {error.code} from {url}: {summarize_http_body(content)}",
                error.code,
            )
        except urllib.error.URLError as error:
            raise LookupErrorWithDetail(f"request failed for {url}: {error}")
        try:
            return json.loads(content)
        except json.JSONDecodeError as error:
            raise LookupErrorWithDetail(f"invalid JSON response from {url}: {error}")

    def audiobookshelf_cover(self, item_id: str) -> tuple[bytes, str]:
        if not self.abs_key:
            raise LookupErrorWithDetail("ABS_API_KEY is not set")
        url = self.abs_endpoint(f"/api/items/{item_id}/cover", {"raw": "1"})
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.abs_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as error:
            content = error.read().decode("utf-8", errors="replace")
            raise LookupErrorWithDetail(
                f"HTTP {error.code} from {url}: {summarize_http_body(content)}",
                error.code,
            )
        except urllib.error.URLError as error:
            raise LookupErrorWithDetail(f"request failed for {url}: {error}")

    def abs_endpoint(self, path: str, query: dict[str, str] | None = None) -> str:
        url = f"{self.abs_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            return f"{url}?{urllib.parse.urlencode(query)}"
        return url


def main() -> int:
    args = parse_args()
    human_output = not any(
        [
            args.json,
            args.tsv,
            args.snapshot,
            args.hardcover_plan,
            args.hardcover_catchup,
            args.hardcover_csv,
            args.goodreads_export,
        ]
    )
    config = LookupConfig(
        sites=["goodreads"]
        if args.goodreads_export or args.hardcover_catchup
        else parse_sites(args.sites),
        sleep_seconds=args.sleep,
        timeout_seconds=args.timeout,
        goodreads_pages=args.goodreads_pages,
        use_firecrawl_search=args.use_firecrawl_search,
        quiet=args.quiet or (human_output and args.only is not None),
        storygraph_url=args.storygraph_url,
    )
    unknown_sites = set(config.sites) - SUPPORTED_SITES
    if unknown_sites:
        print(f"unknown site(s): {', '.join(sorted(unknown_sites))}", file=sys.stderr)
        return 2

    try:
        clients = ApiClients(args.timeout)
        if not args.book_json and not clients.abs_key:
            raise LookupErrorWithDetail("ABS_API_KEY is not set")
        books = load_books(args, clients)
        if args.snapshot:
            write_snapshot(args.snapshot, books)
            print(f"ABS snapshot: {len(books)} books\n  file: {args.snapshot}")
            return 0
        if args.hardcover_csv:
            plan_records = (
                hardcover_plan_records_from_json(args.hardcover_plan_json)
                if args.hardcover_plan_json
                else None
            )
            write_hardcover_csv(args.hardcover_csv, books, plan_records)
            print(f"Hardcover import: {len(books)} books\n  file: {args.hardcover_csv}")
            return 0
        if args.hardcover_plan:
            if not clients.hardcover_key:
                raise LookupErrorWithDetail("HARDCOVER_API_KEY is not set")
            plans = hardcover_plan_books(books, clients, args.sleep, args.quiet)
            write_hardcover_plan_artifacts(args.hardcover_plan, books, plans)
            print_hardcover_plan_summary(args.hardcover_plan, books, plans)
            return 3 if any(plan.error for plan in plans) else 0
        if args.hardcover_catchup:
            if not clients.hardcover_key:
                raise LookupErrorWithDetail("HARDCOVER_API_KEY is not set")
            plan_records = (
                hardcover_plan_records_from_json(args.hardcover_plan_json)
                if args.hardcover_plan_json
                else None
            )
            cache = JsonCache(args.cache, not args.no_cache, args.refresh)
            completed = hardcover_catchup_books(
                args.hardcover_catchup,
                books,
                clients,
                cache,
                config,
                plan_records,
            )
            print(
                f"Hardcover catch-up: {completed}/{len(books)} books complete\n"
                f"  state: {args.hardcover_catchup / 'hardcover-catchup.json'}\n"
                f"  Goodreads: {args.hardcover_catchup / 'goodreads-import.csv'}"
            )
            return 0
        cache = JsonCache(args.cache, not args.no_cache, args.refresh)
        rows = lookup_books(books, clients, cache, config)
        if args.goodreads_export:
            write_goodreads_export_artifacts(args.goodreads_export, rows)
            print_goodreads_export_summary(args.goodreads_export, rows)
            return 3 if args.fail_on_error and rows_have_errors(rows) else 0
    except LookupErrorWithDetail as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    output_rows = filter_rows(rows, args.only)
    if args.json:
        print(json.dumps(output_rows, indent=2, sort_keys=True))
    elif args.tsv:
        print_tsv(output_rows, config.sites)
    elif config.quiet:
        print_human(output_rows, config.sites)
    return 3 if args.fail_on_error and rows_have_errors(rows) else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch finished Audiobookshelf books and find StoryGraph, "
            "Hardcover, Goodreads, and Amazon URLs."
        ),
        epilog=(
            "Environment: ABS_API_KEY is required for Audiobookshelf input; "
            "HARDCOVER_API_KEY enables Hardcover; FIRECRAWL_API_URL defaults to "
            f"{DEFAULT_FIRECRAWL_API_URL}. Example: %(prog)s --finished-after "
            "2026-08-01 --only low,error,none --quiet; %(prog)s --book "
            "'Bunny Girl Evolution 2' --sites storygraph --refresh; %(prog)s "
            "--book 'Goddess Alchemy' --sites storygraph --storygraph-url URL; "
            "%(prog)s --book-json books.json --goodreads-export exports; "
            "%(prog)s --book-json books.json --hardcover-catchup catchup"
        ),
    )
    parser.add_argument(
        "since",
        nargs="?",
        type=valid_regex,
        help="stop before the first finished title matching this regex",
    )
    parser.add_argument(
        "--book-json",
        type=Path,
        help="read books from a JSON file instead of querying Audiobookshelf",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--book",
        metavar="TITLE",
        help="process one finished Audiobookshelf book by exact title",
    )
    selection.add_argument(
        "--finished-after",
        type=valid_iso_date,
        help="include books finished after YYYY-MM-DD",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="process all finished books",
    )
    parser.add_argument(
        "--sites",
        default="storygraph,hardcover,goodreads,amazon",
        help="comma-separated sites to query: storygraph,hardcover,goodreads,amazon",
    )
    parser.add_argument(
        "--storygraph-url",
        type=valid_storygraph_url,
        help="use and cache a user-confirmed StoryGraph URL for --book",
    )
    parser.add_argument("--limit", type=positive_int, help="limit rows for testing")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit structured JSON")
    output.add_argument("--tsv", action="store_true", help="emit TSV")
    output.add_argument(
        "--snapshot",
        type=Path,
        metavar="FILE",
        help="write ABS book metadata to FILE without external lookups",
    )
    output.add_argument(
        "--hardcover-plan",
        type=Path,
        metavar="DIR",
        help="write a read-only Hardcover import plan to DIR",
    )
    output.add_argument(
        "--hardcover-catchup",
        type=Path,
        metavar="DIR",
        help="interactively sync Hardcover and build a resumable Goodreads CSV",
    )
    output.add_argument(
        "--hardcover-csv",
        type=Path,
        metavar="FILE",
        help=(
            "write Hardcover's custom import format to FILE; its importer may ignore "
            "edition IDs, so prefer --hardcover-catchup"
        ),
    )
    output.add_argument(
        "--goodreads-export",
        type=Path,
        metavar="DIR",
        help="write a Goodreads-compatible import CSV and review TSV to DIR",
    )
    parser.add_argument(
        "--hardcover-plan-json",
        type=Path,
        metavar="FILE",
        help="reuse a saved Hardcover plan in --hardcover-csv or --hardcover-catchup",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress logs")
    parser.add_argument("--no-cache", action="store_true", help="disable local lookup cache")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore cached results while saving new matches",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=default_cache_path(),
        help="JSON cache path",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="seconds to sleep after uncached external lookups",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--goodreads-pages",
        type=positive_int,
        default=2,
        help="maximum Goodreads editions pages to scan",
    )
    parser.add_argument(
        "--use-firecrawl-search",
        action="store_true",
        help="also use Firecrawl /search after direct site searches miss",
    )
    parser.add_argument(
        "--only",
        type=parse_statuses,
        help="emit rows containing one of: error,high,low,medium,none,search",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="exit 3 after output if any site lookup failed",
    )
    args = parser.parse_args(argv)
    if args.book_json:
        if args.since or args.book or args.finished_after or args.all:
            parser.error("selection options cannot be used with --book-json")
    elif args.since and (args.book or args.finished_after or args.all):
        parser.error("title cutoff cannot be combined with another selection option")
    elif not (args.since or args.book or args.finished_after or args.all):
        parser.error("provide a title cutoff, --book, --finished-after, or --all")
    if args.storygraph_url and not args.book:
        parser.error("--storygraph-url requires --book")
    if args.storygraph_url and "storygraph" not in parse_sites(args.sites):
        parser.error("--storygraph-url requires storygraph in --sites")
    if args.hardcover_plan_json and not (
        args.hardcover_csv or args.hardcover_catchup
    ):
        parser.error(
            "--hardcover-plan-json requires --hardcover-csv or --hardcover-catchup"
        )
    if args.sleep < 0:
        parser.error("--sleep must be zero or greater")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def parse_sites(sites: str) -> list[str]:
    return unique([site.strip() for site in sites.split(",") if site.strip()])


def parse_statuses(statuses: str) -> set[str]:
    parsed = {status.strip() for status in statuses.split(",") if status.strip()}
    unknown = parsed - SUPPORTED_STATUSES
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown status(es): {', '.join(sorted(unknown))}"
        )
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def valid_iso_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD") from None
    return value


def valid_regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as error:
        raise argparse.ArgumentTypeError(f"invalid regex: {error}") from None
    return value


def valid_storygraph_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not STORYGRAPH_BOOK_RE.fullmatch(url):
        raise argparse.ArgumentTypeError(
            "must be a https://app.thestorygraph.com/books/UUID URL"
        )
    return url


def normalize_firecrawl_api_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LookupErrorWithDetail(f"invalid FIRECRAWL_API_URL: {url!r}")
    if parsed.query or parsed.fragment:
        raise LookupErrorWithDetail("FIRECRAWL_API_URL cannot contain a query or fragment")
    path = parsed.path.rstrip("/") or "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def default_cache_path() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        return Path(cache_home) / "abs-finished-urls" / "cache.json"
    return Path.home() / ".cache" / "abs-finished-urls" / "cache.json"


def load_books(args: argparse.Namespace, clients: ApiClients) -> list[Book]:
    if args.book_json:
        books = books_from_json_path(args.book_json)
        return books[: args.limit] if args.limit else books
    return audiobookshelf_finished_books(
        args.since,
        args.finished_after,
        args.all,
        args.limit,
        clients,
        args.book,
    )


def books_from_json_path(path: Path) -> list[Book]:
    try:
        records = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LookupErrorWithDetail(f"failed to read book JSON {path}: {error}")
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        raise LookupErrorWithDetail(f"book JSON {path} must be an object or list")
    return [book_from_record(record) for record in records]


def hardcover_plan_records_from_json(path: Path) -> list[dict[str, Any]]:
    try:
        records = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LookupErrorWithDetail(f"failed to read Hardcover plan JSON {path}: {error}")
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise LookupErrorWithDetail(f"Hardcover plan JSON {path} must be a list of objects")
    return records


def audiobookshelf_finished_books(
    since: str | None,
    finished_after: str | None,
    include_all: bool,
    limit: int | None,
    clients: ApiClients,
    book_title: str | None = None,
) -> list[Book]:
    if not (since or book_title or finished_after or include_all):
        raise LookupErrorWithDetail("a finished-book selection is required")
    response = clients.audiobookshelf_json(abs_library_items_path(clients), abs_finished_query())
    results = response.get("results") if isinstance(response, dict) else None
    if not isinstance(results, list):
        raise LookupErrorWithDetail("Audiobookshelf library response did not include results")
    if book_title:
        matches = []
        for item in results:
            book = book_from_abs_item(item)
            if book.title.casefold() == book_title.strip().casefold():
                matches.append(book)
        if not matches:
            raise LookupErrorWithDetail(
                f"finished Audiobookshelf book {book_title!r} was not found"
            )
        if len(matches) > 1:
            raise LookupErrorWithDetail(
                f"title {book_title!r} matched {len(matches)} finished books"
            )
        return [book_with_abs_progress(matches[0], clients)]
    if finished_after:
        books = []
        for item in results:
            book = book_with_abs_progress(book_from_abs_item(item), clients)
            if not book.finished_at:
                continue
            if book.finished_at <= finished_after:
                break
            books.append(book)
            if limit and len(books) >= limit:
                break
        return list(reversed(books))

    pattern = re.compile(since) if since else None
    selected = []
    found_cutoff = pattern is None
    for item in results:
        book = book_from_abs_item(item)
        if pattern and pattern.search(book.title):
            found_cutoff = True
            break
        selected.append(book)
    if not found_cutoff:
        raise LookupErrorWithDetail(
            f"title cutoff {since!r} was not found; use --all for full history"
        )
    if limit:
        selected = selected[:limit]
    books = [book_with_abs_progress(book, clients) for book in selected]
    return list(reversed(books))


def abs_library_items_path(clients: ApiClients) -> str:
    return f"/api/libraries/{clients.abs_library_id}/items"


def abs_finished_query() -> dict[str, str]:
    filter_value = base64.b64encode(b"finished").decode("ascii")
    return {
        "limit": "0",
        "filter": f"progress.{filter_value}",
        "desc": "1",
        "sort": "progress.finishedAt",
    }


def book_from_abs_item(item: Any) -> Book:
    if not isinstance(item, dict):
        raise LookupErrorWithDetail(f"unexpected Audiobookshelf item: {item!r}")
    media = item.get("media")
    metadata = media.get("metadata") if isinstance(media, dict) else None
    if not isinstance(metadata, dict):
        raise LookupErrorWithDetail(f"Audiobookshelf item missing metadata: {item!r}")
    record = {
        "title": metadata.get("title"),
        "author": metadata.get("authorName"),
        "isbn": metadata.get("isbn"),
        "asin": metadata.get("asin"),
        "id": item.get("id"),
        "subtitle": metadata.get("subtitle"),
        "narrator": metadata.get("narratorName"),
        "publisher": metadata.get("publisher"),
        "published_date": metadata.get("publishedDate"),
        "published_year": metadata.get("publishedYear"),
        "language": metadata.get("language"),
        "duration_seconds": media.get("duration") if isinstance(media, dict) else None,
        "series_name": metadata.get("seriesName"),
        "cover_path": media.get("coverPath") if isinstance(media, dict) else None,
    }
    return book_from_record(record)


def book_with_abs_progress(book: Book, clients: ApiClients) -> Book:
    if not book.abs_id:
        return book
    progress = clients.audiobookshelf_json(f"/api/me/progress/{book.abs_id}")
    if not isinstance(progress, dict):
        raise LookupErrorWithDetail(f"Audiobookshelf progress missing for {book.abs_id}")
    return replace(
        book,
        started_at=date_from_abs_millis(progress.get("startedAt")),
        finished_at=date_from_abs_millis(progress.get("finishedAt")),
    )


def book_from_record(record: Any) -> Book:
    if not isinstance(record, dict):
        raise LookupErrorWithDetail(f"unexpected book row: {record!r}")
    title = str(record.get("title") or "").strip()
    author = str(record.get("author") or "").strip()
    isbn = clean_identifier(record.get("isbn"))
    asin = clean_identifier(record.get("asin"))
    abs_id = clean_optional_string(record.get("id") or record.get("abs_id"))
    started_at = clean_optional_string(record.get("startedAt") or record.get("started_at"))
    finished_at = clean_optional_string(record.get("finishedAt") or record.get("finished_at"))
    if not title or not author:
        raise LookupErrorWithDetail(f"book row missing title/author: {record!r}")
    return Book(
        title=title,
        author=author,
        isbn=isbn,
        asin=asin,
        abs_id=abs_id,
        started_at=started_at,
        finished_at=finished_at,
        subtitle=clean_optional_string(record.get("subtitle")),
        narrator=clean_optional_string(record.get("narrator")),
        publisher=clean_optional_string(record.get("publisher")),
        published_date=clean_optional_string(record.get("published_date")),
        published_year=clean_optional_string(record.get("published_year")),
        language=clean_optional_string(record.get("language")),
        duration_seconds=clean_optional_int(record.get("duration_seconds")),
        series_name=clean_optional_string(record.get("series_name")),
        cover_path=clean_optional_string(record.get("cover_path")),
    )


def audible_enrich_book(
    book: Book,
    clients: ApiClients,
    cache: dict[str, dict[str, Any]],
) -> tuple[Book, str | None]:
    asin = (clean_identifier(book.asin) or "").upper()
    if not asin:
        return book, None
    cached = cache.get(asin)
    if cached:
        return audible_book_from_cache(book, cached), None
    try:
        product = clients.audible_product(asin)
    except LookupErrorWithDetail as error:
        return book, f"Audible metadata unavailable: {error}"
    returned_asin = (clean_identifier(product.get("asin")) or "").upper()
    title = audible_title(product.get("title"))
    authors = audible_names(product.get("authors"))
    if returned_asin != asin:
        return book, f"Audible ASIN mismatch: expected {asin}, got {returned_asin or 'none'}"
    if not title or not audible_title_compatible(book.title, title):
        return book, f"Audible title mismatch: expected {book.title}, got {title or 'none'}"
    expected_author = normalize_text(first_author(book.author))
    if expected_author and expected_author not in {
        normalize_text(author) for author in authors
    }:
        return book, f"Audible author mismatch for {book.author}"
    try:
        chapter_info = clients.audible_chapter_info(asin)
    except LookupErrorWithDetail:
        chapter_info = {}
    release_date = clean_optional_string(product.get("release_date"))
    if not release_date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
        release_date = None
    duration = clean_optional_int(chapter_info.get("runtime_length_sec"))
    series_name = audible_series_name(product.get("series"))
    record = {
        "title": title,
        "author": audible_author_string(authors, book.author),
        "isbn": clean_identifier(product.get("isbn")) or book.isbn,
        "subtitle": audible_title(product.get("subtitle")),
        "narrator": ", ".join(audible_names(product.get("narrators")))
        or book.narrator,
        "publisher": clean_optional_string(product.get("publisher_name"))
        or book.publisher,
        "published_date": release_date,
        "published_year": release_date[:4] if release_date else book.published_year,
        "language": clean_optional_string(product.get("language")) or book.language,
        "duration_seconds": duration or book.duration_seconds,
        "series_name": series_name or book.series_name,
        "intro_seconds": audible_milliseconds_seconds(
            chapter_info.get("brandIntroDurationMs")
        ),
        "outro_seconds": audible_milliseconds_seconds(
            chapter_info.get("brandOutroDurationMs")
        ),
        "format_type": clean_optional_string(product.get("format_type")),
        "copyright": clean_optional_string(product.get("copyright")),
        "cover_url": audible_cover_url(product.get("product_images")),
        "sample_url": clean_optional_string(product.get("sample_url")),
        "genres": audible_genres(product.get("category_ladders")),
    }
    cache[asin] = record
    return audible_book_from_cache(book, record), None


def audible_book_from_cache(book: Book, record: dict[str, Any]) -> Book:
    cached_author = clean_optional_string(record.get("author"))
    subtitle = (
        audible_title(record.get("subtitle")) if "subtitle" in record else book.subtitle
    )
    return replace(
        book,
        title=audible_title(record.get("title")) or book.title,
        author=audible_author_string(
            [value.strip() for value in (cached_author or "").split(",") if value.strip()],
            book.author,
        ),
        isbn=clean_identifier(record.get("isbn")) or book.isbn,
        subtitle=subtitle,
        narrator=clean_optional_string(record.get("narrator")) or book.narrator,
        publisher=clean_optional_string(record.get("publisher")) or book.publisher,
        published_date=clean_optional_string(record.get("published_date"))
        or book.published_date,
        published_year=clean_optional_string(record.get("published_year"))
        or book.published_year,
        language=clean_optional_string(record.get("language")) or book.language,
        duration_seconds=clean_optional_int(record.get("duration_seconds"))
        or book.duration_seconds,
        series_name=clean_optional_string(record.get("series_name")) or book.series_name,
    )


def audible_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return unique(
        [
            name
            for item in value
            if isinstance(item, dict)
            and (name := str(item.get("name") or "").strip())
        ]
    )


def audible_title(value: Any) -> str | None:
    title = clean_optional_string(value)
    return re.sub(r"\s+:", ":", title) if title else None


def audible_author_string(authors: list[str], fallback: str) -> str:
    if not authors:
        return fallback
    translator_names = {
        normalize_text(match.group(1))
        for value in fallback.split(",")
        if (match := re.match(r"^(.+?)\s+-\s+translator$", value.strip(), re.IGNORECASE))
    }
    values = []
    for author in authors:
        name = re.sub(
            r"\s+-\s+translator$", "", author, flags=re.IGNORECASE
        ).strip()
        values.append(
            f"{name} - translator" if normalize_text(name) in translator_names else name
        )
    return ", ".join(values)


def audible_title_compatible(expected: str, actual: str) -> bool:
    expected_normalized = normalize_text(expected)
    actual_normalized = normalize_text(actual)
    expected_base = normalize_text(expected.split(":", 1)[0])
    actual_base = normalize_text(actual.split(":", 1)[0])
    return bool(expected_normalized and actual_normalized) and (
        expected_normalized == actual_normalized
        or expected_base == actual_base
        or expected_normalized in actual_normalized
        or actual_normalized in expected_normalized
    )


def audible_series_name(value: Any) -> str | None:
    if not isinstance(value, list) or not value or not isinstance(value[0], dict):
        return None
    title = str(value[0].get("title") or "").strip()
    sequence = str(value[0].get("sequence") or "").strip()
    if not title:
        return None
    return f"{title} #{sequence}" if sequence else title


def audible_milliseconds_seconds(value: Any) -> float | None:
    try:
        return int(value) / 1000
    except (TypeError, ValueError):
        return None


def audible_cover_url(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for size in ("1215", "900", "500"):
        url = clean_optional_string(value.get(size))
        if url:
            return url
    return None


def audible_genres(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names = []
    for category in value:
        ladder = category.get("ladder") if isinstance(category, dict) else None
        if not isinstance(ladder, list):
            continue
        names.extend(
            str(item.get("name") or "").strip()
            for item in ladder
            if isinstance(item, dict) and item.get("name")
        )
    return unique([name for name in names if name])


def date_from_abs_millis(value: Any) -> str | None:
    if value is None:
        return None
    try:
        timestamp = int(value) / 1000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def lookup_books(
    books: list[Book],
    clients: ApiClients,
    cache: JsonCache,
    config: LookupConfig,
) -> list[dict[str, Any]]:
    rows = []
    provider_errors = preflight_provider_errors(clients, config)
    for index, book in enumerate(books, start=1):
        log(config, book_progress_message(index, len(books), book))
        site_results: dict[str, SiteResult] = {}
        for site in config.sites:
            provider = PROVIDER_BY_SITE[site]
            if provider in provider_errors:
                result = SiteResult(None, "error", "provider unavailable", provider_errors[provider])
                log(config, site_result_message(site, result))
            else:
                try:
                    result = lookup_site(site, book, clients, cache, config)
                except ProviderUnavailableError as error:
                    provider_errors[provider] = str(error)
                    result = SiteResult(None, "error", "provider unavailable", str(error))
                    log(config, site_result_message(site, result))
            site_results[site] = result
        rows.append(row_from_results(book, site_results))
        try:
            cache.save()
        except LookupErrorWithDetail as error:
            log(config, f"warning: {error}")
            cache.enabled = False
    return rows


def preflight_provider_errors(
    clients: ApiClients, config: LookupConfig
) -> dict[str, str]:
    errors = {}
    if "hardcover" in config.sites and not clients.hardcover_key:
        errors["hardcover"] = "HARDCOVER_API_KEY is not set"
    return errors


def book_progress_message(index: int, total: int, book: Book) -> str:
    dates = date_range(book.started_at, book.finished_at)
    if dates:
        return f"[{index}/{total}] {dates} — {book.title} — {book.author}"
    return f"[{index}/{total}] {book.title} — {book.author}"


def date_range(started_at: str | None, finished_at: str | None) -> str | None:
    if started_at and finished_at:
        if started_at == finished_at:
            return finished_at
        return f"{started_at} → {finished_at}"
    return finished_at or started_at


def lookup_site(
    site: str,
    book: Book,
    clients: ApiClients,
    cache: JsonCache,
    config: LookupConfig,
) -> SiteResult:
    if site == "storygraph" and config.storygraph_url:
        result = SiteResult(
            config.storygraph_url,
            "high",
            "user-confirmed StoryGraph URL",
        )
        cache.put(site, book, result)
        log(config, site_result_message(site, result))
        return result
    if site not in NETWORK_SITES:
        result = uncached_lookup_site(site, book, clients, config)
        log(config, site_result_message(site, result))
        return result
    cached = cache.get(site, book)
    if cached:
        log(config, site_result_message(site, cached))
        return cached
    try:
        result = uncached_lookup_site(site, book, clients, config)
    except LookupErrorWithDetail as error:
        if error.status_code in {401, 403, 404}:
            raise ProviderUnavailableError(str(error), error.status_code)
        result = SiteResult(None, "error", "", str(error))
    cache.put(site, book, result)
    log(config, site_result_message(site, result))
    sleep_between_requests(config)
    return result


def uncached_lookup_site(
    site: str,
    book: Book,
    clients: ApiClients,
    config: LookupConfig,
) -> SiteResult:
    if site == "storygraph":
        return lookup_storygraph(book, clients, config)
    if site == "hardcover":
        return lookup_hardcover(book, clients)
    if site == "goodreads":
        return lookup_goodreads(book, clients, config)
    if site == "amazon":
        return lookup_amazon(book)
    raise LookupErrorWithDetail(f"unsupported site: {site}")


def site_result_message(site: str, result: SiteResult) -> str:
    if result.url:
        message = f"  {site} [{result.confidence}]: {result.url} ({result.evidence})"
        if result.error:
            message += f"; error: {result.error}"
        return message
    if result.error:
        return f"  {site} [error]: {result.error}"
    return f"  {site} [none]: not found ({result.evidence})"


def lookup_amazon(book: Book) -> SiteResult:
    asin = (book.asin or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9]{10}", asin):
        return SiteResult(
            f"https://www.amazon.com/dp/{asin}",
            "high",
            "matched Amazon product by Audiobookshelf ASIN",
        )
    return SiteResult(
        amazon_search_url(book),
        "search",
        "Amazon Audible search from title and author",
    )


def amazon_search_url(book: Book) -> str:
    query = f"{book.title} {first_author(book.author)} Audible Audiobook"
    params = urllib.parse.urlencode({"i": "audible", "k": query})
    return f"https://www.amazon.com/s?{params}"


def lookup_storygraph(book: Book, clients: ApiClients, config: LookupConfig) -> SiteResult:
    candidate = storygraph_main_url(book, clients, config)
    if not candidate:
        return SiteResult(None, "none", "no StoryGraph search result")
    editions_url = storygraph_editions_url(candidate)
    if not editions_url:
        return SiteResult(None, "none", f"no editions URL in result {candidate}")
    scrape = clients.firecrawl_scrape(editions_url)
    markdown = str(scrape.get("markdown") or "")
    url = find_storygraph_identifier_match(markdown, book)
    if url:
        return SiteResult(url, "high", "matched ISBN/UID on StoryGraph editions page")
    url = find_storygraph_audio_match(markdown)
    if url:
        return SiteResult(url, "medium", "fell back to first audio edition")
    return SiteResult(editions_url, "low", "fell back to StoryGraph editions page")


def storygraph_main_url(book: Book, clients: ApiClients, config: LookupConfig) -> str | None:
    for search_url in storygraph_search_urls(book):
        empty_results = 0
        for _ in range(3):
            scrape = clients.firecrawl_scrape(search_url)
            if storygraph_security_verification(scrape):
                time.sleep(max(config.sleep_seconds, 1))
                continue
            url = best_storygraph_scrape_url(scrape, book)
            if url:
                return url
            empty_results += 1
            if empty_results >= 2:
                break
    if not config.use_firecrawl_search:
        return None
    query = storygraph_query(book)
    results = clients.firecrawl_search(query, 8)
    return best_storygraph_result(results, book)


def storygraph_search_urls(book: Book) -> list[str]:
    primary = storygraph_search_url(book)
    fallback = storygraph_search_url(
        book,
        title=book.title.split(":", maxsplit=1)[0].strip(),
        author=spaced_author_initials(first_author(book.author)),
    )
    return unique([primary, fallback])


def storygraph_search_url(
    book: Book, title: str | None = None, author: str | None = None
) -> str:
    query = f"{title or book.title} {author or first_author(book.author)}"
    return f"https://app.thestorygraph.com/browse?search_term={urllib.parse.quote(query, safe='')}"


def storygraph_query(book: Book) -> str:
    parts = ["site:app.thestorygraph.com", quote_search(book.title)]
    parts.append(quote_search(first_author(book.author)))
    return " ".join(parts)


def best_storygraph_scrape_url(scrape: dict[str, Any], book: Book) -> str | None:
    markdown = str(scrape.get("markdown") or "")
    scored: list[tuple[int, str]] = []
    for url in storygraph_book_urls(scrape):
        if "/editions" in url:
            continue
        candidate_title = storygraph_candidate_title(markdown, url)
        if candidate_title and not matching_title_numbers(book.title, candidate_title):
            continue
        score = 0
        before = 0 if candidate_title else 200
        for window in markdown_windows_for_url(markdown, url, before, 1000):
            text = f"{candidate_title or ''} {window}".casefold()
            score = max(score, storygraph_text_match_score(text, book))
        if score:
            scored.append((score, url))
    if not scored:
        return None
    scored.sort(reverse=True)
    score, url = scored[0]
    if score < MIN_TITLE_AUTHOR_SCORE:
        return None
    return url


def storygraph_book_urls(scrape: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for link in scrape.get("links") or []:
        match = STORYGRAPH_BOOK_RE.search(str(link))
        if match:
            urls.append(match.group(0))
    markdown = str(scrape.get("markdown") or "")
    urls.extend(match.group(0) for match in STORYGRAPH_BOOK_RE.finditer(markdown))
    return unique(urls)


def best_storygraph_result(results: list[dict[str, Any]], book: Book) -> str | None:
    scored: list[tuple[int, str]] = []
    for result in results:
        url = str(result.get("url") or "")
        if "app.thestorygraph.com/books/" not in url:
            continue
        candidate_title = clean_optional_string(result.get("title"))
        if candidate_title and not matching_title_numbers(book.title, candidate_title):
            continue
        text = result_text(result)
        score = storygraph_text_match_score(text, book)
        if "/editions" in url:
            score += 2
        scored.append((score, url))
    if not scored:
        return None
    scored.sort(reverse=True)
    score, url = scored[0]
    if score < MIN_TITLE_AUTHOR_SCORE:
        return None
    return url


def storygraph_editions_url(url: str) -> str | None:
    match = STORYGRAPH_BOOK_RE.search(url)
    if not match:
        return None
    return f"{match.group(0)}/editions"


def storygraph_candidate_title(markdown: str, url: str) -> str | None:
    pattern = rf"^#+\s+\[([^\]\n]+)\]\({re.escape(url)}\)"
    match = re.search(pattern, markdown, re.MULTILINE)
    return match.group(1).strip() if match else None


def storygraph_security_verification(scrape: dict[str, Any]) -> bool:
    markdown = str(scrape.get("markdown") or "").casefold()
    return (
        "performing security verification" in markdown
        and "waiting for app.thestorygraph.com to respond" in markdown
    )


def matching_title_numbers(expected: str, candidate: str) -> bool:
    return re.findall(r"\d+", expected) == re.findall(r"\d+", candidate)


def storygraph_text_match_score(text: str, book: Book) -> int:
    titles = unique([book.title, book.title.split(":", maxsplit=1)[0].strip()])
    return max(text_match_score(text, book, title) for title in titles)


def spaced_author_initials(author: str) -> str:
    return re.sub(r"(?<=[A-Za-z])\.(?=[A-Za-z])", ". ", author)


def find_storygraph_identifier_match(markdown: str, book: Book) -> str | None:
    for identifier in [book.isbn, book.asin]:
        if not identifier:
            continue
        for match in re.finditer(re.escape(identifier), markdown, re.IGNORECASE):
            url = last_storygraph_book_url(markdown[: match.start()])
            if url:
                return url
    return None


def find_storygraph_audio_match(markdown: str) -> str | None:
    pattern = r"Format:\s*(?:Audio|Audiobook|Audio CD)|\baudiobook\b"
    for match in re.finditer(pattern, markdown, re.IGNORECASE):
        url = last_storygraph_book_url(markdown[: match.start()])
        if url:
            return url
    return None


def last_storygraph_book_url(text: str) -> str | None:
    matches = list(STORYGRAPH_BOOK_RE.finditer(text))
    if not matches:
        return None
    return matches[-1].group(0)


def lookup_hardcover(book: Book, clients: ApiClients) -> SiteResult:
    exact = hardcover_exact_match(book, clients)
    if exact:
        return exact
    fallback = hardcover_search_fallback(book, clients)
    if fallback:
        return fallback
    return SiteResult(None, "none", "no Hardcover match")


def hardcover_plan_books(
    books: list[Book],
    clients: ApiClients,
    sleep_seconds: float,
    quiet: bool,
) -> list[HardcoverPlan]:
    plans = []
    for index, book in enumerate(books, start=1):
        if not quiet:
            print(book_progress_message(index, len(books), book), file=sys.stderr)
        try:
            plan = hardcover_plan_book(book, clients)
        except LookupErrorWithDetail as error:
            plan = HardcoverPlan("error", "error", "Hardcover lookup failed", error=str(error))
        plans.append(plan)
        if not quiet:
            print(f"  hardcover-plan [{plan.confidence}]: {plan.action} ({plan.evidence})", file=sys.stderr)
        if sleep_seconds > 0 and index < len(books):
            time.sleep(sleep_seconds)
    return plans


def hardcover_catchup_books(
    directory: Path,
    books: list[Book],
    clients: ApiClients,
    cache: JsonCache,
    config: LookupConfig,
    plan_records: list[dict[str, Any]] | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    state = load_hardcover_catchup_state(directory)
    entries = state["books"]
    unresolved = state.setdefault("unresolved", {})
    created = state.setdefault("created", {})
    catalog_choices = state.setdefault("catalog_choices", {})
    audible_cache = state.setdefault("audible", {})
    plans = {
        str(record.get("abs_id")): hardcover_plan_from_record(record)
        for record in plan_records or []
        if record.get("abs_id")
    }
    completed = 0

    for book in books:
        abs_book = book
        book, audible_issue = audible_enrich_book(book, clients, audible_cache)
        save_hardcover_catchup_state(directory, state)
        key = hardcover_catchup_key(book)
        entry = entries.get(key)
        created_this_run = False
        if not entry:
            metadata_issues = [audible_issue] if audible_issue else []
            created_entry = created.get(key)
            if created_entry:
                metadata_issues = unique(
                    [
                        *metadata_issues,
                        *list(created_entry.get("metadata_issues") or []),
                    ]
                )
                plan = HardcoverPlan(
                    "use_existing_audiobook",
                    "high",
                    "resuming a previously created audiobook edition",
                    hardcover_url=created_entry.get("hardcover_url"),
                    book_id=clean_optional_int(created_entry.get("book_id")),
                    edition_id=clean_optional_int(created_entry.get("edition_id")),
                    cover_needed=bool(created_entry.get("cover_needed")),
                )
            else:
                plan = plans.get(str(book.abs_id)) or hardcover_plan_book(book, clients)
            if plan.action == "review_book_matches":
                selected_url, stop = choose_hardcover_book(
                    book, plan.alternatives, input_fn, output_fn
                )
                if stop:
                    break
                if not selected_url:
                    record_hardcover_catchup_unresolved(
                        directory, state, unresolved, key, book, plan
                    )
                    output_fn(f"Skipped {book.title}")
                    continue
                plan = hardcover_plan_selected_book_url(book, selected_url, clients)
            book_id = plan.book_id
            edition_id = plan.edition_id
            hardcover_url = plan.hardcover_url
            if plan.action == "create_book_and_audiobook_edition":
                answer = input_fn(
                    f"Create Hardcover book and audiobook for {book.title}? [y/N/q] "
                ).strip().casefold()
                if answer == "q":
                    break
                if answer not in {"y", "yes"}:
                    record_hardcover_catchup_unresolved(
                        directory, state, unresolved, key, book, plan
                    )
                    output_fn(f"Skipped {book.title}")
                    continue
                metadata, creation_issues, stop = hardcover_creation_metadata(
                    book, clients, catalog_choices, input_fn, output_fn
                )
                metadata_issues = unique([*metadata_issues, *creation_issues])
                save_hardcover_catchup_state(directory, state)
                if stop:
                    break
                if not any(
                    item.get("contribution") == "Author"
                    for item in metadata.get("contributions", [])
                ):
                    plan = replace(
                        plan,
                        evidence="; ".join(metadata_issues or ["author not resolved"]),
                    )
                    record_hardcover_catchup_unresolved(
                        directory, state, unresolved, key, book, plan
                    )
                    output_fn(f"Skipped {book.title}: author not resolved")
                    continue
                book_id, edition_id, hardcover_url = (
                    hardcover_create_book_and_audiobook(book, clients, metadata)
                )
                checkpoint_hardcover_created_edition(
                    directory,
                    state,
                    created,
                    key,
                    book_id,
                    edition_id,
                    hardcover_url,
                    metadata_issues,
                )
                created_this_run = True
            elif plan.action == "create_audiobook_edition" and book_id:
                answer = input_fn(
                    f"Create audiobook edition for {book.title} on "
                    f"{plan.hardcover_url or 'the matched Hardcover book'}? [y/N/q] "
                ).strip().casefold()
                if answer == "q":
                    break
                if answer not in {"y", "yes"}:
                    record_hardcover_catchup_unresolved(
                        directory, state, unresolved, key, book, plan
                    )
                    output_fn(f"Skipped {book.title}")
                    continue
                metadata, creation_issues, stop = hardcover_creation_metadata(
                    book, clients, catalog_choices, input_fn, output_fn
                )
                metadata_issues = unique([*metadata_issues, *creation_issues])
                save_hardcover_catchup_state(directory, state)
                if stop:
                    break
                edition_id, hardcover_url = hardcover_create_audiobook_edition(
                    book, book_id, clients, metadata
                )
                checkpoint_hardcover_created_edition(
                    directory,
                    state,
                    created,
                    key,
                    book_id,
                    edition_id,
                    hardcover_url,
                    metadata_issues,
                )
                created_this_run = True
            elif plan.action == "review_audiobook_editions" and book_id:
                selection, stop = choose_hardcover_edition(
                    book, plan.alternatives, input_fn, output_fn
                )
                if stop:
                    break
                if not selection:
                    record_hardcover_catchup_unresolved(
                        directory, state, unresolved, key, book, plan
                    )
                    output_fn(f"Skipped {book.title}")
                    continue
                edition_id, hardcover_url = selection
            elif plan.action != "use_existing_audiobook" or not (
                book_id and edition_id
            ):
                record_hardcover_catchup_unresolved(
                    directory, state, unresolved, key, book, plan
                )
                output_fn(f"Skipped {book.title}: {plan.evidence}")
                continue
            if not (book_id and edition_id):
                raise LookupErrorWithDetail(
                    f"Hardcover plan for {book.title} has no book or edition ID"
                )
            hardcover_sync_reading(book, book_id, edition_id, clients)
            entry = {
                **asdict(book),
                "book_id": book_id,
                "edition_id": edition_id,
                "hardcover_url": hardcover_url,
            }
            entries[key] = entry
            fix_reasons = list(metadata_issues)
            if plan.cover_needed or plan.action in {
                "create_audiobook_edition",
                "create_book_and_audiobook_edition",
            }:
                fix_reasons.insert(0, "cover missing")
            if fix_reasons:
                unresolved[key] = {
                    **asdict(book),
                    "action": "fix_hardcover_metadata",
                    "evidence": "; ".join(fix_reasons),
                    "hardcover_url": hardcover_url,
                    "alternatives": (),
                }
            else:
                unresolved.pop(key, None)
            save_hardcover_catchup_state(directory, state)
            write_hardcover_catchup_fix_list(directory, unresolved)
        entry.update(asdict(book))
        created_entry = created.get(key)
        if created_entry and not created_entry.get("metadata_version"):
            legacy_issues = hardcover_legacy_creation_issues(book)
            current = unresolved.get(key) or {
                **asdict(book),
                "action": "fix_hardcover_metadata",
                "hardcover_url": entry.get("hardcover_url"),
                "alternatives": (),
            }
            reasons = unique(
                [
                    *str(current.get("evidence") or "").split("; "),
                    *legacy_issues,
                ]
            )
            current["evidence"] = "; ".join(reason for reason in reasons if reason)
            unresolved[key] = current
        if key in unresolved:
            current = unresolved[key]
            reasons = [
                reason
                for reason in str(current.get("evidence") or "").split("; ")
                if reason and not reason.startswith("publication date missing")
            ]
            if audible_issue:
                reasons.append(audible_issue)
            if (
                created_entry
                and (clean_optional_int(created_entry.get("metadata_version")) or 0) < 2
            ):
                reasons.extend(
                    hardcover_audible_update_reasons(abs_book, book)
                )
            current.update(asdict(book))
            current["evidence"] = "; ".join(unique(reasons))
        edition_id = clean_optional_int(entry.get("edition_id"))
        previous_metadata_issues = list(entry.get("metadata_issues") or [])
        if edition_id and not created_this_run and (
            not entry.get("metadata_checked_version") or previous_metadata_issues
        ):
            try:
                metadata_issues = hardcover_existing_edition_issues(
                    book, edition_id, clients
                )
            except LookupErrorWithDetail as exc:
                metadata_issues = [f"edition metadata check failed: {exc}"]
            update_issues = hardcover_edition_update_issues(metadata_issues)
            if update_issues:
                output_fn(f"Hardcover metadata issues for {book.title}:")
                for issue in update_issues:
                    output_fn(f"  - {issue}")
                answer = input_fn(
                    f"Update Hardcover metadata for {book.title}? [y/N/q] "
                ).strip().casefold()
                if answer == "q":
                    return completed
                if answer in {"y", "yes"}:
                    metadata, resolution_issues, stop = hardcover_creation_metadata(
                        book, clients, catalog_choices, input_fn, output_fn
                    )
                    save_hardcover_catchup_state(directory, state)
                    if stop:
                        return completed
                    if any(
                        re.match(r"^(author|translator|narrator) not found:", issue)
                        for issue in resolution_issues
                    ):
                        metadata.pop("contributions", None)
                    try:
                        deferred_issues = hardcover_update_audiobook_edition(
                            book, edition_id, clients, metadata
                        )
                    except LookupErrorWithDetail as exc:
                        metadata_issues = unique(
                            [*metadata_issues, f"edition update failed: {exc}"]
                        )
                    else:
                        metadata_issues = hardcover_existing_edition_issues(
                            book, edition_id, clients
                        )
                        if "ISBN belongs to another Hardcover edition" in deferred_issues:
                            metadata_issues = [
                                issue
                                for issue in metadata_issues
                                if not issue.startswith("ISBN ")
                            ]
                        if "ASIN belongs to another Hardcover edition" in deferred_issues:
                            metadata_issues = [
                                issue
                                for issue in metadata_issues
                                if not issue.startswith("ASIN ")
                            ]
                        metadata_issues = unique([*metadata_issues, *deferred_issues])
            entry["metadata_checked_version"] = 1
            entry["metadata_issues"] = metadata_issues
            current = unresolved.get(key)
            reasons = [
                reason
                for reason in str((current or {}).get("evidence") or "").split("; ")
                if reason and reason not in previous_metadata_issues
            ]
            if metadata_issues:
                unresolved[key] = {
                    **asdict(book),
                    "action": "fix_hardcover_metadata",
                    "evidence": "; ".join(unique([*reasons, *metadata_issues])),
                    "hardcover_url": entry.get("hardcover_url"),
                    "alternatives": (),
                }
            elif current:
                if reasons:
                    current.update(asdict(book))
                    current["evidence"] = "; ".join(unique(reasons))
                else:
                    unresolved.pop(key, None)
        if entry.get("metadata_checked_version") and key in unresolved:
            current = unresolved[key]
            existing_reasons = [
                reason
                for reason in str(current.get("evidence") or "").split("; ")
                if reason
            ]
            reasons = hardcover_reconciled_fix_reasons(
                existing_reasons, list(entry.get("metadata_issues") or [])
            )
            if reasons:
                current["evidence"] = "; ".join(reasons)
            else:
                unresolved.pop(key, None)
        if edition_id:
            entry["edit_url"] = hardcover_edition_edit_url(edition_id)
        if entry.get("cover_file"):
            migrated_cover = normalize_abs_cover_location(
                directory, str(entry["cover_file"])
            )
            if migrated_cover:
                entry["cover_file"] = migrated_cover
            else:
                entry.pop("cover_file", None)
                entry["cover_original"] = False
        if book.abs_id and book.cover_path and not entry.get("cover_original"):
            entry["cover_file"] = download_abs_cover(directory, book, clients)
            entry["cover_original"] = True
        if key in unresolved:
            unresolved[key]["edit_url"] = entry.get("edit_url")
            unresolved[key]["cover_file"] = entry.get("cover_file")
        save_hardcover_catchup_state(directory, state)
        write_hardcover_catchup_fix_list(directory, unresolved)
        output_fn(str(entry.get("hardcover_url") or ""))
        if entry.get("edit_url"):
            output_fn(str(entry["edit_url"]))
        if entry.get("cover_file"):
            output_fn(f"Cover: {directory / str(entry['cover_file'])}")

        if "goodreads" not in entry:
            result = lookup_site("goodreads", book, clients, cache, config)
            entry["goodreads"] = asdict(result)
            save_hardcover_catchup_state(directory, state)
            cache.save()
            write_hardcover_catchup_goodreads(directory, books, entries)

        completed += 1

    write_hardcover_catchup_goodreads(directory, books, entries)
    write_hardcover_catchup_fix_list(directory, unresolved)
    return completed


def choose_hardcover_edition(
    book: Book,
    alternatives: tuple[str, ...],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> tuple[tuple[int, str] | None, bool]:
    output_fn(f"Multiple audiobook editions for {book.title}:")
    choices = []
    for index, url in enumerate(alternatives, 1):
        match = re.search(r"/editions/(\d+)$", url)
        if not match:
            continue
        choices.append((int(match.group(1)), url))
        output_fn(f"{len(choices)}. {url}")
    if not choices:
        return None, False
    while True:
        answer = input_fn(
            f"Choose 1-{len(choices)}, [s]kip, or [q]uit: "
        ).strip().casefold()
        if answer == "q":
            return None, True
        if answer in {"", "s", "skip"}:
            return None, False
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1], False


def choose_hardcover_book(
    book: Book,
    alternatives: tuple[str, ...],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> tuple[str | None, bool]:
    output_fn(f"Multiple Hardcover books match {book.title}:")
    for index, url in enumerate(alternatives, 1):
        output_fn(f"{index}. {url}")
    while alternatives:
        answer = input_fn(
            f"Choose 1-{len(alternatives)}, [s]kip, or [q]uit: "
        ).strip().casefold()
        if answer == "q":
            return None, True
        if answer in {"", "s", "skip"}:
            return None, False
        if answer.isdigit() and 1 <= int(answer) <= len(alternatives):
            return alternatives[int(answer) - 1], False
    return None, False


def hardcover_plan_from_record(record: dict[str, Any]) -> HardcoverPlan:
    return HardcoverPlan(
        action=str(record.get("action") or ""),
        confidence=str(record.get("confidence") or "low"),
        evidence=str(record.get("evidence") or "saved Hardcover plan"),
        hardcover_url=clean_optional_string(record.get("hardcover_url")),
        book_id=clean_optional_int(record.get("book_id")),
        edition_id=clean_optional_int(record.get("edition_id")),
        alternatives=tuple(record.get("alternatives") or ()),
        cover_needed=bool(record.get("cover_needed")),
        hardcover_duration_seconds=clean_optional_int(
            record.get("hardcover_duration_seconds")
        ),
        hardcover_language=clean_optional_string(record.get("hardcover_language")),
        error=clean_optional_string(record.get("error")),
    )


def hardcover_catchup_key(book: Book) -> str:
    return str(book.abs_id or cache_key("hardcover-catchup", book))


def load_hardcover_catchup_state(directory: Path) -> dict[str, Any]:
    path = directory / "hardcover-catchup.json"
    if not path.exists():
        return {"version": 1, "books": {}, "unresolved": {}, "created": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LookupErrorWithDetail(f"failed to read Hardcover catch-up state: {error}")
    if not isinstance(value, dict) or not isinstance(value.get("books"), dict):
        raise LookupErrorWithDetail("Hardcover catch-up state must contain a books object")
    return value


def save_hardcover_catchup_state(directory: Path, state: dict[str, Any]) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        write_atomic_text(
            directory / "hardcover-catchup.json",
            json.dumps(state, indent=2, sort_keys=True) + "\n",
        )
    except OSError as error:
        raise LookupErrorWithDetail(f"failed to write Hardcover catch-up state: {error}")


def checkpoint_hardcover_created_edition(
    directory: Path,
    state: dict[str, Any],
    created: dict[str, dict[str, Any]],
    key: str,
    book_id: int,
    edition_id: int,
    hardcover_url: str,
    metadata_issues: list[str],
) -> None:
    created[key] = {
        "book_id": book_id,
        "edition_id": edition_id,
        "hardcover_url": hardcover_url,
        "cover_needed": True,
        "metadata_version": 2,
        "metadata_issues": metadata_issues,
    }
    save_hardcover_catchup_state(directory, state)


def hardcover_legacy_creation_issues(book: Book) -> list[str]:
    issues = ["author needs review"]
    if book.narrator:
        issues.append("narrator needs review")
    if book.publisher:
        issues.append("publisher needs review")
    if book.asin:
        issues.append("country needs review")
    return issues


def record_hardcover_catchup_unresolved(
    directory: Path,
    state: dict[str, Any],
    unresolved: dict[str, dict[str, Any]],
    key: str,
    book: Book,
    plan: HardcoverPlan,
) -> None:
    unresolved[key] = {**asdict(book), **asdict(plan)}
    save_hardcover_catchup_state(directory, state)
    write_hardcover_catchup_fix_list(directory, unresolved)


def write_hardcover_catchup_fix_list(
    directory: Path,
    unresolved: dict[str, dict[str, Any]],
) -> None:
    headers = [
        "title",
        "author",
        "action",
        "evidence",
        "needed_author",
        "needed_narrator",
        "needed_publisher",
        "needed_country",
        "needed_language",
        "needed_duration_seconds",
        "needed_publish_date",
        "known_publish_year",
        "needed_asin",
        "needed_isbn",
        "hardcover_url",
        "edit_url",
        "cover_file",
        "alternatives",
    ]
    lines = ["\t".join(headers)]
    for record in unresolved.values():
        book = book_from_record(record)
        date_issue = hardcover_publish_date_issue(book)
        evidence = "; ".join(
            unique(
                [
                    *(str(record.get("evidence") or "").split("; ")),
                    *([date_issue] if date_issue else []),
                ]
            )
        ).strip("; ")
        lines.append(
            "\t".join(
                tsv_cell(value)
                for value in [
                    record.get("title"),
                    record.get("author"),
                    record.get("action"),
                    evidence,
                    book.author,
                    book.narrator,
                    book.publisher,
                    "United States of America (us)" if book.asin else None,
                    book.language,
                    book.duration_seconds,
                    hardcover_publish_date(book),
                    book.published_year,
                    book.asin,
                    book.isbn,
                    record.get("hardcover_url"),
                    record.get("edit_url"),
                    record.get("cover_file"),
                    ", ".join(record.get("alternatives") or []),
                ]
            )
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        write_atomic_text(directory / "fix-list.tsv", "\n".join(lines) + "\n")
    except OSError as error:
        raise LookupErrorWithDetail(f"failed to write Hardcover fix list: {error}")


def write_hardcover_catchup_goodreads(
    directory: Path,
    _books: list[Book],
    entries: dict[str, dict[str, Any]],
) -> None:
    rows = []
    for entry in entries.values():
        if not entry or not isinstance(entry.get("goodreads"), dict):
            continue
        detail = entry["goodreads"]
        enriched_book = book_from_record(entry)
        rows.append(
            {
                **asdict(enriched_book),
                "urls": {"goodreads": detail.get("url")},
                "details": {"goodreads": detail},
            }
        )
    write_goodreads_export_artifacts(directory, rows)


def hardcover_edition_fallback_url(book: Book, edition_id: int) -> str:
    return f"https://hardcover.app/books/{slugify(book.title)}/editions/{edition_id}"


def hardcover_edition_edit_url(edition_id: int) -> str:
    return f"https://hardcover.app/editions/{edition_id}/edit"


def download_abs_cover(directory: Path, book: Book, clients: ApiClients) -> str:
    if not book.abs_id:
        raise LookupErrorWithDetail(f"cannot download cover for {book.title}: no ABS ID")
    content, content_type = clients.audiobookshelf_cover(book.abs_id)
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(content_type, ".img")
    filename = f"{slugify(book.title) or 'cover'}-{book.abs_id[:8]}{extension}"
    relative = Path("covers") / filename
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_bytes(content)
    tmp_path.replace(path)
    return str(relative)


def normalize_abs_cover_location(directory: Path, cover_file: str) -> str | None:
    relative = Path(cover_file)
    current = directory / relative
    target_relative = Path("covers") / relative.name
    target = directory / target_relative
    if relative == target_relative:
        return str(target_relative) if current.exists() else None
    if target.exists():
        return str(target_relative)
    if not current.exists():
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        current.replace(target)
    except OSError as error:
        raise LookupErrorWithDetail(f"failed to move ABS cover into covers directory: {error}")
    return str(target_relative)


def hardcover_creation_metadata(
    book: Book,
    clients: ApiClients,
    choices: dict[str, dict[str, Any]],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> tuple[dict[str, Any], list[str], bool]:
    contributions = []
    issues = []
    for name, contribution, role_id in hardcover_contributor_specs(book):
        person_id, stop = hardcover_resolve_person(
            name, clients, choices, input_fn, output_fn
        )
        if stop:
            return {}, issues, True
        if not person_id:
            issues.append(f"{contribution.lower()} not found: {name}")
            continue
        item = {"author_id": person_id, "contribution": contribution}
        if role_id:
            item["contributor_role_id"] = role_id
        if item not in contributions:
            contributions.append(item)

    metadata: dict[str, Any] = {"contributions": contributions}
    if book.publisher:
        publisher_id, stop = hardcover_resolve_publisher(
            book.publisher, clients, choices, input_fn, output_fn
        )
        if stop:
            return {}, issues, True
        if publisher_id:
            metadata["publisher_id"] = publisher_id
        else:
            issues.append(f"publisher not found: {book.publisher}")
    date_issue = hardcover_publish_date_issue(book)
    if date_issue:
        issues.append(date_issue)
    return metadata, issues, False


def hardcover_contributor_specs(book: Book) -> list[tuple[str, str, int | None]]:
    specs = []
    for value in book.author.split(","):
        name = value.strip()
        role = "Author"
        role_id = 1
        match = re.match(r"^(.+?)\s+-\s+translator$", name, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            role = "Translator"
            role_id = 64
        if name:
            specs.append((name, role, role_id))
    for value in (book.narrator or "").split(","):
        name = value.strip()
        if name:
            specs.append((name, "Narrator", None))
    return specs


def hardcover_resolve_person(
    name: str,
    clients: ApiClients,
    choices: dict[str, dict[str, Any]],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> tuple[int | None, bool]:
    key = f"person:{normalize_text(name)}"
    cached = choices.get(key)
    if cached and clean_optional_int(cached.get("id")):
        return int(cached["id"]), False
    records = clients.hardcover_query(
        """
        query PeopleForEdition($name: String!) {
          authors(where: {name: {_eq: $name}}, limit: 20) {
            id name slug books_count
            contributions_aggregate { aggregate { count } }
          }
        }
        """,
        {"name": name},
    ).get("authors") or []
    records = [dict(record, uses=hardcover_person_usage(record)) for record in records]
    selected, stop = hardcover_choose_catalog_record(
        name,
        records,
        "uses",
        "author",
        choices,
        key,
        input_fn,
        output_fn,
        prompt_single=False,
    )
    if selected or stop:
        return selected, stop

    search = clients.hardcover_query(
        """
        query SearchPeopleForEdition($name: String!) {
          search(query: $name, query_type: "Author", per_page: 10, page: 1) {
            results
          }
        }
        """,
        {"name": name},
    ).get("search") or {}
    results = search.get("results") or {}
    hits = results.get("hits") or [] if isinstance(results, dict) else []
    records = [
        dict(
            hit.get("document") or {},
            uses=clean_optional_int(
                (hit.get("document") or {}).get("books_count")
            )
            or 0,
        )
        for hit in hits
        if isinstance(hit, dict)
        and hardcover_person_names_compatible(
            name, str((hit.get("document") or {}).get("name") or "")
        )
    ]
    normalized_name = normalize_text(name)
    normalized_matches = [
        record
        for record in records
        if normalize_text(str(record.get("name") or "")) == normalized_name
    ]
    if normalized_matches:
        records = normalized_matches
    return hardcover_choose_catalog_record(
        name,
        records,
        "uses",
        "author",
        choices,
        key,
        input_fn,
        output_fn,
        prompt_single=not normalized_matches,
    )


def hardcover_person_usage(record: dict[str, Any]) -> int:
    aggregate = record.get("contributions_aggregate") or {}
    contribution_count = clean_optional_int((aggregate.get("aggregate") or {}).get("count"))
    return max(clean_optional_int(record.get("books_count")) or 0, contribution_count or 0)


def hardcover_resolve_publisher(
    name: str,
    clients: ApiClients,
    choices: dict[str, dict[str, Any]],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> tuple[int | None, bool]:
    key = f"publisher:{normalize_text(name)}"
    cached = choices.get(key)
    if cached and clean_optional_int(cached.get("id")):
        return int(cached["id"]), False
    records = clients.hardcover_query(
        """
        query PublishersForEdition($name: String!) {
          publishers(where: {name: {_eq: $name}}, limit: 20) {
            id name slug editions_count
          }
        }
        """,
        {"name": name},
    ).get("publishers") or []
    return hardcover_choose_catalog_record(
        name,
        records,
        "editions_count",
        "publisher",
        choices,
        key,
        input_fn,
        output_fn,
        prompt_single=False,
    )


def hardcover_choose_catalog_record(
    name: str,
    records: list[dict[str, Any]],
    usage_field: str,
    kind: str,
    choices: dict[str, dict[str, Any]],
    key: str,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    prompt_single: bool,
) -> tuple[int | None, bool]:
    ranked = sorted(
        (record for record in records if clean_optional_int(record.get("id"))),
        key=lambda record: clean_optional_int(record.get(usage_field)) or 0,
        reverse=True,
    )
    if not ranked:
        return None, False
    top_usage = clean_optional_int(ranked[0].get(usage_field)) or 0
    tied = [
        record
        for record in ranked
        if (clean_optional_int(record.get(usage_field)) or 0) == top_usage
    ]
    if not prompt_single and len(tied) == 1:
        selected = tied[0]
    else:
        output_fn(f"Choose existing Hardcover {kind} for {name}:")
        for index, record in enumerate(ranked, 1):
            output_fn(
                f"{index}. {record.get('name')} "
                f"({record.get(usage_field) or 0} uses)"
            )
        while True:
            answer = input_fn(
                f"Choose 1-{len(ranked)}, [s]kip, or [q]uit: "
            ).strip().casefold()
            if answer == "q":
                return None, True
            if answer in {"", "s", "skip"}:
                return None, False
            if answer.isdigit() and 1 <= int(answer) <= len(ranked):
                selected = ranked[int(answer) - 1]
                break
    choices[key] = {
        "id": int(selected["id"]),
        "name": selected.get("name"),
        "slug": selected.get("slug"),
        "uses": clean_optional_int(selected.get(usage_field)) or 0,
    }
    return int(selected["id"]), False


def hardcover_person_names_compatible(expected: str, candidate: str) -> bool:
    def compact(value: str) -> list[str]:
        tokens = normalize_text(value).split()
        if len(tokens) < 3:
            return tokens
        return [tokens[0], *[token for token in tokens[1:-1] if len(token) > 1], tokens[-1]]

    return bool(candidate) and compact(expected) == compact(candidate)


def hardcover_create_audiobook_edition(
    book: Book,
    book_id: int,
    clients: ApiClients,
    metadata: dict[str, Any] | None = None,
) -> tuple[int, str]:
    result = clients.hardcover_query(
        """
        mutation InsertAudiobookEdition($bookId: Int!, $edition: EditionInput!) {
          insert_edition(book_id: $bookId, edition: $edition) {
            id
            errors
            warnings
            edition { id book { slug } }
          }
        }
        """,
        {
            "bookId": book_id,
            "edition": {"dto": hardcover_audiobook_dto(book, metadata)},
        },
    ).get("insert_edition") or {}
    errors = result.get("errors") or []
    if errors:
        raise LookupErrorWithDetail(
            f"failed to create Hardcover audiobook edition: {'; '.join(errors)}"
        )
    try:
        edition_id = int(result["id"])
    except (KeyError, TypeError, ValueError):
        raise LookupErrorWithDetail(
            "failed to create Hardcover audiobook edition: no ID returned"
        )
    slug = (
        ((result.get("edition") or {}).get("book") or {}).get("slug")
        or slugify(book.title)
    )
    return edition_id, f"https://hardcover.app/books/{slug}/editions/{edition_id}"


def hardcover_create_book_and_audiobook(
    book: Book,
    clients: ApiClients,
    metadata: dict[str, Any] | None = None,
) -> tuple[int, int, str]:
    if metadata is None:
        author_name = first_author(book.author).strip()
        authors = clients.hardcover_query(
            """
            query AuthorForBookCreation($name: String!) {
              authors(where: {name: {_eq: $name}}, limit: 10) { id name }
            }
            """,
            {"name": author_name},
        ).get("authors") or []
        if len(authors) != 1:
            raise LookupErrorWithDetail(
                f"expected one Hardcover author named {author_name!r}, found {len(authors)}"
            )
        metadata = {"contributions": [{"author_id": int(authors[0]["id"])}]}
    dto = hardcover_audiobook_dto(book, metadata)
    result = clients.hardcover_query(
        """
        mutation InsertBookWithAudiobook($edition: EditionInput!) {
          insert_book(edition: $edition) {
            id
            errors
            edition { id book { id slug } }
          }
        }
        """,
        {"edition": {"dto": dto}},
    ).get("insert_book") or {}
    errors = result.get("errors") or []
    if errors:
        raise LookupErrorWithDetail(
            f"failed to create Hardcover book: {'; '.join(errors)}"
        )
    edition = result.get("edition") or {}
    hardcover_book = edition.get("book") or {}
    try:
        edition_id = int(result["id"])
        book_id = int(hardcover_book["id"])
    except (KeyError, TypeError, ValueError):
        raise LookupErrorWithDetail(
            "failed to create Hardcover book and audiobook: no IDs returned"
        )
    slug = hardcover_book.get("slug") or slugify(book.title)
    return (
        book_id,
        edition_id,
        f"https://hardcover.app/books/{slug}/editions/{edition_id}",
    )


def hardcover_update_audiobook_edition(
    book: Book,
    edition_id: int,
    clients: ApiClients,
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    dto = hardcover_audiobook_dto(book, metadata)
    deferred: list[str] = []
    for _attempt in range(3):
        result = clients.hardcover_query(
            """
            mutation UpdateAudiobookEdition($id: Int!, $edition: EditionInput!) {
              update_edition(id: $id, edition: $edition) {
                id
                errors
                warnings
              }
            }
            """,
            {"id": edition_id, "edition": {"dto": dto}},
        ).get("update_edition") or {}
        errors = [str(error) for error in result.get("errors") or []]
        if errors:
            unhandled = []
            for error in errors:
                normalized = error.casefold()
                if "already in use" in normalized and "isbn_" in normalized:
                    dto.pop("isbn_10", None)
                    dto.pop("isbn_13", None)
                    deferred.append("ISBN belongs to another Hardcover edition")
                elif "already in use" in normalized and "asin" in normalized:
                    dto.pop("asin", None)
                    deferred.append("ASIN belongs to another Hardcover edition")
                else:
                    unhandled.append(error)
            if not unhandled and deferred:
                continue
            raise LookupErrorWithDetail(
                "failed to update Hardcover audiobook edition: "
                + "; ".join(unhandled or errors)
            )
        try:
            returned_id = int(result["id"])
        except (KeyError, TypeError, ValueError):
            raise LookupErrorWithDetail(
                "failed to update Hardcover audiobook edition: no ID returned"
            )
        if returned_id != edition_id:
            raise LookupErrorWithDetail(
                "failed to update Hardcover audiobook edition: returned the wrong ID"
            )
        return unique(deferred)
    raise LookupErrorWithDetail(
        "failed to update Hardcover audiobook edition after removing conflicting identifiers"
    )


def hardcover_audiobook_dto(
    book: Book, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    identifier = clean_identifier(book.isbn)
    dto: dict[str, Any] = {
        "title": book.title,
        "subtitle": book.subtitle,
        "asin": clean_identifier(book.asin),
        "isbn_10": identifier if identifier and len(identifier) == 10 else None,
        "isbn_13": identifier if identifier and len(identifier) == 13 else None,
        "audio_seconds": book.duration_seconds,
        "reading_format_id": 2,
        "edition_format": "Audible Audio" if book.asin else "Audiobook",
        "release_date": hardcover_publish_date(book) or None,
        "language_id": 1 if hardcover_language_code(book.language) == "en" else None,
        "country_id": 1 if book.asin else None,
    }
    dto.update(metadata or {})
    return {key: value for key, value in dto.items() if value is not None}


def hardcover_sync_reading(
    book: Book,
    book_id: int,
    edition_id: int,
    clients: ApiClients,
) -> str:
    current = clients.hardcover_query(
        """
        query UserBookForSync($bookId: Int!) {
          me {
            user_books(where: {book_id: {_eq: $bookId}}, limit: 1) {
              id
              edition_id
              status_id
              first_started_reading_date
              last_read_date
              read_count
              user_book_reads {
                id
                started_at
                finished_at
                edition_id
              }
            }
          }
        }
        """,
        {"bookId": book_id},
    )
    me = current.get("me") or []
    user_books = me[0].get("user_books") or [] if me else []
    desired_user_book = {
        "edition_id": edition_id,
        "status_id": 3,
        "first_started_reading_date": book.started_at,
        "last_read_date": book.finished_at,
        "read_count": 1,
    }
    if user_books:
        user_book = user_books[0]
        user_book_id = int(user_book["id"])
        automatic_read_created = False
    else:
        result = clients.hardcover_query(
            """
            mutation InsertUserBookForSync($object: UserBookCreateInput!) {
              insert_user_book(object: $object) {
                id
                error
                user_book {
                  id
                  edition_id
                  status_id
                  first_started_reading_date
                  last_read_date
                  read_count
                  user_book_reads {
                    id
                    started_at
                    finished_at
                    edition_id
                  }
                }
              }
            }
            """,
            {
                "object": {
                    "book_id": book_id,
                    "date_added": book.finished_at,
                    **desired_user_book,
                }
            },
        ).get("insert_user_book") or {}
        user_book_id = hardcover_mutation_id(result, "create Hardcover library record")
        returned_user_book = result.get("user_book")
        user_book = (
            returned_user_book
            if isinstance(returned_user_book, dict)
            else {"id": user_book_id, **desired_user_book, "user_book_reads": []}
        )
        automatic_read_created = True

    if user_books and any(
        user_book.get(field) != value
        for field, value in desired_user_book.items()
        if value is not None
    ):
        result = clients.hardcover_query(
            """
            mutation UpdateUserBookForSync($id: Int!, $object: UserBookUpdateInput!) {
              update_user_book(id: $id, object: $object) {
                id
                error
                user_book {
                  id
                  edition_id
                  status_id
                  first_started_reading_date
                  last_read_date
                  read_count
                  user_book_reads {
                    id
                    started_at
                    finished_at
                    edition_id
                  }
                }
              }
            }
            """,
            {"id": user_book_id, "object": desired_user_book},
        ).get("update_user_book") or {}
        hardcover_mutation_id(result, "update Hardcover library record")
        returned_user_book = result.get("user_book")
        if isinstance(returned_user_book, dict):
            automatic_read_created = (
                not (user_book.get("user_book_reads") or [])
                and len(returned_user_book.get("user_book_reads") or []) == 1
            )
            user_book = returned_user_book

    reads = user_book.get("user_book_reads") or []
    matching_read = next(
        (
            read
            for read in reads
            if read.get("started_at") == book.started_at
            and read.get("finished_at") == book.finished_at
        ),
        None,
    )
    desired_read = {
        "started_at": book.started_at,
        "finished_at": book.finished_at,
        "edition_id": edition_id,
    }
    automatic_read = reads[0] if automatic_read_created and len(reads) == 1 else None
    read_to_update = (
        matching_read
        if matching_read and matching_read.get("edition_id") != edition_id
        else automatic_read if not matching_read else None
    )
    if read_to_update:
        result = clients.hardcover_query(
            """
            mutation UpdateUserBookReadForSync($id: Int!, $object: DatesReadInput!) {
              update_user_book_read(id: $id, object: $object) { id error }
            }
            """,
            {"id": int(read_to_update["id"]), "object": desired_read},
        ).get("update_user_book_read") or {}
        hardcover_mutation_id(result, "update Hardcover reading dates")
    elif not matching_read:
        result = clients.hardcover_query(
            """
            mutation InsertUserBookReadForSync(
              $userBookId: Int!
              $read: DatesReadInput!
            ) {
              insert_user_book_read(
                user_book_id: $userBookId
                user_book_read: $read
              ) { id error }
            }
            """,
            {"userBookId": user_book_id, "read": desired_read},
        ).get("insert_user_book_read") or {}
        hardcover_mutation_id(result, "create Hardcover reading dates")

    return hardcover_edition_fallback_url(book, edition_id)


def hardcover_mutation_id(result: dict[str, Any], action: str) -> int:
    if result.get("error"):
        raise LookupErrorWithDetail(f"failed to {action}: {result['error']}")
    try:
        return int(result["id"])
    except (KeyError, TypeError, ValueError):
        raise LookupErrorWithDetail(f"failed to {action}: no ID returned")


def hardcover_plan_book(book: Book, clients: ApiClients) -> HardcoverPlan:
    isbn13 = book.isbn or ""
    isbn10 = isbn13_to_isbn10(isbn13) or isbn13
    query = """
    query HardcoverPlanLookup(
      $isbn13: String!, $isbn10: String!, $asin: String!, $q: String!, $type: String!
    ) {
      editions(
        where: {
          _or: [
            {isbn_13: {_eq: $isbn13}},
            {isbn_10: {_eq: $isbn10}},
            {asin: {_eq: $asin}}
          ]
        },
        limit: 20
      ) {
        id title isbn_13 isbn_10 asin physical_format audio_seconds release_date
        reading_format { id format }
        language { id language code2 }
        image { id url }
        book { id slug title }
        contributions { author { id name } contribution }
      }
      search(query: $q, query_type: $type, per_page: 5, page: 1) { results }
    }
    """
    data = clients.hardcover_query(
        query,
        {
            "isbn13": impossible_query_value(isbn13),
            "isbn10": impossible_query_value(isbn10),
            "asin": impossible_query_value(book.asin),
            "q": f"{book.title} {first_author(book.author)}",
            "type": "Book",
        },
    )
    exact_editions = [
        edition
        for edition in data.get("editions") or []
        if isinstance(edition, dict) and hardcover_is_audio(edition)
    ]
    exact = best_hardcover_edition(exact_editions, book)
    if exact:
        return hardcover_plan_from_edition(
            exact,
            "use_existing_audiobook",
            "high",
            "matched audiobook edition by ABS ISBN/ASIN",
        )

    candidates = hardcover_search_candidates((data.get("search") or {}).get("results"), book)
    if not candidates:
        return HardcoverPlan(
            "create_book_and_audiobook_edition",
            "high",
            "no matching Hardcover book",
            cover_needed=True,
        )
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        top_score = candidates[0][0]
        return HardcoverPlan(
            "review_book_matches",
            "low",
            "multiple Hardcover books have the same match score",
            alternatives=tuple(
                url
                for score, candidate in candidates
                if score == top_score and (url := hardcover_book_url(candidate))
            ),
        )

    candidate = candidates[0][1]
    book_id = clean_optional_int(candidate.get("id"))
    hardcover_url = hardcover_book_url(candidate, editions=True)
    if book_id is None:
        return HardcoverPlan(
            "error",
            "error",
            "matched Hardcover book did not include an integer ID",
            hardcover_url=hardcover_url,
            error="missing Hardcover book ID",
        )
    return hardcover_plan_selected_book(book, book_id, hardcover_url, clients)


def hardcover_plan_selected_book_url(
    book: Book,
    url: str,
    clients: ApiClients,
) -> HardcoverPlan:
    match = re.fullmatch(r"https://hardcover\.app/books/([^/]+)(?:/editions)?/?", url)
    if not match:
        return HardcoverPlan(
            "error",
            "error",
            "selected Hardcover URL is invalid",
            hardcover_url=url,
            error="invalid Hardcover book URL",
        )
    data = clients.hardcover_query(
        """
        query HardcoverBookBySlug($slug: String!) {
          books(where: {slug: {_eq: $slug}}, limit: 2) { id slug }
        }
        """,
        {"slug": match.group(1)},
    )
    books = data.get("books") or []
    if len(books) != 1 or clean_optional_int(books[0].get("id")) is None:
        return HardcoverPlan(
            "error",
            "error",
            "selected Hardcover book could not be resolved",
            hardcover_url=url,
            error="selected Hardcover book missing or ambiguous",
        )
    book_id = int(books[0]["id"])
    return hardcover_plan_selected_book(
        book,
        book_id,
        f"https://hardcover.app/books/{match.group(1)}/editions",
        clients,
    )


def hardcover_plan_selected_book(
    book: Book,
    book_id: int,
    hardcover_url: str | None,
    clients: ApiClients,
) -> HardcoverPlan:
    editions = hardcover_editions_for_book(book_id, clients)
    audio_editions = [edition for edition in editions if hardcover_is_audio(edition)]
    identifier_matches = [
        edition for edition in audio_editions if hardcover_identifier_matches(edition, book)
    ]
    if identifier_matches:
        return hardcover_plan_from_edition(
            best_hardcover_edition(identifier_matches, book) or identifier_matches[0],
            "use_existing_audiobook",
            "high",
            "matched audiobook edition on the selected Hardcover book",
        )
    if len(audio_editions) == 1:
        metadata_matches, evidence = hardcover_audio_metadata_matches(book, audio_editions[0])
        return hardcover_plan_from_edition(
            audio_editions[0],
            "use_existing_audiobook" if metadata_matches else "review_existing_audiobook",
            "medium",
            evidence,
        )
    if len(audio_editions) > 1:
        return HardcoverPlan(
            "review_audiobook_editions",
            "low",
            "found multiple audiobook editions without a matching ABS identifier",
            hardcover_url=hardcover_url,
            book_id=book_id,
            alternatives=tuple(
                url for edition in audio_editions if (url := hardcover_edition_url(edition))
            ),
        )
    return HardcoverPlan(
        "create_audiobook_edition",
        "high",
        "matched Hardcover book has no audiobook edition",
        hardcover_url=hardcover_url,
        book_id=book_id,
        cover_needed=True,
    )


def hardcover_editions_for_book(book_id: int, clients: ApiClients) -> list[dict[str, Any]]:
    query = """
    query HardcoverBookEditions($bookId: Int!) {
      editions(where: {book_id: {_eq: $bookId}}, limit: 50) {
        id title isbn_13 isbn_10 asin physical_format audio_seconds release_date
        reading_format { id format }
        language { id language code2 }
        image { id url }
        book { id slug title }
        contributions { author { id name } contribution }
      }
    }
    """
    data = clients.hardcover_query(query, {"bookId": book_id})
    editions = data.get("editions")
    if not isinstance(editions, list):
        raise LookupErrorWithDetail(f"Hardcover editions missing for book {book_id}")
    return [edition for edition in editions if isinstance(edition, dict)]


def hardcover_search_candidates(results: Any, book: Book) -> list[tuple[int, dict[str, Any]]]:
    if isinstance(results, dict):
        results = results.get("hits")
    if not isinstance(results, list):
        return []
    scored = []
    for item in results:
        document = item.get("document") if isinstance(item, dict) else None
        candidate = document if isinstance(document, dict) else item
        if not isinstance(candidate, dict):
            continue
        candidate_title = str(candidate.get("title") or "")
        if candidate_title and not matching_title_numbers(book.title, candidate_title):
            continue
        score = hardcover_book_candidate_score(candidate, book)
        if score >= MIN_TITLE_AUTHOR_SCORE:
            scored.append((score, candidate))
    return sorted(scored, key=lambda item: item[0], reverse=True)


def hardcover_book_candidate_score(candidate: dict[str, Any], book: Book) -> int:
    candidate_title = normalize_text(str(candidate.get("title") or ""))
    title_score = 0
    for title in unique([book.title, book.title.split(":", maxsplit=1)[0].strip()]):
        requested_title = normalize_text(title)
        if not requested_title or not candidate_title:
            continue
        if requested_title == candidate_title:
            title_score = max(title_score, 8)
        elif requested_title.startswith(candidate_title) or candidate_title.startswith(
            requested_title
        ):
            title_score = max(title_score, 4)

    requested_author = normalize_text(first_author(book.author))
    candidate_authors = normalize_text(" ".join(hardcover_candidate_authors(candidate)))
    if not title_score or not requested_author or requested_author not in candidate_authors:
        return 0

    score = title_score + 3
    if book.published_year and str(book.published_year) in hardcover_candidate_years(candidate):
        score += 2
    return score


def hardcover_candidate_authors(candidate: dict[str, Any]) -> list[str]:
    names = candidate.get("author_names")
    if isinstance(names, str):
        return [names]
    if isinstance(names, list):
        return [str(name) for name in names if name]

    contributions = candidate.get("contributions")
    if not isinstance(contributions, list):
        return []
    return [
        str(author["name"])
        for contribution in contributions
        if isinstance(contribution, dict)
        and isinstance((author := contribution.get("author")), dict)
        and author.get("name")
    ]


def hardcover_candidate_years(candidate: dict[str, Any]) -> set[str]:
    years = set()
    for key in ("release_year", "original_release_year", "publication_year"):
        value = candidate.get(key)
        if value is not None:
            years.add(str(value))
    for key in ("release_date", "original_release_date"):
        value = str(candidate.get(key) or "")
        match = re.match(r"(\d{4})", value)
        if match:
            years.add(match.group(1))
    return years


def hardcover_plan_from_edition(
    edition: dict[str, Any], action: str, confidence: str, evidence: str
) -> HardcoverPlan:
    edition_book = edition.get("book")
    book_id = (
        clean_optional_int(edition_book.get("id"))
        if isinstance(edition_book, dict)
        else None
    )
    return HardcoverPlan(
        action,
        confidence,
        evidence,
        hardcover_url=hardcover_edition_url(edition),
        book_id=book_id,
        edition_id=clean_optional_int(edition.get("id")),
        cover_needed=not hardcover_edition_has_cover(edition),
        hardcover_duration_seconds=clean_optional_int(edition.get("audio_seconds")),
        hardcover_language=hardcover_edition_language(edition),
    )


def hardcover_audio_metadata_matches(book: Book, edition: dict[str, Any]) -> tuple[bool, str]:
    hardcover_duration = clean_optional_int(edition.get("audio_seconds"))
    if book.duration_seconds is None or hardcover_duration is None:
        return False, "sole audiobook edition is missing duration data"

    duration_delta = abs(book.duration_seconds - hardcover_duration)
    if duration_delta > HARDCOVER_DURATION_TOLERANCE_SECONDS:
        return False, f"sole audiobook duration differs from ABS by {duration_delta} seconds"

    hardcover_language = hardcover_edition_language(edition)
    if (
        book.language
        and hardcover_language
        and normalize_text(book.language) != normalize_text(hardcover_language)
    ):
        return False, "sole audiobook language differs from ABS"

    return True, f"sole audiobook duration is within {duration_delta} seconds of ABS"


def hardcover_edition_language(edition: dict[str, Any]) -> str | None:
    language = edition.get("language")
    if isinstance(language, dict):
        return clean_optional_string(language.get("language"))
    return None


def hardcover_fix_reasons(book: Book, plan: HardcoverPlan) -> list[str]:
    reasons = []
    if plan.action in {"create_book_and_audiobook_edition", "review_book_matches"}:
        reasons.append("book not identified")
    if plan.action in {"create_book_and_audiobook_edition", "create_audiobook_edition"}:
        reasons.append("audiobook edition missing")
    if plan.action == "review_audiobook_editions":
        reasons.append("audiobook edition not identified")
    if plan.action == "review_existing_audiobook":
        reasons.append("audiobook metadata needs review")
    if plan.action == "error":
        reasons.append("lookup error")
    if plan.edition_id is not None and plan.hardcover_duration_seconds is None:
        reasons.append("duration missing")
    if plan.cover_needed:
        reasons.append("cover missing")
    return reasons


def hardcover_existing_edition_issues(
    book: Book, edition_id: int, clients: ApiClients
) -> list[str]:
    data = clients.hardcover_query(
        """
        query HardcoverEditionMetadata($editionId: Int!) {
          editions(where: {id: {_eq: $editionId}}, limit: 1) {
            id title subtitle isbn_13 isbn_10 asin audio_seconds release_date
            reading_format { id format }
            language { id language code2 }
            country { id name code2 }
            publisher { id name }
            image { id url }
            contributions {
              author { id name }
              contribution
              contributor_role { id name }
            }
          }
        }
        """,
        {"editionId": edition_id},
    )
    editions = data.get("editions")
    if not isinstance(editions, list) or len(editions) != 1:
        return ["edition metadata unavailable"]
    edition = editions[0]
    if not isinstance(edition, dict):
        return ["edition metadata unavailable"]

    issues = []
    edition_title = clean_optional_string(edition.get("title"))
    if edition_title != book.title:
        issues.append(f"title needs update: {book.title}")
    if book.subtitle:
        edition_subtitle = clean_optional_string(edition.get("subtitle"))
        if edition_subtitle != book.subtitle:
            issues.append(f"subtitle needs update: {book.subtitle}")
    if book.asin:
        asin = clean_identifier(edition.get("asin"))
        if not asin:
            issues.append("ASIN missing")
        elif asin != clean_identifier(book.asin):
            issues.append(f"ASIN needs update: {book.asin}")

    if book.isbn:
        edition_isbns = {
            clean_identifier(edition.get("isbn_13")),
            clean_identifier(edition.get("isbn_10")),
        } - {None}
        expected_isbns = {
            clean_identifier(book.isbn),
            clean_identifier(isbn13_to_isbn10(book.isbn)),
        } - {None}
        if not edition_isbns:
            issues.append("ISBN missing")
        elif not edition_isbns & expected_isbns:
            issues.append(f"ISBN needs update: {book.isbn}")

    duration = clean_optional_int(edition.get("audio_seconds"))
    if book.duration_seconds is not None:
        if duration is None:
            issues.append("duration missing")
        elif duration != book.duration_seconds:
            issues.append(f"duration needs update: {book.duration_seconds}")

    if book.published_date:
        release_date = clean_optional_string(edition.get("release_date"))
        if not release_date:
            issues.append("publication date missing")
        elif release_date != book.published_date:
            issues.append(f"publication date needs update: {book.published_date}")

    language = hardcover_edition_language(edition)
    if book.language:
        if not language:
            issues.append("language missing")
        elif normalize_text(language) != normalize_text(book.language):
            issues.append(f"language needs update: {book.language}")

    if book.asin:
        country = edition.get("country")
        country_code = (
            clean_optional_string(country.get("code2"))
            if isinstance(country, dict)
            else None
        )
        if not country_code:
            issues.append("country missing")
        elif country_code.casefold() != "us":
            issues.append("country needs update: United States of America (us)")

    if book.publisher:
        publisher = edition.get("publisher")
        publisher_name = (
            clean_optional_string(publisher.get("name"))
            if isinstance(publisher, dict)
            else None
        )
        if not publisher_name:
            issues.append("publisher missing")
        elif normalize_text(publisher_name) != normalize_text(book.publisher):
            issues.append(f"publisher needs update: {book.publisher}")

    contributions = edition.get("contributions")
    actual_contributions = set()
    if isinstance(contributions, list):
        for contribution in contributions:
            if not isinstance(contribution, dict):
                continue
            author = contribution.get("author")
            name = (
                clean_optional_string(author.get("name"))
                if isinstance(author, dict)
                else None
            )
            role = clean_optional_string(contribution.get("contribution"))
            if not role:
                contributor_role = contribution.get("contributor_role")
                role = (
                    clean_optional_string(contributor_role.get("name"))
                    if isinstance(contributor_role, dict)
                    else None
                )
            if name and role:
                actual_contributions.add((normalize_text(name), normalize_text(role)))
    for name, role, _role_id in hardcover_contributor_specs(book):
        if (normalize_text(name), normalize_text(role)) not in actual_contributions:
            issues.append(f"{role.casefold()} missing: {name}")

    if not hardcover_edition_has_cover(edition):
        issues.append("cover missing")
    return issues


def hardcover_edition_update_issues(issues: list[str]) -> list[str]:
    return [
        issue
        for issue in issues
        if issue != "cover missing"
        and issue != "edition metadata unavailable"
        and not issue.endswith("belongs to another Hardcover edition")
        and not issue.startswith("edition update failed:")
        and not issue.startswith("edition metadata check failed:")
    ]


def hardcover_reconciled_fix_reasons(
    existing: list[str], audited: list[str]
) -> list[str]:
    stale_catalog_issue = re.compile(
        r"^(?:author|translator|narrator|publisher) (?:not found:|needs review$)"
    )
    retained = [
        issue
        for issue in existing
        if not stale_catalog_issue.match(issue)
        and issue not in {"country needs review", "cover missing"}
    ]
    return unique([*retained, *audited])


def hardcover_book_url(candidate: dict[str, Any], editions: bool = False) -> str | None:
    slug = clean_optional_string(candidate.get("slug"))
    if not slug:
        return None
    suffix = "/editions" if editions else ""
    return f"https://hardcover.app/books/{slug}{suffix}"


def hardcover_identifier_matches(edition: dict[str, Any], book: Book) -> bool:
    identifiers = {
        clean_identifier(edition.get("isbn_13")),
        clean_identifier(edition.get("isbn_10")),
        clean_identifier(edition.get("asin")),
    }
    return bool(identifiers & {book.isbn, book.asin} - {None})


def hardcover_edition_has_cover(edition: dict[str, Any]) -> bool:
    image = edition.get("image")
    return isinstance(image, dict) and bool(image.get("id") or image.get("url"))


def hardcover_exact_match(book: Book, clients: ApiClients) -> SiteResult | None:
    if not book.isbn and not book.asin:
        return None
    isbn13 = book.isbn or ""
    isbn10 = isbn13_to_isbn10(isbn13) or isbn13
    query = """
    query LookupEdition($isbn13: String!, $isbn10: String!, $asin: String!) {
      editions(
        where: {
          _or: [
            {isbn_13: {_eq: $isbn13}},
            {isbn_10: {_eq: $isbn10}},
            {asin: {_eq: $asin}}
          ]
        },
        limit: 20
      ) {
        id
        title
        isbn_13
        isbn_10
        asin
        physical_format
        reading_format { format }
        book { id slug title }
      }
    }
    """
    variables = {
        "isbn13": impossible_query_value(isbn13),
        "isbn10": impossible_query_value(isbn10),
        "asin": impossible_query_value(book.asin),
    }
    data = clients.hardcover_query(query, variables)
    editions = data.get("editions")
    if not isinstance(editions, list) or not editions:
        return None
    edition = best_hardcover_edition(editions, book)
    if not edition:
        return None
    url = hardcover_edition_url(edition)
    if not url:
        return None
    return SiteResult(url, "high", "matched Hardcover edition by ISBN/ASIN")


def hardcover_search_fallback(book: Book, clients: ApiClients) -> SiteResult | None:
    query = """
    query SearchBooks($q: String!, $type: String!) {
      search(query: $q, query_type: $type, per_page: 5, page: 1) {
        results
      }
    }
    """
    variables = {"q": f"{book.title} {first_author(book.author)}", "type": "Book"}
    data = clients.hardcover_query(query, variables)
    search = data.get("search")
    if not isinstance(search, dict):
        return None
    result = best_hardcover_search_result(search.get("results"), book)
    if not result:
        return None
    slug = result.get("slug")
    if not isinstance(slug, str) or not slug:
        return None
    url = f"https://hardcover.app/books/{slug}/editions"
    return SiteResult(url, "low", "fell back to Hardcover editions page")


def best_hardcover_edition(editions: list[dict[str, Any]], book: Book) -> dict[str, Any] | None:
    scored: list[tuple[int, dict[str, Any]]] = []
    for edition in editions:
        score = text_match_score(result_text(edition), book)
        if hardcover_is_audio(edition):
            score += 5
        if clean_identifier(edition.get("isbn_13")) == book.isbn:
            score += 10
        if clean_identifier(edition.get("isbn_10")) == book.isbn:
            score += 10
        if clean_identifier(edition.get("asin")) == book.asin:
            score += 10
        scored.append((score, edition))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    score, result = scored[0]
    if score < MIN_TITLE_AUTHOR_SCORE:
        return None
    return result


def hardcover_edition_url(edition: dict[str, Any]) -> str | None:
    edition_id = edition.get("id")
    book = edition.get("book")
    if not isinstance(book, dict):
        return None
    slug = book.get("slug")
    if not slug or edition_id is None:
        return None
    return f"https://hardcover.app/books/{slug}/editions/{edition_id}"


def hardcover_is_audio(edition: dict[str, Any]) -> bool:
    reading_format = edition.get("reading_format")
    values = [edition.get("physical_format"), edition.get("title")]
    if isinstance(reading_format, dict):
        if reading_format.get("id") == 2:
            return True
        values.append(reading_format.get("format"))
    return any(
        str(value).casefold() == "listened" or "audio" in str(value).casefold()
        for value in values
        if value
    )


def best_hardcover_search_result(results: Any, book: Book) -> dict[str, Any] | None:
    if isinstance(results, dict):
        results = results.get("hits")
    if not isinstance(results, list):
        return None
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in results:
        document = item.get("document") if isinstance(item, dict) else None
        candidate = document if isinstance(document, dict) else item
        if not isinstance(candidate, dict):
            continue
        score = text_match_score(result_text(candidate), book)
        scored.append((score, candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    score, result = scored[0]
    if score < MIN_TITLE_AUTHOR_SCORE:
        return None
    return result


def lookup_goodreads(book: Book, clients: ApiClients, config: LookupConfig) -> SiteResult:
    main_url = goodreads_main_url(book, clients, config)
    if not main_url:
        return SiteResult(None, "none", "no Goodreads search result")
    scrape = clients.firecrawl_scrape(main_url)
    work_id = goodreads_work_id(scrape)
    if not work_id:
        return SiteResult(main_url, "low", "found Goodreads book, but not work editions")
    slug = goodreads_slug(main_url) or slugify(book.title)
    editions_url = f"https://www.goodreads.com/work/editions/{work_id}-{slug}"
    fallback: str | None = None
    for page in range(1, config.goodreads_pages + 1):
        url = editions_url if page == 1 else f"{editions_url}?page={page}"
        try:
            editions = clients.firecrawl_scrape(url)
        except LookupErrorWithDetail as error:
            return SiteResult(
                editions_url,
                "low",
                "fell back to Goodreads editions page after scrape failure",
                str(error),
            )
        audible = find_goodreads_format(editions, "Audible Audio")
        if audible:
            return SiteResult(audible, "medium", "matched Goodreads Audible Audio edition")
        if not fallback:
            fallback = find_goodreads_format(editions, "Audiobook")
        if not has_goodreads_next_page(editions):
            break
    if fallback:
        return SiteResult(fallback, "medium", "fell back to Goodreads Audiobook edition")
    return SiteResult(editions_url, "low", "fell back to Goodreads editions page")


def goodreads_main_url(book: Book, clients: ApiClients, config: LookupConfig) -> str | None:
    search_url = goodreads_search_url(book)
    scrape = clients.firecrawl_scrape(search_url)
    url = best_goodreads_scrape_url(scrape, book)
    if url:
        return url
    if not config.use_firecrawl_search:
        return None
    query = f"{quote_search(book.title)} {quote_search(first_author(book.author))} Goodreads"
    results = clients.firecrawl_search(query, 8)
    scored: list[tuple[int, str]] = []
    for result in results:
        url = clean_goodreads_url(str(result.get("url") or ""))
        if not url:
            continue
        candidate_title = clean_optional_string(result.get("title"))
        if candidate_title and not matching_title_numbers(book.title, candidate_title):
            continue
        score = text_match_score(result_text(result), book)
        if candidate_title:
            score += goodreads_candidate_title_bonus(book.title, candidate_title)
        scored.append((score, url))
    if not scored:
        return None
    scored.sort(reverse=True)
    score, url = scored[0]
    if score < MIN_TITLE_AUTHOR_SCORE:
        return None
    return url


def goodreads_search_url(book: Book) -> str:
    query = f"{book.title} {first_author(book.author)}"
    encoded = urllib.parse.urlencode({"q": query})
    return f"https://www.goodreads.com/search?{encoded}"


def best_goodreads_scrape_url(scrape: dict[str, Any], book: Book) -> str | None:
    markdown = str(scrape.get("markdown") or "")
    scored: list[tuple[int, str]] = []
    for url in goodreads_book_urls(scrape):
        candidate_title = goodreads_candidate_title(markdown, url)
        if candidate_title and not matching_title_numbers(book.title, candidate_title):
            continue
        score = 0
        before = 0 if candidate_title else 200
        for window in markdown_windows_for_url(markdown, url, before, 1000):
            text = f"{candidate_title or ''} {window}".casefold()
            score = max(score, text_match_score(text, book))
        if candidate_title:
            score += goodreads_candidate_title_bonus(book.title, candidate_title)
        if score:
            scored.append((score, url))
    if not scored:
        return None
    scored.sort(reverse=True)
    score, url = scored[0]
    if score < MIN_TITLE_AUTHOR_SCORE:
        return None
    return url


def goodreads_candidate_title(markdown: str, url: str) -> str | None:
    pattern = rf"^#+\s+\[([^\]\n]+)\]\({re.escape(url)}\)"
    match = re.search(pattern, markdown, re.MULTILINE)
    if match:
        return match.group(1).strip()
    book_id = re.search(r"/book/show/(\d+)", url)
    if not book_id:
        return None
    pattern = rf"(?<!!)\[([^\]\n]+)\]\([^\)\n]*/book/show/{book_id.group(1)}(?:[-?][^\)]*)?\)"
    match = re.search(pattern, markdown)
    return match.group(1).strip() if match else None


def goodreads_work_id(scrape: dict[str, Any]) -> str | None:
    links = scrape.get("links") or []
    for text in [json.dumps(links), str(scrape.get("markdown") or "")]:
        match = re.search(r"goodreads\.com/(?:book/similar|work/quotes)/(\d+)", text)
        if match:
            return match.group(1)
        match = re.search(r"goodreads\.com/work/editions/(\d+)", text)
        if match:
            return match.group(1)
    return None


def goodreads_candidate_title_bonus(title: str, candidate_title: str) -> int:
    requested = title.strip().casefold()
    candidate = candidate_title.strip().casefold()
    if candidate == requested:
        return 6
    if candidate.startswith(f"{requested}:"):
        return 4
    if candidate.startswith(f"{requested} /"):
        return -2
    return 0


def find_goodreads_format(scrape: dict[str, Any], wanted_format: str) -> str | None:
    markdown = str(scrape.get("markdown") or "")
    urls = goodreads_book_urls(scrape)
    for url in urls:
        for window in markdown_windows_for_url(markdown, url, 900, 250):
            if wanted_format.casefold() in window.casefold():
                return url
    return None


def goodreads_book_urls(scrape: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for link in scrape.get("links") or []:
        url = clean_goodreads_url(str(link))
        if url:
            urls.append(url)
    markdown = str(scrape.get("markdown") or "")
    for match in GOODREADS_BOOK_RE.finditer(markdown):
        url = clean_goodreads_url(match.group(0))
        if url:
            urls.append(url)
    return unique(urls)


def markdown_windows_for_url(
    markdown: str,
    url: str,
    before: int,
    after: int,
) -> list[str]:
    windows = []
    patterns = [re.escape(url)]
    book_id = re.search(r"/book/show/(\d+)", url)
    if book_id:
        patterns.append(r"/book/show/" + re.escape(book_id.group(1)))
    for pattern in patterns:
        for match in re.finditer(pattern, markdown):
            start = max(0, match.start() - before)
            end = min(len(markdown), match.end() + after)
            windows.append(markdown[start:end])
    return windows


def has_goodreads_next_page(scrape: dict[str, Any]) -> bool:
    links = [str(link) for link in scrape.get("links") or []]
    return any("/work/editions/" in link and "page=" in link for link in links)


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
    before_attempt: Callable[[], None] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        if before_attempt:
            before_attempt()
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                content = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            content = error.read().decode("utf-8", errors="replace")
            if error.code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise LookupErrorWithDetail(
                f"HTTP {error.code} from {url}: {summarize_http_body(content)}",
                error.code,
            )
        except urllib.error.URLError as error:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise LookupErrorWithDetail(f"request failed for {url}: {error}")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as error:
            raise LookupErrorWithDetail(f"invalid JSON response from {url}: {error}")
        if isinstance(parsed, dict):
            if parsed.get("success") is False:
                raise LookupErrorWithDetail(f"API request failed for {url}: {parsed}")
            return parsed
        raise LookupErrorWithDetail(f"JSON response from {url} was not an object")
    raise LookupErrorWithDetail(f"request failed for {url}")


def bearer_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "abs-finished-urls/1.0",
    }


def firecrawl_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "abs-finished-urls/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def firecrawl_search_results(response: dict[str, Any]) -> list[dict[str, Any]]:
    for value in [response.get("data"), response.get("web")]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and isinstance(value.get("web"), list):
            return [item for item in value["web"] if isinstance(item, dict)]
    return []


def row_from_results(book: Book, site_results: dict[str, SiteResult]) -> dict[str, Any]:
    return {
        **asdict(book),
        "urls": {site: result.url for site, result in site_results.items()},
        "details": {site: asdict(result) for site, result in site_results.items()},
    }


def filter_rows(
    rows: list[dict[str, Any]], statuses: set[str] | None
) -> list[dict[str, Any]]:
    if not statuses:
        return rows
    return [
        row
        for row in rows
        if any(result_status(detail) in statuses for detail in row["details"].values())
    ]


def result_status(detail: dict[str, Any]) -> str:
    return "error" if detail.get("error") else str(detail.get("confidence") or "none")


def rows_have_errors(rows: list[dict[str, Any]]) -> bool:
    return any(
        detail.get("error")
        for row in rows
        for detail in row["details"].values()
    )


def print_snapshot(books: list[Book]) -> None:
    print(json.dumps([asdict(book) for book in books], indent=2, sort_keys=True))


def write_snapshot(path: Path, books: list[Book]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_text(path, json.dumps([asdict(book) for book in books], indent=2, sort_keys=True))
    except OSError as error:
        raise LookupErrorWithDetail(f"failed to write ABS snapshot to {path}: {error}")


def write_hardcover_csv(
    path: Path,
    books: list[Book],
    plan_records: list[dict[str, Any]] | None = None,
) -> None:
    plans_by_abs_id = {
        str(record.get("abs_id")): record
        for record in (plan_records or [])
        if record.get("abs_id")
    }
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=HARDCOVER_CSV_HEADERS)
    writer.writeheader()
    for book in books:
        writer.writerow(hardcover_csv_row(book, plans_by_abs_id))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_text(path, output.getvalue())
    except OSError as error:
        raise LookupErrorWithDetail(f"failed to write Hardcover CSV to {path}: {error}")


def hardcover_csv_row(
    book: Book,
    plans_by_abs_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    isbn = clean_identifier(book.isbn)
    isbn13 = isbn if isbn and len(isbn) == 13 else None
    isbn10 = isbn if isbn and len(isbn) == 10 else isbn13_to_isbn10(isbn13 or "")
    contributors = [
        hardcover_csv_contributor(name)
        for name in book.author.split(",")
        if name.strip()
    ]
    contributors.extend(
        f"{name.strip()} (Narrator)"
        for name in (book.narrator or "").split(",")
        if name.strip()
    )
    plan = (plans_by_abs_id or {}).get(book.abs_id or "", {})
    edition_id = plan.get("edition_id")
    book_id = plan.get("book_id") if not edition_id else None
    return {
        "Title": book.title,
        "Author": ", ".join(contributors),
        "Series": hardcover_csv_series(book.series_name),
        "Status": "Read",
        "Privacy": "",
        "Hardcover Book ID": str(book_id) if book_id else "",
        "Hardcover Edition ID": str(edition_id) if edition_id else "",
        "ISBN 10": isbn10 or "",
        "ISBN 13": isbn13 or "",
        "ASIN": book.asin or "",
        "Media": "Audiobook",
        "Country Code": "",
        "Language Code": hardcover_language_code(book.language),
        "Binding": "Audible Audio" if book.asin else "Audiobook",
        "Pages": "",
        "Duration in Seconds": book.duration_seconds or "",
        "Publish Date": hardcover_publish_date(book),
        "Publisher": book.publisher or "",
        "Genres": "",
        "Moods": "",
        "Tags": "",
        "Content Warnings": "",
        "Lists": "",
        "Date Added": "",
        "Date Started": book.started_at or "",
        "Date Finished": book.finished_at or "",
        "Rating": "",
        "Review": "",
        "Review Contains Spoilers": "No",
        "Sponsored Review": "No",
        "Review Date": "",
        "Review URL": "",
        "Review Media URL": "",
        "Private Notes": "",
        "Owned": "No",
        "Compilation": "No",
        "Review Slate": "",
    }


def hardcover_csv_series(series_name: str | None) -> str:
    return re.sub(r"\s+#([\d.]+)$", r" (#\1)", series_name or "")


def hardcover_csv_contributor(value: str) -> str:
    name = value.strip()
    match = re.fullmatch(r"(.+?)\s+-\s+(.+)", name)
    if not match:
        return name
    return f"{match.group(1)} ({match.group(2).title()})"


def hardcover_language_code(language: str | None) -> str:
    value = (language or "").strip().casefold()
    if len(value) in {2, 3}:
        return value
    return {"english": "en"}.get(value, "")


def hardcover_publish_date(book: Book) -> str:
    value = (book.published_date or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return ""


def hardcover_publish_date_issue(book: Book) -> str | None:
    if hardcover_publish_date(book):
        return None
    year = str(book.published_year or "").strip()
    if re.fullmatch(r"\d{4}", year):
        return f"publication date missing (known year: {year})"
    return "publication date missing"


def hardcover_audible_update_reasons(original: Book, audible: Book) -> list[str]:
    reasons = []
    for label, previous, current in (
        ("title", original.title, audible.title),
        ("subtitle", original.subtitle, audible.subtitle),
        ("author", original.author, audible.author),
        ("narrator", original.narrator, audible.narrator),
        ("publisher", original.publisher, audible.publisher),
        ("language", original.language, audible.language),
    ):
        if current and normalize_text(str(previous or "")) != normalize_text(str(current)):
            reasons.append(f"{label} needs update: {current}")
    if audible.isbn and clean_identifier(original.isbn) != clean_identifier(audible.isbn):
        reasons.append(f"ISBN needs update: {audible.isbn}")
    if (
        audible.duration_seconds
        and original.duration_seconds != audible.duration_seconds
    ):
        reasons.append(f"duration needs update: {audible.duration_seconds}")
    publication_date = hardcover_publish_date(audible)
    if publication_date and hardcover_publish_date(original) != publication_date:
        reasons.append(f"publication date needs update: {publication_date}")
    return reasons


def write_goodreads_export_artifacts(directory: Path, rows: list[dict[str, Any]]) -> None:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=GOODREADS_EXPORT_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow(goodreads_export_row(row))

    review_headers = [
        "title",
        "author",
        "reason",
        "goodreads_url",
        "confidence",
        "evidence",
        "error",
        "isbn",
        "asin",
        "amazon_url",
    ]
    review_tsv = ["\t".join(review_headers)]
    for row in rows:
        detail = row.get("details", {}).get("goodreads") or {}
        reason = goodreads_review_reason(detail)
        if not reason:
            continue
        book = book_from_record(row)
        review_tsv.append(
            "\t".join(
                tsv_cell(value)
                for value in [
                    book.title,
                    book.author,
                    reason,
                    detail.get("url"),
                    detail.get("confidence"),
                    detail.get("evidence"),
                    detail.get("error"),
                    book.isbn,
                    book.asin,
                    lookup_amazon(book).url,
                ]
            )
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        write_atomic_text(directory / "goodreads-import.csv", output.getvalue())
        write_atomic_text(directory / "goodreads-review.tsv", "\n".join(review_tsv) + "\n")
    except OSError as error:
        raise LookupErrorWithDetail(f"failed to write Goodreads export in {directory}: {error}")


def print_goodreads_export_summary(directory: Path, rows: list[dict[str, Any]]) -> None:
    review_count = sum(
        bool(goodreads_review_reason(row.get("details", {}).get("goodreads") or {}))
        for row in rows
    )
    print(
        f"Goodreads import: {len(rows)} books, {review_count} need review\n"
        f"  import: {directory / 'goodreads-import.csv'}\n"
        f"  review: {directory / 'goodreads-review.tsv'}"
    )


def goodreads_export_row(row: dict[str, Any]) -> dict[str, Any]:
    book = book_from_record(row)
    detail = row.get("details", {}).get("goodreads") or {}
    isbn = clean_identifier(book.isbn)
    isbn13 = isbn if isbn and len(isbn) == 13 else None
    isbn10 = isbn if isbn and len(isbn) == 10 else isbn13_to_isbn10(isbn13 or "")
    author, _, additional_authors = book.author.partition(",")
    date_read = (book.finished_at or "").replace("-", "/")
    return {
        "Book Id": goodreads_book_id(detail.get("url")),
        "Title": book.title,
        "Author": author.strip(),
        "Author l-f": "",
        "Additional Authors": additional_authors.strip(),
        "ISBN": goodreads_spreadsheet_identifier(isbn10),
        "ISBN13": goodreads_spreadsheet_identifier(isbn13),
        "My Rating": 0,
        "Average Rating": "",
        "Publisher": book.publisher or "",
        "Binding": "Audible Audio" if book.asin else "Audiobook",
        "Number of Pages": "",
        "Year Published": book.published_year or "",
        "Original Publication Year": "",
        "Date Read": date_read,
        "Date Added": date_read,
        "Bookshelves": "",
        "Bookshelves with positions": "",
        "Exclusive Shelf": "read",
        "My Review": "",
        "Spoiler": "",
        "Private Notes": "",
        "Read Count": 1,
        "Owned Copies": 0,
    }


def goodreads_book_id(url: Any) -> str:
    match = re.search(r"goodreads\.com/book/show/(\d+)", str(url or ""))
    return match.group(1) if match else ""


def goodreads_review_reason(detail: dict[str, Any]) -> str | None:
    if detail.get("error"):
        return "Goodreads lookup error"
    if not detail.get("url"):
        return "no Goodreads match"
    if not goodreads_book_id(detail.get("url")):
        return "no direct Goodreads book ID"
    if detail.get("confidence") == "low":
        return "low-confidence Goodreads match"
    return None


def goodreads_spreadsheet_identifier(identifier: str | None) -> str:
    return f'="{identifier}"' if identifier else ""


def write_hardcover_plan_artifacts(
    directory: Path,
    books: list[Book],
    plans: list[HardcoverPlan],
) -> None:
    rows = [{**asdict(book), **asdict(plan)} for book, plan in zip(books, plans)]
    headers = list(rows[0]) if rows else []
    plan_tsv = ["\t".join(headers)] if headers else []
    for row in rows:
        plan_tsv.append("\t".join(plan_tsv_cell(row.get(header)) for header in headers))

    cover_headers = [
        "title",
        "author",
        "action",
        "hardcover_url",
        "amazon_url",
        "asin",
        "abs_cover_path",
        "reason",
    ]
    cover_tsv = ["\t".join(cover_headers)]
    for book, plan in zip(books, plans):
        if not plan.cover_needed:
            continue
        cover_tsv.append(
            "\t".join(
                tsv_cell(value)
                for value in [
                    book.title,
                    book.author,
                    plan.action,
                    plan.hardcover_url,
                    lookup_amazon(book).url,
                    book.asin,
                    book.cover_path,
                    plan.evidence,
                ]
            )
        )

    fix_headers = [
        "title",
        "author",
        "issues",
        "action",
        "hardcover_url",
        "amazon_url",
        "abs_duration_seconds",
        "hardcover_duration_seconds",
        "abs_language",
        "hardcover_language",
        "abs_cover_path",
    ]
    fix_tsv = ["\t".join(fix_headers)]
    for book, plan in zip(books, plans):
        reasons = hardcover_fix_reasons(book, plan)
        if not reasons:
            continue
        fix_tsv.append(
            "\t".join(
                tsv_cell(value)
                for value in [
                    book.title,
                    book.author,
                    "; ".join(reasons),
                    plan.action,
                    plan.hardcover_url,
                    lookup_amazon(book).url,
                    book.duration_seconds,
                    plan.hardcover_duration_seconds,
                    book.language,
                    plan.hardcover_language,
                    book.cover_path,
                ]
            )
        )

    try:
        directory.mkdir(parents=True, exist_ok=True)
        write_atomic_text(directory / "hardcover-plan.json", json.dumps(rows, indent=2, sort_keys=True))
        write_atomic_text(directory / "hardcover-plan.tsv", "\n".join(plan_tsv) + "\n")
        write_atomic_text(directory / "cover-todo.tsv", "\n".join(cover_tsv) + "\n")
        write_atomic_text(directory / "fix-list.tsv", "\n".join(fix_tsv) + "\n")
    except OSError as error:
        raise LookupErrorWithDetail(f"failed to write Hardcover plan in {directory}: {error}")


def print_hardcover_plan_summary(
    directory: Path, books: list[Book], plans: list[HardcoverPlan]
) -> None:
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan.action] = counts.get(plan.action, 0) + 1
    print(f"Hardcover plan: {len(plans)} books")
    for action, count in sorted(counts.items()):
        print(f"  {action}: {count}")
    fixes = [
        (book, hardcover_fix_reasons(book, plan))
        for book, plan in zip(books, plans)
        if hardcover_fix_reasons(book, plan)
    ]
    print(f"Fix list: {len(fixes)} books")
    for book, reasons in fixes:
        print(f"  {book.title}: {', '.join(reasons)}")
    print(f"  fix file: {directory / 'fix-list.tsv'}")
    print(f"  files: {directory}")


def print_human(rows: list[dict[str, Any]], sites: list[str]) -> None:
    for index, row in enumerate(rows, start=1):
        book = book_from_record(row)
        print(book_progress_message(index, len(rows), book))
        for site in sites:
            detail = row["details"].get(site)
            if detail:
                print(site_result_message(site, SiteResult(**detail)))


def print_tsv(rows: list[dict[str, Any]], sites: list[str]) -> None:
    headers = ["title", "author", "isbn", "asin", "started_at", "finished_at"]
    for site in sites:
        headers.extend(
            [
                f"{site}_url",
                f"{site}_confidence",
                f"{site}_evidence",
                f"{site}_error",
            ]
        )
    print("\t".join(headers))
    for row in rows:
        values = [
            row.get("title"),
            row.get("author"),
            row.get("isbn"),
            row.get("asin"),
            row.get("started_at"),
            row.get("finished_at"),
        ]
        for site in sites:
            detail = row["details"].get(site) or {}
            values.extend(
                [
                    detail.get("url"),
                    detail.get("confidence"),
                    detail.get("evidence"),
                    detail.get("error"),
                ]
            )
        print("\t".join(tsv_cell(value) for value in values))


def clean_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return re.sub(r"[^0-9A-Za-zXx]", "", text).upper()


def clean_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clean_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def impossible_query_value(value: str | None) -> str:
    return value or "__ABS_FINISHED_URLS_NO_MATCH__"


def cache_key(site: str, book: Book) -> str:
    parts = [site, book.title, book.author, book.isbn or "", book.asin or ""]
    return "|".join(part.strip().casefold() for part in parts)


def clean_goodreads_url(url: str) -> str | None:
    match = GOODREADS_BOOK_RE.search(url)
    if not match:
        return None
    parsed = urllib.parse.urlparse(match.group(0))
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def goodreads_slug(url: str) -> str | None:
    match = re.search(r"/book/show/\d+-([\w.-]+)", url)
    if match:
        return match.group(1)
    return None


def quote_search(text: str) -> str:
    escaped = text.replace('"', "")
    return f'"{escaped}"'


def first_author(author: str) -> str:
    return author.split(",", maxsplit=1)[0].strip()


def result_text(value: Any) -> str:
    if isinstance(value, dict):
        pieces = []
        for item in value.values():
            if isinstance(item, (str, int, float)):
                pieces.append(str(item))
            elif isinstance(item, dict):
                pieces.append(result_text(item))
            elif isinstance(item, list):
                pieces.extend(result_text(child) for child in item)
        return " ".join(pieces).casefold()
    return str(value).casefold()


def text_match_score(text: str, book: Book, title: str | None = None) -> int:
    score = 0
    normalized_text = normalize_text(text)
    normalized_title = normalize_text(title or book.title)
    normalized_author = normalize_text(first_author(book.author))
    if normalized_title and normalized_title in normalized_text:
        score += 4
    if normalized_author and normalized_author in normalized_text:
        score += 3
    if book.isbn and book.isbn.casefold() in text:
        score += 6
    if book.asin and book.asin.casefold() in text:
        score += 6
    return score


def normalize_text(text: str) -> str:
    normalized = "".join(
        character if character.isalnum() else " " for character in text.casefold()
    )
    return " ".join(normalized.split())


def isbn13_to_isbn10(isbn13: str) -> str | None:
    if not re.fullmatch(r"978\d{10}", isbn13):
        return None
    body = isbn13[3:12]
    total = sum((10 - index) * int(digit) for index, digit in enumerate(body))
    check = (11 - (total % 11)) % 11
    check_digit = "X" if check == 10 else str(check)
    return f"{body}{check_digit}"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return slug or "book"


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def tsv_cell(value: Any) -> str:
    return str(value or "").replace("\t", " ").replace("\n", " ")


def plan_tsv_cell(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = " | ".join(str(item) for item in value)
    return tsv_cell(value)


def write_atomic_text(path: Path, content: str) -> None:
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(content)
    tmp_path.replace(path)


def summarize_http_body(content: str, limit: int = 240) -> str:
    summary = re.sub(r"\s+", " ", content).strip()
    if len(summary) <= limit:
        return summary
    return f"{summary[: limit - 3]}..."


def sleep_between_requests(config: LookupConfig) -> None:
    if config.sleep_seconds > 0:
        time.sleep(config.sleep_seconds)


def log(config: LookupConfig, message: str) -> None:
    if not config.quiet:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
