"""The SVT canary: does SVT still answer, does it still list episodes, and
do the operator's own mappings still resolve?

These tests are about the canary in isolation. Its wiring into the app --
the background task, `/health`, and the config page's status strip -- is
exercised in `test_app.py` (`compute_health` end to end) and
`test_config_ui.py` (rendering), because that is where the "one
computation, two surfaces" property actually lives.
"""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from svtplay_arr.canary import (
    ATTENTION_STATES,
    SONARR_DEGRADED_STATES,
    STATE_SONARR,
    DEGRADED_STATES,
    STATE_NO_MAPPINGS,
    STATE_OK,
    STATE_SERIES,
    STATE_STALE,
    STATE_SVT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    STATE_UNRESOLVABLE,
    UNRESOLVABLE_NO_AIR_DATE,
    UNRESOLVABLE_NOT_IN_SONARR,
    UNRESOLVABLE_NO_ORDINALS,
    Resolvability,
    SonarrCanary,
    SvtCanary,
    sonarr_unavailable_status,
    unavailable_status,
)
from svtplay_arr.sonarr import (
    REASON_BAD_URL,
    REASON_HTTP,
    REASON_MESSAGES,
    REASON_NOT_SONARR,
    REASON_REFUSED,
    REASON_TLS,
    REASON_UNAUTHORIZED,
    REASON_UNREACHABLE,
    SonarrApiError,
    SonarrStatus,
)
from svtplay_arr.models import Mapping, SonarrEpisode, SvtEpisode
from svtplay_arr.svt.client import SvtApiError, episodes_from_details_page

_T0 = datetime(2026, 8, 27, 9, 0, 0, tzinfo=timezone.utc)

# The Stage 1 differential captures, read here for the one show that
# motivated the resolvability half of this check. A hand-built list of
# ordinal-less episodes would prove only that the code does what it was
# written to do; this is the response SVT actually sent for
# `uppdrag-granskning`, and it is what exposed the defect in the first
# place. Parsed through the shipped reader, so the episodes under test are
# exactly the objects the resolver would be handed.
_FIXTURES = Path(__file__).parent / "fixtures/svt"


def _captured(show: str) -> list[SvtEpisode]:
    body = json.loads(
        (_FIXTURES / f"details-{show}-20260828.json").read_text(encoding="utf-8")
    )
    return episodes_from_details_page(next(iter(body["data"].values())))


def _sonarr_error(reason: str) -> SonarrApiError:
    """A SonarrApiError exactly as `SonarrClient` would raise it.

    Built through the same message table the client uses rather than with a
    message of its own, so a test cannot pass against wording no real
    failure would ever carry.
    """
    return SonarrApiError(REASON_MESSAGES[reason], reason=reason)


class Clock:
    """A hand-wound clock, so staleness is testable without sleeping."""

    def __init__(self, start: datetime = _T0):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _mapping(tvdb_id: int, slug: str | None = None, title: str | None = None):
    return Mapping(
        tvdb_id=tvdb_id,
        svt_series_id=f"svt{tvdb_id}",
        svt_slug=slug or f"show-{tvdb_id}",
        series_title=title or f"Show {tvdb_id}",
    )


def _episodes(n: int) -> list[SvtEpisode]:
    return [
        SvtEpisode(
            svt_id=f"e{i}",
            title=f"{i}. Avsnitt",
            url=f"/video/e{i}/show/avsnitt-{i}",
            ordinal=i,
            published=date(2026, 8, 20),
            available=True,
            duration_s=1800,
        )
        for i in range(1, n + 1)
    ]


class FakeSvt:
    """Stands in for `SvtClient`, and deliberately offers *only*
    `list_episodes`.

    Every other method -- `search_series`, `resolve_quality` -- is absent by
    construction, so a canary that ever grew a second SVT call, or reached
    for a write path, fails these tests with an AttributeError rather than
    quietly costing SVT more requests than the arithmetic in the docs claims.
    """

    def __init__(self, results: dict | None = None, default=None):
        self.results = results or {}
        self.default = default if default is not None else _episodes(3)
        self.calls: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def list_episodes(self, slug: str):
        self.calls.append(slug)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            outcome = self.results.get(slug, self.default)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "hang":
                await asyncio.Event().wait()  # never returns
            await asyncio.sleep(0)
            return outcome
        finally:
            self.in_flight -= 1


def _canary(mappings, svt, clock=None, **kw):
    kw.setdefault("spacing_s", 0.0)
    kw.setdefault("initial_delay_s", 0.0)
    kw.setdefault("probe_timeout_s", 5.0)
    return SvtCanary(
        (lambda: list(mappings)) if not callable(mappings) else mappings,
        svt,
        clock=clock or Clock(),
        **kw,
    )


# --- Never checked is not healthy ------------------------------------------
#
# The defect this whole feature exists to fix is "up, reporting fine,
# grabbing nothing". A canary that reported an unknown as ok would rebuild
# that defect one level up, so this is the first thing pinned.


def test_before_any_round_the_state_is_unknown_not_ok():
    c = _canary([_mapping(1)], FakeSvt())
    s = c.status()
    assert s["state"] == STATE_UNKNOWN
    assert s["state"] != STATE_OK
    assert s["last_checked"] is None
    assert s["last_success"] is None
    assert s["checked"] == 0


def test_unknown_is_not_yet_a_degrade():
    # It must not read as healthy, but it must not cry wolf either: for the
    # first interval after a restart nothing is known to be *wrong*. The
    # states that do carry a degrade are pinned below; staleness is what
    # eventually turns a permanent unknown loud.
    c = _canary([_mapping(1)], FakeSvt())
    assert c.status()["degraded"] is False


def test_an_unknown_that_never_resolves_goes_stale_and_degrades():
    clock = Clock()
    c = _canary([_mapping(1)], FakeSvt(), clock=clock, interval_s=3600.0)
    assert c.status()["state"] == STATE_UNKNOWN
    clock.advance(3 * 3600.0 + 1)
    s = c.status()
    assert s["state"] == STATE_STALE
    assert s["degraded"] is True


async def test_a_recovered_show_stops_asking_for_attention():
    # The amber must clear on its own once the row is fixed, or it becomes
    # the same permanent noise by another route.
    results = {"show-2": SvtApiError("404", status_code=404)}
    c = _canary([_mapping(1), _mapping(2)], FakeSvt(results))
    await c.run_once()
    assert c.status()["needs_attention"] is True
    results.pop("show-2")
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_OK
    assert s["needs_attention"] is False
    assert s["degraded"] is False


