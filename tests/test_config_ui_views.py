"""The config page's four views, the nav between them, and what moved.

The page began as a settings form and grew a status strip, a mappings
table, a Check control, a Find mappings sweep and a canary onto the bottom
of it. It is read far more often to answer "is it working" than to change
a setting -- and a setting needs a restart before it does anything -- so
the landing view is now Status and the form is one click away.

These tests are about the split itself: that every view is reachable and
says where you are, that `/config` still serves the entry point it is
documented and deployed as, and that every write route still re-renders
the view its control lives on, with the operator's own submitted values
and the reason it was refused. That last property is the one this split
could most easily have broken invisibly, since a refusal that renders the
wrong view still renders a perfectly valid page.
"""

import asyncio
import re
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from svtplay_arr.api.config_ui import VIEWS, build_config_router
from svtplay_arr.mappings import MappingTable, add_mapping

TITLE = "Gift vid första ögonkastet"
API_KEY = "SECRET-KEY-VALUE"


class FakeSvt:
    async def search_series(self, query):
        return []

    async def list_episodes(self, slug):
        return []


class FakeSonarr:
    async def all_series(self):
        return []

    async def episodes(self, series_id):
        return []


def _paths(tmp_path: Path):
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        f"sonarr_api_key: {API_KEY}\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    return cfg, maps


def _client(tmp_path: Path, **kwargs) -> TestClient:
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), **kwargs)
    )
    return TestClient(app)


def _nav(html: str) -> str:
    m = re.search(r'<nav class="nav".*?</nav>', html, re.S)
    assert m, f"no nav bar in:\n{html[:2000]}"
    return m.group(0)


def _current_view(html: str) -> str:
    """The nav link marked as the page you are on, by its label."""
    nav = _nav(html)
    marked = re.findall(r'aria-current="page"[^>]*>([^<]+)</a>', nav)
    assert len(marked) == 1, f"expected exactly one current nav link, got {marked}"
    return marked[0].strip()


# --- The four views ---------------------------------------------------


def test_config_still_serves_the_entry_point_and_it_is_status(tmp_path: Path):
    # /config is documented, deployed, and the published SSO resource
    # points at it. The restructure may change which view it serves; it
    # may not change whether it serves one, and it may not answer with a
    # redirect that an SSO gateway or a bookmark has to follow.
    r = _client(tmp_path).get("/config")

    assert r.status_code == 200
    assert "<h2>Status</h2>" in r.text
    assert _current_view(r.text) == "Status"


@pytest.mark.parametrize("key,label,path", VIEWS)
def test_every_view_is_reachable_and_says_where_you_are(
    tmp_path: Path, key: str, label: str, path: str
):
    r = _client(tmp_path).get(path)

    assert r.status_code == 200
    assert _current_view(r.text) == label


@pytest.mark.parametrize("key,label,path", VIEWS)
def test_every_view_links_to_every_other_view(
    tmp_path: Path, key: str, label: str, path: str
):
    # The nav is the only way between these pages -- there is no
    # client-side router and no menu anywhere else -- so a view that
    # renders without a full nav bar is a dead end.
    nav = _nav(_client(tmp_path).get(path).text)

    for _other_key, other_label, other_path in VIEWS:
        assert f'href="{other_path}"' in nav, f"{path} does not link to {other_path}"
        assert f">{other_label}</a>" in nav


def test_the_settings_form_moved_off_the_landing_page(tmp_path: Path):
    # The point of the split: the landing view is no longer a form. If the
    # form is still rendered on /config, nothing has actually changed for
    # the operator who opens this page to ask whether it is working.
    body = _client(tmp_path).get("/config").text

    assert 'action="/config/settings"' not in body
    assert 'name="sonarr_url"' not in body
    # ...and it is still one click away, rather than gone.
    assert 'href="/config/settings"' in body


def test_the_mappings_table_moved_off_the_landing_page(tmp_path: Path):
    body = _client(tmp_path).get("/config").text

    assert '<table class="mappings">' not in body
    assert 'href="/config/mappings"' in body


@pytest.mark.parametrize(
    "path", [p for _k, _l, p in VIEWS if p != "/config/settings"]
)
def test_no_view_but_the_form_itself_renders_the_api_key(
    tmp_path: Path, path: str
):
    # The key is deliberately in the settings form's own source -- it is an
    # editable field, and masking it there is shoulder-surfing cover, not
    # confidentiality. Nowhere else. Every new surface this restructure
    # adds is a new place for it to leak, so this walks them all rather
    # than naming the one that was checked by hand.
    assert API_KEY not in _client(tmp_path).get(path).text


# --- The sub-pages are part of a view, not loose ----------------------


@pytest.mark.parametrize("path", ["/config/mappings/new"])
def test_the_mapping_sub_pages_carry_the_nav_too(tmp_path: Path, path: str):
    r = _client(tmp_path).get(path)

    assert r.status_code == 200
    assert _current_view(r.text) == "Mappings"


def test_the_search_results_page_carries_the_nav(tmp_path: Path):
    r = _client(tmp_path).post("/config/mappings/search", data={"q": "gift"})

    assert r.status_code == 200
    assert _current_view(r.text) == "Mappings"


def test_the_sweep_result_page_carries_the_nav(tmp_path: Path):
    r = _client(tmp_path).post(
        "/config/mappings/discover", data={"expected_mtime": ""}
    )

    assert r.status_code == 200
    assert _current_view(r.text) == "Mappings"


