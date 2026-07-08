#!/usr/bin/env python3
"""Find tracking-site URLs for recently finished Audiobookshelf books."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_FIRECRAWL_API_URL = "https://firecrawl.samesies.gay/v1"
DEFAULT_ABS_BASE_URL = "https://abs.samesies.gay"
DEFAULT_ABS_LIBRARY_ID = "98216a96-0651-43e3-b6a5-44e192b53e03"
HARDCOVER_API_URL = "https://api.hardcover.app/v1/graphql"
SUPPORTED_SITES = {"amazon", "goodreads", "hardcover", "storygraph"}
NETWORK_SITES = {"goodreads", "hardcover", "storygraph"}
STORYGRAPH_BOOK_RE = re.compile(
    r"https://app\.thestorygraph\.com/books/[0-9a-fA-F-]+"
)
GOODREADS_BOOK_RE = re.compile(
    r"https://www\.goodreads\.com/book/show/\d+(?:[-\w.]*)?"
)
MIN_TITLE_AUTHOR_SCORE = 7


@dataclass(frozen=True)
class Book:
    title: str
    author: str
    isbn: str | None
    asin: str | None
    abs_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class SiteResult:
    url: str | None
    confidence: str
    evidence: str
    error: str | None = None


@dataclass
class LookupConfig:
    sites: set[str]
    sleep_seconds: float
    timeout_seconds: float
    goodreads_pages: int
    use_firecrawl_search: bool
    quiet: bool


class LookupErrorWithDetail(RuntimeError):
    """Raised when an external lookup fails with a useful message."""


class JsonCache:
    def __init__(self, path: Path, enabled: bool) -> None:
        self.path = path
        self.enabled = enabled
        self.data: dict[str, dict[str, Any]] = {}
        if enabled:
            self.data = self._load()

    def get(self, site: str, book: Book) -> SiteResult | None:
        if not self.enabled:
            return None
        key = cache_key(site, book)
        record = self.data.get(key)
        if not record:
            return None
        result = SiteResult(**record)
        if not result.url:
            del self.data[key]
            return None
        return result

    def put(self, site: str, book: Book, result: SiteResult) -> None:
        if self.enabled and result.url:
            self.data[cache_key(site, book)] = asdict(result)

    def save(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp_path.replace(self.path)

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
        self.firecrawl_url = os.environ.get("FIRECRAWL_API_URL", DEFAULT_FIRECRAWL_API_URL)
        self.firecrawl_key = os.environ.get("FIRECRAWL_API_KEY")
        self.hardcover_key = os.environ.get("HARDCOVER_API_KEY")
        self.abs_url = os.environ.get("ABS_BASE_URL", DEFAULT_ABS_BASE_URL)
        self.abs_library_id = os.environ.get("ABS_LIBRARY_ID", DEFAULT_ABS_LIBRARY_ID)
        self.abs_key = os.environ.get("ABS_API_KEY")

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

    def hardcover_query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.hardcover_key:
            raise LookupErrorWithDetail("HARDCOVER_API_KEY is not set")
        response = post_json(
            HARDCOVER_API_URL,
            {"query": query, "variables": variables},
            bearer_headers(self.hardcover_key),
            self.timeout_seconds,
        )
        errors = response.get("errors")
        if errors:
            messages = "; ".join(error.get("message", str(error)) for error in errors)
            raise LookupErrorWithDetail(f"Hardcover GraphQL error: {messages}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise LookupErrorWithDetail("Hardcover GraphQL response did not include data")
        return data

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
            raise LookupErrorWithDetail(f"HTTP {error.code} from {url}: {content}")
        except urllib.error.URLError as error:
            raise LookupErrorWithDetail(f"request failed for {url}: {error}")
        try:
            return json.loads(content)
        except json.JSONDecodeError as error:
            raise LookupErrorWithDetail(f"invalid JSON response from {url}: {error}")

    def abs_endpoint(self, path: str, query: dict[str, str] | None = None) -> str:
        url = f"{self.abs_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            return f"{url}?{urllib.parse.urlencode(query)}"
        return url


def main() -> int:
    args = parse_args()
    config = LookupConfig(
        sites=parse_sites(args.sites),
        sleep_seconds=args.sleep,
        timeout_seconds=args.timeout,
        goodreads_pages=args.goodreads_pages,
        use_firecrawl_search=args.use_firecrawl_search,
        quiet=args.quiet,
    )
    unknown_sites = config.sites - SUPPORTED_SITES
    if unknown_sites:
        print(f"unknown site(s): {', '.join(sorted(unknown_sites))}", file=sys.stderr)
        return 2

    try:
        clients = ApiClients(args.timeout)
        books = load_books(args, clients)
        if args.limit:
            books = books[: args.limit]
        # Audiobookshelf fetches newest-first so the cutoff regex can stop early.
        books = list(reversed(books))
        cache = JsonCache(args.cache, not args.no_cache)
        rows = lookup_books(books, clients, cache, config)
        cache.save()
    except LookupErrorWithDetail as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    elif args.tsv:
        print_tsv(rows, config.sites)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch finished Audiobookshelf books and find StoryGraph, "
            "Hardcover, Goodreads, and Amazon URLs."
        )
    )
    parser.add_argument("since", help="stop before the first finished title matching this regex")
    parser.add_argument(
        "--book-json",
        type=Path,
        help="read books from a JSON file instead of querying Audiobookshelf",
    )
    parser.add_argument(
        "--sites",
        default="storygraph,hardcover,goodreads,amazon",
        help="comma-separated sites to query: storygraph,hardcover,goodreads,amazon",
    )
    parser.add_argument("--limit", type=int, help="limit rows for testing")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    parser.add_argument("--tsv", action="store_true", help="emit a final TSV summary")
    parser.add_argument("--quiet", action="store_true", help="suppress progress logs")
    parser.add_argument("--no-cache", action="store_true", help="disable local lookup cache")
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
        type=int,
        default=2,
        help="maximum Goodreads editions pages to scan",
    )
    parser.add_argument(
        "--use-firecrawl-search",
        action="store_true",
        help="fall back to Firecrawl /search when direct site search scraping misses",
    )
    return parser.parse_args()


def parse_sites(sites: str) -> set[str]:
    return {site.strip() for site in sites.split(",") if site.strip()}


def default_cache_path() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        return Path(cache_home) / "abs-finished-urls" / "cache.json"
    return Path.home() / ".cache" / "abs-finished-urls" / "cache.json"


def load_books(args: argparse.Namespace, clients: ApiClients) -> list[Book]:
    if args.book_json:
        return books_from_json_path(args.book_json)
    return audiobookshelf_finished_books(args.since, clients)


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


def audiobookshelf_finished_books(since: str, clients: ApiClients) -> list[Book]:
    response = clients.audiobookshelf_json(abs_library_items_path(clients), abs_finished_query())
    results = response.get("results") if isinstance(response, dict) else None
    if not isinstance(results, list):
        raise LookupErrorWithDetail("Audiobookshelf library response did not include results")
    books = []
    pattern = re.compile(since)
    for item in results:
        book = book_from_abs_item(item)
        if pattern.search(book.title):
            break
        book = book_with_abs_progress(book, clients)
        books.append(book)
    return books


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
    }
    return book_from_record(record)


def book_with_abs_progress(book: Book, clients: ApiClients) -> Book:
    if not book.abs_id:
        return book
    progress = clients.audiobookshelf_json(f"/api/me/progress/{book.abs_id}")
    if not isinstance(progress, dict):
        raise LookupErrorWithDetail(f"Audiobookshelf progress missing for {book.abs_id}")
    return Book(
        title=book.title,
        author=book.author,
        isbn=book.isbn,
        asin=book.asin,
        abs_id=book.abs_id,
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
    )


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
    for index, book in enumerate(books, start=1):
        log(config, book_progress_message(index, len(books), book))
        site_results: dict[str, SiteResult] = {}
        for site in sorted(config.sites):
            site_results[site] = lookup_site(site, book, clients, cache, config)
        rows.append(row_from_results(book, site_results))
    return rows


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
        return f"  {site}: {result.url}"
    if result.error:
        return f"  {site}: {result.error}"
    return f"  {site}: not found"


def lookup_amazon(book: Book) -> SiteResult:
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
    search_url = storygraph_search_url(book)
    scrape = clients.firecrawl_scrape(search_url)
    url = best_storygraph_scrape_url(scrape, book)
    if url:
        return url
    if not config.use_firecrawl_search:
        return None
    query = storygraph_query(book)
    results = clients.firecrawl_search(query, 8)
    return best_storygraph_result(results, book)


def storygraph_search_url(book: Book) -> str:
    query = f"{book.title} {first_author(book.author)}"
    encoded = urllib.parse.urlencode({"search_term": query})
    return f"https://app.thestorygraph.com/browse?{encoded}"


def storygraph_query(book: Book) -> str:
    parts = ["site:app.thestorygraph.com", quote_search(book.title)]
    parts.append(quote_search(first_author(book.author)))
    identifier = book.isbn or book.asin
    if identifier:
        parts.append(quote_search(identifier))
    return " ".join(parts)


def best_storygraph_scrape_url(scrape: dict[str, Any], book: Book) -> str | None:
    markdown = str(scrape.get("markdown") or "")
    scored: list[tuple[int, str]] = []
    for url in storygraph_book_urls(scrape):
        if "/editions" in url:
            continue
        score = 0
        for window in markdown_windows_for_url(markdown, url, 200, 1000):
            score = max(score, text_match_score(window.casefold(), book))
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
        text = result_text(result)
        score = text_match_score(text, book)
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
        values.append(reading_format.get("format"))
    return any("audio" in str(value).casefold() for value in values if value)


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
        score = text_match_score(result_text(result), book)
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
        score = 0
        for window in markdown_windows_for_url(markdown, url, 200, 1000):
            score = max(score, text_match_score(window.casefold(), book))
        if score:
            scored.append((score, url))
    if not scored:
        return None
    scored.sort(reverse=True)
    score, url = scored[0]
    if score < MIN_TITLE_AUTHOR_SCORE:
        return None
    return url


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
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                content = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            content = error.read().decode("utf-8", errors="replace")
            if error.code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise LookupErrorWithDetail(f"HTTP {error.code} from {url}: {content}")
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
        "title": book.title,
        "author": book.author,
        "isbn": book.isbn,
        "asin": book.asin,
        "abs_id": book.abs_id,
        "started_at": book.started_at,
        "finished_at": book.finished_at,
        "urls": {site: result.url for site, result in site_results.items()},
        "details": {site: asdict(result) for site, result in site_results.items()},
    }


def print_tsv(rows: list[dict[str, Any]], sites: set[str]) -> None:
    ordered_sites = [
        site for site in ["storygraph", "hardcover", "goodreads", "amazon"] if site in sites
    ]
    headers = ["title", "author", "isbn", "asin", "started_at", "finished_at", *ordered_sites]
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
        values.extend(row["urls"].get(site) for site in ordered_sites)
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


def text_match_score(text: str, book: Book) -> int:
    score = 0
    normalized_text = normalize_text(text)
    if normalize_text(book.title) in normalized_text:
        score += 4
    if normalize_text(first_author(book.author)) in normalized_text:
        score += 3
    if book.isbn and book.isbn.casefold() in text:
        score += 6
    if book.asin and book.asin.casefold() in text:
        score += 6
    return score


def normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


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


def sleep_between_requests(config: LookupConfig) -> None:
    if config.sleep_seconds > 0:
        time.sleep(config.sleep_seconds)


def log(config: LookupConfig, message: str) -> None:
    if not config.quiet:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
