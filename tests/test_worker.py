import asyncio
from pathlib import Path
import pytest
from svtplay_arr.downloader import FakeDownloader
from svtplay_arr.models import JobStatus
from svtplay_arr.store import JobStore, JobStoreError
from svtplay_arr.worker import Worker


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



def _setup(tmp_path: Path):
    inc = tmp_path / "incomplete"
    comp = tmp_path / "completed"
    inc.mkdir()
    comp.mkdir()
    store = _store(tmp_path)
    return store, Worker(store, FakeDownloader(steps=2, total_bytes=100), inc, comp)


async def test_completed_file_lands_in_completed_dir(tmp_path: Path):
    store, worker = _setup(tmp_path)
    job = store.create("KZmQ5JY", "Show - S15E01 - WEBDL-1080p", "WEBDL-1080p", 100)
    await worker.run_job(job.nzo_id)
    out = tmp_path / "completed" / "Show - S15E01 - WEBDL-1080p.mkv"
    assert out.exists()
    assert store.get(job.nzo_id).status is JobStatus.COMPLETED
    assert store.get(job.nzo_id).storage_path == str(out)


async def test_incomplete_dir_is_emptied_after_publish(tmp_path: Path):
    store, worker = _setup(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 100)
    await worker.run_job(job.nzo_id)
    assert list((tmp_path / "incomplete").iterdir()) == []


async def test_progress_is_recorded_during_download(tmp_path: Path):
    store, worker = _setup(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 100)
    await worker.run_job(job.nzo_id)
    assert store.get(job.nzo_id).downloaded_bytes == 100


async def test_download_failure_marks_job_failed(tmp_path: Path):
    class Boom:
        async def download(self, *a, **k):
            raise RuntimeError("geo-blocked")

    inc, comp = tmp_path / "i", tmp_path / "c"
    inc.mkdir(); comp.mkdir()
    store = _store(tmp_path)
    worker = Worker(store, Boom(), inc, comp)
    job = store.create("a", "stem", "WEBDL-1080p", 100)
    await worker.run_job(job.nzo_id)
    got = store.get(job.nzo_id)
    assert got.status is JobStatus.FAILED
    assert "geo-blocked" in got.fail_message
    assert list(comp.iterdir()) == [], "no partial file may reach completed/"


async def test_sweep_removes_stale_incomplete(tmp_path: Path):
    store, worker = _setup(tmp_path)
    stale = tmp_path / "incomplete" / "leftover"
    stale.mkdir()
    (stale / "partial.ts").write_bytes(b"x")
    worker.sweep_incomplete()
    assert list((tmp_path / "incomplete").iterdir()) == []


async def test_sidecar_subtitles_are_carried_across(tmp_path: Path):
    class WithSubs:
        async def download(self, svt_id, dest_dir, stem, on_progress):
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / f"{stem}.mkv").write_bytes(b"\0")
            (dest_dir / f"{stem}.sv.srt").write_text("1\n", encoding="utf-8")
            on_progress(1, 1)
            return dest_dir / f"{stem}.mkv"

    inc, comp = tmp_path / "i", tmp_path / "c"
    inc.mkdir(); comp.mkdir()
    store = _store(tmp_path)
    worker = Worker(store, WithSubs(), inc, comp)
    job = store.create("a", "Show - S15E01 - WEBDL-1080p", "WEBDL-1080p", 1)
    await worker.run_job(job.nzo_id)
    assert (comp / "Show - S15E01 - WEBDL-1080p.sv.srt").exists()


async def test_published_file_has_mode_664(tmp_path: Path):
    store, worker = _setup(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 100)
    await worker.run_job(job.nzo_id)
    out = tmp_path / "completed" / "stem.mkv"
    assert (out.stat().st_mode & 0o777) == 0o664


async def test_completed_dir_has_mode_775(tmp_path: Path):
    store, worker = _setup(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 100)
    await worker.run_job(job.nzo_id)
    comp = tmp_path / "completed"
    assert (comp.stat().st_mode & 0o777) == 0o775


