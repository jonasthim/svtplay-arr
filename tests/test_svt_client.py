import json
import re
from pathlib import Path

import httpx
import pytest

from svtplay_arr.svt.client import SvtApiError, SvtClient, derive_slug

FIX = Path(__file__).parent / "fixtures/svt"

_ALIAS_RE = re.compile(r"query\(\$q: String!\)\{(\w+):")


def _client(handler) -> SvtClient:
    transport = httpx.MockTransport(handler)
    return SvtClient(http=httpx.AsyncClient(transport=transport))


def _alias_of(request: httpx.Request) -> str:
    """Pull the per-request GraphQL field alias back out of the query text.

    Mirrors what a real GraphQL server does: echo whatever alias the client
    asked for. The client asserts on this being present in the response
    (Finding B) as the CDN-cache-mismatch guard.
    """
    query = request.url.params["query"]
    match = _ALIAS_RE.match(query)
    assert match, f"no alias found in query: {query!r}"
    return match.group(1)


async def test_search_returns_series_hits():
    body = json.loads((FIX / "search-gvfo-20260824.json").read_text(encoding="utf-8"))
    real_hits = body["data"]["search"]

    def handler(request):
        alias = _alias_of(request)
        return httpx.Response(200, json={"data": {alias: real_hits}})

    hits = await _client(handler).search_series("gift vid första ögonkastet")
    assert hits[0].svt_id == "jpmQD3q"
    assert hits[0].typename == "TvSeries"
    assert "<em>" not in hits[0].name  # search highlighting stripped


async def test_search_sends_cache_buster():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        alias = _alias_of(request)
        return httpx.Response(200, json={"data": {alias: []}})

    await _client(handler).search_series("x")
    assert "cb=" in seen["url"]


async def test_search_sends_term_as_graphql_variable_not_interpolated():
    """Finding C: the term must travel as a GraphQL variable, not be spliced
    into the query document -- a trailing backslash used to be able to
    escape the closing quote of a hand-built query string."""
    seen = {}

    def handler(request):
        seen["query"] = request.url.params["query"]
        seen["variables"] = json.loads(request.url.params["variables"])
        alias = _alias_of(request)
        return httpx.Response(200, json={"data": {alias: []}})

    term = 'weird" term \\'
    await _client(handler).search_series(term)
    assert term not in seen["query"]
    assert seen["variables"] == {"q": term}


async def test_graphql_errors_raise():
    def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "boom"}]})

    with pytest.raises(SvtApiError):
        await _client(handler).search_series("x")


async def test_graphql_response_missing_alias_raises():
    """The CDN was observed serving a well-formed body for a *different*
    query. A body with a `data` block that does not contain the alias we
    asked for must be rejected, not silently treated as an empty result."""

    def handler(request):
        return httpx.Response(200, json={"data": {"someOtherQuery": []}})

    with pytest.raises(SvtApiError):
        await _client(handler).search_series("x")


async def test_search_skips_malformed_hit_without_raising():
    def handler(request):
        alias = _alias_of(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    alias: [
                        {"name": "Missing svtId", "item": {"__typename": "TvSeries"}},
                        {"svtId": "abc123", "item": {}},  # missing __typename
                        {
                            "svtId": "jpmQD3q",
                            "name": "Gift vid första ögonkastet",
                            "item": {"__typename": "TvSeries"},
                        },
                    ]
                }
            },
        )

    hits = await _client(handler).search_series("x")
    assert len(hits) == 1
    assert hits[0].svt_id == "jpmQD3q"


async def test_search_http_error_surfaces_as_svt_api_error():
    def handler(request):
        return httpx.Response(503, text="upstream down")

    with pytest.raises(SvtApiError):
        await _client(handler).search_series("x")


async def test_list_episodes_parses_show_page():
    html = (FIX / "show-gvfo-20260824.html").read_text(encoding="utf-8")

    def handler(request):
        return httpx.Response(200, text=html)

    eps = await _client(handler).list_episodes("gift-vid-forsta-ogonkastet")
    assert len(eps) == 27


async def test_list_episodes_http_error_surfaces_as_svt_api_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(SvtApiError):
        await _client(handler).list_episodes("gift-vid-forsta-ogonkastet")


async def test_list_episodes_404_carries_the_status_code():
    """The config page's mapping check tells a nonexistent slug apart from
    every other SVT failure by this attribute -- it must survive the
    httpx.HTTPStatusError -> SvtApiError translation."""

    def handler(request):
        return httpx.Response(404, text="not found")

    with pytest.raises(SvtApiError) as excinfo:
        await _client(handler).list_episodes("no-such-slug")
    assert excinfo.value.status_code == 404