# --- Every write route still re-renders its own view ------------------


def _settings_form(tmp_path: Path, mtime, **over):
    form = {
        "sonarr_url": "http://sonarr.test:8989",
        "sonarr_api_key": API_KEY,
        "incomplete_dir": f"{tmp_path}/i",
        "completed_dir": f"{tmp_path}/c",
        "air_date_tolerance_days": "1",
        "rss_window_days": "14",
        "max_concurrent_downloads": "1",
        "expected_mtime": "" if mtime is None else str(mtime),
    }
    form.update(over)
    return form


def test_a_refused_save_re_renders_the_form_with_what_was_submitted(
    tmp_path: Path,
):
    # The hard-won behaviour this restructure had to carry across intact: a
    # save refused over one bad field must come back as the settings form,
    # with every other field the operator typed still in it, and the reason
    # on the page. Coming back as the Status view -- a perfectly valid
    # page -- would silently discard the whole form.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_settings_form(
            tmp_path,
            cfg.stat().st_mtime,
            air_date_tolerance_days="not-a-number",
            sonarr_url="http://moved.test:8989",
        ),
    )

    assert r.status_code == 200
    assert _current_view(r.text) == "Settings"
    assert '<p class="error">' in r.text
    # The value that was typed, not the one still on disk.
    assert 'value="http://moved.test:8989"' in r.text
    assert 'value="not-a-number"' in r.text
    # ...and nothing was written.
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["sonarr_url"] == (
        "http://sonarr.test:8989"
    )


def test_a_successful_save_re_renders_the_form_not_the_landing_view(
    tmp_path: Path,
):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))

    r = TestClient(app).post(
        "/config/settings", data=_settings_form(tmp_path, cfg.stat().st_mtime)
    )

    assert r.status_code == 200
    assert _current_view(r.text) == "Settings"
    assert '<p class="notice">' in r.text


def test_a_refused_delete_re_renders_the_mappings_view(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))

    r = TestClient(app).post(
        "/config/mappings/999999/delete", data={"expected_mtime": ""}
    )

    assert r.status_code == 200
    assert _current_view(r.text) == "Mappings"
    assert '<p class="error">' in r.text
    # The table the operator was looking at is still there to try again on.
    assert TITLE in r.text


def test_a_successful_delete_re_renders_the_mappings_view(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))

    r = TestClient(app).post(
        "/config/mappings/288649/delete",
        data={"expected_mtime": str(maps.stat().st_mtime)},
    )

    assert r.status_code == 200
    assert _current_view(r.text) == "Mappings"
    assert '<p class="notice">' in r.text
    assert MappingTable.load(maps).all() == []


def test_the_no_js_check_re_renders_the_mappings_view(tmp_path: Path):
    r = _client(tmp_path).post("/config/mappings/288649/check")

    assert r.status_code == 200
    assert _current_view(r.text) == "Mappings"


@pytest.mark.parametrize("key,label,path", VIEWS)
def test_the_health_read_does_not_happen_on_the_event_loop(
    tmp_path: Path, key: str, label: str, path: str
):
    # compute_health reads the job store, and every route here is a
    # coroutine -- so calling it inline runs a blocking sqlite read on the
    # event loop the download worker also runs on. Rendering any view
    # would then stall the downloads it is reporting on for as long as the
    # worker held the store's lock.
    #
    # Inside an asyncio.to_thread worker there is no running loop; asking
    # for one is the only check here that a version of the test could not
    # pass by accident, since the test client already runs the app off the
    # main thread.
    where = []

    def _provider():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            where.append("off the loop")
        else:
            where.append("on the loop")
        return {"status": "ok", "worker_alive": True, "active_jobs": 0,
                "same_filesystem": True, "mappings": 1,
                "mappings_ever_loaded": True, "mappings_degraded": False}

    _client(tmp_path, status_provider=_provider).get(path)

    assert where, "the status provider was never called"
    assert set(where) == {"off the loop"}, where


def test_the_pending_restart_banner_is_on_every_view(tmp_path: Path):
    # It says the running service is not using what is on disk, which is a
    # fact about whether the service is working -- not a fact about the
    # form. An operator who saved a setting last week and never restarted
    # must meet it on the page they actually open.
    from svtplay_arr.config import Settings

    cfg, maps = _paths(tmp_path)
    booted = Settings.load(cfg)
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace("14400", "1")
        + "rss_window_days: 30\n",
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), booted=booted)
    )
    client = TestClient(app)

    for _key, _label, path in VIEWS:
        assert '<p class="pending">' in client.get(path).text, path


@pytest.mark.parametrize(
    "path,method,data",
    [
        ("/config/mappings/new", "get", None),
        ("/config/mappings/search", "post", {"q": "gift"}),
        ("/config/mappings/discover", "post", {"expected_mtime": ""}),
    ],
)
def test_every_mapping_sub_page_leads_back_to_the_table(
    tmp_path: Path, path: str, method: str, data
):
    # These pages were written when /config *was* the mappings table, so
    # their way out pointed there. It is now the Status view, and an
    # operator who cancels an Add or finishes a sweep wants the table they
    # came from rather than the landing page.
    client = _client(tmp_path)
    r = client.get(path) if method == "get" else client.post(path, data=data)
    body = r.text
    escape = re.sub(r'<nav class="nav".*?</nav>', "", body, flags=re.S)

    assert r.status_code == 200
    assert 'href="/config/mappings"' in escape, (
        f"{path} has no way back to the mappings table outside the nav"
    )