async def test_no_file_visible_in_completed_before_download_finishes(tmp_path: Path):
    """The video must not be importable while it is still partial: publish
    only happens after the downloader returns, and only via atomic rename."""
    seen_during_download = []
    inc, comp = tmp_path / "i", tmp_path / "c"

    class SlowDownloader:
        async def download(self, svt_id, dest_dir, stem, on_progress):
            dest_dir.mkdir(parents=True, exist_ok=True)
            partial = dest_dir / f"{stem}.mkv"
            partial.write_bytes(b"\0" * 5)
            on_progress(5, 100)
            seen_during_download.append(list(comp.iterdir()))
            partial.write_bytes(b"\0" * 100)
            on_progress(100, 100)
            return partial

    inc.mkdir(); comp.mkdir()
    store = _store(tmp_path)
    worker = Worker(store, SlowDownloader(), inc, comp)
    job = store.create("a", "stem", "WEBDL-1080p", 100)
    await worker.run_job(job.nzo_id)
    assert seen_during_download == [[]], "completed/ must be empty while download is in flight"
    assert (comp / "stem.mkv").exists()


async def test_run_forever_dispatches_each_job_at_most_once_while_queued(tmp_path: Path):
    """A job whose download setup takes longer than one poll tick must not be
    re-dispatched on every subsequent tick: that would redo the entire
    download (and re-publish, re-complete) once per tick until it finally
    finishes."""

    class SlowStartDownloader:
        def __init__(self):
            self.call_count = 0

        async def download(self, svt_id, dest_dir, stem, on_progress):
            self.call_count += 1
            dest_dir.mkdir(parents=True, exist_ok=True)
            # Stays QUEUED (no progress reported) across several poll ticks.
            await asyncio.sleep(0.08)
            (dest_dir / f"{stem}.mkv").write_bytes(b"\0")
            on_progress(1, 1)
            return dest_dir / f"{stem}.mkv"

    inc, comp = tmp_path / "i", tmp_path / "c"
    inc.mkdir(); comp.mkdir()
    store = _store(tmp_path)
    downloader = SlowStartDownloader()
    worker = Worker(store, downloader, inc, comp)
    job = store.create("a", "stem", "WEBDL-1080p", 1)

    runner = asyncio.create_task(worker.run_forever(poll_seconds=0.01))
    await asyncio.sleep(0.2)
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass

    assert downloader.call_count == 1
    assert store.get(job.nzo_id).status is JobStatus.COMPLETED


async def test_concurrency_one_serializes_downloads(tmp_path: Path):
    """The semaphore must actually prevent two downloads from running at the
    same time, not merely exist."""
    active = 0
    max_active = 0

    class TrackingDownloader:
        async def download(self, svt_id, dest_dir, stem, on_progress):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            dest_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.sleep(0.02)
            (dest_dir / f"{stem}.mkv").write_bytes(b"\0")
            on_progress(1, 1)
            active -= 1
            return dest_dir / f"{stem}.mkv"

    inc, comp = tmp_path / "i", tmp_path / "c"
    inc.mkdir(); comp.mkdir()
    store = _store(tmp_path)
    worker = Worker(store, TrackingDownloader(), inc, comp, concurrency=1)
    job1 = store.create("a", "stem1", "WEBDL-1080p", 1)
    job2 = store.create("b", "stem2", "WEBDL-1080p", 1)

    await asyncio.gather(worker.run_job(job1.nzo_id), worker.run_job(job2.nzo_id))

    assert max_active == 1
    assert store.get(job1.nzo_id).status is JobStatus.COMPLETED
    assert store.get(job2.nzo_id).status is JobStatus.COMPLETED


