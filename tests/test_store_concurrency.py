"""The job store under concurrent access, and proof the test has teeth.

The original defect here was found empirically, not by reading: driving one
`sqlite3.Connection` from several threads at once handed readers back rows
with torn columns -- a NULL status, an empty stem, another row's size -- and
raised nothing. 272 corrupted reads were reproduced before the global lock
was added to fix it. The lock worked, and cost the store its concurrency:
every read and every write took turns, on the event loop, including the
worker's progress writes behind a page render's full table scan.

Per-thread connections replace that lock, so the same standard applies: a
hammer test that drives the store from OS threads *and* asyncio tasks at
once, in the shapes the worker and the routes actually produce, asserting
that every row read back is internally coherent -- not merely that nothing
raised.

`test_the_hammer_catches_the_original_defect` is the control. It runs the
identical hammer against the design that was replaced -- one shared
connection, nothing serialising it -- and requires it to fail. Without that,
this file would prove only that the hammer is quiet, which a hammer that
does nothing also is.
"""

import asyncio
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from svtplay_arr import store as store_module
from svtplay_arr.models import Job, JobStatus
from svtplay_arr.store import JobStore

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


# --- What a coherent row looks like -----------------------------------

_SEEDED = 6
_TICKS = 250
_READ_ROUNDS = 150
_TASK_ROUNDS = 50
_READERS = 3


def _expected(index: int) -> dict:
    """Every column of job `index`, derived from its index alone.

    The point of deriving them is that a reader can check a row against
    what that row claims to be, with no shared state and no snapshot to go
    stale -- which is what makes a torn row (one column from job 3, the
    next from job 5) detectable at all.
    """
    return {
        "svt_id": f"svt{index:03d}",
        "stem": f"Show {index:03d} - S01E{index:02d} - WEBDL-1080p",
        "quality": "WEBDL-1080p" if index % 2 == 0 else "WEBDL-720p",
        "size_bytes": 1_000_000 + index,
    }


def _check(job: Job) -> None:
    index = int(job.svt_id.removeprefix("svt"))
    want = _expected(index)
    assert job.stem == want["stem"], f"stem {job.stem!r} on {job.svt_id}"
    assert job.quality == want["quality"], f"quality {job.quality!r} on {job.svt_id}"
    assert job.size_bytes == want["size_bytes"], f"size {job.size_bytes!r}"
    # `status` is checked by the store itself: `_to_job` raises JobStoreError
    # on any literal outside the enum, which is exactly how the original
    # corruption first surfaced.
    assert isinstance(job.status, JobStatus)
    assert job.downloaded_bytes is not None
    assert 0 <= job.downloaded_bytes <= job.size_bytes, (
        f"progress {job.downloaded_bytes} of {job.size_bytes} on {job.svt_id}"
    )
    assert job.nzo_id.startswith("SVTPLAY-"), f"nzo_id {job.nzo_id!r}"
    if job.status is JobStatus.COMPLETED:
        assert job.storage_path, f"completed {job.svt_id} with no storage_path"


def _seed(store, count: int = _SEEDED) -> list[str]:
    ids = []
    for index in range(count):
        want = _expected(index)
        ids.append(
            store.create(
                want["svt_id"], want["stem"], want["quality"], want["size_bytes"]
            ).nzo_id
        )
    return ids


# --- The hammer -------------------------------------------------------


def _writer(store, ids, errors, ready, stop) -> None:
    """The worker's shape: progress ticks across every job."""
    ready.wait(timeout=30)
    try:
        for tick in range(_TICKS):
            for nzo_id in ids:
                store.update_progress(nzo_id, tick)
    except Exception as exc:  # noqa: BLE001 - collected for the assertion
        errors.append(exc)
    finally:
        stop.set()


