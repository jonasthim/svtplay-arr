import asyncio
import sqlite3
from pathlib import Path

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile as StarletteUploadFile

from svtplay_arr.api.sab import build_sab_router
from svtplay_arr.store import JobStore


# Every store a test opens, closed by the autouse fixture below. A JobStore
# holds a sqlite3.Connection; left open it surfaces as "ResourceWarning:
# unclosed database" whenever the GC happens to get to it -- see
# JobStore.close for why that matters.
_OPEN_STORES: list[JobStore] = []


@pytest.fixture(autouse=True)
def _close_open_stores():
    yield
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "jobs.db")
    _OPEN_STORES.append(store)
    return store


NZB = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><head>'
    '<meta type="svt_id">KZmQ5JY</meta>'
    '<meta type="stem">Show - S15E01 - WEBDL-1080p</meta>'
    '<meta type="quality">WEBDL-1080p</meta>'
    '<meta type="size">1435287295</meta>'
    "</head></nzb>"
)


def _client(tmp_path: Path):
    store = _store(tmp_path)
    app = FastAPI()
    app.include_router(build_sab_router(store, tmp_path / "completed"))
    return TestClient(app), store


def _corrupt_status(tmp_path: Path, nzo_id: str, status: str) -> None:
    """Simulate a hand-edited/out-of-band row bypassing JobStore, so a
    listing call raises JobStoreError -- used to prove queue/history degrade
    instead of surfacing a 500 to Sonarr."""
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("UPDATE jobs SET status = ? WHERE nzo_id = ?", (status, nzo_id))
    conn.commit()
    conn.close()


def test_version(tmp_path: Path):
    c, _ = _client(tmp_path)
    assert c.get("/sabnzbd/api", params={"mode": "version"}).json()["version"]


def test_addfile_creates_job(tmp_path: Path):
    c, store = _client(tmp_path)
    r = c.post(
        "/sabnzbd/api",
        params={"mode": "addfile"},
        files={"name": ("release.nzb", NZB, "application/x-nzb")},
    )
    body = r.json()
    assert body["status"] is True
    nzo_id = body["nzo_ids"][0]
    assert store.get(nzo_id).svt_id == "KZmQ5JY"
    assert store.get(nzo_id).stem == "Show - S15E01 - WEBDL-1080p"
    # Without this the SAB queue reports 0% forever, which is the whole
    # reason SAB emulation was chosen over a blackhole client.
    assert store.get(nzo_id).size_bytes == 1435287295


def test_queue_reports_percentage(tmp_path: Path):
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 1000)
    store.update_progress(job.nzo_id, 250)
    slots = c.get("/sabnzbd/api", params={"mode": "queue"}).json()["queue"]["slots"]
    assert slots[0]["percentage"] == "25"
    assert slots[0]["nzo_id"] == job.nzo_id


def test_history_exposes_storage_path(tmp_path: Path):
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    store.complete(job.nzo_id, "/downloads/completed/stem.mkv")
    slot = c.get("/sabnzbd/api", params={"mode": "history"}).json()["history"]["slots"][0]
    assert slot["status"] == "Completed"
    assert slot["storage"] == "/downloads/completed/stem.mkv"


def test_history_reports_failure_message(tmp_path: Path):
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    store.fail(job.nzo_id, "geo-blocked")
    slot = c.get("/sabnzbd/api", params={"mode": "history"}).json()["history"]["slots"][0]
    assert slot["status"] == "Failed"
    assert slot["fail_message"] == "geo-blocked"


def test_get_config_reports_complete_dir(tmp_path: Path):
    c, _ = _client(tmp_path)
    cfg = c.get("/sabnzbd/api", params={"mode": "get_config"}).json()
    assert cfg["config"]["misc"]["complete_dir"] == str(tmp_path / "completed")


def test_get_config_exposes_categories(tmp_path: Path):
    # Sonarr's GetStatus() looks its configured category up in this list and
    # derives OutputRootFolders from it; TestCategory() runs on every save of
    # the download client and fails validation outright when the category is
    # missing -- so without this the operator cannot save the client at all.
    # `dir` must be present and must not end in "*": Sonarr calls
    # `Dir.TrimEnd('*')` on it, and reads a trailing "*" as "job folders
    # disabled" and fails validation for that instead.
    c, _ = _client(tmp_path)
    cfg = c.get("/sabnzbd/api", params={"mode": "get_config"}).json()
    categories = cfg["config"]["categories"]
    by_name = {entry["name"]: entry for entry in categories}
    assert "tv" in by_name, "Sonarr's configured category (see deploy/README.md)"
    assert "*" in by_name, "Sonarr's fallback when the named category is absent"
    for entry in categories:
        assert entry["dir"] == ""