async def test_progress_write_failure_does_not_fail_healthy_download(tmp_path: Path):
    class FlakyProgressStore:
        """Wraps a real JobStore; update_progress always raises, everything
        else is delegated -- models a transient bookkeeping failure that
        must not be allowed to abort an otherwise healthy download."""

        def __init__(self, inner: JobStore):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def update_progress(self, nzo_id, downloaded_bytes):
            raise JobStoreError("simulated transient failure")

    inc, comp = tmp_path / "i", tmp_path / "c"
    inc.mkdir(); comp.mkdir()
    real_store = _store(tmp_path)
    store = FlakyProgressStore(real_store)
    worker = Worker(store, FakeDownloader(steps=2, total_bytes=100), inc, comp)
    job = real_store.create("a", "stem", "WEBDL-1080p", 100)

    await worker.run_job(job.nzo_id)

    got = real_store.get(job.nzo_id)
    assert got.status is JobStatus.COMPLETED
    assert (comp / "stem.mkv").exists()


async def test_publish_failure_removes_already_moved_sidecars(tmp_path: Path):
    class WithSubs:
        async def download(self, svt_id, dest_dir, stem, on_progress):
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / f"{stem}.mkv").write_bytes(b"\0")
            (dest_dir / f"{stem}.sv.srt").write_text("1\n", encoding="utf-8")
            on_progress(1, 1)
            return dest_dir / f"{stem}.mkv"

    inc, comp = tmp_path / "i", tmp_path / "c"
    inc.mkdir(); comp.mkdir()
    store = _store(tmp_path)
    worker = Worker(store, WithSubs(), inc, comp)
    job = store.create("a", "stem", "WEBDL-1080p", 1)
    # Force the video's rename to fail by occupying its destination with a
    # directory: os.rename refuses to rename a file onto an existing dir.
    (comp / "stem.mkv").mkdir()

    await worker.run_job(job.nzo_id)

    assert store.get(job.nzo_id).status is JobStatus.FAILED
    assert list(comp.iterdir()) == [comp / "stem.mkv"], (
        "the sidecar moved before the failed video rename must not be left "
        "behind as an orphan"
    )


async def test_deleted_mid_download_is_not_published(tmp_path: Path):
    """Simulates Sonarr calling mode=delete while a download is in flight,
    modeling the *real* SvtplayDlDownloader's actual progress call pattern
    (see downloader.py): repeated mid-download on_progress ticks from its
    poller, AND -- critically -- one more unconditional
    on_progress(size, size) call immediately before it returns
    (downloader.py:124), which still fires *after* the delete lands and
    *before* run_job's post-download status re-check ever runs.

    A fake that calls store.fail() and then returns immediately, with no
    further progress call, does not exercise this: it would pass even with
    the resurrection bug fully present, because nothing after the fail()
    call would touch the store again. This is deliberately built to
    reproduce that: the real defect was update_progress() overwriting a
    Failed row back to Downloading on that final unconditional call, so
    worker.py's re-check saw a job that still looked like it was running
    and published it anyway."""
    context: dict = {}

    class RealisticDownloader:
        """Mirrors SvtplayDlDownloader's shape: poll-tick progress calls
        while the download runs, a delete landing mid-flight, more poll
        ticks after the delete (the poller doesn't know or care that the
        job was deleted), and the mandatory final on_progress(size, size)
        right before returning."""

        async def download(self, svt_id, dest_dir, stem, on_progress):
            dest_dir.mkdir(parents=True, exist_ok=True)
            video = dest_dir / f"{stem}.mkv"
            video.write_bytes(b"\0" * 4)
            on_progress(4, 0)  # first poll tick, mid-download
            # mode=delete lands on this job while its download keeps
            # running -- the downloader has no cancellation hook.
            context["store"].fail(context["nzo_id"], "removed from queue")
            video.write_bytes(b"\0" * 8)
            on_progress(8, 0)  # another poll tick, after the delete
            video.write_bytes(b"\0" * 10)
            size = video.stat().st_size
            on_progress(size, size)  # downloader.py:124's mandatory final call
            return video

    inc, comp = tmp_path / "i", tmp_path / "c"
    inc.mkdir(); comp.mkdir()
    store = _store(tmp_path)
    worker = Worker(store, RealisticDownloader(), inc, comp)
    job = store.create("a", "stem", "WEBDL-1080p", 10)
    context["store"] = store
    context["nzo_id"] = job.nzo_id

    await worker.run_job(job.nzo_id)

    got = store.get(job.nzo_id)
    assert got.status is JobStatus.FAILED
    assert got.fail_message == "removed from queue"
    assert got.storage_path is None
    assert list(comp.iterdir()) == [], "deleted job must not be published"
    assert list(inc.iterdir()) == [], "staging must still be cleaned up"


