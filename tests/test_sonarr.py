import traceback
from datetime import date
import httpx
import pytest
from svtplay_arr.sonarr import SonarrClient, SonarrApiError, _parse_date

SERIES = [{"id": 70, "tvdbId": 288649, "title": "Gift vid första ögonkastet"}]
EPISODES = [
    {"seasonNumber": 15, "episodeNumber": 1, "airDate": "2026-08-23", "title": "TBA"},
    {"seasonNumber": 15, "episodeNumber": 2, "airDate": "2026-08-23", "title": "TBA"},
    {"seasonNumber": 15, "episodeNumber": 3, "airDate": "2026-08-30", "title": "TBA"},
]


def _client(handler) -> SonarrClient:
    return SonarrClient(
        base_url="http://sonarr.test",
        api_key="k",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _handler(request):
    if request.url.path == "/api/v3/series":
        return httpx.Response(200, json=SERIES)
    if request.url.path == "/api/v3/episode":
        return httpx.Response(200, json=EPISODES)
    return httpx.Response(404)


async def test_series_id_for_tvdb():
    assert await _client(_handler).series_id_for_tvdb(288649) == 70


async def test_series_id_for_unknown_tvdb_is_none():
    assert await _client(_handler).series_id_for_tvdb(999999) is None


async def test_episode_lookup_returns_air_date():
    ep = await _client(_handler).episode(70, 15, 3)
    assert ep.air_date == date(2026, 8, 30)
    assert ep.title == "TBA"


async def test_api_key_is_sent_as_header():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("X-Api-Key")
        return httpx.Response(200, json=SERIES)

    await _client(handler).series_id_for_tvdb(288649)
    assert seen["key"] == "k"


async def test_http_error_on_series_raises_sonarr_api_error():
    def handler(request):
        return httpx.Response(500, json={"error": "Internal Server Error"})

    with pytest.raises(SonarrApiError):
        await _client(handler).series_id_for_tvdb(288649)


async def test_http_error_on_episode_raises_sonarr_api_error():
    def handler(request):
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=SERIES)
        return httpx.Response(500, json={"error": "Internal Server Error"})

    with pytest.raises(SonarrApiError):
        await _client(handler).episode(70, 15, 3)


def test_parse_date_with_empty_date_returns_none():
    assert _parse_date("") is None


def test_parse_date_with_none_returns_none():
    assert _parse_date(None) is None


def test_parse_date_with_malformed_date_returns_none():
    assert _parse_date("not-a-date") is None


async def test_episodes_returns_every_episode_for_the_series():
    eps = await _client(_handler).episodes(70)
    assert [(e.season, e.episode) for e in eps] == [(15, 1), (15, 2), (15, 3)]
    assert eps[2].air_date == date(2026, 8, 30)


async def test_episodes_skips_malformed_entries_without_raising():
    def handler(request):
        return httpx.Response(200, json=[
            "not-a-dict",
            {"seasonNumber": "fifteen", "episodeNumber": 1},   # non-int season
            {"seasonNumber": 15, "episodeNumber": None},       # non-int number
            {"seasonNumber": 15, "episodeNumber": 9, "airDate": "2026-09-06",
             "title": "TBA"},
        ])

    eps = await _client(handler).episodes(70)
    assert [(e.season, e.episode) for e in eps] == [(15, 9)]


async def test_episodes_http_error_surfaces_as_sonarr_api_error():
    def handler(request):
        return httpx.Response(500)

    with pytest.raises(SonarrApiError):
        await _client(handler).episodes(70)


# --- status(): is this Sonarr, does it accept this key, what can it see? ----
#
# The service has always been able to be pointed at the wrong Sonarr, or at
# the right one with a wrong key, and find out only by never grabbing
# anything. These pin the shapes that failure can take, because each one
# sends the operator somewhere different -- and pin that none of them, on
# any path, can carry the API key into whatever renders them.

import socket
import ssl

from svtplay_arr.sonarr import (
    REASON_BAD_URL,
    REASON_CONNECT,
    REASON_HTTP,
    REASON_MESSAGES,
    REASON_NOT_SONARR,
    REASON_REFUSED,
    REASON_TIMEOUT,
    REASON_TLS,
    REASON_UNAUTHORIZED,
    REASON_UNKNOWN,
    REASON_UNREACHABLE,
    SonarrStatus,
)