async def test_a_round_that_stops_happening_goes_stale():
    clock = Clock()
    svt = FakeSvt()
    c = _canary([_mapping(1)], svt, clock=clock, interval_s=3600.0)
    await c.run_once()
    assert c.status()["state"] == STATE_OK
    clock.advance(3 * 3600.0 + 1)
    s = c.status()
    assert s["state"] == STATE_STALE
    assert s["degraded"] is True
    # The last confirmed success is still reported: "SVT worked at 09:00 and
    # nothing has checked since" is a different sentence from "SVT is
    # broken", and the operator needs the first one.
    assert s["last_success"] == _T0.isoformat()


def test_staleness_has_a_floor_that_a_short_interval_cannot_undercut():
    # A round over a large library legitimately takes minutes, so with a
    # one-minute interval three intervals would elapse *during* a round that
    # is working perfectly. Staleness means "the check itself is broken", and
    # a check that accuses itself is worth nothing.
    clock = Clock()
    c = _canary([_mapping(1)], FakeSvt(), clock=clock, interval_s=60.0)
    clock.advance(3 * 60.0 + 1)
    assert c.status()["state"] == STATE_UNKNOWN
    clock.advance(900.0)
    assert c.status()["state"] == STATE_STALE


# --- Telling the two failure shapes apart ----------------------------------


async def test_every_mapping_failing_reports_the_svt_or_parser_shape():
    svt = FakeSvt(default=SvtApiError("boom"))
    c = _canary([_mapping(1), _mapping(2), _mapping(3)], svt)
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_SVT
    assert s["degraded"] is True
    assert s["needs_attention"] is True
    assert s["checked"] == 3
    assert s["failing"] == 3


# --- Which findings turn the light red -------------------------------------
#
# A failing show is real and it is the operator's to fix, but it must not
# hold /health's top-level status red until they get round to deleting the
# row. This project has already shipped the other version of that mistake:
# the installer warning that fired on 100% of fresh installs, which the docs
# then taught the reader to explain away. A warning meant to prevent a
# serious failure is worth nothing once it is background noise -- and the
# noise here would be sitting on the exact channel the `svt` shape needs.


async def test_one_failing_show_asks_for_attention_without_turning_the_light_red():
    svt = FakeSvt(results={"show-2": SvtApiError("404", status_code=404)})
    c = _canary([_mapping(1), _mapping(2), _mapping(3)], svt)
    await c.run_once()
    s = c.status()
    assert s["needs_attention"] is True
    assert s["degraded"] is False
    # ...and it is still fully reported, so an operator polling /health can
    # apply whatever policy they like. This is about what turns the light
    # red, not about hiding anything.
    assert s["failing"] == 1
    assert [f["tvdb_id"] for f in s["failing_series"]] == [2]


async def test_the_urgent_shape_still_turns_the_light_red():
    # The one that must survive: nothing will be grabbed until this is
    # fixed, and it is now the only canary state standing between the
    # operator and a month of silently missing episodes.
    svt = FakeSvt(default=SvtApiError("boom"))
    c = _canary([_mapping(1), _mapping(2)], svt)
    await c.run_once()
    s = c.status()
    assert s["degraded"] is True
    assert s["needs_attention"] is True


def test_the_states_that_turn_the_light_red_are_exactly_these():
    # Pinned as a set, so adding a state cannot quietly widen or narrow what
    # alerting fires on.
    assert DEGRADED_STATES == {STATE_STALE, STATE_SVT, STATE_UNAVAILABLE}
    assert STATE_SERIES not in DEGRADED_STATES
    assert STATE_SERIES in ATTENTION_STATES
    assert DEGRADED_STATES < ATTENTION_STATES


async def test_one_mapping_failing_reports_the_per_show_shape():
    svt = FakeSvt(results={"show-2": SvtApiError("404", status_code=404)})
    c = _canary([_mapping(1), _mapping(2), _mapping(3)], svt)
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_SERIES
    assert s["checked"] == 3
    assert s["failing"] == 1
    # A single boolean cannot tell the operator which row to edit; the
    # offending series has to be nameable from the report itself.
    assert [f["tvdb_id"] for f in s["failing_series"]] == [2]
    assert s["failing_series"][0]["series_title"] == "Show 2"
    assert s["failing_series"][0]["svt_slug"] == "show-2"


async def test_the_two_shapes_are_distinguishable_at_one_mapping():
    # With exactly one mapping, "all of them" and "one of them" are the same
    # set. The SVT-or-parser shape is the one that must win: a lone mapping
    # failing is indistinguishable from SVT being down, and the more urgent
    # reading is the safe one.
    svt = FakeSvt(default=SvtApiError("boom"))
    c = _canary([_mapping(1)], svt)
    await c.run_once()
    assert c.status()["state"] == STATE_SVT


async def test_all_succeeding_reports_ok_with_the_episode_count():
    svt = FakeSvt(results={"show-1": _episodes(4), "show-2": _episodes(6)})
    c = _canary([_mapping(1), _mapping(2)], svt)
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_OK
    assert s["degraded"] is False
    assert s["failing"] == 0
    assert s["episodes_seen"] == 10
    assert s["last_checked"] == _T0.isoformat()
    assert s["last_success"] == _T0.isoformat()


async def test_no_mappings_is_its_own_state_and_not_a_degrade():
    # A fresh install legitimately has nothing to check. That is neither a
    # success (nothing proved SVT works) nor a failure -- reporting it as
    # either would be a lie the operator acts on.
    svt = FakeSvt()
    c = _canary([], svt)
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_NO_MAPPINGS
    assert s["degraded"] is False
    assert s["checked"] == 0
    assert svt.calls == []


# --- Zero episodes is a failure, not a success -----------------------------


async def test_a_page_that_parses_to_no_episodes_counts_as_failing():
    # THE case this feature exists for. If SVT changes its page format the
    # request still returns 200 and the listing returns []. Counting
    # that as a success would make the canary report "ok" through exactly
    # the outage it was built to catch.
    svt = FakeSvt(default=[])
    c = _canary([_mapping(1), _mapping(2)], svt)
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_SVT
    assert s["failing"] == 2
    assert "no episodes" in s["last_error"].lower()


async def test_zero_episodes_on_one_show_is_the_per_show_shape():
    svt = FakeSvt(results={"show-2": []})
    c = _canary([_mapping(1), _mapping(2)], svt)
    await c.run_once()
    assert c.status()["state"] == STATE_SERIES


# --- Per-mapping bookkeeping -----------------------------------------------


