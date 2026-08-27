"""Per-mapping SVT check state in the Mappings view.

The canary already records, per mapping, when it was last checked, when it
last succeeded, how many episodes it saw and the last error. Nothing
rendered any of it: to find out that one row had stopped resolving you had
to suspect it first and press Check. This puts it in front of the operator
on arrival, so Check becomes a re-check rather than the only way to know.

The two markers on a row mean different things and must stay
distinguishable. `Auto-matched` says *nobody confirmed this mapping is the
right show*; the canary state says *this mapping stopped resolving on SVT*.
Both can be true of the same row at once, and neither implies the other.
"""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from svtplay_arr.api.config_ui import build_config_router
from svtplay_arr.mappings import add_mapping

TITLE = "Gift vid första ögonkastet"
TVDB = 288649


class FakeSvt:
    def __init__(self):
        self.list_episodes_calls: list[str] = []

    async def search_series(self, query):
        return []

    async def list_episodes(self, slug):
        self.list_episodes_calls.append(slug)
        return []


class FakeSonarr:
    async def all_series(self):
        return []


def _paths(tmp_path: Path):
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        "sonarr_api_key: SECRET-KEY-VALUE\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=TVDB, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    return cfg, maps


def _state(**over) -> dict:
    row = {
        "tvdb_id": TVDB,
        "series_title": TITLE,
        "svt_slug": "gift-vid-forsta-ogonkastet",
        "ok": True,
        "last_checked": "2026-08-26T18:00:00+00:00",
        "last_success": "2026-08-26T18:00:00+00:00",
        "episode_count": 12,
        "last_error": None,
        "last_error_at": None,
    }
    row.update(over)
    return row


def _client(tmp_path: Path, states=None, svt=None, **kwargs) -> TestClient:
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, svt or FakeSvt(), FakeSonarr(),
            mapping_state_provider=(lambda: list(states)) if states is not None
            else None,
            **kwargs,
        )
    )
    return TestClient(app)


def _row(html: str) -> str:
    """The mapping's own table row, isolated.

    Asserting on the whole body would pass on a word rendered in the
    explanatory note beneath the table, or in a banner, rather than
    against the row it is supposed to describe.
    """
    rows = re.findall(r'<tr class="mapping-row".*?</tr>', html, re.S)
    assert len(rows) == 1, f"expected one mapping row, found {len(rows)}"
    return rows[0]


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


# --- A dead mapping is visible without pressing Check ------------------


def test_a_failing_mapping_is_visible_without_pressing_check(tmp_path: Path):
    # The whole point. Before this, a row that had stopped resolving looked
    # exactly like one that was fine, and the only way to find out was to
    # suspect it and press Check.
    svt = FakeSvt()
    body = _client(
        tmp_path,
        states=[_state(
            ok=False,
            last_error="SVT has nothing at slug 'gift-vid-forsta-ogonkastet' "
                       "(404 not found) -- the show may have ended",
            episode_count=None,
        )],
        svt=svt,
    ).get("/config/mappings").text

    assert "404 not found" in body
    # ...and nothing on this page called SVT to find that out. The state is
    # the canary's, already collected on its own slow loop; a page render
    # that fired a request per mapping would be a new way to hammer SVT.
    assert svt.list_episodes_calls == []


def test_a_resolving_mapping_says_so_with_what_it_saw(tmp_path: Path):
    row = _row(
        _client(tmp_path, states=[_state(ok=True, episode_count=12)])
        .get("/config/mappings").text
    )

    assert "12" in row
    assert "episode" in _text(row).lower()


def test_a_mapping_the_canary_has_not_reached_is_not_called_ok(tmp_path: Path):
    # `ok` is tri-state in the canary for exactly this reason: "not checked
    # since this process started" is not the same claim as "checked and
    # fine", and rendering them the same way is the defect the canary
    # exists to avoid, one level up.
    row = _text(_row(
        _client(tmp_path, states=[_state(ok=None, episode_count=None,
                                         last_checked=None, last_success=None)])
        .get("/config/mappings").text
    ))

    assert "Not checked" in row
    assert "Resolves" not in row
    assert "Failing" not in row


def test_a_failing_mapping_with_no_recorded_error_still_reads_as_failing(
    tmp_path: Path,
):
    row = _text(_row(
        _client(tmp_path, states=[_state(ok=False, last_error=None)])
        .get("/config/mappings").text
    ))

    assert "Failing" in row


