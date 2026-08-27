"""What a fresh install sees, and what one Find mappings click costs.

Two moments this page has never handled well.

A brand-new install has no mappings and no downloads. The mappings table
rendered one line of it -- "No mappings yet" -- to an operator who had
just installed the thing and had no idea what a mapping was or how to get
one. That is the single moment the page has their attention and the one
thing it never explained.

And Find mappings is a plain form POST that walks the whole Sonarr library
and can issue hundreds of SVT requests. With JavaScript off it sits there
for a minute with no output at all. What it costs has to be on the page
*before* the click, since after it there is nothing to read.
"""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from svtplay_arr.api.config_ui import (
    _SWEEP_CAP,
    _SWEEP_REQUEST_BUDGET,
    build_config_router,
)
from svtplay_arr.mappings import add_mapping

TITLE = "Gift vid första ögonkastet"

_BASE_HTML = (
    Path(__file__).resolve().parent.parent
    / "src" / "svtplay_arr" / "templates" / "base.html"
)


class FakeSvt:
    async def search_series(self, query):
        return []

    async def list_episodes(self, slug):
        return []


class FakeSonarr:
    async def all_series(self):
        return []


def _config(tmp_path: Path) -> Path:
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        "sonarr_api_key: SECRET-KEY-VALUE\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    return cfg


def _fresh_client(tmp_path: Path, **kwargs) -> TestClient:
    """A genuinely fresh install: a valid, empty mappings file."""
    cfg = _config(tmp_path)
    maps = tmp_path / "mappings.yaml"
    maps.write_text("series: []\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), **kwargs)
    )
    return TestClient(app)


def _populated_client(tmp_path: Path, **kwargs) -> TestClient:
    cfg = _config(tmp_path)
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), **kwargs)
    )
    return TestClient(app)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _inline_script() -> str:
    m = re.search(r"<script>(.*?)</script>", _BASE_HTML.read_text(encoding="utf-8"), re.S)
    assert m, "no inline <script> in base.html"
    return m.group(1)


# --- The empty install ------------------------------------------------


def test_a_fresh_install_is_told_what_a_mapping_is(tmp_path: Path):
    # The operator has just installed this. "No mappings yet" is true and
    # useless: nothing on the page said what a mapping was, why the thing
    # cannot work them out for itself, or what to press.
    body = _text(_fresh_client(tmp_path).get("/config/mappings").text)

    assert "No mappings yet" in body
    assert "tvdb" in body.lower()
    assert "slug" in body.lower()


def test_a_fresh_install_is_pointed_at_find_mappings(tmp_path: Path):
    body = _fresh_client(tmp_path).get("/config/mappings").text
    empty = re.search(r'<td colspan="\d+"[^>]*>(.*?)</td>', body, re.S)

    assert empty, f"no empty-state cell in:\n{body}"
    assert "Find mappings" in _text(empty.group(1))
    # ...and the control it names is on the same page.
    assert 'action="/config/mappings/discover"' in body


def test_the_landing_view_of_a_fresh_install_says_what_to_do_next(
    tmp_path: Path,
):
    # Status is where an operator arrives. A fresh install with no
    # mappings will never download anything, and that is the single most
    # important thing this page can say to them.
    body = _text(_fresh_client(tmp_path).get("/config").text)

    assert "No mappings yet" in body
    assert "Find mappings" in body


def test_the_explanation_is_absent_once_there_are_mappings(tmp_path: Path):
    # It is onboarding, not permanent furniture. An operator with a
    # working table should not be told what a mapping is on every visit.
    body = _text(_populated_client(tmp_path).get("/config/mappings").text)

    assert "No mappings yet" not in body


def test_a_broken_mappings_file_gets_the_error_not_the_onboarding(
    tmp_path: Path,
):
    # A file that failed to load is not a fresh install, and telling
    # someone whose YAML is malformed to press Find mappings would be
    # advice to sweep over a file that cannot be read.
    cfg = _config(tmp_path)
    maps = tmp_path / "mappings.yaml"
    maps.write_text("series: [unterminated\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    body = _text(TestClient(app).get("/config/mappings").text)

    assert "could not be read" in body
    assert "No mappings yet" not in body


def test_an_empty_activity_view_explains_what_would_appear(tmp_path: Path):
    body = _text(
        _fresh_client(
            tmp_path, activity_provider=lambda: {"active": [], "history": []}
        ).get("/config/activity").text
    )

    assert "Nothing has been downloaded yet" in body
    # A fresh install will never download anything until a mapping exists,
    # which is the actual reason this list is empty on day one.
    assert "mapping" in body.lower()


# --- What the sweep costs, said before the click ----------------------


def test_the_sweep_says_it_takes_a_while_before_it_is_clicked(
    tmp_path: Path,
):
    # With JavaScript off this is a blocking POST that can sit for a
    # minute with no output. After the click there is nothing to read, so
    # the warning has to be on the page beforehand.
    body = _text(_populated_client(tmp_path).get("/config/mappings").text)

    assert "take a while" in body or "takes a while" in body
    assert "minute" in body


def test_the_sweep_names_the_bounds_it_actually_runs_with(tmp_path: Path):
    # Numbers read off the module's own constants rather than written into
    # prose, so the page cannot promise a bound the sweep does not honour.
    body = _text(_populated_client(tmp_path).get("/config/mappings").text)

    assert str(_SWEEP_CAP) in body
    assert str(_SWEEP_REQUEST_BUDGET) in body


def test_the_sweep_control_is_still_a_plain_form(tmp_path: Path):
    # The feedback is an enhancement; it must not have turned the sweep
    # into something that needs JavaScript.
    body = _populated_client(tmp_path).get("/config/mappings").text

    assert re.search(
        r'<form method="post" action="/config/mappings/discover"', body
    ), f"the sweep is no longer a plain form:\n{body}"
    assert 'type="submit"' in body


def test_no_sweep_progress_element_is_server_rendered(tmp_path: Path):
    # Same rule as the mappings filter: a progress note that can never
    # update is worse than none, so with JavaScript off nothing is
    # rendered at all.
    body = _populated_client(tmp_path).get("/config/mappings").text
    # The stylesheet names the class it will style and the script names
    # the class it will create; the markup itself must not.
    markup = re.sub(r"<(style|script)>.*?</\1>", "", body, flags=re.S)

    assert "sweep-progress" not in markup
    # ...and the mechanism that adds it is present, so this cannot pass by
    # the feature having quietly vanished.
    assert "initSweepProgress" in body


def test_the_sweep_enhancement_never_intercepts_the_submit(tmp_path: Path):
    # If it called preventDefault the sweep would depend on script to
    # happen at all. It only annotates a submission the browser is already
    # performing.
    script = _inline_script()
    m = re.search(
        r"function\s+initSweepProgress\s*\(\s*\)\s*\{(.*?)\n      \}", script, re.S
    )
    assert m, "initSweepProgress is not defined in the inline script"
    body = m.group(1)

    assert "preventDefault" not in body
    assert "fetch(" not in body
    # It does say something, though.
    assert "textContent" in body
