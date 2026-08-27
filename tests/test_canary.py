"""The SVT canary: does SVT still answer, does the parser still work, and
do the operator's own mappings still resolve?

These tests are about the canary in isolation. Its wiring into the app --
the background task, `/health`, and the config page's status strip -- is
exercised in `test_app.py` (`compute_health` end to end) and
`test_config_ui.py` (rendering), because that is where the "one
computation, two surfaces" property actually lives.
"""

import asyncio
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from svtplay_arr.canary import (
    STATE_NO_MAPPINGS,
    STATE_OK,
    STATE_SERIES,
    STATE_STALE,
    STATE_SVT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    SvtCanary,
    unavailable_status,
)
from svtplay_arr.models import Mapping, SvtEpisode
from svtplay_arr.svt.client import SvtApiError

_T0 = datetime(2026, 8, 27, 9, 0, 0, tzinfo=timezone.utc)


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
    assert s["checked"] == 3
    assert s["failing"] == 3


async def test_one_mapping_failing_reports_the_per_show_shape():
    svt = FakeSvt(results={"show-2": SvtApiError("404", status_code=404)})
    c = _canary([_mapping(1), _mapping(2), _mapping(3)], svt)
    await c.run_once()
    s = c.status()
    assert s["state"] == STATE_SERIES
    assert s["degraded"] is True
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
    # request still returns 200 and parse_show_page returns []. Counting
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
        async def _probe(self, mapping):
            if mapping.tvdb_id == 1:
                raise RuntimeError("escaped the guard")
            return True, 3, None

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
    assert s["last_checked"] is None
