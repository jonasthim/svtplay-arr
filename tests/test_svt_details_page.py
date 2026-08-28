"""The episode listing, against real captured SVT responses.

Two things live here.

**A differential.** `svt/parser.py` scraped the SVT Play show page with a
regex over its escaped React flight payload; `client.episodes_from_details_
page` reads the Contento `detailsPageByPath` response instead. On
2026-08-28 both were run live against the same four shows, minutes apart,
and both sides of that comparison are committed: `details-*-20260828.json`
is byte-for-byte what the shipped query received, and `scraped-*-20260828.
json` is the retired scraper's own output for the same show at the same
moment. So the comparison still runs, and still means something, now that
the scraper itself is gone.

The rule the differential enforces is not "the two agree". It is "the two
agree on every field the resolver's matching depends on, and disagree only
in the four specific ways the scraper was wrong". Anything else is a defect
in the migration.

**The page-shape assertions** the retired `test_svt_parser.py` carried, on
the same show, now read off the structured response: the upcoming episodes,
the ordinals, the special with no ordinal at all, and the two episodes
sharing an air date that make the ordinal load-bearing.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from svtplay_arr.svt.client import episodes_from_details_page

FIX = Path(__file__).parent / "fixtures/svt"
CAPTURED = date(2026, 8, 28)

# The four shows the spike compared, chosen to cover the shapes that break
# things: one currently airing with a large upcoming block, one with
# thirteen seasons, one grouped by production period instead of season, and
# one offering nothing at all.
SHOWS = ["gvfo", "husdrommar", "uppdrag-granskning", "mitt-i-naturen"]

# Fields whose value the resolver matches on, or refuses on. These must be
# bit-identical between the two implementations -- that is the entire
# premise of doing the transport swap separately from anything else.
#
# `ordinal` is the one that matters most: it is what disambiguates when SVT
# labels a run "Sasong 14" that TVDB calls season 15 and two episodes share
# an air date, and it is signal 2 of `episode_matches`. It is identical
# here because `_ordinal` was moved across unchanged and applied to the
# GraphQL `heading` and `urls.svtplay`, which carry the same strings the
# page did.
UNCHANGED_FIELDS = ("title", "url", "ordinal", "available")

# Episodes the scraper never produced. `Ky2mZPn` is a real, published,
# available episode of Uppdrag granskning: the scraper's regex matched
# nothing on its teaser and skipped it in silence, leaving a hole in that
# selection's numbering (9, 8, 7, then 5). 60 of 61 still parsed, so the
# canary could not see it either -- an empty list is what the canary
# watches for, and this was never empty.
NEWLY_VISIBLE = {"uppdrag-granskning": {"Ky2mZPn"}}

# Episodes whose publication date changes. SVT's show-page subheadings
# either carry no year at all ("3 apr") or carry one the scraper's regex
# discarded ("26 jun 2025"); either way it re-derived the year from a
# +/-180-day window around today, which is wrong for anything older than
# six months. `validFrom` is an exact timestamp and needs no inference.
REDATED = {"gvfo": 0, "husdrommar": 29, "uppdrag-granskning": 42, "mitt-i-naturen": 0}


def _details(show: str) -> list:
    """Episodes as the shipped client builds them from the captured body.

    The body is stored exactly as SVT sent it, so its `data` block is keyed
    by the random per-request field alias that request happened to use --
    which is the cache-busting nonce as well. Reading the single value out
    of `data` is the honest way to use it.
    """
    body = json.loads(
        (FIX / f"details-{show}-20260828.json").read_text(encoding="utf-8")
    )
    return episodes_from_details_page(next(iter(body["data"].values())))


def _scraped(show: str) -> dict:
    """The retired scraper's own output for the same show, same day."""
    baseline = json.loads(
        (FIX / f"scraped-{show}-20260828.json").read_text(encoding="utf-8")
    )
    return {e["svt_id"]: e for e in baseline["episodes"]}


def _published_iso(episode) -> str | None:
    return episode.published.isoformat() if episode.published else None


# --- the differential -------------------------------------------------------


@pytest.mark.parametrize("show", SHOWS)
def test_graphql_lists_every_episode_the_scraper_did(show):
    scraped = _scraped(show)
    graphql = {e.svt_id: e for e in _details(show)}

    assert set(scraped) - set(graphql) == set(), (
        "GraphQL dropped an episode the scraper found; that is a migration "
        "defect, not an improvement"
    )
    assert set(graphql) - set(scraped) == NEWLY_VISIBLE.get(show, set())


