from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from svtplay_arr.api.newznab import build_newznab_router
from svtplay_arr.models import Mapping, Release

REL = Release(
    guid="svtplay-abc123",
    title="Gift vid första ögonkastet - S15E01 - WEBDL-1080p",
    svt_id="KZmQ5JY",
    quality="WEBDL-1080p",
    size_bytes=1_435_287_295,
    published=datetime(2026, 8, 24, tzinfo=timezone.utc),
)

# A second series, deliberately one whose title carries a character
# `naming._sanitise` strips ("?"): the release title says "Vem vet mest"
# while the mapping table says "Vem vet mest?". A query is matched against
# the mapping table's spelling, so both spellings have to find it.
QUIZ = Release(
    guid="svtplay-quiz01",
    title="Vem vet mest - S01E04 - WEBDL-1080p",
    svt_id="Kx8qLm2",
    quality="WEBDL-1080p",
    size_bytes=900_000_000,
    published=datetime(2026, 8, 23, tzinfo=timezone.utc),
)

GIFT_MAPPING = Mapping(
    tvdb_id=288649,
    svt_series_id="jBd1eA9",
    svt_slug="gift-vid-forsta-ogonkastet",
    series_title="Gift vid första ögonkastet",
)
QUIZ_MAPPING = Mapping(
    tvdb_id=331919,
    svt_series_id="jvXBqbQ",
    svt_slug="vem-vet-mest",
    series_title="Vem vet mest?",
)
MAPPINGS = [GIFT_MAPPING, QUIZ_MAPPING]


class FakeMappings:
    def __init__(self, mappings=()):
        self._m = list(mappings)

    def all(self):
        return list(self._m)


class FakeResolver:
    def __init__(self, release, recent=None):
        self._r = release
        self._recent = recent
        self.recent_calls: list[int] = []
        self.resolve_calls: list[tuple[int, int, int]] = []

    async def resolve(self, tvdb_id, season, episode):
        self.resolve_calls.append((tvdb_id, season, episode))
        return self._r

    async def recent(self, within_days, today=None):
        self.recent_calls.append(within_days)
        if self._recent is not None:
            return list(self._recent)
        return [] if self._r is None else [self._r]


def _client(release=REL, rss_window_days=7, recent=None, mappings=MAPPINGS) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_newznab_router(
            FakeResolver(release, recent), rss_window_days, FakeMappings(mappings)
        )
    )
    return TestClient(app)


def _titles(body: str) -> list[str]:
    return [i.find("title").text for i in ET.fromstring(body).findall(".//item")]


def test_caps_advertises_tvdbid():
    # Load-bearing: without tvdbid in supportedParams Sonarr searches by title.
    body = _client().get("/api/?t=caps").text
    root = ET.fromstring(body)
    tv = root.find(".//tv-search")
    assert tv.attrib["available"] == "yes"
    assert "tvdbid" in tv.attrib["supportedParams"]


def test_tvsearch_returns_one_item():
    body = _client().get(
        "/api/", params={"t": "tvsearch", "tvdbid": 288649, "season": 15, "ep": 1}
    ).text
    root = ET.fromstring(body)
    items = root.findall(".//item")
    assert len(items) == 1
    assert items[0].find("title").text == REL.title


def test_tvsearch_returns_empty_channel_when_unresolved():
    body = _client(release=None).get(
        "/api/", params={"t": "tvsearch", "tvdbid": 1, "season": 1, "ep": 1}
    ).text
    assert ET.fromstring(body).findall(".//item") == []


def test_resolver_failure_is_empty_not_500():
    class Boom:
        async def resolve(self, *a):
            raise RuntimeError("svt changed shape")

    app = FastAPI()
    app.include_router(build_newznab_router(Boom(), 7, FakeMappings(MAPPINGS)))
    r = TestClient(app).get(
        "/api/", params={"t": "tvsearch", "tvdbid": 1, "season": 1, "ep": 1}
    )
    assert r.status_code == 200
    assert ET.fromstring(r.text).findall(".//item") == []


def test_nzb_download_is_well_formed_and_carries_svt_id():
    r = _client().get("/api/nzb/svtplay-abc123", params={"svt_id": "KZmQ5JY",
                      "stem": REL.title, "quality": "WEBDL-1080p",
                      "size": REL.size_bytes})
    root = ET.fromstring(r.text)
    metas = {m.attrib["type"]: m.text for m in root.iter()
             if m.tag.endswith("meta")}
    assert metas["svt_id"] == "KZmQ5JY"
    # Size must survive to the download client or the SAB queue shows 0%.
    assert metas["size"] == str(REL.size_bytes)


def test_tvsearch_link_carries_size():
    body = _client().get(
        "/api/", params={"t": "tvsearch", "tvdbid": 288649, "season": 15, "ep": 1}
    ).text
    link = ET.fromstring(body).find(".//item/link").text
    assert f"size={REL.size_bytes}" in link