async def test_per_mapping_records_checked_succeeded_count_and_error():
    clock = Clock()
    svt = FakeSvt(results={"show-1": _episodes(2)})
    c = _canary([_mapping(1)], svt, clock=clock)
    await c.run_once()

    (row,) = c.per_mapping()
    assert row["tvdb_id"] == 1
    assert row["ok"] is True
    assert row["last_checked"] == _T0.isoformat()
    assert row["last_success"] == _T0.isoformat()
    assert row["episode_count"] == 2
    assert row["last_error"] is None
    assert row["last_error_at"] is None

    # Now the same show starts failing. The last *successful* check must
    # survive: "worked an hour ago, failing now" and "never worked" call for
    # different actions.
    svt.results["show-1"] = SvtApiError("gone", status_code=404)
    clock.advance(3600)
    await c.run_once()
    (row,) = c.per_mapping()
    assert row["ok"] is False
    assert row["last_checked"] == clock.now.isoformat()
    assert row["last_success"] == _T0.isoformat()
    assert row["episode_count"] == 2  # what the last good check saw
    assert row["last_error_at"] == clock.now.isoformat()
    assert "404" in row["last_error"]


async def test_a_never_checked_mapping_is_not_reported_as_ok():
    c = _canary([_mapping(1)], FakeSvt())
    (row,) = c.per_mapping()
    assert row["ok"] is None
    assert row["last_checked"] is None
    assert row["last_success"] is None


async def test_a_removed_mapping_stops_being_reported():
    rows = [_mapping(1), _mapping(2)]
    c = _canary(lambda: list(rows), FakeSvt())
    await c.run_once()
    assert len(c.per_mapping()) == 2
    rows.pop()
    await c.run_once()
    assert [r["tvdb_id"] for r in c.per_mapping()] == [1]


async def test_the_failure_list_is_capped_but_the_count_is_not():
    svt = FakeSvt(default=SvtApiError("boom"))
    c = _canary([_mapping(i) for i in range(1, 21)], svt)
    await c.run_once()
    s = c.status()
    assert s["failing"] == 20
    assert len(s["failing_series"]) == 5
    assert s["failing_series_truncated"] is True


# --- A hanging or hostile SVT must not stall anything ----------------------


async def test_a_hanging_svt_is_a_timeout_not_a_stall():
    svt = FakeSvt(default="hang")
    c = _canary([_mapping(1)], svt, probe_timeout_s=0.05)
    await asyncio.wait_for(c.run_once(), timeout=5.0)
    s = c.status()
    assert s["state"] == STATE_SVT
    assert "timed out" in s["last_error"].lower()


async def test_one_hanging_show_does_not_cost_the_others_their_check():
    svt = FakeSvt(results={"show-1": "hang"})
    c = _canary(
        [_mapping(1), _mapping(2), _mapping(3)], svt, probe_timeout_s=0.05
    )
    await asyncio.wait_for(c.run_once(), timeout=5.0)
    s = c.status()
    assert s["state"] == STATE_SERIES
    assert s["failing"] == 1
    assert s["checked"] == 3


async def test_concurrency_is_bounded():
    svt = FakeSvt()
    c = _canary([_mapping(i) for i in range(1, 21)], svt, concurrency=3)
    await c.run_once()
    # N mappings per hour against an unofficial API: the point of the bound
    # is that a large library never presents SVT with a burst.
    assert svt.max_in_flight <= 3


async def test_probes_are_staggered():
    svt = FakeSvt()
    c = _canary([_mapping(i) for i in range(1, 4)], svt, spacing_s=0.05,
                concurrency=3)
    started = asyncio.get_running_loop().time()
    await c.run_once()
    assert asyncio.get_running_loop().time() - started >= 0.09


# --- Nothing the canary does may break the service -------------------------


async def test_a_probe_raising_something_unexpected_does_not_break_the_round():
    svt = FakeSvt(results={"show-1": RuntimeError("not an SvtApiError")})
    c = _canary([_mapping(1), _mapping(2)], svt)
    await c.run_once()  # must not raise
    s = c.status()
    assert s["failing"] == 1
    assert s["checked"] == 2


async def test_an_unreadable_mapping_table_does_not_complete_a_round():
    # "I could not read the mappings" is not "there are no mappings to
    # check". Completing a round here would report no_mappings -- a
    # reassuring state -- for a service that is checking nothing at all.
    def _boom():
        raise RuntimeError("mappings.yaml is on fire")

    c = _canary(_boom, FakeSvt())
    await c.run_once()  # must not raise
    s = c.status()
    assert s["state"] == STATE_UNKNOWN
    assert s["last_checked"] is None
    assert "on fire" in s["last_error"]
    assert s["last_error_at"] == _T0.isoformat()


async def test_run_forever_survives_a_round_that_raises():
    class Exploding(SvtCanary):
        rounds = 0

        async def run_once(self):
            Exploding.rounds += 1
            raise RuntimeError("boom")

    c = Exploding(
        lambda: [_mapping(1)],
        FakeSvt(),
        interval_s=0.01,
        initial_delay_s=0.0,
        clock=Clock(),
    )
    task = asyncio.create_task(c.run_forever())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if Exploding.rounds >= 3:
            break
    assert task.done() is False, "the canary loop died on a failing round"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert Exploding.rounds >= 3


async def test_a_round_is_cancellable_mid_probe():
    # Shutdown cancels this task while a round may be in flight, and the
    # lifespan awaits it before closing the shared HTTP client. A round that
    # swallowed cancellation would hang shutdown; one that leaked it into
    # the round's own error handling would report a false SVT failure on the
    # way out.
    svt = FakeSvt(default="hang")
    c = _canary([_mapping(1), _mapping(2)], svt, probe_timeout_s=30.0)
    task = asyncio.create_task(c.run_once())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # ...and the interrupted round did not become a completed one.
    assert c.status()["state"] == STATE_UNKNOWN


async def test_a_probe_that_escapes_its_own_guard_costs_only_that_probe():
    # `_probe` is written not to raise, so this exercises the net under a bug
    # in it: one probe blowing up must cost that probe, not the round.
    class Leaky(SvtCanary):
        async def _probe(self, mapping, series_index=None, index_error=None):
            if mapping.tvdb_id == 1:
                raise RuntimeError("escaped the guard")
            return True, 3, None, Resolvability()

    c = Leaky(
        lambda: [_mapping(1), _mapping(2)],
        FakeSvt(),
        spacing_s=0.0,
        initial_delay_s=0.0,
        clock=Clock(),
    )
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_SERIES
    assert s["checked"] == 2
    assert s["failing"] == 1
    assert "escaped the guard" in s["failing_series"][0]["error"]


