import re
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
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


def test_wal_mode_and_busy_timeout_are_configured(tmp_path: Path, monkeypatch):
    # WAL is a property of the database file, so it is set once at open and
    # every later connection inherits it -- which is exactly what makes
    # per-thread connections safe. busy_timeout is per-connection and has to
    # be applied to each one, so both are asserted on a connection the store
    # opened for a *different* thread than the one that built it.
    #
    # busy_timeout is asserted against a patched value, and that is the only
    # way this half of the test means anything: `sqlite3.connect()`'s own
    # `timeout` argument defaults to 5.0 seconds -- busy_timeout = 5000,
    # exactly what this module sets. An earlier version of this test
    # asserted 5000 and passed with the pragma deleted outright. 7321 is a
    # number the standard library will never hand back on its own.
    from svtplay_arr import store as store_module

    monkeypatch.setattr(store_module, "_BUSY_TIMEOUT_MS", 7321)
    s = _store(tmp_path)
    settings = {}

    def read():
        conn = s._connection()
        settings["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        settings["busy_timeout"] = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    t = threading.Thread(target=read)
    t.start()
    t.join()

    assert settings == {"journal_mode": "wal", "busy_timeout": 7321}


def test_the_store_works_on_a_database_that_never_reached_wal(
    tmp_path: Path, monkeypatch
):
    # What the WAL warning claims: slower, not wrong. Asserted rather than
    # asserted-in-a-comment, because it is the whole justification for
    # degrading instead of refusing to start.
    from svtplay_arr import store as store_module

    monkeypatch.setattr(store_module, "_enable_wal", lambda conn, db_path: None)
    s = _store(tmp_path)
    assert s._connection().execute("PRAGMA journal_mode").fetchone()[0] != "wal"

    job = s.create("a", "stem", "WEBDL-1080p", 100)
    s.update_progress(job.nzo_id, 50)
    s.complete(job.nzo_id, "/downloads/completed/stem.mkv")
    errors: list = []

    def read():
        try:
            for _ in range(50):
                s.all_jobs()
                s.get(job.nzo_id)
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(3)]
    for t in threads:
        t.start()
    for _ in range(50):
        s.create("b", "other", "WEBDL-1080p", 1)
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert s.get(job.nzo_id).status is JobStatus.COMPLETED
    assert len(s.history()) == 1


def test_a_closed_store_stays_closed_for_a_thread_that_never_used_it(
    tmp_path: Path,
):
    # `close()` sets the flag before it drains the registry, and without
    # that a thread checking out afterwards gets a brand new connection
    # registered into a list nothing will ever drain again -- a store that
    # silently works after being closed, and leaks. Asked of a thread that
    # has never touched this store, because a thread that has one cached in
    # its thread-local would be answered from there either way.
    s = JobStore(tmp_path / "jobs.db")
    s.create("a", "stem", "WEBDL-1080p", 1)
    s.close()
    outcome: list = []

    def use_it():
        try:
            outcome.append(s.all_jobs())
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            outcome.append(exc)

    t = threading.Thread(target=use_it)
    t.start()
    t.join(timeout=10)

    assert len(outcome) == 1
    assert isinstance(outcome[0], JobStoreError), outcome
    assert s._connections == [], "a closed store registered a new connection"


def _rollback_journal_database(tmp_path: Path) -> Path:
    """A database in sqlite's default journal mode, never opened by the store.

    Opening it through `JobStore` would put it into WAL, and a database
    already in WAL answers the pragma with "wal" whatever else is true of
    it -- so a test built on one would take the early return and prove
    nothing.
    """
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE jobs (nzo_id TEXT PRIMARY KEY)")
        conn.commit()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
    finally:
        conn.close()
    return db


def test_a_database_that_will_not_go_into_wal_is_used_anyway(monkeypatch):
    # WAL is wanted, not required. Every release before this one issued the
    # same pragma and ignored the result, so there are db_paths in the world
    # where it does not take and the service has always worked -- an NFS or
    # CIFS mount, where sqlite falls back to a VFS with no shared memory.
    # docs/configuration.md invites moving db_path there. Refusing to start
    # on one of those would be a regression dressed as a safety check:
    # without WAL this store is slower, not wrong.
    from svtplay_arr import store as store_module

    monkeypatch.setattr(store_module, "_WAL_TIMEOUT_S", 0.0)
    # An in-memory database reports "memory" and cannot be moved to WAL --
    # the cheapest honest stand-in for a mode the pragma will not change.
    conn = sqlite3.connect(":memory:")
    try:
        store_module._enable_wal(conn, Path("/srv/nfs/jobs.db"))  # must not raise
    finally:
        conn.close()


