"""Enrichment spent most of its HTTP budget on fetches that decided nothing.

Every miss was retried even when the server said 404 — a deterministic answer
that costs the same twice. Every keyword group appended eleven blind path
guesses even when the homepage linked its contact page properly. Nothing
bounded the number of pages one lead could fetch except the wall-clock
timeout, so blowing the budget produced a truncated crawl and a false
`manual_visual_review_needed`. And every fetch opened a fresh TCP+TLS
connection.

These tests pin the fetch budget itself: how many requests a lead is allowed
to make, which ones it picks, and that the connection is reused. They do not
pin wall clock — the point is that the removed work changed no verdict.
"""
from __future__ import annotations

import os
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import enrich_leads
from enrich_leads import find_candidate_pages, scrape_site

CONTACT_KEYWORDS = ["contact", "estimate", "quote", "schedule", "request"]


class _FakeResponse:
    def __init__(self, status_code: int, url: str, text: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.text = text


def _homepage(body: str) -> str:
    return f"<html><body>{body}</body></html>"


STRONG_FORM = _homepage(
    """
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
    """
)


def _record_fetches(homepage_html: str, calls: list[str]):
    """Serve the homepage and anything it links; 404 every unlinked guess.

    A page the homepage links resolves — that is what makes a site "well
    linked" — and a linked contact page carries a form, because a contact
    page that loads and offers nothing does not answer the contact group and
    the crawl correctly keeps looking.
    """

    def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
        calls.append(url)
        if url == "https://example.com":
            return {"ok": True, "status_code": 200, "final_url": url, "html": homepage_html, "error": ""}
        if f'href="{url.replace("https://example.com", "")}"' in homepage_html:
            path = url.replace("https://example.com", "").lower()
            contactish = any(term in path for term in ("contact", "estimate", "quote", "schedule", "request"))
            html = STRONG_FORM if contactish else _homepage("<p>A page.</p>")
            return {"ok": True, "status_code": 200, "final_url": url, "html": html, "error": ""}
        return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

    return fake_fetch


class PooledConnectionTest(unittest.TestCase):
    def test_one_thread_reuses_one_session(self) -> None:
        # A bare requests.get is a fresh TCP+TLS handshake per page; a session
        # keeps the connection alive across a lead's whole crawl.
        self.assertIs(enrich_leads.fetch_session(), enrich_leads.fetch_session())

    def test_each_worker_thread_gets_its_own_session(self) -> None:
        # requests.Session is not documented as thread-safe, and enrichment
        # runs a pool of them.
        sessions: list[object] = []
        lock = threading.Lock()

        def grab() -> None:
            with lock:
                sessions.append(enrich_leads.fetch_session())

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(sessions), 4)
        self.assertEqual(len({id(session) for session in sessions}), 4)

    def test_fetch_url_goes_through_the_pooled_session(self) -> None:
        session = enrich_leads.fetch_session()
        response = _FakeResponse(200, "https://example.com", "<html></html>")
        with patch.object(session, "get", return_value=response) as get:
            result = enrich_leads.fetch_url("https://example.com")

        self.assertEqual(get.call_count, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)


class RetryBudgetTest(unittest.TestCase):
    def _fetch(self, responses: list[_FakeResponse]) -> tuple[dict, int]:
        calls: list[str] = []

        def fake_get(url: str, **kwargs: object) -> _FakeResponse:
            calls.append(url)
            return responses[min(len(calls) - 1, len(responses) - 1)]

        with patch.object(enrich_leads.fetch_session(), "get", side_effect=fake_get):
            result = enrich_leads.fetch_url("https://example.com/contact")
        return result, len(calls)

    def test_a_404_is_fetched_once(self) -> None:
        # 404 is a deterministic answer. Retrying it doubles the cost of every
        # miss and adds a sleep on top.
        result, attempts = self._fetch([_FakeResponse(404, "https://example.com/contact")])

        self.assertEqual(attempts, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 404)

    def test_a_500_is_still_retried(self) -> None:
        # Server errors are the transient case the retry exists for.
        result, attempts = self._fetch([_FakeResponse(503, "https://example.com/contact")])

        self.assertEqual(attempts, 2)
        self.assertEqual(result["status_code"], 503)


