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
        "last_checked_age_s": 720.0,
        "last_success": "2026-08-26T18:00:00+00:00",
        "last_success_age_s": 720.0,
        "episode_count": 12,
        "last_error": None,
        "last_error_at": None,
        "last_error_age_s": None,
        "resolves": None,
        "unresolvable_reason": None,
        "resolvability_note": None,
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
            last_success=None, last_success_age_s=None,
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
            last_success_age_s=32_400.0,
        )]).get("/config/mappings").text
    ))

    assert "9 hours ago" in row


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


# --- Ages, not instants -----------------------------------------------


def _sample_health(svt: dict) -> dict:
    return {
        "status": "ok", "same_filesystem": True, "worker_alive": True,
        "active_jobs": 0, "mappings": 1, "mappings_ever_loaded": True,
        "mappings_degraded": False, "svt": svt,
    }


def _sample_svt(**over) -> dict:
    svt = {
        "state": "ok", "degraded": False, "needs_attention": False,
        "alive": True, "checked": 1, "failing": 0, "episodes_seen": 12,
        "last_checked": "2026-08-26T18:00:00+00:00",
        "last_checked_age_s": 720.0,
        "last_success": "2026-08-26T18:00:00+00:00",
        "last_success_age_s": 720.0,
        "last_error": None, "last_error_at": None,
        "failing_series": [], "failing_series_truncated": False,
    }
    svt.update(over)
    return svt


def test_the_column_says_how_long_ago_not_when(tmp_path: Path):
    # An ISO instant makes the reader hold the current time in their head,
    # work out the timezone and subtract -- every glance, on a phone,
    # where it also costs the most width in the row. The strip has always
    # rendered an age; the table used to render the instant, one click
    # apart on the same page.
    row = _row(
        _client(tmp_path, states=[_state(ok=True, last_checked_age_s=1_200.0)])
        .get("/config/mappings").text
    )

    assert "20 min ago" in _text(row)
    # The instant is still reachable where precision helps, but it is not
    # what the column reads as.
    assert 'title="2026-08-26T18:00:00+00:00"' in row
    assert ">2026-08-26T18:00:00+00:00<" not in row


def test_a_long_dead_mapping_is_not_measured_in_minutes(tmp_path: Path):
    # The canary's per-mapping state survives for as long as the process
    # does, so "last resolved" can be days old. Rendering that as
    # "4320 min ago" is arithmetic homework, not an answer.
    row = _text(_row(
        _client(tmp_path, states=[_state(
            ok=False, last_error="SVT timed out",
            last_checked_age_s=60.0,
            last_success="2026-08-24T09:00:00+00:00",
            last_success_age_s=259_200.0,
        )]).get("/config/mappings").text
    ))

    assert "3 days ago" in row


@pytest.mark.parametrize(
    "seconds,phrase",
    [
        (5.0, "just now"),
        (1_200.0, "20 min ago"),
        (14_400.0, "4 hours ago"),
        (259_200.0, "3 days ago"),
    ],
)
def test_the_table_and_the_strip_say_the_same_thing_about_one_moment(
    tmp_path: Path, seconds: float, phrase: str
):
    # The strip and this column are one click apart and describe
    # overlapping moments. Two copies of the arithmetic would drift on
    # rounding first and wording second -- "4 hours" beside "240 min"
    # about the same instant is worse than either alone. One formatter,
    # asserted from both ends.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(
                _sample_svt(last_success_age_s=seconds)
            ),
            mapping_state_provider=lambda: [_state(
                ok=False, last_error="SVT timed out",
                last_success="2026-08-26T18:00:00+00:00",
                last_success_age_s=seconds,
            )],
        )
    )
    body = TestClient(app).get("/config/mappings").text
    strip = _text(body[body.index('class="status-strip"'):body.index("</div>", body.index('class="status-strip"'))])

    assert f"confirmed {phrase}" in strip, strip
    assert f"Last resolved {phrase}" in _text(_row(body))


def test_a_state_row_without_the_age_fields_still_renders(tmp_path: Path):
    # The provider is a seam: a row from before the ages existed must
    # degrade to saying so, not raise into a 500 on the one page an
    # operator opens when something is already wrong.
    stale = _state(ok=True)
    del stale["last_checked_age_s"]
    del stale["last_success_age_s"]

    r = _client(tmp_path, states=[stale]).get("/config/mappings")

    assert r.status_code == 200
    assert "unknown length of time" in _text(_row(r.text))


# --- The Status view must not read an unreadable check as "all fine" ---


def test_the_status_view_says_the_check_state_could_not_be_read(
    tmp_path: Path,
):
    # The Status view counts failing rows out of the canary's per-mapping
    # state. When that read *failed* there are no rows to count, and a bare
    # "2 series are offered to Sonarr" then reads as "and none of them are
    # broken" -- while /config/mappings, one click away, says the state
    # could not be read for every row. Two surfaces, one fact, disagreeing
    # is what the rest of this page is built to avoid.
    def _boom():
        raise RuntimeError("the canary is on fire")

    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(), mapping_state_provider=_boom
        )
    )
    r = TestClient(app).get("/config")
    body = _text(r.text)

    assert r.status_code == 200
    assert "could not be read" in body


def test_the_status_view_hedges_the_same_way_the_mappings_view_does(
    tmp_path: Path,
):
    # ...and it is the *same* fact, so the two pages have to agree about
    # it. Asserted from both ends rather than pinning one page's wording,
    # because the defect this replaces was precisely a disagreement.
    def _boom():
        raise RuntimeError("the canary is on fire")

    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(), mapping_state_provider=_boom
        )
    )
    client = TestClient(app)

    assert "could not be read" in _text(client.get("/config").text)
    assert "could not be read" in _text(client.get("/config/mappings").text)


