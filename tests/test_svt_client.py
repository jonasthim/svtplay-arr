import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from svtplay_arr.svt.client import SvtApiError, SvtClient, derive_slug

FIX = Path(__file__).parent / "fixtures/svt"

_ALIAS_RE = re.compile(r"query\(\$\w+: String!\)\{(\w+):")


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


async def test_cache_buster_travels_inside_variables():
    """The SVT CDN keys on (path, ua, variables) -- not on the query text,
    and not on a `cb` query parameter. Measured live 2026-08-28: varying a
    `cb` param changed nothing, and the same body came back; varying an
    (undeclared, therefore server-ignored) variable did serve a fresh one,
    3/3, against a fixed-value control that did not. So the nonce is only a
    cache-buster where it is here. A mutation moving it back out to a query
    param must fail this."""
    seen = {}

    def handler(request):
        seen["params"] = request.url.params
        seen["variables"] = json.loads(request.url.params["variables"])
        alias = _alias_of(request)
        return httpx.Response(200, json={"data": {alias: []}})

    await _client(handler).search_series("x")
    assert "cb" in seen["variables"], "the nonce is not in the CDN's cache key"
    assert seen["variables"]["cb"]
    assert "cb" not in seen["params"], (
        "a `cb` query param is not in the cache key and buys nothing"
    )


async def test_repeated_identical_calls_send_different_variables():
    """Two searches for the *same* term must still differ in `variables`,
    or the second is served the first's cached body -- which, since the
    body carries the first request's field alias, fails the alias check and
    raises. That is what used to happen for any repeat inside the 20s TTL."""
    seen = []

    def handler(request):
        seen.append(json.loads(request.url.params["variables"]))
        alias = _alias_of(request)
        return httpx.Response(200, json={"data": {alias: []}})

    client = _client(handler)
    await client.search_series("same term")
    await client.search_series("same term")

    assert seen[0]["q"] == seen[1]["q"] == "same term"
    assert seen[0] != seen[1], "the CDN would serve the second one the first's body"


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
    assert seen["variables"]["q"] == term


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


# --- episode listing, from the Contento details page ------------------------
#
# `list_episodes` reads `detailsPageByPath`, which carries every field
# `SvtEpisode` needs as a typed value, for the same one GET the show-page
# HTML cost -- at roughly 7% of the bytes. What it replaced was a regex scan
# over an escaped React flight payload; the failure modes below are the ones
# that swap changes, and each is asserted rather than assumed.


def _page(*selections) -> dict:
    return {"associatedContent": list(selections)}


def _selection(*teasers, selection_type="season") -> dict:
    return {
        "selectionType": selection_type,
        "items": list(teasers),
        "itemsPaginated": {"totalSize": len(teasers)},
    }


def _teaser(
    svt_id="KZmQ5JY",
    heading="1. Tager du..?",
    url=None,
    duration=3498,
    valid_from="2026-08-23T02:00:00+02:00",
    overlay=None,
    typename="Episode",
) -> dict:
    if url is None:
        url = f"/video/{svt_id}/gift-vid-forsta-ogonkastet/1-tager-du"
    return {
        "heading": heading,
        "upcomingOverlay": overlay,
        "item": {
            "__typename": typename,
            "svtId": svt_id,
            "duration": duration,
            "validFrom": valid_from,
            "urls": {"svtplay": url},
        },
    }


def _details_client(page, seen=None) -> SvtClient:
    """A client whose GraphQL endpoint answers with `page` under the alias
    the request actually asked for -- which is what a real server does, and
    what `_graphql`'s echo check requires."""

    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json={"data": {_alias_of(request): page}})

    return _client(handler)


async def test_list_episodes_builds_episodes_from_the_details_page():
    page = _page(_selection(_teaser()))

    eps = await _details_client(page).list_episodes("gift-vid-forsta-ogonkastet")

    assert len(eps) == 1
    ep = eps[0]
    assert ep.svt_id == "KZmQ5JY"
    assert ep.title == "1. Tager du..?"
    assert ep.url == "/video/KZmQ5JY/gift-vid-forsta-ogonkastet/1-tager-du"
    assert ep.ordinal == 1
    assert ep.published == date(2026, 8, 23)
    assert ep.available is True
    assert ep.duration_s == 3498  # exact seconds, not the page's rounded minutes


async def test_list_episodes_asks_for_one_graphql_get_and_nothing_else():
    """This replaced one HTML fetch. It must not have become two requests --
    the resolver's RSS sweep calls it per mapping, on Sonarr's poll."""
    seen = []
    await _details_client(_page(_selection(_teaser())), seen).list_episodes("a-slug")

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url).startswith("https://api.svt.se/contento/graphql")
    assert json.loads(request.url.params["variables"])["path"] == "/a-slug"


async def test_list_episodes_keeps_svts_own_utc_offset_when_taking_the_date():
    """`validFrom` is offset-aware and SVT publishes at local midnight-ish.
    Normalising to UTC first would move an episode published at 00:30+02:00
    onto the previous day -- and the air date is one of the resolver's two
    signals, so a day's slip is a wrong match or a missed one."""
    page = _page(_selection(_teaser(valid_from="2026-08-23T00:30:00+02:00")))

    eps = await _details_client(page).list_episodes("x")

    assert eps[0].published == date(2026, 8, 23)