# --- hardening: malformed / hostile / oversized input must never 500 -----


def test_addfile_rejects_malformed_xml(tmp_path: Path):
    c, store = _client(tmp_path)
    r = c.post(
        "/sabnzbd/api",
        params={"mode": "addfile"},
        files={"name": ("release.nzb", b"this is not xml <<<", "application/x-nzb")},
    )
    assert r.status_code == 200
    assert r.json()["status"] is False
    assert store.all_active() == []


def test_addfile_rejects_entity_expansion_bomb(tmp_path: Path):
    # This is exactly what defusedxml exists to stop: stdlib ElementTree
    # would happily expand this. Load-bearing, so it gets its own test.
    bomb = (
        '<?xml version="1.0"?>\n'
        "<!DOCTYPE lolz [\n"
        ' <!ENTITY lol "lol">\n'
        " <!ELEMENT lolz (#PCDATA)>\n"
        ' <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        "]>\n"
        "<lolz>&lol1;</lolz>"
    )
    c, store = _client(tmp_path)
    r = c.post(
        "/sabnzbd/api",
        params={"mode": "addfile"},
        files={"name": ("bomb.nzb", bomb, "application/x-nzb")},
    )
    assert r.status_code == 200
    assert r.json()["status"] is False
    assert store.all_active() == []


def test_addfile_rejects_oversized_upload(tmp_path: Path):
    # Still syntactically valid NZB/XML, just padded past the read cap, to
    # isolate the size guard from the malformed-XML path above.
    padding = "x" * (70 * 1024)
    huge_nzb = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><head>'
        '<meta type="svt_id">KZmQ5JY</meta>'
        '<meta type="stem">Show - S15E01 - WEBDL-1080p</meta>'
        f'<meta type="padding">{padding}</meta>'
        "</head></nzb>"
    )
    assert len(huge_nzb) > 64 * 1024
    c, store = _client(tmp_path)
    r = c.post(
        "/sabnzbd/api",
        params={"mode": "addfile"},
        files={"name": ("huge.nzb", huge_nzb, "application/x-nzb")},
    )
    assert r.status_code == 200
    assert r.json()["status"] is False
    assert store.all_active() == []


def test_addfile_rejects_nzb_missing_required_meta(tmp_path: Path):
    incomplete = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><head>'
        '<meta type="quality">WEBDL-1080p</meta>'
        "</head></nzb>"
    )
    c, store = _client(tmp_path)
    r = c.post(
        "/sabnzbd/api",
        params={"mode": "addfile"},
        files={"name": ("incomplete.nzb", incomplete, "application/x-nzb")},
    )
    assert r.status_code == 200
    assert r.json()["status"] is False
    assert store.all_active() == []


def test_addfile_requires_upload(tmp_path: Path):
    c, _ = _client(tmp_path)
    r = c.post("/sabnzbd/api", params={"mode": "addfile"})
    assert r.status_code == 200
    assert r.json()["status"] is False


# --- removal: SABnzbd's real protocol, which is not `mode=delete` ---------
#
# There is no top-level `mode=delete` in SABnzbd. Removal is
# `mode=queue&name=delete&value=NZO_ID` and
# `mode=history&name=delete&value=NZO_ID`, and Sonarr's SabnzbdProxy
# (RemoveFromQueue/RemoveFromHistory) sends exactly those. The previous
# implementation handled an invented `mode=delete` and declared no `name`
# parameter at all, so Sonarr's real queue-delete fell straight into the
# queue-listing branch: it returned the queue and deleted nothing.


def test_queue_delete_terminates_the_job(tmp_path: Path):
    # Sonarr's RemoveFromQueue. Removing a stuck item from Sonarr's queue is
    # the documented remedy for an interrupted job, and it was a silent no-op.
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    r = c.get(
        "/sabnzbd/api",
        params={"mode": "queue", "name": "delete", "del_files": 0, "value": job.nzo_id},
    )
    assert r.json()["status"] is True
    assert "queue" not in r.json(), "a delete must not answer with the queue listing"
    assert store.get(job.nzo_id).status.value == "Failed"


def test_history_delete_removes_the_row(tmp_path: Path):
    # Sonarr's RemoveFromHistory, which is how it cleans up after
    # failed-download handling. Failing the row instead of removing it would
    # leave it in history forever, so history would grow unbounded.
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    store.complete(job.nzo_id, "/downloads/completed/stem.mkv")
    r = c.get(
        "/sabnzbd/api",
        params={
            "mode": "history",
            "name": "delete",
            "del_files": 0,
            "archive": 1,
            "value": job.nzo_id,
        },
    )
    assert r.json()["status"] is True
    assert store.get(job.nzo_id) is None
    slots = c.get("/sabnzbd/api", params={"mode": "history"}).json()["history"]["slots"]
    assert slots == []