def test_the_status_view_does_not_hedge_when_the_state_reads_fine(
    tmp_path: Path,
):
    # The other half: a canary that answered and found nothing wrong must
    # not be hedged over, or the hedge stops meaning anything.
    body = _text(_client(tmp_path, states=[_state(ok=True)]).get("/config").text)

    assert "could not be read" not in body


def test_no_state_provider_is_not_reported_as_a_failed_read(tmp_path: Path):
    # Nothing was asked, which is not the same as something that could not
    # be answered -- and in a deployment with no state provider there is no
    # canary at all, so the page makes no claim about SVT anywhere on it.
    body = _text(_client(tmp_path, states=None).get("/config").text)

    assert "could not be read" not in body


# --- The row that resolves nothing -------------------------------------
#
# A mapping whose slug works, whose episode list is full, and none of whose
# episodes can ever be matched to an episode Sonarr has. `Resolves` would
# be true of it and deeply misleading, because the thing it is true about
# is not the thing the operator wants to know.

NOTE = (
    "This mapping can never match anything. SVT lists 61 available "
    "episodes and not one of them carries an episode number."
)


def test_a_row_that_can_never_match_says_so_instead_of_resolves(
    tmp_path: Path,
):
    row = _text(_row(
        _client(tmp_path, states=[_state(
            ok=True, resolves=False, unresolvable_reason="no_ordinals",
            resolvability_note=NOTE,
        )]).get("/config/mappings").text
    ))

    assert "Resolves nothing" in row
    assert NOTE in row
    # ...and it does not also claim the reassuring thing.
    assert "12 episodes" not in row


def test_the_row_that_can_never_match_is_amber_and_not_red(tmp_path: Path):
    # Same urgency as the ended-show case: the rest of the feed works, and
    # in the no-ordinal case there is nothing to fix, so a red row would be
    # permanent by construction -- which is how a marker stops being read.
    row = _row(
        _client(tmp_path, states=[_state(
            ok=True, resolves=False, unresolvable_reason="no_ordinals",
            resolvability_note=NOTE,
        )]).get("/config/mappings").text
    )

    assert 'class="status-chip warn"' in row
    assert 'class="status-chip error"' not in row


def test_a_row_nothing_is_known_about_still_reads_as_resolving(tmp_path: Path):
    # `resolves: None` is "not determined this round" -- a Sonarr outage, a
    # brand-new series, a show whose episodes are all upcoming. It is not a
    # finding and must not be rendered as one.
    row = _text(_row(
        _client(tmp_path, states=[_state(ok=True, resolves=None)])
        .get("/config/mappings").text
    ))

    assert "Resolves nothing" not in row
    assert "Resolves" in row


def test_a_state_row_from_before_this_check_existed_still_renders(
    tmp_path: Path,
):
    # The provider is a seam. A row with no `resolves` key at all must
    # render as the SVT check alone rather than raise into a 500 on the one
    # page an operator opens when something is already wrong.
    old = _state(ok=True)
    del old["resolves"]
    del old["resolvability_note"]

    r = _client(tmp_path, states=[old]).get("/config/mappings")

    assert r.status_code == 200
    # The row, not the whole page: the explanatory note beneath the table
    # names both states, so a body-wide assertion would pass on the note.
    assert "Resolves nothing" not in _row(r.text)


# --- ...and the banner above it ----------------------------------------


def _unresolvable(**over) -> dict:
    row = {
        "tvdb_id": TVDB, "series_title": TITLE,
        "svt_slug": "gift-vid-forsta-ogonkastet",
        "reason": "no_ordinals", "note": NOTE,
    }
    row.update(over)
    return row


def test_the_banner_names_the_show_and_says_why(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(_sample_svt(
                state="unresolvable", needs_attention=True,
                unresolvable=1, unresolvable_series=[_unresolvable()],
            )),
        )
    )
    body = TestClient(app).get("/config").text

    assert "resolves nothing" in _text(body).lower()
    assert TITLE in body
    assert NOTE in _text(body)


def test_a_failing_row_and_an_unresolvable_row_are_both_reported(
    tmp_path: Path,
):
    # `state` is one word and can only name one shape, so the banner for
    # this finding is keyed on its own count rather than on the state --
    # otherwise a row that fails on SVT would hide a *different* row that
    # returns a perfect episode list nothing can ever match.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(_sample_svt(
                state="series", needs_attention=True, checked=2, failing=1,
                failing_series=[{
                    "tvdb_id": 99, "series_title": "Another Show",
                    "svt_slug": "another-show", "error": "404 not found",
                }],
                unresolvable=1, unresolvable_series=[_unresolvable()],
            )),
        )
    )
    text = _text(TestClient(app).get("/config").text)

    assert "Another Show" in text
    assert TITLE in text
    assert NOTE in text


def test_the_banner_stays_away_when_every_mapping_can_match(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(_sample_svt()),
        )
    )
    text = _text(TestClient(app).get("/config").text)

    assert "resolves nothing" not in text.lower()


def test_a_status_dict_from_before_this_check_still_renders(tmp_path: Path):
    # Same seam as the row above, one level up: a status_provider that
    # predates these keys renders the rest of the strip rather than
    # failing.
    svt = _sample_svt()
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(svt),
        )
    )
    r = TestClient(app).get("/config")

    assert r.status_code == 200
    assert "unresolvable" not in svt
