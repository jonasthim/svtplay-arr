import json
from datetime import date
from pathlib import Path

from svtplay_arr.models import (
    Mapping, QualityInfo, SonarrEpisode, SvtEpisode,
)
from svtplay_arr.resolver import Resolver
from svtplay_arr.svt.client import episodes_from_details_page

FIXTURE = Path(__file__).parent / "fixtures/svt/details-gvfo-20260828.json"
# The fixture's real capture date. `recent()` takes `today` explicitly, so
# pinning it here keeps the window assertions from drifting with wall-clock
# time.
CAPTURED = date(2026, 8, 28)


def _captured_episodes():
    """The real captured SVT response, through the real client mapping.

    The body is stored as SVT sent it, so its `data` block is keyed by that
    request's field alias.
    """
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return episodes_from_details_page(next(iter(body["data"].values())))

MAP = Mapping(288649, "jpmQD3q", "gift-vid-forsta-ogonkastet",
              "Gift vid första ögonkastet")


class FakeMappings:
    def __init__(self, mapping=MAP):
        self._m = mapping

    def for_tvdb(self, tvdb_id):
        return self._m if self._m and tvdb_id == self._m.tvdb_id else None

    def all(self):
        return [self._m] if self._m else []


class FakeSonarr:
    def __init__(self, episodes):
        self._eps = episodes

    async def series_id_for_tvdb(self, tvdb_id):
        return 70

    async def episode(self, series_id, season, episode):
        return self._eps.get((season, episode))

    async def episodes(self, series_id):
        return list(self._eps.values())


class FakeSvt:
    # Defaults to the real fixture's exact duration (video-KZmQ5JY-*.json
    # has contentDuration: 3498) so the default fixture exercises the
    # exact-duration path; pass quality_duration_s=None to exercise the
    # fallback/nominal paths in resolver tests.
    def __init__(self, episodes, quality_duration_s=3498):
        self._eps = episodes
        self._quality_duration_s = quality_duration_s

    async def list_episodes(self, slug):
        return self._eps

    async def resolve_quality(self, svt_id):
        return QualityInfo("WEBDL-1080p", 1080, 3282, self._quality_duration_s)


SONARR_EPS = {
    (15, 1): SonarrEpisode(70, 15, 1, date(2026, 8, 23), "TBA"),
    (15, 2): SonarrEpisode(70, 15, 2, date(2026, 8, 23), "TBA"),
    (15, 3): SonarrEpisode(70, 15, 3, date(2026, 8, 30), "TBA"),
    (15, 4): SonarrEpisode(70, 15, 4, None, "TBA"),  # unaired, no confirmed date yet
}

SVT_EPS = [
    SvtEpisode("KZmQ5JY", "1 Tager du", "/video/KZmQ5JY/x/1-tager-du", 1,
               date(2026, 8, 23), True, 3499),
    SvtEpisode("eakXp9m", "2. Jag får kämpa", "/video/eakXp9m/x/2-jag", 2,
               date(2026, 8, 23), True, 2899),
    SvtEpisode("egWP26b", "XL: 3. Avslöjandet", "/video/egWP26b/x/3-a", 3,
               date(2026, 8, 30), False, 2880),
]


def _resolver(svt_eps=SVT_EPS, mapping=MAP, quality_duration_s=3498):
    return Resolver(FakeMappings(mapping), FakeSonarr(SONARR_EPS),
                    FakeSvt(svt_eps, quality_duration_s))


async def test_shared_air_date_disambiguated_by_ordinal():
    # S15E01 and S15E02 BOTH air 2026-08-23. Date alone cannot separate them.
    r1 = await _resolver().resolve(288649, 15, 1)
    r2 = await _resolver().resolve(288649, 15, 2)
    assert r1.svt_id == "KZmQ5JY"
    assert r2.svt_id == "eakXp9m"


async def test_svt_season_label_is_never_consulted():
    # SVT calls this run "Sasong 14"; Sonarr calls it 15. Resolving S15E01
    # proves the resolver is not reading SVT's season number.
    assert (await _resolver().resolve(288649, 15, 1)) is not None


async def test_upcoming_episode_is_never_offered():
    assert (await _resolver().resolve(288649, 15, 3)) is None


async def test_unmapped_series_returns_none():
    assert (await _resolver(mapping=None).resolve(999, 1, 1)) is None


async def test_ambiguous_candidates_return_none():
    dupes = [
        SvtEpisode("aaa", "1. A", "/video/aaa/x/1-a", 1, date(2026, 8, 23), True, 100),
        SvtEpisode("bbb", "1. B", "/video/bbb/x/1-b", 1, date(2026, 8, 23), True, 100),
    ]
    assert (await _resolver(svt_eps=dupes).resolve(288649, 15, 1)) is None


