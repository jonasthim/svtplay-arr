"""The corroboration gate, and the sweep built on it.

Every test here is about one question: when is svtplay-arr allowed to write
a mapping nobody confirmed? The answer this module encodes is "only when
the series' own episodes say so", and these tests are the whole safety
argument for it.

Two mutations must fail this file, and are named where they would bite:

* loosening the gate to accept a **single** corroborating episode
  (`test_one_matching_episode_is_never_enough`,
  `test_the_short_run_fallback_refuses_a_lone_agreement`)
* loosening it to accept when a **second candidate also corroborates**
  (`test_two_corroborating_candidates_write_nothing`,
  `test_a_partly_agreeing_rival_blocks_the_write`)
"""

import asyncio
from datetime import date, timedelta

import pytest

from svtplay_arr.discovery import (
    ACCEPT_MIN_EPISODES,
    SHORT_RUN_MIN_EPISODES,
    Candidate,
    Evidence,
    corroborate,
    corroborated_match,
    normalise_sonarr_title,
    normalise_title,
    sweep_for_mappings,
)
from svtplay_arr.models import Mapping, SonarrEpisode, SvtEpisode, SvtSearchHit

# A weekly Swedish show: one episode every seven days from this date.
FIRST = date(2026, 8, 3)


def _hit(name, svt_id="abc123", typename="TvSeries"):
    return SvtSearchHit(svt_id=svt_id, name=name, typename=typename)


def _svt_ep(ordinal, published, *, available=True, svt_id=None):
    return SvtEpisode(
        svt_id=svt_id or f"v{ordinal}",
        title=f"Avsnitt {ordinal}",
        url=f"/video/v{ordinal}/show/{ordinal}",
        ordinal=ordinal,
        published=published,
        available=available,
        duration_s=None,
    )


def _sonarr_ep(number, air_date, *, season=1, series_id=1):
    return SonarrEpisode(
        series_id=series_id, season=season, episode=number,
        air_date=air_date, title="",
    )


def _weekly(count, *, start=FIRST, first_ordinal=1, offset_days=0, prefix="v"):
    """`count` SVT episodes, one a week, all available."""
    return [
        _svt_ep(
            first_ordinal + i,
            start + timedelta(days=7 * i + offset_days),
            svt_id=f"{prefix}{first_ordinal + i}",
        )
        for i in range(count)
    ]


def _sonarr_weekly(count, *, start=FIRST, season=1, first=1, series_id=1):
    return [
        _sonarr_ep(first + i, start + timedelta(days=7 * i),
                   season=season, series_id=series_id)
        for i in range(count)
    ]


# --- normalisation, which now only ranks and deduplicates -------------


def test_normalisation_casefolds_and_collapses_whitespace():
    assert normalise_title("  Gift   VID\tförsta ögonkastet ") == (
        "gift vid första ögonkastet"
    )


def test_the_shared_form_never_strips_a_year():
    assert normalise_title("Solsidan (2019)") == "solsidan (2019)"


def test_the_sonarr_form_strips_one_trailing_parenthesised_year():
    assert normalise_sonarr_title("Solsidan (2019)") == "solsidan"


def test_the_sonarr_form_keeps_a_year_that_is_not_trailing():
    assert normalise_sonarr_title("1917 (1917) och sedan") == (
        "1917 (1917) och sedan"
    )


def test_normalisation_does_not_fold_diacritics():
    # It no longer decides anything, but folding å/ä/ö would still rank a
    # genuinely different Swedish show first and spend the run's
    # corroboration budget on it.
    assert normalise_title("Mörka hjärtan") != normalise_title("Morka hjartan")


# --- counting the evidence -------------------------------------------


def test_a_matching_run_is_counted_episode_for_episode():
    got = corroborate(_sonarr_weekly(5), _weekly(5), tolerance_days=1)
    assert (got.matched, got.comparable) == (5, 5)
    assert got.corroborates


def test_a_different_show_at_the_same_ordinals_corroborates_nothing():
    # Same episode numbers, dates months apart: SVT's "Vem vet mest?" run
    # against Sonarr's other programme of the same name.
    other = _weekly(5, start=FIRST + timedelta(days=200))
    got = corroborate(_sonarr_weekly(5), other, tolerance_days=1)
    assert got.matched == 0
    assert got.comparable == 5      # the ordinals exist, the dates do not agree
    assert not got.corroborates


def test_upcoming_svt_episodes_are_not_evidence_and_not_comparable():
    svt = _weekly(2) + [
        _svt_ep(3, FIRST + timedelta(days=14), available=False),
        _svt_ep(4, FIRST + timedelta(days=21), available=False),
    ]
    got = corroborate(_sonarr_weekly(4), svt, tolerance_days=1)
    # Only what SVT has actually published can be compared, so the
    # denominator is 2 and the short-run fallback is what applies.
    assert (got.matched, got.comparable) == (2, 2)


def test_a_sonarr_special_dated_alongside_the_run_is_not_evidence():
    # S00E01 shares the ordinal and the air date with S01E01. Counting it
    # would turn one agreeing episode into two -- inflating a coincidence
    # straight through the threshold.
    sonarr = _sonarr_weekly(3) + [_sonarr_ep(1, FIRST, season=0)]
    got = corroborate(sonarr, _weekly(3), tolerance_days=1)
    assert got.matched == 3


