from __future__ import annotations

import unittest
from unittest.mock import patch

import places_search


def _page(ids, token=None):
    data = {"places": [{"id": pid, "displayName": {"text": pid}} for pid in ids]}
    if token:
        data["nextPageToken"] = token
    return data


class TextSearchPagedTest(unittest.TestCase):
    def test_combines_results_across_pages_until_no_token(self) -> None:
        pages = [
            _page(["a", "b"], token="tok-1"),
            _page(["c", "d"]),  # no token -> stop
        ]
        with patch("places_search.request_json", side_effect=pages) as call:
            places, pages_fetched = places_search.text_search_paged(
                "key", "epoxy Cleveland OH", page_size=20, max_pages=3
            )
        self.assertEqual([p["id"] for p in places], ["a", "b", "c", "d"])
        self.assertEqual(pages_fetched, 2)
        self.assertEqual(call.call_count, 2)
        # Second request must repeat params and carry the pageToken.
        second_body = call.call_args_list[1].kwargs["json_body"]
        self.assertEqual(second_body["pageToken"], "tok-1")
        self.assertEqual(second_body["pageSize"], 20)
        self.assertEqual(second_body["textQuery"], "epoxy Cleveland OH")

    def test_max_pages_stops_even_when_token_remains(self) -> None:
        pages = [
            _page(["a"], token="tok-1"),
            _page(["b"], token="tok-2"),
            _page(["c"], token="tok-3"),  # still a token, but cap is 2
        ]
        with patch("places_search.request_json", side_effect=pages) as call:
            places, pages_fetched = places_search.text_search_paged(
                "key", "q", page_size=20, max_pages=2
            )
        self.assertEqual([p["id"] for p in places], ["a", "b"])
        self.assertEqual(pages_fetched, 2)
        self.assertEqual(call.call_count, 2)

    def test_duplicate_place_ids_across_pages_are_deduped(self) -> None:
        pages = [
            _page(["a", "b"], token="tok-1"),
            _page(["b", "c"]),  # "b" repeats across pages
        ]
        with patch("places_search.request_json", side_effect=pages):
            places, pages_fetched = places_search.text_search_paged(
                "key", "q", page_size=20, max_pages=3
            )
        self.assertEqual([p["id"] for p in places], ["a", "b", "c"])
        self.assertEqual(pages_fetched, 2)

    def test_page_size_capped_at_twenty(self) -> None:
        with patch("places_search.request_json", side_effect=[_page(["a"])]) as call:
            places_search.text_search_paged("key", "q", page_size=50, max_pages=1)
        self.assertEqual(call.call_args_list[0].kwargs["json_body"]["pageSize"], 20)

    def test_wrapper_returns_single_page_list(self) -> None:
        with patch("places_search.request_json", side_effect=[_page(["a", "b"], token="tok")]) as call:
            result = places_search.text_search("key", "q", 20)
        self.assertEqual([p["id"] for p in result], ["a", "b"])
        self.assertEqual(call.call_count, 1)  # wrapper never paginates


if __name__ == "__main__":
    unittest.main()