@pytest.mark.parametrize("show", SHOWS)
@pytest.mark.parametrize("field", UNCHANGED_FIELDS)
def test_the_fields_the_resolver_matches_on_are_bit_identical(show, field):
    """The whole point of doing the transport swap on its own.

    `episode_matches` and the two-signal gate are untouched by this change,
    so for any episode both implementations see, they must hand the
    resolver the same thing. A failure here means the migration changed
    what gets grabbed, which is the one outcome it was not allowed to have.
    """
    scraped = _scraped(show)
    graphql = {e.svt_id: e for e in _details(show)}

    differing = {
        svt_id: (scraped[svt_id][field], getattr(graphql[svt_id], field))
        for svt_id in set(scraped) & set(graphql)
        if scraped[svt_id][field] != getattr(graphql[svt_id], field)
    }
    assert differing == {}


@pytest.mark.parametrize("show", SHOWS)
def test_publication_dates_change_only_where_the_scraper_guessed_the_year(show):
    scraped = _scraped(show)
    graphql = {e.svt_id: e for e in _details(show)}
    shared = set(scraped) & set(graphql)

    differing = {
        svt_id
        for svt_id in shared
        if scraped[svt_id]["published"] != _published_iso(graphql[svt_id])
    }
    assert len(differing) == REDATED[show]

    # Every one of them is the *year* moving, never the day or month: the
    # scraper read those correctly off the subheading and only the year was
    # ever inferred. A day-level change would mean `validFrom` is being read
    # wrong -- most likely normalised to UTC, which moves an episode
    # published at 00:30+02:00 onto the previous day.
    for svt_id in differing:
        was, now = scraped[svt_id]["published"], _published_iso(graphql[svt_id])
        assert now is not None, "an episode must not lose its date"
        if was is not None:
            assert was[4:] == now[4:], f"{svt_id}: {was} -> {now} moved more than a year"

    # And nothing loses a date it had, anywhere on the show.
    assert all(e.published is not None for e in graphql.values())


@pytest.mark.parametrize("show", SHOWS)
def test_durations_change_only_by_rounding_or_the_hour_long_bug(show):
    """Two distinct causes, and the second one is not rounding.

    The page renders "58 min", so the scraper was never more precise than
    the minute; `item.duration` is exact seconds. That accounts for every
    difference under a minute.

    The rest are episodes an hour or longer, which the page renders as
    "1 tim 9 min" -- and `(\\d+)\\s*min` reads that as nine minutes. A 69
    minute episode was advertised at 540 seconds, and "1 tim" exactly (no
    minutes remainder) parsed as no duration at all. `resolve_quality`
    prefers the video endpoint's exact `contentDuration`, so this was a
    fallback being wrong rather than every size being wrong, but a size
    estimate off by a factor of seven is a size estimate Sonarr may act on.
    """
    scraped = _scraped(show)
    graphql = {e.svt_id: e for e in _details(show)}

    for svt_id in set(scraped) & set(graphql):
        was, now = scraped[svt_id]["duration_s"], graphql[svt_id].duration_s
        assert now is not None, f"{svt_id}: GraphQL must carry an exact duration"
        if was is None or abs(was - now) >= 60:
            assert now >= 3600, (
                f"{svt_id}: {was} -> {now} is neither rounding nor the "
                "over-an-hour subheading bug; something else changed"
            )


# --- the three behaviour changes, each asserted on purpose ------------------


def test_a_published_episode_the_scraper_missed_is_now_offered():
    """Change 1. `Ky2mZPn` is available and dated, so the resolver can now
    match it -- an episode the feed could not offer yesterday."""
    episode = next(e for e in _details("uppdrag-granskning") if e.svt_id == "Ky2mZPn")

    assert episode.available is True
    assert episode.published == date(2026, 7, 22)
    assert episode.title == '"Kodnamn EA20"'
    assert episode.svt_id not in _scraped("uppdrag-granskning")


def test_back_catalogue_episodes_stop_carrying_an_invented_year():
    """Change 2. The resolver matches an SVT episode to a Sonarr one partly
    on air date, so a wrong year is a silent non-match: back-catalogue
    episodes of a long-running show are simply unreachable today, and
    nothing reports it. These become reachable."""
    graphql = {e.svt_id: e for e in _details("husdrommar")}
    scraped = _scraped("husdrommar")

    # A concrete one, so this fails with the actual dates in the message.
    assert scraped["827rEXR"]["published"] == "2026-09-22"
    assert graphql["827rEXR"].published == date(2025, 9, 22)
    # The scraper put that episode 25 days in the *future* of the capture
    # date, which is also why it never matched anything.
    assert graphql["827rEXR"].published < CAPTURED


