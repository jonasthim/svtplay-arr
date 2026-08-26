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
