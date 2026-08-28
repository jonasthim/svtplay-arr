"""Periodic proof that this service's two dependencies still answer.

Two checks live here, and they are deliberately two classes rather than
one.

`SvtCanary` fans out over the operator's own mappings, staggered, and has
to tell "every mapping failed" from "one mapping failed" because those two
findings call for completely different actions. `SonarrCanary` makes one
request to one endpoint and is binary: Sonarr works or it does not.
Generalising the first into something that could also do the second would
mean a class carrying `no_mappings` and `series` states that are
meaningless for Sonarr, and a probe abstraction whose only shared part is
"loop, bound the call, record what happened" -- which is exactly the part
that *is* shared, as `_PeriodicCheck` below.

The alternative -- leaving `SvtCanary` checking Sonarr too -- was rejected
on the plainest possible grounds: the next person to read the name would be
wrong about what the class does.

--- The SVT canary --------------------------------------------------------

Periodic proof that SVT still answers and still lists episodes.

This project's design is "refuse on doubt, return nothing". That is why the
media library is safe -- but it is also what makes failure and idleness
indistinguishable. If SVT changes what it returns, `list_episodes` returns
`[]`, the resolver returns nothing, the feed goes empty, and Sonarr grabs
nothing. Every existing check keeps saying `ok`, because every existing
check is about *this* service: the worker task, the mapping table, the
filesystem, the job store. None of them has ever known whether SVT is
there. The operator finds out weeks later, wondering why no episodes have
appeared.

The episode listing reads an undocumented GraphQL API with no stability
guarantee. That breaking is a *when*, not an *if*, so the one silence this
codebase could not detect was its own. A *structural* break now raises --
SVT answers a vanished field with an `errors` block, which the client turns
into `SvtApiError` and this reports by name. A *semantic* one, where the
response is still valid and no longer means what it did, is as silent as a
drifting regex ever was, and is exactly what the rule below is for.

**The canary is the operator's own mappings, not a hardcoded show.** A
hardcoded slug is a fixture that rots: the show ends, SVT retires the URL,
and the canary reports a failure that is about the fixture rather than the
service. Checking the rows that are actually in mappings.yaml is both a real
signal and directly useful -- it answers "do my mappings still work" as a
side effect, and it is exactly the set of shows whose absence the operator
would notice.

**Zero episodes is a failure, not a success.** This is the single decision
the whole module turns on. An SVT response that changed meaning still
returns HTTP 200; the listing just finds nothing in it. Treating an empty
list
as "the check passed, SVT answered" would make this report `ok` through
precisely the outage it exists to catch.

**Two failure shapes, because they need different actions.**
  - *Every* mapping failing points at SVT, not at any one show: nothing
    will be grabbed until it is fixed, the operator can do nothing about the cause,
    and they must know immediately.
  - *One* mapping failing points at that show -- ended, re-slugged, moved.
    It is fixed by editing one row.
A single boolean cannot carry that, so `status()` reports the counts and
names the failing series.

**Never checked is not healthy.** `STATE_UNKNOWN` is its own state and is
never collapsed into `STATE_OK`. It is not *itself* a degrade (for the first
interval after a restart nothing is known to be wrong, and a check that
cried wolf on every boot would be worth no more than the silence it
replaced) -- but an unknown that never resolves becomes `STATE_STALE`, which
is. Between those two, and the canary task's own liveness which `app.py`
reports alongside this, there is no way for "nothing is checking SVT" to sit
quietly behind a green line.

**State is in memory, and deliberately.** What this reports is "is SVT
answering *now*", which a restart genuinely invalidates -- a success from
before the process died proves nothing about the process that replaced it.
Persisting it would mean a schema change to the job store for a fact with no
value across restarts, and would let a stale success outlive the run that
earned it. So a restart resets to `STATE_UNKNOWN`, which is explicit and
reported as such, rather than implied by a missing field.

**It writes nothing.** Three calls, all read-only GETs:
`SvtClient.list_episodes` -- the same listing `Resolver` and the config
page's Check control already use -- and `SonarrClient.all_series` and
`SonarrClient.episodes`, which the resolver already makes several times an
hour. It never touches the mapping writer, the config writer or the job
store, and there is no Sonarr endpoint in its reach that could write.

**It cannot degrade the service.** Every round is wrapped: a probe that
raises costs that probe, a round that raises costs that round, and
`run_forever` never dies of anything but cancellation. A hanging SVT or a
hanging Sonarr is bounded by a per-probe timeout rather than being allowed
to wedge the loop.

--- The mapping that resolves nothing -------------------------------------

Everything above answers "does SVT still list episodes for this row". A
mapping can pass that on every round of its life and still **resolve
nothing, ever**.

`uppdrag-granskning` is the live example. Correct slug, HTTP 200, 61
episodes -- and `_ordinal` returns None for every one of them, because that
show's titles and play URLs encode no number, so `episode_matches` signal 2
refuses all 61. The show has been mapped, has been returning episodes, and
has never grabbed anything. The cause cannot be fixed from SVT's data; see
`docs/design/2026-08-28-svt-episode-ordinals.md`.

**The SVT half is structurally unable to see this.** It watches for an
empty list or a failed fetch; this returns a full, healthy list that simply
never matches. It was found by accident, in a differential against a
scraper being deleted for unrelated reasons.

So each round asks a second question of each mapping: can its episodes
match *any* of Sonarr's episodes for that series? See `resolvability`.

**The counting is `discovery.corroborate`, not a second copy of it.** That
function already counts how many of Sonarr's episodes have a corresponding
SVT episode under `matching.episode_matches` -- the resolver's own rule, in
its one implementation. That arithmetic now decides two things (whether a
mapping may be written unconfirmed, and whether one already written can
ever work), and two copies of it drifting is this codebase's most
persistent defect class.

**Zero matches is not the finding.** Getting that distinction right is the
whole of this half, because a false alarm here trains the operator to
ignore the one signal that would have caught a real problem -- a lesson
this project has already learned twice. Three shapes produce zero matches
and are not broken: Sonarr has no aired episode yet (a newly added
series), every SVT episode is still upcoming, or Sonarr has no such series
at all. Only "SVT has available episodes, Sonarr has aired episodes, and
no pair of them agrees" is reported, and anything that cannot be placed
there confidently is reported as undetermined instead.

**It says which of the two reasons it is.** No SVT episode carries an
ordinal at all is unfixable and means removing the mapping or accepting
it; ordinals exist and no air date lines up suggests a wrong mapping or a
tolerance too tight. Those send the operator to completely different
places, so `status()` carries the reason as a value and the page renders
its sentence.

**It is amber, not red.** `STATE_UNRESOLVABLE` is in `ATTENTION_STATES`
and not in `DEGRADED_STATES`, exactly like the ended-show case -- see
those two names for the argument. One show being inert does not stop
anything else working, and in the `no_ordinals` case there is nothing to
do about it ever, so a red light over it would be permanent by
construction.

**A Sonarr outage degrades this half and nothing else.** `resolves` goes
to None with a reason, `resolvability_unknown` counts the rows, and the
SVT half's `ok`/`episode_count`/`failing` are untouched. It raises no
alarm of its own, because `SonarrCanary` already turns the top-level light
red for a Sonarr that is not answering and a second alarm for one cause is
the noise this module exists to avoid.

--- The Sonarr check ------------------------------------------------------

`SonarrCanary` closes the same gap on the other dependency, and it is the
more critical of the two: without Sonarr's air dates the resolver cannot
resolve anything at all, so a wrong URL or a rotated key means every search
and every RSS poll silently returns nothing -- while `/health` reported
`ok`, because nothing in this service had ever asked Sonarr a question.

Two recent changes made that worse. The API key is editable through the
configuration page, so it can be mistyped, saved, and only found out about
weeks later; and settings need a restart, so there is a real window in
which the file is right and the running service is not.

**Sonarr down degrades `status`.** Read `DEGRADED_STATES` above before
disagreeing: `STATE_SERIES` is kept out of that set because one dead
mapping row does not stop anything else working, and a light that stays red
over it is a light nobody reads. There is no equivalent here. Sonarr either
answers or nothing can be grabbed at all, which is precisely what a red
light is for.

**The page's Test connection button is not this.** They answer different
questions, and both are worth having: the button tests the values in the
form, before a save, and says whether they would work; this tests what the
running service is actually using, hourly, and says whether it still does.
Between a save and a restart those two are legitimately different answers.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from svtplay_arr.config import Settings
from svtplay_arr.discovery import corroborate
from svtplay_arr.models import SonarrEpisode, SvtEpisode
from svtplay_arr.sonarr import REASON_UNKNOWN, SonarrApiError
from svtplay_arr.svt.client import SvtApiError

log = logging.getLogger(__name__)

# Roughly hourly. With N mappings that is N requests per hour against an
# unofficial API -- see `_SPACING_S` and `_CONCURRENCY` for what keeps that
# from arriving as a burst.
DEFAULT_INTERVAL_S = 3600.0
# Longer than any single probe, shorter than the interval: a round has to
# finish well inside its own period or `STATE_STALE` starts firing.
DEFAULT_PROBE_TIMEOUT_S = 20.0
# In-flight SVT requests, not in-flight rounds.
DEFAULT_CONCURRENCY = 2
# Gap between successive probe starts. The reason a 40-mapping library does
# not open 40 connections to svtplay.se in the same second.
DEFAULT_SPACING_S = 2.0
# Held back from startup on purpose: a restart is already the moment the
# service is busiest (Sonarr re-polls the feed, the worker sweeps
# incomplete/), and the canary has nothing urgent to say in its first
# seconds. It also keeps the check off the network in every test that
# merely starts the app.
DEFAULT_INITIAL_DELAY_S = 30.0

# How many intervals may pass with no completed round before the silence is
# itself the finding. Three, not one: a single slow or skipped round is
# normal, and a check that degrades on one late tick teaches the operator to
# ignore it.
_STALE_INTERVALS = 3
# ...but never sooner than this, however short the configured interval is. A
# round over a large library legitimately takes minutes (`DEFAULT_SPACING_S`
# times the number of mappings, plus whatever SVT is doing), and with a
# one-minute interval three intervals would elapse *during* a round that is
# working perfectly. Staleness means "something is deeply wrong with the
# check itself", so it can afford to be slow; being wrong about it is what it
# cannot afford.
_MIN_STALE_AFTER_S = 900.0
# A floor, so a misconfigured interval of 0 cannot turn `run_forever` into a
# busy loop hammering SVT.
_MIN_INTERVAL_S = 0.05
# How many failing series are named in `status()`. The full per-mapping
# breakdown belongs in the mappings view, which reads `per_mapping()`; the
# headline needs enough to tell a re-slugged show from a broken SVT.
_MAX_REPORTED_FAILURES = 5

# No round has completed since this process started. Not healthy, not yet a
# degrade -- see the module docstring.
STATE_UNKNOWN = "unknown"
# No round has completed for `_STALE_INTERVALS` intervals. Whatever is
# supposed to be checking SVT is not doing it.
STATE_STALE = "stale"
# The last round had nothing to check. A fresh install legitimately has no
# mappings; that is neither a success nor a failure.
STATE_NO_MAPPINGS = "no_mappings"
# The last round resolved every mapping.
STATE_OK = "ok"
# The last round resolved some mappings and not others: those shows.
STATE_SERIES = "series"
# Every mapping resolved on SVT, and at least one of them returns episodes
# that can never match anything Sonarr has. See the module docstring: this
# is the shape the SVT half is structurally unable to see, because from its
# side it is a perfect pass.
STATE_UNRESOLVABLE = "unresolvable"
# The last round resolved none of its mappings: SVT, not any one show.
STATE_SVT = "svt"
# Reading the canary's own state failed. Unknown for an unknown reason.
STATE_UNAVAILABLE = "unavailable"

# The states that turn `/health`'s top-level `status` to "degraded".
#
# `STATE_UNKNOWN` is deliberately absent and `STATE_STALE` deliberately
# present; that pair is the whole "must not read as healthy, must not cry
# wolf" balance.
#
# `STATE_SERIES` is deliberately absent too, and that one is worth stating
# outright, because the obvious choice is the wrong one. A dead row *is* a
# real failure and it *is* the operator's to fix -- but consider what
# actually happens if it holds `status` red: a show ends, SVT retires the
# URL, the operator does not get round to deleting the row, and every
# monitoring setup polling this endpoint has a permanently red check. Within
# a week they stop looking at it. Then the day SVT breaks the listing and the
# state goes to `svt`, the signal built to catch exactly that arrives on a
# channel everyone has already learned to ignore.
#
# This project has shipped that defect once already -- the installer warning
# that fired on 100% of fresh installs, which the docs then taught the reader
# to explain away. A warning meant to prevent a serious failure is worth
# nothing once it is background noise.
#
# So the two shapes get different urgency as well as different words: `svt`
# means nothing will be grabbed and the operator cannot fix the cause, which
# is what a red light is for; `series` means one row is dead, which does not
# stop anything else working and belongs in front of the operator's eyes
# rather than on the machine-readable endpoint's verdict. `/health` still
# *reports* the failing rows either way -- see `failing_series` -- so an
# operator polling it can apply whatever policy they like. This is about
# what turns the light red, not about hiding anything.
DEGRADED_STATES = frozenset({STATE_STALE, STATE_SVT, STATE_UNAVAILABLE})
# ...and the states that must be visible on every rendered surface, which is
# the same set plus `series` and `unresolvable`. The strip and its banner
# key off this; only `/health`'s top-level `status` keys off
# DEGRADED_STATES above.
#
# `STATE_UNRESOLVABLE` sits here for exactly the reason `STATE_SERIES` does,
# and the argument is if anything stronger. It is one show; the rest of the
# feed works; there is nothing urgent to do about it tonight -- and in the
# `no_ordinals` case there is nothing to do about it *ever*, so a red light
# over it would be permanent by construction, which is the precise recipe
# for a light nobody reads. It still has to be loud on the page, because
# the whole point is that this failure is otherwise invisible.
ATTENTION_STATES = DEGRADED_STATES | {STATE_SERIES, STATE_UNRESOLVABLE}

# Why a mapping can never resolve. Two values, because they send the
# operator to completely different places and "this mapping resolves
# nothing" on its own does not say which.
#
# No SVT episode carries an ordinal at all, so `episode_matches` signal 2
# refuses every one of them. Unfixable from here -- the ordinal is derived
# from `/avsnitt-N` in the play URL or a leading `N.` in the heading, and
# shows grouped by production period carry neither. The action is to remove
# the mapping or to accept that it is inert. See
# `docs/design/2026-08-28-svt-episode-ordinals.md`.
UNRESOLVABLE_NO_ORDINALS = "no_ordinals"
# Ordinals exist, and no SVT episode agrees with a Sonarr episode on both
# number and air date. That is a mapping that may well be pointed at the
# wrong programme, or an `air_date_tolerance_days` too tight for how far
# SVT's publication dates sit from Sonarr's air dates. Both are fixable,
# and neither is what the case above means.
UNRESOLVABLE_NO_AIR_DATE = "no_air_date"
# The two together, so a caller asking "is this a finding" cannot drift
# from the list of findings.
UNRESOLVABLE_REASONS = frozenset(
    {UNRESOLVABLE_NO_ORDINALS, UNRESOLVABLE_NO_AIR_DATE}
)

# ...and why nothing could be concluded. These are *not* findings. Each one
# is a shape where zero matches is the expected, healthy answer, and
# alarming on any of them would be the false alarm that teaches an operator
# to ignore the two reasons above.
#
# Nothing SVT lists is downloadable yet -- every episode is flagged
# upcoming. Nothing can match, and nothing being matchable yet is not a
# broken mapping.
UNDETERMINED_NOTHING_AVAILABLE = "nothing_available"
# Sonarr has no aired episode for this series: a newly added series, or one
# whose season is scheduled but has not started. Same argument.
UNDETERMINED_NOTHING_AIRED = "nothing_aired"
# Sonarr's library has no series with this mapping's tvdb id. Nothing can
# match, but the cause is not this mapping's episodes and this check does
# not claim it as one.
UNDETERMINED_NOT_IN_SONARR = "not_in_sonarr"
# Sonarr could not be asked. The one undetermined value that is *reported*
# as a count, because it is the one that would otherwise let a Sonarr
# outage read as a clean sweep.
UNDETERMINED_SONARR_UNAVAILABLE = "sonarr_unavailable"
# Nothing asked. The SVT half failed for this row (so there is no episode
# list to compare), or no Sonarr client was wired in at all.
UNDETERMINED_NOT_CHECKED = "not_checked"

# The last Sonarr check failed: unreachable, unauthenticated, or answering
# like something that is not Sonarr. `SonarrCanary.status()` carries the
# reason. Named to mirror `STATE_SVT` -- the dependency itself is the
# problem, and there is nothing for the operator to fix at the row level
# because there are no rows.
STATE_SONARR = "sonarr"

# What turns `/health`'s top-level `status` red for Sonarr.
#
# `STATE_SONARR` is in it, unlike SVT's `STATE_SERIES`, and the asymmetry is
# the point. `STATE_SERIES` is one dead show out of many: everything else
# still resolves, the operator fixes it when they get to it, and a red light
# that sits there for a week teaches them to stop looking at it. A Sonarr
# that is not answering has no partial version -- nothing is grabbed, no
# search returns anything, and the RSS feed has no air dates to match
# against. That is what a red light is for.
#
# `STATE_UNKNOWN` is absent here for exactly the reason it is absent above:
# the first interval after a restart knows nothing, and a check that
# degrades on every boot is one nobody reads. It is still never reported as
# `ok` -- it is its own state, it renders as its own chip, and an unknown
# that never resolves becomes `STATE_STALE`, which is in this set.
SONARR_DEGRADED_STATES = frozenset(
    {STATE_STALE, STATE_SONARR, STATE_UNAVAILABLE}
)
# The same set, and that is the whole difference from SVT: there is no
# "worth your attention but not worth an alert" shape here, because Sonarr
# has no equivalent of one show having ended.
SONARR_ATTENTION_STATES = SONARR_DEGRADED_STATES

# The Sonarr check's own loop constants. Not shared with SVT's, and not
# offered as a setting either.
#
# `svt_canary_interval_minutes` exists because SVT's is an undocumented API
# this project has no right to hammer, and an operator on a metered or
# rate-limited connection needs a way to back off without a code change.
# None of that applies to Sonarr: it is the operator's own service, usually
# on the same host, and the resolver already calls `all_series()` on every
# RSS poll -- several times an hour. One more request per hour is not a knob
# worth adding to config.yaml.
DEFAULT_SONARR_INTERVAL_S = 3600.0
# Shorter than SVT's 30s. A mistyped key saved through the settings page is
# discovered on the restart that applies it, and the whole value of this
# check is that the operator learns then rather than from episodes that
# never arrive. One request to a service that is usually on the same host
# does not need to be held back from startup the way a fan-out over SVT
# does.
DEFAULT_SONARR_INITIAL_DELAY_S = 5.0
# Well inside `DEFAULT_SONARR_INTERVAL_S`, and long enough that a Sonarr
# doing a library refresh is not reported as down.
DEFAULT_SONARR_TIMEOUT_S = 15.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Resolvability:
    """Whether one mapping's episodes can match any of Sonarr's, and why.

    `resolves` is tri-state and the three values are three different
    claims, not two plus a fallback:

    * `True`  -- at least one SVT episode agrees with exactly one Sonarr
      episode under `matching.episode_matches`. This mapping can resolve.
    * `False` -- both sides have real content and no pair of them agrees.
      This mapping resolves nothing and will not start on its own. The one
      finding this whole computation exists to produce.
    * `None`  -- nothing could be concluded. Never rendered as either of
      the above, because "not checked" reading as healthy is the defect
      this module exists to remove, one level in.

    `reason` is always set except on `True`: one of `UNRESOLVABLE_REASONS`
    when `resolves` is False, one of the `UNDETERMINED_*` values when it is
    None. A caller never has to distinguish "no reason" from "a reason of
    None".

    `note` is the same fact as a sentence for the operator, and it is what
    the page and `/health` render. It never contains anything but this
    module's own words, the mapping's slug, counts, and -- for a Sonarr
    failure -- one of `sonarr.REASON_MESSAGES`, which are fixed literals.
    The API key cannot reach it.
    """

    resolves: bool | None = None
    reason: str | None = None
    note: str | None = None

    @property
    def is_finding(self) -> bool:
        return self.reason in UNRESOLVABLE_REASONS


# Nothing was asked, and nothing is claimed. Shared rather than rebuilt at
# each of its five sites so the "not checked" verdict has one spelling.
_NOT_CHECKED = Resolvability(None, UNDETERMINED_NOT_CHECKED)


def resolvability(
    svt_episodes,
    sonarr_episodes,
    *,
    slug: str,
    tolerance_days: int,
    today: date,
) -> Resolvability:
    """Can this mapping's episodes match any of Sonarr's? Pure; never raises.

    **The match counting is not done here.** It is `discovery.corroborate`,
    which counts strictly one-to-one agreements under
    `matching.episode_matches` -- the resolver's own rule, in its one
    implementation. A second copy of that arithmetic drifting from the
    first is this codebase's most persistent defect class, and it now
    decides two different things: whether a mapping may be *written*
    unconfirmed, and whether one already written can ever work. Those must
    be the same question asked twice, not two questions that resemble each
    other.

    `corroborate`'s numerator is the right bar for this and not merely a
    convenient one. It requires an SVT episode and a Sonarr episode that
    each have exactly one partner; where that fails the resolver refuses
    too, in both directions (`Resolver.resolve` and `_recent_for` both give
    up on `len(candidates) != 1`). So `matched == 0` really does mean
    "nothing here resolves", rather than "nothing here resolves under a
    stricter rule than the one that runs".

    Everything before that call is exclusions, and getting them right is
    the whole job. Zero matches is the *expected* answer in three
    situations that are not broken, and a check that cried wolf on any of
    them would train the operator to ignore the one signal that catches a
    real problem -- a lesson this project has already learned twice, in the
    installer's fresh-install warning and in the canary's own ended-show
    case. Anything that cannot be placed confidently in the third category
    below is reported as undetermined, never as a finding.

    `today` is passed in rather than read here, so this stays pure and the
    caller's one clock read covers the whole round. It is used for exactly
    one thing: telling a series Sonarr has scheduled from one it has aired.
    Note that `corroborate` itself is, and remains, clock-free -- see
    `Evidence`'s docstring for why a wall-clock reading inside a gate that
    *writes* was a safety hole. Here the direction of harm is reversed: a
    clock behind under-counts what has aired and so raises fewer alarms,
    and the worst a clock ahead can do is show one warning too many on a
    page, which the next round corrects.
    """
    listed = [
        e for e in svt_episodes or () if isinstance(e, SvtEpisode) and e.available
    ]
    if not listed:
        # Exclusion 1: nothing SVT lists is downloadable yet. A show
        # between seasons, or one whose next run is announced and not out.
        return Resolvability(
            None,
            UNDETERMINED_NOTHING_AVAILABLE,
            f"Nothing to compare yet: every episode SVT lists for {slug!r} is "
            "still upcoming, so none of them is downloadable.",
        )

    # Season 0 excluded for the same reason `corroborate` and
    # `Resolver._recent_for` exclude it: a Sonarr special dated alongside
    # the run is not evidence about the run.
    aired = [
        se
        for se in sonarr_episodes or ()
        if isinstance(se, SonarrEpisode)
        and se.season > 0
        and se.air_date is not None
        and se.air_date <= today
    ]
    if not aired:
        # Exclusion 2: a newly added series, or one whose season is
        # scheduled but has not started. Sonarr dates a whole season from
        # the day it is announced, so "has episodes" is not "has aired".
        return Resolvability(
            None,
            UNDETERMINED_NOTHING_AIRED,
            "Nothing to compare yet: Sonarr has no aired episode for this "
            "series.",
        )

    evidence = corroborate(
        sonarr_episodes, svt_episodes, tolerance_days=tolerance_days
    )
    if evidence.matched > 0:
        return Resolvability(
            True,
            None,
            f"{evidence.matched} of Sonarr's aired episodes have a matching "
            f"episode on SVT.",
        )

    # Both sides have real content and nothing agrees. The mapping is
    # valid, SVT answers, the episode list is full -- and it has never
    # grabbed anything and never will until something changes.
    if not any(e.ordinal is not None for e in listed):
        return Resolvability(
            False,
            UNRESOLVABLE_NO_ORDINALS,
            f"This mapping can never match anything. SVT lists "
            f"{len(listed)} available episode"
            f"{'' if len(listed) == 1 else 's'} for {slug!r} and not one of "
            "them carries an episode number, so every one of them is refused "
            "before any date is compared. Nothing about this show or this "
            "mapping can fix it: either remove the row, or keep it knowing "
            "it will never grab anything.",
        )
    return Resolvability(
        False,
        UNRESOLVABLE_NO_AIR_DATE,
        f"This mapping can never match anything as it stands. SVT's episodes "
        f"for {slug!r} carry episode numbers, and none of them agrees with a "
        f"Sonarr episode on both number and air date within "
        f"{tolerance_days} day{'' if tolerance_days == 1 else 's'}. Check "
        "that this row points at the right programme, and at the air dates "
        "Sonarr holds for it.",
    )


def _iso(when: datetime | None) -> str | None:
    return None if when is None else when.isoformat()


@dataclass(frozen=True)
class MappingHealth:
    """What is known about one mapping's last check.

    `ok` is tri-state on purpose. `None` is "not checked since this process
    started", which is not the same claim as `False`, and rendering the two
    the same way is the defect this module exists to avoid one level up.

    `last_success` and `episode_count` describe the last check that
    *worked*, not the last check. "Worked an hour ago, failing now" and
    "never worked" call for different actions from the operator, and only
    keeping both timestamps can tell them apart.

    `resolves`, `unresolvable_reason` and `resolvability_note` are the
    other half of the check -- see `Resolvability` -- and they follow the
    opposite rule to `last_success`: they always describe *this* round and
    are cleared when this round could not determine them. A high-water
    mark is right for "did SVT ever answer"; it would be wrong here,
    because a stale `False` kept across a Sonarr outage would go on
    accusing a mapping nothing has looked at.
    """

    tvdb_id: int
    series_title: str
    svt_slug: str
    ok: bool | None = None
    last_checked: datetime | None = None
    last_success: datetime | None = None
    episode_count: int | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    resolves: bool | None = None
    unresolvable_reason: str | None = None
    resolvability_note: str | None = None

    def as_dict(self, now: datetime) -> dict:
        """This row's state, with both an instant and an age per timestamp.

        The ages are the reason `now` is a parameter rather than read in
        here. "When did this last work" is asked in the present tense, and
        an ISO instant makes the reader hold the current time in their
        head, work out the timezone and subtract -- every glance. The
        rendered surfaces show the age; the instant stays alongside it for
        the cases where the exact moment matters (the mappings table hangs
        it off a `title`).

        Computed with the same `_age_s` `status()` uses, off the same
        clock, so the mappings table and the status strip cannot disagree
        about how old the same moment is. Formatting the number into words
        is the templates' business and is likewise done in exactly one
        place -- see `_age.html`.
        """
        return {
            "tvdb_id": self.tvdb_id,
            "series_title": self.series_title,
            "svt_slug": self.svt_slug,
            "ok": self.ok,
            "last_checked": _iso(self.last_checked),
            "last_checked_age_s": _age_s(self.last_checked, now),
            "last_success": _iso(self.last_success),
            "last_success_age_s": _age_s(self.last_success, now),
            "episode_count": self.episode_count,
            "last_error": self.last_error,
            "last_error_at": _iso(self.last_error_at),
            "last_error_age_s": _age_s(self.last_error_at, now),
            # Tri-state, and rendered as three things. `None` here is "not
            # determined this round", which is not the same claim as
            # "matches nothing" and must never be shown as "fine".
            "resolves": self.resolves,
            "unresolvable_reason": self.unresolvable_reason,
            "resolvability_note": self.resolvability_note,
        }


def unavailable_status() -> dict:
    """What `app.compute_health` reports when `status()` itself failed.

    Degraded, not unknown-and-calm: the canary not being readable is not the
    same as the canary having nothing to say yet, and the difference matters
    because only one of them resolves on its own.
    """
    return {
        "state": STATE_UNAVAILABLE,
        "degraded": True,
        "needs_attention": True,
        "last_checked_age_s": None,
        "last_success_age_s": None,
        "checked": None,
        "failing": None,
        "episodes_seen": None,
        "last_checked": None,
        "last_success": None,
        "last_error": None,
        "last_error_at": None,
        "failing_series": [],
        "failing_series_truncated": False,
        # None, not 0: "no mapping was found unresolvable" and "nothing is
        # known about any mapping" are different claims, and only one of
        # them is reassuring.
        "unresolvable": None,
        "unresolvable_series": [],
        "unresolvable_series_truncated": False,
        "resolvability_unknown": None,
        "resolvability_error": None,
    }


class _PeriodicCheck:
    """The loop, the clock and the staleness rule both checks share.

    Extracted rather than copied, because this is the part that must not be
    allowed to differ between them: a monitoring component that can kill its
    own task replaces one silent failure with another, and two copies of
    "never dies of anything but cancellation" is two chances to get that
    wrong. What each check actually *does* -- one endpoint or a fan-out over
    mappings, one failure shape or two -- lives in the subclass, which is
    the whole reason there are two subclasses.

    `_WHAT` names the check in log lines, so a message never says "SVT"
    about a Sonarr round.
    """

    _WHAT = "check"

    def __init__(
        self, *, interval_s: float, probe_timeout_s: float,
        initial_delay_s: float, clock,
    ):
        self._interval = max(float(interval_s), _MIN_INTERVAL_S)
        self._probe_timeout = float(probe_timeout_s)
        self._initial_delay = max(float(initial_delay_s), 0.0)
        self._now = clock

        # Startup is the reference staleness is measured from until a round
        # completes. Without it a check that never manages a single round
        # would sit at "unknown" forever, which is the one way this feature
        # could reproduce the silence it exists to remove.
        self._started_at = self._now()

        # The last *completed* round.
        self._last_round_at: datetime | None = None
        # The last round that actually proved the dependency was working.
        self._last_success_at: datetime | None = None

        # The most recent failure from anywhere in the check, kept outside
        # any round summary because a failure to even start a round is
        # exactly when the operator most needs the reason.
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None

    def _is_stale(self, now: datetime) -> bool:
        """Has too long passed with no completed round?

        Ahead of every other branch in both `state()` implementations, so a
        stale success can never be served as a fresh one.
        """
        reference = self._last_round_at or self._started_at
        stale_after = max(self._interval * _STALE_INTERVALS, _MIN_STALE_AFTER_S)
        return (now - reference).total_seconds() > stale_after

    async def run_forever(self) -> None:
        """The loop. Dies only of cancellation.

        `app.py` runs this as a background task and reports its liveness the
        same way it reports the worker's, because a monitoring task that
        silently stopped monitoring is the failure this whole module is
        about, one level in.
        """
        if self._initial_delay:
            await asyncio.sleep(self._initial_delay)
        while True:
            await self.run_once_guarded()
            await asyncio.sleep(self._interval)

    async def run_once_guarded(self) -> None:
        """One round with the loop's own net around it. Never raises.

        `run_once` is already guarded internally, so this is the net under
        that -- a check able to kill its own task would replace one silent
        failure with another. It is a named method rather than a `try` in
        the loop so the "a failing round costs nothing but that round"
        property can be exercised directly, on the app's real check,
        without a test reaching into the loop.

        `CancelledError` is a `BaseException` and passes straight through,
        so shutdown still works.
        """
        try:
            await self.run_once()
        except Exception:
            log.exception("%s round failed; retrying next tick", self._WHAT)

    async def run_once(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _record_error(self, message: str | None, at: datetime | None = None) -> None:
        if not message:
            return
        self._last_error = message
        self._last_error_at = at or self._now()


class SvtCanary(_PeriodicCheck):
    """Checks the operator's own mappings against SVT, on a slow loop.

    `mappings_provider` is a zero-argument callable returning the current
    `Mapping` rows -- in the app it is `ReloadingMappingTable.all`, so the
    canary checks exactly the table the feed is serving from rather than a
    second copy loaded for the occasion. It is a callable rather than a list
    so a mapping added through the config page is picked up on the next
    round with no restart.

    `svt` is the shared `SvtClient`; only `list_episodes` is ever called.

    `sonarr` is the shared `SonarrClient`, and it is optional: without it
    the SVT half runs exactly as before and every row's `resolves` stays
    `None`. Only `all_series` and `episodes` are ever called, both
    read-only GETs. It is here rather than in a third check because the
    resolvability question needs *both* episode lists, and the SVT one is
    already in hand at this point in the round -- a separate check would
    double this service's traffic against an unofficial API to learn
    nothing new.
    """

    def __init__(
        self,
        mappings_provider,
        svt,
        sonarr=None,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        concurrency: int = DEFAULT_CONCURRENCY,
        spacing_s: float = DEFAULT_SPACING_S,
        initial_delay_s: float = DEFAULT_INITIAL_DELAY_S,
        # `Settings`' own default, never a literal: this must compare at
        # the tolerance the resolver will later match at, and a number
        # written here would be a second home for an operator-visible
        # setting.
        tolerance_days: int = Settings.air_date_tolerance_days,
        clock=_utcnow,
    ):
        super().__init__(
            interval_s=interval_s,
            probe_timeout_s=probe_timeout_s,
            initial_delay_s=initial_delay_s,
            clock=clock,
        )
        self._mappings_provider = mappings_provider
        self._svt = svt
        self._sonarr = sonarr
        self._tolerance_days = int(tolerance_days)
        self._concurrency = max(int(concurrency), 1)
        self._spacing = max(float(spacing_s), 0.0)

        # Per-mapping state, keyed by tvdb_id. Rebuilt from the current
        # mapping rows on every round, so a deleted mapping stops being
        # reported rather than lingering as a permanent phantom failure.
        self._health: dict[int, MappingHealth] = {}

        # The last *completed* round's summary, swapped in as a unit at the
        # end of the round. Reading a half-finished round would let the
        # "all failing" / "one failing" distinction -- the whole point of
        # the report -- be decided by which probes happened to be done.
        self._checked = 0
        self._failing = 0
        self._episodes_seen = 0
        # ...and the resolvability half of the same round. Counts only,
        # per round: a verdict is never carried forward, so a Sonarr
        # outage clears them rather than leaving a stale accusation.
        self._unresolvable = 0
        self._resolvability_unknown = 0
        self._resolvability_error: str | None = None

    # --- Reporting --------------------------------------------------------

    def state(self) -> str:
        now = self._now()
        if self._is_stale(now):
            # Checked once and then never again is the same problem as never
            # checked at all: nothing current is known about SVT.
            return STATE_STALE
        if self._last_round_at is None:
            return STATE_UNKNOWN
        if self._checked == 0:
            return STATE_NO_MAPPINGS
        if self._failing >= self._checked:
            # With exactly one mapping the two shapes are the same set, and
            # this branch wins: a lone mapping failing is indistinguishable
            # from SVT being down, and the more urgent reading is the safe
            # one to act on.
            return STATE_SVT
        if self._failing:
            return STATE_SERIES
        # Ranked below both of the above, and only because `state` is one
        # word: a row SVT will not even list is the louder finding, and an
        # unresolvable row alongside it is still reported in full by
        # `status()` rather than lost to the headline.
        if self._unresolvable:
            return STATE_UNRESOLVABLE
        return STATE_OK

    def status(self) -> dict:
        """The canary's contribution to `/health` -- and, through it, to the
        config page's status strip.

        There is exactly one computation behind both surfaces (see
        `app.compute_health`), and this is the part of it that knows about
        SVT. Nothing renders a second opinion of any of these facts.
        """
        now = self._now()
        state = self.state()
        failing = [h for h in self._health.values() if h.ok is False]
        unresolvable = [
            h
            for h in sorted(self._health.values(), key=_by_tvdb)
            if h.unresolvable_reason in UNRESOLVABLE_REASONS
        ]
        return {
            "state": state,
            # Does this turn /health's top-level light red? See
            # DEGRADED_STATES for why one failing show deliberately does not.
            "degraded": state in DEGRADED_STATES,
            # Is there a finding the operator should be looking at? A
            # superset: `series` is here and not above, which is exactly the
            # gap between "worth your attention" and "worth an alert".
            "needs_attention": state in ATTENTION_STATES,
            # Ages, not just timestamps. "When did we last confirm SVT
            # works" is the strip's headline question, and an ISO instant in
            # UTC makes the operator do timezone arithmetic to answer it.
            # Computed here, off the same clock as everything else in this
            # dict, so the page renders an age rather than deriving one --
            # the same "one computation, two surfaces" rule the rest of
            # `/health` follows.
            "last_checked_age_s": _age_s(self._last_round_at, now),
            "last_success_age_s": _age_s(self._last_success_at, now),
            # Of the last completed round. Zero with state `no_mappings`
            # means there was nothing to check, not that checking failed.
            "checked": self._checked,
            "failing": self._failing,
            "episodes_seen": self._episodes_seen,
            "last_checked": _iso(self._last_round_at),
            # The last time SVT and the episode listing were demonstrably
            # working.
            # Survives a later failure on purpose: it is the difference
            # between "broke this hour" and "never worked".
            "last_success": _iso(self._last_success_at),
            "last_error": self._last_error,
            "last_error_at": _iso(self._last_error_at),
            # Enough to name the show to go and fix. Capped, because the
            # full breakdown belongs in the mappings view; `failing` above
            # is always the true count.
            "failing_series": [
                {
                    "tvdb_id": h.tvdb_id,
                    "series_title": h.series_title,
                    "svt_slug": h.svt_slug,
                    "error": h.last_error,
                }
                for h in failing[:_MAX_REPORTED_FAILURES]
            ],
            "failing_series_truncated": len(failing) > _MAX_REPORTED_FAILURES,
            # --- the mapping that resolves nothing ---------------------
            #
            # Reported alongside the counts above rather than folded into
            # them: a row that fails on SVT and a row that returns a
            # perfect episode list nothing can ever match are different
            # findings with different fixes, and `failing` has always
            # meant the first one.
            "unresolvable": self._unresolvable,
            "unresolvable_series": [
                {
                    "tvdb_id": h.tvdb_id,
                    "series_title": h.series_title,
                    "svt_slug": h.svt_slug,
                    # Machine-readable, so a monitoring setup can tell the
                    # unfixable case from the fixable one without matching
                    # on prose. One of UNRESOLVABLE_REASONS.
                    "reason": h.unresolvable_reason,
                    "note": h.resolvability_note,
                }
                for h in unresolvable[:_MAX_REPORTED_FAILURES]
            ],
            "unresolvable_series_truncated": (
                len(unresolvable) > _MAX_REPORTED_FAILURES
            ),
            # How many rows this round could not decide because Sonarr
            # could not be asked. Present so that a Sonarr outage reads as
            # "this was not checked" rather than as `unresolvable: 0`,
            # which is what a clean sweep looks like. It does not raise
            # attention here -- `SonarrCanary` already turns the top-level
            # light red for a Sonarr that is not answering, and a second
            # alarm for the same cause is the noise this module avoids.
            "resolvability_unknown": self._resolvability_unknown,
            # One of `sonarr.REASON_MESSAGES` (fixed literals), or this
            # module's own words. Never the exception's, and never a URL.
            "resolvability_error": self._resolvability_error,
        }

    def per_mapping(self) -> list[dict]:
        """Every current mapping's own check state, in tvdb_id order.

        Not rendered on the status strip -- the headline there is "is SVT
        working, and when did we last confirm it". This is what a
        per-mapping view reads.

        Each timestamp comes with an age in seconds beside it, computed
        here rather than by whatever renders this, and by the same `_age_s`
        `status()` uses. That is what stops the mappings table and the
        strip drifting apart on what "20 minutes ago" means.

        Mappings the canary has not reached yet appear here with `ok: None`
        rather than being absent. A row missing from this list would be read
        as "nothing to report about it", which is the same reassuring
        silence the module exists to remove -- one mapping down. Reading the
        provider is guarded for the usual reason: a report must not be able
        to raise.
        """
        # One clock read for the whole report, so two rows checked in the
        # same round cannot come back with ages a few milliseconds apart.
        now = self._now()
        known = dict(self._health)
        try:
            for mapping in self._mappings_provider() or []:
                known.setdefault(
                    mapping.tvdb_id,
                    MappingHealth(
                        tvdb_id=mapping.tvdb_id,
                        series_title=mapping.series_title,
                        svt_slug=mapping.svt_slug,
                    ),
                )
        except Exception:
            log.warning(
                "SVT canary could not read the mappings while reporting",
                exc_info=True,
            )
        return [h.as_dict(now) for h in sorted(known.values(), key=_by_tvdb)]

    # --- Running ----------------------------------------------------------

    _WHAT = "SVT canary"

    async def run_once(self) -> None:
        """One full round over the current mappings. Never raises."""
        try:
            mappings = list(self._mappings_provider() or [])
        except Exception as exc:
            # Deliberately does NOT complete a round. "I could not read the
            # mappings" is not "there are no mappings to check": completing
            # here would report the reassuring `no_mappings` state for a
            # service that is checking nothing at all. Leaving the round
            # uncompleted means staleness eventually makes it loud, and
            # `last_error` says why in the meantime.
            log.warning("SVT canary could not read the mappings", exc_info=True)
            self._record_error(f"could not read the mappings: {exc}")
            return

        # One library read for the whole round, ahead of the fan-out.
        # `SonarrClient.series_id_for_tvdb` fetches the series list on
        # every call, so asking it per mapping would cost N reads of the
        # same list to learn the same thing.
        series_index, index_error = await self._sonarr_series_index()

        # Built per round, not held on the instance: `asyncio.Semaphore`
        # binds to the loop it is first awaited on and refuses to be used
        # from another, and `SvtCanary` is constructed by `create_app`
        # outside any running loop. A per-round gate has no lifetime to get
        # wrong, and bounds exactly what it needs to -- in-flight probes
        # within one round.
        gate = asyncio.Semaphore(self._concurrency)
        results = await asyncio.gather(
            *(
                self._staggered(i, m, gate, series_index, index_error)
                for i, m in enumerate(mappings)
            ),
            return_exceptions=True,
        )

        now = self._now()
        health: dict[int, MappingHealth] = {}
        checked = failing = episodes = 0
        unresolvable = resolvability_unknown = 0
        resolvability_error: str | None = None
        for mapping, result in zip(mappings, results):
            previous = self._health.get(mapping.tvdb_id)
            if isinstance(result, BaseException):
                # `_probe` is written not to raise, so this is the net under
                # a bug in it rather than an expected path. One probe
                # blowing up must cost that probe, not the round.
                log.error(
                    "SVT canary probe for %r raised", mapping.svt_slug,
                    exc_info=result,
                )
                result = (
                    False,
                    None,
                    f"probe failed unexpectedly: {result}",
                    Resolvability(None, UNDETERMINED_NOT_CHECKED),
                )
            ok, episode_count, error, resolves = result
            checked += 1
            if ok:
                episodes += episode_count or 0
            else:
                failing += 1
                self._record_error(error, at=now)
            if resolves.is_finding:
                unresolvable += 1
            elif resolves.reason == UNDETERMINED_SONARR_UNAVAILABLE:
                resolvability_unknown += 1
                resolvability_error = resolvability_error or resolves.note
            health[mapping.tvdb_id] = _merge(previous, mapping, now, ok,
                                             episode_count, error, resolves)

        self._health = health
        self._last_round_at = now
        self._checked = checked
        self._failing = failing
        self._episodes_seen = episodes
        self._unresolvable = unresolvable
        self._resolvability_unknown = resolvability_unknown
        self._resolvability_error = resolvability_error
        if checked and failing < checked:
            self._last_success_at = now

    async def _sonarr_series_index(self) -> tuple[dict[int, int] | None, str | None]:
        """tvdb id -> Sonarr series id, or (None, why not). Never raises.

        `None` is "Sonarr could not be asked", which every row then reports
        as undetermined. It is deliberately not an empty dict: an empty
        library and an unreachable Sonarr are different facts, and only one
        of them says anything about a mapping.
        """
        if self._sonarr is None:
            return None, None
        try:
            series = await asyncio.wait_for(
                self._sonarr.all_series(), timeout=self._probe_timeout
            )
        except TimeoutError:
            return None, (
                f"Sonarr did not answer within {self._probe_timeout:g}s, so "
                "whether these mappings can match anything is unknown."
            )
        except SonarrApiError as exc:
            # `str(exc)` is one of REASON_MESSAGES -- a fixed literal with
            # no URL and no key in it. Same discipline as `SonarrCanary`.
            return None, str(exc)
        except Exception:
            # This module's own words rather than the exception's, so an
            # unexpected type cannot smuggle anything -- an API key
            # included -- onto a rendered page.
            log.warning(
                "SVT canary could not read Sonarr's series list", exc_info=True
            )
            return None, (
                "Sonarr's series list could not be read, so whether these "
                "mappings can match anything is unknown. Check svtplay-arr's "
                "log."
            )
        index: dict[int, int] = {}
        for entry in series or ():
            if not isinstance(entry, dict):
                continue
            tvdb_id, series_id = entry.get("tvdbId"), entry.get("id")
            if isinstance(tvdb_id, int) and isinstance(series_id, int):
                index[tvdb_id] = series_id
        return index, None

    async def _staggered(
        self, index: int, mapping, gate: asyncio.Semaphore,
        series_index, index_error,
    ):
        if self._spacing and index:
            # Spread the round's requests out instead of opening N
            # connections to svtplay.se in the same second. Sequenced by
            # position rather than by a shared pacer, so it needs no state
            # and cannot deadlock.
            await asyncio.sleep(index * self._spacing)
        async with gate:
            return await self._probe(mapping, series_index, index_error)

    async def _probe(
        self, mapping, series_index=None, index_error=None,
    ) -> tuple[bool, int | None, str | None, Resolvability]:
        """Check one mapping. Returns (ok, episode_count, error, resolvability).

        Never raises except on cancellation. Read-only throughout: the same
        `list_episodes` call `Resolver` makes, and -- when a Sonarr client
        is wired in and SVT answered -- one `episodes()` read of the
        series' episode list. Nothing else on either client.

        The two halves are independent by construction. A Sonarr that is
        down cannot change `ok`, `episode_count` or `error`; it can only
        leave the resolvability undetermined. Both are bounded by the same
        per-probe timeout, and both run inside this round's stagger and
        concurrency gate, so the Sonarr requests arrive at the same
        measured pace as the SVT ones rather than as a burst.
        """
        slug = mapping.svt_slug
        try:
            episodes = await asyncio.wait_for(
                self._svt.list_episodes(slug), timeout=self._probe_timeout
            )
        except TimeoutError:
            # `asyncio.TimeoutError` is this same builtin on 3.11+. A slow
            # or hanging SVT costs one probe its timeout and nothing else --
            # it can neither stall the round nor wedge the loop.
            return (
                False,
                None,
                f"SVT timed out for {slug!r} after "
                f"{self._probe_timeout:g}s",
                _NOT_CHECKED,
            )
        except SvtApiError as exc:
            # The status code is why `SvtApiError` carries one: a 404 is the
            # single most likely per-show failure (the show ended, or SVT
            # re-slugged it) and it is fixed by editing that one row, so the
            # report has to say so rather than reading like a generic
            # outage. Every other cause -- network, timeout, malformed body
            # -- has no status to report and falls through.
            if exc.status_code == 404:
                return (
                    False,
                    None,
                    f"SVT has nothing at slug {slug!r} (404 not found) -- the "
                    "show may have ended, or its URL changed",
                    _NOT_CHECKED,
                )
            return (
                False, None, f"SVT check for {slug!r} failed: {exc}",
                _NOT_CHECKED,
            )
        except Exception as exc:
            # Anything else is caught for the same reason every other guard
            # in this project catches broadly: a monitoring component must
            # not be able to fail the thing it monitors.
            return (
                False, None, f"SVT check for {slug!r} failed: {exc}",
                _NOT_CHECKED,
            )

        count = len(episodes or [])
        if count == 0:
            # The whole point. A 200 carrying no episodes is what a
            # semantic SVT change looks like from here, and it is also what
            # a retired show looks like -- the counts in `status()` are what
            # separate those two, not this probe.
            return (
                False,
                0,
                f"SVT answered for {slug!r} but listed no episodes "
                "-- the show may have ended, or SVT has changed what it "
                "returns and svtplay-arr needs updating",
                _NOT_CHECKED,
            )
        return (
            True, count, None,
            await self._resolvability(mapping, episodes, series_index, index_error),
        )

    async def _resolvability(
        self, mapping, svt_episodes, series_index, index_error,
    ) -> Resolvability:
        """Can this mapping's episodes match any of Sonarr's? Never raises.

        The SVT episode list is the one this round already fetched, so this
        costs SVT nothing. Against Sonarr it costs one `episodes()` read
        per mapping per round, on top of the round's single `all_series()`.
        Sonarr is the operator's own service and the resolver already calls
        it several times an hour on every RSS poll, so that is cheap -- but
        it is still bounded (`wait_for`), still staggered, and still behind
        the round's concurrency gate, because "cheap" is not "unbounded".
        """
        if self._sonarr is None:
            return _NOT_CHECKED
        if series_index is None:
            return Resolvability(
                None,
                UNDETERMINED_SONARR_UNAVAILABLE,
                index_error
                or "Sonarr could not be asked, so whether this mapping can "
                "match anything is unknown.",
            )
        series_id = series_index.get(mapping.tvdb_id)
        if series_id is None:
            # Not a finding. Nothing can match, but the cause is not this
            # mapping's episodes, and this check only ever claims that one
            # shape. `Resolver` logs the same situation per request.
            return Resolvability(
                None,
                UNDETERMINED_NOT_IN_SONARR,
                f"Sonarr has no series with tvdb id {mapping.tvdb_id}, so "
                "there is nothing to compare these episodes against.",
            )
        try:
            sonarr_episodes = await asyncio.wait_for(
                self._sonarr.episodes(series_id), timeout=self._probe_timeout
            )
        except TimeoutError:
            return Resolvability(
                None,
                UNDETERMINED_SONARR_UNAVAILABLE,
                f"Sonarr did not answer within {self._probe_timeout:g}s, so "
                "whether this mapping can match anything is unknown.",
            )
        except SonarrApiError as exc:
            # A fixed literal from REASON_MESSAGES. No URL, no key.
            return Resolvability(
                None, UNDETERMINED_SONARR_UNAVAILABLE, str(exc)
            )
        except Exception:
            log.warning(
                "SVT canary could not read Sonarr's episodes for %r",
                mapping.svt_slug, exc_info=True,
            )
            return Resolvability(
                None,
                UNDETERMINED_SONARR_UNAVAILABLE,
                "Sonarr's episode list could not be read, so whether this "
                "mapping can match anything is unknown. Check svtplay-arr's "
                "log.",
            )
        return resolvability(
            svt_episodes,
            sonarr_episodes,
            slug=mapping.svt_slug,
            tolerance_days=self._tolerance_days,
            today=self._now().date(),
        )


def sonarr_unavailable_status() -> dict:
    """What `app.compute_health` reports when `SonarrCanary.status()` failed.

    Degraded, for the same reason as `unavailable_status` above: the check
    not being readable is not the same as the check having nothing to say
    yet, and only one of those resolves on its own.
    """
    return {
        "state": STATE_UNAVAILABLE,
        "degraded": True,
        "needs_attention": True,
        "last_checked_age_s": None,
        "last_success_age_s": None,
        "last_checked": None,
        "last_success": None,
        "last_error": None,
        "last_error_reason": None,
        "last_error_at": None,
        "version": None,
        "series_count": None,
    }


class SonarrCanary(_PeriodicCheck):
    """Checks that Sonarr is there, is Sonarr, and is *this* Sonarr.

    `sonarr` is the shared `SonarrClient` -- the same instance the resolver
    matches against, built from the settings the service actually booted
    with. That is deliberate and is what makes this check different from the
    configuration page's Test connection button: the button answers "would
    these values work", this answers "is what is running still working".
    Between a settings save and the restart that applies it, those two
    questions have different correct answers, and both are worth having.

    Only `SonarrClient.status()` is ever called. It reads
    `/api/v3/system/status` and the series list, and writes nothing --
    there is no Sonarr endpoint in this class's reach that could.
    """

    _WHAT = "Sonarr check"

    def __init__(
        self,
        sonarr,
        *,
        interval_s: float = DEFAULT_SONARR_INTERVAL_S,
        probe_timeout_s: float = DEFAULT_SONARR_TIMEOUT_S,
        initial_delay_s: float = DEFAULT_SONARR_INITIAL_DELAY_S,
        clock=_utcnow,
    ):
        super().__init__(
            interval_s=interval_s,
            probe_timeout_s=probe_timeout_s,
            initial_delay_s=initial_delay_s,
            clock=clock,
        )
        self._sonarr = sonarr
        # The last round's verdict, swapped in as a unit at the end of it.
        self._ok: bool | None = None
        self._last_error_reason: str | None = None
        # From the last check that *worked*. Kept across a later failure for
        # the same reason `last_success` is: "answered an hour ago with 42
        # series, failing now" and "never answered" are different situations.
        self._version: str | None = None
        self._series_count: int | None = None

    # --- Reporting --------------------------------------------------------

    def state(self) -> str:
        now = self._now()
        if self._is_stale(now):
            return STATE_STALE
        if self._last_round_at is None:
            # Nothing has been checked since this process started. Not
            # healthy, and never rendered as such -- see the module
            # docstring and SONARR_DEGRADED_STATES.
            return STATE_UNKNOWN
        return STATE_OK if self._ok else STATE_SONARR

    def status(self) -> dict:
        """This check's contribution to `/health`, and through it to the
        configuration page's status strip.

        One computation, two surfaces -- `app.compute_health` calls this
        once and both render its result verbatim. Nothing re-derives any of
        it.

        Carries no URL and no key. `last_error` is one of
        `sonarr.REASON_MESSAGES`, which are fixed literals; `version` and
        `series_count` come from Sonarr's own answer.
        """
        now = self._now()
        state = self.state()
        return {
            "state": state,
            "degraded": state in SONARR_DEGRADED_STATES,
            "needs_attention": state in SONARR_ATTENTION_STATES,
            "last_checked_age_s": _age_s(self._last_round_at, now),
            "last_success_age_s": _age_s(self._last_success_at, now),
            "last_checked": _iso(self._last_round_at),
            "last_success": _iso(self._last_success_at),
            "last_error": self._last_error,
            # The machine-readable half of the same fact, so a monitoring
            # setup can branch on "the key was rejected" without matching on
            # prose. One of sonarr.REASON_*.
            "last_error_reason": self._last_error_reason,
            "last_error_at": _iso(self._last_error_at),
            # What the last working check saw. The count is the one that
            # says which Sonarr answered rather than merely that one did.
            "version": self._version,
            "series_count": self._series_count,
        }

    # --- Running ----------------------------------------------------------

    async def run_once(self) -> None:
        """One check. Never raises.

        Bounded by `wait_for` on top of whatever timeout the shared httpx
        client carries: this runs on the same event loop as the download
        worker and the routes, and a Sonarr that accepts a connection and
        then says nothing must cost this round its timeout and nothing else.
        """
        now = self._now()
        try:
            result = await asyncio.wait_for(
                self._sonarr.status(), timeout=self._probe_timeout
            )
        except TimeoutError:
            self._fail(
                now,
                f"Sonarr did not answer within {self._probe_timeout:g}s.",
                "timeout",
            )
            return
        except SonarrApiError as exc:
            # `str(exc)` is one of REASON_MESSAGES -- a fixed literal with
            # no URL and no key in it. See sonarr.py's module docstring for
            # why the message is built there rather than here.
            self._fail(now, str(exc), exc.reason)
            return
        except Exception as exc:
            # Broad, for the reason every guard in this module is broad: a
            # monitoring component must not be able to fail the thing it
            # monitors. The message is this module's own words about a
            # non-Sonarr exception rather than the exception's, so an
            # unexpected type cannot smuggle anything into a rendered page.
            log.warning(
                "Sonarr check failed unexpectedly (%s)",
                type(exc).__name__,
                exc_info=True,
            )
            self._fail(
                now,
                "The Sonarr check failed unexpectedly. Check svtplay-arr's log.",
                REASON_UNKNOWN,
            )
            return

        self._ok = True
        self._version = result.version
        self._series_count = result.series_count
        self._last_round_at = now
        self._last_success_at = now

    def _fail(self, now: datetime, message: str, reason: str) -> None:
        self._ok = False
        self._last_error_reason = reason
        self._record_error(message, at=now)
        # A failure still *completes* a round: something is known about
        # Sonarr, and it is bad. Not completing here would let the state
        # drift to `stale` and report "nothing is checking Sonarr" over a
        # check that is running perfectly and finding a real problem.
        self._last_round_at = now


def _age_s(when: datetime | None, now: datetime) -> float | None:
    if when is None:
        return None
    # Clamped at zero: a clock step backwards must not produce a negative
    # age that renders as "-3 minutes ago" on the operator's page.
    return max((now - when).total_seconds(), 0.0)


def _by_tvdb(health: MappingHealth) -> int:
    return health.tvdb_id


def _merge(
    previous: MappingHealth | None,
    mapping,
    now: datetime,
    ok: bool,
    episode_count: int | None,
    error: str | None,
    resolves: Resolvability = _NOT_CHECKED,
) -> MappingHealth:
    """Fold one probe's outcome into what was already known about a mapping.

    A failure keeps the previous `last_success` and `episode_count`: they
    describe the last check that *worked*, and losing them on the first
    failure would erase the only evidence that this show ever resolved.

    The resolvability fields do the opposite, deliberately: they are always
    this round's, so a `False` verdict cannot outlive the round that earned
    it. Keeping one would go on accusing a mapping through a Sonarr outage
    that looked at nothing.
    """
    base = previous or MappingHealth(
        tvdb_id=mapping.tvdb_id,
        series_title=mapping.series_title,
        svt_slug=mapping.svt_slug,
    )
    # The row may have been edited between rounds; the identifying fields
    # always come from the mapping table, never from the stale copy.
    base = replace(
        base,
        series_title=mapping.series_title,
        svt_slug=mapping.svt_slug,
        ok=ok,
        last_checked=now,
        resolves=resolves.resolves,
        unresolvable_reason=(
            resolves.reason if resolves.is_finding else None
        ),
        resolvability_note=resolves.note,
    )
    if ok:
        return replace(
            base, last_success=now, episode_count=episode_count,
        )
    return replace(base, last_error=error, last_error_at=now)