def test_queue_without_a_name_still_lists_the_queue(tmp_path: Path):
    # The delete branch must key on name == "delete", not on mode alone:
    # Sonarr polls plain mode=queue constantly.
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    slots = c.get(
        "/sabnzbd/api", params={"mode": "queue", "start": 0, "limit": 20}
    ).json()["queue"]["slots"]
    assert [s["nzo_id"] for s in slots] == [job.nzo_id]


def test_delete_accepts_comma_separated_ids(tmp_path: Path):
    c, store = _client(tmp_path)
    a = store.create("a", "stem-a", "WEBDL-1080p", 10)
    b = store.create("b", "stem-b", "WEBDL-1080p", 10)
    r = c.get(
        "/sabnzbd/api",
        params={"mode": "queue", "name": "delete", "value": f"{a.nzo_id},{b.nzo_id}"},
    )
    assert r.json()["status"] is True
    assert store.get(a.nzo_id).status.value == "Failed"
    assert store.get(b.nzo_id).status.value == "Failed"


def test_delete_all_empties_the_queue(tmp_path: Path):
    c, store = _client(tmp_path)
    a = store.create("a", "stem-a", "WEBDL-1080p", 10)
    b = store.create("b", "stem-b", "WEBDL-1080p", 10)
    r = c.get(
        "/sabnzbd/api", params={"mode": "queue", "name": "delete", "value": "all"}
    )
    assert r.json()["status"] is True
    assert store.all_active() == []
    assert store.get(a.nzo_id).status.value == "Failed"
    assert store.get(b.nzo_id).status.value == "Failed"


def test_history_delete_all_empties_history_without_touching_the_queue(tmp_path: Path):
    c, store = _client(tmp_path)
    done = store.create("a", "stem-a", "WEBDL-1080p", 10)
    store.complete(done.nzo_id, "/downloads/completed/stem-a.mkv")
    running = store.create("b", "stem-b", "WEBDL-1080p", 10)
    r = c.get(
        "/sabnzbd/api", params={"mode": "history", "name": "delete", "value": "all"}
    )
    assert r.json()["status"] is True
    assert store.get(done.nzo_id) is None
    assert store.get(running.nzo_id) is not None


def test_queue_delete_refuses_a_job_that_already_finished(tmp_path: Path):
    # Removal used to fail the job unconditionally, with no existence check
    # and no status check -- so a Completed job whose file had already landed
    # in completed/ was flipped to Failed, which Sonarr reads as a failed
    # download for a file it may already have imported.
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    store.complete(job.nzo_id, "/downloads/completed/stem.mkv")
    c.get(
        "/sabnzbd/api",
        params={"mode": "queue", "name": "delete", "value": job.nzo_id},
    )
    got = store.get(job.nzo_id)
    assert got.status.value == "Completed"
    assert got.storage_path == "/downloads/completed/stem.mkv"


def test_delete_of_an_unknown_id_does_not_error(tmp_path: Path):
    c, store = _client(tmp_path)
    r = c.get(
        "/sabnzbd/api",
        params={"mode": "queue", "name": "delete", "value": "SVTPLAY-nosuchjob"},
    )
    assert r.status_code == 200
    assert r.json()["status"] is True


def test_delete_without_value_does_not_error(tmp_path: Path):
    c, _ = _client(tmp_path)
    r = c.get("/sabnzbd/api", params={"mode": "queue", "name": "delete"})
    assert r.status_code == 200
    assert r.json()["status"] is False


def test_top_level_delete_mode_is_not_a_sabnzbd_mode(tmp_path: Path):
    # Guards against re-inventing it: `mode=delete` does not exist in
    # SABnzbd's API and nothing Sonarr sends uses it.
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    r = c.get("/sabnzbd/api", params={"mode": "delete", "value": job.nzo_id})
    assert r.json()["status"] is False
    assert store.get(job.nzo_id).status.value == "Queued"


def test_unsupported_mode_returns_status_false(tmp_path: Path):
    c, _ = _client(tmp_path)
    r = c.get("/sabnzbd/api", params={"mode": "made_up_mode"})
    assert r.status_code == 200
    assert r.json()["status"] is False


def test_queue_degrades_on_store_error_instead_of_500(tmp_path: Path):
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    _corrupt_status(tmp_path, job.nzo_id, "Bogus")
    r = c.get("/sabnzbd/api", params={"mode": "queue"})
    assert r.status_code == 200
    assert r.json()["queue"]["slots"] == []