def test_title_with_xml_special_chars_produces_well_formed_xml():
    # Swedish titles plausibly contain &, <, >, and quotes; an unescaped
    # value would produce XML Sonarr can't parse.
    release = Release(
        guid="svtplay-def456",
        title='Rock & Roll "Special" <Edition> - S01E01 - WEBDL-1080p',
        svt_id="xyz789",
        quality="WEBDL-1080p",
        size_bytes=1000,
        published=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    body = _client(release=release).get(
        "/api/", params={"t": "tvsearch", "tvdbid": 1, "season": 1, "ep": 1}
    ).text
    root = ET.fromstring(body)  # raises ParseError if malformed
    assert root.find(".//item/title").text == release.title


# --- RSS / bare tvsearch ---------------------------------------------------
#
# Sonarr sends a bare `t=tvsearch` (no tvdbid) for BOTH its save-time
# indexer test and every RSS sync, and it REJECTS an indexer whose test
# query returns an empty channel: "Query successful, but no results in the
# configured categories were returned from your indexer." That rejection
# made the indexer impossible to add through the UI at all -- found only by
# deploying against a real Sonarr.


def test_bare_tvsearch_returns_releases_not_an_empty_channel():
    body = _client().get("/api/", params={"t": "tvsearch"}).text
    items = ET.fromstring(body).findall(".//item")
    assert len(items) == 1, "an empty channel here makes Sonarr reject the indexer"
    assert items[0].find("title").text == REL.title


def test_bare_tvsearch_failure_is_an_empty_feed_not_a_500():
    class Boom:
        async def resolve(self, *a):
            raise RuntimeError("nope")

        async def recent(self, *a, **k):
            raise RuntimeError("svt changed shape")

    app = FastAPI()
    app.include_router(build_newznab_router(Boom(), 7, FakeMappings(MAPPINGS)))
    r = TestClient(app).get("/api/", params={"t": "tvsearch"})
    assert r.status_code == 200
    assert ET.fromstring(r.text).findall(".//item") == []


def test_bare_tvsearch_forwards_the_configured_window():
    # A wiring regression here -- wrong value, or the argument dropped --
    # would otherwise be invisible: the feed still returns items either way.
    resolver = FakeResolver(REL)
    app = FastAPI()
    app.include_router(build_newznab_router(resolver, 21, FakeMappings(MAPPINGS)))
    TestClient(app).get("/api/", params={"t": "tvsearch"})
    assert resolver.recent_calls == [21]


# --- Text query (`q`) ------------------------------------------------------
#
# The caps document advertises `q` on both `search` and `tv-search`. Sonarr
# never sends it -- it searches by tvdbid -- but this is a public protocol
# contract, and Prowlarr and hand-rolled clients do. A `q` filters the
# recent-releases feed down to the series whose mapped `series_title`
# matches, case-insensitively, as a substring.
#
# The fix is deliberately in this direction -- make the code honour the
# document rather than narrow the document to the code. Narrowing it means
# editing the one string Sonarr reads to decide how it may query this
# indexer, and nothing here can check that edit against a real Sonarr;
# honouring it leaves that string byte-identical.
#
# Filtering is NOT matching. Every release involved has already come back
# from `resolve()`, so this can only remove items from a feed. It can never
# introduce one, and it cannot produce a title -- and therefore a filename
# -- that a targeted search would not have produced.


def test_query_filters_the_feed_to_the_matching_series():
    body = _client(recent=[REL, QUIZ]).get(
        "/api/", params={"t": "tvsearch", "q": "Vem vet"}
    ).text
    assert _titles(body) == [QUIZ.title]


def test_query_is_case_insensitive():
    body = _client(recent=[REL, QUIZ]).get(
        "/api/", params={"t": "tvsearch", "q": "vEm VeT mEsT"}
    ).text
    assert _titles(body) == [QUIZ.title]


def test_query_matches_the_mapping_tables_spelling_not_the_sanitised_one():
    # "Vem vet mest?" loses its "?" on the way into a release title, because
    # the title is also the filename. Matching against the mapping table
    # rather than the release title is what makes the operator's own
    # spelling -- the one the config page shows them -- find the series.
    body = _client(recent=[REL, QUIZ]).get(
        "/api/", params={"t": "tvsearch", "q": "Vem vet mest?"}
    ).text
    assert _titles(body) == [QUIZ.title]


def test_query_matching_nothing_returns_an_empty_channel():
    # Correct here and *only* here: the client asked for something specific
    # and there is none of it. A bare feed with no q must never do this --
    # see the RSS section above.
    body = _client(recent=[REL, QUIZ]).get(
        "/api/", params={"t": "tvsearch", "q": "Vetenskapens värld"}
    ).text
    assert _titles(body) == []


def test_query_matching_a_mapping_with_nothing_recent_is_an_empty_channel():
    body = _client(recent=[REL]).get(
        "/api/", params={"t": "tvsearch", "q": "Vem vet mest"}
    ).text
    assert _titles(body) == []


def test_blank_query_is_treated_as_no_query_not_as_a_query_matching_nothing():
    # Load-bearing. Clients (Prowlarr among them) send a bare `&q=` on an
    # unfiltered search. Read as a query, that would filter on the empty
    # string -- or worse, match nothing -- the indexer test would see an
    # empty channel, and the indexer would be rejected. That is the exact
    # defect this project already shipped once.
    for blank in ("", "   "):
        body = _client(recent=[REL, QUIZ]).get(
            "/api/", params={"t": "tvsearch", "q": blank}
        ).text
        assert _titles(body) == [REL.title, QUIZ.title], (
            f"q={blank!r} must behave as no q at all"
        )


def test_query_on_t_search_is_honoured_too():
    body = _client(recent=[REL, QUIZ]).get(
        "/api/", params={"t": "search", "q": "Vem vet"}
    ).text
    assert _titles(body) == [QUIZ.title]


def test_bare_t_search_returns_the_feed_not_an_empty_channel():
    # `t=search` is advertised as available="yes", and a client that tests an
    # indexer with an unfiltered search must not be met with the empty
    # channel that makes Sonarr (and Prowlarr) reject it.
    body = _client(recent=[REL, QUIZ]).get("/api/", params={"t": "search"}).text
    assert _titles(body) == [REL.title, QUIZ.title]


def test_a_targeted_search_ignores_q():
    # tvdbid identifies the series exactly; q is a text guess at it. Sonarr
    # sends the series' *English* title in q alongside tvdbid, while the
    # mapping table holds SVT's Swedish one -- filtering the exact answer by
    # that guess would drop correct grabs in production.
    body = _client().get(
        "/api/",
        params={
            "t": "tvsearch",
            "tvdbid": 288649,
            "season": 15,
            "ep": 1,
            "q": "Married at First Sight Sweden",
        },
    ).text
    assert _titles(body) == [REL.title]


def test_an_unsupported_function_is_still_an_empty_channel():
    # `q` does not turn every `t` into a search: only the two functions the
    # caps document advertises as available.
    body = _client(recent=[REL, QUIZ]).get(
        "/api/", params={"t": "music", "q": "Vem vet"}
    ).text
    assert _titles(body) == []


def test_query_failure_is_an_empty_feed_not_a_500():
    class BoomMappings:
        def all(self):
            raise RuntimeError("mappings file vanished mid-request")

    app = FastAPI()
    app.include_router(build_newznab_router(FakeResolver(REL), 7, BoomMappings()))
    r = TestClient(app).get("/api/", params={"t": "tvsearch", "q": "anything"})
    assert r.status_code == 200
    assert ET.fromstring(r.text).findall(".//item") == []


# --- The caps document and the route must agree ----------------------------
#
# The defect this section exists to prevent: caps advertised `q` for a year
# with no `q` handling anywhere in the route. Sonarr was unaffected (it
# searches by tvdbid), so nothing failed -- the protocol contract was simply
# a lie, and only a reader of the code could tell. Every param the caps
# document advertises therefore needs a check here proving the route reads
# it, and adding one to caps without adding its check fails this test.


def _advertised_params(kind: str) -> list[str]:
    root = ET.fromstring(_client().get("/api/?t=caps").text)
    element = root.find(f".//{kind}")
    assert element is not None, f"caps has no <{kind}> element"
    assert element.attrib["available"] == "yes"
    return [p.strip() for p in element.attrib["supportedParams"].split(",") if p.strip()]


def _check_q() -> None:
    body = _client(recent=[REL, QUIZ]).get(
        "/api/", params={"t": "tvsearch", "q": "Vem vet"}
    ).text
    assert _titles(body) == [QUIZ.title], "q was advertised but did not filter"


def _targeted_call() -> tuple[int, int, int]:
    resolver = FakeResolver(REL)
    app = FastAPI()
    app.include_router(build_newznab_router(resolver, 7, FakeMappings(MAPPINGS)))
    TestClient(app).get(
        "/api/", params={"t": "tvsearch", "tvdbid": 11, "season": 22, "ep": 33}
    )
    assert resolver.resolve_calls, "no targeted search reached the resolver"
    return resolver.resolve_calls[0]


def _check_tvdbid() -> None:
    assert _targeted_call()[0] == 11


def _check_season() -> None:
    assert _targeted_call()[1] == 22


def _check_ep() -> None:
    assert _targeted_call()[2] == 33


_PARAM_CHECKS = {
    "q": _check_q,
    "tvdbid": _check_tvdbid,
    "season": _check_season,
    "ep": _check_ep,
}


@pytest.mark.parametrize("kind", ["search", "tv-search"])
def test_every_advertised_param_is_actually_honoured(kind):
    advertised = _advertised_params(kind)
    assert advertised, f"<{kind}> advertises available=yes with no supportedParams"
    for name in advertised:
        assert name in _PARAM_CHECKS, (
            f"caps advertises {name!r} on <{kind}> but nothing here proves the "
            "route reads it -- either implement it or stop advertising it"
        )
        _PARAM_CHECKS[name]()