async def test_list_episodes_marks_an_upcoming_selection_unavailable():
    """Signal 1: SVT files not-yet-published episodes in their own
    selection. Offering one gets its GUID blocklisted before it airs, and
    the GUID is stable, so the episode is lost permanently rather than
    fetched late."""
    page = _page(
        _selection(
            _teaser(svt_id="egWP26b", overlay={"heading": "Söndag"}),
            selection_type="upcoming",
        )
    )

    eps = await _details_client(page).list_episodes("x")

    assert eps[0].available is False


async def test_list_episodes_marks_a_teaser_with_an_overlay_unavailable():
    """Signal 2, independent of the selection: a non-null `upcomingOverlay`.
    Asserted separately because the two signals are what make availability
    over-determined -- SVT would have to break both at once to hand us an
    unaired episode, where the old regex had exactly one anchor.

    The overlay heading here is a weekday, not "Kommer". Matching that text
    is what missed the *next* episode on the captured page: precisely the
    one a weekly grab asks for."""
    page = _page(_selection(_teaser(overlay={"heading": "Söndag"})))

    eps = await _details_client(page).list_episodes("x")

    assert eps[0].available is False


async def test_list_episodes_never_reports_an_episode_available_on_one_signal():
    """The same episode in two selections: unavailable anywhere is
    unavailable everywhere. Document order would otherwise decide, and
    seasons come before "Kommande", so first-wins resolves the dangerous
    way."""
    page = _page(
        _selection(_teaser()),
        _selection(_teaser(overlay={"heading": "Kommer"}), selection_type="upcoming"),
    )

    eps = await _details_client(page).list_episodes("x")

    assert len(eps) == 1
    assert eps[0].available is False


async def test_list_episodes_excludes_items_that_are_not_episodes():
    """Trailers, singles and linked series share the teaser shape. This is
    the structural equivalent of the old regex's `"__typename":"Episode"`
    anchor -- a field comparison rather than a text match."""
    page = _page(
        _selection(
            _teaser(svt_id="jgWYBgb", typename="Trailer"),
            _teaser(svt_id="jQ7Ex3V", typename="Single"),
            _teaser(),
        )
    )

    eps = await _details_client(page).list_episodes("x")

    assert [e.svt_id for e in eps] == ["KZmQ5JY"]


async def test_list_episodes_raises_when_a_selection_is_truncated():
    """`items` returns a selection whole today -- 119 episodes came back in
    one response, and every selection's length matched its `totalSize`. If
    SVT ever imposes a default cap, a silently short season is a silently
    unreachable episode, which is the failure class this project refuses.
    Loud beats short."""
    page = _page(
        {
            "selectionType": "season",
            "items": [_teaser()],
            "itemsPaginated": {"totalSize": 11},
        }
    )

    with pytest.raises(SvtApiError) as excinfo:
        await _details_client(page).list_episodes("x")
    assert "11" in str(excinfo.value)


async def test_list_episodes_raises_when_the_page_cannot_say_how_many_there_are():
    """A missing `totalSize` is the truncation check going blind, not a
    reason to trust whatever arrived."""
    page = _page({"selectionType": "season", "items": [_teaser()]})

    with pytest.raises(SvtApiError):
        await _details_client(page).list_episodes("x")


async def test_list_episodes_returns_empty_for_a_show_offering_nothing():
    """A real, current answer for a show whose run has ended -- SVT sends
    `associatedContent: []`. It is legitimately empty, which is why the
    canary treats zero episodes as a failure rather than this doing so."""
    eps = await _details_client(_page()).list_episodes("mitt-i-naturen")
    assert eps == []


async def test_list_episodes_unknown_slug_carries_a_404():
    """SVT answers an unknown path with HTTP 200 and a null page. The config
    page's Check control tells "no such slug" from every other SVT failure
    by `status_code == 404`; without this translation that branch goes quiet
    and a typo reports as a generic outage."""

    def handler(request):
        return httpx.Response(200, json={"data": {_alias_of(request): None}})

    with pytest.raises(SvtApiError) as excinfo:
        await _client(handler).list_episodes("no-such-slug")
    assert excinfo.value.status_code == 404


async def test_list_episodes_graphql_error_surfaces_as_svt_api_error():
    """A field that vanishes from SVT's schema comes back as HTTP 200 with
    an `errors` block. That is the whole point of the swap: structural
    breakage is now a named error the canary reports, where the regex
    returned `[]` and said nothing."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "errors": [
                    {"message": "Validation error (FieldUndefined@[detailsPageByPath/heading])"}
                ]
            },
        )

    with pytest.raises(SvtApiError):
        await _client(handler).list_episodes("x")


async def test_list_episodes_stale_cdn_body_is_refused_rather_than_read_as_empty():
    """A cached body belonging to a narrower query with the same path has no
    `associatedContent` at all, so a naive read returns `[]` -- the silent
    empty list, reintroduced through a new door. The alias echo check is
    what closes it."""

    def handler(request):
        return httpx.Response(200, json={"data": {"qsomeoneelse": {}}})

    with pytest.raises(SvtApiError):
        await _client(handler).list_episodes("x")


async def test_list_episodes_http_error_surfaces_as_svt_api_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(SvtApiError):
        await _client(handler).list_episodes("gift-vid-forsta-ogonkastet")


async def test_list_episodes_non_status_failure_has_no_status_code():
    """A transport-level failure is a different kind of failure than the
    null page a nonexistent slug produces, and must not be reported as
    one -- the config page words those two outcomes differently."""

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