def _reader(store, ids, errors, ready, stop) -> None:
    """The routes' shape: `mode=queue`, `mode=history`, the Activity view."""
    ready.wait(timeout=30)
    rounds = 0
    # A floor on the rounds, not merely "until the writer stops". Thread
    # start-up is not scheduled: a reader that first ran after the writer
    # had already finished its ticks would report no errors having never
    # overlapped a single write, which is exactly how the negative control
    # below was observed to pass by accident. The floor is what makes the
    # overlap a property of the test rather than of the scheduler.
    while rounds < _READ_ROUNDS or not stop.is_set():
        rounds += 1
        try:
            for nzo_id in ids:
                job = store.get(nzo_id)
                if job is not None:
                    _check(job)
            for source in (store.queued, store.all_active, store.history,
                           store.all_jobs):
                for job in source():
                    _check(job)
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)


def _hammer_threads(store, ids, errors, stop) -> list[threading.Thread]:
    """One writer and `_READERS` readers, released together by a barrier."""
    ready = threading.Barrier(1 + _READERS, timeout=30)
    threads = [
        threading.Thread(target=_writer, args=(store, ids, errors, ready, stop))
    ]
    threads += [
        threading.Thread(target=_reader, args=(store, ids, errors, ready, stop))
        for _ in range(_READERS)
    ]
    return threads


def _hammer_with_threads(store, ids: list[str]) -> list:
    """Run the thread half of the hammer to completion. Returns errors."""
    errors: list = []
    stop = threading.Event()
    threads = _hammer_threads(store, ids, errors, stop)
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a hammer thread never finished"
    return errors


# --- The store as it is now -------------------------------------------


def test_threads_reading_and_writing_at_once_never_see_a_torn_row(tmp_path: Path):
    store = _store(tmp_path)
    ids = _seed(store)

    errors = _hammer_with_threads(store, ids)

    assert errors == [], errors[:5]
    # The writes all landed, in full, despite the readers.
    for nzo_id in ids:
        assert store.get(nzo_id).downloaded_bytes == _TICKS - 1


async def test_tasks_and_threads_hammering_together_never_see_a_torn_row(
    tmp_path: Path,
):
    """The real shape: the worker's thread, the request threadpool, and a
    handful of coroutines on the event loop, all on one store at once.

    The asyncio half goes through the store's own `*_async` mirrors -- the
    seam every route and the worker use -- so this exercises the path
    production actually takes, threadpool hop included, rather than a
    hand-rolled `to_thread` written for the test.
    """
    store = _store(tmp_path)
    ids = _seed(store)
    errors: list = []
    stop = threading.Event()

    threads = _hammer_threads(store, ids, errors, stop)

    async def polling_task():
        # `mode=queue` and `mode=history`, the way sab.py issues them.
        # Floored for the same reason `_reader` is: a task first scheduled
        # after the writer thread had finished would poll a store nothing
        # was writing to and prove nothing.
        rounds = 0
        while rounds < _TASK_ROUNDS or not stop.is_set():
            rounds += 1
            try:
                for job in await store.all_active_async():
                    _check(job)
                for job in await store.history_async():
                    _check(job)
                for nzo_id in ids:
                    job = await store.get_async(nzo_id)
                    if job is not None:
                        _check(job)
            except Exception as exc:  # noqa: BLE001 - collected below
                errors.append(exc)
            await asyncio.sleep(0)

    async def churn_task():
        # `mode=addfile` followed by `mode=history&name=delete`: rows
        # appearing and disappearing under the readers above.
        rounds = 0
        while rounds < _TASK_ROUNDS or not stop.is_set():
            rounds += 1
            try:
                want = _expected(_SEEDED)
                job = await store.create_async(
                    want["svt_id"], want["stem"], want["quality"], want["size_bytes"]
                )
                await store.update_progress_async(job.nzo_id, 1)
                await store.fail_async(job.nzo_id, "removed from queue")
                assert await store.delete_async(job.nzo_id) is True
            except Exception as exc:  # noqa: BLE001 - collected below
                errors.append(exc)
            await asyncio.sleep(0)

    for t in threads:
        t.start()
    tasks = [asyncio.create_task(polling_task()) for _ in range(2)]
    tasks.append(asyncio.create_task(churn_task()))
    await asyncio.gather(*tasks)
    for t in threads:
        t.join(timeout=60)

    assert not any(t.is_alive() for t in threads), "a hammer thread never finished"
    assert errors == [], errors[:5]
    for nzo_id in ids:
        assert (await store.get_async(nzo_id)).downloaded_bytes == _TICKS - 1
    # Everything the churn task created, it also deleted.
    assert {j.nzo_id for j in store.all_jobs()} == set(ids)


