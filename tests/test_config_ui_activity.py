"""The Activity view: what is in flight, and what recently happened.

The job store has held this since the SAB endpoints were written; the UI
has simply never shown it. Until now, when a grab failed the only record
of why was `journalctl`.

Two properties are worth more than the rest here, and both are about not
lying:

* **A store that cannot be read is not a store with nothing in it.** An
  empty list rendered as "nothing has failed" is the exact defect this
  project keeps rediscovering -- the same one behind the mappings table's
  three-way empty state and the canary's `unknown` vs `ok`. It has to be
  impossible to reach that sentence from a failed read.
* **A failure is more interesting than a success.** "Why didn't that
  episode arrive?" is the question this view exists for, so a failed grab
  has to carry whatever the store recorded about why.
"""

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from svtplay_arr.api.config_ui import VIEWS, build_config_router
from svtplay_arr.mappings import add_mapping

TITLE = "Gift vid första ögonkastet"


class FakeSvt:
    async def search_series(self, query):
        return []

    async def list_episodes(self, slug):
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
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    return cfg, maps


def _job(**over) -> dict:
    job = {
        "nzo_id": "SVTPLAY-abc123",
        "stem": "Gift vid första ögonkastet - S01E03 - WEBDL-1080p",
        "quality": "WEBDL-1080p",
        "status": "Completed",
        "size_bytes": 1_500_000_000,
        "downloaded_bytes": 1_500_000_000,
        "storage_path": "/srv/media/complete",
        "fail_message": None,
        "created_at": "2026-08-25 19:04:11",
    }
    job.update(over)
    return job


def _client(tmp_path: Path, activity_provider=None) -> TestClient:
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            activity_provider=activity_provider,
        )
    )
    return TestClient(app)


def _static(active=(), history=()):
    return lambda: {"active": list(active), "history": list(history)}


