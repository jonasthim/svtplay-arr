import asyncio
import hashlib
import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest
from markupsafe import escape
from fastapi.testclient import TestClient
from svtplay_arr.app import create_app
from svtplay_arr.config import ConfigError, Settings
from svtplay_arr.models import Release, SonarrEpisode, SvtEpisode
from svtplay_arr.sonarr import (
    REASON_MESSAGES,
    REASON_REFUSED,
    REASON_UNAUTHORIZED,
    SonarrApiError,
    SonarrStatus,
)
from svtplay_arr.store import JobStoreError


def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "i").mkdir()
    (tmp_path / "c").mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "sonarr_url: http://sonarr.test\n"
        "sonarr_api_key: sekrit-sonarr-api-key\n"
        f"incomplete_dir: {tmp_path}/i\n"
        f"completed_dir: {tmp_path}/c\n"
        f"mappings_file: {tmp_path}/mappings.yaml\n"
        f"db_path: {tmp_path}/jobs.db\n",
        encoding="utf-8",
    )
    return Settings(
        sonarr_url="http://sonarr.test",
        sonarr_api_key="sekrit-sonarr-api-key",
        incomplete_dir=tmp_path / "i",
        completed_dir=tmp_path / "c",
        mappings_file=tmp_path / "mappings.yaml",
        db_path=tmp_path / "jobs.db",
        config_path=config_path,
    )