async def test_non_sidecar_files_in_staging_are_not_published(tmp_path: Path):
    class WithJunk:
        async def download(self, svt_id, dest_dir, stem, on_progress):
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / f"{stem}.mkv").write_bytes(b"\0")
            (dest_dir / "unrelated.tmp").write_bytes(b"junk")
            on_progress(1, 1)
            return dest_dir / f"{stem}.mkv"

    inc, comp = tmp_path / "i", tmp_path / "c"
    inc.mkdir(); comp.mkdir()
    store = _store(tmp_path)
    worker = Worker(store, WithJunk(), inc, comp)
    job = store.create("a", "stem", "WEBDL-1080p", 1)

    await worker.run_job(job.nzo_id)

    assert store.get(job.nzo_id).status is JobStatus.COMPLETED
    assert sorted(p.name for p in comp.iterdir()) == ["stem.mkv"]


# --- restart reconciliation ------------------------------------------------


async def test_sweep_fails_jobs_left_downloading_by_a_restart(tmp_path: Path):
    """A job that was mid-download when the process died stayed Downloading
    forever: sweep_incomplete() deleted its partial file but never touched
    the store, and run_forever only ever re-dispatches Queued jobs. Sonarr's
    Activity tab showed it stuck at its last percentage indefinitely, with no
    error, and the episode was never retried.

    Marking it Failed is what makes Sonarr's autoRedownloadFailed re-search
    it, which is the only recovery available -- the original in-flight
    download cannot be resumed.
    """
    store, worker = _setup(tmp_path)
    interrupted = store.create("a", "stem-a", "WEBDL-1080p", 100)
    store.update_progress(interrupted.nzo_id, 40)
    assert store.get(interrupted.nzo_id).status is JobStatus.DOWNLOADING

    worker.sweep_incomplete()

    got = store.get(interrupted.nzo_id)
    assert got.status is JobStatus.FAILED
    assert "interrupted" in got.fail_message
    # It must show up in history (where Sonarr reads fail_message) and be
    # gone from the queue.
    assert [j.nzo_id for j in store.history()] == [interrupted.nzo_id]
    assert store.all_active() == []


async def test_sweep_leaves_queued_and_finished_jobs_alone(tmp_path: Path):
    store, worker = _setup(tmp_path)
    queued = store.create("a", "stem-a", "WEBDL-1080p", 100)
    done = store.create("b", "stem-b", "WEBDL-1080p", 100)
    store.complete(done.nzo_id, "/downloads/completed/stem-b.mkv")

    worker.sweep_incomplete()

    assert store.get(queued.nzo_id).status is JobStatus.QUEUED
    assert store.get(done.nzo_id).status is JobStatus.COMPLETED
    assert store.get(done.nzo_id).storage_path == "/downloads/completed/stem-b.mkv"


async def test_reconciled_job_is_redispatched_only_after_a_fresh_grab(tmp_path: Path):
    """The reconciled job must be genuinely terminal, not merely relabelled:
    update_progress() refuses to move a terminal row (see store.py), so
    nothing can quietly resurrect it, and run_forever must not pick it up."""
    store, worker = _setup(tmp_path)
    interrupted = store.create("a", "stem-a", "WEBDL-1080p", 100)
    store.update_progress(interrupted.nzo_id, 40)

    worker.sweep_incomplete()
    store.update_progress(interrupted.nzo_id, 90)  # a late tick from nowhere

    assert store.get(interrupted.nzo_id).status is JobStatus.FAILED
    assert store.queued() == []


