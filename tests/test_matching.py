"""One matching rule, two callers, no second copy.

`svtplay_arr.matching.episode_matches` decides whether an SVT episode is a
given Sonarr episode. `resolver.py` asks it forwards to answer a Sonarr
search; `discovery.py` counts how often it holds to decide whether a whole
series mapping may be written with nobody confirming it. If those two ever
diverged, the sweep could corroborate a mapping on evidence the resolver
then refuses -- or write one on evidence the resolver would never have
accepted.

These tests are the guarantee that they cannot diverge. Two of them are
identity checks (both modules hold the *same object*) and two are
behavioural (each module actually *calls* it, rather than merely importing
it beside its own copy).
"""

from datetime import date

import pytest

from svtplay_arr import discovery as discovery_mod
from svtplay_arr import matching, resolver as resolver_mod
from svtplay_arr.matching import episode_matches
from svtplay_arr.models import SonarrEpisode, SvtEpisode


def _svt(ordinal=1, published=date(2026, 8, 23), available=True):
    return SvtEpisode(
        svt_id=f"e{ordinal}", title=f"Avsnitt {ordinal}",
        url=f"/video/e{ordinal}/show/{ordinal}", ordinal=ordinal,
        published=published, available=available, duration_s=None,
    )


# --- the rule itself -------------------------------------------------


def test_an_available_episode_on_the_same_date_at_the_same_ordinal_matches():
    assert episode_matches(_svt(), date(2026, 8, 23), 1, tolerance_days=1)


def test_an_upcoming_episode_never_matches():
    assert not episode_matches(
        _svt(available=False), date(2026, 8, 23), 1, tolerance_days=1
    )


def test_an_episode_with_no_publication_date_never_matches():
    assert not episode_matches(
        _svt(published=None), date(2026, 8, 23), 1, tolerance_days=1
    )


def test_an_episode_with_no_ordinal_never_matches():
    ep = SvtEpisode(
        svt_id="x", title="Special", url="/video/x", ordinal=None,
        published=date(2026, 8, 23), available=True, duration_s=None,
    )
    assert not episode_matches(ep, date(2026, 8, 23), 1, tolerance_days=1)


def test_a_different_ordinal_never_matches():
    assert not episode_matches(_svt(ordinal=2), date(2026, 8, 23), 1,
                               tolerance_days=1)


@pytest.mark.parametrize("tolerance,offset,expected", [
    (0, 0, True), (0, 1, False),
    (1, 1, True), (1, 2, False),
    (3, 3, True), (3, 4, False),
])
def test_the_date_window_is_the_tolerance_it_is_given(tolerance, offset, expected):
    # The tolerance is a parameter, never a constant in this module: it is
    # `Settings.air_date_tolerance_days`, and a default here would be a
    # second place for an operator-visible setting to live.
    air = date(2026, 8, 23)
    ep = _svt(published=date(2026, 8, 23 + offset))
    assert episode_matches(ep, air, 1, tolerance_days=tolerance) is expected


def test_the_date_window_is_symmetric():
    assert episode_matches(
        _svt(published=date(2026, 8, 22)), date(2026, 8, 23), 1, tolerance_days=1
    )


# --- one implementation ----------------------------------------------


def test_both_callers_hold_the_same_function_object():
    # Not "an equivalent function": the same one. A module that grew its
    # own copy would fail here even if the copy were byte-identical today.
    assert resolver_mod.episode_matches is matching.episode_matches
    assert discovery_mod.episode_matches is matching.episode_matches


def test_no_module_but_matching_does_the_tolerance_arithmetic():
    # The arithmetic that *is* the rule -- `abs((a - b).days) > tolerance`
    # -- may exist in exactly one file. Importing the shared function and
    # then quietly comparing dates beside it would pass the identity test
    # above; this one is what catches that.
    from pathlib import Path

    for name in ("resolver.py", "discovery.py"):
        source = Path("src/svtplay_arr") / name
        assert ".days)" not in source.read_text(encoding="utf-8"), (
            f"{name} does its own air-date arithmetic; the rule lives in "
            "matching.py and there may be only one copy of it"
        )


async def test_the_resolver_actually_calls_the_shared_rule(monkeypatch):
    # Behavioural, not structural: neutering the shared rule must neuter
    # the resolver. A resolver with a private copy would still match.
    from svtplay_arr.resolver import Resolver

    class Mappings:
        def for_tvdb(self, tvdb_id):
            from svtplay_arr.models import Mapping
            return Mapping(tvdb_id=1, svt_series_id="s", svt_slug="show",
                           series_title="Show")

    class Sonarr:
        async def series_id_for_tvdb(self, tvdb_id):
            return 5

        async def episode(self, series_id, season, episode):
            return SonarrEpisode(series_id=5, season=1, episode=1,
                                 air_date=date(2026, 8, 23), title="Ett")

    class Svt:
        async def list_episodes(self, slug):
            return [_svt()]

        async def resolve_quality(self, svt_id):
            from svtplay_arr.models import QualityInfo
            return QualityInfo(label="WEBDL-1080p", height=1080,
                               bitrate_kbps=4000, duration_s=1800)

    r = Resolver(Mappings(), Sonarr(), Svt(), 1)
    assert await r.resolve(1, 1, 1) is not None

    monkeypatch.setattr(resolver_mod, "episode_matches", lambda *a, **k: False)
    assert await r.resolve(1, 1, 1) is None


def test_the_sweeps_corroboration_actually_calls_the_shared_rule(monkeypatch):
    from svtplay_arr.discovery import corroborate

    sonarr_eps = [
        SonarrEpisode(series_id=5, season=1, episode=n,
                      air_date=date(2026, 8, 22 + n), title="")
        for n in (1, 2, 3)
    ]
    svt_eps = [_svt(ordinal=n, published=date(2026, 8, 22 + n))
               for n in (1, 2, 3)]

    assert corroborate(sonarr_eps, svt_eps, tolerance_days=1).matched == 3

    monkeypatch.setattr(discovery_mod, "episode_matches", lambda *a, **k: False)
    assert corroborate(sonarr_eps, svt_eps, tolerance_days=1).matched == 0