def test_health_reports_ok(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        body = c.get("/health").json()
        assert body["status"] == "ok"
        assert body["same_filesystem"] is True


def test_health_reports_the_running_version(tmp_path: Path):
    # pyproject.toml has no static version any more (see version.py and its
    # own docstring for why): the version in this response has to be the
    # one hatch-vcs baked into this package's own installed metadata, not a
    # guess or a hardcoded expectation that could drift from it the same
    # way the old static field did.
    from importlib.metadata import version as installed_version

    with TestClient(create_app(_settings(tmp_path))) as c:
        body = c.get("/health").json()
    assert body["version"] == installed_version("svtplay-arr")
    assert body["version"] != "0.1.0"  # the number that was false for two releases


def test_health_reports_the_version_as_unknown_rather_than_500ing(
    tmp_path: Path, monkeypatch
):
    # A monitoring endpoint must not crash because its own version lookup
    # failed -- same rule every other field in compute_health follows. An
    # install whose dist-info went missing gets an honest "unknown", never
    # a wrong number and never a 500.
    from importlib.metadata import PackageNotFoundError

    def _boom(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr("svtplay_arr.version._installed_version", _boom)

    with TestClient(create_app(_settings(tmp_path))) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["version"] == "unknown"


def test_health_flags_split_filesystem(tmp_path: Path):
    s = _settings(tmp_path)
    s.completed_dir = Path("/proc")
    with TestClient(create_app(s)) as c:
        body = c.get("/health").json()
        assert body["same_filesystem"] is False
        # The whole point of this check is that it changes the reported
        # health, not just that a field exists alongside an unaffected
        # "ok" -- a previous review could not verify the check was wired
        # into anything at all.
        assert body["status"] == "degraded"


def test_caps_is_mounted(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        assert "tvdbid" in c.get("/api/?t=caps").text


def test_sab_is_mounted(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        assert c.get("/sabnzbd/api", params={"mode": "version"}).status_code == 200


def test_health_flags_dead_worker_task(tmp_path: Path, monkeypatch):
    # If the worker task dies (e.g. an unexpected exception escapes
    # run_forever), the service must not keep reporting "ok" while silently
    # downloading nothing. Force the worker's run_forever to fail
    # immediately and confirm /health notices.
    async def _dies_immediately(self, poll_seconds: float = 2.0) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("svtplay_arr.worker.Worker.run_forever", _dies_immediately)

    with TestClient(create_app(_settings(tmp_path))) as c:
        deadline = time.monotonic() + 2.0
        body = c.get("/health").json()
        while body["worker_alive"] and time.monotonic() < deadline:
            time.sleep(0.02)
            body = c.get("/health").json()

        assert body["worker_alive"] is False
        assert body["status"] == "degraded"


def test_health_survives_store_read_failure(tmp_path: Path, monkeypatch):
    # /health must never turn into a 500 just because reading job counts
    # failed -- it is monitoring infrastructure and must not be able to
    # fail the thing it monitors (or itself).
    def _boom(self):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr("svtplay_arr.store.JobStore.all_active", _boom)

    with TestClient(create_app(_settings(tmp_path))) as c:
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["active_jobs"] is None


def test_startup_failure_still_closes_http_client(tmp_path: Path, monkeypatch):
    # sweep_incomplete()'s own ignore_errors=True/missing_ok=True guards
    # don't cover iterdir() itself, so a real OSError (e.g. a permissions
    # problem on incomplete/) can still escape it during startup, before the
    # lifespan's try/finally is even entered. That must not leak the
    # httpx.AsyncClient created in create_app().
    closed = {"called": False}
    orig_aclose = httpx.AsyncClient.aclose

    async def spy_aclose(self):
        closed["called"] = True
        await orig_aclose(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", spy_aclose)

    def _boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr("svtplay_arr.worker.Worker.sweep_incomplete", _boom)

    app = create_app(_settings(tmp_path))
    with pytest.raises(OSError):
        with TestClient(app):
            pass

    assert closed["called"] is True


def test_factory_reads_config_path_from_env(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg.write_text(
        "sonarr_url: http://sonarr.test\nsonarr_api_key: k\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n"
        f"db_path: {tmp_path}/jobs.db\nmappings_file: {tmp_path}/mappings.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SVTPLAY_ARR_CONFIG", str(cfg))
    from svtplay_arr.app import create_app_from_env

    with TestClient(create_app_from_env()) as c:
        assert c.get("/health").json()["status"] == "ok"


def test_download_link_is_built_from_the_incoming_request(tmp_path: Path, monkeypatch):
    """The .nzb link Sonarr is handed must point back at the host Sonarr
    used to reach us.

    It used to be built from `listen_host`/`listen_port`, which default to
    "0.0.0.0":9800 and which deploy/README.md documented verbatim. Every
    <link>/<enclosure url> was therefore `http://0.0.0.0:9800/api/nzb/...`.
    Sonarr fetches that from its own container, where 0.0.0.0 resolves to
    its own loopback with nothing listening: every grab failed at the .nzb
    fetch, and nothing in our logs recorded it because the request never
    arrived. Deriving the link from `request.base_url` makes it reachable by
    construction -- Sonarr connected over it a moment ago.

    Built through create_app deliberately: the previous test passed a
    hand-written base_url straight into the router, so it could not have
    caught what production actually generated.
    """
    release = Release(
        guid="svtplay-abc123",
        title="Gift vid första ögonkastet - S15E01 - WEBDL-1080p",
        svt_id="KZmQ5JY",
        quality="WEBDL-1080p",
        size_bytes=1_435_287_295,
        published=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    async def _resolve(self, tvdb_id, season, episode):
        return release

    monkeypatch.setattr("svtplay_arr.resolver.Resolver.resolve", _resolve)

    with TestClient(create_app(_settings(tmp_path))) as c:
        body = c.get(
            "/api/", params={"t": "tvsearch", "tvdbid": 288649, "season": 15, "ep": 1}
        ).text

    root = ET.fromstring(body)
    link = root.find(".//item/link").text
    enclosure = root.find(".//item/enclosure").attrib["url"]
    assert link.startswith("http://testserver/api/nzb/svtplay-abc123?"), link
    assert enclosure == link
    assert "0.0.0.0" not in link


def test_create_app_refuses_overlapping_download_dirs(tmp_path: Path):
    # The guard is worthless unless something calls it. sweep_incomplete()
    # runs on every startup and rmtree's everything in incomplete_dir, so a
    # completed_dir nested inside it loses the library -- refusing to start
    # is the only safe answer.
    s = _settings(tmp_path)
    s.completed_dir = s.incomplete_dir / "completed"
    with pytest.raises(ConfigError):
        create_app(s)


def test_config_page_is_mounted(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.get("/config")
        assert r.status_code == 200
        assert "svtplay-arr" in r.text


def test_config_page_renders_the_settings_form(tmp_path: Path):
    # Guards against the vacuous-pass failure mode where the settings form
    # renders with every field blank -- e.g. because config_path was None
    # and _index fell into its error branch (base.html still renders
    # {% block content %} alongside the error banner, so the form's
    # structure -- field names -- is present either way). Only an actual
    # *configured value* appearing as an input's value can distinguish a
    # correctly-wired form from the error-path form.
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        assert f'value="{s.sonarr_url}"' in c.get("/config/settings").text


def test_config_page_renders_the_api_key_as_an_editable_field(tmp_path: Path):
    # Replaces test_config_page_does_not_leak_the_api_key. Reversed on
    # 2026-08-25: the key is configuration, and being the one setting that
    # required SSH was the asymmetry the page exists to remove. The value is
    # in the page source whichever way the field's Show/Hide button is set;
    # what stands between it and the internet is network isolation, or the
    # SSO reverse proxy in front of the site, not the page's silence. See
    # docs/design/2026-08-25-config-ui-design.md, "Reversed after
    # implementation".
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        body = c.get("/config/settings").text
    assert s.sonarr_api_key in body
    assert 'name="sonarr_api_key"' in body
    assert 'type="password"' in body


def test_the_configured_svt_ua_reaches_the_services_svt_client(
    tmp_path: Path, monkeypatch
):
    # The middle joint of the svt_ua path. tests/test_config.py pins the
    # two ends -- Settings.load reads the key, and SvtClient puts it on the
    # wire as the `ua` query parameter -- but neither notices if this call
    # site quietly stops passing it, which silently reverts every
    # deployment that set it back to the default. So: build the app the way
    # the service does, from a config.yaml that sets the key, and ask the
    # client it constructed what it is actually going to send.
    (tmp_path / "i").mkdir()
    (tmp_path / "c").mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test\nsonarr_api_key: k\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n"
        f"mappings_file: {tmp_path}/mappings.yaml\ndb_path: {tmp_path}/jobs.db\n"
        "svt_ua: some-other-client\n",
        encoding="utf-8",
    )

    import svtplay_arr.app as app_module

    real = app_module.SvtClient
    built = {}

    def spy(http, *args, **kwargs):
        client = real(http, *args, **kwargs)
        # Asked of the constructed client rather than of the arguments, so
        # this keeps meaning the same thing if the parameter is renamed or
        # starts being passed by keyword.
        built["ua"] = client._ua_param
        return client

    monkeypatch.setattr(app_module, "SvtClient", spy)

    with TestClient(create_app(Settings.load(cfg))):
        pass

    assert built["ua"] == "some-other-client"


def test_health_never_exposes_the_api_key(tmp_path: Path):
    # Unchanged by the config-page reversal, and pinned so it stays that
    # way: /health is the one endpoint reachable without going through the
    # settings form, and it is what external monitoring would scrape.
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        r = c.get("/health")
    assert s.sonarr_api_key not in r.text


def test_newznab_responses_never_expose_the_api_key(tmp_path: Path):
    # Sonarr consumes these, and their bodies end up in Sonarr's own logs
    # and UI. The key has no business in either.
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        assert s.sonarr_api_key not in c.get("/api/?t=caps").text
        assert s.sonarr_api_key not in c.get("/api/?t=tvsearch").text


def test_sab_responses_never_expose_the_api_key(tmp_path: Path):
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        for mode in ("version", "queue", "history", "get_config"):
            r = c.get("/sabnzbd/api", params={"mode": mode})
            assert s.sonarr_api_key not in r.text, mode


def test_a_mapping_added_on_disk_is_visible_without_a_restart(tmp_path: Path):
    # Must observe the resolver's own mapping table, not the config page's:
    # the page builds its list via a separate MappingTable.load(...) call
    # (config_ui.py), completely decoupled from what create_app hands the
    # Resolver. Asserting on GET /config here would still pass if the
    # resolver were wired back to a load-once MappingTable, while the
    # Newznab feed Sonarr actually consumes went permanently stale after
    # every mapping addition -- silently contradicting the page's own
    # "mappings apply immediately" notice. Driving the real Newznab feed
    # instead would require the resolver to reach SVT over the network from
    # a unit test, so this reads the live mapping table app.state.resolver
    # exposes directly.
    from svtplay_arr.mappings import add_mapping

    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        mappings = c.app.state.resolver._mappings
        assert mappings.for_tvdb(288649) is None
        add_mapping(
            s.mappings_file, tvdb_id=288649, svt_series_id="jpmQD3q",
            svt_slug="gift-vid-forsta-ogonkastet",
            series_title="Gift vid första ögonkastet", expected_mtime=None,
        )
        mapping = mappings.for_tvdb(288649)
        assert mapping is not None
        assert mapping.series_title == "Gift vid första ögonkastet"


def test_settings_load_sets_config_path(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test\nsonarr_api_key: k\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    s = Settings.load(cfg)
    assert s.config_path == cfg


def test_create_app_starts_with_a_legitimately_empty_mappings_file(tmp_path: Path):
    # `series: []` is the file the config page writes when the last mapping
    # is deleted. The loader now refuses unrecognised top-level shapes, and
    # this is the one shape that must stay a successful load -- a service
    # that will not boot after the operator removes their last show would be
    # a worse failure than the one that strictness fixes.
    s = _settings(tmp_path)
    s.mappings_file.write_text("series: []\n", encoding="utf-8")
    with TestClient(create_app(s)) as c:
        assert c.get("/health").status_code == 200
        assert c.app.state.resolver._mappings.all() == []


def test_create_app_starts_with_an_unloadable_mappings_file(tmp_path: Path):
    # A mappings file the loader rejects must degrade, never stop the
    # service coming up: the config page is the tool for fixing it.
    s = _settings(tmp_path)
    s.mappings_file.write_text("- not a mapping document\n", encoding="utf-8")
    with TestClient(create_app(s)) as c:
        assert c.get("/config").status_code == 200


def _good_mappings(s: Settings) -> None:
    from svtplay_arr.mappings import add_mapping

    add_mapping(
        s.mappings_file, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet",
        series_title="Gift vid första ögonkastet", expected_mtime=None,
    )


def test_health_reports_the_loaded_mapping_count(tmp_path: Path):
    s = _settings(tmp_path)
    _good_mappings(s)
    with TestClient(create_app(s)) as c:
        body = c.get("/health").json()
        assert body["status"] == "ok"
        assert body["mappings"] == 1
        assert body["mappings_ever_loaded"] is True
        assert body["mappings_degraded"] is False


def test_health_flags_a_degraded_mapping_table(tmp_path: Path):
    # Before this branch an invalid mappings.yaml raised inside create_app,
    # uvicorn exited, and Restart=on-failure made it loud. Now the table
    # degrades to the last known-good instead -- which is right for the
    # feed, but leaves the service up, serving a stale table, and (until
    # this) reporting "ok". A service that is up, healthy and grabbing
    # nothing is this project's named failure mode.
    s = _settings(tmp_path)
    _good_mappings(s)
    with TestClient(create_app(s)) as c:
        assert c.get("/health").json()["status"] == "ok"

        stat = s.mappings_file.stat()
        s.mappings_file.write_text("series:\n", encoding="utf-8")
        os.utime(s.mappings_file, (stat.st_atime + 10, stat.st_mtime + 10))

        body = c.get("/health").json()
        assert body["status"] == "degraded"
        assert body["mappings_degraded"] is True
        # ...and the feed is genuinely unaffected: the last-good table is
        # still what the resolver serves.
        assert body["mappings"] == 1
        assert body["mappings_ever_loaded"] is True


def test_health_never_500s_on_a_broken_mapping_table(tmp_path: Path, monkeypatch):
    def _boom(self):
        raise RuntimeError("mapping table is on fire")

    monkeypatch.setattr(
        "svtplay_arr.mappings.ReloadingMappingTable.status", _boom, raising=False
    )
    with TestClient(create_app(_settings(tmp_path))) as c:
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"
        assert resp.json()["mappings"] is None


def test_config_page_reports_a_pending_restart_against_the_booted_settings(
    tmp_path: Path,
):
    # The persistent banner can only exist if create_app hands the config
    # router the Settings the service actually booted with. Asserting it
    # through build_config_router alone would still pass with `booted`
    # dropped here, and the page would go back to showing the new value
    # with no sign the running service is still using the old one.
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        assert '<p class="pending">' not in c.get("/config").text

        c.post("/config/settings", data={
            "expected_mtime": str(s.config_path.stat().st_mtime),
            "sonarr_url": s.sonarr_url,
            "incomplete_dir": str(s.incomplete_dir),
            "completed_dir": str(s.completed_dir),
            "air_date_tolerance_days": "3",
            "rss_window_days": "7",
            "max_concurrent_downloads": "1",
        })

        body = c.get("/config").text
        assert '<p class="pending">' in body
        assert "Air date tolerance (days)" in body
        # ...while the resolver really is still running with the old value.
        assert c.app.state.resolver._tolerance == 1


def test_config_page_and_health_agree(tmp_path: Path, monkeypatch):
    # The entire point of extracting compute_health() into one place in
    # app.py: the config page and /health must always describe the same
    # state, never two independently-computed opinions about it. A status
    # strip that disagreed with /health would be worse than no strip at
    # all, because the operator would trust the one in front of them.
    #
    # Exercised across several states -- not just the healthy default --
    # because a provider that only agrees with /health in the common case
    # would still let the two drift apart on exactly the states an
    # operator most needs them to agree on.
    s = _settings(tmp_path)
    _good_mappings(s)

    with TestClient(create_app(s)) as c:

        def assert_agree() -> dict:
            health = c.get("/health").json()
            page = c.get("/config").text
            assert f"Service: {health['status']}" in page
            assert health["version"] in page
            assert ("Worker: alive" in page) == health["worker_alive"]
            assert ("Worker: dead" in page) == (not health["worker_alive"])
            mcount = health["mappings"] if health["mappings"] is not None else "?"
            assert f"Mappings: {mcount}" in page
            assert ("DEGRADED" in page) == health["mappings_degraded"]
            if health["same_filesystem"]:
                assert "Same filesystem: yes" in page
            else:
                assert "Same filesystem: no" in page
            ajobs = (
                health["active_jobs"]
                if health["active_jobs"] is not None
                else "unknown"
            )
            assert f"Active jobs: {ajobs}" in page
            return health

        # 1. the healthy default.
        health = assert_agree()
        assert health["status"] == "ok"
        assert health["mappings_degraded"] is False

        # 2. a degraded mapping table -- the state this feature exists to
        # surface, today discoverable only via `curl localhost:9800/health`.
        stat = s.mappings_file.stat()
        s.mappings_file.write_text("series:\n", encoding="utf-8")
        os.utime(s.mappings_file, (stat.st_atime + 10, stat.st_mtime + 10))
        health = assert_agree()
        assert health["mappings_degraded"] is True
        assert health["status"] == "degraded"

        # 3. a store read failure -- /health degrades active_jobs to None
        # rather than raising; the page must show the same "unknown".
        def _boom(self):
            raise RuntimeError("db is on fire")

        monkeypatch.setattr("svtplay_arr.store.JobStore.all_active", _boom)
        health = assert_agree()
        assert health["active_jobs"] is None


def test_config_page_agrees_with_a_dead_worker(tmp_path: Path, monkeypatch):
    # Same agreement property as test_config_page_and_health_agree, for the
    # one state that can only be produced by killing the worker task before
    # the app starts (see test_health_flags_dead_worker_task).
    async def _dies_immediately(self, poll_seconds: float = 2.0) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("svtplay_arr.worker.Worker.run_forever", _dies_immediately)

    with TestClient(create_app(_settings(tmp_path))) as c:
        deadline = time.monotonic() + 2.0
        health = c.get("/health").json()
        while health["worker_alive"] and time.monotonic() < deadline:
            time.sleep(0.02)
            health = c.get("/health").json()
        assert health["worker_alive"] is False
        assert health["status"] == "degraded"

        page = c.get("/config").text
        assert "Worker: dead" in page
        assert "Service: degraded" in page
        assert 'class="status-chip error"' in page


def test_config_page_flags_split_filesystem(tmp_path: Path):
    s = _settings(tmp_path)
    s.completed_dir = Path("/proc")
    with TestClient(create_app(s)) as c:
        page = c.get("/config").text
        assert "Same filesystem: no" in page
        assert 'class="status-chip warn"' in page


def test_config_page_survives_a_store_read_failure(tmp_path: Path, monkeypatch):
    # Mirrors test_health_survives_store_read_failure: the page must be at
    # least as forgiving as /health, and this is the exact condition the
    # spec calls out as an existing test the page must survive too.
    def _boom(self):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr("svtplay_arr.store.JobStore.all_active", _boom)

    with TestClient(create_app(_settings(tmp_path))) as c:
        resp = c.get("/config")
        assert resp.status_code == 200
        assert "Active jobs: unknown" in resp.text


def test_config_page_never_exposes_the_api_key_in_the_status_strip(tmp_path: Path):
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        page = c.get("/config").text
        strip = page[page.index('class="status-strip"'):]
        strip = strip[: strip.index("</div>")]
        assert s.sonarr_api_key not in strip


# --- The job store's lifetime ----------------------------------------------
#
# create_app opens one JobStore for the process, and the worker drives it
# from a background task. Closing it is therefore a shutdown-only act, and
# it has to happen after the worker task has been cancelled AND awaited --
# closing it earlier would leave the worker writing progress into a closed
# connection.


def test_the_job_store_is_open_for_as_long_as_the_app_is_serving(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        assert c.get("/health").json()["active_jobs"] == 0
        assert app.state.job_store.all_active() == []


def test_shutdown_closes_the_job_store(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        c.get("/health")
    with pytest.raises(JobStoreError):
        app.state.job_store.all_active()


def test_startup_failure_also_closes_the_job_store(tmp_path: Path, monkeypatch):
    # The sibling of test_startup_failure_still_closes_http_client above: the
    # store is opened in create_app, before the lifespan is entered at all,
    # so the same early-failure path has to release it.
    def _boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr("svtplay_arr.worker.Worker.sweep_incomplete", _boom)

    app = create_app(_settings(tmp_path))
    with pytest.raises(OSError):
        with TestClient(app):
            pass

    with pytest.raises(JobStoreError):
        app.state.job_store.all_active()


def test_a_text_query_reaches_the_live_mapping_table(tmp_path: Path, monkeypatch):
    """Wiring, not filtering -- the filter itself is tested in
    tests/test_newznab.py.

    `q` is matched against `series_title` from the mapping table, so the
    router needs the same live table the resolver reads. Hand it the wrong
    object (or none) and every query quietly answers with an empty channel,
    which looks exactly like "no such series" -- the kind of two-places
    drift that is this project's most common defect.
    """
    s = _settings(tmp_path)
    s.mappings_file.write_text(
        "series:\n"
        "  - tvdb_id: 288649\n"
        "    svt_series_id: jBd1eA9\n"
        "    svt_slug: gift-vid-forsta-ogonkastet\n"
        "    series_title: Gift vid första ögonkastet\n",
        encoding="utf-8",
    )
    release = Release(
        guid="svtplay-abc123",
        title="Gift vid första ögonkastet - S15E01 - WEBDL-1080p",
        svt_id="KZmQ5JY",
        quality="WEBDL-1080p",
        size_bytes=1_435_287_295,
        published=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    async def _recent(self, within_days, today=None):
        return [release]

    monkeypatch.setattr("svtplay_arr.resolver.Resolver.recent", _recent)

    with TestClient(create_app(s)) as c:
        hit = c.get("/api/", params={"t": "tvsearch", "q": "gift vid"}).text
        miss = c.get("/api/", params={"t": "tvsearch", "q": "Vetenskapens värld"}).text
        bare = c.get("/api/", params={"t": "tvsearch"}).text

    assert [i.find("title").text for i in ET.fromstring(hit).findall(".//item")] == [
        release.title
    ]
    assert ET.fromstring(miss).findall(".//item") == []
    # And the feed Sonarr's indexer test fires is untouched by any of this.
    assert len(ET.fromstring(bare).findall(".//item")) == 1


# --- The SVT canary --------------------------------------------------------
#
# The one silence this service could not detect was its own. Everything
# /health knew about was *this* process -- the worker, the store, the
# mapping table, the filesystem -- so an SVT change left the listing
# returning [], the feed empty, Sonarr grabbing nothing, and /health saying
# "ok" throughout. These tests are about the wiring: one computation behind
# both surfaces, and a canary whose own death is visible.


def _mappings_file(tmp_path: Path, *rows: tuple[int, str, str]) -> None:
    body = "series:\n"
    for tvdb_id, slug, title in rows:
        body += (
            f"  - tvdb_id: {tvdb_id}\n"
            f"    svt_series_id: svt{tvdb_id}\n"
            f"    svt_slug: {slug}\n"
            f"    series_title: {title}\n"
        )
    (tmp_path / "mappings.yaml").write_text(body, encoding="utf-8")


def _episode(i: int) -> SvtEpisode:
    return SvtEpisode(
        svt_id=f"e{i}", title=f"{i}. Avsnitt", url=f"/video/e{i}/s/avsnitt-{i}",
        ordinal=i, published=date(2026, 8, 20), available=True, duration_s=1800,
    )


def _svt_returns(monkeypatch, per_slug: dict) -> list[str]:
    """Point the app's real SvtClient at canned episode lists.

    Patched on the class create_app actually constructs, so the canary is
    exercised through the same client `Resolver` uses rather than through a
    stand-in wired up for the test. Returns the list every requested slug
    is appended to. A slug with no entry parses to zero episodes, which is
    what an SVT format change looks like from here.
    """
    seen: list[str] = []

    async def _list_episodes(self, slug):
        seen.append(slug)
        outcome = per_slug.get(slug, [])
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(
        "svtplay_arr.svt.client.SvtClient.list_episodes", _list_episodes
    )
    return seen


def _run_a_canary_round(app) -> None:
    """Drive one round of the app's own canary, synchronously.

    The canary otherwise settles for its startup delay and then sleeps an
    hour, so a test that wants to see a round has to ask for one. This is
    the app's real canary -- the same object `/health` reports on -- not a
    second instance built for the test, which is the only way these tests
    can say anything about the wiring.
    """
    asyncio.run(app.state.svt_canary.run_once())


def test_health_carries_the_canary_and_never_calls_an_unknown_healthy(
    tmp_path: Path,
):
    # A fresh process has checked nothing. Reporting that as "ok" would
    # rebuild the exact defect this feature removes, one level up.
    with TestClient(create_app(_settings(tmp_path))) as c:
        svt = c.get("/health").json()["svt"]
    assert svt["state"] == "unknown"
    assert svt["state"] != "ok"
    assert svt["last_checked"] is None
    assert svt["last_success"] is None
    assert svt["alive"] is True


def test_an_unchecked_canary_does_not_cry_wolf(tmp_path: Path):
    # It must not read as healthy, but it must not make every restart
    # report a degraded service either -- a check that is degraded for the
    # first hour of every boot is one operators learn to ignore.
    with TestClient(create_app(_settings(tmp_path))) as c:
        body = c.get("/health").json()
    assert body["svt"]["degraded"] is False
    assert body["status"] == "ok"


def test_healths_existing_fields_are_unchanged_by_the_canary(tmp_path: Path):
    # Sonarr health-check setups may poll /health. The canary is additive:
    # nothing existing may be removed, renamed or retyped.
    with TestClient(create_app(_settings(tmp_path))) as c:
        body = c.get("/health").json()
    assert body["status"] == "ok"
    assert body["same_filesystem"] is True
    assert body["worker_alive"] is True
    assert body["active_jobs"] == 0
    assert body["mappings"] == 0
    assert body["mappings_ever_loaded"] is False
    assert body["mappings_degraded"] is False


def test_health_flags_a_dead_canary_task(tmp_path: Path, monkeypatch):
    # Same precedent as worker_alive: a background task that silently
    # stopped doing its job must not look like one that is doing it. A dead
    # canary would otherwise sit at "unknown" forever -- the one way this
    # feature could reintroduce the silence it exists to remove.
    async def _dies_immediately(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "svtplay_arr.canary.SvtCanary.run_forever", _dies_immediately
    )

    with TestClient(create_app(_settings(tmp_path))) as c:
        deadline = time.monotonic() + 2.0
        body = c.get("/health").json()
        while body["svt"]["alive"] and time.monotonic() < deadline:
            time.sleep(0.02)
            body = c.get("/health").json()

        assert body["svt"]["alive"] is False
        assert body["svt"]["degraded"] is True
        assert body["status"] == "degraded"

        page = c.get("/config").text
        assert "SVT: NOT BEING CHECKED" in page
        assert 'class="status-chip error"' in page


def test_the_canary_checks_the_operators_own_mappings(tmp_path: Path, monkeypatch):
    # Not a hardcoded show: a hardcoded slug is a fixture that rots -- the
    # show ends, SVT retires the URL, and the canary reports a failure about
    # the fixture rather than the service. Checking the operator's real rows
    # answers "do my mappings still work" as a side effect.
    _mappings_file(
        tmp_path,
        (1, "gift-vid-forsta-ogonkastet", "Gift vid första ögonkastet"),
        (2, "morgonstudion", "Morgonstudion"),
    )
    seen = _svt_returns(
        monkeypatch,
        {
            "gift-vid-forsta-ogonkastet": [_episode(1), _episode(2)],
            "morgonstudion": [_episode(1), _episode(2)],
        },
    )

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        svt = c.get("/health").json()["svt"]

    assert sorted(seen) == ["gift-vid-forsta-ogonkastet", "morgonstudion"]
    assert svt["state"] == "ok"
    assert svt["checked"] == 2
    assert svt["failing"] == 0
    assert svt["episodes_seen"] == 4
    assert svt["last_success"] is not None


def test_every_mapping_failing_reads_as_svt_or_the_parser(
    tmp_path: Path, monkeypatch,
):
    # The urgent shape: nothing will be grabbed until it is fixed, and the
    # operator can do nothing about the cause but must know immediately.
    # Every slug here returns a page that parses to zero episodes, which is
    # exactly what an SVT format change looks like from this side.
    _mappings_file(tmp_path, (1, "a-show", "A Show"), (2, "b-show", "B Show"))
    _svt_returns(monkeypatch, {})

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        body = c.get("/health").json()
        page = c.get("/config").text

    assert body["svt"]["state"] == "svt"
    assert body["svt"]["checked"] == 2
    assert body["svt"]["failing"] == 2
    # The shape that must keep turning the light red: nothing will be
    # grabbed until it is fixed and the operator cannot fix the cause.
    assert body["svt"]["degraded"] is True
    assert body["status"] == "degraded"
    assert "SVT: FAILING" in page
    assert 'class="status-chip error"' in page


def test_one_mapping_failing_reads_as_that_show(tmp_path: Path, monkeypatch):
    # The other shape, and it needs a different action: this show ended, was
    # re-slugged, or moved, and the operator fixes it by editing one row. So
    # the row has to be nameable from the report -- a single boolean cannot
    # tell these two apart, which is the whole reason this reports counts.
    _mappings_file(tmp_path, (1, "a-show", "A Show"), (2, "b-show", "B Show"))
    _svt_returns(monkeypatch, {"a-show": [_episode(1)]})

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        body = c.get("/health").json()
        page = c.get("/config").text

    assert body["svt"]["state"] == "series"
    assert body["svt"]["checked"] == 2
    assert body["svt"]["failing"] == 1
    assert [f["tvdb_id"] for f in body["svt"]["failing_series"]] == [2]
    assert body["svt"]["needs_attention"] is True

    # ...and the top-level light stays green. A dead row is real and it is
    # the operator's to fix, but if it held /health red until they got round
    # to deleting it, every monitoring setup polling this endpoint would have
    # a permanently red check inside a week -- and the `svt` shape, which is
    # the one that means nothing will be grabbed, would then arrive on a
    # channel everyone had learned to ignore. Same defect as the installer
    # warning that fired on 100% of fresh installs.
    assert body["svt"]["degraded"] is False
    assert body["status"] == "ok"

    # Nothing is hidden, though: /health names the failing row (above) and
    # the page renders it at full width. Scoped to the canary's own banner,
    # because the mappings table below prints every series title -- an
    # unscoped search for "B Show" would pass with nothing about the canary
    # rendered at all.
    banner = _canary_banner(page)
    assert "B Show" in banner
    assert "b-show" in banner
    assert "1 of 2 mappings" in banner
    # Amber, not red: the two shapes differ in urgency as well as wording.
    assert 'class="status-chip warn"' in page
    # The urgent shape's wording must not appear for a single failing show:
    # it would send the operator looking for an outage that is not there.
    assert "SVT: FAILING" not in page


def test_the_config_page_and_health_agree_about_the_canary(
    tmp_path: Path, monkeypatch,
):
    # One computation, two surfaces. Two places deriving one fact and
    # drifting apart is this codebase's most common defect class, and a
    # status strip disagreeing with /health would be worse than no strip --
    # the operator trusts the one in front of them.
    _mappings_file(tmp_path, (1, "a-show", "A Show"))
    _svt_returns(monkeypatch, {})

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        body = c.get("/health").json()
        page = c.get("/config").text

    assert body["svt"]["state"] == "svt"
    assert body["status"] == "degraded"
    assert "Service: degraded" in page
    assert "SVT: FAILING" in page
    # The count the page prints is the count /health computed, not a second
    # tally taken while rendering.
    assert f"{body['svt']['checked']} mapping" in page


def test_a_canary_round_that_raises_leaves_the_service_healthy(
    tmp_path: Path, monkeypatch,
):
    # The canary failing must never degrade the service: an exception in it
    # cannot kill the loop, the worker, or a request.
    async def _boom(self):
        raise RuntimeError("canary is on fire")

    monkeypatch.setattr("svtplay_arr.canary.SvtCanary.run_once", _boom)

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        asyncio.run(app.state.svt_canary.run_once_guarded())
        body = c.get("/health").json()
        assert body["worker_alive"] is True
        assert body["svt"]["alive"] is True
        assert body["status"] == "ok"
        assert c.get("/config").status_code == 200


def test_a_dead_canary_task_still_turns_the_light_red(tmp_path: Path, monkeypatch):
    # The case that matters most now that one failing show does not. With
    # `series` off the top-level verdict, a dead canary and the `svt` shape
    # are the whole of what stands between the operator and a month of
    # silently missing episodes -- so both must reach `status`.
    async def _dies_immediately(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "svtplay_arr.canary.SvtCanary.run_forever", _dies_immediately
    )
    # A perfectly healthy mapping, so nothing *else* could account for the
    # degrade: the only thing wrong is that nothing is checking.
    _mappings_file(tmp_path, (1, "a-show", "A Show"))
    _svt_returns(monkeypatch, {"a-show": [_episode(1)]})

    with TestClient(create_app(_settings(tmp_path))) as c:
        deadline = time.monotonic() + 2.0
        body = c.get("/health").json()
        while body["svt"]["alive"] and time.monotonic() < deadline:
            time.sleep(0.02)
            body = c.get("/health").json()

        assert body["svt"]["alive"] is False
        assert body["svt"]["degraded"] is True
        assert body["svt"]["needs_attention"] is True
        assert body["status"] == "degraded"


def test_health_never_500s_on_a_broken_canary(tmp_path: Path, monkeypatch):
    # Same rule as the mapping table's own guard: /health is monitoring
    # infrastructure and must not be able to fail the thing it monitors.
    def _boom(self):
        raise RuntimeError("canary state is on fire")

    monkeypatch.setattr("svtplay_arr.canary.SvtCanary.status", _boom)

    with TestClient(create_app(_settings(tmp_path))) as c:
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["svt"]["state"] == "unavailable"
        assert resp.json()["svt"]["degraded"] is True
        assert resp.json()["status"] == "degraded"
        assert c.get("/config").status_code == 200


def test_the_canary_never_writes_anything(tmp_path: Path, monkeypatch):
    # Read-only observation. It may not call the resolver's write paths or
    # the mapping writer, and nothing on disk may move because it ran.
    _mappings_file(tmp_path, (1, "a-show", "A Show"))
    _svt_returns(monkeypatch, {"a-show": [_episode(1)]})

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        before = _tree_digest(tmp_path)
        _run_a_canary_round(app)
        _run_a_canary_round(app)
        assert _tree_digest(tmp_path) == before
        assert c.get("/health").json()["active_jobs"] == 0
        assert app.state.job_store.all_active() == []


def _canary_banner(page: str) -> str:
    """The canary's own banner, isolated from the rest of the page.

    The mappings table renders every series title, so an assertion made
    against the whole page can pass while the canary rendered nothing at
    all. The banner is the first `<p class="error">` after the status
    strip, which is where base.html puts it.
    """
    strip_end = page.index('class="status-strip"')
    strip_end = page.index("</div>", strip_end)
    starts = [
        i
        for i in (
            page.find('<p class="error">', strip_end),
            page.find('<p class="warn">', strip_end),
        )
        if i != -1
    ]
    assert starts, "no canary banner on the page"
    start = min(starts)
    return " ".join(page[start: page.index("</p>", start)].split())


def _tree_digest(root: Path) -> dict:
    return {
        str(p): (p.stat().st_mtime, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_shutdown_cancels_the_canary_task(tmp_path: Path):
    # Same lifetime as the worker, and for a concrete reason: the canary
    # drives the shared httpx client, so it has to be stopped before the
    # lifespan closes it.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        assert c.get("/health").json()["svt"]["alive"] is True
    assert app.state.svt_canary_task.done() is True


def test_health_never_exposes_the_api_key_through_the_canary(tmp_path: Path):
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        assert s.sonarr_api_key not in c.get("/health").text


# --- The Activity view reads the app's own job store -------------------


def test_the_activity_view_shows_a_job_from_the_services_own_store(
    tmp_path: Path,
):
    # The whole seam, end to end: a row written through the store the
    # worker uses reaches the config page. Every other test of this view
    # hands the router a fake provider, so this is the one that would fail
    # if `compute_activity` were wired to the wrong store, or to none.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        job = app.state.job_store.create("svt-1", "Stem - S01E01", "WEBDL-1080p", 10)
        app.state.job_store.fail(job.nzo_id, "svtplay-dl exited 1")

        body = c.get("/config/activity").text

    assert "Stem - S01E01" in body
    assert "svtplay-dl exited 1" in body


def test_the_activity_view_reports_an_unreadable_store_as_unreadable(
    tmp_path: Path, monkeypatch
):
    # The distinction the whole view turns on. Unlike /health -- which
    # swallows a store failure and reports `active_jobs: null` because
    # Sonarr may be polling it -- this must not degrade to an empty list:
    # "nothing has failed" and "the failures cannot be read" are different
    # answers, and only one of them is true here.
    def _boom(self):
        raise JobStoreError("db is on fire")

    monkeypatch.setattr("svtplay_arr.store.JobStore.all_jobs", _boom)

    with TestClient(create_app(_settings(tmp_path))) as c:
        resp = c.get("/config/activity")

    assert resp.status_code == 200
    assert "could not be read" in resp.text
    assert "Nothing has been downloaded yet" not in resp.text


def test_the_activity_view_does_not_change_healths_contract(tmp_path: Path):
    # /health is Sonarr-facing infrastructure; some setups poll it. The
    # Activity view reads the same store through a separate provider and
    # must add nothing to, and remove nothing from, this response.
    with TestClient(create_app(_settings(tmp_path))) as c:
        body = c.get("/health").json()

    # This set is the whole contract, so every change to it is a deliberate
    # one made here rather than discovered downstream. `sonarr` was added on
    # 2026-08-28 alongside the background Sonarr check, on the same terms
    # `svt` was: purely additive, nothing above it removed, renamed or
    # retyped, and the two nested blocks are the only place any new field
    # goes. `version` was added the same way, alongside deriving it from the
    # git tag instead of a hand-maintained pyproject.toml field -- see
    # version.py.
    assert set(body) == {
        "status", "same_filesystem", "worker_alive", "active_jobs", "svt",
        "sonarr",
        "mappings", "mappings_ever_loaded", "mappings_degraded",
        "version",
    }


def test_the_activity_view_never_exposes_the_api_key(tmp_path: Path):
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        assert s.sonarr_api_key not in c.get("/config/activity").text


# --- Per-mapping canary state reaches the Mappings view ----------------


def test_the_mappings_view_shows_the_apps_own_canary_state(
    tmp_path: Path, monkeypatch
):
    # End to end through the service's real canary: a row that stops
    # resolving is visible in the Mappings view without anyone pressing
    # Check. One canary, one set of findings -- this reads the same
    # in-memory state /health's `svt` block is computed from.
    _mappings_file(
        tmp_path,
        (1, "gift-vid-forsta-ogonkastet", "Gift vid första ögonkastet"),
        (2, "morgonstudion", "Morgonstudion"),
    )
    _svt_returns(
        monkeypatch,
        {
            "gift-vid-forsta-ogonkastet": [_episode(1), _episode(2)],
            # Answers, parses to nothing: what a retired show or a broken
            # parser looks like from here.
            "morgonstudion": [],
        },
    )

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        body = c.get("/config/mappings").text
        health = c.get("/health").json()

    assert "Failing" in body
    assert "listed no episodes" in body
    # ...and the page and /health are describing the same one failing row.
    assert health["svt"]["failing"] == 1


def test_the_mappings_view_never_calls_svt_to_render_that_state(
    tmp_path: Path, monkeypatch
):
    # The state is the canary's, already collected on its own slow loop. A
    # render that fired a request per mapping would be a new way to hammer
    # SVT's unofficial API, on the page an operator refreshes.
    _mappings_file(tmp_path, (1, "gift-vid-forsta-ogonkastet", "Gift"))
    seen = _svt_returns(monkeypatch, {"gift-vid-forsta-ogonkastet": [_episode(1)]})

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        seen.clear()
        c.get("/config/mappings")
        c.get("/config")

    assert seen == []


# --- The mapping that resolves nothing reaches /health and the page ----
#
# A mapping can pass every check above -- right slug, HTTP 200, a full
# episode list -- and still never match anything Sonarr has, which is the
# one failure the SVT half is structurally unable to see. These are the
# wiring: one canary, one computation, and both surfaces rendering it.


def _sonarr_library(monkeypatch, per_tvdb: dict, *, error=None) -> list[int]:
    """Point the app's real SonarrClient at a canned library.

    Patched on the class `create_app` constructs, so the canary is
    exercised through the same client the resolver matches with. Returns
    the list every requested series id is appended to, which is what pins
    the request cost.
    """
    series_ids = {tvdb: 1000 + tvdb for tvdb in per_tvdb}
    asked: list[int] = []

    async def _all_series(self):
        if error is not None:
            raise error
        return [{"tvdbId": t, "id": i} for t, i in series_ids.items()]

    async def _episodes(self, series_id):
        asked.append(series_id)
        for tvdb, sid in series_ids.items():
            if sid == series_id:
                return list(per_tvdb[tvdb])
        return []

    monkeypatch.setattr("svtplay_arr.sonarr.SonarrClient.all_series", _all_series)
    monkeypatch.setattr("svtplay_arr.sonarr.SonarrClient.episodes", _episodes)
    return asked


def _sonarr_episode(i: int, air_date: date) -> SonarrEpisode:
    return SonarrEpisode(
        series_id=0, season=1, episode=i, air_date=air_date, title="TBA",
    )


def _unmatchable(n: int) -> list[SonarrEpisode]:
    """Sonarr episodes that have aired and can agree with nothing.

    Same numbers as `_episode`, a year away from its 2026-08-20, so both
    sides carry real content and no pair of them can ever match.
    """
    return [_sonarr_episode(i, date(2025, 8, 20)) for i in range(1, n + 1)]


def test_health_reports_a_mapping_that_can_never_resolve(
    tmp_path: Path, monkeypatch,
):
    _mappings_file(tmp_path, (1, "a-show", "A Show"))
    _svt_returns(monkeypatch, {"a-show": [_episode(1), _episode(2)]})
    _sonarr_library(monkeypatch, {1: _unmatchable(2)})

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        body = c.get("/health").json()

    svt = body["svt"]
    assert svt["unresolvable"] == 1
    assert svt["unresolvable_series"][0]["series_title"] == "A Show"
    assert svt["unresolvable_series"][0]["reason"] == "no_air_date"
    # The SVT half is untouched: the slug resolves and lists episodes.
    assert svt["failing"] == 0
    assert svt["episodes_seen"] == 2
    # Prominent, and not red. One show being inert does not stop anything
    # else working, and a light that stays red over it is one nobody reads
    # by the time SVT itself breaks -- see DEGRADED_STATES in canary.py.
    assert svt["needs_attention"] is True
    assert svt["degraded"] is False
    assert body["status"] == "ok"


def test_the_page_says_which_mappings_resolve_nothing_and_why(
    tmp_path: Path, monkeypatch,
):
    _mappings_file(tmp_path, (1, "a-show", "A Show"))
    _svt_returns(monkeypatch, {"a-show": [_episode(1), _episode(2)]})
    _sonarr_library(monkeypatch, {1: _unmatchable(2)})

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        page = c.get("/config").text
        mappings = c.get("/config/mappings").text
        health = c.get("/health").json()

    assert "resolve" in page and "nothing" in page
    assert "A Show" in page
    assert 'class="status-chip warn"' in page
    # ...and the row itself, on the page that lists the rows.
    assert "Resolves nothing" in mappings
    # One computation behind both: the page renders the same note /health
    # carries rather than deriving a second opinion of it, verbatim. Escaped
    # the way the template escapes it, so this compares the rendered text
    # rather than a paraphrase of it.
    note = str(escape(health["svt"]["unresolvable_series"][0]["note"]))
    assert note in mappings
    assert note in page


def test_a_mapping_that_resolves_is_not_reported_as_resolving_nothing(
    tmp_path: Path, monkeypatch,
):
    _mappings_file(tmp_path, (1, "a-show", "A Show"))
    _svt_returns(monkeypatch, {"a-show": [_episode(1), _episode(2)]})
    _sonarr_library(
        monkeypatch, {1: [_sonarr_episode(i, date(2026, 8, 20)) for i in (1, 2)]}
    )

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        body = c.get("/health").json()
        page = c.get("/config").text

    assert body["svt"]["state"] == "ok"
    assert body["svt"]["unresolvable"] == 0
    assert body["status"] == "ok"
    assert "resolves nothing" not in page.lower()


def test_a_sonarr_outage_degrades_only_the_resolvability_half(
    tmp_path: Path, monkeypatch,
):
    # The SVT half must keep working and the page must keep rendering. The
    # unresolvable count must not read as a clean sweep, which is what a
    # bare 0 would look like.
    _mappings_file(tmp_path, (1, "a-show", "A Show"))
    _svt_returns(monkeypatch, {"a-show": [_episode(1)]})
    _sonarr_library(
        monkeypatch, {1: _unmatchable(1)},
        error=SonarrApiError(
            REASON_MESSAGES[REASON_REFUSED], reason=REASON_REFUSED
        ),
    )

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        body = c.get("/health").json()
        page = c.get("/config").text

    svt = body["svt"]
    assert svt["state"] == "ok"
    assert svt["failing"] == 0
    assert svt["episodes_seen"] == 1
    assert svt["unresolvable"] == 0
    assert svt["resolvability_unknown"] == 1
    assert svt["resolvability_error"] == REASON_MESSAGES[REASON_REFUSED]
    assert "resolves nothing" not in page.lower()
    assert "sekrit-sonarr-api-key" not in page


def test_the_resolvability_check_costs_one_episode_call_per_mapping(
    tmp_path: Path, monkeypatch,
):
    _mappings_file(tmp_path, (1, "a-show", "A Show"), (2, "b-show", "B Show"))
    _svt_returns(monkeypatch, {"a-show": [_episode(1)], "b-show": [_episode(1)]})
    asked = _sonarr_library(
        monkeypatch, {1: _unmatchable(1), 2: _unmatchable(1)}
    )

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        c.get("/config")
        c.get("/config/mappings")
        c.get("/health")

    # One per mapping for the round, and not one more for any render: the
    # page reads what the canary already collected.
    assert sorted(asked) == [1001, 1002]


def test_health_never_exposes_the_api_key_through_the_resolvability_check(
    tmp_path: Path, monkeypatch,
):
    _mappings_file(tmp_path, (1, "a-show", "A Show"))
    _svt_returns(monkeypatch, {"a-show": [_episode(1)]})
    _sonarr_library(
        monkeypatch, {1: _unmatchable(1)},
        error=RuntimeError("X-Api-Key: sekrit-sonarr-api-key"),
    )

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_canary_round(app)
        body = c.get("/health").text
        page = c.get("/config").text
        mappings = c.get("/config/mappings").text

    for rendered in (body, page, mappings):
        assert "sekrit-sonarr-api-key" not in rendered


def _set_created_at(tmp_path: Path, nzo_id: str, when: str) -> None:
    """Backdate a job, bypassing JobStore.

    The schema's own default is `datetime('now')`, i.e. UTC at *second*
    resolution -- so several jobs created in one test land on the same
    timestamp and `ORDER BY created_at` has nothing to order them by. Any
    test about ordering has to set the column itself or it is asserting on
    whatever sqlite happened to return.
    """
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("UPDATE jobs SET created_at = ? WHERE nzo_id = ?", (when, nzo_id))
    conn.commit()
    conn.close()


def _finished(store, tmp_path: Path, stem: str, day: int, fail: str | None = None):
    job = store.create("svt", stem, "WEBDL-1080p", 1)
    if fail is None:
        store.complete(job.nzo_id, "/tmp/x.mkv")
    else:
        store.fail(job.nzo_id, fail)
    _set_created_at(tmp_path, job.nzo_id, f"2026-08-{day:02d} 10:00:00")
    return job


def test_the_activity_view_lists_the_newest_finished_job_first(tmp_path: Path):
    # The store returns rows oldest first, which is what Sonarr's queue
    # wants and the opposite of what a human reading a log wants. Dropping
    # the reversal leaves a page that looks perfectly plausible and is in
    # the wrong order.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        store = app.state.job_store
        _finished(store, tmp_path, "Oldest - S01E01", 10)
        _finished(store, tmp_path, "Middle - S01E02", 15)
        _finished(store, tmp_path, "Newest - S01E03", 20)

        body = c.get("/config/activity").text

    assert body.index("Newest - S01E03") < body.index("Middle - S01E02")
    assert body.index("Middle - S01E02") < body.index("Oldest - S01E01")


def test_todays_failure_is_not_pushed_off_the_page_by_older_successes(
    tmp_path: Path,
):
    # The concrete scenario the ordering exists for: a library that has
    # been running a while has more finished jobs than one page shows. If
    # the oldest survive the cut, today's failed grab is off the page
    # entirely -- on the one view whose stated purpose is "why didn't that
    # episode arrive?".
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        store = app.state.job_store
        for i in range(60):
            _finished(store, tmp_path, f"Old {i:02d} - S01E01", 10)
        _finished(store, tmp_path, "Missing - S02E04", 26,
                  fail="svtplay-dl exited 1: no streams found")

        body = c.get("/config/activity").text

    assert "Missing - S02E04" in body
    assert "svtplay-dl exited 1: no streams found" in body


def test_the_activity_view_is_bounded_however_long_the_history_is(
    tmp_path: Path,
):
    # Sonarr deletes history entries as it imports, so a healthy install
    # stays well under this. A broken one will not, and the cap is what
    # keeps one page render bounded.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        store = app.state.job_store
        for i in range(60):
            _finished(store, tmp_path, f"Episode {i:02d}", 10 + (i % 20))

        body = c.get("/config/activity").text

    assert body.count('<li class="job">') == 50, (
        "the finished-job list is not bounded at the documented limit"
    )


def test_health_does_not_read_the_store_on_the_event_loop(tmp_path: Path):
    # The same argument the config page's status strip makes, and it
    # applies here at least as strongly: /health is polled on a schedule
    # by a monitor rather than loaded by hand, and compute_health blocks --
    # it reads the same JobStore the download worker writes job progress
    # through, and stats both download directories. Reading it inline from
    # an async route runs all of that on the loop the worker runs on.
    #
    # Asked of asyncio rather than of thread identity: inside an
    # asyncio.to_thread worker there is no running loop, and there is
    # nowhere else this could be called from where that is true.
    where = []

    def _all_active(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            where.append("off the loop")
        else:
            where.append("on the loop")
        return []

    with TestClient(create_app(_settings(tmp_path))) as c:
        # Patched after startup so the worker's own use of the store is
        # not what this observes.
        from svtplay_arr.store import JobStore

        original = JobStore.all_active
        JobStore.all_active = _all_active
        try:
            resp = c.get("/health")
        finally:
            JobStore.all_active = original

    assert resp.status_code == 200
    assert where, "/health never read the store"
    assert set(where) == {"off the loop"}, where


# --- The Sonarr check reaches /health and the status strip -----------------
#
# The same gap the SVT canary closed, on the dependency that matters more:
# without Sonarr's air dates the resolver cannot resolve anything at all, so
# a wrong URL or a rotated key means every search and every RSS poll
# silently returns nothing -- while every field on /health stayed green,
# because nothing in this service had ever asked Sonarr a question. These
# tests are about the wiring: one computation behind both surfaces, and a
# check whose own death is visible.


def _sonarr_returns(monkeypatch, outcome) -> list[int]:
    """Point the app's real SonarrClient.status at a canned answer.

    Patched on the class `create_app` actually constructs, so the check is
    exercised through the same client the resolver uses rather than through
    a stand-in wired up for the test. Returns a list appended to on each
    call, so "it asked Sonarr exactly once per round" is observable.
    """
    calls: list[int] = []

    async def _status(self):
        calls.append(1)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("svtplay_arr.sonarr.SonarrClient.status", _status)
    return calls


def _run_a_sonarr_round(app) -> None:
    """One round of the app's own Sonarr check, synchronously.

    The app's real check -- the object `/health` reports on -- not a second
    instance built for the test, which is the only way these tests say
    anything about the wiring.
    """
    asyncio.run(app.state.sonarr_canary.run_once())


def test_health_carries_the_sonarr_check_and_never_calls_it_healthy_unchecked(
    tmp_path: Path,
):
    # A fresh process has proved nothing about Sonarr. Reporting that as
    # "ok" would rebuild the exact defect this closes, one level up.
    with TestClient(create_app(_settings(tmp_path))) as c:
        sonarr = c.get("/health").json()["sonarr"]
    assert sonarr["state"] == "unknown"
    assert sonarr["state"] != "ok"
    assert sonarr["last_checked"] is None
    assert sonarr["last_success"] is None
    assert sonarr["series_count"] is None
    assert sonarr["alive"] is True


def test_an_unchecked_sonarr_does_not_cry_wolf(tmp_path: Path):
    # Not healthy, and not a degrade for the first interval after a restart
    # either -- a check that is red on every boot is one operators learn to
    # read past, which is the failure mode this project has shipped before.
    with TestClient(create_app(_settings(tmp_path))) as c:
        body = c.get("/health").json()
    assert body["sonarr"]["degraded"] is False
    assert body["status"] == "ok"


def test_a_working_sonarr_reports_its_version_and_series_count(
    tmp_path: Path, monkeypatch,
):
    calls = _sonarr_returns(
        monkeypatch, SonarrStatus(version="4.0.10.2544", series_count=42)
    )
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_sonarr_round(app)
        body = c.get("/health").json()
        page = c.get("/config").text

    assert calls == [1]
    assert body["sonarr"]["state"] == "ok"
    assert body["sonarr"]["version"] == "4.0.10.2544"
    assert body["sonarr"]["series_count"] == 42
    assert body["status"] == "ok"
    # The count on the page is the count /health computed, not a second
    # tally taken while rendering.
    assert "Sonarr: ok" in page
    assert "42 series" in page


def test_a_sonarr_that_is_down_turns_the_service_light_red(
    tmp_path: Path, monkeypatch,
):
    # The decision this feature turns on. Unlike one failing mapping row,
    # Sonarr has no partial failure: nothing resolves, nothing is grabbed,
    # and no search returns anything.
    _sonarr_returns(
        monkeypatch,
        SonarrApiError(REASON_MESSAGES[REASON_REFUSED], reason=REASON_REFUSED),
    )
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_sonarr_round(app)
        body = c.get("/health").json()
        page = c.get("/config").text

    assert body["sonarr"]["state"] == "sonarr"
    assert body["sonarr"]["degraded"] is True
    assert body["sonarr"]["last_error_reason"] == REASON_REFUSED
    assert body["status"] == "degraded"
    assert "Sonarr: FAILING" in page
    assert 'class="status-chip error"' in page


def test_the_config_page_and_health_agree_about_sonarr(
    tmp_path: Path, monkeypatch,
):
    # One computation, two surfaces. Two places deriving one fact and
    # drifting apart is this codebase's most common defect class, and a
    # strip that disagreed with /health would be worse than no strip.
    _sonarr_returns(
        monkeypatch,
        SonarrApiError(
            REASON_MESSAGES[REASON_UNAUTHORIZED], reason=REASON_UNAUTHORIZED
        ),
    )
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        _run_a_sonarr_round(app)
        body = c.get("/health").json()
        page = c.get("/config").text

    assert body["status"] == "degraded"
    assert "Service: degraded" in page
    assert "Sonarr: FAILING" in page
    # The sentence the page shows is the one /health carries, not a second
    # reading of the same failure.
    assert body["sonarr"]["last_error"].split(".")[0] in page


def test_health_flags_a_dead_sonarr_check_task(tmp_path: Path, monkeypatch):
    # Same precedent as worker_alive and the SVT canary: a background task
    # that silently stopped doing its job must not look like one that is
    # doing it, or this check becomes a second silence rather than the end
    # of the first.
    async def _dies_immediately(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "svtplay_arr.canary.SonarrCanary.run_forever", _dies_immediately
    )

    with TestClient(create_app(_settings(tmp_path))) as c:
        deadline = time.monotonic() + 2.0
        body = c.get("/health").json()
        while body["sonarr"]["alive"] and time.monotonic() < deadline:
            time.sleep(0.02)
            body = c.get("/health").json()

        assert body["sonarr"]["alive"] is False
        assert body["sonarr"]["degraded"] is True
        assert body["status"] == "degraded"

        page = c.get("/config").text
        assert "Sonarr: NOT BEING CHECKED" in page
        assert 'class="status-chip error"' in page


def test_health_never_500s_on_a_broken_sonarr_check(tmp_path: Path, monkeypatch):
    # /health is monitoring infrastructure and must not be able to fail the
    # thing it monitors.
    def _boom(self):
        raise RuntimeError("the Sonarr check's state is on fire")

    monkeypatch.setattr("svtplay_arr.canary.SonarrCanary.status", _boom)

    with TestClient(create_app(_settings(tmp_path))) as c:
        resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["sonarr"]["state"] == "unavailable"
        assert resp.json()["sonarr"]["degraded"] is True
        assert resp.json()["status"] == "degraded"
        assert c.get("/config").status_code == 200


def test_a_sonarr_round_that_raises_leaves_the_service_healthy(
    tmp_path: Path, monkeypatch,
):
    async def _boom(self):
        raise RuntimeError("the Sonarr check is on fire")

    monkeypatch.setattr("svtplay_arr.canary.SonarrCanary.run_once", _boom)

    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        asyncio.run(app.state.sonarr_canary.run_once_guarded())
        body = c.get("/health").json()
        assert body["worker_alive"] is True
        assert body["sonarr"]["alive"] is True
        assert c.get("/config").status_code == 200


def test_the_sonarr_check_never_writes_anything(tmp_path: Path, monkeypatch):
    # Read-only observation: nothing on disk may move because it ran, and
    # the job store may not gain a row.
    _sonarr_returns(
        monkeypatch, SonarrStatus(version="4.0.10.2544", series_count=3)
    )
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        before = _tree_digest(tmp_path)
        _run_a_sonarr_round(app)
        _run_a_sonarr_round(app)
        assert _tree_digest(tmp_path) == before
        assert c.get("/health").json()["active_jobs"] == 0
        assert app.state.job_store.all_active() == []


def test_shutdown_cancels_the_sonarr_check_task(tmp_path: Path):
    # Same lifetime as the worker and the SVT canary, and for the same
    # concrete reason: it drives the shared httpx client, so it has to be
    # stopped before the lifespan closes it.
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        assert c.get("/health").json()["sonarr"]["alive"] is True
    assert app.state.sonarr_canary_task.done() is True


def test_health_never_exposes_the_api_key_through_the_sonarr_check(
    tmp_path: Path, monkeypatch,
):
    # Every path: unchecked, working, and each classified failure. The key
    # is what this check authenticates with, so it is the one field with a
    # real route onto the endpoint external monitoring scrapes.
    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        assert s.sonarr_api_key not in c.get("/health").text

    for reason in REASON_MESSAGES:
        _sonarr_returns(
            monkeypatch,
            SonarrApiError(REASON_MESSAGES[reason], reason=reason),
        )
        app = create_app(s)
        with TestClient(app) as c:
            _run_a_sonarr_round(app)
            assert s.sonarr_api_key not in c.get("/health").text, reason
            assert s.sonarr_api_key not in c.get("/config").text, reason


def test_the_status_strip_never_exposes_the_api_key_through_the_check(
    tmp_path: Path, monkeypatch,
):
    # An exception carrying the key in its own message is the realistic way
    # this breaks: the check's catch-all must report in its own words, not
    # in the exception's.
    s = _settings(tmp_path)
    _sonarr_returns(
        monkeypatch, RuntimeError(f"X-Api-Key: {s.sonarr_api_key}")
    )
    app = create_app(s)
    with TestClient(app) as c:
        _run_a_sonarr_round(app)
        assert s.sonarr_api_key not in c.get("/health").text
        assert s.sonarr_api_key not in c.get("/config").text
        assert c.get("/health").json()["sonarr"]["state"] == "sonarr"


def test_the_settings_view_can_test_the_apps_own_sonarr_credentials(
    tmp_path: Path, monkeypatch,
):
    # The whole seam, end to end: the Test connection button reaches a real
    # SonarrClient built from the values that were posted. Every other test
    # of the control hands the router a fake probe, so this is the one that
    # would fail if `sonarr_probe` were wired to nothing, or to the client
    # bound to the booted key rather than to what was submitted.
    seen: list[str] = []

    async def _status(self):
        seen.append(self._headers["X-Api-Key"])
        return SonarrStatus(version="4.0.10.2544", series_count=7)

    monkeypatch.setattr("svtplay_arr.sonarr.SonarrClient.status", _status)

    s = _settings(tmp_path)
    with TestClient(create_app(s)) as c:
        r = c.post(
            "/config/settings/test",
            data={
                "sonarr_url": "http://typed.sonarr:8989",
                "sonarr_api_key": "TYPED-NOT-YET-SAVED",
            },
        )
    assert r.status_code == 200
    assert seen == ["TYPED-NOT-YET-SAVED"]
    assert "7 series" in r.text
    # ...and the key the service booted with is not what was tested, which
    # is the point: the operator is asking about the value in the form.
    assert s.sonarr_api_key not in seen


def test_the_environment_key_never_leaves_for_a_host_the_request_body_names(
    tmp_path: Path, monkeypatch,
):
    # End to end through a real create_app, because this is the one place
    # the guard's inputs come from the composition root: `booted.sonarr_url`
    # and the config file's own value.
    #
    # $SONARR_API_KEY is a value this page deliberately never renders and
    # never writes to disk (SECURITY.md says so). Substituting it for any
    # URL the form happened to carry would send it to whatever host a
    # request body names -- immediately, with no config write, no restart
    # and no log line carrying the value -- and there is no CSRF token on
    # this form and no Origin check in this service, so that is reachable
    # cross-site.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-ONLY-SECRET-NEVER-RENDERED")
    seen: list[tuple[str, str]] = []

    async def _status(self):
        seen.append((self._base, self._headers["X-Api-Key"]))
        return SonarrStatus(version="4.0.10.2544", series_count=7)

    monkeypatch.setattr("svtplay_arr.sonarr.SonarrClient.status", _status)

    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as c:
        away = c.post(
            "/config/settings/test",
            data={
                "sonarr_url": "http://attacker.invalid:9999",
                "sonarr_api_key": "ANY-JUNK",
            },
        )
        home = c.post(
            "/config/settings/test",
            data={
                "sonarr_url": settings.sonarr_url,
                "sonarr_api_key": "ANY-JUNK",
            },
        )

    assert away.status_code == 200 and home.status_code == 200
    assert seen == [
        # The URL from the body got the key from the body, and nothing else.
        ("http://attacker.invalid:9999", "ANY-JUNK"),
        # The URL this service is configured for still gets the effective
        # key, which is what a restart would use against it anyway.
        (settings.sonarr_url, "ENV-ONLY-SECRET-NEVER-RENDERED"),
    ]
    assert "ENV-ONLY-SECRET-NEVER-RENDERED" not in away.text


# --- Shutdown stops the downloads before it closes the store ----------


def test_shutdown_stops_in_flight_downloads_before_closing_the_store(
    tmp_path: Path, monkeypatch
):
    """The lifespan cancelled the worker's poll loop and then closed the job
    store -- but the downloads that loop had dispatched are separate tasks,
    and cancelling their parent does nothing to them. They ran on into a
    closed store and died with a JobStoreError wherever they had got to.

    The bad window is between publishing a finished file into `completed/`
    and recording it: the file is there for Sonarr to import, the row still
    says Downloading, the next start fails that row, Sonarr re-grabs the
    episode, and the first copy is orphaned in the library.

    Asserted at the moment the store is closed rather than on the wreckage
    afterwards. That window is microseconds wide, so a test that waited for
    it to be hit would be a coin toss; "no download was still running when
    the store went away" is the property that closes it, and it is exactly
    what `Worker.drain` is for.
    """
    from svtplay_arr.downloader import FakeDownloader
    from svtplay_arr.models import JobStatus

    monkeypatch.setattr(
        "svtplay_arr.app.SvtplayDlDownloader",
        lambda *a, **k: FakeDownloader(steps=500, total_bytes=100, delay=0.01),
    )
    s = _settings(tmp_path)
    app = create_app(s)
    store = app.state.job_store
    job = store.create("a", "stem", "WEBDL-1080p", 100)

    still_running: list[str] = []
    # The bound method, so the suite's own leak tracking still runs.
    tracked_close = store.close

    def recording_close():
        try:
            still_running.extend(
                repr(task)
                for task in asyncio.all_tasks()
                if not task.done() and "run_job" in repr(task)
            )
        except RuntimeError:  # pragma: no cover - close off the loop
            pass
        return tracked_close()

    store.close = recording_close

    with TestClient(app) as c:
        # Wait until the worker has actually picked the job up, or this
        # would pass by shutting down before anything was in flight.
        deadline = time.monotonic() + 5
        while (
            store.get(job.nzo_id).status is not JobStatus.DOWNLOADING
            and time.monotonic() < deadline
        ):
            c.get("/health")
        assert store.get(job.nzo_id).status is JobStatus.DOWNLOADING

    assert still_running == [], still_running


def test_a_failed_startup_stops_the_downloads_too(tmp_path: Path, monkeypatch):
    # The shutdown path drains before closing the store; the startup-failure
    # path did not. Unreachable today -- the only thing that can raise up
    # there runs before the poll loop exists -- but an asymmetry between two
    # branches that must do the same thing is what survives until the day
    # something between those lines starts raising.
    s = _settings(tmp_path)
    app = create_app(s)
    worker = app.state.worker
    store = app.state.job_store

    dispatched: list = []

    async def never_finishes():
        await asyncio.sleep(30)

    def explode():
        # Stand in for a download already dispatched when startup dies.
        task = asyncio.ensure_future(never_finishes())
        worker._jobs.add(task)
        dispatched.append(task)
        raise OSError("incomplete_dir is not readable")

    monkeypatch.setattr(worker, "sweep_incomplete", explode)

    running_at_close: list[bool] = []
    tracked_close = store.close  # bound, so the suite's leak tracking still runs

    def recording_close():
        running_at_close.append(any(not t.done() for t in dispatched))
        return tracked_close()

    store.close = recording_close

    with pytest.raises(OSError):
        with TestClient(app):
            pass  # pragma: no cover - startup raises before this runs

    assert dispatched, "the stand-in download was never created"
    # Asked at the moment the store is closed, not afterwards: the test
    # client cancels whatever is left when its event loop goes away, so
    # every task looks cancelled by the time this returns whether or not
    # anything here did it in the right order.
    assert running_at_close == [False], running_at_close
    # And the store was still closed, so the suite's leak guard stays quiet.
    with pytest.raises(Exception):
        store.all_jobs()