def test_uppdrag_granskning_still_has_no_ordinals_and_still_will_not_resolve():
    """Change 3, and it is deliberately *not* a change yet.

    The brief for this migration expected Uppdrag granskning to start
    resolving. It does not, and it must not: that show is grouped by
    production period, its play URLs carry no `/avsnitt-N` and its headings
    no leading number, so `_ordinal` returns None for all 61 -- and
    `episode_matches` signal 2 refuses every one of them, exactly as it
    does today.

    `item.number` is populated for all 61 and would unlock the show. It is
    also populated for specials, where the scraper correctly gave None, and
    `resolver.py::_recent_for` states that as an assumption it relies on.
    Adopting it is a safety decision with its own tests, not a side effect
    of changing transport. This test is the marker that it has not happened
    yet; it is meant to be rewritten by the change that does it.
    """
    episodes = _details("uppdrag-granskning")

    assert len(episodes) == 61
    assert all(e.ordinal is None for e in episodes)


# --- page shape, on the show the whole suite is built around ----------------


@pytest.fixture
def gvfo():
    return _details("gvfo")


def test_lists_every_episode_on_the_page(gvfo):
    assert len(gvfo) == 27


def test_excludes_unrelated_related_content(gvfo):
    assert all("gift-vid-forsta-ogonkastet" in e.url for e in gvfo)


def test_marks_upcoming_episodes_unavailable(gvfo):
    upcoming = [e for e in gvfo if not e.available]
    assert len(upcoming) == 14
    assert any(e.svt_id == "8Dvo3wJ" for e in upcoming)


def test_the_next_episode_flagged_by_weekday_is_unavailable(gvfo):
    """`egWP26b` is the *next* episode. Its overlay heading is the weekday
    "Sondag", not "Kommer", which is why availability is taken from the
    overlay's existence and its selection rather than from its text.
    Offering it means a guaranteed failed grab -- and because release GUIDs
    are stable across searches, Sonarr blocklists that GUID and still has it
    blocklisted when the episode genuinely airs. Permanent loss, not a late
    retry."""
    episode = next(e for e in gvfo if e.svt_id == "egWP26b")
    assert episode.available is False


def test_reads_each_episode_from_its_own_teaser(gvfo):
    """Every field below belongs to KZmQ5JY and to nothing else on the page.

    The scraper's regex could run past the end of one teaser into the next,
    and did: it once paired the page's show-hero heading with this
    episode's id, giving it another episode's air date and no ordinal at
    all -- so S15E01, the episode this project exists to fetch, resolved to
    nothing. There is no equivalent failure available here, because there
    is no scanning: the fields are read out of this teaser's own object.
    """
    episode = next(e for e in gvfo if e.svt_id == "KZmQ5JY")
    assert episode.title == "1. Tager du..?"
    assert episode.ordinal == 1
    assert episode.published == date(2026, 8, 23)
    assert episode.duration_s == 3498


def test_extracts_ordinal_from_a_numbered_heading(gvfo):
    assert next(e for e in gvfo if e.svt_id == "eakXp9m").ordinal == 2


def test_extracts_ordinal_from_an_avsnitt_slug(gvfo):
    assert next(e for e in gvfo if e.svt_id == "jN3GBby").ordinal == 6


def test_a_special_still_has_no_ordinal(gvfo):
    """`item.number` says 1 for this one. `_ordinal` says None, which is
    what the scraper said and what `_recent_for` relies on. See
    `test_uppdrag_granskning_still_has_no_ordinals...` above."""
    special = next(e for e in gvfo if e.svt_id == "KBMY9zX")
    assert special.title == "Gift vid första ögonkastet - Vad hände sen?"
    assert special.ordinal is None


def test_two_episodes_share_an_air_date(gvfo):
    """Which is what makes the ordinal load-bearing rather than
    decorative: on air date alone these two are indistinguishable."""
    same_day = [e for e in gvfo if e.published == date(2026, 8, 23) and e.available]
    assert {e.svt_id for e in same_day} == {"KZmQ5JY", "eakXp9m"}
    assert {e.ordinal for e in same_day} == {1, 2}


def test_never_exposes_a_season_number(gvfo):
    # SVT labels this run "Sasong 14" while Sonarr calls it S15.
    assert not hasattr(gvfo[0], "season")