async def test_air_date_outside_tolerance_returns_none():
    far = [SvtEpisode("ccc", "1. C", "/video/ccc/x/1-c", 1,
                      date(2026, 7, 1), True, 100)]
    assert (await _resolver(svt_eps=far).resolve(288649, 15, 1)) is None


async def test_release_title_matches_proven_import_format():
    r = await _resolver().resolve(288649, 15, 1)
    assert r.title == "Gift vid första ögonkastet - S15E01 - WEBDL-1080p"


async def test_guid_is_stable_across_calls():
    a = await _resolver().resolve(288649, 15, 1)
    b = await _resolver().resolve(288649, 15, 1)
    assert a.guid == b.guid


async def test_size_uses_exact_video_duration_when_present():
    # quality.duration_s (3498, exact) deliberately differs from the SVT
    # episode's page duration (3499, from the "N min" subheading) so the
    # assertion proves the exact value wins, not just "some number came
    # through."
    r = await _resolver(quality_duration_s=3498).resolve(288649, 15, 1)
    assert r.size_bytes == int(3498 * 3282 * (1000 / 8))


async def test_size_falls_back_to_page_duration_when_exact_missing():
    r = await _resolver(quality_duration_s=None).resolve(288649, 15, 1)
    assert r.size_bytes == int(3499 * 3282 * (1000 / 8))  # SVT_EPS[0].duration_s


async def test_size_uses_nominal_and_warns_when_both_durations_missing(caplog):
    no_duration = [
        SvtEpisode("KZmQ5JY", "1 Tager du", "/video/KZmQ5JY/x/1-tager-du", 1,
                   date(2026, 8, 23), True, None),
    ]
    with caplog.at_level("WARNING"):
        r = await _resolver(
            svt_eps=no_duration, quality_duration_s=None
        ).resolve(288649, 15, 1)
    assert r is not None
    assert r.size_bytes > 0  # never silently 0-byte
    assert any("duration" in rec.message.lower() for rec in caplog.records)


async def test_missing_sonarr_episode_returns_none_and_logs_mismatch(caplog):
    with caplog.at_level("INFO"):
        assert (await _resolver().resolve(288649, 15, 99)) is None
    messages = " ".join(r.message for r in caplog.records)
    assert "no episode" in messages.lower()


async def test_unconfirmed_air_date_returns_none_and_logs_distinctly(caplog):
    with caplog.at_level("INFO"):
        assert (await _resolver().resolve(288649, 15, 4)) is None
    messages = " ".join(r.message for r in caplog.records)
    assert "confirmed air date" in messages.lower()
    assert "no episode" not in messages.lower()


# --- end-to-end guardrail: real parser output into the real resolver -------
#
# Every other resolver test above hand-writes its SvtEpisode list. That is
# how two production defects survived a full unit suite at once: the
# hand-written doubles described the page as the spec said it was, not as the
# captured response actually maps. This test builds its SVT episode list by
# running the real `episodes_from_details_page` over the real captured SVT
# response, so the only fakes left are Sonarr (an external service) and
# quality resolution (a separate HTTP call). If the episode mapping regresses
# in a way that changes what the resolver can match, this fails -- which
# neither a listing-only nor a resolver-only test did.


def _fixture_resolver():
    episodes = _captured_episodes()
    return Resolver(FakeMappings(MAP), FakeSonarr(SONARR_EPS), FakeSvt(episodes))


async def test_real_page_resolves_s15e01_and_s15e02_and_refuses_s15e03():
    # S15E01 and S15E02 share air date 2026-08-23 and are separated only by
    # SVT's ordinal. S15E03 (2026-08-30) exists on the page as `egWP26b` but
    # is flagged upcoming, so it must not be offered at all.
    r1 = await _fixture_resolver().resolve(288649, 15, 1)
    r2 = await _fixture_resolver().resolve(288649, 15, 2)
    r3 = await _fixture_resolver().resolve(288649, 15, 3)

    assert r1 is not None and r1.svt_id == "KZmQ5JY"
    assert r1.title == "Gift vid första ögonkastet - S15E01 - WEBDL-1080p"
    assert r2 is not None and r2.svt_id == "eakXp9m"
    assert r3 is None, "the next episode is upcoming; offering it loses it forever"