def test_history_degrades_on_store_error_instead_of_500(tmp_path: Path):
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    _corrupt_status(tmp_path, job.nzo_id, "Bogus")
    r = c.get("/sabnzbd/api", params={"mode": "history"})
    assert r.status_code == 200
    assert r.json()["history"]["slots"] == []


def test_queue_percentage_is_clamped_to_100(tmp_path: Path):
    # svtplay-dl's remux step writes the .ts and the resulting .mkv into
    # staging at once, so downloaded_bytes (bytes-on-disk) routinely
    # exceeds the nzb's declared size_bytes mid-download. Unclamped, this
    # would report over 100% to Sonarr's Activity tab.
    c, store = _client(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 1000)
    store.update_progress(job.nzo_id, 1500)
    slots = c.get("/sabnzbd/api", params={"mode": "queue"}).json()["queue"]["slots"]
    assert slots[0]["percentage"] == "100"


def test_addfile_ignores_negative_size(tmp_path: Path):
    negative_nzb = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><head>'
        '<meta type="svt_id">KZmQ5JY</meta>'
        '<meta type="stem">Show - S15E01 - WEBDL-1080p</meta>'
        '<meta type="size">-1</meta>'
        "</head></nzb>"
    )
    c, store = _client(tmp_path)
    r = c.post(
        "/sabnzbd/api",
        params={"mode": "addfile"},
        files={"name": ("negative.nzb", negative_nzb, "application/x-nzb")},
    )
    nzo_id = r.json()["nzo_ids"][0]
    assert store.get(nzo_id).size_bytes == 0


def test_addfile_catches_unexpected_read_error(tmp_path: Path, monkeypatch):
    # The never-500 constraint is absolute: an error reading the in-flight
    # multipart upload (e.g. a client disconnect mid-upload) is neither
    # malformed input nor a store error, but must still degrade rather than
    # surface as a 500.
    async def boom(self, *args, **kwargs):
        raise RuntimeError("client disconnected")

    monkeypatch.setattr(StarletteUploadFile, "read", boom)
    c, store = _client(tmp_path)
    r = c.post(
        "/sabnzbd/api",
        params={"mode": "addfile"},
        files={"name": ("release.nzb", NZB, "application/x-nzb")},
    )
    assert r.status_code == 200
    assert r.json()["status"] is False
    assert store.all_active() == []


# --- Where the store reads happen -------------------------------------


def _records_the_thread(where: list[str], answer):
    """A stand-in for a sync JobStore method that notes where it ran."""

    def probe(self, *args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            where.append("off the loop")
        else:
            where.append("on the loop")
        return answer

    return probe


@pytest.mark.parametrize(
    "mode,method,answer",
    [
        ("queue", "all_active", []),
        ("history", "history", []),
    ],
)
def test_sonarrs_polls_do_not_read_the_store_on_the_event_loop(
    tmp_path: Path, mode: str, method: str, answer
):
    # Sonarr polls these on a schedule, and this service shares one event
    # loop between its routes and the download worker. A route that read
    # the store inline would run a blocking sqlite read on that loop and
    # stall the download it is reporting on. The store's `*_async` mirrors
    # are what keep the route a coroutine and the read off the loop; this
    # is the only thing that observes the difference.
    #
    # Asked of asyncio rather than of thread identity: the test client
    # already runs the app off the main thread, so "not the main thread"
    # is true either way -- but inside an `asyncio.to_thread` worker there
    # is no running loop at all, and there is nowhere else this could be
    # called from where that is true.
    where: list[str] = []
    client, _store_ = _client(tmp_path)
    original = getattr(JobStore, method)
    setattr(JobStore, method, _records_the_thread(where, answer))
    try:
        resp = client.get(f"/sabnzbd/api?mode={mode}&apikey=x")
    finally:
        setattr(JobStore, method, original)

    assert resp.status_code == 200
    assert where, f"mode={mode} never read the store"
    assert set(where) == {"off the loop"}, where


def test_addfile_does_not_write_to_the_store_on_the_event_loop(tmp_path: Path):
    # The write half of the same rule. `mode=addfile` is the one route
    # Sonarr uses that changes the table, and an INSERT taken on the loop
    # stalls it just as a read does.
    where: list[str] = []
    client, store = _client(tmp_path)
    original = JobStore.create

    def probe(self, *args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            where.append("off the loop")
        else:
            where.append("on the loop")
        return original(self, *args, **kwargs)

    JobStore.create = probe
    try:
        resp = client.post(
            "/sabnzbd/api?mode=addfile",
            files={"name": ("x.nzb", NZB, "application/x-nzb")},
        )
    finally:
        JobStore.create = original

    assert resp.json()["status"] is True
    assert where == ["off the loop"], where
