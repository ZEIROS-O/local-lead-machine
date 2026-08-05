"""Every lead launched its own Chromium, and some launched two.

`check_rendered_site` and `detect_rendered_form` each opened their own
`sync_playwright()` context and their own browser, so a lead that needed both
paid two full Chromium starts — roughly a second each — and then slept an
unconditional 1.5s per page on top, twice. None of that changed a single
verdict; it is the launch and the sleep that cost, not the render.

These tests pin the mechanics rather than the speedup: one browser per worker
thread, one browser *context* per lead closed whether the render succeeds or
explodes, the mobile pass still rendered as a mobile device, and a settle that
waits for the network to go quiet instead of always sleeping. Playwright is
faked throughout — CI installs the package but never `playwright install
chromium`, so a test that needs the real binary is a test that never runs.
"""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import enrich_leads
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

PAGE_HTML = "<html><body><h1>Epoxy floors</h1></body></html>"


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeSite:
    """What the fake Chromium serves for a lead."""

    def __init__(
        self,
        *,
        status: int = 200,
        html: str = PAGE_HTML,
        text: str = "Epoxy floors",
        final_url: str = "https://example.com",
        goto_error: Exception | None = None,
        idle_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.html = html
        self.text = text
        self.final_url = final_url
        self.goto_error = goto_error
        self.idle_error = idle_error


class _FakePage:
    def __init__(self, browser: "_FakeBrowser", context: "_FakeContext") -> None:
        self._browser = browser
        self._context = context
        self.url = browser.site.final_url

    def goto(self, url, wait_until=None, timeout=None):
        self._browser.navigations.append(url)
        if self._browser.site.goto_error:
            raise self._browser.site.goto_error
        return _FakeResponse(self._browser.site.status)

    def wait_for_load_state(self, state, timeout=None):
        self._browser.load_state_waits.append((state, timeout))
        if self._browser.site.idle_error:
            raise self._browser.site.idle_error

    def wait_for_timeout(self, milliseconds):
        self._browser.fixed_waits.append(milliseconds)

    def content(self):
        return self._browser.site.html

    def inner_text(self, selector, timeout=None):
        return self._browser.site.text

    def screenshot(self, path=None, full_page=False):
        self._browser.screenshots.append((str(path), full_page))
        Path(path).write_bytes(b"png")


class _FakeContext:
    def __init__(self, browser: "_FakeBrowser", options: dict) -> None:
        self.browser = browser
        self.options = options
        self.closed = False
        self.pages: list[_FakePage] = []

    def new_page(self) -> _FakePage:
        page = _FakePage(self.browser, self)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, site: _FakeSite) -> None:
        self.site = site
        self.contexts: list[_FakeContext] = []
        self.navigations: list[str] = []
        self.load_state_waits: list[tuple[str, int | None]] = []
        self.fixed_waits: list[int] = []
        self.screenshots: list[tuple[str, bool]] = []
        self.closed = False
        self.connected = True

    def new_context(self, **options) -> _FakeContext:
        context = _FakeContext(self, options)
        self.contexts.append(context)
        return context

    def is_connected(self) -> bool:
        return self.connected

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _Launcher:
    """Stands in for `_launch_browser`, counting how often it is called."""

    def __init__(self, site: _FakeSite | None = None) -> None:
        self.site = site or _FakeSite()
        self.handles: list[enrich_leads.BrowserHandle] = []
        self._lock = threading.Lock()

    def __call__(self) -> enrich_leads.BrowserHandle:
        handle = enrich_leads.BrowserHandle(_FakePlaywright(), _FakeBrowser(self.site))
        with self._lock:
            self.handles.append(handle)
        return handle

    @property
    def launches(self) -> int:
        return len(self.handles)

    @property
    def browser(self) -> _FakeBrowser:
        return self.handles[-1].browser


class _BrowserTestCase(unittest.TestCase):
    def setUp(self) -> None:
        enrich_leads.close_thread_browser()
        self.addCleanup(enrich_leads.close_thread_browser)


