import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from svtplay_arr.models import JobStatus
from svtplay_arr.store import JobStore, JobStoreError


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


def _corrupt_status(tmp_path: Path, nzo_id: str, status: str) -> None:
    """Simulate a hand-edited/out-of-band row: write a status literal that
    isn't one of JobStatus's values, bypassing JobStore entirely."""
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("UPDATE jobs SET status = ? WHERE nzo_id = ?", (status, nzo_id))
    conn.commit()
    conn.close()


def test_create_and_get(tmp_path: Path):
    s = _store(tmp_path)
    job = s.create(svt_id="KZmQ5JY", stem="Show - S15E01 - WEBDL-1080p",
                   quality="WEBDL-1080p", size_bytes=1_400_000_000)
    assert s.get(job.nzo_id).status is JobStatus.QUEUED


def test_progress_updates(tmp_path: Path):
    s = _store(tmp_path)
    job = s.create("a", "stem", "WEBDL-1080p", 100)
    s.update_progress(job.nzo_id, downloaded_bytes=50)
    assert s.get(job.nzo_id).downloaded_bytes == 50
    assert s.get(job.nzo_id).status is JobStatus.DOWNLOADING


def test_complete_records_storage_path(tmp_path: Path):
    s = _store(tmp_path)
    job = s.create("a", "stem", "WEBDL-1080p", 100)
    s.complete(job.nzo_id, "/downloads/completed/svtplay/stem.mkv")
    got = s.get(job.nzo_id)
    assert got.status is JobStatus.COMPLETED
    assert got.storage_path.endswith("stem.mkv")
    assert got in s.history()


def test_fail_records_message(tmp_path: Path):
    s = _store(tmp_path)
    job = s.create("a", "stem", "WEBDL-1080p", 100)
    s.fail(job.nzo_id, "geo-blocked")
    assert s.get(job.nzo_id).fail_message == "geo-blocked"


def test_update_progress_does_not_resurrect_a_failed_job(tmp_path: Path):
    # A downloader that has no cancellation hook keeps reporting progress
    # for as long as it runs, including one unconditional final call right
    # before it returns, with no way to know the job was independently
    # terminated (e.g. by mode=delete) while it was in flight. Without this
    # guard, that next progress tick silently overwrites a Failed row back
    # to Downloading -- see worker.py's post-download re-check, which this
    # protects.
    s = _store(tmp_path)
    job = s.create("a", "stem", "WEBDL-1080p", 100)
    s.fail(job.nzo_id, "removed from queue")
    s.update_progress(job.nzo_id, 50)
    got = s.get(job.nzo_id)
    assert got.status is JobStatus.FAILED
    assert got.fail_message == "removed from queue"


def test_update_progress_does_not_resurrect_a_completed_job(tmp_path: Path):
    s = _store(tmp_path)
    job = s.create("a", "stem", "WEBDL-1080p", 100)
    s.complete(job.nzo_id, "/downloads/completed/stem.mkv")
    s.update_progress(job.nzo_id, 50)
    got = s.get(job.nzo_id)
    assert got.status is JobStatus.COMPLETED
    assert got.storage_path == "/downloads/completed/stem.mkv"


def test_update_progress_still_moves_a_queued_or_downloading_job(tmp_path: Path):
    # The guard must not break the ordinary path: Queued -> Downloading, and
    # further progress ticks while already Downloading, must both still work
    # exactly as before.
    s = _store(tmp_path)
    job = s.create("a", "stem", "WEBDL-1080p", 100)
    s.update_progress(job.nzo_id, 30)
    got = s.get(job.nzo_id)
    assert got.status is JobStatus.DOWNLOADING
    assert got.downloaded_bytes == 30
    s.update_progress(job.nzo_id, 70)
    got = s.get(job.nzo_id)
    assert got.status is JobStatus.DOWNLOADING
    assert got.downloaded_bytes == 70


def test_active_excludes_finished(tmp_path: Path):
    s = _store(tmp_path)
    a = s.create("a", "s1", "WEBDL-1080p", 1)
    b = s.create("b", "s2", "WEBDL-1080p", 1)
    s.complete(b.nzo_id, "/x")
    assert [j.nzo_id for j in s.all_active()] == [a.nzo_id]


def test_get_raises_job_store_error_on_corrupted_status(tmp_path: Path):
    s = _store(tmp_path)
    job = s.create("a", "stem", "WEBDL-1080p", 100)
    _corrupt_status(tmp_path, job.nzo_id, "Bogus")
    with pytest.raises(JobStoreError):
        s.get(job.nzo_id)


def test_listing_methods_raise_rather_than_silently_drop_corrupted_job(
    tmp_path: Path,
):
    # A row with an unrecognised status literal would never match any
    # listing method's status filter, so before this was fixed it simply
    # vanished from queued()/all_active()/history() instead of erroring --
    # meaning a corrupted job would silently disappear from Sonarr's queue.
    # Assert the deliberate choice: surface the corruption loudly instead.
    s = _store(tmp_path)
    job = s.create("a", "stem", "WEBDL-1080p", 100)
    _corrupt_status(tmp_path, job.nzo_id, "Bogus")
    with pytest.raises(JobStoreError):
        s.queued()
    with pytest.raises(JobStoreError):
        s.all_active()
    with pytest.raises(JobStoreError):
        s.history()