def _text(html: str) -> str:
    """The rendered page with its tags stripped, for prose assertions."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


ACTIVITY_PAGES = ["/config", "/config/activity"]


# --- The view exists and is part of the page --------------------------


def test_activity_is_one_of_the_views(tmp_path: Path):
    assert ("activity", "Activity", "/config/activity") in VIEWS


def test_the_activity_view_lists_what_is_in_flight(tmp_path: Path):
    provider = _static(
        active=[_job(status="Downloading", downloaded_bytes=750_000_000,
                     stem="Sportnytt - S01E02 - WEBDL-1080p")],
    )
    body = _client(tmp_path, provider).get("/config/activity").text

    assert "Sportnytt - S01E02 - WEBDL-1080p" in body
    assert "Downloading" in body
    # Progress, because "in flight" with no number against it does not
    # distinguish a download that is moving from one that is wedged.
    assert "50%" in body


def test_the_activity_view_lists_what_recently_finished(tmp_path: Path):
    provider = _static(history=[_job(stem="Done - S01E01 - WEBDL-1080p")])
    body = _client(tmp_path, provider).get("/config/activity").text

    assert "Done - S01E01 - WEBDL-1080p" in body
    assert "Completed" in body
    assert "2026-08-25 19:04:11" in body


# --- Failures are the point -------------------------------------------


@pytest.mark.parametrize("path", ACTIVITY_PAGES)
def test_a_failed_grab_says_why_the_store_says_it_failed(
    tmp_path: Path, path: str
):
    # Before this view existed, the only record of this string was
    # journalctl on the host. It is the entire answer to "why didn't that
    # episode arrive?", so it has to be on the page rather than implied by
    # the word "Failed".
    provider = _static(history=[
        _job(
            status="Failed",
            stem="Missing - S02E04 - WEBDL-1080p",
            fail_message="svtplay-dl exited 1: no streams found",
            downloaded_bytes=0,
        ),
    ])
    body = _client(tmp_path, provider).get(path).text

    assert "Missing - S02E04 - WEBDL-1080p" in body
    assert "svtplay-dl exited 1: no streams found" in body


def test_a_failure_is_marked_by_more_than_its_colour(tmp_path: Path):
    # The theme rules apply here like everywhere else on this page: hue is
    # never the only carrier. The status word is in the markup.
    provider = _static(history=[
        _job(status="Failed", fail_message="boom"),
        _job(status="Completed", nzo_id="SVTPLAY-def456"),
    ])
    body = _client(tmp_path, provider).get("/config/activity").text

    assert "Failed" in _text(body)
    assert "Completed" in _text(body)


def test_a_failure_with_no_recorded_reason_says_so_rather_than_nothing(
    tmp_path: Path,
):
    # `fail_message` is nullable. Rendering a failed job with a blank
    # explanation reads as a failure nobody bothered to explain; it is
    # actually a failure the store has nothing about, and the next place
    # to look is the log.
    provider = _static(history=[_job(status="Failed", fail_message=None)])
    body = _text(_client(tmp_path, provider).get("/config/activity").text)

    assert "recorded no reason" in body


# --- Failure is not emptiness -----------------------------------------


@pytest.mark.parametrize("path", ACTIVITY_PAGES)
def test_a_store_that_cannot_be_read_is_never_rendered_as_nothing_happened(
    tmp_path: Path, path: str
):
    # The distinction this project keeps getting wrong. A provider that
    # raises must produce a page that says the store could not be read --
    # never one that says there is nothing to show, which would tell an
    # operator whose database is broken that no download has ever failed.
    def _boom():
        raise RuntimeError("database is locked")

    r = _client(tmp_path, _boom).get(path)
    body = _text(r.text)

    assert r.status_code == 200
    assert "could not be read" in body
    assert "Nothing has been downloaded yet" not in body
    assert "Nothing is downloading" not in body


@pytest.mark.parametrize("path", ACTIVITY_PAGES)
def test_an_empty_store_says_nothing_has_happened_and_is_not_an_error(
    tmp_path: Path, path: str
):
    # The other half: a fresh install genuinely has nothing, and must not
    # be shown a scary "could not be read".
    r = _client(tmp_path, _static()).get(path)
    body = _text(r.text)

    assert r.status_code == 200
    assert "Nothing has been downloaded yet" in body
    assert "could not be read" not in body


@pytest.mark.parametrize("path", ACTIVITY_PAGES)
def test_no_activity_provider_at_all_is_not_nothing_happened_either(
    tmp_path: Path, path: str
):
    # A router built without one -- as every test that does not care about
    # activity builds it -- knows nothing about the store. That is a third
    # state, and it is not "no downloads".
    body = _text(_client(tmp_path, None).get(path).text)

    assert "Nothing has been downloaded yet" not in body
    assert "not available" in body


def test_the_degraded_activity_page_still_has_its_nav(tmp_path: Path):
    # A degraded page is still a page: the operator has to be able to get
    # to Settings and Mappings from it.
    def _boom():
        raise RuntimeError("database is locked")

    body = _client(tmp_path, _boom).get("/config/activity").text

    assert '<nav class="nav"' in body
    for _key, _label, path in VIEWS:
        assert f'href="{path}"' in body


# --- Where the read happens -------------------------------------------


@pytest.mark.parametrize("path", ACTIVITY_PAGES)
def test_the_store_read_does_not_happen_on_the_event_loop(
    tmp_path: Path, path: str
):
    # Every route here is `async def`, because JobStore drives one
    # sqlite3.Connection behind a blocking threading.Lock. That makes the
    # route a coroutine -- so a plain call would run a blocking sqlite
    # read *on the event loop*, the same loop the download worker runs on,
    # and a page render would stall the downloads it is reporting on.
    # asyncio.to_thread is what keeps both true; nothing else observes it.
    #
    # Asked of asyncio rather than of `threading.current_thread()`: the
    # test client runs the app on a thread of its own, so "not the main
    # thread" is true either way and a version of this test asserting that
    # passed with the hop removed. Inside a to_thread worker there is no
    # running loop at all, and that is not true of anywhere else this could
    # be called from.
    where = []

    def _provider():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            where.append("off the loop")
        else:
            where.append("on the loop")
        return {"active": [], "history": []}

    _client(tmp_path, _provider).get(path)

    assert where, "the activity provider was never called"
    assert set(where) == {"off the loop"}, where


# --- The Status view's own summary ------------------------------------


def test_the_status_view_shows_the_most_recent_activity(tmp_path: Path):
    provider = _static(
        active=[_job(status="Downloading", stem="Now - S01E01 - WEBDL-1080p")],
        history=[_job(stem="Then - S01E01 - WEBDL-1080p")],
    )
    body = _client(tmp_path, provider).get("/config").text

    assert "Now - S01E01 - WEBDL-1080p" in body
    assert "Then - S01E01 - WEBDL-1080p" in body
    assert 'href="/config/activity"' in body


def test_the_status_summary_is_bounded(tmp_path: Path):
    # The store keeps history until Sonarr deletes it. The landing view is
    # a summary, not the log.
    provider = _static(history=[
        _job(nzo_id=f"SVTPLAY-{i:06d}", stem=f"Episode {i}") for i in range(40)
    ])
    body = _client(tmp_path, provider).get("/config").text

    shown = [i for i in range(40) if f"Episode {i}<" in body]
    assert 0 < len(shown) <= 10, shown
    # ...and the full list is one click away rather than truncated silently.
    assert 'href="/config/activity"' in body


def test_the_status_view_puts_a_failure_in_front_of_a_success(tmp_path: Path):
    # A summary that shows the five most recent rows can hide the one
    # failure behind five later successes -- which is precisely the row the
    # operator opened this page for.
    provider = _static(history=[
        _job(nzo_id=f"SVTPLAY-ok{i}", stem=f"Fine {i}") for i in range(10)
    ] + [
        # Oldest, so a plain "most recent five" summary would bury it
        # behind five later successes -- which is exactly the row the
        # operator opened this page for.
        _job(nzo_id="SVTPLAY-fail", stem="Broken - S01E01",
             status="Failed", fail_message="svtplay-dl exited 1"),
    ])
    body = _client(tmp_path, provider).get("/config").text

    assert "Broken - S01E01" in body
    assert "svtplay-dl exited 1" in body