async def test_run_forever_is_cancellable_during_its_initial_delay():
    c = _canary([_mapping(1)], FakeSvt(), initial_delay_s=30.0)
    task = asyncio.create_task(c.run_forever())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_initial_delay_holds_the_first_round_back():
    svt = FakeSvt()
    c = _canary([_mapping(1)], svt, initial_delay_s=30.0, interval_s=30.0)
    task = asyncio.create_task(c.run_forever())
    await asyncio.sleep(0.05)
    assert svt.calls == [], "the canary fired at startup instead of settling"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- Read-only ------------------------------------------------------------


def _tree_digest(root: Path) -> dict[str, tuple[float, str]]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p)] = (
                p.stat().st_mtime,
                hashlib.sha256(p.read_bytes()).hexdigest(),
            )
    return out


async def test_the_canary_writes_nothing(tmp_path: Path):
    # It is observation, not action: it may not touch mappings.yaml,
    # config.yaml or the job store, and it may not call anything on the SVT
    # client except the read-only episode listing (FakeSvt has no other
    # method at all, so a second call fails with AttributeError).
    (tmp_path / "mappings.yaml").write_text(
        "series:\n"
        "  - tvdb_id: 1\n"
        "    svt_series_id: svt1\n"
        "    svt_slug: show-1\n"
        "    series_title: Show 1\n",
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text("sonarr_url: http://x\n", encoding="utf-8")
    (tmp_path / "jobs.db").write_bytes(b"not really a database")
    before = _tree_digest(tmp_path)

    svt = FakeSvt(results={"show-1": SvtApiError("boom")})
    c = _canary([_mapping(1)], svt)
    await c.run_once()
    await c.run_once()

    assert _tree_digest(tmp_path) == before
    assert svt.calls == ["show-1", "show-1"]


# --- The unavailable fallback ---------------------------------------------


def test_unavailable_status_is_degraded_and_not_ok():
    # Used when even reading the canary's own state failed. Unknown for an
    # unknown reason is not a state to report calmly.
    s = unavailable_status()
    assert s["state"] == STATE_UNAVAILABLE
    assert s["degraded"] is True
    assert s["needs_attention"] is True
    assert s["last_checked"] is None


# --- Ages, computed here and nowhere else ------------------------------


async def test_per_mapping_reports_ages_beside_its_instants():
    # "When did this last work" is a question in the present tense. An ISO
    # instant answers it only after the reader holds the current time in
    # their head, works out the timezone and subtracts -- every glance at
    # the table. The instants stay for precision; the ages are what gets
    # rendered.
    clock = Clock()
    svt = FakeSvt(results={"show-1": _episodes(2)})
    c = _canary([_mapping(1)], svt, clock=clock)
    await c.run_once()

    (row,) = c.per_mapping()
    assert row["last_checked_age_s"] == 0.0
    assert row["last_success_age_s"] == 0.0

    clock.advance(3600)
    (row,) = c.per_mapping()
    assert row["last_checked_age_s"] == 3600.0
    assert row["last_success_age_s"] == 3600.0
    # ...and the instants are still there for the cases that want them.
    assert row["last_checked"] == _T0.isoformat()


async def test_a_never_checked_mapping_has_no_age_rather_than_a_zero():
    # Zero would render as "just now", which is the reassuring reading of
    # a row nothing is known about -- the same collapse `ok` is tri-state
    # to avoid.
    c = _canary([_mapping(1)], FakeSvt())

    (row,) = c.per_mapping()
    assert row["last_checked_age_s"] is None
    assert row["last_success_age_s"] is None


async def test_the_per_mapping_age_agrees_with_the_status_ages():
    # The mappings table and the status strip sit one click apart and
    # describe overlapping moments. Both ages come from `_age_s` off the
    # same clock, so they cannot drift; two separate computations would.
    clock = Clock()
    svt = FakeSvt(results={"show-1": _episodes(2)})
    c = _canary([_mapping(1)], svt, clock=clock)
    await c.run_once()
    clock.advance(1234)

    (row,) = c.per_mapping()
    status = c.status()

    assert row["last_success_age_s"] == status["last_success_age_s"] == 1234.0
    assert row["last_checked_age_s"] == status["last_checked_age_s"] == 1234.0


async def test_an_error_carries_an_age_too():
    clock = Clock()
    svt = FakeSvt(default=SvtApiError("gone", status_code=404))
    c = _canary([_mapping(1)], svt, clock=clock)
    await c.run_once()
    clock.advance(60)

    (row,) = c.per_mapping()
    assert row["last_error_age_s"] == 60.0
    assert row["last_error_at"] == _T0.isoformat()


async def test_a_clock_step_backwards_never_produces_a_negative_age():
    # Rendered, a negative age reads as "-3 minutes ago". Clamped in
    # `_age_s`, which is the one place ages are computed -- so this holds
    # for the per-mapping rows without a second guard.
    clock = Clock()
    svt = FakeSvt(results={"show-1": _episodes(1)})
    c = _canary([_mapping(1)], svt, clock=clock)
    await c.run_once()
    clock.advance(-600)

    (row,) = c.per_mapping()
    assert row["last_checked_age_s"] == 0.0


# --- The Sonarr check -------------------------------------------------------
#
# A sibling class, not a second mode of SvtCanary: see the module docstring
# in canary.py. These are about the check in isolation; its wiring into
# `/health` and the status strip is exercised in test_app.py, where the
# "one computation, two surfaces" property actually lives.


class FakeSonarr:
    """Stands in for `SonarrClient`, offering *only* `status()`.

    Every other method is absent by construction, so a check that ever grew
    a second Sonarr call -- or reached for anything that writes -- fails
    with an AttributeError rather than quietly doing more than it claims.
    """

    def __init__(self, result=None, error=None, hang=False):
        self.result = result
        self.error = error
        self.hang = hang
        self.calls = 0

    async def status(self):
        self.calls += 1
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error
        return self.result or SonarrStatus(version="4.0.10.2544", series_count=42)


def _sonarr_canary(sonarr, clock=None, **over):
    kwargs = {"initial_delay_s": 0.0, "clock": clock or Clock()}
    kwargs.update(over)
    return SonarrCanary(sonarr, **kwargs)


def test_a_sonarr_check_that_has_never_run_is_not_reported_as_healthy():
    # The defect this whole feature removes, one level up: a fresh process
    # has proved nothing about Sonarr, and saying "ok" would be a claim it
    # cannot make.
    s = _sonarr_canary(FakeSonarr()).status()
    assert s["state"] == STATE_UNKNOWN
    assert s["state"] != STATE_OK
    assert s["last_checked"] is None
    assert s["last_success"] is None
    assert s["version"] is None
    assert s["series_count"] is None


def test_a_sonarr_check_that_has_never_run_does_not_cry_wolf_either():
    # Not healthy, and not a degrade for the first interval after a restart
    # -- the same balance DEGRADED_STATES strikes for SVT, and for the same
    # reason: a check that is red on every boot is one nobody reads.
    s = _sonarr_canary(FakeSonarr()).status()
    assert s["degraded"] is False
    assert s["needs_attention"] is False


async def test_a_working_sonarr_reports_its_version_and_series_count():
    c = _sonarr_canary(FakeSonarr())
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_OK
    assert s["degraded"] is False
    assert s["version"] == "4.0.10.2544"
    assert s["series_count"] == 42
    assert s["last_success"] == _T0.isoformat()
    assert s["last_error"] is None


async def test_a_sonarr_that_is_down_degrades_the_service():
    # The decision the brief turns on, and the one place this differs from
    # STATE_SERIES: Sonarr has no partial failure. Nothing resolves, nothing
    # is grabbed, and no search returns anything.
    c = _sonarr_canary(FakeSonarr(error=_sonarr_error(REASON_REFUSED)))
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_SONARR
    assert s["degraded"] is True
    assert s["needs_attention"] is True
    assert STATE_SONARR in SONARR_DEGRADED_STATES


async def test_the_reason_reaches_the_report_as_well_as_the_sentence():
    # A monitoring setup must be able to branch on "the key was rejected"
    # without matching on prose, and the operator needs the prose.
    c = _sonarr_canary(FakeSonarr(error=_sonarr_error(REASON_UNAUTHORIZED)))
    await c.run_once()
    s = c.status()
    assert s["last_error_reason"] == REASON_UNAUTHORIZED
    assert s["last_error"] == REASON_MESSAGES[REASON_UNAUTHORIZED]
    assert s["last_error_at"] == _T0.isoformat()


async def test_every_failure_shape_arrives_distinguishably():
    # Each of these sends the operator somewhere completely different, and
    # collapsing any two of them into "Sonarr could not be reached" is what
    # makes the report worthless.
    for reason in (
        REASON_BAD_URL, REASON_UNREACHABLE, REASON_REFUSED, REASON_TLS,
        REASON_UNAUTHORIZED, REASON_NOT_SONARR, REASON_HTTP,
    ):
        c = _sonarr_canary(FakeSonarr(error=_sonarr_error(reason)))
        await c.run_once()
        s = c.status()
        assert s["last_error_reason"] == reason, reason
        assert s["state"] == STATE_SONARR, reason


async def test_a_failure_keeps_what_the_last_working_check_saw():
    # "Answered an hour ago with 42 series, failing now" and "never
    # answered" are different situations and only one of them means the
    # settings were always wrong.
    clock = Clock()
    sonarr = FakeSonarr()
    c = _sonarr_canary(sonarr, clock=clock)
    await c.run_once()
    clock.advance(3600)
    sonarr.error = _sonarr_error(REASON_REFUSED)
    await c.run_once()

    s = c.status()
    assert s["state"] == STATE_SONARR
    assert s["version"] == "4.0.10.2544"
    assert s["series_count"] == 42
    assert s["last_success"] == _T0.isoformat()
    assert s["last_success_age_s"] == 3600.0


async def test_a_failing_check_is_never_reported_as_a_stalled_one():
    # A failure completes a round. If it did not, a check that is running
    # perfectly and finding a real problem would drift to `stale` and start
    # claiming nothing is checking Sonarr at all.
    clock = Clock()
    c = _sonarr_canary(
        FakeSonarr(error=_sonarr_error(REASON_REFUSED)), clock=clock
    )
    await c.run_once()
    clock.advance(60)
    assert c.status()["state"] == STATE_SONARR


async def test_a_check_that_stops_completing_goes_stale_and_degrades():
    clock = Clock()
    c = _sonarr_canary(FakeSonarr(), clock=clock, interval_s=3600.0)
    await c.run_once()
    assert c.status()["state"] == STATE_OK
    clock.advance(3 * 3600.0 + 1)
    s = c.status()
    assert s["state"] == STATE_STALE
    assert s["degraded"] is True
    # ...and the last confirmed success is still reported, because "Sonarr
    # worked at 09:00 and nothing has checked since" is a different sentence
    # from "Sonarr is broken".
    assert s["last_success"] == _T0.isoformat()


async def test_a_hanging_sonarr_costs_the_round_its_timeout_and_no_more():
    # A Sonarr that accepts the connection and then says nothing must not be
    # able to wedge the loop the download worker and every route share.
    c = _sonarr_canary(FakeSonarr(hang=True), probe_timeout_s=0.01)
    await asyncio.wait_for(c.run_once(), timeout=2.0)
    s = c.status()
    assert s["state"] == STATE_SONARR
    assert s["last_error_reason"] == "timeout"


async def test_a_round_that_raises_something_unexpected_costs_only_that_round():
    class Exploding:
        async def status(self):
            raise RuntimeError("boom")

    c = _sonarr_canary(Exploding())
    await c.run_once_guarded()
    s = c.status()
    assert s["state"] == STATE_SONARR
    assert s["last_error_reason"] == "unknown"
    # This module's own words, not the exception's: an unexpected type must
    # not be able to smuggle its message onto a rendered page.
    assert "boom" not in s["last_error"]


async def test_the_sonarr_check_never_reports_the_api_key():
    # The constraint the feature turns on. Every path: success, every
    # classified failure, a timeout, and an unexpected exception that
    # happens to be carrying the key in its own message.
    key = "sekrit-sonarr-api-key"

    class Leaky:
        async def status(self):
            raise RuntimeError(f"X-Api-Key: {key}")

    checks = [_sonarr_canary(FakeSonarr()), _sonarr_canary(Leaky()),
              _sonarr_canary(FakeSonarr(hang=True), probe_timeout_s=0.01)]
    checks += [
        _sonarr_canary(FakeSonarr(error=_sonarr_error(r)))
        for r in REASON_MESSAGES
    ]
    for c in checks:
        await c.run_once_guarded()
        assert key not in repr(c.status())


async def test_the_sonarr_check_calls_nothing_but_status():
    sonarr = FakeSonarr()
    c = _sonarr_canary(sonarr)
    await c.run_once()
    await c.run_once()
    assert sonarr.calls == 2


def test_the_unavailable_report_is_degraded_and_claims_nothing():
    s = sonarr_unavailable_status()
    assert s["state"] == STATE_UNAVAILABLE
    assert s["degraded"] is True
    assert s["needs_attention"] is True
    assert s["version"] is None
    assert s["series_count"] is None
    # Same keys as a real report, so nothing rendering it has to branch on
    # which of the two it was handed.
    assert set(s) == set(_sonarr_canary(FakeSonarr()).status())


async def test_run_forever_survives_a_round_that_raises():
    class Exploding:
        def __init__(self):
            self.calls = 0

        async def status(self):
            self.calls += 1
            raise RuntimeError("boom")

    sonarr = Exploding()
    c = _sonarr_canary(sonarr, interval_s=0.01)
    task = asyncio.create_task(c.run_forever())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if sonarr.calls >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sonarr.calls >= 2, "the loop died on the first failing round"


# --- The mapping that resolves nothing -------------------------------------
#
# The failure this half of the check exists for is one the SVT half cannot
# see, by construction. `uppdrag-granskning` is the live example: a correct
# slug, a 200, a full list of 61 episodes -- and not one of them carries an
# ordinal, so `matching.episode_matches` signal 2 refuses every one and the
# mapping has never grabbed anything. To the SVT probe that is a perfect
# pass, and to an operator it is indistinguishable from a series between
# seasons.
#
# The condition is deliberately narrow. A false alarm here trains the
# operator to ignore the one signal that would catch a real problem, which
# is the mistake this project has already shipped twice -- the installer's
# fresh-install warning and the canary's own ended-show case. So the three
# not-broken shapes below are pinned first, and each is mutation-checked:
# every one must fail if it starts alarming.


def _sonarr_episode(season: int, episode: int, air_date: date | None):
    return SonarrEpisode(
        series_id=0, season=season, episode=episode, air_date=air_date,
        title="TBA",
    )


def _aired(n: int, *, first: date = date(2026, 8, 20), season: int = 1):
    """`n` Sonarr episodes, one a day, ending on `first`.

    Dated in the past of `_T0` so they count as aired against the canary's
    own clock, and numbered from 1 so they line up with `_episodes(n)`.
    """
    return [
        _sonarr_episode(season, i, first - timedelta(days=n - i))
        for i in range(1, n + 1)
    ]


class FakeSonarrLibrary:
    """Stands in for `SonarrClient`, offering *only* `all_series` and
    `episodes`.

    Both are read-only GETs. Every other method -- `status`, and anything
    that could write -- is absent by construction, so a check that grew a
    second Sonarr call, or reached for a write path, fails these tests with
    an AttributeError rather than quietly costing more than the arithmetic
    in the docs claims.
    """

    def __init__(self, library=None, *, series_error=None,
                 episodes_error=None, hang=False):
        # {tvdb_id: [SonarrEpisode, ...]}
        self.library = dict(library or {})
        self.series_error = series_error
        self.episodes_error = episodes_error
        self.hang = hang
        self.series_calls = 0
        self.episode_calls: list[int] = []

    def _series_id(self, tvdb_id: int) -> int:
        return 1000 + tvdb_id

    async def all_series(self):
        self.series_calls += 1
        if self.series_error is not None:
            raise self.series_error
        await asyncio.sleep(0)
        return [
            {"tvdbId": t, "id": self._series_id(t)} for t in sorted(self.library)
        ]

    async def episodes(self, series_id: int):
        self.episode_calls.append(series_id)
        if self.hang:
            await asyncio.Event().wait()  # never returns
        if self.episodes_error is not None:
            raise self.episodes_error
        await asyncio.sleep(0)
        for tvdb_id, eps in self.library.items():
            if self._series_id(tvdb_id) == series_id:
                return list(eps)
        return []


def _row(canary, tvdb_id: int = 1) -> dict:
    return next(r for r in canary.per_mapping() if r["tvdb_id"] == tvdb_id)


# --- ...and the three shapes that are not it -------------------------------


async def test_a_mapping_whose_episodes_match_is_not_flagged():
    svt = FakeSvt(results={"show-1": _episodes(3)})
    sonarr = FakeSonarrLibrary({1: _aired(3)})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    s = c.status()
    assert s["state"] == STATE_OK
    assert s["unresolvable"] == 0
    assert s["unresolvable_series"] == []
    assert _row(c)["resolves"] is True


async def test_a_series_with_nothing_aired_in_sonarr_is_not_flagged():
    # A newly added series. Sonarr knows the season and has dated every
    # episode of it, but none has aired, so nothing can match -- and
    # nothing being able to match yet is not a broken mapping.
    svt = FakeSvt(results={"show-1": _episodes(3)})
    future = [
        _sonarr_episode(1, i, _T0.date() + timedelta(days=i)) for i in (1, 2, 3)
    ]
    sonarr = FakeSonarrLibrary({1: future})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    s = c.status()
    assert s["state"] == STATE_OK
    assert s["unresolvable"] == 0
    row = _row(c)
    assert row["resolves"] is None
    assert row["unresolvable_reason"] is None


async def test_a_series_sonarr_has_no_episodes_for_at_all_is_not_flagged():
    svt = FakeSvt(results={"show-1": _episodes(3)})
    sonarr = FakeSonarrLibrary({1: []})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    assert c.status()["unresolvable"] == 0
    assert _row(c)["resolves"] is None


async def test_a_show_whose_episodes_are_all_upcoming_is_not_flagged():
    # Every SVT episode is flagged upcoming, so none is downloadable yet.
    # Nothing can be grabbed, and nothing being grabbable yet is not a
    # mapping that can never work.
    upcoming = [replace(e, available=False) for e in _episodes(3)]
    svt = FakeSvt(results={"show-1": upcoming})
    sonarr = FakeSonarrLibrary({1: _aired(3)})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    assert c.status()["unresolvable"] == 0
    assert _row(c)["resolves"] is None


# --- ...and the shape that is ----------------------------------------------


async def test_available_episodes_aired_episodes_and_no_pair_matching_is_flagged():
    svt = FakeSvt(results={"show-1": _episodes(3)})
    # Same episode numbers, a year apart: both sides have real content and
    # no pair of them can ever agree.
    sonarr = FakeSonarrLibrary({1: _aired(3, first=date(2025, 8, 20))})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    s = c.status()
    assert s["state"] == STATE_UNRESOLVABLE
    assert s["unresolvable"] == 1
    assert s["unresolvable_series"][0]["tvdb_id"] == 1
    assert s["unresolvable_series"][0]["reason"] == UNRESOLVABLE_NO_AIR_DATE
    assert _row(c)["resolves"] is False


async def test_uppdrag_granskning_is_flagged_and_blames_the_missing_ordinals():
    """The show this half of the check exists for, on its own capture.

    61 episodes, a healthy 200, and `_ordinal` returns None for every one
    of them because the titles encode no number -- see
    `docs/design/2026-08-28-svt-episode-ordinals.md` for why that cannot be
    fixed. The reason matters as much as the finding: no ordinal anywhere
    means the mapping cannot be made to work, which sends the operator
    somewhere completely different from "the dates disagree".
    """
    episodes = _captured("uppdrag-granskning")
    assert len(episodes) == 61 and all(e.ordinal is None for e in episodes)

    svt = FakeSvt(results={"show-1": episodes})
    sonarr = FakeSonarrLibrary({1: _aired(20, first=date(2026, 6, 1))})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    s = c.status()
    assert s["state"] == STATE_UNRESOLVABLE
    assert s["unresolvable"] == 1
    assert s["unresolvable_series"][0]["reason"] == UNRESOLVABLE_NO_ORDINALS
    # ...and the SVT half is untouched: the slug resolves, 61 episodes.
    assert s["failing"] == 0
    row = _row(c)
    assert row["ok"] is True
    assert row["episode_count"] == 61
    assert row["resolves"] is False


async def test_the_two_reasons_are_distinguished():
    # They send the operator to different places -- one is unfixable and
    # means removing the mapping or accepting it, the other suggests a
    # wrong mapping or a tolerance too tight -- so they must never share a
    # sentence.
    svt = FakeSvt(results={
        "show-1": _captured("uppdrag-granskning"),
        "show-2": _episodes(3),
    })
    sonarr = FakeSonarrLibrary({
        1: _aired(20, first=date(2026, 6, 1)),
        2: _aired(3, first=date(2025, 8, 20)),
    })
    c = _canary([_mapping(1), _mapping(2)], svt, sonarr=sonarr,
                tolerance_days=1)
    await c.run_once()

    reasons = {
        u["tvdb_id"]: u["reason"] for u in c.status()["unresolvable_series"]
    }
    assert reasons == {1: UNRESOLVABLE_NO_ORDINALS, 2: UNRESOLVABLE_NO_AIR_DATE}
    notes = {r["tvdb_id"]: r["resolvability_note"] for r in c.per_mapping()}
    assert notes[1] != notes[2]
    assert all(n for n in notes.values())


async def test_a_series_missing_from_sonarr_is_its_own_finding():
    """The tvdb id in mappings.yaml is not in Sonarr's library.

    Dead in exactly the way an ended show is dead: it will never resolve,
    `Resolver.resolve` gives up on it before reaching a matching rule, and
    until now nothing said so. Its own reason, not one of the other two,
    because the remedy is different in kind -- those are about the episode
    data, this is about the row pointing at nothing.
    """
    svt = FakeSvt(results={"show-1": _episodes(3)})
    sonarr = FakeSonarrLibrary({99: _aired(3)})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    s = c.status()
    assert s["state"] == STATE_UNRESOLVABLE
    assert s["unresolvable"] == 1
    assert s["unresolvable_series"][0]["reason"] == UNRESOLVABLE_NOT_IN_SONARR
    # Amber, like the rest of this finding: one row is dead and the
    # operator fixes it in one action.
    assert s["needs_attention"] is True
    assert s["degraded"] is False
    row = _row(c)
    assert row["resolves"] is False
    assert "tvdb id 1" in row["resolvability_note"]


async def test_a_missing_series_is_not_folded_into_the_other_two_reasons():
    # The remedy differs -- remove the row or re-add the series in Sonarr,
    # rather than anything about ordinals or air dates -- so reporting it
    # under either of those would send the operator to the wrong place.
    svt = FakeSvt(results={
        "show-1": _captured("uppdrag-granskning"),
        "show-2": _episodes(3),
        "show-3": _episodes(3),
    })
    sonarr = FakeSonarrLibrary({
        1: _aired(20, first=date(2026, 6, 1)),
        2: _aired(3, first=date(2025, 8, 20)),
    })
    c = _canary([_mapping(1), _mapping(2), _mapping(3)], svt, sonarr=sonarr,
                tolerance_days=1)
    await c.run_once()

    reasons = {
        u["tvdb_id"]: u["reason"] for u in c.status()["unresolvable_series"]
    }
    assert reasons == {
        1: UNRESOLVABLE_NO_ORDINALS,
        2: UNRESOLVABLE_NO_AIR_DATE,
        3: UNRESOLVABLE_NOT_IN_SONARR,
    }
    notes = {r["tvdb_id"]: r["resolvability_note"] for r in c.per_mapping()}
    assert len(set(notes.values())) == 3


async def test_a_missing_series_is_reported_even_when_svt_has_nothing_out_yet():
    # The exclusions are about "nothing to compare *yet*"; this row has
    # nothing to compare against ever, whatever SVT is currently offering.
    upcoming = [replace(e, available=False) for e in _episodes(3)]
    svt = FakeSvt(results={"show-1": upcoming})
    sonarr = FakeSonarrLibrary({99: _aired(3)})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    s = c.status()
    assert s["unresolvable"] == 1
    assert s["unresolvable_series"][0]["reason"] == UNRESOLVABLE_NOT_IN_SONARR


async def test_a_tolerance_wide_enough_to_match_clears_the_finding():
    # The date reason really is about the tolerance and the dates, and not
    # about anything else: widen it and the same two lists agree.
    svt = FakeSvt(results={"show-1": _episodes(3)})
    sonarr = FakeSonarrLibrary({1: _aired(3, first=date(2026, 8, 24))})
    tight = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=0)
    await tight.run_once()
    assert tight.status()["unresolvable"] == 1

    loose = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=5)
    await loose.run_once()
    assert loose.status()["unresolvable"] == 0