KEY = "sekrit-sonarr-api-key"
SYSTEM_STATUS = {"version": "4.0.10.2544", "appName": "Sonarr"}


def _keyed_client(handler, base_url: str = "http://sonarr.test") -> SonarrClient:
    return SonarrClient(
        base_url=base_url,
        api_key=KEY,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _status_handler(request):
    if request.url.path == "/api/v3/system/status":
        return httpx.Response(200, json=SYSTEM_STATUS)
    if request.url.path == "/api/v3/series":
        return httpx.Response(200, json=SERIES)
    return httpx.Response(404)


async def test_status_reports_the_version_and_the_series_count():
    # The series count is the fact that separates "something answered" from
    # "the right Sonarr answered": a wrong-but-live Sonarr authenticates
    # perfectly and reports a library the operator will not recognise.
    s = await _keyed_client(_status_handler).status()
    assert isinstance(s, SonarrStatus)
    assert s.version == "4.0.10.2544"
    assert s.series_count == 1


async def test_status_sends_the_key_as_a_header_on_both_requests():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("X-Api-Key")))
        return _status_handler(request)

    await _keyed_client(handler).status()
    assert seen == [
        ("/api/v3/system/status", KEY),
        ("/api/v3/series", KEY),
    ]


async def test_status_reports_an_empty_library_as_zero_not_as_a_failure():
    # A brand-new Sonarr with no series is correctly configured. Reporting
    # that as a failure would send the operator to fix a URL that is right.
    def handler(request):
        if request.url.path == "/api/v3/system/status":
            return httpx.Response(200, json=SYSTEM_STATUS)
        return httpx.Response(200, json=[])

    s = await _keyed_client(handler).status()
    assert s.series_count == 0


async def _reason_of(handler, base_url: str = "http://sonarr.test") -> str:
    with pytest.raises(SonarrApiError) as caught:
        await _keyed_client(handler, base_url).status()
    return caught.value.reason


async def test_a_rejected_key_is_reported_as_rejected_not_as_unreachable():
    # 401 and "nothing is listening" are the two most common outcomes and
    # they need opposite actions: fix the key, or fix the address.
    async def go(code):
        return await _reason_of(lambda request: httpx.Response(code))

    assert await go(401) == REASON_UNAUTHORIZED
    assert await go(403) == REASON_UNAUTHORIZED


async def test_something_that_is_not_sonarr_is_reported_as_such():
    # A reverse proxy, a login page, another *arr on the same port: all of
    # them answer 200 and none of them is Sonarr.
    def html(request):
        return httpx.Response(200, text="<html><body>Sign in</body></html>")

    assert await _reason_of(html) == REASON_NOT_SONARR

    def wrong_json(request):
        return httpx.Response(200, json={"hello": "world"})

    assert await _reason_of(wrong_json) == REASON_NOT_SONARR


async def test_a_sonarr_shaped_status_with_an_unusable_series_list_is_not_sonarr():
    def handler(request):
        if request.url.path == "/api/v3/system/status":
            return httpx.Response(200, json=SYSTEM_STATUS)
        return httpx.Response(200, json={"not": "a list"})

    assert await _reason_of(handler) == REASON_NOT_SONARR


async def test_a_refused_connection_is_told_apart_from_an_unknown_host():
    def refused(request):
        raise httpx.ConnectError(
            "All connection attempts failed", request=request
        ) from ConnectionRefusedError(111, "Connection refused")

    def unresolvable(request):
        raise httpx.ConnectError(
            "[Errno -2] Name or service not known", request=request
        ) from socket.gaierror(-2, "Name or service not known")

    assert await _reason_of(refused) == REASON_REFUSED
    assert await _reason_of(unresolvable) == REASON_UNREACHABLE


async def test_a_tls_failure_is_told_apart_from_a_plain_connection_failure():
    # httpx reports a certificate that will not verify as the same
    # ConnectError class as a refused port; only the chain underneath tells
    # them apart, and the fix ("trust this certificate" vs "check the port")
    # has nothing in common.
    def tls(request):
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
            request=request,
        ) from ssl.SSLCertVerificationError("certificate verify failed")

    assert await _reason_of(tls) == REASON_TLS