class BrowserReuseTest(_BrowserTestCase):
    def test_many_leads_share_one_chromium(self) -> None:
        # The launch is the expensive part and it is per-thread work, not
        # per-lead work.
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            for _ in range(3):
                enrich_leads.check_rendered_site("https://example.com")

        self.assertEqual(launcher.launches, 1)
        self.assertEqual(len(launcher.browser.navigations), 3)

    def test_each_worker_thread_launches_its_own(self) -> None:
        # Playwright's sync API is bound to the thread that created it, so a
        # shared browser across the enrichment pool would be a crash, not an
        # optimisation.
        launcher = _Launcher()
        seen: list[object] = []
        lock = threading.Lock()

        def grab() -> None:
            browser = enrich_leads.thread_browser()
            with lock:
                seen.append(browser)
            enrich_leads.close_thread_browser()

        with patch.object(enrich_leads, "_launch_browser", launcher):
            threads = [threading.Thread(target=grab) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(launcher.launches, 3)
        self.assertEqual(len({id(browser) for browser in seen}), 3)

    def test_a_browser_that_cannot_open_a_context_is_discarded(self) -> None:
        # `is_connected()` is true for a browser that is still up but out of
        # file descriptors after a long run. Its failure is graded as a *site*
        # problem — "Unknown", no mobile screenshot — so keeping it would
        # misreport every remaining lead on this thread rather than one.
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.thread_browser()
            launcher.browser.new_context = lambda **_: (_ for _ in ()).throw(
                RuntimeError("Target page, context or browser has been closed")
            )
            first = enrich_leads.check_rendered_site("https://example.com")
            second = enrich_leads.check_rendered_site("https://example.com")

        self.assertEqual(first["rendered_site_status"], "Unknown")
        self.assertEqual(launcher.launches, 2)  # the poisoned one, then a fresh one
        self.assertTrue(launcher.handles[0].browser.closed)
        self.assertEqual(second["rendered_site_status"], "Loaded")

    def test_a_dead_browser_is_replaced_rather_than_reused(self) -> None:
        # Reuse turns one crashed Chromium into every remaining lead on that
        # thread failing, which is worse than the launch it saved.
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.thread_browser()
            launcher.browser.connected = False
            enrich_leads.thread_browser()

        self.assertEqual(launcher.launches, 2)
        self.assertTrue(launcher.handles[0].browser.closed)

    def test_closing_the_thread_browser_stops_playwright_too(self) -> None:
        # The driver is a subprocess; closing the browser alone leaks it.
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.thread_browser()
        handle = launcher.handles[0]

        enrich_leads.close_thread_browser()
        enrich_leads.close_thread_browser()  # idempotent

        self.assertTrue(handle.browser.closed)
        self.assertTrue(handle.playwright.stopped)

    def test_a_browser_that_fails_to_close_does_not_abort_the_run(self) -> None:
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.thread_browser()
        handle = launcher.handles[0]
        handle.browser.close = lambda: (_ for _ in ()).throw(RuntimeError("driver gone"))

        enrich_leads.close_thread_browser()

        self.assertTrue(handle.playwright.stopped)

    def test_pool_shutdown_closes_every_worker_browser(self) -> None:
        # A worker's Chromium can only be closed on the thread that opened it,
        # so the pool has to hand each worker its own closing task before it
        # shuts down.
        launcher = _Launcher()
        # Run at the shipped default rather than a literal, so whatever width
        # browser runs actually use is the width this path is proven at.
        workers = enrich_leads.DEFAULT_BROWSER_ENRICH_WORKERS
        # The pool only spawns a thread when no idle one is free, so a fast
        # enrich can run twelve leads on two threads. Hold every worker at once
        # to guarantee the full width is open when shutdown runs.
        all_workers_busy = threading.Barrier(workers, timeout=10)
        state = threading.local()

        def enrich(row):
            enrich_leads.thread_browser()
            if not getattr(state, "waited", False):
                state.waited = True
                all_workers_busy.wait()
            return {"google": row}

        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.enrich_rows_concurrently(
                [{"place_id": f"place-{index}"} for index in range(4 * workers)],
                workers=workers,
                enrich=enrich,
                on_complete=lambda *_: None,
            )

        self.assertEqual(launcher.launches, workers)
        for handle in launcher.handles:
            self.assertTrue(handle.browser.closed)
            self.assertTrue(handle.playwright.stopped)

    def test_a_pool_that_never_grew_still_closes_what_it_opened(self) -> None:
        # One lead, four configured workers: the pool only ever spawns the one
        # thread it needed. The browser it opened still has to be closed, and
        # the run must not sit out the rendezvous timeout waiting for three
        # workers that were never started.
        launcher = _Launcher()

        def enrich(row):
            enrich_leads.thread_browser()
            return {"google": row}

        started = time.monotonic()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.enrich_rows_concurrently(
                [{"place_id": "place-1"}],
                workers=4,
                enrich=enrich,
                on_complete=lambda *_: None,
            )
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(launcher.launches, 1)
        for handle in launcher.handles:
            self.assertTrue(handle.browser.closed)
        self.assertLess(elapsed, enrich_leads.BROWSER_SHUTDOWN_RENDEZVOUS_SECONDS)

    def test_an_enrichment_that_explodes_still_closes_the_browsers(self) -> None:
        # `on_complete` raising is not caught inside the pool loop.
        launcher = _Launcher()

        def enrich(row):
            enrich_leads.thread_browser()
            return {"google": row}

        def on_complete(*_):
            raise RuntimeError("the writer died")

        with patch.object(enrich_leads, "_launch_browser", launcher):
            with self.assertRaises(RuntimeError):
                enrich_leads.enrich_rows_concurrently(
                    [{"place_id": "place-1"}],
                    workers=2,
                    enrich=enrich,
                    on_complete=on_complete,
                )

        for handle in launcher.handles:
            self.assertTrue(handle.browser.closed)

    def test_the_serial_path_closes_its_browser_too(self) -> None:
        launcher = _Launcher()

        def enrich(row):
            enrich_leads.thread_browser()
            return {"google": row}

        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.enrich_rows_concurrently(
                [{"place_id": "place-1"}],
                workers=1,
                enrich=enrich,
                on_complete=lambda *_: None,
            )

        self.assertEqual(launcher.launches, 1)
        self.assertTrue(launcher.handles[0].browser.closed)


class PerLeadIsolationTest(_BrowserTestCase):
    def test_a_lead_gets_its_own_context_and_closes_it(self) -> None:
        # The browser outlives the lead now, so cookies, storage and service
        # workers have to die with the context or they ride along to the next
        # lead on this thread.
        launcher = _Launcher()
        with TemporaryDirectory() as tmp, patch.object(enrich_leads, "_launch_browser", launcher):
            root = Path(tmp)
            enrich_leads.check_rendered_site(
                "https://example.com",
                desktop_path=root / "desktop.png",
                mobile_path=root / "mobile.png",
            )

        self.assertEqual(len(launcher.browser.contexts), 2)
        self.assertTrue(all(context.closed for context in launcher.browser.contexts))

    def test_the_context_is_closed_even_when_the_render_explodes(self) -> None:
        launcher = _Launcher(_FakeSite(goto_error=RuntimeError("net::ERR_CONNECTION_REFUSED")))
        with patch.object(enrich_leads, "_launch_browser", launcher):
            result = enrich_leads.check_rendered_site("https://example.com")

        self.assertEqual(result["rendered_site_status"], "Broken")
        self.assertTrue(all(context.closed for context in launcher.browser.contexts))

    def test_an_http_error_still_closes_the_context(self) -> None:
        # This path returns early, before the screenshots.
        launcher = _Launcher(_FakeSite(status=404))
        with patch.object(enrich_leads, "_launch_browser", launcher):
            result = enrich_leads.check_rendered_site("https://example.com")

        self.assertEqual(result["rendered_site_status"], "Broken")
        self.assertTrue(all(context.closed for context in launcher.browser.contexts))

    def test_the_mobile_pass_is_still_rendered_as_a_mobile_device(self) -> None:
        # `is_mobile` is a context option, not a page one. Putting both pages
        # in one context would render the mobile shot as a narrow desktop and
        # move `mobile_visual_score` — which this change is not allowed to do.
        launcher = _Launcher()
        with TemporaryDirectory() as tmp, patch.object(enrich_leads, "_launch_browser", launcher):
            root = Path(tmp)
            enrich_leads.check_rendered_site(
                "https://example.com",
                desktop_path=root / "desktop.png",
                mobile_path=root / "mobile.png",
            )

        desktop, mobile = launcher.browser.contexts
        self.assertEqual(desktop.options["viewport"], {"width": 1440, "height": 1200})
        self.assertNotIn("is_mobile", desktop.options)
        self.assertEqual(mobile.options["viewport"], {"width": 390, "height": 1200})
        self.assertTrue(mobile.options["is_mobile"])

    def test_both_screenshots_are_still_full_page(self) -> None:
        launcher = _Launcher()
        with TemporaryDirectory() as tmp, patch.object(enrich_leads, "_launch_browser", launcher):
            root = Path(tmp)
            result = enrich_leads.check_rendered_site(
                "https://example.com",
                desktop_path=root / "desktop.png",
                mobile_path=root / "mobile.png",
            )

        self.assertEqual(result["rendered_site_status"], "Loaded")
        self.assertEqual([shot[1] for shot in launcher.browser.screenshots], [True, True])
        self.assertTrue(result["desktop"].endswith("desktop.png"))
        self.assertTrue(result["mobile"].endswith("mobile.png"))

    def test_the_form_check_shares_the_pooled_browser(self) -> None:
        # This was the second Chromium launch a single lead could pay for.
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.check_rendered_site("https://example.com")
            enrich_leads.detect_rendered_form("https://example.com")

        self.assertEqual(launcher.launches, 1)
        self.assertTrue(all(context.closed for context in launcher.browser.contexts))

    def test_the_form_check_closes_its_context_when_the_render_fails(self) -> None:
        launcher = _Launcher(_FakeSite(goto_error=PlaywrightTimeoutError("timeout")))
        with patch.object(enrich_leads, "_launch_browser", launcher):
            signals = enrich_leads.detect_rendered_form("https://example.com")

        self.assertEqual(signals["form_detection_method"], "rendered_browser")
        self.assertFalse(signals["forms_found"])
        self.assertTrue(all(context.closed for context in launcher.browser.contexts))


class RenderSettleTest(_BrowserTestCase):
    def test_a_quiet_page_is_not_slept_on(self) -> None:
        # 1.5s per page, twice per lead, on a page that finished loading.
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.check_rendered_site("https://example.com")

        self.assertEqual([state for state, _ in launcher.browser.load_state_waits], ["networkidle"])
        self.assertEqual(launcher.browser.fixed_waits, [])

    def test_a_busy_page_is_never_slower_than_the_sleep_it_replaced(self) -> None:
        # Ad and chat widgets keep a page from ever reaching network idle. A
        # 2.5s ceiling plus a fallback measured up to 6s/lead *slower* than the
        # 1.5s sleep this replaced, so the ceiling is that sleep: a timed-out
        # settle has already spent the whole budget and adds nothing on top.
        launcher = _Launcher(_FakeSite(idle_error=PlaywrightTimeoutError("still busy")))
        with TemporaryDirectory() as tmp, patch.object(enrich_leads, "_launch_browser", launcher):
            result = enrich_leads.check_rendered_site(
                "https://example.com",
                desktop_path=Path(tmp) / "desktop.png",
            )

        self.assertEqual(result["rendered_site_status"], "Loaded")
        self.assertEqual(launcher.browser.fixed_waits, [])
        self.assertLessEqual(enrich_leads.RENDER_SETTLE_TIMEOUT_MS, 1500)

    def test_a_load_state_that_cannot_be_reported_still_gets_a_wait(self) -> None:
        # "Still busy" and "could not tell you" are different answers: the
        # second one waited out nothing, so the page would be read unrendered.
        launcher = _Launcher(_FakeSite(idle_error=RuntimeError("no load state")))
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.check_rendered_site("https://example.com")

        self.assertEqual(len(launcher.browser.fixed_waits), 1)
        self.assertGreater(launcher.browser.fixed_waits[0], 0)
        self.assertLessEqual(launcher.browser.fixed_waits[0], enrich_leads.RENDER_SETTLE_FALLBACK_MS)

    def test_the_form_check_keeps_the_full_observation_window(self) -> None:
        # This is the one path that reads evidence out of the DOM, and it only
        # runs when the static crawl found no form. A page that goes quiet
        # early can still inject its estimate form late, and reporting "no
        # form" about it *adds* pain (+20 no estimate form, +18/+12 weak or
        # basic) — a cheaper look must not manufacture that.
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.detect_rendered_form("https://example.com")

        self.assertEqual(len(launcher.browser.fixed_waits), 1)
        self.assertGreaterEqual(
            launcher.browser.fixed_waits[0], enrich_leads.RENDER_FORM_SETTLE_FLOOR_MS - 100
        )
        self.assertGreaterEqual(enrich_leads.RENDER_FORM_SETTLE_FLOOR_MS, 1500)

    def test_the_screenshot_pass_does_not_pay_the_form_floor(self) -> None:
        # Nothing is read out of that DOM — the screenshot is the artifact, and
        # network idle is a better "the images arrived" signal than a sleep.
        launcher = _Launcher()
        with TemporaryDirectory() as tmp, patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.check_rendered_site(
                "https://example.com",
                desktop_path=Path(tmp) / "desktop.png",
                mobile_path=Path(tmp) / "mobile.png",
            )

        self.assertEqual(launcher.browser.fixed_waits, [])

    def test_the_settle_never_outlasts_the_render_budget(self) -> None:
        # The per-lead timeout shrinks this to whatever is left; waiting for
        # idle must not spend the whole remaining budget.
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.check_rendered_site("https://example.com", timeout_ms=900)

        _, timeout = launcher.browser.load_state_waits[0]
        self.assertLessEqual(timeout, 900)

    def test_the_settle_is_bounded_below_the_render_timeout(self) -> None:
        launcher = _Launcher()
        with patch.object(enrich_leads, "_launch_browser", launcher):
            enrich_leads.check_rendered_site("https://example.com", timeout_ms=25000)

        _, timeout = launcher.browser.load_state_waits[0]
        self.assertEqual(timeout, enrich_leads.RENDER_SETTLE_TIMEOUT_MS)
        self.assertLessEqual(enrich_leads.RENDER_SETTLE_TIMEOUT_MS, 3000)


if __name__ == "__main__":
    unittest.main()
