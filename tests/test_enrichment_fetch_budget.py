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
from pathlib import Path
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

        self.assertLessEqual(len(calls), 1 + enrich_leads.MAX_CANDIDATE_PAGE_FETCHES)
        self.assertLess(len(calls), 31)  # the site offered thirty

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

        # Asserted as a set: at the shipped serial default these do go out in
        # interleave order, but this test does not pin the width, and under a
        # raised `PAGE_FETCH_WORKERS` the four candidates share one wave and
        # which of them the fake records first is then a race. Order is pinned
        # where the width is pinned — `SerialWidthIsTheOldCrawlTest` for the
        # crawl, `SubmissionOrderTest` for the assembled output.
        self.assertEqual(calls[0], "https://example.com")
        self.assertEqual(
            sorted(calls[1:]),
            [
                "https://example.com/about",
                "https://example.com/contact-us",
                "https://example.com/gallery",
                "https://example.com/services",
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


CHROME = '<nav><button>Menu</button></nav><input name="q"><iframe src="https://maps.google.com/x"></iframe>'

BASIC_CONTACT_FORM = _homepage(
    CHROME
    + """
    <h1>Contact us</h1>
    <form>
      <input name="name"><input type="email" name="email">
      <textarea name="message"></textarea><button type="submit">Send</button>
    </form>
    """
)

CHROME_ONLY = _homepage(CHROME + "<h1>404 — Page Not Found</h1>")


class TerminalFormTest(unittest.TestCase):
    """Stop looking only when more looking cannot change the answer.

    `evidence._derive_visible_pain_profile` treats `strong_estimate_form`
    (estimate_form_detected Yes *and* form_strength Strong) as terminal;
    anything less still scores pain — +20 for no estimate form, +18 weak,
    +12 basic. So a contact page carrying a basic form, or only nav chrome,
    leaves the question open and the crawl has to keep going.

    `forms_found` is the wrong bar: `detect_form_signals` returns Weak (never
    None) for any page with a `<button>`, `<input>` or `<iframe>`, so a
    hamburger menu satisfies it.
    """

    def _crawl(self, contact_html: str) -> tuple[list[str], dict]:
        calls: list[str] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            if url == "https://example.com":
                return {
                    "ok": True,
                    "status_code": 200,
                    "final_url": url,
                    "html": _homepage(CHROME + '<a href="/contact">Contact</a>'),
                    "error": "",
                }
            if url == "https://example.com/contact":
                return {"ok": True, "status_code": 200, "final_url": url, "html": contact_html, "error": ""}
            if url == "https://example.com/free-estimate":
                return {"ok": True, "status_code": 200, "final_url": url, "html": STRONG_FORM, "error": ""}
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            site = scrape_site("https://example.com")
        return calls, site

    def test_nav_chrome_on_a_soft_404_does_not_end_the_search(self) -> None:
        calls, site = self._crawl(CHROME_ONLY)

        self.assertIn("https://example.com/free-estimate", calls)
        self.assertEqual(site["estimate_form_detected"], "Yes")
        self.assertEqual(site["form_strength"], "Strong")

    def test_a_basic_contact_form_does_not_end_the_search(self) -> None:
        # The ordinary shape: a real contact form that is not an estimate
        # funnel, with the estimate page one unlinked URL away.
        calls, site = self._crawl(BASIC_CONTACT_FORM)

        self.assertIn("https://example.com/free-estimate", calls)
        self.assertEqual(site["estimate_form_detected"], "Yes")
        self.assertEqual(site["form_strength"], "Strong")

    def test_a_strong_estimate_form_does_end_the_search(self) -> None:
        # This is terminal — nothing further could improve the answer.
        calls, _site = self._crawl(STRONG_FORM)

        self.assertNotIn("https://example.com/free-estimate", calls)


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

    def test_pass_one_cannot_spend_the_slot_reserved_for_an_unlinked_group(self) -> None:
        # A per-city nav gives the contact group fifteen linked pages and the
        # services group none. Pass 1 must leave room for pass 2 to probe
        # /services, or `weak_services` (+5, and a corroborator worth another
        # +12) fires about a page that was never requested.
        nav = "".join(f'<a href="/contact-{city}">Contact</a>' for city in range(15))
        calls: list[str] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append(url)
            if url == "https://example.com":
                return {"ok": True, "status_code": 200, "final_url": url, "html": _homepage(nav), "error": ""}
            if url == "https://example.com/services":
                return {"ok": True, "status_code": 200, "final_url": url, "html": _homepage("<p>Our services.</p>"), "error": ""}
            return {"ok": True, "status_code": 200, "final_url": url, "html": CHROME_ONLY, "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            site = scrape_site("https://example.com")

        self.assertIn("https://example.com/services", calls)
        self.assertTrue((site["pages"].get("services") or {}).get("ok"))

    def test_the_budget_cannot_be_set_below_the_fallback_ladder(self) -> None:
        # A budget under the ladder silently re-creates the bug this whole
        # module exists to prevent, across every lead in the run.
        floor = len(enrich_leads.CONTACT_ESTIMATE_PATHS) + 3
        with patch.dict(os.environ, {"MAX_CANDIDATE_PAGE_FETCHES": "2"}, clear=False):
            self.assertGreaterEqual(enrich_leads.resolve_candidate_page_budget(), floor)

    def test_truncation_never_inflates_the_pain_score(self) -> None:
        # The property that matters, stated directly: whatever the ceiling
        # does, it must not make a site look worse than crawling it without
        # one. Pain is what carries a lead past VISIBLE_PAIN_BLOCKER, so an
        # inflated score on a truncated crawl is a manufactured lead.
        from lead_machine.evidence import build_evidence_profile

        nav = "".join(f'<a href="/contact-{index}">Contact</a>' for index in range(20))

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            if url == "https://example.com":
                return {"ok": True, "status_code": 200, "final_url": url, "html": _homepage(nav), "error": ""}
            if url == "https://example.com/services":
                return {"ok": True, "status_code": 200, "final_url": url, "html": _homepage("<p>Services.</p>"), "error": ""}
            return {"ok": True, "status_code": 200, "final_url": url, "html": CHROME_ONLY, "error": ""}

        def pain(budget: int) -> int:
            with patch("enrich_leads.fetch_url", side_effect=fake_fetch), patch(
                "enrich_leads.resolve_candidate_page_budget", return_value=budget
            ):
                site = scrape_site("https://example.com")
            profile = build_evidence_profile(
                {"google": {"business_name": "X", "reviews": 30, "rating": 4.5}, "website": site}
            )
            return profile.get("visible_pain_score")

        self.assertLessEqual(pain(enrich_leads.MAX_CANDIDATE_PAGE_FETCHES), pain(500))

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
        # Above the floor, the operator is in charge.
        # AGENTS.md: every MAX_* cap reads from the environment.
        with patch.dict(os.environ, {"MAX_CANDIDATE_PAGE_FETCHES": "24"}, clear=False):
            self.assertEqual(enrich_leads.resolve_candidate_page_budget(), 24)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                enrich_leads.resolve_candidate_page_budget(), enrich_leads.MAX_CANDIDATE_PAGE_FETCHES
            )


class TruncatedAbsenceIsNotEvidenceTest(unittest.TestCase):
    """A form we stopped short of is not a form the site does not have.

    `TruncationIsVisibleTest.test_truncation_never_inflates_the_pain_score`
    states the property but cannot catch this: its fixture has no form at any
    budget, so the absence flags fire identically on both sides of the
    comparison and the delta is zero either way. The fixture here is the one
    that separates them — the site's only estimate form sits on the 20th
    linked contact page, well past the 16-fetch ceiling. Crawled whole, it is
    a healthy site. Crawled to the ceiling, every absence we report is a
    statement about our own budget.

    The `"Unknown"` rewrite already handles the graded fields. The flags are
    the other half of the same sentence: `score_leads.score_online_weakness`
    charges +8 for `no_form_detected` and +8 for `no_clear_cta_detected`, and
    `evidence._derive_visible_pain_profile` adds +14 for the CTA — all inside
    branches that the rewrite to `"Unknown"` *guarantees* are entered, because
    `has_strong_estimate_form` can no longer be true. Left ungated, the
    rewrite lowers the pain by one route and the flags put it straight back.
    """

    # Zero-padded because the candidate URLs are crawled in sorted order, and
    # `/contact-19` unpadded sorts tenth. The form has to be on the page that
    # sorts last, or the ceiling reaches it and there is nothing to test.
    _NAV = "".join(f'<a href="/contact-{index:02d}">Contact {index}</a>' for index in range(20))

    # No <form>, no <input>, no <button>, no <iframe> — `detect_form_signals`
    # grades any of those as Weak, which would satisfy `forms_found` and stop
    # `no_form_detected` firing for the wrong reason. And no CTA_TERMS phrase,
    # so `no_clear_cta_detected` is genuinely reachable.
    _FORMLESS = _homepage("<h2>Our work</h2><p>Serving Northeast Ohio since 1998.</p>")

    def _fake_fetch(self, url: str, timeout: int = 15) -> dict[str, object]:
        if url == "https://example.com":
            html = _homepage(self._NAV)
        elif url == "https://example.com/contact-19":  # the 20th, past the ceiling
            html = STRONG_FORM
        else:
            html = self._FORMLESS
        return {"ok": True, "status_code": 200, "final_url": url, "html": html, "error": ""}

    def _crawl(self, budget: int) -> dict[str, object]:
        with patch("enrich_leads.fetch_url", side_effect=self._fake_fetch), patch(
            "enrich_leads.resolve_candidate_page_budget", return_value=budget
        ):
            return scrape_site("https://example.com")

    def _record(self, site: dict[str, object]) -> dict[str, object]:
        return {
            "google": {"business_name": "X", "reviews": 30, "rating": 4.5},
            "website": site,
            "derived": {},
        }

    def test_the_whole_site_has_a_strong_estimate_form(self) -> None:
        # The control. Without this the ceiling case proves nothing: it would
        # be a site that really has no form.
        site = self._crawl(500)

        self.assertEqual(site["estimate_form_detected"], "Yes")
        self.assertEqual(site["form_strength"], "Strong")
        self.assertNotIn("no_form_detected", site["site_issue_flags"])
        self.assertNotIn("no_clear_cta_detected", site["site_issue_flags"])
        self.assertNotIn(enrich_leads.CANDIDATE_BUDGET_FLAG, site["site_issue_flags"])

    def test_the_ceiling_does_not_assert_the_form_is_missing(self) -> None:
        site = self._crawl(enrich_leads.MAX_CANDIDATE_PAGE_FETCHES)

        # Precondition: we really did stop early, and really did not find it.
        self.assertIn(enrich_leads.CANDIDATE_BUDGET_FLAG, site["site_issue_flags"])
        self.assertFalse(site["forms_found"])

        self.assertNotIn("no_form_detected", site["site_issue_flags"])
        self.assertNotIn("no_clear_cta_detected", site["site_issue_flags"])

    def test_the_ceiling_leaves_the_lead_unknown_not_healthy(self) -> None:
        # Dropping the flags must not read as "we looked and it was fine".
        # Every field the flags spoke for has to say Unknown, which is what
        # the graded fields already do and what downstream already ignores.
        site = self._crawl(enrich_leads.MAX_CANDIDATE_PAGE_FETCHES)

        self.assertEqual(site["contact_form_detected"], "Unknown")
        self.assertEqual(site["estimate_form_detected"], "Unknown")
        self.assertEqual(site["form_strength"], "Unknown")
        self.assertEqual(site["cta_strength"], "Unknown")

    def test_the_ceiling_cannot_score_worse_than_the_whole_site(self) -> None:
        # The downstream half, read-only: both scorers that charge for these
        # flags must come out no higher on the truncated crawl than on the
        # complete one. Pain is what carries a lead past
        # `sendability.VISIBLE_PAIN_BLOCKER`, so a truncated crawl scoring
        # higher is a lead manufactured out of our own budget.
        from lead_machine.evidence import build_evidence_profile
        from score_leads import score_online_weakness

        truncated = self._record(self._crawl(enrich_leads.MAX_CANDIDATE_PAGE_FETCHES))
        whole = self._record(self._crawl(500))

        self.assertLessEqual(
            score_online_weakness(truncated)[0], score_online_weakness(whole)[0]
        )
        self.assertLessEqual(
            build_evidence_profile(truncated)["visible_pain_score"],
            build_evidence_profile(whole)["visible_pain_score"],
        )


class BlankBodyAbsenceIsNotEvidenceTest(unittest.TestCase):
    """A page we could not read is not a page with nothing on it.

    Same sentence as `TruncatedAbsenceIsNotEvidenceTest`, other half of the
    branch. That class covers the fetch-budget case, which is the only one
    `truncated_without_answer` gates; every fixture in it is a 200 carrying
    real HTML. The case that shipped the defect is a 200 carrying *no body* —
    a React shell, a title-only document, a 0-byte response. `fetch_url` sets
    `ok = status < 400`, so none of them is a failure and none reaches
    `empty_site_result`; the crawl takes the normal path, `detect_form_signals`
    early-returns `_empty_form_signals()`, and both absence flags are appended
    about a page nobody ever read.

    The title-only fixture is the one that matters most, and it is why the
    predicate is body-only. `extract_visible_text` runs `get_text()` over the
    whole document and decomposes only `script/style/noscript/svg`, so
    `<title>` — which lives in `<head>` — survives. A blank SPA therefore has
    non-empty "visible text" and `render_outcome: "loaded"`, and any predicate
    written against those reports the page as read. Measured on the corpus:
    of the twelve businesses claiming absence off an unreadable body, eight
    are title-only and would slip a whole-document check.

    There is deliberately no character threshold. Three sites in the corpus
    have genuinely parsed bodies of 76, 94 and 225 characters; any cutoff that
    catches a title catches those too, and suppressing real absence signal
    corpus-wide is the more expensive mistake. Structural emptiness only.
    """

    # The real title of the lead this was written around — Door Works &
    # Construction, whose homepage and all fourteen crawled subpages returned
    # these 56 characters and nothing else.
    DOOR_WORKS_TITLE = "Door Works | Garage Door Repair &amp; Installation | NE Ohio"

    SPA_SHELL = '<html><head></head><body><div id="root"></div></body></html>'
    TITLE_ONLY = f"<html><head><title>{DOOR_WORKS_TITLE}</title></head><body></body></html>"
    ZERO_BYTE = ""

    # Genuinely parsed, genuinely no form: the control. No <form>, <input>,
    # <button> or <iframe> (each of which `detect_form_signals` grades as Weak)
    # and no CTA_TERMS phrase, so both absence claims are honestly reachable.
    PARSED_NO_FORM = _homepage("<h2>Our work</h2><p>Serving Northeast Ohio since 1998.</p>")

    def _crawl(self, html: str) -> dict[str, object]:
        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            return {"ok": True, "status_code": 200, "final_url": url, "html": html, "error": ""}

        # A budget of 500 keeps `budget_exhausted` false, so anything these
        # tests observe comes from the blank body and not from truncation.
        with patch("enrich_leads.fetch_url", side_effect=fake_fetch), patch(
            "enrich_leads.resolve_candidate_page_budget", return_value=500
        ):
            return scrape_site("https://example.com")

    def _record(self, site: dict[str, object]) -> dict[str, object]:
        return {
            "google": {"business_name": "X", "reviews": 30, "rating": 4.5},
            "website": site,
            "derived": {},
        }

    def _assert_absence_withheld(self, site: dict[str, object]) -> None:
        flags = site["site_issue_flags"]

        # Precondition: the crawl really did complete, and really did find
        # nothing. Without this the assertions below pass for the wrong reason.
        self.assertNotIn(enrich_leads.CANDIDATE_BUDGET_FLAG, flags)
        self.assertFalse(site["forms_found"])

        self.assertNotIn("no_form_detected", flags)
        self.assertNotIn("no_clear_cta_detected", flags)
        self.assertNotIn("no_email_detected", flags)
        self.assertNotIn("no_high_ticket_terms_detected", flags)

        # Dropping the flags must not read as "we looked and it was fine".
        self.assertEqual(site["contact_form_detected"], "Unknown")
        self.assertEqual(site["estimate_form_detected"], "Unknown")
        self.assertEqual(site["form_strength"], "Unknown")
        self.assertEqual(site["cta_strength"], "Unknown")

        # "none" already means three different things — parsed and formless,
        # body empty, fetch failed. Only the second is recoverable by a
        # rendered pass, so it needs its own name.
        self.assertEqual(site["form_detection_method"], "no_body")
        self.assertIn("empty_body_on_200", flags)

    def test_body_text_extraction_ignores_the_head(self) -> None:
        extract = enrich_leads.extract_visible_body_text

        self.assertEqual(extract(self.SPA_SHELL), "")
        self.assertEqual(extract(self.TITLE_ONLY), "")
        self.assertEqual(extract(self.ZERO_BYTE), "")
        self.assertIn("Northeast Ohio", extract(self.PARSED_NO_FORM))

        # The whole-document reading the plan proposed, for contrast: this is
        # what a title-only document looks like to `extract_visible_text`.
        self.assertTrue(enrich_leads.extract_visible_text(self.TITLE_ONLY).strip())

    def test_an_spa_shell_asserts_nothing(self) -> None:
        self._assert_absence_withheld(self._crawl(self.SPA_SHELL))

    def test_a_title_only_body_asserts_nothing(self) -> None:
        site = self._crawl(self.TITLE_ONLY)

        # The regression itself: `render_outcome` reports this page as loaded
        # and `visible_text` is 56 non-empty characters, so the absence gate
        # cannot be keyed on either.
        self.assertEqual(site["verification_snapshot"]["render_outcome"], "loaded")
        self.assertTrue(site["verification_snapshot"]["visible_text"].strip())

        self._assert_absence_withheld(site)

    def test_a_zero_byte_two_hundred_asserts_nothing(self) -> None:
        # `fetch_url` sets `ok = status < 400`, so an empty 200 from a parked
        # domain is not a failure and never reaches `empty_site_result` — the
        # one path that already writes "Unknown" correctly.
        self._assert_absence_withheld(self._crawl(self.ZERO_BYTE))

    def test_a_parsed_body_with_no_form_still_reports_absence(self) -> None:
        # Guarding against over-broadness is half the fix. This site was read
        # end to end and genuinely has no form and no CTA; saying so is the
        # signal the whole pipeline runs on.
        site = self._crawl(self.PARSED_NO_FORM)
        flags = site["site_issue_flags"]

        self.assertIn("no_form_detected", flags)
        self.assertIn("no_clear_cta_detected", flags)
        self.assertNotIn("empty_body_on_200", flags)
        self.assertEqual(site["form_detection_method"], "none")
        self.assertEqual(site["estimate_form_detected"], "No")

    def test_a_blank_body_cannot_score_worse_than_a_parsed_one(self) -> None:
        # The property, mirroring the truncation test: absence of data must
        # never outscore presence of data. Pain is what carries a lead past
        # `sendability.VISIBLE_PAIN_BLOCKER`, so a site we could not read
        # scoring higher than one we read is a lead manufactured out of our
        # own parser.
        from lead_machine.evidence import build_evidence_profile
        from score_leads import score_online_weakness

        parsed = self._record(self._crawl(self.PARSED_NO_FORM))

        for name, html in (
            ("spa shell", self.SPA_SHELL),
            ("title only", self.TITLE_ONLY),
            ("zero byte", self.ZERO_BYTE),
        ):
            with self.subTest(body=name):
                blank = self._record(self._crawl(html))
                self.assertLessEqual(
                    score_online_weakness(blank)[0], score_online_weakness(parsed)[0]
                )
                self.assertLessEqual(
                    build_evidence_profile(blank)["visible_pain_score"],
                    build_evidence_profile(parsed)["visible_pain_score"],
                )

    def test_the_rendered_check_reaches_a_page_with_no_ctas(self) -> None:
        # The trigger required `and ctas`, which is exactly the page an SPA
        # cannot produce: no body, no CTA terms, no rendered pass, no way back.
        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            return {"ok": True, "status_code": 200, "final_url": url, "html": self.SPA_SHELL, "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch), patch(
            "enrich_leads.resolve_candidate_page_budget", return_value=500
        ), patch("enrich_leads.detect_rendered_form") as rendered:
            rendered.return_value = enrich_leads._empty_form_signals()
            scrape_site("https://example.com", rendered_form_check=True)

        rendered.assert_called_once()

    def test_a_rendered_form_survives_the_unknown_rewrite(self) -> None:
        # The other direction, and the reason the rewrite is not keyed on the
        # blank body alone: §2.3 exists to let the browser answer the question
        # a blank static body could not, and the rewrite must not then discard
        # the answer it came back with.
        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            return {"ok": True, "status_code": 200, "final_url": url, "html": self.SPA_SHELL, "error": ""}

        strong = enrich_leads.detect_form_signals(STRONG_FORM, page_url="https://example.com")
        strong["form_detection_method"] = "rendered_browser"

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch), patch(
            "enrich_leads.resolve_candidate_page_budget", return_value=500
        ), patch("enrich_leads.detect_rendered_form", return_value=strong):
            site = scrape_site("https://example.com", rendered_form_check=True)

        self.assertEqual(site["estimate_form_detected"], "Yes")
        self.assertEqual(site["form_strength"], "Strong")
        self.assertEqual(site["form_detection_method"], "rendered_browser")

    def test_a_basic_rendered_form_also_survives_the_unknown_rewrite(self) -> None:
        # Strong is not the bar. Guarding the rewrite on `has_terminal_form`
        # alone spared only Strong forms, and on the real recovery run twelve
        # of thirteen SPAs came back `embedded_provider` with a Basic form and
        # were blanked to Unknown anyway — discarding a true finding and the
        # legitimate +12 "Basic estimate form only" pain that goes with it.
        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            return {"ok": True, "status_code": 200, "final_url": url, "html": self.SPA_SHELL, "error": ""}

        basic = enrich_leads.detect_form_signals(
            '<form><input name="email"><button type="submit">Send</button></form>',
            page_url="https://example.com",
        )
        basic["form_detection_method"] = "embedded_provider"
        self.assertTrue(basic["forms_found"])
        self.assertNotEqual(basic["form_strength"], "Strong")

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch), patch(
            "enrich_leads.resolve_candidate_page_budget", return_value=500
        ), patch("enrich_leads.detect_rendered_form", return_value=basic):
            site = scrape_site("https://example.com", rendered_form_check=True)

        self.assertNotEqual(site["form_strength"], "Unknown")
        self.assertEqual(site["form_detection_method"], "embedded_provider")
        self.assertNotIn("no_form_detected", site["site_issue_flags"])