async def test_run_forever_survives_a_store_error(tmp_path: Path):
    """queued() raises JobStoreError if any row carries an unrecognised
    status. Unguarded, that killed the worker task permanently -- and a
    restart did not help, because the offending row is still there. One bad
    row must not stop every future download."""
    inc, comp = tmp_path / "i", tmp_path / "c"
    inc.mkdir(); comp.mkdir()
    real_store = _store(tmp_path)

    class FlakyStore:
        """Raises from queued() for the first few polls, then recovers.

        `queued_async` is spelled out rather than left to `__getattr__`:
        the worker polls through the store's async mirror, and delegating
        it to the inner store would run the *real* `queued()` and this
        double would never fail at all.
        """

        def __init__(self, inner: JobStore, failures: int):
            self._inner = inner
            self.remaining = failures

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def queued(self):
            if self.remaining > 0:
                self.remaining -= 1
                raise JobStoreError("row has an unrecognised status")
            return self._inner.queued()

        async def queued_async(self):
            return self.queued()

    store = FlakyStore(real_store, failures=3)
    worker = Worker(store, FakeDownloader(steps=2, total_bytes=100), inc, comp)
    job = real_store.create("a", "stem", "WEBDL-1080p", 100)

    runner = asyncio.create_task(worker.run_forever(poll_seconds=0.01))
    deadline = asyncio.get_running_loop().time() + 2.0
    while (
        real_store.get(job.nzo_id).status is not JobStatus.COMPLETED
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.01)
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass

    assert store.remaining == 0, "the loop must have kept polling after the errors"
    assert real_store.get(job.nzo_id).status is JobStatus.COMPLETED


# --- Where the worker's own store reads happen ------------------------