async def test_a_timeout_is_reported_as_a_timeout():
    def slow(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    assert await _reason_of(slow) == REASON_TIMEOUT


async def test_a_url_that_is_not_an_http_address_is_reported_as_a_bad_url():
    # The most likely typo of all: pasting "sonarr.lan:8989" out of a
    # browser's address bar. httpx refuses it before any connection is
    # attempted -- as `UnsupportedProtocol` through its real transport, and
    # as a bare ValueError from deeper in the stack through the mock one.
    # Both are the same operator mistake and both have to arrive as a
    # SonarrApiError, because a bare ValueError escaping this client is the
    # 500 the config page is not allowed to produce.
    assert (
        await _reason_of(_status_handler, base_url="sonarr.lan:8989")
        == REASON_BAD_URL
    )

    real = SonarrClient(
        base_url="sonarr.lan:8989", api_key=KEY, http=httpx.AsyncClient()
    )
    with pytest.raises(SonarrApiError) as caught:
        await real.status()
    assert caught.value.reason == REASON_BAD_URL
    assert KEY not in str(caught.value)


async def test_any_other_http_status_is_reported_with_its_code():
    def five_hundred(request):
        return httpx.Response(503)

    with pytest.raises(SonarrApiError) as caught:
        await _keyed_client(five_hundred).status()
    assert caught.value.reason == REASON_HTTP
    assert caught.value.status_code == 503
    assert "503" in str(caught.value)


async def test_every_reason_has_a_message_of_its_own():
    # A reason with no entry would render as the catch-all, i.e. as "we do
    # not know", for a failure we do in fact know the shape of.
    for reason in (
        REASON_BAD_URL, REASON_UNREACHABLE, REASON_REFUSED, REASON_TLS,
        REASON_CONNECT, REASON_TIMEOUT, REASON_UNAUTHORIZED,
        REASON_NOT_SONARR, REASON_HTTP, REASON_UNKNOWN,
    ):
        assert REASON_MESSAGES[reason].strip()
    assert len(set(REASON_MESSAGES.values())) == len(REASON_MESSAGES)


async def test_no_failure_of_any_shape_carries_the_api_key():
    # The constraint the whole feature turns on. httpx puts the *request*
    # on its exceptions, headers and all, so anything that renders an
    # httpx error's own repr -- or the request hanging off it -- leaks the
    # key. Every shape is walked here, not just the one that was easiest to
    # write, because a single unclassified path rendering `str(exc)` from
    # httpx is all it would take.
    def refused(request):
        raise httpx.ConnectError("nope", request=request) from ConnectionRefusedError()

    def unresolvable(request):
        raise httpx.ConnectError("nope", request=request) from socket.gaierror()

    def tls(request):
        raise httpx.ConnectError("nope", request=request) from ssl.SSLError("bad cert")

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def weird(request):
        raise httpx.HTTPError("something httpx has not been taught about")

    handlers = [
        refused, unresolvable, tls, slow, weird,
        lambda request: httpx.Response(401),
        lambda request: httpx.Response(403),
        lambda request: httpx.Response(503),
        lambda request: httpx.Response(200, text="not json"),
        lambda request: httpx.Response(200, json={"no": "version"}),
    ]
    for handler in handlers:
        with pytest.raises(SonarrApiError) as caught:
            await _keyed_client(handler).status()
        exc = caught.value
        assert KEY not in str(exc)
        assert KEY not in repr(exc)
        assert KEY not in "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )

    # ...and the same for a URL httpx refuses outright, which fails before
    # a request object exists at all.
    with pytest.raises(SonarrApiError) as caught:
        await _keyed_client(_status_handler, base_url="sonarr.lan:8989").status()
    assert KEY not in "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )


async def test_the_existing_calls_keep_raising_sonarr_api_error_with_a_reason():
    # all_series/episodes now classify through the same path status() does,
    # so the resolver and the config page keep the one exception type they
    # already catch, and gain the reason for free.
    def dead(request):
        raise httpx.ConnectError("nope", request=request) from ConnectionRefusedError()

    with pytest.raises(SonarrApiError) as caught:
        await _keyed_client(dead).all_series()
    assert caught.value.reason == REASON_REFUSED

    with pytest.raises(SonarrApiError) as caught:
        await _keyed_client(dead).episodes(70)
    assert caught.value.reason == REASON_REFUSED


async def test_a_series_list_that_is_not_json_is_still_a_sonarr_api_error():
    def handler(request):
        return httpx.Response(200, text="<html>nope</html>")

    with pytest.raises(SonarrApiError):
        await _keyed_client(handler).all_series()
