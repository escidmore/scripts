import importlib.util
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("abs-finished-urls.py")
SPEC = importlib.util.spec_from_file_location("abs_finished_urls", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import {SCRIPT}")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeAbsClient:
    abs_library_id = "library"

    def __init__(self):
        self.progress_ids = []

    def audiobookshelf_json(self, path, query=None):
        if path.endswith("/items"):
            return {
                "results": [
                    self.item("1", "Newest"),
                    self.item("2", "Older"),
                    self.item("3", "Cutoff"),
                ]
            }
        book_id = path.rsplit("/", 1)[-1]
        self.progress_ids.append(book_id)
        return {"startedAt": 1_700_000_000_000, "finishedAt": 1_700_086_400_000}

    @staticmethod
    def item(book_id, title):
        return {
            "id": book_id,
            "media": {"metadata": {"title": title, "authorName": "Author"}},
        }


class FinishedUrlsTests(unittest.TestCase):
    def test_firecrawl_root_is_normalized_to_v1(self):
        with mock.patch.dict(
            os.environ, {"FIRECRAWL_API_URL": "https://firecrawl.example"}
        ):
            client = MODULE.ApiClients(1)

        self.assertEqual(
            client.firecrawl_endpoint("scrape"),
            "https://firecrawl.example/v1/scrape",
        )

    def test_audiobookshelf_cover_requests_the_raw_original(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"original cover"
        response.__enter__.return_value.headers.get_content_type.return_value = (
            "image/jpeg"
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "ABS_BASE_URL": "https://abs.example",
                    "ABS_API_KEY": "token",
                },
            ),
            mock.patch.object(
                MODULE.urllib.request, "urlopen", return_value=response
            ) as urlopen,
        ):
            client = MODULE.ApiClients(10)
            content, content_type = client.audiobookshelf_cover("item-1")

        self.assertEqual(content, b"original cover")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            "https://abs.example/api/items/item-1/cover?raw=1",
        )

    def test_audible_enrichment_prefers_catalog_metadata_and_exact_runtime(self):
        book = MODULE.Book(
            "The Original",
            "Nell Stevens",
            "old-isbn",
            "B0F1Z3P3XS",
            abs_id="abs-1",
            started_at="2026-05-12",
            finished_at="2026-05-27",
            subtitle="Old subtitle",
            narrator="Old Narrator",
            publisher="Old Publisher",
            published_year="2025",
            language="English",
            duration_seconds=36433,
            cover_path="/library/cover.jpg",
        )
        clients = mock.Mock()
        clients.audible_product.return_value = {
            "asin": "B0F1Z3P3XS",
            "title": "The Original",
            "subtitle": "A Novel",
            "authors": [{"name": "Nell Stevens"}],
            "narrators": [
                {"name": "Kristin Atherton"},
                {"name": "Matthew Spencer"},
            ],
            "publisher_name": "Blackstone Publishing",
            "isbn": "9798228487291",
            "language": "english",
            "release_date": "2025-07-01",
            "series": [{"title": "Example Series", "sequence": "2"}],
        }
        clients.audible_chapter_info.return_value = {
            "runtime_length_sec": 36444,
            "brandIntroDurationMs": 2043,
            "brandOutroDurationMs": 5061,
        }
        cache = {}

        enriched, issue = MODULE.audible_enrich_book(book, clients, cache)
        cached, cached_issue = MODULE.audible_enrich_book(book, clients, cache)

        self.assertIsNone(issue)
        self.assertIsNone(cached_issue)
        self.assertEqual(enriched, cached)
        self.assertEqual(enriched.subtitle, "A Novel")
        self.assertEqual(enriched.narrator, "Kristin Atherton, Matthew Spencer")
        self.assertEqual(enriched.publisher, "Blackstone Publishing")
        self.assertEqual(enriched.isbn, "9798228487291")
        self.assertEqual(enriched.published_date, "2025-07-01")
        self.assertEqual(enriched.duration_seconds, 36444)
        self.assertEqual(enriched.series_name, "Example Series #2")
        self.assertEqual(enriched.started_at, "2026-05-12")
        self.assertEqual(enriched.cover_path, "/library/cover.jpg")
        self.assertEqual(cache["B0F1Z3P3XS"]["intro_seconds"], 2.043)
        self.assertEqual(clients.audible_product.call_count, 1)
        self.assertEqual(clients.audible_chapter_info.call_count, 1)

    def test_audible_enrichment_normalizes_title_punctuation(self):
        book = MODULE.Book(
            "Welcome to the Blast",
            "Plum Parrot",
            None,
            "B0G1D3NZLJ",
            subtitle="Neon Dust, Book 1",
        )
        clients = mock.Mock()
        clients.audible_product.return_value = {
            "asin": "B0G1D3NZLJ",
            "title": "Welcome to the Blast : A Cyberpunk Adventure",
            "authors": [{"name": "Plum Parrot"}],
        }
        clients.audible_chapter_info.return_value = {}

        enriched, issue = MODULE.audible_enrich_book(book, clients, {})

        self.assertIsNone(issue)
        self.assertEqual(enriched.title, "Welcome to the Blast: A Cyberpunk Adventure")
        self.assertIsNone(enriched.subtitle)

    def test_audible_enrichment_rejects_a_mismatched_product(self):
        book = MODULE.Book("Expected Book", "Expected Author", None, "B012345678")
        clients = mock.Mock()
        clients.audible_product.return_value = {
            "asin": "B087654321",
            "title": "Different Book",
            "authors": [{"name": "Different Author"}],
        }
        cache = {}

        enriched, issue = MODULE.audible_enrich_book(book, clients, cache)

        self.assertEqual(enriched, book)
        self.assertIn("ASIN mismatch", issue)
        self.assertEqual(cache, {})
        clients.audible_chapter_info.assert_not_called()

    def test_limit_is_applied_before_progress_requests(self):
        client = FakeAbsClient()

        books = MODULE.audiobookshelf_finished_books(
            since="Cutoff",
            finished_after=None,
            include_all=False,
            limit=1,
            clients=client,
        )

        self.assertEqual([book.title for book in books], ["Newest"])
        self.assertEqual(client.progress_ids, ["1"])

    def test_finished_after_stops_at_the_date_cutoff(self):
        client = FakeAbsClient()
        finished_dates = {
            "1": "2026-08-03",
            "2": "2026-08-02",
            "3": "2026-08-01",
        }

        def add_progress(book, _client):
            return MODULE.Book(
                title=book.title,
                author=book.author,
                isbn=book.isbn,
                asin=book.asin,
                abs_id=book.abs_id,
                started_at=book.started_at,
                finished_at=finished_dates[book.abs_id],
            )

        with mock.patch.object(MODULE, "book_with_abs_progress", side_effect=add_progress):
            books = MODULE.audiobookshelf_finished_books(
                since=None,
                finished_after="2026-08-01",
                include_all=False,
                limit=None,
                clients=client,
            )

        self.assertEqual([book.title for book in books], ["Older", "Newest"])

    def test_unmatched_cutoff_fails_safely(self):
        with self.assertRaisesRegex(MODULE.LookupErrorWithDetail, "not found"):
            MODULE.audiobookshelf_finished_books(
                since="Missing",
                finished_after=None,
                include_all=False,
                limit=None,
                clients=FakeAbsClient(),
            )

    def test_single_book_selects_one_exact_title_case_insensitively(self):
        client = FakeAbsClient()

        books = MODULE.audiobookshelf_finished_books(
            since=None,
            finished_after=None,
            include_all=False,
            limit=None,
            clients=client,
            book_title="older",
        )

        self.assertEqual([book.title for book in books], ["Older"])
        self.assertEqual(client.progress_ids, ["2"])

    def test_single_book_is_a_standalone_selection_mode(self):
        args = MODULE.parse_args(["--book", "Older", "--sites", "storygraph"])

        self.assertEqual(args.book, "Older")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(["Cutoff", "--book", "Older"])

    def test_storygraph_url_requires_a_single_storygraph_book(self):
        url = "https://app.thestorygraph.com/books/88a7f1da-b043-4cef-bf10-b70ed161bbae"
        args = MODULE.parse_args(
            ["--book", "Goddess Alchemy", "--sites", "storygraph", "--storygraph-url", url]
        )

        self.assertEqual(args.storygraph_url, url)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(["--all", "--storygraph-url", url])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(
                ["--book", "Goddess Alchemy", "--sites", "amazon", "--storygraph-url", url]
            )

    def test_storygraph_url_bypasses_network_and_persists_during_refresh(self):
        book = MODULE.Book("Goddess Alchemy", "Rain Harlow", None, "B0GD8VLV5P")
        url = "https://app.thestorygraph.com/books/88a7f1da-b043-4cef-bf10-b70ed161bbae"
        config = MODULE.LookupConfig(
            sites=["storygraph"],
            sleep_seconds=0,
            timeout_seconds=1,
            goodreads_pages=1,
            use_firecrawl_search=False,
            quiet=True,
            storygraph_url=url,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = MODULE.JsonCache(path, enabled=True, refresh=True)
            with mock.patch.object(MODULE, "uncached_lookup_site") as lookup:
                rows = MODULE.lookup_books([book], mock.Mock(), cache, config)

            persisted = MODULE.JsonCache(path, enabled=True).get("storygraph", book)

        lookup.assert_not_called()
        self.assertEqual(rows[0]["details"]["storygraph"]["url"], url)
        self.assertEqual(persisted.url, url)
        self.assertEqual(persisted.evidence, "user-confirmed StoryGraph URL")

    def test_json_input_preserves_order_and_needs_no_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "books.json"
            path.write_text(
                json.dumps(
                    [
                        {"title": "First", "author": "Author"},
                        {"title": "Second", "author": "Author"},
                    ]
                )
            )
            args = MODULE.parse_args(["--book-json", str(path), "--limit", "1"])
            books = MODULE.load_books(args, mock.Mock())

        self.assertEqual([book.title for book in books], ["First"])

    def test_output_modes_are_mutually_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(["--all", "--json", "--tsv"])

        args = MODULE.parse_args(["--all", "--snapshot", "snapshot.json"])
        self.assertEqual(args.snapshot, Path("snapshot.json"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(["--all", "--snapshot", "snapshot.json", "--json"])

        args = MODULE.parse_args(
            ["--book-json", "books.json", "--goodreads-export", "exports"]
        )
        self.assertEqual(args.goodreads_export, Path("exports"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(
                [
                    "--book-json",
                    "books.json",
                    "--goodreads-export",
                    "exports",
                    "--json",
                ]
            )

        args = MODULE.parse_args(
            [
                "--book-json",
                "books.json",
                "--hardcover-csv",
                "hardcover.csv",
                "--hardcover-plan-json",
                "plan.json",
            ]
        )
        self.assertEqual(args.hardcover_csv, Path("hardcover.csv"))
        self.assertEqual(args.hardcover_plan_json, Path("plan.json"))

        args = MODULE.parse_args(
            [
                "--book-json",
                "books.json",
                "--hardcover-catchup",
                "catchup",
                "--hardcover-plan-json",
                "plan.json",
            ]
        )
        self.assertEqual(args.hardcover_catchup, Path("catchup"))
        self.assertEqual(args.hardcover_plan_json, Path("plan.json"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(
                [
                    "--book-json",
                    "books.json",
                    "--hardcover-csv",
                    "hardcover.csv",
                    "--json",
                ]
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(
                ["--book-json", "books.json", "--hardcover-plan-json", "plan.json"]
            )

    def test_title_and_date_cutoffs_are_mutually_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            MODULE.parse_args(["Cutoff", "--finished-after", "2026-08-01"])

    def test_site_order_is_preserved(self):
        self.assertEqual(
            MODULE.parse_sites("amazon,storygraph,amazon,goodreads"),
            ["amazon", "storygraph", "goodreads"],
        )

    def test_storygraph_retries_an_empty_browse_result(self):
        book = MODULE.Book(
            "Sweetbitter Song", "Rosie Hewlett", None, "B0GD2CZZZH"
        )
        url = "https://app.thestorygraph.com/books/be026e5e-dd0d-4126-9353-c91591a0fffb"
        clients = mock.Mock()
        clients.firecrawl_scrape.side_effect = [
            {},
            {
                "links": [url],
                "markdown": f"Sweetbitter Song by Rosie Hewlett ({url})",
            },
        ]
        config = MODULE.LookupConfig(
            sites=["storygraph"],
            sleep_seconds=0,
            timeout_seconds=1,
            goodreads_pages=1,
            use_firecrawl_search=False,
            quiet=True,
        )

        self.assertEqual(
            MODULE.storygraph_search_url(book),
            "https://app.thestorygraph.com/browse?search_term=Sweetbitter%20Song%20Rosie%20Hewlett",
        )
        result = MODULE.storygraph_main_url(book, clients, config)

        self.assertEqual(result, url)
        clients.firecrawl_search.assert_not_called()

    @mock.patch.object(MODULE.time, "sleep")
    def test_storygraph_retries_a_security_verification_page(self, sleep):
        book = MODULE.Book("Goddess Alchemy", "Rain Harlow", None, None)
        url = "https://app.thestorygraph.com/books/88a7f1da-b043-4cef-bf10-b70ed161bbae"
        clients = mock.Mock()
        clients.firecrawl_scrape.side_effect = [
            {"markdown": "Performing security verification. Waiting for app.thestorygraph.com to respond."},
            {
                "links": [url],
                "markdown": f"""
### [Goddess Alchemy: An Isekai LitRPG Fantasy]({url})
[Rain Harlow](https://example.com/author)
""",
            },
        ]
        config = MODULE.LookupConfig(
            sites=["storygraph"],
            sleep_seconds=0,
            timeout_seconds=1,
            goodreads_pages=1,
            use_firecrawl_search=False,
            quiet=True,
        )

        self.assertEqual(MODULE.storygraph_main_url(book, clients, config), url)
        sleep.assert_called_once_with(1)

    def test_storygraph_falls_back_to_title_without_subtitle(self):
        book = MODULE.Book(
            "Cinnamon Bun Volume 7: A Wholesome LitRPG",
            "RavensDagger",
            None,
            "B0FHL4HQBR",
        )
        url = "https://app.thestorygraph.com/books/18faaa0a-e680-4f60-aedb-1129787372e3"
        clients = mock.Mock()
        clients.firecrawl_scrape.side_effect = [
            {},
            {},
            {
                "links": [url],
                "markdown": f"""
### [Cinnamon Bun, Volume 7]({url})
[RavensDagger](https://example.com/author)
""",
            },
        ]
        config = MODULE.LookupConfig(
            sites=["storygraph"],
            sleep_seconds=0,
            timeout_seconds=1,
            goodreads_pages=1,
            use_firecrawl_search=False,
            quiet=True,
        )

        self.assertEqual(MODULE.storygraph_main_url(book, clients, config), url)
        self.assertEqual(
            clients.firecrawl_scrape.call_args_list[-1].args[0],
            "https://app.thestorygraph.com/browse?search_term=Cinnamon%20Bun%20Volume%207%20RavensDagger",
        )

    def test_storygraph_falls_back_to_spaced_author_initials(self):
        book = MODULE.Book("Lost Souls and a Demoness 2", "N.C. Lux", None, None)
        url = "https://app.thestorygraph.com/books/998ba503-aa63-4839-9502-59bef3799b06"
        clients = mock.Mock()
        clients.firecrawl_scrape.side_effect = [
            {},
            {},
            {
                "links": [url],
                "markdown": f"""
### [Lost Souls and a Demoness 2]({url})
[N. C. Lux](https://example.com/author)
""",
            },
        ]
        config = MODULE.LookupConfig(
            sites=["storygraph"],
            sleep_seconds=0,
            timeout_seconds=1,
            goodreads_pages=1,
            use_firecrawl_search=False,
            quiet=True,
        )

        self.assertEqual(MODULE.storygraph_main_url(book, clients, config), url)
        self.assertEqual(
            clients.firecrawl_scrape.call_args_list[-1].args[0],
            "https://app.thestorygraph.com/browse?search_term=Lost%20Souls%20and%20a%20Demoness%202%20N.%20C.%20Lux",
        )

    def test_storygraph_generic_search_does_not_require_asin(self):
        book = MODULE.Book(
            "Sweetbitter Song", "Rosie Hewlett", None, "B0GD2CZZZH"
        )
        query = MODULE.storygraph_query(book)

        self.assertIn('"Sweetbitter Song"', query)
        self.assertIn('"Rosie Hewlett"', query)
        self.assertNotIn(book.asin, query)

    def test_storygraph_prefers_the_matching_numbered_volume(self):
        book = MODULE.Book(
            "Bunny Girl Evolution 2", "Sir Bedivere the Mad", None, "B0GSXD1GNC"
        )
        volume_one = "https://app.thestorygraph.com/books/d2958d70-a844-4e50-9f81-c89467067c2b"
        volume_two = "https://app.thestorygraph.com/books/cf9a7e78-566d-495c-980a-0fabde778b5a"
        scrape = {
            "links": [volume_one, volume_two],
            "markdown": f"""
Search results for 'Bunny Girl Evolution 2 Sir Bedivere the Mad'
### [Bunny Girl Evolution 1]({volume_one})
[Sir Bedivere The Mad](https://example.com/author)
### [Bunny Girl Evolution 2: A Monster Evolution LitRPG]({volume_two})
[Sir Bedivere The Mad](https://example.com/author)
""",
        }

        self.assertEqual(MODULE.best_storygraph_scrape_url(scrape, book), volume_two)

    def test_storygraph_rejects_a_numbered_sequel_for_an_unnumbered_title(self):
        book = MODULE.Book("Andy in the Apocalypse", "Plum Parrot", None, None)
        sequel = "https://app.thestorygraph.com/books/1e75827a-e9e9-4316-9499-ceabb8cd2304"
        scrape = {
            "links": [sequel],
            "markdown": f"""
Search results for 'Andy in the Apocalypse Plum Parrot'
### [Andy in the Apocalypse 2]({sequel})
[Plum Parrot](https://example.com/author)
""",
        }

        self.assertIsNone(MODULE.best_storygraph_scrape_url(scrape, book))

    def test_goodreads_rejects_a_numbered_sequel_for_an_unnumbered_title(self):
        book = MODULE.Book(
            "Dressed to Kill: A Monster Seamstress LitRPG",
            "Crown Fall",
            None,
            "B0CR6NPFT2",
        )
        sequel = "https://www.goodreads.com/book/show/238465766-dressed-to-kill-2"
        original = "https://www.goodreads.com/book/show/214717827-dressed-to-kill"
        scrape = {
            "links": [sequel, original],
            "markdown": f"""
# Search results for Dressed to Kill: A Monster Seamstress LitRPG Crown Fall
### [Dressed to Kill 2]({sequel})
Crown Fall
### [Dressed to Kill: A Monster Seamstress LitRPG]({original})
Crown Fall
""",
        }

        self.assertEqual(MODULE.best_goodreads_scrape_url(scrape, book), original)

    def test_goodreads_prefers_the_book_over_a_study_guide_or_bundle(self):
        bi = MODULE.Book("Bi", "Julia Shaw", None, "B0B2KQ2HPT")
        bi_book = "https://www.goodreads.com/book/show/58667392-bi"
        bi_guide = "https://www.goodreads.com/book/show/61491082-study-guide-analysis-of-bi"
        bi_scrape = {
            "links": [bi_book, bi_guide],
            "markdown": f"""
### [Bi: The Hidden Culture, History, and Science of Bisexuality]({bi_book})
Julia Shaw
### [STUDY GUIDE, ANALYSIS OF Bi: The Hidden Culture, History, and Science of Bisexuality]({bi_guide})
Julia Shaw
""",
        }
        memory = MODULE.Book("The Memory Illusion", "Julia Shaw", None, "B01E9E62P4")
        memory_book = "https://www.goodreads.com/book/show/29610119-the-memory-illusion"
        memory_bundle = "https://www.goodreads.com/book/show/49369307-the-memory-illusion-making-evil"
        memory_scrape = {
            "links": [memory_book, memory_bundle],
            "markdown": f"""
### [The Memory Illusion: Remembering, Forgetting, and the Science of False Memory]({memory_book})
Julia Shaw
### [The Memory Illusion / Making Evil]({memory_bundle})
Julia Shaw
""",
        }

        self.assertEqual(MODULE.best_goodreads_scrape_url(bi_scrape, bi), bi_book)
        self.assertEqual(
            MODULE.best_goodreads_scrape_url(memory_scrape, memory), memory_book
        )

    def test_provider_failure_is_not_retried_for_sibling_sites(self):
        books = [
            MODULE.Book("One", "Author", None, None),
            MODULE.Book("Two", "Author", None, None),
        ]
        config = MODULE.LookupConfig(
            sites=["goodreads", "storygraph"],
            sleep_seconds=0,
            timeout_seconds=1,
            goodreads_pages=1,
            use_firecrawl_search=False,
            quiet=True,
        )
        cache = MODULE.JsonCache(Path("unused"), enabled=False)
        error = MODULE.LookupErrorWithDetail("provider route missing", status_code=404)

        with mock.patch.object(MODULE, "uncached_lookup_site", side_effect=error) as lookup:
            rows = MODULE.lookup_books(books, mock.Mock(), cache, config)

        self.assertEqual(lookup.call_count, 1)
        self.assertTrue(all(row["details"]["goodreads"]["error"] for row in rows))
        self.assertTrue(all(row["details"]["storygraph"]["error"] for row in rows))

    def test_cache_retries_low_confidence_and_error_results(self):
        book = MODULE.Book("Title", "Author", None, None)
        cache = MODULE.JsonCache(Path("unused"), enabled=True)

        cache.put("goodreads", book, MODULE.SiteResult("low", "low", "fallback"))
        cache.put(
            "storygraph",
            book,
            MODULE.SiteResult("error", "low", "fallback", "temporary failure"),
        )
        cache.put("hardcover", book, MODULE.SiteResult("high", "high", "exact"))

        self.assertIsNone(cache.get("goodreads", book))
        self.assertIsNone(cache.get("storygraph", book))
        self.assertEqual(cache.get("hardcover", book).url, "high")

    def test_refresh_preserves_unrelated_cached_results(self):
        first = MODULE.Book("First", "Author", None, None)
        second = MODULE.Book("Second", "Author", None, None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = MODULE.JsonCache(path, enabled=True)
            cache.put("goodreads", first, MODULE.SiteResult("first", "high", "exact"))
            cache.save()

            refreshed = MODULE.JsonCache(path, enabled=True, refresh=True)
            refreshed.put(
                "goodreads", second, MODULE.SiteResult("second", "high", "exact")
            )
            refreshed.save()
            loaded = MODULE.JsonCache(path, enabled=True)

        self.assertEqual(loaded.get("goodreads", first).url, "first")
        self.assertEqual(loaded.get("goodreads", second).url, "second")

    def test_cache_is_saved_after_each_book(self):
        books = [
            MODULE.Book("One", "Author", None, None),
            MODULE.Book("Two", "Author", None, None),
        ]
        config = MODULE.LookupConfig(
            sites=["goodreads"],
            sleep_seconds=0,
            timeout_seconds=1,
            goodreads_pages=1,
            use_firecrawl_search=False,
            quiet=True,
        )
        cache = mock.Mock()
        cache.get.return_value = MODULE.SiteResult("cached", "high", "exact")

        MODULE.lookup_books(books, mock.Mock(), cache, config)

        self.assertEqual(cache.save.call_count, 2)

    def test_unicode_metadata_does_not_match_arbitrary_text(self):
        book = MODULE.Book("東京", "村上", None, None)

        self.assertEqual(MODULE.normalize_text(book.title), "東京")
        self.assertEqual(MODULE.text_match_score("unrelated result", book), 0)

    def test_amazon_uses_exact_asin_when_available(self):
        result = MODULE.lookup_amazon(MODULE.Book("Title", "Author", None, "b012345678"))

        self.assertEqual(result.url, "https://www.amazon.com/dp/B012345678")
        self.assertEqual(result.confidence, "high")

    def test_amazon_falls_back_to_search_for_invalid_asin(self):
        result = MODULE.lookup_amazon(MODULE.Book("Title", "Author", None, "not/as-in"))

        self.assertEqual(result.confidence, "search")

    def test_snapshot_includes_audiobook_metadata(self):
        book = MODULE.Book(
            "Title",
            "Author",
            None,
            "B012345678",
            narrator="Narrator",
            publisher="Publisher",
            duration_seconds=3600,
        )
        output = io.StringIO()

        with redirect_stdout(output):
            MODULE.print_snapshot([book])

        record = json.loads(output.getvalue())[0]
        self.assertEqual(record["narrator"], "Narrator")
        self.assertEqual(record["publisher"], "Publisher")
        self.assertEqual(record["duration_seconds"], 3600)

    def test_abs_metadata_populates_audiobook_snapshot_fields(self):
        book = MODULE.book_from_abs_item(
            {
                "id": "book-id",
                "media": {
                    "duration": 3600.9,
                    "coverPath": "/metadata/cover.jpg",
                    "metadata": {
                        "title": "Title",
                        "authorName": "Author",
                        "narratorName": "Narrator",
                        "publisher": "Publisher",
                        "publishedYear": "2026",
                        "language": "English",
                        "seriesName": "Series #1",
                    },
                },
            }
        )

        self.assertEqual(book.narrator, "Narrator")
        self.assertEqual(book.duration_seconds, 3600)
        self.assertEqual(book.cover_path, "/metadata/cover.jpg")

    def test_hardcover_listened_reading_format_is_audio(self):
        self.assertTrue(
            MODULE.hardcover_is_audio(
                {"reading_format": {"id": 2, "format": "Listened"}}
            )
        )

    def test_hardcover_plan_uses_an_exact_audiobook_edition(self):
        book = MODULE.Book("Title", "Author", None, "B012345678")
        edition = {
            "id": 12,
            "asin": "B012345678",
            "reading_format": {"id": 2, "format": "Listened"},
            "image": {"id": 3},
            "book": {"id": 34, "slug": "title", "title": "Title"},
        }
        clients = mock.Mock()
        clients.hardcover_query.return_value = {
            "editions": [edition],
            "search": {"results": {"hits": []}},
        }

        plan = MODULE.hardcover_plan_book(book, clients)

        self.assertEqual(plan.action, "use_existing_audiobook")
        self.assertEqual(plan.edition_id, 12)
        self.assertFalse(plan.cover_needed)
        self.assertEqual(clients.hardcover_query.call_count, 1)

    def test_hardcover_requests_are_paced_at_thirty_per_minute(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data": {}}'
        with (
            mock.patch.dict(os.environ, {"HARDCOVER_API_KEY": "token"}),
            mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response),
            mock.patch.object(MODULE.time, "monotonic", side_effect=[0.0, 0.0, 2.0]),
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            clients = MODULE.ApiClients(10)
            clients.hardcover_query("query { me { id } }", {})
            clients.hardcover_query("query { me { id } }", {})

        sleep.assert_called_once_with(2.0)

    def test_hardcover_sync_repairs_the_library_and_read_editions(self):
        book = MODULE.Book(
            "A Practical Guide to Evil I",
            "David Verburg",
            None,
            "B0FG4LG58Q",
            started_at="2026-05-18",
            finished_at="2026-05-20",
        )
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {
                "me": [
                    {
                        "user_books": [
                            {
                                "id": 10,
                                "edition_id": 99,
                                "status_id": 3,
                                "user_book_reads": [
                                    {
                                        "id": 20,
                                        "started_at": "2026-05-18",
                                        "finished_at": "2026-05-20",
                                        "edition_id": 99,
                                    }
                                ],
                            }
                        ]
                    }
                ]
            },
            {"update_user_book": {"id": 10, "error": None}},
            {"update_user_book_read": {"id": 20, "error": None}},
        ]

        url = MODULE.hardcover_sync_reading(book, 2072287, 32217542, clients)

        self.assertEqual(
            url,
            "https://hardcover.app/books/a-practical-guide-to-evil-i/editions/32217542",
        )
        self.assertEqual(clients.hardcover_query.call_count, 3)
        self.assertEqual(
            clients.hardcover_query.call_args_list[1].args[1]["object"]["edition_id"],
            32217542,
        )
        self.assertEqual(
            clients.hardcover_query.call_args_list[2].args[1]["object"],
            {
                "started_at": "2026-05-18",
                "finished_at": "2026-05-20",
                "edition_id": 32217542,
            },
        )

    def test_hardcover_sync_creates_an_exact_edition_reading_record(self):
        book = MODULE.Book(
            "Calla Falling",
            "Tallie Rose",
            None,
            "B0CBCXQG2T",
            started_at="2026-05-20",
            finished_at="2026-05-21",
        )
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {"me": [{"user_books": []}]},
            {
                "insert_user_book": {
                    "id": 10,
                    "error": None,
                    "user_book": {
                        "id": 10,
                        "edition_id": 40000000,
                        "status_id": 3,
                        "first_started_reading_date": "2026-05-20",
                        "last_read_date": "2026-05-21",
                        "read_count": 1,
                        "user_book_reads": [
                            {
                                "id": 30,
                                "started_at": None,
                                "finished_at": "2026-08-30",
                                "edition_id": 40000000,
                            }
                        ],
                    },
                }
            },
            {"update_user_book_read": {"id": 30, "error": None}},
        ]

        MODULE.hardcover_sync_reading(book, 1190863, 40000000, clients)

        self.assertEqual(clients.hardcover_query.call_count, 3)
        self.assertEqual(
            clients.hardcover_query.call_args_list[1].args[1]["object"],
            {
                "book_id": 1190863,
                "edition_id": 40000000,
                "status_id": 3,
                "date_added": "2026-05-21",
                "first_started_reading_date": "2026-05-20",
                "last_read_date": "2026-05-21",
                "read_count": 1,
            },
        )
        self.assertEqual(
            clients.hardcover_query.call_args_list[2].args[1],
            {
                "id": 30,
                "object": {
                    "started_at": "2026-05-20",
                    "finished_at": "2026-05-21",
                    "edition_id": 40000000,
                },
            },
        )

    def test_hardcover_sync_reuses_an_automatic_read_after_status_update(self):
        book = MODULE.Book(
            "The Original",
            "Nell Stevens",
            "9798228487291",
            "B0F1Z3P3XS",
            started_at="2026-05-12",
            finished_at="2026-05-27",
        )
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {
                "me": [
                    {
                        "user_books": [
                            {
                                "id": 10,
                                "edition_id": None,
                                "status_id": 1,
                                "first_started_reading_date": None,
                                "last_read_date": None,
                                "read_count": 0,
                                "user_book_reads": [],
                            }
                        ]
                    }
                ]
            },
            {
                "update_user_book": {
                    "id": 10,
                    "error": None,
                    "user_book": {
                        "id": 10,
                        "edition_id": 12,
                        "status_id": 3,
                        "first_started_reading_date": "2026-05-12",
                        "last_read_date": "2026-05-27",
                        "read_count": 1,
                        "user_book_reads": [
                            {
                                "id": 30,
                                "started_at": None,
                                "finished_at": "2026-08-30",
                                "edition_id": 12,
                            }
                        ],
                    },
                }
            },
            {"update_user_book_read": {"id": 30, "error": None}},
        ]

        MODULE.hardcover_sync_reading(book, 1775722, 12, clients)

        self.assertEqual(clients.hardcover_query.call_count, 3)
        self.assertEqual(
            clients.hardcover_query.call_args_list[2].args[1],
            {
                "id": 30,
                "object": {
                    "started_at": "2026-05-12",
                    "finished_at": "2026-05-27",
                    "edition_id": 12,
                },
            },
        )

    def test_hardcover_sync_is_a_noop_when_the_exact_read_already_exists(self):
        book = MODULE.Book(
            "Title",
            "Author",
            None,
            "B012345678",
            started_at="2026-05-20",
            finished_at="2026-05-21",
        )
        clients = mock.Mock()
        clients.hardcover_query.return_value = {
            "me": [
                {
                    "user_books": [
                        {
                            "id": 10,
                            "edition_id": 12,
                            "status_id": 3,
                            "first_started_reading_date": "2026-05-20",
                            "last_read_date": "2026-05-21",
                            "read_count": 1,
                            "user_book_reads": [
                                {
                                    "id": 20,
                                    "started_at": "2026-05-20",
                                    "finished_at": "2026-05-21",
                                    "edition_id": 12,
                                }
                            ],
                        }
                    ]
                }
            ]
        }

        MODULE.hardcover_sync_reading(book, 34, 12, clients)

        self.assertEqual(clients.hardcover_query.call_count, 1)

    def test_hardcover_catchup_goodreads_keeps_all_completed_books(self):
        current_book = MODULE.Book(
            "Second Title",
            "Second Author",
            None,
            "B000000002",
            abs_id="abs-2",
            finished_at="2026-05-22",
        )
        entries = {
            "abs-1": {
                "abs_id": "abs-1",
                "title": "First Title",
                "author": "First Author",
                "asin": "B000000001",
                "finished_at": "2026-05-21",
                "goodreads": {
                    "url": "https://www.goodreads.com/book/show/1",
                    "confidence": "medium",
                    "evidence": "matched edition",
                    "error": None,
                },
            },
            "abs-2": {
                "abs_id": "abs-2",
                "title": "Second Title",
                "author": "Second Author",
                "asin": "B000000002",
                "finished_at": "2026-05-22",
                "goodreads": {
                    "url": "https://www.goodreads.com/book/show/2",
                    "confidence": "medium",
                    "evidence": "matched edition",
                    "error": None,
                },
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            MODULE.write_hardcover_catchup_goodreads(
                directory, [current_book], entries
            )
            with (directory / "goodreads-import.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual([row["Title"] for row in rows], ["First Title", "Second Title"])

    def test_hardcover_catchup_completes_one_book_and_exports_goodreads(self):
        book = MODULE.Book(
            "Title",
            "Author",
            None,
            "B012345678",
            abs_id="abs-1",
            started_at="2026-05-20",
            finished_at="2026-05-21",
            cover_path="/library/Title/cover.jpg",
        )
        plan_records = [
            {
                "abs_id": "abs-1",
                "action": "use_existing_audiobook",
                "confidence": "high",
                "evidence": "matched audiobook",
                "hardcover_url": "https://hardcover.app/books/title/editions/12",
                "book_id": 34,
                "edition_id": 12,
            }
        ]
        clients = mock.Mock()
        clients.audiobookshelf_cover.return_value = (b"\xff\xd8cover", "image/jpeg")
        clients.hardcover_query.return_value = {
            "me": [
                {
                    "user_books": [
                        {
                            "id": 10,
                            "edition_id": 12,
                            "status_id": 3,
                            "first_started_reading_date": "2026-05-20",
                            "last_read_date": "2026-05-21",
                            "read_count": 1,
                            "user_book_reads": [
                                {
                                    "id": 20,
                                    "started_at": "2026-05-20",
                                    "finished_at": "2026-05-21",
                                    "edition_id": 12,
                                }
                            ],
                        }
                    ]
                }
            ]
        }
        config = MODULE.LookupConfig(["goodreads"], 0, 10, 2, False, True)
        outputs = []

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            cache = MODULE.JsonCache(output / "cache.json", True)
            with mock.patch.object(
                MODULE,
                "lookup_site",
                return_value=MODULE.SiteResult(
                    "https://www.goodreads.com/book/show/123", "high", "matched"
                ),
            ):
                completed = MODULE.hardcover_catchup_books(
                    output,
                    [book],
                    clients,
                    cache,
                    config,
                    plan_records,
                    input_fn=mock.Mock(side_effect=AssertionError("unexpected prompt")),
                    output_fn=outputs.append,
                )
            state = json.loads((output / "hardcover-catchup.json").read_text())
            with (output / "goodreads-import.csv").open(newline="") as handle:
                goodreads = list(csv.DictReader(handle))
            cover_bytes = (
                output / state["books"]["abs-1"]["cover_file"]
            ).read_bytes()

        self.assertEqual(completed, 1)
        self.assertEqual(state["books"]["abs-1"]["edition_id"], 12)
        self.assertTrue(state["books"]["abs-1"]["cover_original"])
        self.assertEqual(
            Path(state["books"]["abs-1"]["cover_file"]).parent,
            Path("covers"),
        )
        self.assertEqual(goodreads[0]["Book Id"], "123")
        self.assertEqual(cover_bytes, b"\xff\xd8cover")
        self.assertIn("https://hardcover.app/editions/12/edit", outputs)

    def test_hardcover_catchup_adds_incomplete_existing_editions_to_fix_list(self):
        book = MODULE.Book(
            "Dead Weight",
            "Hildur Knútsdóttir, Mary Robinette Kowal - translator",
            "9781250446978",
            "B0FML8D6TY",
            abs_id="abs-dead-weight",
            started_at="2026-05-27",
            finished_at="2026-05-28",
            narrator="Mary Robinette Kowal",
            publisher="Macmillan Audio",
            duration_seconds=11481,
        )
        plan_records = [
            {
                "abs_id": "abs-dead-weight",
                "action": "use_existing_audiobook",
                "confidence": "high",
                "evidence": "matched audiobook",
                "hardcover_url": "https://hardcover.app/books/dead-weight-2026/editions/33230051",
                "book_id": 2084634,
                "edition_id": 33230051,
            }
        ]
        clients = mock.Mock()
        clients.audible_product.return_value = {
            "asin": "B0FML8D6TY",
            "title": "Dead Weight",
            "authors": [
                {"name": "Hildur Knútsdóttir"},
                {"name": "Mary Robinette Kowal", "role": "translator"},
            ],
            "narrators": [{"name": "Mary Robinette Kowal"}],
            "publisher_name": "Macmillan Audio",
            "isbn": "9781250446978",
            "language": "english",
            "release_date": "2026-05-26",
        }
        clients.audible_chapter_info.return_value = {"runtime_length_sec": 11484}
        clients.hardcover_query.side_effect = [
            {
                "me": [
                    {
                        "user_books": [
                            {
                                "id": 10,
                                "edition_id": 33230051,
                                "status_id": 3,
                                "first_started_reading_date": "2026-05-27",
                                "last_read_date": "2026-05-28",
                                "read_count": 1,
                                "user_book_reads": [
                                    {
                                        "id": 20,
                                        "started_at": "2026-05-27",
                                        "finished_at": "2026-05-28",
                                        "edition_id": 33230051,
                                    }
                                ],
                            }
                        ]
                    }
                ]
            },
            {
                "editions": [
                    {
                        "id": 33230051,
                        "title": "Dead Weight",
                        "audio_seconds": 11474,
                        "reading_format": {"id": 2, "format": "Listened"},
                        "image": {"id": 1},
                        "contributions": [
                            {
                                "author": {"name": "Hildur Knútsdóttir"},
                                "contribution": "Author",
                            },
                            {
                                "author": {"name": "Mary Robinette Kowal"},
                                "contribution": "Translator",
                            },
                        ],
                    }
                ]
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with mock.patch.object(
                MODULE,
                "lookup_site",
                return_value=MODULE.SiteResult(None, "none", "no Goodreads match"),
            ):
                MODULE.hardcover_catchup_books(
                    output,
                    [book],
                    clients,
                    MODULE.JsonCache(output / "cache.json", True),
                    MODULE.LookupConfig(["goodreads"], 0, 10, 2, False, True),
                    plan_records,
                    input_fn=lambda _prompt: "n",
                    output_fn=lambda _message: None,
                )
            state = json.loads((output / "hardcover-catchup.json").read_text())
            fix_list = (output / "fix-list.tsv").read_text()

        self.assertEqual(
            state["books"]["abs-dead-weight"]["metadata_checked_version"], 1
        )
        self.assertIn("ASIN missing", fix_list)
        self.assertIn("narrator missing: Mary Robinette Kowal", fix_list)

    def test_hardcover_catchup_repairs_confirmed_existing_edition_metadata(self):
        book = MODULE.Book(
            "Dead Weight",
            "Hildur Knútsdóttir, Mary Robinette Kowal - translator",
            "9781250446978",
            "B0FML8D6TY",
            abs_id="abs-dead-weight",
            started_at="2026-05-27",
            finished_at="2026-05-28",
            narrator="Mary Robinette Kowal",
            publisher="Macmillan Audio",
            published_date="2026-05-26",
            language="english",
            duration_seconds=11484,
        )
        plan_records = [
            {
                "abs_id": "abs-dead-weight",
                "action": "use_existing_audiobook",
                "confidence": "high",
                "evidence": "matched audiobook",
                "hardcover_url": "https://hardcover.app/books/dead-weight-2026/editions/33230051",
                "book_id": 2084634,
                "edition_id": 33230051,
            }
        ]
        incomplete = {
            "id": 33230051,
            "title": "Dead Weight",
            "audio_seconds": 11474,
            "reading_format": {"id": 2, "format": "Listened"},
            "image": {"id": 1},
            "contributions": [
                {
                    "author": {"name": "Hildur Knútsdóttir"},
                    "contribution": "Author",
                },
                {
                    "author": {"name": "Mary Robinette Kowal"},
                    "contribution": "Translator",
                },
            ],
        }
        complete = {
            **incomplete,
            "asin": "B0FML8D6TY",
            "isbn_13": "9781250446978",
            "audio_seconds": 11484,
            "release_date": "2026-05-26",
            "language": {"id": 1, "language": "English", "code2": "en"},
            "country": {"id": 1, "name": "United States of America", "code2": "us"},
            "publisher": {"id": 242, "name": "Macmillan Audio"},
            "contributions": [
                {
                    "author": {"name": "Hildur Knútsdóttir"},
                    "contribution": "Author",
                },
                {
                    "author": {"name": "Mary Robinette Kowal"},
                    "contribution": "Translator",
                },
                {
                    "author": {"name": "Mary Robinette Kowal"},
                    "contribution": "Narrator",
                },
            ],
        }
        clients = mock.Mock()
        clients.audible_product.return_value = {
            "asin": "B0FML8D6TY",
            "title": "Dead Weight",
            "authors": [
                {"name": "Hildur Knútsdóttir"},
                {"name": "Mary Robinette Kowal", "role": "translator"},
            ],
            "narrators": [{"name": "Mary Robinette Kowal"}],
            "publisher_name": "Macmillan Audio",
            "isbn": "9781250446978",
            "language": "english",
            "release_date": "2026-05-26",
        }
        clients.audible_chapter_info.return_value = {"runtime_length_sec": 11484}
        audits = iter([incomplete, complete])
        mutation_dtos = []

        def hardcover_response(query, variables):
            if " me {" in query:
                return {
                    "me": [
                        {
                            "user_books": [
                                {
                                    "id": 10,
                                    "edition_id": 33230051,
                                    "status_id": 3,
                                    "first_started_reading_date": "2026-05-27",
                                    "last_read_date": "2026-05-28",
                                    "read_count": 1,
                                    "user_book_reads": [
                                        {
                                            "id": 20,
                                            "started_at": "2026-05-27",
                                            "finished_at": "2026-05-28",
                                            "edition_id": 33230051,
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            if "HardcoverEditionMetadata" in query:
                return {"editions": [next(audits)]}
            if "PeopleForEdition" in query:
                people = {
                    "Hildur Knútsdóttir": {
                        "id": 1,
                        "name": "Hildur Knútsdóttir",
                        "books_count": 5,
                    },
                    "Mary Robinette Kowal": {
                        "id": 2,
                        "name": "Mary Robinette Kowal",
                        "books_count": 50,
                    },
                }
                return {"authors": [people[variables["name"]]]}
            if "PublishersForEdition" in query:
                return {
                    "publishers": [
                        {
                            "id": 242,
                            "name": "Macmillan Audio",
                            "editions_count": 1000,
                        }
                    ]
                }
            if "UpdateAudiobookEdition" in query:
                mutation_dtos.append(variables["edition"]["dto"])
                return {
                    "update_edition": {
                        "id": 33230051,
                        "errors": [],
                        "warnings": [],
                    }
                }
            self.fail(f"unexpected Hardcover operation: {query}")

        clients.hardcover_query.side_effect = hardcover_response
        prompts = []

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with mock.patch.object(
                MODULE,
                "lookup_site",
                return_value=MODULE.SiteResult(None, "none", "no Goodreads match"),
            ):
                MODULE.hardcover_catchup_books(
                    output,
                    [book],
                    clients,
                    MODULE.JsonCache(output / "cache.json", True),
                    MODULE.LookupConfig(["goodreads"], 0, 10, 2, False, True),
                    plan_records,
                    input_fn=lambda prompt: prompts.append(prompt) or "y",
                    output_fn=lambda _message: None,
                )
            state = json.loads((output / "hardcover-catchup.json").read_text())
            fix_list = (output / "fix-list.tsv").read_text()

        dto = mutation_dtos[0]
        self.assertIn("Update Hardcover metadata for Dead Weight", prompts[0])
        self.assertEqual(dto["asin"], "B0FML8D6TY")
        self.assertEqual(dto["audio_seconds"], 11484)
        self.assertEqual(dto["release_date"], "2026-05-26")
        self.assertEqual(dto["publisher_id"], 242)
        self.assertEqual(len(dto["contributions"]), 3)
        self.assertEqual(state["books"]["abs-dead-weight"]["metadata_issues"], [])
        self.assertNotIn("Dead Weight", fix_list)

    def test_hardcover_update_skips_an_isbn_used_by_another_edition(self):
        book = MODULE.Book(
            "They Bloom at Night",
            "Trang Thanh Tran",
            "9781547615995",
            "B0DWXZX1D6",
            duration_seconds=27504,
            published_date="2025-03-04",
            language="English",
        )
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {
                "update_edition": {
                    "id": None,
                    "errors": ["isbn_10 '1547615990' is already in use by another edition"],
                    "warnings": [],
                }
            },
            {
                "update_edition": {
                    "id": 12,
                    "errors": [],
                    "warnings": [],
                }
            },
        ]

        deferred = MODULE.hardcover_update_audiobook_edition(
            book, 12, clients, {}
        )

        self.assertEqual(deferred, ["ISBN belongs to another Hardcover edition"])
        retry_dto = clients.hardcover_query.call_args_list[1].args[1]["edition"]["dto"]
        self.assertNotIn("isbn_10", retry_dto)
        self.assertNotIn("isbn_13", retry_dto)
        self.assertEqual(retry_dto["asin"], "B0DWXZX1D6")
        self.assertEqual(retry_dto["audio_seconds"], 27504)

    def test_hardcover_fix_reasons_drop_resolved_catalog_lookup_failures(self):
        self.assertEqual(
            MODULE.hardcover_reconciled_fix_reasons(
                ["cover missing", "author not found: Sir Bedivere the Mad"],
                ["cover missing"],
            ),
            ["cover missing"],
        )
        self.assertEqual(
            MODULE.hardcover_reconciled_fix_reasons(["cover missing"], []),
            [],
        )

    def test_hardcover_creates_a_populated_audiobook_edition(self):
        book = MODULE.Book(
            "Calla Falling",
            "Tallie Rose",
            "9781234567897",
            "B0CBCXQG2T",
            subtitle="A Novel",
            published_year="2023",
            language="English",
            duration_seconds=23524,
        )
        clients = mock.Mock()
        clients.hardcover_query.return_value = {
            "insert_edition": {
                "id": 40,
                "errors": [],
                "warnings": [],
                "edition": {"id": 40, "book": {"slug": "calla-falling"}},
            }
        }

        edition_id, url = MODULE.hardcover_create_audiobook_edition(
            book, 1190863, clients
        )

        self.assertEqual(edition_id, 40)
        self.assertEqual(
            url, "https://hardcover.app/books/calla-falling/editions/40"
        )
        self.assertEqual(
            clients.hardcover_query.call_args.args[1],
            {
                "bookId": 1190863,
                "edition": {
                    "dto": {
                        "title": "Calla Falling",
                        "subtitle": "A Novel",
                        "asin": "B0CBCXQG2T",
                        "isbn_13": "9781234567897",
                        "audio_seconds": 23524,
                        "reading_format_id": 2,
                        "edition_format": "Audible Audio",
                        "language_id": 1,
                        "country_id": 1,
                    }
                },
            },
        )

    def test_hardcover_does_not_invent_a_publish_date_from_a_year(self):
        book = MODULE.Book(
            "Calla Falling",
            "Tallie Rose",
            None,
            "B0CBCXQG2T",
            published_year="2023",
        )

        self.assertEqual(MODULE.hardcover_publish_date(book), "")
        self.assertNotIn("release_date", MODULE.hardcover_audiobook_dto(book))

    def test_audible_metadata_identifies_created_edition_updates(self):
        original = MODULE.Book(
            "The Original",
            "Nell Stevens",
            "9798228487291",
            "B0F1Z3P3XS",
            subtitle="A Novel",
            publisher="Blackstone Publishing",
            duration_seconds=36433,
        )
        audible = replace(
            original,
            published_date="2025-07-01",
            published_year="2025",
            duration_seconds=36444,
        )

        self.assertEqual(
            MODULE.hardcover_audible_update_reasons(original, audible),
            [
                "duration needs update: 36444",
                "publication date needs update: 2025-07-01",
            ],
        )

    def test_existing_hardcover_audiobook_reports_missing_audible_metadata(self):
        book = MODULE.Book(
            "Dead Weight",
            "Hildur Knútsdóttir, Mary Robinette Kowal - translator",
            "9781250446978",
            "B0FML8D6TY",
            narrator="Mary Robinette Kowal",
            publisher="Macmillan Audio",
            published_date="2026-05-26",
            language="english",
            duration_seconds=11484,
        )
        clients = mock.Mock()
        clients.hardcover_query.return_value = {
            "editions": [
                {
                    "id": 33230051,
                    "title": "Dead Weight",
                    "subtitle": None,
                    "asin": None,
                    "isbn_13": None,
                    "isbn_10": None,
                    "audio_seconds": 11474,
                    "release_date": None,
                    "reading_format": {"id": 2, "format": "Listened"},
                    "language": None,
                    "country": None,
                    "publisher": None,
                    "image": {"id": 1},
                    "contributions": [
                        {
                            "author": {"id": 1, "name": "Hildur Knútsdóttir"},
                            "contribution": "Author",
                            "contributor_role": {"id": 1, "name": "Author"},
                        },
                        {
                            "author": {"id": 2, "name": "Mary Robinette Kowal"},
                            "contribution": "Translator",
                            "contributor_role": None,
                        },
                    ],
                }
            ]
        }

        self.assertEqual(
            MODULE.hardcover_existing_edition_issues(book, 33230051, clients),
            [
                "ASIN missing",
                "ISBN missing",
                "duration needs update: 11484",
                "publication date missing",
                "language missing",
                "country missing",
                "publisher missing",
                "narrator missing: Mary Robinette Kowal",
            ],
        )

    def test_existing_hardcover_audiobook_reports_a_title_correction(self):
        book = MODULE.Book(
            "Welcome to the Blast: A Cyberpunk Adventure",
            "Plum Parrot",
            None,
            "B0G1D3NZLJ",
        )
        clients = mock.Mock()
        clients.hardcover_query.return_value = {
            "editions": [
                {
                    "id": 12,
                    "title": "Welcome to the Blast : A Cyberpunk Adventure",
                    "asin": "B0G1D3NZLJ",
                }
            ]
        }

        issues = MODULE.hardcover_existing_edition_issues(book, 12, clients)

        self.assertIn(
            "title needs update: Welcome to the Blast: A Cyberpunk Adventure",
            issues,
        )

    def test_hardcover_catchup_confirms_creation_before_syncing(self):
        book = MODULE.Book(
            "Calla Falling",
            "Tallie Rose",
            None,
            "B0CBCXQG2T",
            abs_id="abs-2",
            started_at="2026-05-20",
            finished_at="2026-05-21",
            duration_seconds=23524,
        )
        plan_records = [
            {
                "abs_id": "abs-2",
                "action": "create_audiobook_edition",
                "confidence": "high",
                "evidence": "matching book has no audiobook edition",
                "hardcover_url": "https://hardcover.app/books/calla-falling/editions",
                "book_id": 1190863,
            }
        ]
        clients = mock.Mock()
        clients.audible_product.return_value = {
            "asin": "B0CBCXQG2T",
            "title": "Calla Falling",
            "subtitle": "An Audible Edition",
            "authors": [{"name": "Tallie Rose"}],
            "isbn": "9781234567897",
            "language": "english",
            "release_date": "2023-07-07",
        }
        clients.audible_chapter_info.return_value = {"runtime_length_sec": 23528}
        clients.hardcover_query.side_effect = [
            {
                "authors": [
                    {"id": 689149, "name": "Tallie Rose", "books_count": 4}
                ]
            },
            {
                "insert_edition": {
                    "id": 40,
                    "errors": [],
                    "warnings": [],
                    "edition": {"id": 40, "book": {"slug": "calla-falling"}},
                }
            },
            {"me": [{"user_books": []}]},
            {"insert_user_book": {"id": 10, "error": None}},
            {"insert_user_book_read": {"id": 20, "error": None}},
        ]
        config = MODULE.LookupConfig(["goodreads"], 0, 10, 2, False, True)
        prompts = []

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            cache = MODULE.JsonCache(output / "cache.json", True)

            def confirm(prompt):
                prompts.append(prompt)
                return "y"

            with mock.patch.object(
                MODULE,
                "lookup_site",
                return_value=MODULE.SiteResult(None, "none", "no Goodreads match"),
            ):
                completed = MODULE.hardcover_catchup_books(
                    output,
                    [book],
                    clients,
                    cache,
                    config,
                    plan_records,
                    input_fn=confirm,
                    output_fn=lambda _message: None,
                )
            state = json.loads((output / "hardcover-catchup.json").read_text())
            fix_list = (output / "fix-list.tsv").read_text()

        self.assertEqual(completed, 1)
        self.assertIn("Create audiobook edition for Calla Falling", prompts[0])
        self.assertEqual(state["books"]["abs-2"]["edition_id"], 40)
        creation = clients.hardcover_query.call_args_list[1].args[1]["edition"]["dto"]
        self.assertEqual(creation["subtitle"], "An Audible Edition")
        self.assertEqual(creation["isbn_13"], "9781234567897")
        self.assertEqual(creation["release_date"], "2023-07-07")
        self.assertEqual(creation["audio_seconds"], 23528)
        self.assertEqual(state["audible"]["B0CBCXQG2T"]["published_date"], "2023-07-07")
        self.assertIn("Calla Falling", fix_list)
        self.assertIn("cover missing", fix_list)

    def test_hardcover_catchup_reuses_the_most_used_catalog_metadata(self):
        book = MODULE.Book(
            "Catalog Book",
            "Existing Author",
            None,
            "B012345678",
            abs_id="abs-catalog",
            narrator="Existing Narrator",
            publisher="Existing Publisher",
            started_at="2026-05-20",
            finished_at="2026-05-21",
        )
        plans = [
            {
                "abs_id": "abs-catalog",
                "action": "create_audiobook_edition",
                "confidence": "high",
                "evidence": "missing audiobook",
                "book_id": 34,
                "hardcover_url": "https://hardcover.app/books/catalog-book/editions",
            }
        ]
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {
                "authors": [
                    {"id": 1, "name": "Existing Author", "books_count": 2},
                    {"id": 2, "name": "Existing Author", "books_count": 20},
                ]
            },
            {
                "authors": [
                    {"id": 3, "name": "Existing Narrator", "books_count": 1},
                    {"id": 4, "name": "Existing Narrator", "books_count": 8},
                ]
            },
            {
                "publishers": [
                    {"id": 5, "name": "Existing Publisher", "editions_count": 2},
                    {"id": 6, "name": "Existing Publisher", "editions_count": 80},
                ]
            },
            {
                "insert_edition": {
                    "id": 40,
                    "errors": [],
                    "warnings": [],
                    "edition": {"id": 40, "book": {"slug": "catalog-book"}},
                }
            },
            {"me": [{"user_books": []}]},
            {"insert_user_book": {"id": 10, "error": None}},
            {"insert_user_book_read": {"id": 20, "error": None}},
        ]

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with mock.patch.object(
                MODULE,
                "lookup_site",
                return_value=MODULE.SiteResult(None, "none", "no Goodreads match"),
            ):
                MODULE.hardcover_catchup_books(
                    output,
                    [book],
                    clients,
                    MODULE.JsonCache(output / "cache.json", True),
                    MODULE.LookupConfig(["goodreads"], 0, 10, 2, False, True),
                    plans,
                    input_fn=lambda _prompt: "y",
                    output_fn=lambda _message: None,
                )
            state = json.loads((output / "hardcover-catchup.json").read_text())

        creation = clients.hardcover_query.call_args_list[3].args[1]["edition"]["dto"]
        self.assertEqual(
            creation["contributions"],
            [
                {
                    "author_id": 2,
                    "contribution": "Author",
                    "contributor_role_id": 1,
                },
                {"author_id": 4, "contribution": "Narrator"},
            ],
        )
        self.assertEqual(creation["publisher_id"], 6)
        self.assertEqual(creation["country_id"], 1)
        self.assertEqual(state["catalog_choices"]["person:existing author"]["id"], 2)
        self.assertEqual(state["catalog_choices"]["publisher:existing publisher"]["id"], 6)

    def test_hardcover_prompts_before_using_a_fuzzy_person_match(self):
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {"authors": []},
            {
                "search": {
                    "results": {
                        "hits": [
                            {
                                "document": {
                                    "id": 1049992,
                                    "name": "Rebecca M. Avery",
                                    "slug": "rebecca-m-avery",
                                    "books_count": 1,
                                }
                            }
                        ]
                    }
                }
            },
        ]
        choices = {}
        prompts = []

        person_id, stop = MODULE.hardcover_resolve_person(
            "Rebecca Avery",
            clients,
            choices,
            input_fn=lambda prompt: prompts.append(prompt) or "1",
            output_fn=lambda _message: None,
        )

        self.assertFalse(stop)
        self.assertEqual(person_id, 1049992)
        self.assertIn("Choose 1-1", prompts[0])
        self.assertEqual(choices["person:rebecca avery"]["id"], 1049992)

    def test_hardcover_accepts_a_case_insensitive_exact_person_match(self):
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {"authors": []},
            {
                "search": {
                    "results": {
                        "hits": [
                            {
                                "document": {
                                    "id": 1155155,
                                    "name": "Sir Bedivere The Mad",
                                    "slug": "sir-bedivere-the-mad",
                                    "books_count": 4,
                                }
                            }
                        ]
                    }
                }
            },
        ]
        choices = {}

        person_id, stop = MODULE.hardcover_resolve_person(
            "Sir Bedivere the Mad",
            clients,
            choices,
            input_fn=mock.Mock(side_effect=AssertionError("unexpected prompt")),
            output_fn=lambda _message: None,
        )

        self.assertFalse(stop)
        self.assertEqual(person_id, 1155155)
        self.assertEqual(choices["person:sir bedivere the mad"]["uses"], 4)

    def test_hardcover_prefers_the_person_with_more_contributions(self):
        clients = mock.Mock()
        clients.hardcover_query.return_value = {
            "authors": [
                {
                    "id": 660117,
                    "name": "Quinn Riley",
                    "slug": "quinn-riley",
                    "books_count": 0,
                    "contributions_aggregate": {"aggregate": {"count": 0}},
                },
                {
                    "id": 1376094,
                    "name": "Quinn Riley",
                    "slug": "quinn-riley-narrator",
                    "books_count": 0,
                    "contributions_aggregate": {"aggregate": {"count": 14}},
                },
            ]
        }
        choices = {}

        person_id, stop = MODULE.hardcover_resolve_person(
            "Quinn Riley",
            clients,
            choices,
            input_fn=mock.Mock(side_effect=AssertionError("unexpected prompt")),
            output_fn=lambda _message: None,
        )

        self.assertFalse(stop)
        self.assertEqual(person_id, 1376094)
        self.assertEqual(choices["person:quinn riley"]["uses"], 14)

    def test_hardcover_catchup_uses_the_selected_audiobook_match(self):
        book = MODULE.Book(
            "The Devils",
            "Joe Abercrombie",
            None,
            "B0CXY69BBB",
            abs_id="abs-3",
            started_at="2026-08-09",
            finished_at="2026-08-15",
        )
        alternatives = [
            "https://hardcover.app/books/the-devils-2025/editions/30",
            "https://hardcover.app/books/the-devils-2025/editions/31",
        ]
        plan_records = [
            {
                "abs_id": "abs-3",
                "action": "review_audiobook_editions",
                "confidence": "low",
                "evidence": "multiple audiobook editions",
                "book_id": 44,
                "alternatives": alternatives,
            }
        ]
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {"me": [{"user_books": []}]},
            {"insert_user_book": {"id": 10, "error": None}},
            {"insert_user_book_read": {"id": 20, "error": None}},
            {
                "editions": [
                    {
                        "id": 31,
                        "title": "The Devils",
                        "asin": "B0CXY69BBB",
                        "image": {"id": 1},
                        "contributions": [
                            {
                                "author": {"name": "Joe Abercrombie"},
                                "contribution": "Author",
                            }
                        ],
                    }
                ]
            },
        ]
        config = MODULE.LookupConfig(["goodreads"], 0, 10, 2, False, True)
        output_lines = []

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            cache = MODULE.JsonCache(output / "cache.json", True)
            with mock.patch.object(
                MODULE,
                "lookup_site",
                return_value=MODULE.SiteResult(None, "none", "no Goodreads match"),
            ):
                completed = MODULE.hardcover_catchup_books(
                    output,
                    [book],
                    clients,
                    cache,
                    config,
                    plan_records,
                    input_fn=lambda _prompt: "2",
                    output_fn=output_lines.append,
                )
            state = json.loads((output / "hardcover-catchup.json").read_text())

        self.assertEqual(completed, 1)
        self.assertEqual(state["books"]["abs-3"]["edition_id"], 31)
        self.assertIn(alternatives[1], output_lines)

    def test_hardcover_catchup_checkpoints_a_created_edition_before_sync(self):
        book = MODULE.Book(
            "Calla Falling",
            "Tallie Rose",
            None,
            "B0CBCXQG2T",
            abs_id="abs-created",
            started_at="2026-05-20",
            finished_at="2026-05-21",
        )
        plans = [
            {
                "abs_id": "abs-created",
                "action": "create_audiobook_edition",
                "confidence": "high",
                "evidence": "missing audiobook",
                "book_id": 1190863,
                "hardcover_url": "https://hardcover.app/books/calla-falling/editions",
                "cover_needed": True,
            }
        ]
        first_clients = mock.Mock()
        first_clients.hardcover_query.side_effect = [
            {
                "authors": [
                    {"id": 689149, "name": "Tallie Rose", "books_count": 4}
                ]
            },
            {
                "insert_edition": {
                    "id": 40,
                    "errors": [],
                    "warnings": [],
                    "edition": {"id": 40, "book": {"slug": "calla-falling"}},
                }
            },
            MODULE.LookupErrorWithDetail("sync failed"),
        ]
        config = MODULE.LookupConfig(["goodreads"], 0, 10, 2, False, True)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(MODULE.LookupErrorWithDetail, "sync failed"):
                MODULE.hardcover_catchup_books(
                    output,
                    [book],
                    first_clients,
                    MODULE.JsonCache(output / "cache.json", True),
                    config,
                    plans,
                    input_fn=lambda _prompt: "y",
                    output_fn=lambda _message: None,
                )
            checkpoint = json.loads(
                (output / "hardcover-catchup.json").read_text()
            )

            second_clients = mock.Mock()
            second_clients.hardcover_query.side_effect = [
                {"me": [{"user_books": []}]},
                {"insert_user_book": {"id": 10, "error": None}},
                {"insert_user_book_read": {"id": 20, "error": None}},
                {
                    "editions": [
                        {
                            "id": 40,
                            "title": "Calla Falling",
                            "asin": "B0CBCXQG2T",
                            "reading_format": {"id": 2, "format": "Listened"},
                            "country": {
                                "id": 1,
                                "name": "United States of America",
                                "code2": "us",
                            },
                            "contributions": [
                                {
                                    "author": {"name": "Tallie Rose"},
                                    "contribution": "Author",
                                }
                            ],
                        }
                    ]
                },
            ]
            with mock.patch.object(
                MODULE,
                "lookup_site",
                return_value=MODULE.SiteResult(None, "none", "no Goodreads match"),
            ):
                completed = MODULE.hardcover_catchup_books(
                    output,
                    [book],
                    second_clients,
                    MODULE.JsonCache(output / "cache.json", True),
                    config,
                    plans,
                    input_fn=lambda _prompt: self.fail("should not confirm twice"),
                    output_fn=lambda _message: None,
                )

        self.assertEqual(checkpoint["created"]["abs-created"]["edition_id"], 40)
        self.assertEqual(completed, 1)
        self.assertNotIn(
            "insert_edition",
            " ".join(call.args[0] for call in second_clients.hardcover_query.call_args_list),
        )

    def test_hardcover_creates_a_missing_book_with_its_audiobook(self):
        book = MODULE.Book(
            "Goddess Alchemy",
            "Rain Harlow",
            None,
            "B0GD8VLV5P",
            duration_seconds=50000,
            language="English",
            published_year="2025",
        )
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {"authors": [{"id": 651781, "name": "Rain Harlow"}]},
            {
                "insert_book": {
                    "id": 41,
                    "errors": [],
                    "edition": {
                        "id": 41,
                        "book": {"id": 55, "slug": "goddess-alchemy"},
                    },
                }
            },
        ]

        book_id, edition_id, url = MODULE.hardcover_create_book_and_audiobook(
            book, clients
        )

        self.assertEqual((book_id, edition_id), (55, 41))
        self.assertEqual(
            url, "https://hardcover.app/books/goddess-alchemy/editions/41"
        )
        dto = clients.hardcover_query.call_args_list[1].args[1]["edition"]["dto"]
        self.assertEqual(dto["contributions"], [{"author_id": 651781}])

    def test_hardcover_catchup_keeps_unresolved_metadata_in_a_fix_list(self):
        book = MODULE.Book(
            "The Memory Illusion",
            "Julia Shaw",
            None,
            "B01E9E62P4",
            abs_id="abs-4",
            duration_seconds=29246,
        )
        plan_records = [
            {
                "abs_id": "abs-4",
                "action": "review_existing_audiobook",
                "confidence": "low",
                "evidence": "sole audiobook edition duration missing",
                "hardcover_url": "https://hardcover.app/books/the-memory-illusion/editions/31435320",
                "book_id": 1,
                "edition_id": 31435320,
            }
        ]
        config = MODULE.LookupConfig(["goodreads"], 0, 10, 2, False, True)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            completed = MODULE.hardcover_catchup_books(
                output,
                [book],
                mock.Mock(),
                MODULE.JsonCache(output / "cache.json", True),
                config,
                plan_records,
                output_fn=lambda _message: None,
            )
            fix_list = (output / "fix-list.tsv").read_text()

        self.assertEqual(completed, 0)
        self.assertIn("The Memory Illusion", fix_list)
        self.assertIn("duration missing", fix_list)

    def test_hardcover_catchup_fix_list_includes_the_needed_values(self):
        unresolved = {
            "abs-2": {
                "title": "Calla Falling",
                "author": "Tallie Rose",
                "narrator": "Rebecca Avery",
                "publisher": "Tallie Rose",
                "language": "English",
                "duration_seconds": 23524,
                "published_year": "2023",
                "asin": "B0CBCXQG2T",
                "isbn": None,
                "action": "fix_hardcover_metadata",
                "evidence": "author needs review; narrator needs review",
                "edit_url": "https://hardcover.app/editions/33239225/edit",
                "cover_file": "covers/calla-falling.jpg",
            }
        }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            MODULE.write_hardcover_catchup_fix_list(output, unresolved)
            with (output / "fix-list.tsv").open(newline="") as handle:
                row = next(csv.DictReader(handle, delimiter="\t"))

        self.assertEqual(row["needed_author"], "Tallie Rose")
        self.assertEqual(row["needed_narrator"], "Rebecca Avery")
        self.assertEqual(row["needed_publisher"], "Tallie Rose")
        self.assertEqual(row["needed_country"], "United States of America (us)")
        self.assertEqual(row["needed_language"], "English")
        self.assertEqual(row["needed_duration_seconds"], "23524")
        self.assertEqual(row["needed_publish_date"], "")
        self.assertEqual(row["known_publish_year"], "2023")
        self.assertEqual(row["needed_asin"], "B0CBCXQG2T")

    def test_hardcover_catchup_uses_the_selected_book_match(self):
        book = MODULE.Book(
            "The Water Outlaws",
            "S. L. Huang",
            None,
            "B0BSB2DVDQ",
            abs_id="abs-5",
            started_at="2026-07-31",
            finished_at="2026-08-05",
            duration_seconds=71037,
            language="English",
        )
        plan_records = [
            {
                "abs_id": "abs-5",
                "action": "review_book_matches",
                "confidence": "low",
                "evidence": "multiple Hardcover books have the same match score",
                "alternatives": [
                    "https://hardcover.app/books/the-water-outlaws",
                    "https://hardcover.app/books/river-judge",
                ],
            }
        ]
        edition = {
            "id": 31,
            "audio_seconds": 71000,
            "reading_format": {"id": 2, "format": "Listened"},
            "language": {"language": "English"},
            "image": {"id": 3},
            "book": {"id": 44, "slug": "the-water-outlaws", "title": "The Water Outlaws"},
        }
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {"books": [{"id": 44, "slug": "the-water-outlaws"}]},
            {"editions": [edition]},
            {"me": [{"user_books": []}]},
            {"insert_user_book": {"id": 10, "error": None}},
            {"insert_user_book_read": {"id": 20, "error": None}},
            {"editions": [edition]},
        ]
        config = MODULE.LookupConfig(["goodreads"], 0, 10, 2, False, True)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with mock.patch.object(
                MODULE,
                "lookup_site",
                return_value=MODULE.SiteResult(None, "none", "no Goodreads match"),
            ):
                completed = MODULE.hardcover_catchup_books(
                    output,
                    [book],
                    clients,
                    MODULE.JsonCache(output / "cache.json", True),
                    config,
                    plan_records,
                    input_fn=lambda _prompt: "1",
                    output_fn=lambda _message: None,
                )
            state = json.loads((output / "hardcover-catchup.json").read_text())

        self.assertEqual(completed, 1)
        self.assertEqual(state["books"]["abs-5"]["book_id"], 44)
        self.assertEqual(state["books"]["abs-5"]["edition_id"], 31)

    def test_hardcover_plan_proposes_an_edition_for_an_existing_book(self):
        book = MODULE.Book("Title", "Author", None, "B012345678")
        candidate = {
            "id": "34",
            "slug": "title",
            "title": "Title",
            "author_names": ["Author"],
        }
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {"editions": [], "search": {"results": {"hits": [{"document": candidate}]}}},
            {"editions": []},
        ]

        plan = MODULE.hardcover_plan_book(book, clients)

        self.assertEqual(plan.action, "create_audiobook_edition")
        self.assertEqual(plan.book_id, 34)
        self.assertTrue(plan.cover_needed)

    def test_hardcover_plan_accepts_a_single_audiobook_with_close_duration(self):
        book = MODULE.Book(
            "Title",
            "Author",
            None,
            "B012345678",
            language="English",
            duration_seconds=36000,
        )
        candidate = {
            "id": "34",
            "slug": "title",
            "title": "Title",
            "author_names": ["Author"],
        }
        edition = {
            "id": 12,
            "audio_seconds": 36240,
            "reading_format": {"id": 2, "format": "Listened"},
            "language": {"language": "English"},
            "image": {"id": 3},
            "book": {"id": 34, "slug": "title", "title": "Title"},
        }
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {"editions": [], "search": {"results": {"hits": [{"document": candidate}]}}},
            {"editions": [edition]},
        ]

        plan = MODULE.hardcover_plan_book(book, clients)

        self.assertEqual(plan.action, "use_existing_audiobook")
        self.assertEqual(plan.hardcover_duration_seconds, 36240)
        self.assertEqual(plan.hardcover_language, "English")

    def test_hardcover_plan_flags_a_single_audiobook_missing_duration(self):
        book = MODULE.Book("Title", "Author", None, None, duration_seconds=36000)
        candidate = {
            "id": "34",
            "slug": "title",
            "title": "Title",
            "author_names": ["Author"],
        }
        edition = {
            "id": 12,
            "audio_seconds": None,
            "reading_format": {"id": 2, "format": "Listened"},
            "image": {"id": 3},
            "book": {"id": 34, "slug": "title", "title": "Title"},
        }
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {"editions": [], "search": {"results": {"hits": [{"document": candidate}]}}},
            {"editions": [edition]},
        ]

        plan = MODULE.hardcover_plan_book(book, clients)

        self.assertEqual(plan.action, "review_existing_audiobook")
        self.assertIn("duration missing", MODULE.hardcover_fix_reasons(book, plan))

    def test_hardcover_plan_proposes_a_book_when_search_misses(self):
        book = MODULE.Book("Missing", "Author", None, "B012345678")
        clients = mock.Mock()
        clients.hardcover_query.return_value = {
            "editions": [],
            "search": {"results": {"hits": []}},
        }

        plan = MODULE.hardcover_plan_book(book, clients)

        self.assertEqual(plan.action, "create_book_and_audiobook_edition")
        self.assertTrue(plan.cover_needed)

    def test_hardcover_plan_ignores_query_text_in_unrelated_search_fields(self):
        book = MODULE.Book("The Water Outlaws", "S. L. Huang", None, "B0BSB2DVDQ")
        clients = mock.Mock()
        clients.hardcover_query.side_effect = [
            {
                "editions": [],
                "search": {
                    "results": {
                        "hits": [
                            {
                                "document": {
                                    "id": "2",
                                    "slug": "river-judge",
                                    "title": "River Judge",
                                    "author_names": ["S. L. Huang"],
                                    "highlight": "The Water Outlaws",
                                }
                            },
                            {
                                "document": {
                                    "id": "1",
                                    "slug": "the-water-outlaws",
                                    "title": "The Water Outlaws",
                                    "author_names": ["S. L. Huang"],
                                }
                            },
                        ]
                    }
                },
            },
            {"editions": []},
        ]

        plan = MODULE.hardcover_plan_book(book, clients)

        self.assertEqual(plan.action, "create_audiobook_edition")
        self.assertEqual(plan.book_id, 1)

    def test_hardcover_plan_prefers_an_exact_title_over_a_sneak_peek(self):
        book = MODULE.Book("The Devils", "Joe Abercrombie", None, "B0CXY69BBB")
        results = {
            "hits": [
                {
                    "document": {
                        "id": "2",
                        "slug": "sneak-peek-for-the-devils",
                        "title": "Sneak Peek for The Devils",
                        "author_names": ["Joe Abercrombie"],
                    }
                },
                {
                    "document": {
                        "id": "1",
                        "slug": "the-devils-2025",
                        "title": "The Devils",
                        "author_names": ["Joe Abercrombie"],
                    }
                },
            ]
        }

        candidates = MODULE.hardcover_search_candidates(results, book)

        self.assertEqual([candidate["id"] for _, candidate in candidates], ["1"])

    def test_hardcover_search_uses_publication_year_to_break_an_exact_title_tie(self):
        book = MODULE.Book(
            "Heroics 101",
            "Aest Belequa",
            None,
            "B0D3264W7N",
            published_year="2024",
        )
        results = {
            "hits": [
                {
                    "document": {
                        "id": "1",
                        "slug": "heroics-101-a-superhero-slice-of-life-litrpg",
                        "title": "Heroics 101",
                        "author_names": ["Aest Belequa"],
                        "release_year": 2023,
                    }
                },
                {
                    "document": {
                        "id": "2",
                        "slug": "heroics-101-2024",
                        "title": "Heroics 101",
                        "author_names": ["Aest Belequa"],
                        "release_year": 2024,
                    }
                },
            ]
        }

        candidates = MODULE.hardcover_search_candidates(results, book)

        self.assertEqual(candidates[0][1]["id"], "2")
        self.assertGreater(candidates[0][0], candidates[1][0])

    def test_hardcover_plan_artifacts_include_cover_follow_up(self):
        book = MODULE.Book(
            "Missing",
            "Author",
            None,
            "B012345678",
            cover_path="/metadata/cover.jpg",
        )
        plan = MODULE.HardcoverPlan(
            "create_book_and_audiobook_edition",
            "high",
            "no matching Hardcover book",
            cover_needed=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            MODULE.write_hardcover_plan_artifacts(output, [book], [plan])
            rows = json.loads((output / "hardcover-plan.json").read_text())
            cover_todo = (output / "cover-todo.tsv").read_text()
            fix_list = (output / "fix-list.tsv").read_text()

        self.assertEqual(rows[0]["action"], "create_book_and_audiobook_edition")
        self.assertIn("https://www.amazon.com/dp/B012345678", cover_todo)
        self.assertIn("/metadata/cover.jpg", cover_todo)
        self.assertIn("book not identified", fix_list)
        self.assertIn("cover missing", fix_list)

    def test_goodreads_export_writes_a_compatible_read_row(self):
        book = MODULE.Book(
            "Title",
            "Author",
            "9780306406157",
            "B012345678",
            finished_at="2026-08-25",
            publisher="Publisher",
            published_year="2026",
        )
        row = MODULE.row_from_results(
            book,
            {
                "goodreads": MODULE.SiteResult(
                    "https://www.goodreads.com/book/show/12345-title",
                    "medium",
                    "matched edition",
                )
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            MODULE.write_goodreads_export_artifacts(output, [row])
            with (output / "goodreads-import.csv").open(newline="") as file:
                record = next(csv.DictReader(file))

        self.assertEqual(record["Book Id"], "12345")
        self.assertEqual(record["ISBN"], '="0306406152"')
        self.assertEqual(record["ISBN13"], '="9780306406157"')
        self.assertEqual(record["Date Read"], "2026/08/25")
        self.assertEqual(record["Exclusive Shelf"], "read")
        self.assertEqual(record["Binding"], "Audible Audio")

    def test_goodreads_export_flags_rows_without_a_direct_book_id(self):
        row = MODULE.row_from_results(
            MODULE.Book("Title", "Author", None, "B012345678"),
            {
                "goodreads": MODULE.SiteResult(
                    "https://www.goodreads.com/work/editions/12345",
                    "low",
                    "matched Goodreads work editions page",
                )
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            MODULE.write_goodreads_export_artifacts(output, [row])
            review = (output / "goodreads-review.tsv").read_text()

        self.assertIn("Title", review)
        self.assertIn("no direct Goodreads book ID", review)
        self.assertIn("https://www.goodreads.com/work/editions/12345", review)

    def test_goodreads_export_uses_generic_audiobook_without_an_asin(self):
        row = MODULE.row_from_results(
            MODULE.Book("Title", "Author", None, None),
            {"goodreads": MODULE.SiteResult(None, "none", "no match")},
        )

        self.assertEqual(MODULE.goodreads_export_row(row)["Binding"], "Audiobook")

    def test_hardcover_csv_contains_full_audiobook_and_reading_metadata(self):
        book = MODULE.Book(
            "Title",
            "Author, Translator - translator",
            "9780306406157",
            "B012345678",
            abs_id="abs-1",
            started_at="2026-08-20",
            finished_at="2026-08-25",
            narrator="Narrator One, Narrator Two",
            publisher="Publisher",
            published_year="2026",
            language="English",
            duration_seconds=3661,
            series_name="Series Name #2",
        )
        expected_headers = [
            "Title", "Author", "Series", "Status", "Privacy",
            "Hardcover Book ID", "Hardcover Edition ID", "ISBN 10", "ISBN 13",
            "ASIN", "Media", "Country Code", "Language Code", "Binding", "Pages",
            "Duration in Seconds", "Publish Date", "Publisher", "Genres", "Moods",
            "Tags", "Content Warnings", "Lists", "Date Added", "Date Started",
            "Date Finished", "Rating", "Review", "Review Contains Spoilers",
            "Sponsored Review", "Review Date", "Review URL", "Review Media URL",
            "Private Notes", "Owned", "Compilation", "Review Slate",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardcover.csv"
            MODULE.write_hardcover_csv(
                path,
                [book],
                [{"abs_id": book.abs_id, "book_id": 42, "edition_id": None}],
            )
            with path.open(newline="") as file:
                reader = csv.DictReader(file)
                record = next(reader)

        self.assertEqual(reader.fieldnames, expected_headers)
        self.assertEqual(
            record["Author"],
            "Author, Translator (Translator), Narrator One (Narrator), Narrator Two (Narrator)",
        )
        self.assertEqual(record["Series"], "Series Name (#2)")
        self.assertEqual(record["ISBN 10"], "0306406152")
        self.assertEqual(record["ISBN 13"], "9780306406157")
        self.assertEqual(record["ASIN"], "B012345678")
        self.assertEqual(record["Hardcover Book ID"], "42")
        self.assertEqual(record["Hardcover Edition ID"], "")
        self.assertEqual(record["Media"], "Audiobook")
        self.assertEqual(record["Language Code"], "en")
        self.assertEqual(record["Binding"], "Audible Audio")
        self.assertEqual(record["Duration in Seconds"], "3661")
        self.assertEqual(record["Publish Date"], "")
        self.assertEqual(record["Date Started"], "2026-08-20")
        self.assertEqual(record["Date Finished"], "2026-08-25")
        self.assertEqual(record["Status"], "Read")

    def test_hardcover_csv_prefers_a_saved_edition_id(self):
        book = MODULE.Book("Title", "Author", None, "B012345678", abs_id="abs-1")

        record = MODULE.hardcover_csv_row(
            book,
            {"abs-1": {"book_id": 42, "edition_id": 84}},
        )

        self.assertEqual(record["Hardcover Book ID"], "")
        self.assertEqual(record["Hardcover Edition ID"], "84")

    def test_tsv_includes_status_and_error_columns(self):
        row = MODULE.row_from_results(
            MODULE.Book("Title", "Author", None, None),
            {
                "goodreads": MODULE.SiteResult(
                    "https://example.com", "low", "fallback", "temporary failure"
                )
            },
        )
        output = io.StringIO()

        with redirect_stdout(output):
            MODULE.print_tsv([row], ["goodreads"])

        header = output.getvalue().splitlines()[0]
        self.assertIn("goodreads_confidence", header)
        self.assertIn("goodreads_error", header)

    def test_human_output_includes_links_and_match_details(self):
        row = MODULE.row_from_results(
            MODULE.Book("Title", "Author", None, None),
            {
                "goodreads": MODULE.SiteResult(
                    "https://example.com", "low", "fallback", "temporary failure"
                )
            },
        )
        output = io.StringIO()

        with redirect_stdout(output):
            MODULE.print_human([row], ["goodreads"])

        report = output.getvalue()
        self.assertIn("[1/1] Title — Author", report)
        self.assertIn("goodreads [low]: https://example.com (fallback)", report)
        self.assertIn("error: temporary failure", report)

    def test_review_filter_selects_low_confidence_and_errors(self):
        rows = [
            MODULE.row_from_results(
                MODULE.Book("Exact", "Author", None, None),
                {"goodreads": MODULE.SiteResult("exact", "high", "exact")},
            ),
            MODULE.row_from_results(
                MODULE.Book("Review", "Author", None, None),
                {"goodreads": MODULE.SiteResult("fallback", "low", "fallback")},
            ),
        ]

        filtered = MODULE.filter_rows(rows, {"low", "error"})

        self.assertEqual([row["title"] for row in filtered], ["Review"])


if __name__ == "__main__":
    unittest.main()