class InterWavePauseTest(unittest.TestCase):
    """The pause belongs in the gaps between waves, not after the last one.

    It used to sit between pages, because pages went out one at a time. They
    go out several at a time now, and pausing *within* a wave would only undo
    the concurrency it was paced against — so the gap that is left to pace is
    the one between waves.
    """

    _BODY = (
        '<a href="/contact-us">Contact</a>'
        '<a href="/services">Services</a>'
        '<a href="/about">About</a>'
        '<a href="/gallery">Gallery</a>'
    )

    def _crawl(self, ceiling: int | None = None, workers: int | None = None) -> tuple[list[str], int]:
        calls: list[str] = []
        with ExitStack() as stack:
            stack.enter_context(
                patch("enrich_leads.fetch_url", side_effect=_record_fetches(_homepage(self._BODY), calls))
            )
            sleep = stack.enter_context(patch("enrich_leads.time.sleep"))
            if ceiling is not None:
                # Patch the resolved budget: the raw constant is floored so an
                # operator cannot set it below the fallback ladder.
                stack.enter_context(
                    patch("enrich_leads.resolve_candidate_page_budget", return_value=ceiling)
                )
            if workers is not None:
                stack.enter_context(patch.dict(os.environ, {"PAGE_FETCH_WORKERS": str(workers)}))
            scrape_site("https://example.com")
        return calls, sleep.call_count

    def test_four_candidate_pages_that_fit_one_wave_are_not_paced(self) -> None:
        calls, pauses = self._crawl(workers=4)

        self.assertEqual(len(calls), 5)  # homepage + four candidates
        self.assertEqual(pauses, 0)

    def test_the_pause_count_tracks_the_wave_count(self) -> None:
        calls, pauses = self._crawl(workers=2)

        self.assertEqual(len(calls), 5)  # homepage + four candidates
        self.assertEqual(pauses, 1)  # two waves of two

    def test_a_serial_crawl_still_paces_between_every_page(self) -> None:
        # The shipped default: every gap between pages is a wave gap, so the
        # pacing is exactly what it was before the crawl went concurrent.
        calls, pauses = self._crawl(workers=1)

        self.assertEqual(len(calls), 5)
        self.assertEqual(pauses, 3)

    def test_the_pause_never_follows_the_last_wave(self) -> None:
        calls, pauses = self._crawl(ceiling=2, workers=1)

        self.assertEqual(len(calls), 3)  # homepage + two candidates
        self.assertEqual(pauses, 1)


