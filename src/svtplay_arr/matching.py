"""The one rule that decides whether an SVT episode *is* a Sonarr episode.

This lives on its own, and in exactly one copy, because two modules now
need it and they need the *same* one:

* `resolver.py` asks it forwards -- "which SVT video is Sonarr's
  S15E03?" -- and the answer becomes a permanent filename in the library,
  because Sonarr runs with renameEpisodes=False.
* `discovery.py` asks it repeatedly -- "how many of this series' episodes
  does this SVT programme agree with?" -- and the answer decides whether a
  whole *series* mapping is written without anyone confirming it, which is
  the same error one level larger.

Those two callers must never drift apart. If the mapping sweep corroborated
under a slightly looser rule than the resolver later matches under, it would
happily write a mapping whose episodes the resolver then refuses -- or,
worse, corroborate on evidence the resolver would have rejected. Two copies
of this rule drifting is this codebase's most reliable source of defects, so
there is one function and both callers import it; `tests/test_matching.py`
pins that they are literally the same object and that each one actually
calls it.

`tolerance_days` is passed in rather than defaulted here on purpose: it is
`Settings.air_date_tolerance_days`, an operator-visible setting, and a
default in this module would be a second place for it to live.
"""

from datetime import date

from svtplay_arr.models import SvtEpisode


def episode_matches(
    ep: SvtEpisode,
    air_date: date,
    episode_number: int,
    *,
    tolerance_days: int,
) -> bool:
    """Does this SVT episode correspond to a Sonarr episode?

    Three signals, all required. Every one exists because of a real trap
    observed in live data on 2026-08-24.
    """
    # Signal 0: it must actually be downloadable. 14 episodes on the
    # same listing page were flagged upcoming (a non-null
    # `upcomingOverlay`), including the *next* one; offering any of them
    # is a guaranteed failed grab, and a failed grab is permanent
    # because the release GUID is stable across searches.
    if not ep.available:
        return False
    # Signal 1: air date agreement, within tolerance.
    if ep.published is None:
        return False
    if abs((ep.published - air_date).days) > tolerance_days:
        return False
    # Signal 2: SVT's own ordinal within its run. NEVER SVT's season
    # number -- SVT labelled a run "Sasong 14" that Sonarr/TVDB call
    # season 15, and two episodes shared an air date (S15E01, S15E02
    # both 2026-08-23), so ordinal is what actually disambiguates.
    if ep.ordinal is None:
        return False
    return ep.ordinal == episode_number
