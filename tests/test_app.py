import os
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient
from svtplay_arr.app import create_app
from svtplay_arr.config import ConfigError, Settings
from svtplay_arr.models import Release
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
        assert f'value="{s.sonarr_url}"' in c.get("/config").text


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
        body = c.get("/config").text
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