def test_wal_mode_and_busy_timeout_are_configured(tmp_path: Path):
    s = _store(tmp_path)
    assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert s._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_concurrent_writer_and_readers_never_see_corrupted_rows(tmp_path: Path):
    # Regression test for a real, reproduced bug: driving the shared
    # sqlite3.Connection from multiple OS threads at once -- one thread
    # calling update_progress() in a loop while several others call
    # get()/queued()/all_active()/history() -- used to hand readers back
    # rows with a NULL or empty status, which _to_job() (correctly) raises
    # on as corruption. That is not real corruption: it is a race in the
    # Python sqlite3 module's per-connection bookkeeping when execute() and
    # commit() from different threads interleave without external
    # synchronisation. WAL mode and busy_timeout do not prevent it -- only
    # serialising access with a lock does. This reliably reproduced 9-40
    # errors per run before JobStore serialised access internally; it must
    # produce zero now.
    s = _store(tmp_path)
    job = s.create("a", "stem", "WEBDL-1080p", 100_000)
    nzo_id = job.nzo_id

    errors = []
    stop = threading.Event()

    def writer():
        for i in range(300):
            try:
                s.update_progress(nzo_id, i)
            except Exception as exc:  # noqa: BLE001 - collecting for assertion
                errors.append(exc)
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                s.get(nzo_id)
                s.queued()
                s.all_active()
                s.history()
            except Exception as exc:  # noqa: BLE001 - collecting for assertion
                errors.append(exc)

    threads = [threading.Thread(target=writer)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert s.get(nzo_id).downloaded_bytes == 299


def test_delete_removes_the_row_entirely(tmp_path: Path):
    # SABnzbd's history delete removes the entry; it does not mark it failed.
    # Without a real deletion, history could only ever grow.
    store = _store(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    store.complete(job.nzo_id, "/downloads/completed/stem.mkv")

    assert store.delete(job.nzo_id) is True
    assert store.get(job.nzo_id) is None
    assert store.history() == []


def test_delete_of_an_unknown_id_reports_no_row_removed(tmp_path: Path):
    store = _store(tmp_path)
    assert store.delete("SVTPLAY-nosuchjob") is False


# --- Closing ---------------------------------------------------------------


def test_close_releases_the_connection(tmp_path: Path):
    s = _store(tmp_path)
    s.create("a", "stem", "WEBDL-1080p", 100)
    s.close()
    # Not a raw sqlite3.ProgrammingError escaping to a caller: every other
    # failure in this class arrives as JobStoreError, and the SAB routes
    # degrade on that one type. A different exception here would reach
    # Sonarr as a 500.
    with pytest.raises(JobStoreError):
        s.all_active()


def test_close_is_idempotent(tmp_path: Path):
    # Both the app's shutdown and a test fixture may close the same store.
    s = _store(tmp_path)
    s.close()
    s.close()


def test_store_is_usable_as_a_context_manager(tmp_path: Path):
    with JobStore(tmp_path / "jobs.db") as s:
        job = s.create("a", "stem", "WEBDL-1080p", 100)
        assert s.get(job.nzo_id) is not None
    with pytest.raises(JobStoreError):
        s.get(job.nzo_id)


def test_a_closed_store_leaves_no_unclosed_database_warning(tmp_path: Path):
    """The defect this whole change exists for: 33 `ResourceWarning:
    unclosed database` across the suite, which is what stopped CI enforcing
    zero warnings.

    Run in a subprocess deliberately. The warning is raised as the
    connection is deallocated, so it arrives through the interpreter's
    *unraisable* hook rather than the normal warnings path -- neither
    `recwarn` nor `warnings.catch_warnings` sees it, and a test written with
    either passes whether or not close() does anything at all (this one did,
    until it was checked). A clean exit under `-W error` is the property CI
    actually depends on, and it is only observable from outside the process.

    On Python 3.12 this proves nothing: sqlite3 only started raising the
    warning in 3.13, so the negative control passes there too. CI runs both
    versions and 3.13 is the one that can catch this.
    """
    script = textwrap.dedent(
        f"""
        import gc
        from pathlib import Path
        from svtplay_arr.store import JobStore

        def run():
            store = JobStore(Path({str(tmp_path / "jobs.db")!r}))
            store.create("a", "stem", "WEBDL-1080p", 100)
            store.close()

        run()          # the store goes out of scope here...
        gc.collect()   # ...and this is where an unclosed one would complain
        """
    )
    proc = subprocess.run(
        [sys.executable, "-W", "error", "-c", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ResourceWarning" not in proc.stderr, proc.stderr
