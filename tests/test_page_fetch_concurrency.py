"""A lead fetched its candidate pages one socket at a time.

After the fetch-budget work a lead makes roughly four candidate-page requests,
each with an 8s ceiling and no ordering dependency on any other. Fetching them
serially means the lead's wall clock is the *sum* of four round trips when it
could be the longest one.

Concurrency threatens exactly one thing here, and it is the thing these tests
mostly pin: `_candidate_page_name` numbers the page slots `estimate`,
`estimate_2`, `estimate_3` by insertion order into `pages`, so naming by
completion order would reshuffle the output of every lead with two contactish
pages. The fetches overlap; the parsing and the naming stay serial and in
submission order.
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

import enrich_leads
from enrich_leads import scrape_site

HOME = "https://example.com"

STRONG_FORM = """
<html><body>
  <h1>Request your estimate</h1>
  <p>Complete the request form below to schedule your estimate appointment.</p>
  <form>
    <input name="first_name"><input name="last_name">
    <input type="email" name="email"><input type="tel" name="phone">
    <input name="address"><textarea name="service_details"></textarea>
    <input type="file" name="image_upload">
    <div class="recaptcha"></div>
    <button type="submit">Submit</button>
  </form>
</body></html>
"""


def _page(html: str, url: str) -> dict[str, object]:
    return {"ok": True, "status_code": 200, "final_url": url, "html": html, "error": ""}


def _missing(url: str) -> dict[str, object]:
    return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}


def _homepage(body: str) -> str:
    return f"<html><body>{body}</body></html>"


class ConcurrentPageFetchTest(unittest.TestCase):
    def test_a_leads_candidate_pages_are_in_flight_together(self) -> None:
        # Four linked contact pages, and a barrier every one of them must
        # reach before any is allowed to return. A serial crawl can never
        # trip it, so it sits out the timeout and peak stays at one.
        linked = {f"{HOME}/contact-{index}" for index in range(4)}
        body = "".join(f'<a href="/contact-{index}">Contact {index}</a>' for index in range(4))
        barrier = threading.Barrier(len(linked))
        lock = threading.Lock()
        in_flight = 0
        peak = 0

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            nonlocal in_flight, peak
            if url == HOME:
                return _page(_homepage(body), url)
            if url not in linked:
                return _missing(url)
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                pass
            with lock:
                in_flight -= 1
            return _missing(url)

        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": str(len(linked))}), patch(
            "enrich_leads.fetch_url", side_effect=fake_fetch
        ):
            scrape_site(HOME)

        self.assertEqual(peak, len(linked))

    def test_a_lone_candidate_page_is_still_crawled(self) -> None:
        calls: list[str] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            if url == HOME:
                return _page(_homepage('<a href="/request-estimate">Estimate</a>'), url)
            if url == f"{HOME}/request-estimate":
                return _page(STRONG_FORM, url)
            return _missing(url)

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            site = scrape_site(HOME)

        self.assertIn(f"{HOME}/request-estimate", calls)
        self.assertEqual(site["form_strength"], "Strong")


class SubmissionOrderTest(unittest.TestCase):
    """Slot names come from submission order, never from who answered first."""

    # `extract_links` sorts the internal links, so `/get-a-quote` is submitted
    # before `/request-estimate` however the homepage happens to order them.
    _BODY = '<a href="/request-estimate">Estimate</a><a href="/get-a-quote">Quote</a>'

    def _crawl(self) -> dict:
        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            if url == HOME:
                return _page(_homepage(self._BODY), url)
            if url == f"{HOME}/get-a-quote":
                time.sleep(0.25)  # submitted first, answers last
                return _page(_homepage("<p>Request a quote by phone.</p>"), url)
            if url == f"{HOME}/request-estimate":
                return _page(STRONG_FORM, url)
            return _missing(url)

        # Width is forced: at the shipped default of 1 both pages go out in
        # order anyway, so the test would pass without exercising anything.
        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": "5"}), patch(
            "enrich_leads.fetch_url", side_effect=fake_fetch
        ):
            return scrape_site(HOME)

    def test_the_slower_first_page_still_takes_the_first_slot(self) -> None:
        # Both paths name themselves `estimate`, so the second one to be
        # *submitted* must be `estimate_2` — even though it is the first to
        # come back.
        site = self._crawl()

        self.assertEqual(site["pages"]["estimate"]["url"], f"{HOME}/get-a-quote")
        self.assertEqual(site["pages"]["estimate_2"]["url"], f"{HOME}/request-estimate")

    def test_each_page_carries_its_own_answer(self) -> None:
        # Collecting results as they complete and pairing them back positionally
        # keeps the slot names right and files each page's HTML under the wrong
        # URL — a quieter failure than a reshuffle, and a worse one, because the
        # form it reports then belongs to a page it did not fetch.
        site = self._crawl()

        self.assertEqual(site["pages"]["estimate_2"]["form_strength"], "Strong")
        self.assertNotEqual(site["pages"]["estimate"]["form_strength"], "Strong")

    def test_the_snapshots_follow_the_same_order(self) -> None:
        # `verification_snapshots` is built from `pages.values()`, which is the
        # same insertion order, and it is what the packet and the verifier read.
        site = self._crawl()

        requested = [snapshot["requested_url"] for snapshot in site["verification_snapshots"]]
        self.assertEqual(
            requested[:3], [HOME, f"{HOME}/get-a-quote", f"{HOME}/request-estimate"]
        )


class TimeoutStillGatesSubmissionTest(unittest.TestCase):
    """The per-lead clock is checked before a wave, not before every page.

    That is the one behaviour concurrency genuinely changes: a lead that runs
    out of budget mid-wave finishes the pages already dispatched instead of
    stopping at the next one. It costs one wave of slack and it is bounded by
    the 8s candidate-page timeout.
    """

    _WIDTH = 4

    def test_the_crawl_finishes_the_wave_in_flight_and_starts_no_other(self) -> None:
        body = "".join(f'<a href="/contact-{index}">Contact {index}</a>' for index in range(8))
        fetched: list[str] = []

        # The clock runs out partway through the first wave. The whole wave was
        # admitted before any of it was fetched, so it finishes; nothing after
        # it is admitted.
        def fake_monotonic() -> float:
            return 0.0 if len(fetched) < 3 else 999.0

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            if url == HOME:
                return _page(_homepage(body), url)
            fetched.append(url)
            return _missing(url)

        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": str(self._WIDTH)}), patch(
            "enrich_leads.fetch_url", side_effect=fake_fetch
        ), patch("enrich_leads.time.monotonic", side_effect=fake_monotonic):
            site = scrape_site(HOME)

        self.assertEqual(len(fetched), self._WIDTH)
        self.assertIn("per_lead_enrichment_timeout", site["site_issue_flags"])

    def test_a_serial_crawl_stops_at_the_next_page(self) -> None:
        # At the shipped width there is no wave to finish, so the old
        # page-at-a-time timeout behaviour is exactly preserved.
        body = "".join(f'<a href="/contact-{index}">Contact {index}</a>' for index in range(8))
        fetched: list[str] = []

        def fake_monotonic() -> float:
            return 0.0 if len(fetched) < 3 else 999.0

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            if url == HOME:
                return _page(_homepage(body), url)
            fetched.append(url)
            return _missing(url)

        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": "1"}), patch(
            "enrich_leads.fetch_url", side_effect=fake_fetch
        ), patch("enrich_leads.time.monotonic", side_effect=fake_monotonic):
            site = scrape_site(HOME)

        self.assertEqual(len(fetched), 3)
        self.assertIn("per_lead_enrichment_timeout", site["site_issue_flags"])


class PageFetchWidthTest(unittest.TestCase):
    def test_the_width_is_operator_tunable(self) -> None:
        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": "2"}, clear=False):
            self.assertEqual(enrich_leads.resolve_page_fetch_workers(), 2)

    def test_a_zero_width_still_fetches(self) -> None:
        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": "0"}, clear=False):
            self.assertEqual(enrich_leads.resolve_page_fetch_workers(), 1)

    def test_the_default_is_serial_because_the_shipped_paths_are_saturated(self) -> None:
        # Not a statement that concurrency does not work -- it is worth 1.42x
        # on an otherwise idle box. It is a statement about where this runs.
        # Both shipped paths already crawl 5-6 leads at once, which overlaps
        # the network waits page concurrency would have filled, and measuring
        # them at width 5 found no gain and ~320 MB more peak PSS. The
        # docstring on resolve_page_fetch_workers carries the numbers.
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(enrich_leads.resolve_page_fetch_workers(), 1)

    def test_the_ceiling_stays_within_what_a_browser_would_open(self) -> None:
        # When an operator does turn it up, the useful range tops out where a
        # browser's own per-host connection limit does. Nothing enforces this
        # -- it is why the measured sweep stopped at 8.
        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": "6"}, clear=False):
            self.assertEqual(enrich_leads.resolve_page_fetch_workers(), 6)


class SerialWidthIsTheOldCrawlTest(unittest.TestCase):
    """`PAGE_FETCH_WORKERS=1` has to reproduce the pre-concurrency crawl.

    It is the escape hatch if a host objects to parallel requests, so it must
    fetch the same pages in the same order with the same pacing.
    """

    _BODY = (
        '<a href="/contact-us">Contact</a>'
        '<a href="/services">Services</a>'
        '<a href="/about">About</a>'
        '<a href="/gallery">Gallery</a>'
    )

    def test_one_worker_fetches_in_order_and_paces_between_every_page(self) -> None:
        calls: list[str] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            if url == HOME:
                return _page(_homepage(self._BODY), url)
            # The linked contact page answers its group, so no blind guesses
            # follow and the four linked pages are the whole crawl.
            if url == f"{HOME}/contact-us":
                return _page(STRONG_FORM, url)
            return _page(_homepage("<p>A page.</p>"), url)

        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": "1"}, clear=False), patch(
            "enrich_leads.fetch_url", side_effect=fake_fetch
        ), patch("enrich_leads.time.sleep") as sleep:
            scrape_site(HOME)

        self.assertEqual(
            calls,
            [
                HOME,
                f"{HOME}/contact-us",
                f"{HOME}/services",
                f"{HOME}/about",
                f"{HOME}/gallery",
            ],
        )
        self.assertEqual(sleep.call_count, 3)  # between the four pages, not after


if __name__ == "__main__":
    unittest.main()