def test_one_svt_episode_claimed_by_two_sonarr_episodes_counts_once_at_most():
    # A double bill: Sonarr lists S01E01 and S01E02 on the same day and SVT
    # published only one episode at ordinal 1. That is one piece of
    # evidence at best, never two.
    sonarr = [_sonarr_ep(1, FIRST), _sonarr_ep(1, FIRST, season=2)]
    got = corroborate(sonarr, [_svt_ep(1, FIRST)], tolerance_days=1)
    assert got.matched == 0


def test_an_ambiguous_sonarr_episode_is_not_counted():
    # Two SVT episodes at ordinal 1 within tolerance: Resolver would refuse
    # to guess between them, so they are not evidence either.
    svt = [_svt_ep(1, FIRST, svt_id="a"), _svt_ep(1, FIRST, svt_id="b")]
    assert corroborate([_sonarr_ep(1, FIRST)], svt, tolerance_days=1).matched == 0


def test_sonarr_episodes_with_no_air_date_are_not_comparable():
    sonarr = [_sonarr_ep(n, None) for n in (1, 2, 3)]
    got = corroborate(sonarr, _weekly(3), tolerance_days=1)
    assert (got.matched, got.comparable) == (0, 0)


def test_nothing_at_all_on_either_side_is_no_evidence_not_agreement():
    assert corroborate([], [], tolerance_days=1) == Evidence(0, 0)
    assert not corroborate([], [], tolerance_days=1).corroborates


@pytest.mark.parametrize("tolerance,drift,expected", [
    (1, 1, True),
    (1, 2, False),
    (3, 2, True),
    (3, 4, False),
    (0, 1, False),
])
def test_corroboration_honours_the_configured_tolerance(tolerance, drift, expected):
    # SVT publishes the whole run `drift` days off Sonarr's air dates. The
    # gate must use the operator's `air_date_tolerance_days`, never a
    # constant of its own -- corroborating at a window the resolver will
    # not later match at is the exact drift this design exists to prevent.
    got = corroborate(
        _sonarr_weekly(4), _weekly(4, offset_days=drift),
        tolerance_days=tolerance,
    )
    assert got.corroborates is expected


# --- the gate --------------------------------------------------------


def _checked(name, matched, comparable, svt_id=None, error=None):
    return Candidate(
        svt_id=svt_id or name, name=name, slug=name.lower(),
        evidence=Evidence(matched=matched, comparable=comparable, error=error),
    )


def test_a_long_run_of_agreement_is_written():
    got = corroborated_match([_checked("Solsidan", 8, 8)])
    assert got is not None and got.name == "Solsidan"


def test_exactly_the_threshold_is_enough():
    assert corroborated_match([_checked("A", ACCEPT_MIN_EPISODES, 9)]) is not None


def test_one_short_of_the_threshold_is_not():
    # The named mutation: accepting `ACCEPT_MIN_EPISODES - 1` here.
    assert corroborated_match([_checked("A", ACCEPT_MIN_EPISODES - 1, 9)]) is None


def test_two_matching_episodes_on_a_long_running_series_are_refused():
    # Eight episodes could have been compared and two agreed. Two weekly
    # shows in the same slot produce exactly this, and it is not evidence
    # that they are the same programme.
    assert corroborated_match([_checked("A", 2, 8)]) is None


def test_one_matching_episode_is_never_enough():
    # Not on a long run...
    assert corroborated_match([_checked("A", 1, 8)]) is None
    # ...and not on a short one either, where "all of them matched" is
    # technically true. One shared air date at ordinal 1 is a coincidence a
    # weekly show produces every week.
    assert corroborated_match([_checked("A", 1, 1)]) is None


def test_the_short_run_fallback_accepts_a_complete_short_run():
    assert corroborated_match([_checked("A", 2, 2)]) is not None
    assert SHORT_RUN_MIN_EPISODES == 2


def test_the_short_run_fallback_refuses_a_lone_agreement():
    # Two episodes to compare, one agreed. "Most of them" is not the rule
    # for a short run; "all of them" is.
    assert corroborated_match([_checked("A", 1, 2)]) is None


def test_no_evidence_at_all_is_never_written():
    # A series Sonarr knows about that has not aired, or that SVT has not
    # published. There is nothing to compare, so there is no confidence.
    assert corroborated_match([_checked("A", 0, 0)]) is None


def test_two_corroborating_candidates_write_nothing():
    # The named mutation: writing the first of them, or the best-scoring
    # one. Two programmes whose episodes both agree is precisely the case a
    # human has to look at.
    assert corroborated_match([_checked("A", 8, 8), _checked("B", 8, 8)]) is None


def test_a_partly_agreeing_rival_blocks_the_write():
    # The rule is not "the best one wins", it is "every other one is
    # refuted". A rival with two agreeing episodes has been out-scored, not
    # ruled out -- and out-scoring is the reasoning that writes permanent
    # filenames for the wrong show.
    assert corroborated_match([_checked("A", 8, 8), _checked("B", 2, 8)]) is None


def test_a_fully_refuted_rival_does_not_block_the_write():
    got = corroborated_match([_checked("A", 8, 8), _checked("B", 0, 8)])
    assert got is not None and got.name == "A"


def test_an_unreadable_candidate_refuses_the_whole_series():
    # An SVT outage while checking one candidate makes that candidate's
    # answer unknown, and an unknown rival is not a refuted rival.
    assert corroborated_match([
        _checked("A", 8, 8),
        _checked("B", 0, 0, error="show page request failed"),
    ]) is None