# --- RSS / recent feed ------------------------------------------------------
#
# A bare `t=tvsearch` (no tvdbid) is what Sonarr sends BOTH for its
# save-time indexer test and for every RSS sync. Returning an empty channel
# made Sonarr reject the indexer outright ("no results in the configured
# categories"), so it could not be added through the UI at all, and RSS had
# to be left disabled. These tests drive the real parser output, for the same
# reason the guardrail above does.


async def test_recent_offers_available_episodes_and_never_the_upcoming_one():
    releases = await _fixture_resolver().recent(within_days=14, today=CAPTURED)

    by_id = {r.svt_id: r for r in releases}
    assert "KZmQ5JY" in by_id, "S15E01 aired 2026-08-23, inside the window"
    assert "eakXp9m" in by_id, "S15E02 aired 2026-08-23, inside the window"
    assert "egWP26b" not in by_id, (
        "S15E03 is upcoming; offering it gets the GUID blocklisted before it airs"
    )


async def test_recent_titles_match_the_targeted_search_exactly():
    # Same episode reached two ways must be the same release to Sonarr, or
    # blocklisting and grab-dedup stop working.
    targeted = await _fixture_resolver().resolve(288649, 15, 1)
    feed = await _fixture_resolver().recent(within_days=14, today=CAPTURED)

    rss = next(r for r in feed if r.svt_id == "KZmQ5JY")
    assert rss.title == targeted.title
    assert rss.guid == targeted.guid


async def test_recent_excludes_episodes_outside_the_window():
    releases = await _fixture_resolver().recent(within_days=1, today=date(2026, 9, 30))
    assert releases == []


async def test_recent_never_matches_an_svt_ordinal_to_a_sonarr_special():
    # A TVDB special dated alongside the run would otherwise capture the
    # episode: it has episodeNumber 1 and the same air date, so both signals
    # agree. The result is a permanent S00E01 filename for what is really
    # S15E01 -- and renameEpisodes=False means that is not a retry.
    # An SVT ordinal belongs to a numbered run by construction; SVT-side
    # specials come out with ordinal=None and never reach this path at all.
    episodes = _captured_episodes()
    sonarr_eps = {
        (0, 1): SonarrEpisode(70, 0, 1, date(2026, 8, 23), "Bakom kulisserna"),
        (15, 1): SonarrEpisode(70, 15, 1, None, "TBA"),   # not yet dated by TVDB
        (15, 2): SonarrEpisode(70, 15, 2, date(2026, 8, 23), "TBA"),
    }
    r = Resolver(FakeMappings(MAP), FakeSonarr(sonarr_eps), FakeSvt(episodes))

    releases = await r.recent(within_days=14, today=CAPTURED)

    assert all("S00E" not in rel.title for rel in releases), (
        f"an SVT ordinal was matched to a Sonarr special: "
        f"{[rel.title for rel in releases]}"
    )


async def test_recent_keeps_good_releases_when_one_candidate_blows_up():
    # An empty feed is what makes Sonarr reject the indexer, so one bad
    # candidate must not discard the ones that already resolved.
    episodes = _captured_episodes()

    class ExplodingQuality(FakeSvt):
        async def resolve_quality(self, svt_id):
            if svt_id == "eakXp9m":
                raise TypeError("unexpected SVT payload")
            return await super().resolve_quality(svt_id)

    r = Resolver(FakeMappings(MAP), FakeSonarr(SONARR_EPS), ExplodingQuality(episodes))

    releases = await r.recent(within_days=14, today=CAPTURED)

    assert [rel.svt_id for rel in releases] == ["KZmQ5JY"]


async def test_recent_fetches_the_episode_list_once_per_sweep():
    # Each candidate goes through a full resolve(), which re-fetches SVT's
    # episode listing and Sonarr's lists. Sonarr polls RSS every few
    # minutes against an unofficial API, so the sweep memoizes those reads:
    # without it this is 1 + one-per-candidate.
    episodes = _captured_episodes()

    class CountingSvt(FakeSvt):
        listings = 0

        async def list_episodes(self, slug):
            CountingSvt.listings += 1
            return await super().list_episodes(slug)

    class CountingSonarr(FakeSonarr):
        episode_lists = 0

        async def episodes(self, series_id):
            CountingSonarr.episode_lists += 1
            return await super().episodes(series_id)

    r = Resolver(FakeMappings(MAP), CountingSonarr(SONARR_EPS), CountingSvt(episodes))
    releases = await r.recent(within_days=14, today=CAPTURED)

    assert len(releases) == 2, "two candidates, so the counts below are meaningful"
    assert CountingSvt.listings == 1
    assert CountingSonarr.episode_lists == 1