def _where_it_ran(where: list[str], inner):
    def probe(self, *args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            where.append("off the loop")
        else:
            where.append("on the loop")
        return inner(self, *args, **kwargs)

    return probe


async def test_every_store_call_the_worker_makes_is_where_it_says_it_is(
    tmp_path: Path,
):
    """The rule, swept rather than sampled.

    An earlier version of this named `get` and `complete` explicitly, so a
    new store call joining `_report_progress` on the event loop would have
    gone unnoticed -- which is exactly the thing the sweep is for. Every
    public store operation is watched, and each one must land where this
    module says it does: off the loop, except the one documented exception.

    `update_progress` is that exception and is asserted *on* the loop
    deliberately. It is the downloader's synchronous `ProgressFn` callback,
    invoked from inside its own call stack with nothing to await; see
    `Worker._report_progress` for why it stays that way and what it costs.
    Asserting it rather than excusing it means that if it ever does move
    off the loop, this test says so instead of quietly agreeing.
    """
    import inspect

    where: dict[str, set[str]] = {}
    operations = [
        name
        for name, value in vars(JobStore).items()
        if not name.startswith("_")
        and not name.endswith("_async")
        and inspect.isfunction(value)
        and name != "close"
    ]
    originals = {name: getattr(JobStore, name) for name in operations}

    def watched(name, inner):
        def probe(self, *args, **kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                where.setdefault(name, set()).add("off the loop")
            else:
                where.setdefault(name, set()).add("on the loop")
            return inner(self, *args, **kwargs)

        return probe

    store, worker = _setup(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 100)
    for name, inner in originals.items():
        setattr(JobStore, name, watched(name, inner))
    try:
        await worker.run_job(job.nzo_id)
    finally:
        for name, inner in originals.items():
            setattr(JobStore, name, inner)

    assert store.get(job.nzo_id).status is JobStatus.COMPLETED
    assert where, "the worker never touched the store"
    assert where.pop("update_progress", None) == {"on the loop"}, (
        "update_progress has moved off the event loop -- good, but say so in"
        " Worker._report_progress and here"
    )
    assert all(v == {"off the loop"} for v in where.values()), where


async def test_the_worker_does_not_read_the_store_on_the_event_loop(
    tmp_path: Path,
):
    # The argument that applies to the routes applies to the worker in the
    # other direction: it shares the loop with every route in the service,
    # so a store read taken inline here stalls a Sonarr poll and a page
    # render. The `*_async` mirrors are what keep it off the loop, and this
    # is the only thing that observes it -- a plain `self._store.get(...)`
    # would leave every other worker test green.
    #
    # Asked of asyncio rather than of thread identity: inside an
    # `asyncio.to_thread` worker there is no running loop at all.
    where: list[str] = []
    store, worker = _setup(tmp_path)
    job = store.create("a", "stem", "WEBDL-1080p", 100)
    originals = {name: getattr(JobStore, name) for name in ("get", "complete")}
    for name, inner in originals.items():
        setattr(JobStore, name, _where_it_ran(where, inner))
    try:
        await worker.run_job(job.nzo_id)
    finally:
        for name, inner in originals.items():
            setattr(JobStore, name, inner)

    assert store.get(job.nzo_id).status is JobStatus.COMPLETED
    assert where, "the worker never read the store"
    assert set(where) == {"off the loop"}, where


async def test_the_poll_loop_does_not_read_the_store_on_the_event_loop(
    tmp_path: Path,
):
    where: list[str] = []
    store, worker = _setup(tmp_path)
    original = JobStore.queued
    JobStore.queued = _where_it_ran(where, original)
    try:
        runner = asyncio.create_task(worker.run_forever(poll_seconds=0.01))
        await asyncio.sleep(0.05)
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
    finally:
        JobStore.queued = original

    assert where, "the poll loop never read the store"
    assert set(where) == {"off the loop"}, where


# --- Stopping the downloads shutdown leaves behind --------------------


async def _dispatched(worker: Worker, timeout: float = 2.0) -> set:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not worker._jobs and loop.time() < deadline:
        await asyncio.sleep(0.01)
    return set(worker._jobs)


async def test_run_forever_keeps_hold_of_the_tasks_it_starts(tmp_path: Path):
    # It used to create them and drop the reference. Nothing could then wait
    # for a download, which is what let the app's shutdown close the job
    # store out from under one -- and, separately, is what asyncio warns
    # about: a task nothing holds can be collected mid-run.
    store, worker = _setup(tmp_path)
    worker._downloader = FakeDownloader(steps=200, total_bytes=100, delay=0.01)
    store.create("a", "stem", "WEBDL-1080p", 100)

    runner = asyncio.create_task(worker.run_forever(poll_seconds=0.01))
    try:
        jobs = await _dispatched(worker)
    finally:
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass

    assert len(jobs) == 1, "the worker did not keep hold of its download task"
    await worker.drain()


async def test_drain_stops_a_download_that_is_still_running(tmp_path: Path):
    store, worker = _setup(tmp_path)
    worker._downloader = FakeDownloader(steps=200, total_bytes=100, delay=0.01)
    job = store.create("a", "stem", "WEBDL-1080p", 100)

    runner = asyncio.create_task(worker.run_forever(poll_seconds=0.01))
    jobs = await _dispatched(worker)
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass

    await worker.drain()

    assert all(task.done() for task in jobs), "drain returned with a job running"
    assert worker._jobs == set(), "a finished task was not let go of"
    # Left Downloading, which is exactly what sweep_incomplete reconciles on
    # the next start -- the documented recovery path, and the same one a
    # power cut takes. What must not happen is a file in completed/ behind a
    # row that never got written.
    assert store.get(job.nzo_id).status is JobStatus.DOWNLOADING
    assert list((tmp_path / "completed").iterdir()) == []


async def test_drain_gives_up_rather_than_holding_shutdown_open(tmp_path: Path):
    # A download runs for as long as a download runs. Shutdown cannot wait
    # for one, and a task that ignores cancellation must not be able to keep
    # the service alive -- JobStore.close is what makes giving up safe.
    store, worker = _setup(tmp_path)
    running = asyncio.Event()
    stubborn = asyncio.Event()

    async def refuses_to_stop():
        running.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stubborn.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(refuses_to_stop())
    worker._jobs.add(task)
    # Started, not merely scheduled: a task cancelled before its first step
    # never reaches its own `except`, and the assertion below would then be
    # about the test rather than about `drain`.
    await running.wait()
    try:
        await worker.drain(timeout=0.1)  # must return
        assert stubborn.is_set(), "drain did not even try to cancel it"
        assert not task.done()
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


async def test_drain_on_a_worker_that_started_nothing_is_a_no_op(tmp_path: Path):
    store, worker = _setup(tmp_path)
    await worker.drain()