def test_no_candidates_is_not_a_match():
    assert corroborated_match([]) is None


def test_an_unchecked_candidate_is_never_the_winner():
    assert corroborated_match([Candidate("s1", "Solsidan", "solsidan")]) is None


def test_the_evidence_says_how_many_matched_out_of_how_many():
    # "2 of 8 episodes matched" is far more use to someone deciding than
    # "needs a decision", and that is the entire reason evidence is carried
    # off the gate rather than discarded.
    assert "2 of 8 episodes matched" in Evidence(2, 8).describe()
    assert "3 are needed" in Evidence(2, 8).describe()
    assert "no episodes to compare" in Evidence(0, 0).describe()
    assert "could not be read" in Evidence(error="boom").describe()


# --- the sweep -------------------------------------------------------


class FakeSonarr:
    def __init__(self, series=None, error=None, episodes=None,
                 episodes_error=None):
        self.series = series if series is not None else []
        self.error = error
        # series_id -> [SonarrEpisode]
        self._episodes = episodes or {}
        self.episodes_error = episodes_error
        self.calls = 0
        self.episode_calls: list[int] = []

    async def all_series(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.series

    async def episodes(self, series_id):
        self.episode_calls.append(series_id)
        if self.episodes_error is not None:
            raise self.episodes_error
        return self._episodes.get(series_id, [])


class FakeSvt:
    """Records every request, and how many ran at once."""

    def __init__(self, results=None, error_for=(), delay=0.0, episodes=None,
                 episodes_error_for=()):
        self.results = results or {}
        self.error_for = set(error_for)
        self.delay = delay
        # slug -> [SvtEpisode]
        self.episodes = episodes or {}
        self.episodes_error_for = set(episodes_error_for)
        self.queries: list[str] = []
        self.slugs: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def search_series(self, query):
        self.queries.append(query)
        async with self._counted():
            if query in self.error_for:
                raise RuntimeError("SVT is down")
            return self.results.get(query, [])

    async def list_episodes(self, slug):
        self.slugs.append(slug)
        async with self._counted():
            if slug in self.episodes_error_for:
                raise RuntimeError("show page request failed")
            return self.episodes.get(slug, [])

    def _counted(self):
        svt = self

        class _Counter:
            async def __aenter__(self):
                svt.in_flight += 1
                svt.peak_in_flight = max(svt.peak_in_flight, svt.in_flight)
                if svt.delay:
                    await asyncio.sleep(svt.delay)

            async def __aexit__(self, *exc):
                svt.in_flight -= 1
                return False

        return _Counter()


def _mapping(tvdb_id, svt_series_id="claimed", series_title="Already Mapped"):
    return Mapping(
        tvdb_id=tvdb_id, svt_series_id=svt_series_id,
        svt_slug="slug", series_title=series_title,
    )


async def _sweep(series, results, *, episodes=None, sonarr_episodes=None,
                 svt_episode_errors=(), **kwargs):
    return await sweep_for_mappings(
        FakeSonarr(series, episodes=sonarr_episodes),
        FakeSvt(results, episodes=episodes,
                episodes_error_for=svt_episode_errors),
        existing_mappings=(),
        tolerance_days=1,
        **kwargs,
    )


# --- what the old gate would have got wrong, in both directions -------


async def test_a_title_match_whose_episodes_disagree_is_refused():
    # The case the old gate wrote. SVT's "Vem vet mest?" is a different
    # programme from Sonarr's "Vem vet mest?" -- same name, its own run,
    # months apart -- and the title told us nothing.
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Vem vet mest?"}],
        {"Vem vet mest?": [_hit("Vem vet mest?", "vvm")]},
        episodes={"vem-vet-mest": _weekly(8, start=FIRST + timedelta(days=300))},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    assert sweep.confident == ()
    (p,) = sweep.needs_decision
    # ...and it says exactly how it decided, not merely that it did.
    assert "0 of 8 episodes matched" in p.candidates[0].note()


async def test_a_title_that_differs_entirely_is_written_when_episodes_agree():
    # The case the old gate missed, and the reason this rewrite exists.
    # TVDB carries the English title; SVT has the Swedish one. These
    # strings will never be equal, and the episodes do not care.
    sweep = await _sweep(
        [{
            "id": 1, "tvdbId": 7,
            "title": "Married at First Sight Sweden",
            "alternateTitles": [{"title": "Gift vid första ögonkastet"}],
        }],
        {"Gift vid första ögonkastet": [
            _hit("Gift vid första ögonkastet", "gvfo")
        ]},
        episodes={"gift-vid-forsta-ogonkastet": _weekly(8)},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    (written,) = sweep.confident
    assert written.svt_series_id == "gvfo"
    assert written.svt_slug == "gift-vid-forsta-ogonkastet"
    # The permanent filename is still Sonarr's spelling, not SVT's.
    assert written.series_title == "Married at First Sight Sweden"
    assert written.evidence.matched == 8


async def test_a_title_that_differs_entirely_is_found_without_an_alternate():
    # Even with no alternate title to search by, a search that happens to
    # return the right programme is now decided on its episodes -- the
    # title being unrelated is no longer a veto.
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Married at First Sight Sweden"}],
        {"Married at First Sight Sweden": [
            _hit("Gift vid första ögonkastet", "gvfo")
        ]},
        episodes={"gift-vid-forsta-ogonkastet": _weekly(8)},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    assert [m.svt_series_id for m in sweep.confident] == ["gvfo"]


async def test_two_candidates_both_corroborating_write_nothing():
    run = _weekly(8)
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1"), _hit("Solsidan repris", "s2")]},
        episodes={"solsidan": run, "solsidan-repris": run},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    assert sweep.confident == ()
    (p,) = sweep.needs_decision
    assert "2 SVT programmes corroborate" in p.reason
    assert {c.svt_id for c in p.candidates} == {"s1", "s2"}


async def test_two_matching_episodes_on_a_long_running_series_are_surfaced():
    # Eight comparable, two agreeing: refused, and the page says so in the
    # numbers rather than in the abstract.
    sonarr = _sonarr_weekly(8)
    svt = _weekly(2) + _weekly(6, start=FIRST + timedelta(days=300),
                               first_ordinal=3)
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": svt},
        sonarr_episodes={1: sonarr},
    )

    assert sweep.confident == ()
    (p,) = sweep.needs_decision
    assert "2 of 8 episodes matched" in p.candidates[0].note()


async def test_the_short_run_fallback_writes_a_complete_two_episode_run():
    # SVT has published two episodes of a new season; the rest are
    # upcoming. Refusing this forever is the old gate's failure wearing a
    # new hat, so all-of-a-short-run is accepted -- but never one.
    svt = _weekly(2) + [
        _svt_ep(3, FIRST + timedelta(days=14), available=False),
    ]
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": svt},
        sonarr_episodes={1: _sonarr_weekly(6)},
    )

    (written,) = sweep.confident
    assert (written.evidence.matched, written.evidence.comparable) == (2, 2)