def test_the_wal_warning_names_what_sqlite_actually_said(tmp_path: Path, caplog):
    # The first version of this said "something else is holding it open" for
    # every failure, having discarded the OperationalError. On the shape it
    # was most likely to meet, the real answer was "attempt to write a
    # readonly database" -- so the operator was sent to hunt a second
    # process that did not exist, once every five seconds, on a
    # Restart=on-failure loop.
    from svtplay_arr import store as store_module

    db = _rollback_journal_database(tmp_path)
    # Read-only, which is what an NFS mount answers with when the pragma
    # tries to take the exclusive lock the conversion needs.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with caplog.at_level("WARNING", logger="svtplay_arr.store"):
            store_module._enable_wal(conn, db)
    finally:
        conn.close()

    assert len(caplog.records) == 1, [r.getMessage() for r in caplog.records]
    message = caplog.records[0].getMessage()
    assert "readonly database" in message, message
    assert str(db) in message
    assert "NFS" in message, "the message must point at the usual cause"


def test_a_wal_failure_that_is_not_contention_is_not_waited_out(tmp_path: Path):
    # Retrying every failure is what put a five second delay in front of a
    # start that was never going to succeed. Only "someone else has the
    # file" is worth waiting out.
    from svtplay_arr import store as store_module

    db = _rollback_journal_database(tmp_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    started = time.monotonic()
    try:
        store_module._enable_wal(conn, db)
    finally:
        conn.close()

    assert time.monotonic() - started < store_module._WAL_TIMEOUT_S / 2


def test_concurrent_writer_and_readers_never_see_corrupted_rows(tmp_path: Path):
    # Regression test for a real, reproduced bug: driving one shared
    # sqlite3.Connection from multiple OS threads at once -- one thread
    # calling update_progress() in a loop while several others call
    # get()/queued()/all_active()/history() -- used to hand readers back
    # rows with a NULL or empty status, which _to_job() (correctly) raises
    # on as corruption. That is not disk corruption: it is a race in the
    # Python sqlite3 module's per-connection bookkeeping when execute() and
    # commit() from different threads interleave. WAL mode and busy_timeout
    # do not prevent it -- not sharing the connection does, which is why
    # the store now opens one per thread. This reliably reproduced 9-40
    # errors per run before that; it must produce zero now.
    #
    # Kept as it was written, deliberately: this is the shape the original
    # defect was found in. tests/test_store_concurrency.py is the wider
    # version, and holds the negative control proving the shape still
    # catches it.
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


# --- What the Activity view reads -------------------------------------


def test_all_jobs_returns_every_row_whatever_its_status(tmp_path: Path):
    # The config page's Activity view needs the queue and the history in
    # the same render. Asking for them separately would mean two full
    # table reads per page view, each taking the connection lock the
    # download worker also writes through; this is the one read they are
    # both partitioned out of.
    s = _store(tmp_path)
    queued = s.create("a", "stem-a", "WEBDL-1080p", 1)
    done = s.create("b", "stem-b", "WEBDL-1080p", 2)
    failed = s.create("c", "stem-c", "WEBDL-1080p", 3)
    s.complete(done.nzo_id, "/tmp/stem-b.mkv")
    s.fail(failed.nzo_id, "svtplay-dl exited 1")

    got = {j.nzo_id: j.status for j in s.all_jobs()}

    assert got == {
        queued.nzo_id: JobStatus.QUEUED,
        done.nzo_id: JobStatus.COMPLETED,
        failed.nzo_id: JobStatus.FAILED,
    }


def test_all_jobs_agrees_with_the_endpoint_specific_listings(tmp_path: Path):
    # Partitioning one read must produce exactly what the two existing
    # listings produce, or the Activity view and Sonarr's queue would
    # disagree about the same store.
    s = _store(tmp_path)
    s.create("a", "stem-a", "WEBDL-1080p", 1)
    done = s.create("b", "stem-b", "WEBDL-1080p", 2)
    s.complete(done.nzo_id, "/tmp/stem-b.mkv")

    every = s.all_jobs()
    active = [j for j in every if j.status in (JobStatus.QUEUED, JobStatus.DOWNLOADING)]
    history = [j for j in every if j.status in (JobStatus.COMPLETED, JobStatus.FAILED)]

    assert active == s.all_active()
    assert history == s.history()


def test_all_jobs_raises_rather_than_hiding_a_read_failure(tmp_path: Path):
    # The whole point of the Activity view's error handling is that "no
    # failures" and "cannot read failures" are different answers. That
    # distinction can only be made one level up if this raises rather than
    # returning an empty list.
    s = _store(tmp_path)
    s.close()

    with pytest.raises(JobStoreError):
        s.all_jobs()


def test_a_job_records_when_it_was_created(tmp_path: Path):
    # "Why didn't that episode arrive?" is not answerable by a list of
    # stems with no times against them. The column has always existed;
    # nothing read it.
    s = _store(tmp_path)
    job = s.create("a", "stem-a", "WEBDL-1080p", 1)

    assert job.created_at
    # sqlite's datetime('now'), i.e. UTC, second resolution.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", job.created_at)
