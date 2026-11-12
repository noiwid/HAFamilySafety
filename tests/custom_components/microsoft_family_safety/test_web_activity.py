"""Websites visited and searches from the mobile activity report (#65)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.microsoft_family_safety.api_client import (
    FamilySafetyWebAPI,
    FamilySafetyWebAPIError,
    summarize_web_activity,
)

CHILD = "1234567890123456"

WEB = {
    "allowed": [
        {"domain": "example.com", "totalCountOfVisitsToDomain": 3,
         "lastVisitToDomain": "2026-11-08T11:36:00", "visits": [{"url": "x"}]},
        {"domain": "news.example", "totalCountOfVisitsToDomain": 108,
         "lastVisitToDomain": "2026-11-09T08:00:00"},
    ],
    "blocked": [{"domain": "blocked.example", "totalCountOfVisitsToDomain": 1}],
}
SEARCH = {
    "searched": [
        {"domain": "bing.com", "searches": [
            {"search": "older", "date": "2026-11-07T10:00:00"},
            {"search": "newer", "date": "2026-11-09T10:00:00"},
        ]}
    ]
}
# Real shape (#65): one row per URL variant, the term inside an object.
VIDEO_SEARCH = {
    "searched": [
        {"domain": "video.example", "searches": [
            {"search": {"url": "https://video.example/r?q=cats", "searchTerm": "cats"},
             "date": "2026-11-10T08:00:00", "visitCount": 3},
            {"search": {"url": "https://m.video.example/r?q=cats#searching",
                        "searchTerm": "cats"},
             "date": "2026-11-11T08:00:00", "visitCount": 2},
            {"search": {"url": "https://video.example/r"}, "date": "2026-11-11T09:00:00"},
        ]}
    ]
}


def test_summary_sorts_and_trims_the_report() -> None:
    summary = summarize_web_activity(WEB, SEARCH)
    assert summary["report_available"] is True
    assert summary["total_visits"] == 112
    assert summary["site_count"] == 3
    assert summary["blocked_count"] == 1
    assert [s["domain"] for s in summary["sites"]] == [
        "news.example", "example.com", "blocked.example"
    ]
    assert summary["sites"][1] == {
        "domain": "example.com", "visits": 3,
        "last_visit": "2026-11-08T11:36:00+00:00", "blocked": False,
    }
    assert [s["search"] for s in summary["searches"]] == ["newer", "older"]


def test_no_report_is_not_zero_browsing() -> None:
    summary = summarize_web_activity(None, None)
    assert summary["report_available"] is False
    assert summary["total_visits"] == 0
    assert summary["sites"] == []


async def test_403_reads_as_no_report_and_skips_the_search_call() -> None:
    api = FamilySafetyWebAPI(authenticator=None)
    api._request = AsyncMock(
        side_effect=FamilySafetyWebAPIError("API request failed with status 403: ")
    )
    summary = await api.get_web_activity(CHILD)
    assert summary["report_available"] is False
    assert api._request.await_count == 1


async def test_report_request_uses_a_boolean_allow_status() -> None:
    api = FamilySafetyWebAPI(authenticator=None)
    api._request = AsyncMock(side_effect=[WEB, SEARCH])
    summary = await api.get_web_activity(CHILD)
    assert summary["total_visits"] == 112
    web_call, search_call = api._request.await_args_list
    assert web_call.args[1] == f"/v1/activityreport/webactivity/{CHILD}"
    assert web_call.kwargs["params"]["allowStatus"] == "true"
    assert web_call.kwargs["params"]["beginTime"].endswith("Z")
    assert "allowStatus" not in search_call.kwargs["params"]


def test_search_objects_are_grouped_by_term() -> None:
    searches = summarize_web_activity(WEB, VIDEO_SEARCH)["searches"]
    assert searches == [{
        "search": "cats", "visits": 3,
        "date": "2026-11-11T08:00:00+00:00", "site": "video.example",
    }]


# Shape of a real blocked[] entry (#65), URLs replaced.
BLOCKED_WEB = {
    "allowed": [
        {"domain": "example.com", "totalCountOfVisitsToDomain": 5,
         "lastVisitToDomain": "2026-11-12T18:00:00",
         "visits": [{"date": "2026-11-12T18:00:00", "visitCount": 5,
                     "pageVisit": {"url": "https://example.com/allowed",
                                   "allowedStatus": 0}}]},
    ],
    "blocked": [
        {"domain": "blocked.example", "lastVisitToDomain": "2026-11-12T17:21:20.9496358",
         "totalCountOfVisitsToDomain": 3, "allowedStatus": 2,
         "detailedAllowedStatus": "BlockedForInappropriateContent",
         "visits": [
             {"date": "2026-11-11T09:00:00", "visitCount": 1,
              "pageVisit": {"url": "https://blocked.example/older",
                            "title": "https://blocked.example/older", "referrer": "",
                            "categories": "", "applicationName": "", "allowedStatus": 2}},
             {"date": "2026-11-12T17:21:20.9496358", "visitCount": 2,
              "pageVisit": {"url": "https://blocked.example/path",
                            "title": "https://blocked.example/path", "referrer": "",
                            "categories": "", "applicationName": "", "allowedStatus": 2}},
         ]},
        {"domain": "no-detail.example", "totalCountOfVisitsToDomain": 1},
    ],
}


def test_blocked_attempts_are_listed_by_url() -> None:
    summary = summarize_web_activity(BLOCKED_WEB, None)
    assert summary["blocked_sites"] == [
        {"url": "https://blocked.example/path", "visits": 2,
         "last_visit": "2026-11-12T17:21:20.949635+00:00", "domain": "blocked.example"},
        {"url": "https://blocked.example/older", "visits": 1,
         "last_visit": "2026-11-11T09:00:00+00:00", "domain": "blocked.example"},
    ]
    # Domain rows are unchanged, and allowed URLs are never listed.
    assert summary["blocked_count"] == 2
    assert {s["domain"] for s in summary["sites"]} == {
        "example.com", "blocked.example", "no-detail.example"
    }


def test_no_report_has_no_blocked_attempts() -> None:
    assert summarize_web_activity(None, None)["blocked_sites"] == []
    assert summarize_web_activity(WEB, SEARCH)["blocked_sites"] == []