async def test_the_short_run_fallback_refuses_when_one_of_two_disagrees():
    svt = [_svt_ep(1, FIRST), _svt_ep(2, FIRST + timedelta(days=99))]
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": svt},
        sonarr_episodes={1: _sonarr_weekly(6)},
    )

    assert sweep.confident == ()
    assert len(sweep.needs_decision) == 1


async def test_a_single_agreeing_episode_is_never_written():
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": [_svt_ep(1, FIRST)]},
        sonarr_episodes={1: _sonarr_weekly(6)},
    )

    assert sweep.confident == ()
    assert len(sweep.needs_decision) == 1


async def test_a_series_with_nothing_aired_is_surfaced_never_written():
    # Sonarr knows about it; nothing has aired and SVT has published
    # nothing. There is no evidence, so there is no confidence.
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": [
            _svt_ep(1, FIRST, available=False),
            _svt_ep(2, FIRST + timedelta(days=7), available=False),
        ]},
        sonarr_episodes={1: [_sonarr_ep(1, None), _sonarr_ep(2, None)]},
    )

    assert sweep.confident == ()
    (p,) = sweep.needs_decision
    assert "no episodes to compare" in p.reason
    assert "no episodes to compare" in p.candidates[0].note()


async def test_an_svt_outage_while_corroborating_writes_nothing_for_that_series():
    # One candidate's episode list answers and agrees; the other's is
    # unreachable. Writing on the first would be deciding a question nobody
    # answered.
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1"), _hit("Solsidan 2", "s2")]},
        episodes={"solsidan": _weekly(8)},
        sonarr_episodes={1: _sonarr_weekly(8)},
        svt_episode_errors={"solsidan-2"},
    )

    assert sweep.confident == ()
    (p,) = sweep.check_failed
    assert p.tvdb_id == 7
    assert "could not be read" in " ".join(c.note() for c in p.candidates)


async def test_an_svt_outage_on_the_only_candidate_writes_nothing():
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={},
        sonarr_episodes={1: _sonarr_weekly(8)},
        svt_episode_errors={"solsidan"},
    )

    assert sweep.confident == ()
    assert len(sweep.check_failed) == 1


async def test_a_failed_search_beside_a_good_one_never_writes_that_series():
    # Half the candidate set is missing, so "exactly one corroborates and
    # the rest do not" cannot be established for this series at all.
    sonarr = FakeSonarr(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan",
          "alternateTitles": [{"title": "Sunny Side"}]}],
        episodes={1: _sonarr_weekly(8)},
    )
    svt = FakeSvt(
        {"Solsidan": [_hit("Solsidan", "s1")]},
        error_for={"Sunny Side"},
        episodes={"solsidan": _weekly(8)},
    )

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    assert sweep.confident == ()
    (p,) = sweep.check_failed
    assert "may be incomplete" in p.reason


async def test_a_sonarr_episode_outage_is_reported_not_written():
    sonarr = FakeSonarr(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        episodes_error=RuntimeError("episode request failed"),
    )
    svt = FakeSvt({"Solsidan": [_hit("Solsidan", "s1")]},
                  episodes={"solsidan": _weekly(8)})

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    assert sweep.confident == ()
    (p,) = sweep.check_failed
    assert "episode request failed" in p.reason
    # And no episode list was fetched from SVT for a series that could not
    # be compared against anything.
    assert svt.slugs == []