class SessionLifetimeTest(unittest.TestCase):
    """The pooled session is per-thread, so it outlives the lead that opened it.

    Connection reuse is worth having *within* a lead — several pages off one
    host — and worth nothing across leads, which are different hosts. What does
    carry across is the cookie jar and the open sockets: a worker thread that
    enriched 40 leads held one jar with 40 sites' cookies in it and never
    closed a single connection. Same-domain cookies will not leak to another
    domain, but the jar and the pool should end with the crawl that opened
    them.
    """

    def tearDown(self) -> None:
        enrich_leads.reset_fetch_session()

    def test_a_leads_cookies_do_not_ride_along_to_the_next_lead(self) -> None:
        session = enrich_leads.fetch_session()
        session.cookies.set("session_id", "lead-one", domain="example.com")

        enrich_leads.reset_fetch_session()

        self.assertEqual(len(enrich_leads.fetch_session().cookies), 0)

    def test_the_pooled_connections_are_released_with_the_lead(self) -> None:
        session = enrich_leads.fetch_session()
        with patch.object(session, "close") as close:
            enrich_leads.reset_fetch_session()

        self.assertEqual(close.call_count, 1)

    def test_resetting_twice_is_harmless(self) -> None:
        enrich_leads.reset_fetch_session()
        enrich_leads.reset_fetch_session()

        self.assertIsNotNone(enrich_leads.fetch_session())

    def test_a_crawl_hands_its_session_back(self) -> None:
        # The reset belongs on the lead boundary, not inside `fetch_url` — a
        # per-fetch reset would give back exactly the handshake the pool exists
        # to avoid. A lead spans a small pool of threads now, so the property
        # is per thread: nobody fetches twice through two different sessions.
        calls: list[str] = []
        lock = threading.Lock()
        sessions_by_thread: dict[int, set[int]] = {}

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            session = enrich_leads.fetch_session()
            with lock:
                calls.append(url)
                sessions_by_thread.setdefault(threading.get_ident(), set()).add(id(session))
            return {"ok": True, "status_code": 200, "final_url": url, "html": STRONG_FORM, "error": ""}

        # Width forced: at the shipped default there is only ever one thread,
        # so the per-thread property would hold vacuously.
        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": "4"}), patch(
            "enrich_leads.fetch_url", side_effect=fake_fetch
        ):
            with patch("enrich_leads.reset_fetch_session") as reset:
                enrich_leads.enrich_row(
                    {"business_name": "Epoxy Pros", "place_id": "place-1", "website_url": "https://example.com"},
                    Path("unused"),
                    take_shots=False,
                )

        self.assertGreater(len(calls), 1)
        self.assertGreater(len(sessions_by_thread), 1)  # the pool really ran
        self.assertTrue(all(len(seen) == 1 for seen in sessions_by_thread.values()))
        self.assertEqual(reset.call_count, 1)  # handed back once, at the end

    def test_the_page_fetch_threads_do_not_outlive_the_lead(self) -> None:
        # `reset_fetch_session` runs on the lead thread and cannot reach a pool
        # thread's thread-local session. What ends those is the thread ending,
        # so the pool has to be joined before the crawl returns — otherwise a
        # run of 150 leads leaks a jar and a socket pool per lead.
        #
        # The width is forced because the shipped default is serial: at width 1
        # every wave is one page, `fetch_wave` stays on the calling thread, and
        # no pool is ever built — so this test would pass with the join deleted.
        body = "".join(f'<a href="/contact-{index}">Contact {index}</a>' for index in range(4))
        calls: list[str] = []
        live_during_crawl: list[int] = []

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            live_during_crawl.append(
                len([t for t in threading.enumerate() if t.name.startswith("page-fetch")])
            )
            return _record_fetches(_homepage(body), calls)(url, timeout)

        with patch.dict(os.environ, {"PAGE_FETCH_WORKERS": "4"}), patch(
            "enrich_leads.fetch_url", side_effect=fake_fetch
        ):
            scrape_site("https://example.com")

        self.assertGreater(max(live_during_crawl), 0)  # a pool really ran
        self.assertEqual([t for t in threading.enumerate() if t.name.startswith("page-fetch")], [])


if __name__ == "__main__":
    unittest.main()