# --- The control: the same hammer against the design that was replaced --


class _SharedConnectionStore(JobStore):
    """The store as it was before the lock, and before per-thread
    connections: one `sqlite3.Connection` handed to every thread, with
    nothing serialising access to it. WAL and busy_timeout are set exactly
    as they were -- they are engine-level settings and never had anything to
    say about this bug, which is the point.
    """

    def _checkout(self):
        conn = getattr(self, "_shared", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            self._shared = conn
            # Registered so `close()` still releases it and the suite's
            # unclosed-database gate stays meaningful for this test too.
            self._connections.append(
                store_module._Registered(
                    threading.current_thread(), conn, threading.RLock()
                )
            )
        # A lock of this thread's own, deliberately: the store's real one is
        # per connection, and giving every thread the same one here would
        # serialise access to the shared connection and hide the very defect
        # this class exists to reproduce.
        lock = getattr(self._local, "lock", None)
        if lock is None:
            lock = threading.RLock()
            self._local.lock = lock
        return conn, lock


def test_the_hammer_catches_the_original_defect(tmp_path: Path):
    """The negative control, and the reason to believe the test above.

    Runs the identical hammer against the shared-connection design. It must
    fail -- if it does not, the passing test above proves nothing about the
    replacement, only that the hammer is quiet.

    Measured on this hammer, ten consecutive runs: 81-156 collected errors
    each, of which 17-47 were torn rows caught by `_check` rather than
    database errors -- rows like `stem 'Show 005 - S01E05' on svt001`, one
    column from one job and the next column from another. Never zero. The
    assertion is only that there is at least one, so the test cannot become
    flaky if a future interpreter narrows the window; a run that produces
    none is a signal that the hammer has stopped exercising the race and
    needs rewriting, not a flake to be retried away.
    """
    store = _SharedConnectionStore(tmp_path / "jobs.db")
    _OPEN_STORES.append(store)
    ids = _seed(store)

    errors = _hammer_with_threads(store, ids)

    assert errors, (
        "the shared-connection design produced no corrupted reads -- this "
        "hammer no longer exercises the defect it exists to catch"
    )
    # And they are the original signature: torn columns and unreadable
    # statuses reaching the caller as data, not as a database error.
    assert any(
        isinstance(exc, (AssertionError, TypeError, ValueError)) for exc in errors
    ), [repr(e) for e in errors[:5]]


# --- Why it is safe now: nothing is shared, and nothing waits ----------


def test_every_thread_gets_its_own_connection(tmp_path: Path):
    # The whole defence, stated directly: two threads must never be handed
    # the same connection object. Everything else in this file is a
    # consequence of that.
    store = _store(tmp_path)
    # The connection objects themselves, not their ids: the store closes a
    # finished thread's connection, and a collected object's id is free to
    # be handed to the next one -- a test comparing ids passes whether or
    # not the connections were ever distinct.
    seen: list[sqlite3.Connection] = []
    lock = threading.Lock()
    ready = threading.Barrier(8, timeout=10)

    def grab():
        conn = store._connection()
        assert store._connection() is conn, "a thread must reuse its connection"
        with lock:
            seen.append(conn)
        # Every thread stays alive until all eight have theirs, so none can
        # be reaped and reissued while the others are still opening.
        ready.wait()

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(seen) == 8
    assert len({id(conn) for conn in seen}) == 8, "two threads shared a connection"


def test_a_read_in_flight_does_not_block_a_write(tmp_path: Path):
    # This is what the global lock cost and what removing it buys back: the
    # download worker's progress write no longer queues behind a page
    # render's full table scan. Under the lock this write would not have
    # completed until the read did, and the assertion below would fail.
    store = _store(tmp_path)
    ids = _seed(store, count=1)
    inside_read = threading.Event()
    let_the_read_finish = threading.Event()
    order: list[str] = []

    class _SlowConnection:
        """This thread's real connection, with one very slow query."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, *args):
            inside_read.set()
            let_the_read_finish.wait(timeout=10)
            return self._inner.execute(*args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def slow_reader():
        # Set on this thread's own thread-local, which is the only place
        # this connection is reachable from -- the design under test is
        # what makes the substitution possible.
        store._local.conn = _SlowConnection(store._connection())
        store.all_jobs()
        order.append("read")

    reader = threading.Thread(target=slow_reader)
    reader.start()
    assert inside_read.wait(timeout=10), "the reader never started its query"

    writer = threading.Thread(
        target=lambda: (store.update_progress(ids[0], 7), order.append("write"))
    )
    writer.start()
    writer.join(timeout=5)

    assert not writer.is_alive(), "the write blocked behind the in-flight read"
    let_the_read_finish.set()
    reader.join(timeout=10)

    assert order == ["write", "read"]
    assert store.get(ids[0]).downloaded_bytes == 7


def test_close_waits_for_an_operation_in_flight_on_another_thread(
    tmp_path: Path,
):
    """The segfault this store's per-connection lock exists to prevent.

    `asyncio.to_thread` does not stop the worker thread when the awaiting
    task is cancelled -- it only abandons the future. The app's shutdown
    cancels the download worker's task and then closes the store, so a
    `store.queued()` dispatched by the worker's last poll can still be
    running on a threadpool thread at the moment `close()` reaches its
    connection. Freeing a `sqlite3.Connection` out from under a live query
    does not raise: it segfaults the interpreter. It took roughly one run
    of the full suite in five before `close()` waited.

    Deterministic here rather than left to that race: the reader is parked
    *inside* its query, and `close()` must not return until it is let go.
    """
    store = JobStore(tmp_path / "jobs.db")
    _seed(store, count=1)
    inside_read = threading.Event()
    let_the_read_finish = threading.Event()
    closed = threading.Event()

    class _SlowConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, *args):
            inside_read.set()
            let_the_read_finish.wait(timeout=10)
            return self._inner.execute(*args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def slow_reader():
        store._local.conn = _SlowConnection(store._connection())
        store.all_jobs()

    reader = threading.Thread(target=slow_reader)
    reader.start()
    assert inside_read.wait(timeout=10), "the reader never started its query"

    closer = threading.Thread(target=lambda: (store.close(), closed.set()))
    closer.start()
    # `close()` must still be waiting: the connection it wants is busy.
    assert not closed.wait(timeout=0.5), "close() freed a connection mid-query"

    let_the_read_finish.set()
    reader.join(timeout=10)
    closer.join(timeout=10)

    assert closed.is_set(), "close() never finished"
    assert not reader.is_alive()


def test_close_gives_up_rather_than_closing_under_a_stuck_query(
    tmp_path: Path, monkeypatch
):
    # The other side of the same decision. An unclosed connection costs a
    # ResourceWarning at some later collection; closing one under a live
    # query costs the process. So a connection that is still busy when the
    # wait runs out is left open, loudly.
    from svtplay_arr import store as store_module

    monkeypatch.setattr(store_module, "_CLOSE_TIMEOUT_S", 0.05)
    store = JobStore(tmp_path / "jobs.db")
    _OPEN_STORES.append(store)
    _seed(store, count=1)
    inside_read = threading.Event()
    let_the_read_finish = threading.Event()
    # The connection `close()` is about to give up on. Held here because
    # nothing else will close it -- which is the point of the test, and
    # also why the test has to close it itself rather than leave a
    # ResourceWarning for a later collection to raise.
    left_open: list[sqlite3.Connection] = []

    class _SlowConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, *args):
            inside_read.set()
            let_the_read_finish.wait(timeout=10)
            return self._inner.execute(*args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def slow_reader():
        real = store._connection()
        left_open.append(real)
        store._local.conn = _SlowConnection(real)
        store.all_jobs()

    reader = threading.Thread(target=slow_reader, name="stuck-reader")
    reader.start()
    try:
        assert inside_read.wait(timeout=10), "the reader never started"
        store.close()  # must return rather than block or crash
    finally:
        let_the_read_finish.set()
        reader.join(timeout=10)

    assert not reader.is_alive()
    assert left_open, "the reader never opened a connection"
    left_open[0].execute("SELECT 1")  # still open, as the warning said
    left_open[0].close()


def test_connections_belonging_to_finished_threads_are_released(tmp_path: Path):
    # A thread's thread-local storage is discarded when it dies, so without
    # the store's registry its connection would be collected by the garbage
    # collector rather than closed -- which is the `ResourceWarning:
    # unclosed database` the warning gate exists to catch. Registered, it is
    # merely idle, and a dead thread cannot be mid-query, so it is closed
    # the next time a new thread opens one.
    store = _store(tmp_path)

    for _ in range(10):
        t = threading.Thread(target=store.all_jobs)
        t.start()
        t.join(timeout=10)

    # The creating thread's, plus at most the most recent worker's.
    assert len(store._connections) <= 2, len(store._connections)


def test_a_store_used_from_many_threads_leaves_no_unclosed_database_warning(
    tmp_path: Path,
):
    """The warning gate, extended to the connections `close()` cannot see
    on the closing thread.

    Run in a subprocess for the same reason as its single-connection
    counterpart in test_store.py: the warning arrives through the
    interpreter's *unraisable* hook as the connection is deallocated, so
    neither `recwarn` nor `warnings.catch_warnings` sees it, and a clean
    exit under `-W error` is the only observable form of the property CI
    depends on. On Python 3.12 it proves nothing -- sqlite3 only started
    raising the warning in 3.13 -- and CI runs both.
    """
    script = textwrap.dedent(
        f"""
        import gc, threading
        from pathlib import Path
        from svtplay_arr.store import JobStore

        def run():
            store = JobStore(Path({str(tmp_path / "jobs.db")!r}))
            store.create("a", "stem", "WEBDL-1080p", 100)
            threads = [threading.Thread(target=store.all_jobs) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            store.close()

        run()
        gc.collect()
        """
    )
    proc = subprocess.run(
        [sys.executable, "-W", "error", "-c", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ResourceWarning" not in proc.stderr, proc.stderr


def test_closing_a_store_closes_the_connections_other_threads_opened(
    tmp_path: Path,
):
    store = JobStore(tmp_path / "jobs.db")
    opened: list[sqlite3.Connection] = []
    t = threading.Thread(target=lambda: opened.append(store._connection()))
    t.start()
    t.join(timeout=10)

    store.close()

    assert opened, "the thread never opened a connection"
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_the_async_mirrors_run_off_the_event_loop(tmp_path: Path):
    # The async mirrors are one-line hops onto a worker thread, so they get
    # a threadpool thread's connection rather than the loop thread's. This
    # pins that they are genuinely off the loop: inside `asyncio.to_thread`
    # there is no running loop, and there is nowhere else this could run
    # where that is true.
    store = _store(tmp_path)
    seen: list[str] = []

    async def drive():
        real = store.all_jobs

        def watched():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                seen.append("off the loop")
            else:
                seen.append("on the loop")
            return real()

        store.all_jobs = watched
        try:
            await store.all_jobs_async()
        finally:
            del store.all_jobs

    asyncio.run(drive())

    assert seen == ["off the loop"]


def test_every_store_operation_has_an_async_twin():
    """The rule, enforced rather than remembered.

    Every route in this service is `async def` and stays that way -- they
    do network I/O to Sonarr and SVT regardless -- so any store call from a
    route or from the worker has to hop off the event loop. A partial set
    of mirrors is a rule with exceptions to remember, and the next
    operation added would quietly get none of it.
    """
    import inspect

    operations = [
        name
        for name, value in vars(JobStore).items()
        if not name.startswith("_")
        and not name.endswith("_async")
        and inspect.isfunction(value)
        and name not in ("close",)
    ]
    assert operations, "no operations found -- the sweep would pass vacuously"
    for name in operations:
        twin = getattr(JobStore, f"{name}_async", None)
        assert twin is not None, f"{name}() has no {name}_async() mirror"
        assert inspect.iscoroutinefunction(twin), f"{name}_async is not async def"


def test_the_async_mirrors_answer_exactly_what_their_sync_twins_do(
    tmp_path: Path,
):
    store = _store(tmp_path)

    async def drive():
        job = await store.create_async("svt000", "stem", "WEBDL-1080p", 100)
        assert (await store.get_async(job.nzo_id)).status is JobStatus.QUEUED
        await store.update_progress_async(job.nzo_id, 40)
        assert [j.nzo_id for j in await store.all_active_async()] == [job.nzo_id]
        assert await store.queued_async() == []
        await store.complete_async(job.nzo_id, "/downloads/completed/stem.mkv")
        assert [j.nzo_id for j in await store.history_async()] == [job.nzo_id]
        assert [j.nzo_id for j in await store.all_jobs_async()] == [job.nzo_id]
        await store.fail_async(job.nzo_id, "later")
        assert (await store.get_async(job.nzo_id)).fail_message == "later"
        assert await store.delete_async(job.nzo_id) is True
        assert await store.get_async(job.nzo_id) is None

    asyncio.run(drive())


def test_the_queue_and_the_activity_view_never_disagree_under_load(
    tmp_path: Path,
):
    # Sonarr's queue (`all_active`) and the config page's Activity view
    # (`all_jobs`, partitioned) read the same table by different routes and
    # on different connections. Under a continuous writer they must still
    # describe the same set of jobs -- the coherence property stated at the
    # level an operator would actually notice it.
    store = _store(tmp_path)
    ids = _seed(store, count=4)
    store.complete(ids[0], "/downloads/completed/a.mkv")
    store.fail(ids[1], "nope")
    mismatches: list[tuple] = []
    stop = threading.Event()

    def compare():
        while not stop.is_set():
            active = {j.nzo_id for j in store.all_active()}
            history = {j.nzo_id for j in store.history()}
            every = {j.nzo_id for j in store.all_jobs()}
            if active | history != every or active & history:
                mismatches.append((sorted(active), sorted(history), sorted(every)))

    watcher = threading.Thread(target=compare)
    watcher.start()
    try:
        errors = _hammer_with_threads(store, ids[2:])
    finally:
        stop.set()
        watcher.join(timeout=10)

    assert errors == [], errors[:5]
    assert mismatches == [], mismatches[:2]


def test_the_hammer_is_not_silently_doing_nothing(tmp_path: Path):
    # Every guarantee in this file rests on the hammer actually reaching
    # the store. A refactor that made `_reader` return early would leave
    # every test here green.
    store = _store(tmp_path)
    ids = _seed(store, count=1)
    reads: list[int] = []
    real_get = store.get

    def counted(nzo_id):
        reads.append(1)
        return real_get(nzo_id)

    store.get = counted
    try:
        errors = _hammer_with_threads(store, ids)
    finally:
        del store.get

    assert errors == []
    assert len(reads) > 100, len(reads)


def test_the_seeded_rows_are_distinguishable(tmp_path: Path):
    # `_check` can only catch a torn row if the rows differ in every
    # column it checks. Two jobs with the same size or the same stem would
    # make a swap between them invisible.
    every = [_expected(i) for i in range(_SEEDED)]
    for key in ("svt_id", "stem", "size_bytes"):
        values = [e[key] for e in every]
        assert len(set(values)) == len(values), key
    assert len({e["quality"] for e in every}) > 1


def test_a_slow_reader_does_not_delay_the_worker_measurably(tmp_path: Path):
    # The claim the change is justified by, measured rather than asserted
    # structurally: with a full table scan running continuously on another
    # thread, a progress write still completes promptly. The threshold is
    # deliberately loose -- this is a smoke test against a return to
    # take-turns behaviour, not a benchmark.
    store = _store(tmp_path)
    ids = _seed(store, count=_SEEDED)
    for _ in range(200):
        store.create("svt000", _expected(0)["stem"], "WEBDL-1080p", 1_000_000)
    stop = threading.Event()

    def scan():
        while not stop.is_set():
            store.all_jobs()

    readers = [threading.Thread(target=scan) for _ in range(3)]
    for t in readers:
        t.start()
    try:
        start = time.monotonic()
        for tick in range(50):
            store.update_progress(ids[0], tick)
        elapsed = time.monotonic() - start
    finally:
        stop.set()
        for t in readers:
            t.join(timeout=10)

    assert elapsed < 5.0, f"50 progress writes took {elapsed:.2f}s under read load"