async def test_a_mapping_that_starts_matching_again_stops_being_flagged():
    # The amber must clear on its own, or it becomes the permanent noise
    # this check is built to avoid.
    svt = FakeSvt(results={"show-1": _episodes(3)})
    sonarr = FakeSonarrLibrary({1: _aired(3, first=date(2025, 8, 20))})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()
    assert c.status()["needs_attention"] is True

    sonarr.library[1] = _aired(3)
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_OK
    assert s["unresolvable"] == 0
    assert s["needs_attention"] is False


# --- What it is worth ------------------------------------------------------


async def test_the_finding_asks_for_attention_without_turning_the_light_red():
    # One show, actionable but not urgent: the rest of the feed works. Same
    # urgency as the ended-show case, and for the same reason -- see
    # DEGRADED_STATES.
    svt = FakeSvt(results={"show-1": _episodes(3)})
    sonarr = FakeSonarrLibrary({1: _aired(3, first=date(2025, 8, 20))})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    s = c.status()
    assert s["needs_attention"] is True
    assert s["degraded"] is False
    assert STATE_UNRESOLVABLE in ATTENTION_STATES
    assert STATE_UNRESOLVABLE not in DEGRADED_STATES


async def test_a_failing_svt_row_still_wins_the_headline():
    # A row that does not resolve on SVT at all is the louder finding, and
    # the unresolvable rows are still reported beside it rather than lost.
    svt = FakeSvt(results={
        "show-1": SvtApiError("404", status_code=404),
        "show-2": _episodes(3),
    })
    sonarr = FakeSonarrLibrary({
        1: _aired(3), 2: _aired(3, first=date(2025, 8, 20)),
    })
    c = _canary([_mapping(1), _mapping(2)], svt, sonarr=sonarr,
                tolerance_days=1)
    await c.run_once()

    s = c.status()
    assert s["state"] == STATE_SERIES
    assert s["unresolvable"] == 1
    assert [u["tvdb_id"] for u in s["unresolvable_series"]] == [2]