async def test_list_episodes_non_status_failure_has_no_status_code():
    """A transport-level failure (here: a malformed response httpx itself
    raises trying to read status) is a different kind of failure than a 404
    and must not be reported as one."""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(SvtApiError) as excinfo:
        await _client(handler).list_episodes("gift-vid-forsta-ogonkastet")
    assert excinfo.value.status_code is None


async def test_resolve_quality_reads_hls_master_playlist():
    """Finding A: /video/{svt_id} carries no resolution/bitrate fields at
    all -- only format/url/resolve/redirect. Quality must be read from the
    HLS master playlist one of those entries points to."""
    video_body = json.loads(
        (FIX / "video-KZmQ5JY-20260824.json").read_text(encoding="utf-8")
    )
    manifest = (FIX / "hls-master-KZmQ5JY-20260824.m3u8").read_text(encoding="utf-8")
    hls_url = next(
        ref["url"]
        for ref in video_body["videoReferences"]
        if ref["format"] == "hls-cmaf-avc"
    )

    def handler(request):
        if str(request.url) == hls_url:
            return httpx.Response(200, text=manifest)
        return httpx.Response(200, json=video_body)

    q = await _client(handler).resolve_quality("KZmQ5JY")
    assert q.height == 1080
    assert q.label == "WEBDL-1080p"
    assert q.bitrate_kbps == 3282


async def test_resolve_quality_reads_exact_content_duration():
    """The video endpoint carries an exact `contentDuration` (seconds) --
    materially more accurate than the show page's coarse "N min"
    subheading. `resolve_quality` must thread it onto QualityInfo so the
    resolver can prefer it over the page's estimate."""
    video_body = json.loads(
        (FIX / "video-KZmQ5JY-20260824.json").read_text(encoding="utf-8")
    )
    assert video_body["contentDuration"] == 3498  # fixture sanity check
    manifest = (FIX / "hls-master-KZmQ5JY-20260824.m3u8").read_text(encoding="utf-8")
    hls_url = next(
        ref["url"]
        for ref in video_body["videoReferences"]
        if ref["format"] == "hls-cmaf-avc"
    )

    def handler(request):
        if str(request.url) == hls_url:
            return httpx.Response(200, text=manifest)
        return httpx.Response(200, json=video_body)

    q = await _client(handler).resolve_quality("KZmQ5JY")
    assert q.duration_s == 3498


async def test_resolve_quality_duration_is_none_when_content_duration_missing():
    video_body = json.loads(
        (FIX / "video-KZmQ5JY-20260824.json").read_text(encoding="utf-8")
    )
    del video_body["contentDuration"]
    manifest = (FIX / "hls-master-KZmQ5JY-20260824.m3u8").read_text(encoding="utf-8")
    hls_url = next(
        ref["url"]
        for ref in video_body["videoReferences"]
        if ref["format"] == "hls-cmaf-avc"
    )

    def handler(request):
        if str(request.url) == hls_url:
            return httpx.Response(200, text=manifest)
        return httpx.Response(200, json=video_body)

    q = await _client(handler).resolve_quality("KZmQ5JY")
    assert q.duration_s is None


async def test_resolve_quality_returns_none_without_hls_reference():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "videoReferences": [
                    {
                        "format": "dash-avc",
                        "url": "https://example.test/dash-avc.mpd",
                        "resolve": "https://example.test/resolve/dash-avc.mpd",
                        "redirect": "https://example.test/redirect/dash-avc.mpd",
                    }
                ]
            },
        )

    q = await _client(handler).resolve_quality("KZmQ5JY")
    assert q is None


async def test_resolve_quality_http_error_surfaces_as_svt_api_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(SvtApiError):
        await _client(handler).resolve_quality("KZmQ5JY")


def test_derive_slug_folds_swedish_diacritics():
    assert derive_slug("Gift vid första ögonkastet") == "gift-vid-forsta-ogonkastet"


def test_derive_slug_drops_apostrophes_instead_of_treating_them_as_a_separator():
    # The real convention elides the contraction rather than dashing it:
    # "annas-hemlighet", not "anna-s-hemlighet".
    assert derive_slug("Anna's hemlighet") == "annas-hemlighet"


def test_derive_slug_never_returns_an_empty_string():
    # A title with no ASCII letters/digits and nothing in the fold table
    # must not silently produce "", which would flow into a mapping as a
    # broken slug (radio value "{svt_id}|").
    slug = derive_slug("深夜食堂")
    assert slug != ""
    assert slug == "needs-manual-slug"
