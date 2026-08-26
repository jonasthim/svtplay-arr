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
        """Raises from queued() for the first few polls, then recovers."""

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