class CandidatePageSelectionTest(unittest.TestCase):
    def test_a_linked_contact_page_suppresses_the_blind_guesses(self) -> None:
        pages = find_candidate_pages(
            "https://example.com",
            ["https://example.com/contact-us"],
            CONTACT_KEYWORDS,
        )

        self.assertEqual(pages, ["https://example.com/contact-us"])

    def test_blind_guesses_still_run_when_the_html_linked_nothing(self) -> None:
        # The fallback is the safety net for sites that never link their
        # contact page. Removing it entirely would lose real forms.
        pages = find_candidate_pages(
            "https://example.com",
            ["https://example.com/blog", "https://example.com/reviews"],
            CONTACT_KEYWORDS,
        )

        self.assertIn("https://example.com/contact", pages)
        self.assertIn("https://example.com/request-estimate", pages)

    def test_every_linked_match_is_kept_not_just_the_first(self) -> None:
        pages = find_candidate_pages(
            "https://example.com",
            ["https://example.com/contact", "https://example.com/free-estimate"],
            CONTACT_KEYWORDS,
        )

        self.assertEqual(
            pages,
            ["https://example.com/contact", "https://example.com/free-estimate"],
        )


class PerLeadFetchCeilingTest(unittest.TestCase):
    def test_a_lead_cannot_fetch_an_unbounded_number_of_pages(self) -> None:
        links = "".join(f'<a href="/contact-{index}">Contact {index}</a>' for index in range(30))
        calls: list[str] = []

        with patch("enrich_leads.fetch_url", side_effect=_record_fetches(_homepage(links), calls)):
            scrape_site("https://example.com")

        self.assertEqual(len(calls), 1 + enrich_leads.MAX_CANDIDATE_PAGE_FETCHES)

    def test_linked_pages_are_not_crowded_out_by_blind_guesses(self) -> None:
        # No contact link, so the contact group falls back to eleven guesses.
        # Those must not consume the ceiling ahead of pages the site links.
        body = (
            '<a href="/services">Services</a>'
            '<a href="/about">About</a>'
            '<a href="/gallery">Gallery</a>'
        )
        calls: list[str] = []

        with patch("enrich_leads.fetch_url", side_effect=_record_fetches(_homepage(body), calls)):
            scrape_site("https://example.com")

        self.assertIn("https://example.com/services", calls)
        self.assertIn("https://example.com/about", calls)
        self.assertIn("https://example.com/gallery", calls)
        self.assertIn("https://example.com/contact", calls)

    def test_a_site_that_links_nothing_still_probes_every_group(self) -> None:
        # A JS-rendered homepage can yield no internal links at all. The
        # contact group alone guesses eleven paths — more than the ceiling — so
        # a flat "contact first" ordering would spend the whole budget there
        # and never probe for a gallery or services page.
        calls: list[str] = []

        with patch("enrich_leads.fetch_url", side_effect=_record_fetches(_homepage("<p>Epoxy floors.</p>"), calls)):
            scrape_site("https://example.com")

        self.assertLessEqual(len(calls), 1 + enrich_leads.MAX_CANDIDATE_PAGE_FETCHES)
        self.assertIn("https://example.com/contact", calls)
        self.assertIn("https://example.com/services", calls)
        self.assertIn("https://example.com/about", calls)
        self.assertIn("https://example.com/gallery", calls)

    def test_the_contact_group_still_gets_most_of_the_budget(self) -> None:
        # Fair sharing must not flatten into one guess each — the contact and
        # estimate pages are what the form signals are read from.
        calls: list[str] = []

        with patch("enrich_leads.fetch_url", side_effect=_record_fetches(_homepage("<p>Epoxy floors.</p>"), calls)):
            scrape_site("https://example.com")

        contact_guesses = [url for url in calls if url.rsplit("/", 1)[-1] in {path.lstrip("/") for path in enrich_leads.CONTACT_ESTIMATE_PATHS}]
        self.assertGreaterEqual(len(contact_guesses), 4)

    def test_a_well_linked_site_stays_well_under_the_ceiling(self) -> None:
        body = (
            '<a href="/contact-us">Contact</a>'
            '<a href="/services">Services</a>'
            '<a href="/about">About</a>'
            '<a href="/gallery">Gallery</a>'
        )
        calls: list[str] = []

        with patch("enrich_leads.fetch_url", side_effect=_record_fetches(_homepage(body), calls)):
            scrape_site("https://example.com")

        self.assertEqual(
            calls,
            [
                "https://example.com",
                "https://example.com/contact-us",
                "https://example.com/services",
                "https://example.com/about",
                "https://example.com/gallery",
            ],
        )


