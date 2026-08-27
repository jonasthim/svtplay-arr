"""Periodic proof that SVT still answers and the parser still works.

This project's design is "refuse on doubt, return nothing". That is why the
media library is safe -- but it is also what makes failure and idleness
indistinguishable. If SVT changes its page format, `parse_show_page` returns
`[]`, the resolver returns nothing, the feed goes empty, and Sonarr grabs
nothing. Every existing check keeps saying `ok`, because every existing
check is about *this* service: the worker task, the mapping table, the
filesystem, the job store. None of them has ever known whether SVT is
there. The operator finds out weeks later, wondering why no episodes have
appeared.

The parser is a regex scan over SVT's Next.js flight payload against an
undocumented API. That breaking is a *when*, not an *if*, so the one silence
this codebase could not detect was its own.

**The canary is the operator's own mappings, not a hardcoded show.** A
hardcoded slug is a fixture that rots: the show ends, SVT retires the URL,
and the canary reports a failure that is about the fixture rather than the
service. Checking the rows that are actually in mappings.yaml is both a real
signal and directly useful -- it answers "do my mappings still work" as a
side effect, and it is exactly the set of shows whose absence the operator
would notice.

**Zero episodes is a failure, not a success.** This is the single decision
the whole module turns on. A show page whose format changed still returns
HTTP 200; `parse_show_page` just finds nothing in it. Treating an empty list
as "the check passed, SVT answered" would make this report `ok` through
precisely the outage it exists to catch.

**Two failure shapes, because they need different actions.**
  - *Every* mapping failing points at SVT or the parser: nothing will be
    grabbed until it is fixed, the operator can do nothing about the cause,
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

**It writes nothing.** The only call it makes is
`SvtClient.list_episodes`, the same read-only listing `Resolver` and the
config page's Check control already use. It never touches the mapping
writer, the config writer, the job store, or Sonarr.

**It cannot degrade the service.** Every round is wrapped: a probe that
raises costs that probe, a round that raises costs that round, and
`run_forever` never dies of anything but cancellation. A hanging SVT is
bounded by a per-probe timeout rather than being allowed to wedge the loop.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone

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
# headline needs enough to tell a re-slugged show from a broken parser.
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
# The last round resolved none of its mappings: SVT or the parser.
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
# a week they stop looking at it. Then the day SVT breaks the parser and the
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
# the same set plus `series`. The strip and its banner key off this; only
# `/health`'s top-level `status` keys off DEGRADED_STATES above.
ATTENTION_STATES = DEGRADED_STATES | {STATE_SERIES}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    def as_dict(self) -> dict:
        return {
            "tvdb_id": self.tvdb_id,
            "series_title": self.series_title,
            "svt_slug": self.svt_slug,
            "ok": self.ok,
            "last_checked": _iso(self.last_checked),
            "last_success": _iso(self.last_success),
            "episode_count": self.episode_count,
            "last_error": self.last_error,
            "last_error_at": _iso(self.last_error_at),
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
    }


class SvtCanary:
    """Checks the operator's own mappings against SVT, on a slow loop.

    `mappings_provider` is a zero-argument callable returning the current
    `Mapping` rows -- in the app it is `ReloadingMappingTable.all`, so the
    canary checks exactly the table the feed is serving from rather than a
    second copy loaded for the occasion. It is a callable rather than a list
    so a mapping added through the config page is picked up on the next
    round with no restart.

    `svt` is the shared `SvtClient`; only `list_episodes` is ever called.
    """

    def __init__(
        self,
        mappings_provider,
        svt,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        concurrency: int = DEFAULT_CONCURRENCY,
        spacing_s: float = DEFAULT_SPACING_S,
        initial_delay_s: float = DEFAULT_INITIAL_DELAY_S,
        clock=_utcnow,
    ):
        self._mappings_provider = mappings_provider
        self._svt = svt
        self._interval = max(float(interval_s), _MIN_INTERVAL_S)
        self._probe_timeout = float(probe_timeout_s)
        self._concurrency = max(int(concurrency), 1)
        self._spacing = max(float(spacing_s), 0.0)
        self._initial_delay = max(float(initial_delay_s), 0.0)
        self._now = clock

        # Startup is the reference staleness is measured from until a round
        # completes. Without it a canary that never manages a single round
        # would sit at "unknown" forever, which is the one way this feature
        # could reproduce the silence it exists to remove.
        self._started_at = self._now()

        # Per-mapping state, keyed by tvdb_id. Rebuilt from the current
        # mapping rows on every round, so a deleted mapping stops being
        # reported rather than lingering as a permanent phantom failure.
        self._health: dict[int, MappingHealth] = {}

        # The last *completed* round's summary, swapped in as a unit at the
        # end of the round. Reading a half-finished round would let the
        # "all failing" / "one failing" distinction -- the whole point of
        # the report -- be decided by which probes happened to be done.
        self._last_round_at: datetime | None = None
        self._checked = 0
        self._failing = 0
        self._episodes_seen = 0

        # The most recent error from anywhere: a probe, or the mapping read
        # that precedes them. Kept outside the round summary because a
        # failure to even start a round is exactly when the operator most
        # needs the reason.
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None

        # The last time *any* mapping resolved, i.e. the last moment SVT and
        # the parser were both demonstrably working.
        self._last_success_at: datetime | None = None

    # --- Reporting --------------------------------------------------------

    def state(self) -> str:
        now = self._now()
        reference = self._last_round_at or self._started_at
        stale_after = max(self._interval * _STALE_INTERVALS, _MIN_STALE_AFTER_S)
        if (now - reference).total_seconds() > stale_after:
            # Checked once and then never again is the same problem as never
            # checked at all: nothing current is known about SVT. This is
            # ahead of every other branch so a stale success can never be
            # served as a fresh one.
            return STATE_STALE
        if self._last_round_at is None:
            return STATE_UNKNOWN
        if self._checked == 0:
            return STATE_NO_MAPPINGS
        if self._failing == 0:
            return STATE_OK
        if self._failing >= self._checked:
            # With exactly one mapping the two shapes are the same set, and
            # this branch wins: a lone mapping failing is indistinguishable
            # from SVT being down, and the more urgent reading is the safe
            # one to act on.
            return STATE_SVT
        return STATE_SERIES

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
            # The last time SVT and the parser were demonstrably working.
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
        }

    def per_mapping(self) -> list[dict]:
        """Every current mapping's own check state, in tvdb_id order.

        Not rendered on the status strip -- the headline there is "is SVT
        working, and when did we last confirm it". This is what a
        per-mapping view reads.

        Mappings the canary has not reached yet appear here with `ok: None`
        rather than being absent. A row missing from this list would be read
        as "nothing to report about it", which is the same reassuring
        silence the module exists to remove -- one mapping down. Reading the
        provider is guarded for the usual reason: a report must not be able
        to raise.
        """
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
        return [h.as_dict() for h in sorted(known.values(), key=_by_tvdb)]

    # --- Running ----------------------------------------------------------

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
        that -- a canary able to kill its own task would replace one silent
        failure with another. It is a named method rather than a `try` in
        the loop so the "a failing round costs nothing but that round"
        property can be exercised directly, on the app's real canary,
        without a test reaching into the loop.

        `CancelledError` is a `BaseException` and passes straight through,
        so shutdown still works.
        """
        try:
            await self.run_once()
        except Exception:
            log.exception("SVT canary round failed; retrying next tick")

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

        # Built per round, not held on the instance: `asyncio.Semaphore`
        # binds to the loop it is first awaited on and refuses to be used
        # from another, and `SvtCanary` is constructed by `create_app`
        # outside any running loop. A per-round gate has no lifetime to get
        # wrong, and bounds exactly what it needs to -- in-flight SVT
        # requests within one round.
        gate = asyncio.Semaphore(self._concurrency)
        results = await asyncio.gather(
            *(self._staggered(i, m, gate) for i, m in enumerate(mappings)),
            return_exceptions=True,
        )

        now = self._now()
        health: dict[int, MappingHealth] = {}
        checked = failing = episodes = 0
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
                result = (False, None, f"probe failed unexpectedly: {result}")
            ok, episode_count, error = result
            checked += 1
            if ok:
                episodes += episode_count or 0
            else:
                failing += 1
                self._record_error(error, at=now)
            health[mapping.tvdb_id] = _merge(previous, mapping, now, ok,
                                             episode_count, error)

        self._health = health
        self._last_round_at = now
        self._checked = checked
        self._failing = failing
        self._episodes_seen = episodes
        if checked and failing < checked:
            self._last_success_at = now

    async def _staggered(self, index: int, mapping, gate: asyncio.Semaphore):
        if self._spacing and index:
            # Spread the round's requests out instead of opening N
            # connections to svtplay.se in the same second. Sequenced by
            # position rather than by a shared pacer, so it needs no state
            # and cannot deadlock.
            await asyncio.sleep(index * self._spacing)
        async with gate:
            return await self._probe(mapping)

    async def _probe(self, mapping) -> tuple[bool, int | None, str | None]:
        """Check one mapping. Returns (ok, episode_count, error).

        Never raises except on cancellation. Read-only: exactly the same
        `list_episodes` call `Resolver` makes, and nothing else.
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
                )
            return False, None, f"SVT check for {slug!r} failed: {exc}"
        except Exception as exc:
            # Anything else is caught for the same reason every other guard
            # in this project catches broadly: a monitoring component must
            # not be able to fail the thing it monitors.
            return False, None, f"SVT check for {slug!r} failed: {exc}"

        count = len(episodes or [])
        if count == 0:
            # The whole point. A 200 with nothing parseable in it is what an
            # SVT format change looks like from here, and it is also what a
            # retired show looks like -- the counts in `status()` are what
            # separate those two, not this probe.
            return (
                False,
                0,
                f"SVT answered for {slug!r} but no episodes could be parsed "
                "-- the show may have ended, or SVT's page format has "
                "changed and the parser needs updating",
            )
        return True, count, None

    def _record_error(self, message: str | None, at: datetime | None = None) -> None:
        if not message:
            return
        self._last_error = message
        self._last_error_at = at or self._now()


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
) -> MappingHealth:
    """Fold one probe's outcome into what was already known about a mapping.

    A failure keeps the previous `last_success` and `episode_count`: they
    describe the last check that *worked*, and losing them on the first
    failure would erase the only evidence that this show ever resolved.
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
    )
    if ok:
        return replace(
            base, last_success=now, episode_count=episode_count,
        )
    return replace(base, last_error=error, last_error_at=now)