async def test_the_sweep_corroborates_at_the_tolerance_it_is_given():
    # The whole run published two days after Sonarr's air dates. At the
    # default tolerance that is not evidence; at the operator's widened
    # one it is -- and it must be the operator's, because that is the
    # window the resolver will later match at.
    args = dict(
        series=[{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        results={"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": _weekly(8, offset_days=2)},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    tight = await _sweep(**args)
    assert tight.confident == ()

    loose = await sweep_for_mappings(
        FakeSonarr(args["series"], episodes=args["sonarr_episodes"]),
        FakeSvt(args["results"], episodes=args["episodes"]),
        existing_mappings=(), tolerance_days=3,
    )
    assert [m.tvdb_id for m in loose.confident] == [7]


# --- candidate generation --------------------------------------------


async def test_alternate_titles_are_searched_too():
    sonarr = FakeSonarr([{
        "id": 1, "tvdbId": 7, "title": "Solsidan",
        "alternateTitles": [{"title": "Sunny Side"}, {"title": "Solsidan SE"}],
    }])
    svt = FakeSvt({})

    await sweep_for_mappings(sonarr, svt, existing_mappings=(), tolerance_days=1)

    assert svt.queries == ["Solsidan", "Sunny Side", "Solsidan SE"]


async def test_repeated_alternate_titles_cost_one_search():
    # Sonarr repeats the same alternate title once per season for many
    # shows. Each repeat would otherwise be a request to an unofficial API
    # for an answer already held.
    sonarr = FakeSonarr([{
        "id": 1, "tvdbId": 7, "title": "Solsidan",
        "alternateTitles": [
            {"title": "Solsidan"}, {"title": "solsidan  "},
            {"title": "Solsidan (2019)"}, {"title": "Sunny Side"},
        ],
    }])
    svt = FakeSvt({})

    await sweep_for_mappings(sonarr, svt, existing_mappings=(), tolerance_days=1)

    # "Solsidan (2019)" dedupes against "Solsidan" on the Sonarr-side form,
    # which is what that asymmetry is still for.
    assert svt.queries == ["Solsidan", "Sunny Side"]


async def test_the_number_of_searches_per_series_is_capped():
    sonarr = FakeSonarr([{
        "id": 1, "tvdbId": 7, "title": "Solsidan",
        "alternateTitles": [{"title": f"Alt {i}"} for i in range(20)],
    }])
    svt = FakeSvt({})

    await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1,
        queries_per_series=2,
    )

    assert svt.queries == ["Solsidan", "Alt 0"]


async def test_malformed_alternate_titles_do_not_break_the_sweep():
    sonarr = FakeSonarr(
        [{
            "id": 1, "tvdbId": 7, "title": "Solsidan",
            "alternateTitles": ["Sunny Side", {"nope": 1}, None, {"title": ""},
                                {"title": 5}],
        }],
        episodes={1: _sonarr_weekly(8)},
    )
    svt = FakeSvt({"Solsidan": [_hit("Solsidan", "s1")]},
                  episodes={"solsidan": _weekly(8)})

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    assert svt.queries == ["Solsidan", "Sunny Side"]
    assert [m.tvdb_id for m in sweep.confident] == [7]


async def test_only_series_typenames_become_candidates():
    # A search hit for a single video carries the show's name but is not
    # the show; mapping to it would point the resolver's slug at nothing.
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Dokument inifrån"}],
        {"Dokument inifrån": [
            _hit("Dokument inifrån", "d1", typename="Episode")
        ]},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    assert sweep.confident == ()
    assert len(sweep.no_match) == 1


async def test_a_blank_svt_id_or_name_never_becomes_a_candidate():
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", ""), _hit("", "s1")]},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )
    assert sweep.confident == () and len(sweep.no_match) == 1


async def test_the_same_programme_returned_by_two_queries_is_checked_once():
    sonarr = FakeSonarr(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan",
          "alternateTitles": [{"title": "Sunny Side"}]}],
        episodes={1: _sonarr_weekly(8)},
    )
    svt = FakeSvt(
        {"Solsidan": [_hit("Solsidan", "s1")],
         "Sunny Side": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": _weekly(8)},
    )

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    assert svt.slugs == ["solsidan"]
    assert [m.svt_series_id for m in sweep.confident] == ["s1"]


async def test_the_number_of_candidates_corroborated_per_series_is_capped():
    hits = [_hit(f"Show {i}", f"s{i}") for i in range(10)]
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": hits},
        episodes={},
        sonarr_episodes={1: _sonarr_weekly(8)},
        corroborate_per_series=2,
    )

    # Two checked, and the rest still offered -- saying plainly that they
    # were not looked at rather than implying they were refuted.
    (p,) = sweep.needs_decision
    checked = [c for c in p.candidates if c.evidence is not None]
    assert len(checked) == 2
    unchecked = [c for c in p.candidates if c.evidence is None]
    assert len(unchecked) == 8
    assert "not checked" in unchecked[0].note()


async def test_an_exactly_named_candidate_is_corroborated_before_the_rest():
    # Ranking, not gating: if two programmes share a name it is those two
    # whose episodes most need comparing, so the run's budget goes there
    # first.
    hits = [_hit("Something Else", "x1"), _hit("Solsidan", "s1")]
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan (2019)"}],
        {"Solsidan (2019)": hits},
        episodes={"solsidan": _weekly(8)},
        sonarr_episodes={1: _sonarr_weekly(8)},
        corroborate_per_series=1,
    )

    assert [m.svt_series_id for m in sweep.confident] == ["s1"]


# --- the batch-wide rules --------------------------------------------


