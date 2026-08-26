from datetime import date
from pathlib import Path

import pytest

from svtplay_arr.svt.parser import _published, parse_show_page

FIXTURE = Path(__file__).parent / "fixtures/svt/show-gvfo-20260824.html"

# The real capture date of the fixture. Pinning `today` to this value keeps
# every fixture-driven date assertion deterministic instead of drifting with
# the "Igår"/"Idag" relative subheadings as real time passes.
CAPTURED = date(2026, 8, 24)


@pytest.fixture
def episodes():
    return parse_show_page(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def episodes_at_capture():
    """Same parse, with `today` pinned to the fixture's capture date so the
    relative "Igår"/"Idag" subheadings resolve deterministically."""
    return parse_show_page(FIXTURE.read_text(encoding="utf-8"), today=CAPTURED)


def test_parses_all_episodes(episodes):
    assert len(episodes) == 27


def test_first_episode_is_read_from_its_own_teaser(episodes_at_capture):
    # Regression: the teaser regex used to join its anchors with a bare
    # `.*?` under re.S, with nothing stopping a match from running past the
    # end of one teaser object into the next. The page has 104 `"heading":"`
    # occurrences but only 43 teasers, so most headings have no `item` of
    # their own and the scan crossed the boundary: match 0 spanned 5,261
    # characters and paired the page's show-hero heading ("Gift vid första
    # ögonkastet", subheading "Nästa avsnitt sön 30 aug") with episode 1's
    # svtId. That produced an SvtEpisode for KZmQ5JY carrying episode 3's
    # air date, no ordinal, and no duration -- so S15E01, the episode this
    # project exists to fetch, resolved to nothing. Every field below is
    # read verbatim from KZmQ5JY's own teaser in the fixture.
    ep = next(e for e in episodes_at_capture if e.svt_id == "KZmQ5JY")
    assert ep.title == "1. Tager du..?"
    assert ep.ordinal == 1
    assert ep.published == date(2026, 8, 23)
    assert ep.duration_s == 58 * 60


def test_excludes_unrelated_related_content(episodes):
    # The page also links /video/jQ7Ex3V/bonuspappor and two other unrelated items.
    assert all("gift-vid-forsta-ogonkastet" in e.url for e in episodes)


def test_marks_upcoming_episodes_unavailable(episodes):
    # 14, not 13: enumerating the fixture, 14 teasers carry a non-null
    # `upcomingOverlay`. The 14th is `egWP26b` below, and the old `== 13`
    # codified the bug rather than the page.
    upcoming = [e for e in episodes if not e.available]
    assert len(upcoming) == 14
    assert any(e.svt_id == "8Dvo3wJ" for e in upcoming)


def test_next_episode_flagged_by_weekday_is_unavailable(episodes):
    # `egWP26b` is the *next* episode, and it sits in the page's
    # "id":"upcoming" / "name":"Kommande" module with a non-null
    # `upcomingOverlay` whose heading is "Söndag", not "Kommer". Matching the
    # literal "Kommer" missed exactly this one, so the resolver offered it
    # for S15E03: Sonarr grabs, the download fails, Sonarr blocklists the
    # GUID -- and because the GUID is deliberately stable across searches,
    # the episode stays blocklisted when it genuinely airs a week later.
    # Permanent loss, every episode, every week.
    ep = next(e for e in episodes if e.svt_id == "egWP26b")
    assert ep.available is False


def test_extracts_ordinal_from_numbered_title(episodes):
    ep = next(e for e in episodes if e.svt_id == "eakXp9m")
    assert ep.ordinal == 2


def test_extracts_ordinal_from_avsnitt_slug(episodes):
    ep = next(e for e in episodes if e.svt_id == "jN3GBby")
    assert ep.ordinal == 6


def test_never_exposes_a_season_number(episodes):
    # SVT labels this run "Sasong 14" while Sonarr calls it S15.
    # SvtEpisode deliberately has no season field.
    assert not hasattr(episodes[0], "season")


def test_relative_yesterday_subheading_resolves_to_a_date():
    # eakXp9m (S15E02) has subheading "Igår 02:00 • 48 min" — SVT's freshest
    # episode is rendered as a relative day, not a day+month pair. This is
    # the episode the whole system exists to fetch right after it airs, so
    # `published` must never be None here.
    eps = parse_show_page(FIXTURE.read_text(encoding="utf-8"), today=CAPTURED)
    ep = next(e for e in eps if e.svt_id == "eakXp9m")
    assert ep.published == date(2026, 8, 23)


def test_day_month_subheading_still_resolves_with_explicit_today():
    eps = parse_show_page(FIXTURE.read_text(encoding="utf-8"), today=CAPTURED)
    ep = next(e for e in eps if e.svt_id == "ja4E6Po")
    assert ep.published == date(2026, 4, 3)


def test_published_rolls_year_forward_for_a_near_term_future_month():
    # Parsed in mid-December, "15 jan" naively lands ~11 months in the past;
    # it is actually an upcoming episode 31 days out, so the year must roll
    # forward to next year.
    assert _published("15 jan", date(2026, 12, 15)) == date(2027, 1, 15)


def test_published_rolls_year_back_for_a_far_future_month():
    # Parsed in mid-January, "20 dec" naively lands ~11 months in the future;
    # it actually aired last December, so the year must roll back (existing
    # behaviour, must not regress).
    assert _published("20 dec", date(2026, 1, 10)) == date(2025, 12, 20)


# --- impossible dates must not take the whole page down --------------------


def test_leap_day_resolves_to_a_leap_year_when_the_reference_year_is_not_one():
    # date(2027, 2, 29) does not exist. Parsed in 2027, the nearest year that
    # does have a 29 February is 2028.
    assert _published("29 feb", date(2027, 6, 1)) == date(2028, 2, 29)


def test_leap_day_uses_the_reference_year_when_it_is_a_leap_year():
    assert _published("29 feb", date(2028, 3, 5)) == date(2028, 2, 29)


def test_leap_day_with_no_valid_adjacent_year_is_simply_unknown():
    # 2025, 2026 and 2027 all lack a 29 February, so there is no defensible
    # date to return -- and None is already how this function says "I don't
    # know", which the resolver treats as a non-match.
    assert _published("29 feb", date(2026, 6, 1)) is None


def test_an_impossible_day_month_pair_is_unknown_rather_than_fatal():
    assert _published("31 apr", date(2026, 6, 1)) is None


def test_a_leap_day_in_one_teaser_does_not_fail_the_whole_page():
    # This is what the crash actually cost: date() raising inside _published
    # aborted parse_show_page for every episode on the page, and leaked a
    # bare ValueError past SvtClient's SvtApiError contract. Built by
    # rewriting one real teaser's subHeading in the captured page, so the
    # rest of the payload stays exactly as SVT served it.
    html = FIXTURE.read_text(encoding="utf-8")
    doctored = html.replace("Igår 02:00 • 58 min", "29 feb 02:00 • 58 min")
    assert doctored != html, "the subHeading to doctor was not found in the fixture"

    episodes = parse_show_page(doctored, today=CAPTURED)

    assert len(episodes) == 27
    ep = next(e for e in episodes if e.svt_id == "KZmQ5JY")
    assert ep.published is None
    assert ep.duration_s == 58 * 60