# --- Sonarr failing degrades this check, and only this check ---------------


async def test_a_sonarr_outage_leaves_resolvability_unknown_not_healthy():
    svt = FakeSvt(results={"show-1": _episodes(3)})
    sonarr = FakeSonarrLibrary(
        {1: _aired(3)}, series_error=_sonarr_error(REASON_REFUSED)
    )
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    s = c.status()
    # The SVT half is untouched: the slug still resolves.
    assert s["state"] == STATE_OK
    assert s["failing"] == 0
    # ...and the other half says so rather than reporting a clean sweep.
    assert s["unresolvable"] == 0
    assert s["resolvability_unknown"] == 1
    assert s["resolvability_error"] == REASON_MESSAGES[REASON_REFUSED]
    row = _row(c)
    assert row["resolves"] is None
    assert row["resolvability_note"]


async def test_a_sonarr_that_fails_only_the_episode_call_degrades_that_row():
    svt = FakeSvt(results={"show-1": _episodes(3), "show-2": _episodes(3)})
    sonarr = FakeSonarrLibrary(
        {1: _aired(3), 2: _aired(3)},
        episodes_error=_sonarr_error(REASON_UNAUTHORIZED),
    )
    c = _canary([_mapping(1), _mapping(2)], svt, sonarr=sonarr,
                tolerance_days=1)
    await c.run_once()

    s = c.status()
    assert s["state"] == STATE_OK
    assert s["resolvability_unknown"] == 2
    assert s["unresolvable"] == 0