async def test_the_series_title_comes_from_sonarr_not_from_svt():
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Gift vid första ögonkastet"}],
        {"Gift vid första ögonkastet": [
            _hit("GIFT VID FÖRSTA ÖGONKASTET", "g1")
        ]},
        episodes={"gift-vid-forsta-ogonkastet": _weekly(8)},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    assert sweep.confident[0].series_title == "Gift vid första ögonkastet"


async def test_an_already_mapped_series_is_skipped_without_any_request():
    sonarr = FakeSonarr(
        [
            {"id": 1, "tvdbId": 288649, "title": "Solsidan"},
            {"id": 2, "tvdbId": 999, "title": "Vem vet mest?"},
        ],
        episodes={2: _sonarr_weekly(8)},
    )
    svt = FakeSvt({"Vem vet mest?": [_hit("Vem vet mest?", "v1")]},
                  episodes={"vem-vet-mest": _weekly(8)})

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=[_mapping(288649)], tolerance_days=1
    )

    assert svt.queries == ["Vem vet mest?"]
    assert sonarr.episode_calls == [2]
    assert sweep.already_mapped == 1


async def test_evidence_decides_which_of_two_rival_series_gets_the_programme():
    # The concern this replaces: "first in Sonarr's list wins" was an
    # arbitrary tiebreak. It is now decided by which series' episodes the
    # programme actually agrees with -- and the loser, listed first, gets
    # nothing.
    run = _weekly(8, start=FIRST + timedelta(days=400))
    sonarr = FakeSonarr(
        [
            {"id": 1, "tvdbId": 100, "title": "Vem vet mest?"},
            {"id": 2, "tvdbId": 200, "title": "Vem vet mest? (2021)"},
        ],
        episodes={
            1: _sonarr_weekly(8),                                 # disagrees
            2: _sonarr_weekly(8, start=FIRST + timedelta(days=400)),
        },
    )
    svt = FakeSvt(
        {"Vem vet mest?": [_hit("Vem vet mest?", "vvm")],
         "Vem vet mest? (2021)": [_hit("Vem vet mest?", "vvm")]},
        episodes={"vem-vet-mest": run},
    )

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    assert [m.tvdb_id for m in sweep.confident] == [200]
    assert [p.tvdb_id for p in sweep.needs_decision] == [100]


async def test_two_series_whose_episodes_both_corroborate_share_no_programme():
    # Both agree with the same SVT run -- Sonarr carrying the show twice.
    # The gate cannot see this (it is per-series), so the batch-wide guard
    # is what stops two tvdb ids landing on one slug.
    run = _weekly(8)
    sonarr = FakeSonarr(
        [
            {"id": 1, "tvdbId": 100, "title": "Vem vet mest?"},
            {"id": 2, "tvdbId": 200, "title": "Vem vet mest? (2021)"},
        ],
        episodes={1: _sonarr_weekly(8), 2: _sonarr_weekly(8)},
    )
    svt = FakeSvt(
        {"Vem vet mest?": [_hit("Vem vet mest?", "vvm")],
         "Vem vet mest? (2021)": [_hit("Vem vet mest?", "vvm")]},
        episodes={"vem-vet-mest": run},
    )

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    assert [m.tvdb_id for m in sweep.confident] == [100]
    (p,) = sweep.already_claimed
    assert p.tvdb_id == 200
    assert "Vem vet mest?" in p.reason and "tvdbId 100" in p.reason
    assert [c.svt_id for c in p.candidates] == ["vvm"]
    assert len({m.svt_series_id for m in sweep.confident}) == len(sweep.confident)


async def test_a_programme_already_mapped_by_hand_is_not_claimed_again():
    sonarr = FakeSonarr(
        [{"id": 2, "tvdbId": 200, "title": "Big Brother (2020)"}],
        episodes={2: _sonarr_weekly(8)},
    )
    svt = FakeSvt({"Big Brother (2020)": [_hit("Big Brother", "bb")]},
                  episodes={"big-brother": _weekly(8)})

    sweep = await sweep_for_mappings(
        sonarr, svt,
        existing_mappings=[_mapping(100, "bb", "Big Brother (2019)")],
        tolerance_days=1,
    )

    assert sweep.confident == ()
    (p,) = sweep.already_claimed
    assert "Big Brother (2019)" in p.reason


async def test_no_svt_hits_at_all_is_reported_not_written():
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Nonexistent Show"}], {}
    )

    assert sweep.confident == () and sweep.needs_decision == ()
    (p,) = sweep.no_match
    assert p.tvdb_id == 7 and p.candidates == ()


async def test_one_failed_series_does_not_abort_the_sweep():
    sonarr = FakeSonarr(
        [
            {"id": 1, "tvdbId": 7, "title": "Broken"},
            {"id": 2, "tvdbId": 8, "title": "Solsidan"},
        ],
        episodes={2: _sonarr_weekly(8)},
    )
    svt = FakeSvt({"Solsidan": [_hit("Solsidan", "s1")]},
                  error_for={"Broken"}, episodes={"solsidan": _weekly(8)})

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    assert [m.tvdb_id for m in sweep.confident] == [8]
    (p,) = sweep.search_failed
    assert p.tvdb_id == 7 and "SVT is down" in p.reason