def test_a_mapping_that_worked_before_it_broke_says_when(tmp_path: Path):
    # "Worked an hour ago, failing now" and "never worked" call for
    # different actions -- the canary keeps both timestamps precisely so
    # this row can tell them apart.
    row = _text(_row(
        _client(tmp_path, states=[_state(
            ok=False, last_error="SVT timed out",
            last_success="2026-08-26T09:15:00+00:00",
        )]).get("/config/mappings").text
    ))

    assert "2026-08-26T09:15:00+00:00" in row


# --- Unknown state is never rendered as healthy -----------------------


def test_a_state_provider_that_raises_leaves_the_page_rendering(tmp_path: Path):
    def _boom():
        raise RuntimeError("the canary is on fire")

    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(), mapping_state_provider=_boom
        )
    )
    r = TestClient(app).get("/config/mappings")
    row = _text(_row(r.text))

    assert r.status_code == 200
    # The table is still usable...
    assert TITLE in r.text
    # ...and no row claims to be fine.
    assert "Resolves" not in row
    assert "Unknown" in row
    # ...and it says the state could not be read, rather than that nothing
    # is reporting one. Same distinction as everywhere else on this page:
    # a read that failed is not an absence of findings, and only one of
    # the two is worth looking in the log for.
    assert "could not be read" in row


def test_no_state_provider_does_not_claim_every_mapping_resolves(
    tmp_path: Path,
):
    row = _text(_row(_client(tmp_path, states=None).get("/config/mappings").text))

    assert "Resolves" not in row
    assert "Unknown" in row
    # ...and it does not claim a read failed either. Nothing was asked.
    assert "could not be read" not in row


def test_a_mapping_the_provider_says_nothing_about_is_not_called_ok(
    tmp_path: Path,
):
    # The canary reports a row for every current mapping, including ones it
    # has not reached. If it ever stops doing that, the row must fall back
    # to "unknown" rather than to silence, which reads as "fine".
    row = _text(_row(_client(tmp_path, states=[]).get("/config/mappings").text))

    assert "Resolves" not in row
    assert "Unknown" in row


# --- Two markers, two meanings ----------------------------------------


def test_auto_matched_and_the_canary_state_are_separate_signals(
    tmp_path: Path,
):
    # One says nobody confirmed this row is the right show; the other says
    # this row stopped resolving. They are independent, and a row can carry
    # both. Collapsing them would lose the difference between "might be the
    # wrong show" and "is no longer on SVT".
    cfg, maps = _paths(tmp_path)
    maps.write_text(
        "series:\n"
        f"  - tvdb_id: {TVDB}\n"
        "    svt_series_id: jpmQD3q\n"
        "    svt_slug: gift-vid-forsta-ogonkastet\n"
        f"    series_title: {TITLE}\n"
        "    source: auto\n",
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            mapping_state_provider=lambda: [_state(ok=False,
                                                   last_error="SVT timed out")],
        )
    )
    row = _text(_row(TestClient(app).get("/config/mappings").text))

    assert "Auto-matched" in row
    assert "Failing" in row


def test_a_confirmed_row_that_resolves_carries_neither_marker(tmp_path: Path):
    row = _text(_row(
        _client(tmp_path, states=[_state(ok=True)]).get("/config/mappings").text
    ))

    assert "Auto-matched" not in row
    assert "Failing" not in row


# --- The Status view points at it -------------------------------------


def test_the_status_view_says_how_many_mappings_are_failing(tmp_path: Path):
    body = _text(
        _client(tmp_path, states=[_state(ok=False, last_error="SVT timed out")])
        .get("/config").text
    )

    assert "1 of them" in body or "1 of these" in body
    assert "not resolving" in body


def test_the_status_view_says_nothing_when_every_mapping_resolves(
    tmp_path: Path,
):
    body = _text(_client(tmp_path, states=[_state(ok=True)]).get("/config").text)

    assert "not resolving" not in body


# --- Check is still the only thing that calls SVT ---------------------


@pytest.mark.parametrize("path", ["/config", "/config/mappings"])
def test_rendering_a_view_never_calls_svt(tmp_path: Path, path: str):
    svt = FakeSvt()
    _client(tmp_path, states=[_state(ok=False, last_error="x")], svt=svt).get(path)

    assert svt.list_episodes_calls == []