async def test_a_hanging_sonarr_costs_one_probe_and_not_the_round():
    svt = FakeSvt(results={"show-1": _episodes(3)})
    sonarr = FakeSonarrLibrary({1: _aired(3)}, hang=True)
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1,
                probe_timeout_s=0.01)
    await asyncio.wait_for(c.run_once(), timeout=5.0)

    s = c.status()
    assert s["state"] == STATE_OK
    assert s["resolvability_unknown"] == 1
    assert s["unresolvable"] == 0


async def test_a_sonarr_that_raises_something_unexpected_says_this_modules_words():
    key = "sekrit-sonarr-api-key"

    class Leaky:
        async def all_series(self):
            raise RuntimeError(f"X-Api-Key: {key}")

    svt = FakeSvt(results={"show-1": _episodes(3)})
    c = _canary([_mapping(1)], svt, sonarr=Leaky(), tolerance_days=1)
    await c.run_once_guarded()

    s = c.status()
    assert s["resolvability_unknown"] == 1
    assert key not in repr(s)
    assert key not in repr(c.per_mapping())


async def test_no_sonarr_client_leaves_resolvability_undetermined():
    # Every mapping resolves on SVT and nothing is claimed about matching.
    svt = FakeSvt(results={"show-1": _episodes(3)})
    c = _canary([_mapping(1)], svt)
    await c.run_once()

    s = c.status()
    assert s["state"] == STATE_OK
    assert s["unresolvable"] == 0
    assert _row(c)["resolves"] is None