async def test_a_sonarr_outage_is_raised_before_anything_is_proposed():
    sonarr = FakeSonarr(error=RuntimeError("sonarr is down"))
    svt = FakeSvt({})

    with pytest.raises(RuntimeError):
        await sweep_for_mappings(sonarr, svt, existing_mappings=())

    assert svt.queries == []


async def test_malformed_sonarr_records_are_skipped_without_any_request():
    sonarr = FakeSonarr([
        "not a dict",
        {"id": 1, "title": "No TVDB id"},
        {"id": 2, "tvdbId": 5, "title": ""},
        # No Sonarr id: its episodes could never be fetched, so it cannot
        # be corroborated and must not be searched for either.
        {"tvdbId": 6, "title": "No Sonarr id"},
    ])
    svt = FakeSvt({})

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    assert svt.queries == []
    assert sweep.confident == () and sweep.proposals == ()
    assert sweep.skipped_records == 4


# --- what one click costs SVT ----------------------------------------


async def test_the_series_cap_is_reported_rather_than_silently_truncating():
    sonarr = FakeSonarr([
        {"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(10)
    ])
    svt = FakeSvt({})

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1, cap=4
    )

    assert len(svt.queries) == 4
    assert (sweep.searched, sweep.not_searched, sweep.cap) == (4, 6, 4)
    assert sweep.capped is True


async def test_an_unreached_cap_is_not_reported_as_capped():
    sweep = await _sweep([{"id": 1, "tvdbId": 7, "title": "Solsidan"}], {}, cap=4)
    assert sweep.capped is False and sweep.not_searched == 0


async def test_the_request_budget_stops_the_run_and_says_so():
    # A partial sweep reported as a complete one is the failure mode that
    # matters here: the operator concludes the library holds no more
    # mappings when the run simply stopped asking.
    sonarr = FakeSonarr([
        {"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(10)
    ])
    svt = FakeSvt({})

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1,
        concurrency=1, request_budget=3,
    )

    assert len(svt.queries) == 3
    assert sweep.requests_used == 3
    assert sweep.request_budget == 3
    assert sweep.budget_exhausted is True
    assert len(sweep.out_of_budget) == 7
    # ...and "we stopped asking" is a distinct statement from "SVT had
    # nothing for these": only the three actually searched are no_match.
    assert len(sweep.no_match) == 3


async def test_a_run_inside_its_budget_is_not_reported_as_exhausted():
    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": _weekly(8)},
        sonarr_episodes={1: _sonarr_weekly(8)},
        request_budget=50,
    )

    assert sweep.budget_exhausted is False
    # One search plus one episode list: the cost of a corroborated series.
    assert sweep.requests_used == 2


async def test_a_series_the_budget_cut_off_mid_check_is_never_written():
    # The budget ran out between two candidates. The first agreed; the
    # second is unknown, and an unknown rival is not a refuted one.
    sonarr = FakeSonarr(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        episodes={1: _sonarr_weekly(8)},
    )
    svt = FakeSvt(
        {"Solsidan": [_hit("Solsidan", "s1"), _hit("Solsidan repris", "s2")]},
        episodes={"solsidan": _weekly(8), "solsidan-repris": _weekly(8)},
    )

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1,
        concurrency=1, request_budget=2,   # one search, one episode list
    )

    assert sweep.confident == ()
    assert len(sweep.out_of_budget) == 1
    assert sweep.budget_exhausted is True