class BrokenLinkedPageTest(unittest.TestCase):
    """A link that 404s is not a contact page, whatever the href says.

    Real case: a HubSpot template emitted `/f/contact/$hs_path` unrendered.
    It matches the contact keywords, so treating "the HTML linked something"
    as proof suppressed the guesses — and the suppressed `/contact` guess was
    the page actually serving the form. The safety net has to survive a linked
    candidate that does not resolve.
    """

    _BODY = '<a href="/f/contact/$hs_path">Contact</a>'
    _FORM = """
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

    def _fetch(self, calls: list[str]):
        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            if url == "https://example.com":
                return {"ok": True, "status_code": 200, "final_url": url, "html": _homepage(self._BODY), "error": ""}
            if url == "https://example.com/contact":
                return {"ok": True, "status_code": 200, "final_url": url, "html": self._FORM, "error": ""}
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

        return fake_fetch

    def test_the_guesses_still_run_when_the_linked_page_404s(self) -> None:
        calls: list[str] = []

        with patch("enrich_leads.fetch_url", side_effect=self._fetch(calls)):
            site = scrape_site("https://example.com")

        self.assertIn("https://example.com/f/contact/$hs_path", calls)
        self.assertIn("https://example.com/contact", calls)
        self.assertEqual(site["form_strength"], "Strong")

    def test_a_working_linked_page_still_suppresses_the_guesses(self) -> None:
        calls: list[str] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            if url == "https://example.com":
                return {
                    "ok": True,
                    "status_code": 200,
                    "final_url": url,
                    "html": _homepage('<a href="/contact-us">Contact</a>'),
                    "error": "",
                }
            if url == "https://example.com/contact-us":
                return {"ok": True, "status_code": 200, "final_url": url, "html": self._FORM, "error": ""}
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            scrape_site("https://example.com")

        self.assertIn("https://example.com/contact-us", calls)
        self.assertNotIn("https://example.com/contact", calls)


class PainIsAbsenceTest(unittest.TestCase):
    """Cutting fetches is NOT uniformly a downgrade.

    `score_leads.score_online_weakness` sells the pain, so absence *adds*
    points: `no_form_detected` is +8 when corroborated, a missing services
    page is +5 and is itself a corroborator. A higher `visible_pain_score`
    clears `sendability.VISIBLE_PAIN_BLOCKER`. So a crawl that stops early
    can make a lead *more* sendable on *less* evidence. Every skipped fetch
    has to be a fetch that could not have changed the answer.
    """

    _FORM = """
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

    def test_a_formless_contact_page_does_not_call_off_the_search(self) -> None:
        # The linked /contact-us resolves but carries no form. The form lives
        # on an unlinked /estimate. Stopping at the linked page would report
        # "no form detected" about a site that has one — and bank +8 pain for
        # it.
        calls: list[str] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            if url == "https://example.com":
                return {
                    "ok": True,
                    "status_code": 200,
                    "final_url": url,
                    "html": _homepage('<a href="/contact-us">Contact</a>'),
                    "error": "",
                }
            if url == "https://example.com/contact-us":
                return {
                    "ok": True,
                    "status_code": 200,
                    "final_url": url,
                    "html": _homepage("<p>Call us on the phone.</p>"),
                    "error": "",
                }
            if url == "https://example.com/estimate":
                return {"ok": True, "status_code": 200, "final_url": url, "html": self._FORM, "error": ""}
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            site = scrape_site("https://example.com")

        self.assertIn("https://example.com/estimate", calls)
        self.assertTrue(site["forms_found"])
        self.assertNotIn("no_form_detected", site["site_issue_flags"])

    def test_a_soft_404_contact_page_does_not_call_off_the_search(self) -> None:
        # Same hazard dressed as a 200: plenty of sites serve "page not found"
        # with a success status.
        calls: list[str] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            if url == "https://example.com":
                return {
                    "ok": True,
                    "status_code": 200,
                    "final_url": url,
                    "html": _homepage('<a href="/contact-us">Contact</a>'),
                    "error": "",
                }
            if url == "https://example.com/contact-us":
                return {
                    "ok": True,
                    "status_code": 200,
                    "final_url": url,
                    "html": _homepage("<h1>Page not found</h1>"),
                    "error": "",
                }
            if url == "https://example.com/contact":
                return {"ok": True, "status_code": 200, "final_url": url, "html": self._FORM, "error": ""}
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            site = scrape_site("https://example.com")

        self.assertTrue(site["forms_found"])

    def test_a_link_heavy_nav_cannot_starve_the_services_probe(self) -> None:
        # A nav and footer full of contact/quote links used to be harmless
        # because nothing was capped. Under a ceiling they can eat the whole
        # budget — and `weak_services` (+5, and a corroborator for two more
        # +8s) then fires about a services page that was never requested.
        nav = "".join(f'<a href="/contact-{index}">Contact {index}</a>' for index in range(12))
        body = f'{nav}<a href="/services">Services</a><a href="/gallery">Gallery</a>'
        calls: list[str] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            html = _homepage(body) if url == "https://example.com" else _homepage("<p>A page.</p>")
            return {"ok": True, "status_code": 200, "final_url": url, "html": html, "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            site = scrape_site("https://example.com")

        self.assertIn("https://example.com/services", calls)
        self.assertIn("https://example.com/gallery", calls)
        self.assertTrue((site["pages"].get("services") or {}).get("ok"))


class TruncationIsVisibleTest(unittest.TestCase):
    """A capped crawl must not read downstream as a complete one.

    The timeout path already flags itself (`per_lead_enrichment_timeout`).
    The fetch ceiling was silent, which is what let a starved crawl look
    like a site that genuinely had nothing.
    """

    def test_hitting_the_ceiling_is_recorded_on_the_lead(self) -> None:
        nav = "".join(f'<a href="/contact-{index}">Contact {index}</a>' for index in range(30))
        calls: list[str] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            html = _homepage(nav) if url == "https://example.com" else _homepage("<p>A page.</p>")
            return {"ok": True, "status_code": 200, "final_url": url, "html": html, "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            site = scrape_site("https://example.com")

        self.assertIn("candidate_page_budget_exhausted", site["site_issue_flags"])

    def test_a_crawl_that_fits_says_nothing(self) -> None:
        body = '<a href="/contact-us">Contact</a><a href="/services">Services</a>'
        calls: list[str] = []

        with patch("enrich_leads.fetch_url", side_effect=_record_fetches(_homepage(body), calls)):
            site = scrape_site("https://example.com")

        self.assertNotIn("candidate_page_budget_exhausted", site["site_issue_flags"])

    def test_the_flag_says_nothing_about_the_site_itself(self) -> None:
        # `_status_from_site` treats ANY flag outside STATIC_FETCH_REVIEW_FLAGS
        # as evidence the site is weak. This flag is a statement about our
        # crawl, not about their business — and "weak" is worth pain points
        # that push a lead toward sendable, so letting it grade would recreate
        # the bug it exists to expose.
        from lead_machine.evidence import _status_from_site

        site = {
            "site_status": "Loaded",
            "rendered_site_status": "Loaded",
            "combined_text": "Epoxy floors in Cleveland.",
            "site_issue_flags": [enrich_leads.CANDIDATE_BUDGET_FLAG],
        }

        self.assertEqual(_status_from_site(site), "okay")

    def test_the_ceiling_is_operator_tunable(self) -> None:
        # AGENTS.md: every MAX_* cap reads from the environment.
        with patch.dict(os.environ, {"MAX_CANDIDATE_PAGE_FETCHES": "3"}, clear=False):
            self.assertEqual(enrich_leads.resolve_candidate_page_budget(), 3)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                enrich_leads.resolve_candidate_page_budget(), enrich_leads.MAX_CANDIDATE_PAGE_FETCHES
            )


class InterPagePauseTest(unittest.TestCase):
    """The pause belongs in the gaps between pages, not after the last one."""

    _BODY = (
        '<a href="/contact-us">Contact</a>'
        '<a href="/services">Services</a>'
        '<a href="/about">About</a>'
        '<a href="/gallery">Gallery</a>'
    )

    def _crawl(self, ceiling: int | None = None) -> tuple[list[str], int]:
        calls: list[str] = []
        with ExitStack() as stack:
            stack.enter_context(
                patch("enrich_leads.fetch_url", side_effect=_record_fetches(_homepage(self._BODY), calls))
            )
            sleep = stack.enter_context(patch("enrich_leads.time.sleep"))
            if ceiling is not None:
                stack.enter_context(patch("enrich_leads.MAX_CANDIDATE_PAGE_FETCHES", ceiling))
            scrape_site("https://example.com")
        return calls, sleep.call_count

    def test_four_candidate_pages_are_paced_three_times(self) -> None:
        calls, pauses = self._crawl()

        self.assertEqual(len(calls), 5)  # homepage + four candidates
        self.assertEqual(pauses, 3)

    def test_the_pause_count_tracks_the_page_count(self) -> None:
        calls, pauses = self._crawl(ceiling=2)

        self.assertEqual(len(calls), 3)  # homepage + two candidates
        self.assertEqual(pauses, 1)


if __name__ == "__main__":
    unittest.main()