# --- What it costs, and that it costs nothing else -------------------------


async def test_one_sonarr_episode_call_per_mapping_and_one_library_read():
    svt = FakeSvt(results={f"show-{i}": _episodes(3) for i in (1, 2, 3)})
    sonarr = FakeSonarrLibrary({1: _aired(3), 2: _aired(3), 3: _aired(3)})
    c = _canary([_mapping(1), _mapping(2), _mapping(3)], svt, sonarr=sonarr,
                tolerance_days=1)
    await c.run_once()

    assert sonarr.series_calls == 1, "the library was read once per mapping"
    assert sorted(sonarr.episode_calls) == [1001, 1002, 1003]

    await c.run_once()
    assert sonarr.series_calls == 2
    assert len(sonarr.episode_calls) == 6


async def test_an_svt_failure_costs_no_sonarr_episode_call():
    # Nothing can be concluded about matching without SVT's list, so the
    # request is not made.
    svt = FakeSvt(default=SvtApiError("boom"))
    sonarr = FakeSonarrLibrary({1: _aired(3)})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()

    assert sonarr.episode_calls == []


async def test_the_resolvability_check_writes_nothing(tmp_path: Path):
    # Same guarantee as the SVT half, now with Sonarr in the round:
    # `FakeSonarrLibrary` offers only the two read-only GETs, so any write
    # path fails with an AttributeError.
    (tmp_path / "mappings.yaml").write_text("series: []\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("sonarr_url: http://x\n", encoding="utf-8")
    (tmp_path / "jobs.db").write_bytes(b"not really a database")
    before = _tree_digest(tmp_path)

    svt = FakeSvt(results={"show-1": _captured("uppdrag-granskning")})
    sonarr = FakeSonarrLibrary({1: _aired(20, first=date(2026, 6, 1))})
    c = _canary([_mapping(1)], svt, sonarr=sonarr, tolerance_days=1)
    await c.run_once()
    await c.run_once()

    assert c.status()["unresolvable"] == 1
    assert _tree_digest(tmp_path) == before


def test_the_unavailable_report_carries_the_resolvability_keys_too():
    # Same keys as a real report, so nothing rendering it has to branch on
    # which of the two it was handed -- and none of them claims a clean
    # sweep.
    real = _canary([_mapping(1)], FakeSvt()).status()
    s = unavailable_status()
    assert set(s) == set(real)
    assert s["unresolvable"] is None
    assert s["unresolvable_series"] == []
    assert s["resolvability_unknown"] is None