async def test_concurrency_is_bounded():
    # Every SVT request counts, not just the searches: a library of 300
    # shows must not become 300 simultaneous requests to an unofficial API.
    sonarr = FakeSonarr(
        [{"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(20)],
        episodes={i: _sonarr_weekly(8) for i in range(20)},
    )
    svt = FakeSvt(
        {f"Show {i}": [_hit(f"Show {i}", f"s{i}")] for i in range(20)},
        delay=0.005,
        episodes={f"show-{i}": _weekly(8) for i in range(20)},
    )

    await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1, concurrency=3
    )

    assert svt.peak_in_flight <= 3
    assert len(svt.queries) == 20 and len(svt.slugs) == 20


async def test_the_sweep_never_resolves_a_stream():
    # Reading episode lists is the point now. Resolving a stream is not:
    # that is the download path, and the sweep must not touch it.
    class QualityIsForbidden(FakeSvt):
        async def resolve_quality(self, svt_id):
            raise AssertionError("the sweep must not resolve a stream")

    sonarr = FakeSonarr([{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
                        episodes={1: _sonarr_weekly(8)})
    svt = QualityIsForbidden({"Solsidan": [_hit("Solsidan", "s1")]},
                             episodes={"solsidan": _weekly(8)})

    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    assert len(sweep.confident) == 1


def test_candidate_is_hashable_and_carries_what_a_write_needs():
    c = Candidate(svt_id="s1", name="Solsidan", slug="solsidan")
    assert (c.svt_id, c.name, c.slug) == ("s1", "Solsidan", "solsidan")
    assert {c}  # frozen and hashable, evidence and all
    assert {Candidate("s1", "Solsidan", "solsidan", Evidence(3, 3))}


# --- The CLI's report ------------------------------------------------


async def test_the_cli_rows_are_what_the_page_would_have_written():
    from svtplay_arr.discovery import confident_rows

    sweep = await _sweep(
        [{"id": 1, "tvdbId": 288649, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": _weekly(8)},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    assert confident_rows(sweep) == [{
        "tvdb_id": 288649,
        "svt_series_id": "s1",
        # Derived, never blank: the old CLI emitted "" here and left the
        # operator to transcribe it off an SVT Play URL.
        "svt_slug": "solsidan",
        "series_title": "Solsidan",
        # Marked, so a row pasted from the CLI is the same kind of row the
        # page writes rather than silently passing as hand-confirmed.
        "source": "auto",
    }]


async def test_the_cli_prints_no_row_for_anything_uncorroborated():
    from svtplay_arr.discovery import confident_rows

    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": _weekly(8, start=FIRST + timedelta(days=300))},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    assert confident_rows(sweep) == []


async def test_the_cli_report_names_every_undecided_series_and_its_evidence():
    from svtplay_arr.discovery import format_report

    sweep = await _sweep(
        [
            {"id": 1, "tvdbId": 7, "title": "Vem vet mest?"},
            {"id": 2, "tvdbId": 8, "title": "Not On SVT"},
        ],
        {"Vem vet mest?": [_hit("Vem vet mest? Junior", "j1")]},
        episodes={"vem-vet-mest-junior": _weekly(
            8, start=FIRST + timedelta(days=300)
        )},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    report = format_report(sweep)

    assert "Vem vet mest?" in report and "j1" in report
    # The number is the point: "0 of 8 episodes matched" is what tells the
    # reader this is a different programme, where "needs a decision" does
    # not.
    assert "0 of 8 episodes matched" in report
    assert "NO SVT MATCH" in report and "Not On SVT" in report


async def test_the_cli_report_says_why_a_row_was_written():
    from svtplay_arr.discovery import format_report

    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
        episodes={"solsidan": _weekly(8)},
        sonarr_episodes={1: _sonarr_weekly(8)},
    )

    assert "8 of 8 episodes matched" in format_report(sweep)


async def test_the_cli_report_says_when_the_cap_bit():
    from svtplay_arr.discovery import format_report

    sweep = await _sweep(
        [{"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(5)],
        {},
        cap=2,
    )

    report = format_report(sweep)
    assert "NOT looked at" in report and "3" in report


async def test_the_cli_report_says_when_the_budget_bit():
    from svtplay_arr.discovery import format_report

    sonarr = FakeSonarr([
        {"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(5)
    ])
    sweep = await sweep_for_mappings(
        sonarr, FakeSvt({}), existing_mappings=(), tolerance_days=1,
        concurrency=1, request_budget=2,
    )

    report = format_report(sweep)
    assert "incomplete" in report
    assert "NOT checked" in report


async def test_the_cli_report_names_a_failed_search_separately_from_no_match():
    sonarr = FakeSonarr([{"id": 1, "tvdbId": 7, "title": "Broken"}])
    svt = FakeSvt({}, error_for={"Broken"})
    sweep = await sweep_for_mappings(
        sonarr, svt, existing_mappings=(), tolerance_days=1
    )

    from svtplay_arr.discovery import format_report

    report = format_report(sweep)
    # "SVT had nothing for this" and "SVT could not be asked" are different
    # facts and must not be reported as the same one.
    assert "SEARCH FAILED" in report
    assert "NO SVT MATCH" not in report


def test_the_console_script_points_at_a_callable_that_exists():
    import tomllib

    from svtplay_arr import discovery

    with open("pyproject.toml", "rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]
    assert scripts["svtplay-arr-suggest-mappings"] == "svtplay_arr.discovery:main"
    assert callable(discovery.main)


# --- structural guarantees -------------------------------------------


def test_the_old_first_hit_wins_helper_is_gone():
    # Two implementations of one idea drifting apart is this codebase's
    # most persistent defect. `suggest_mappings` was rewritten out of
    # existence rather than fixed in parallel; nothing may quietly restore
    # a second, laxer matcher beside the gate.
    import svtplay_arr.mappings as mappings_mod

    assert not hasattr(mappings_mod, "suggest_mappings")
    assert not hasattr(mappings_mod, "main")


def test_the_old_title_equality_gate_is_gone():
    # For the same reason. `confident_match` decided on a string
    # comparison; leaving it importable beside the evidence gate invites a
    # caller that still uses it, and a caller that still uses it is the old
    # behaviour restored in one line.
    import svtplay_arr.discovery as discovery_mod

    assert not hasattr(discovery_mod, "confident_match")


def test_the_sweep_cannot_reach_the_matching_or_download_path():
    # A structural version of the rule: this module proposes mappings and
    # nothing else. It shares the *rule* with the resolver (via
    # `svtplay_arr.matching`, which is deliberately not on this list) but
    # must never reach the resolver, the worker, the job store or the
    # naming of a release -- if it ever did, "the sweep only writes
    # mappings" would stop being true by construction and become a thing
    # someone has to remember.
    import ast
    from pathlib import Path

    tree = ast.parse(Path("src/svtplay_arr/discovery.py").read_text("utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    forbidden = {
        "svtplay_arr.resolver",
        "svtplay_arr.worker",
        "svtplay_arr.store",
        "svtplay_arr.downloader",
        "svtplay_arr.naming",
    }
    assert not (imported & forbidden)


def test_the_default_tolerance_is_settings_own_and_not_a_second_copy():
    from svtplay_arr.config import Settings
    from svtplay_arr.discovery import _DEFAULT_TOLERANCE_DAYS

    assert _DEFAULT_TOLERANCE_DAYS is Settings.air_date_tolerance_days
