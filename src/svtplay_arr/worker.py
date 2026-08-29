"""Job execution and publication.

Takes queued jobs from the store, drives a `Downloader` into a per-job
staging directory under `incomplete/`, and publishes the finished file into
`completed/` by atomic rename. Sonarr's completed-download handling imports
whatever it finds in `completed/`, so a partial file must never appear there:
publication happens only after the downloader has returned successfully, and
only via `Path.rename`, which is atomic within one filesystem. If
`incomplete/` and `completed/` are ever on different filesystems, `rename`
silently degrades to copy-then-delete and this guarantee is lost --
`Settings.dirs_share_filesystem()` exists to catch that case elsewhere.

Everything here that runs on the event loop reaches the job store through
its `*_async` mirrors rather than calling it directly: this worker shares a
loop with every route in the service, so a store read taken inline here
would stall a Sonarr poll and a page render, exactly as a page render's
store read used to stall this worker. The two exceptions are deliberate and
named where they are: `sweep_incomplete` (startup only, already blocking on
`rmtree`) and `_report_progress` (a synchronous callback in the downloader's
own call stack, and one small UPDATE on this thread's own connection).
"""

import asyncio
import logging
import shutil
from pathlib import Path

from svtplay_arr.downloader import Downloader
from svtplay_arr.models import JobStatus
from svtplay_arr.store import JobStore

log = logging.getLogger(__name__)

_FILE_MODE = 0o664
_DIR_MODE = 0o775


class WorkerError(RuntimeError):
    """Raised when a job cannot be published as expected."""


class Worker:
    def __init__(
        self,
        store: JobStore,
        downloader: Downloader,
        incomplete_dir: Path,
        completed_dir: Path,
        concurrency: int = 1,
    ):
        self._store = store
        self._downloader = downloader
        self._incomplete = incomplete_dir
        self._completed = completed_dir
        self._sem = asyncio.Semaphore(concurrency)
        # Populated by run_forever() before a job is dispatched, cleared in
        # run_job()'s outer finally. Without this, a job that stays QUEUED
        # for longer than one poll tick (i.e. essentially every real
        # download, whose setup alone can exceed poll_seconds) gets
        # re-dispatched as a brand new task on every subsequent tick,
        # multiplying bandwidth and -- at concurrency > 1 -- letting two
        # duplicates race on the same staging directory.
        self._in_flight: set[str] = set()
        # The task objects behind those ids. `run_forever` used to create
        # them and drop the reference, which meant nothing could wait for
        # them: shutdown cancelled the poll loop, saw it finish, and closed
        # the job store while a download was still between `_publish()` and
        # its `complete()` write. That job then died with a JobStoreError,
        # leaving the file in `completed/` and the row saying Downloading --
        # so the next start failed it, Sonarr re-grabbed the episode, and
        # the first copy was orphaned. See `drain`.
        #
        # A strong reference is also what stops the event loop garbage
        # collecting a running task, which asyncio documents and nothing
        # here was relying on only by luck.
        self._jobs: set[asyncio.Task] = set()

    def sweep_incomplete(self) -> None:
        """Clean up after a crash: drop stale partials AND reconcile the store.

        Both halves matter. Partials must never be importable, so anything
        left in incomplete/ goes. But a job that was mid-download when the
        process died is also still Downloading in the store, and run_forever
        only ever re-dispatches Queued jobs -- so without the reconciliation
        below it would sit in Sonarr's Activity tab at its last reported
        percentage forever, never retried and never marked failed, with no
        error shown anywhere.

        Marking it Failed is the whole recovery path: the in-flight download
        cannot be resumed (svtplay-dl has no resume, and its partial has just
        been deleted), but a Failed job with a fail_message is exactly what
        Sonarr's autoRedownloadFailed acts on, so the episode gets a fresh
        grab of its own accord.
        """
        self._fail_interrupted_jobs()
        if not self._incomplete.exists():
            return
        for child in self._incomplete.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def _fail_interrupted_jobs(self) -> None:
        # Downloading -> Failed is a legal move for store.fail() (unlike
        # update_progress, which refuses to move a row *out* of a terminal
        # state), and once failed the row is terminal, so a stray late
        # progress tick cannot resurrect it.
        #
        # Store errors are logged and swallowed rather than raised: this runs
        # during startup, and reconciliation failing must not stop the far
        # more important partial-file sweep below it, nor prevent the service
        # from coming up at all.
        try:
            active = self._store.all_active()
        except Exception:
            log.exception("could not read active jobs to reconcile after restart")
            return
        for job in active:
            if job.status is not JobStatus.DOWNLOADING:
                continue
            log.warning(
                "job %s (%s) was still downloading at startup; failing it so "
                "Sonarr re-searches",
                job.nzo_id,
                job.stem,
            )
            try:
                self._store.fail(job.nzo_id, "interrupted by restart")
            except Exception:
                log.exception("could not fail interrupted job %s", job.nzo_id)

    async def run_job(self, nzo_id: str) -> None:
        try:
            job = await self._store.get_async(nzo_id)
            if job is None or job.status is not JobStatus.QUEUED:
                # Nothing to do: unknown job, or a stale/duplicate dispatch
                # for a job another invocation has already started, finished,
                # or failed.
                return
            staging = self._incomplete / nzo_id
            async with self._sem:
                # Re-read after the semaphore: this job may have been queued
                # behind another in-flight run of itself (a stale dispatch
                # that outlived run_forever's own in-flight tracking, or a
                # direct duplicate call) and already finished while we
                # waited. A stale snapshot must not cause a redo.
                job = await self._store.get_async(nzo_id)
                if job is None or job.status is not JobStatus.QUEUED:
                    return
                try:
                    await self._downloader.download(
                        job.svt_id,
                        staging,
                        job.stem,
                        lambda done, total: self._report_progress(nzo_id, done),
                    )
                    if not await self._still_running(nzo_id):
                        # Something external (e.g. Sonarr's mode=delete)
                        # terminated this job while the download was in
                        # flight. The download itself has no cancellation
                        # hook, so it ran to completion anyway -- but
                        # nothing may reach completed/ for a job that was
                        # deleted, and the terminal status that ended it
                        # (Failed, typically) must not be overwritten by an
                        # unconditional complete().
                        log.info(
                            "job %s no longer running after download finished;"
                            " skipping publish",
                            nzo_id,
                        )
                        return
                    final = self._publish(staging, job.stem)
                    # Synchronous, and this is the second and last place in
                    # this module where that is deliberate. There must be
                    # no await between publishing the file and recording
                    # it: a cancellation landing in that gap leaves
                    # `stem.mkv` in `completed/` for Sonarr to import while
                    # the row still says Downloading, so the next start
                    # fails that row, Sonarr re-grabs the episode, and the
                    # first copy is orphaned in the library. `drain` makes
                    # cancellation here routine rather than exotic, and
                    # forcing the gap open produced that outcome 40 times
                    # out of 40.
                    #
                    # `asyncio.shield` was the obvious alternative and does
                    # not close it: `to_thread` cancelled before the
                    # executor picks the call up drops it entirely, so the
                    # window becomes "until a worker thread starts the
                    # write" rather than vanishing -- and every route's
                    # store access shares that executor, so it is not a
                    # narrow window under load. Removing the suspension
                    # point is the only version with no gap at all.
                    #
                    # The cost is one small UPDATE on the event loop, at
                    # most once per completed download -- far less than
                    # `_report_progress` already spends per tick, and
                    # bounded the same way. See `_report_progress`.
                    self._store.complete(nzo_id, str(final))
                except Exception as exc:
                    log.exception("job %s failed", nzo_id)
                    await self._store.fail_async(nzo_id, str(exc))
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
        finally:
            self._in_flight.discard(nzo_id)

    async def _still_running(self, nzo_id: str) -> bool:
        """True if the job is still QUEUED/DOWNLOADING right now. Re-reads
        the store rather than trusting the snapshot taken before the
        download started, since that snapshot can't see an out-of-band
        change (e.g. mode=delete) made while the download was in flight."""
        current = await self._store.get_async(nzo_id)
        return current is not None and current.status in (
            JobStatus.QUEUED,
            JobStatus.DOWNLOADING,
        )

    def _report_progress(self, nzo_id: str, downloaded_bytes: int) -> None:
        # Synchronous, unlike everything else here that touches the store,
        # and therefore **a blocking sqlite write on the event loop**. Say
        # it plainly, because it is the one place in this service where
        # that is still true and an earlier version of this comment implied
        # otherwise: `SvtplayDlDownloader` polls the staging directory from
        # a coroutine and calls this from there, so the write runs on the
        # loop the routes are served on. Ordinarily it is one small UPDATE
        # against this thread's own connection and costs microseconds. It
        # is not bounded, though: if another connection is holding the WAL
        # write lock, this waits out `busy_timeout` -- five seconds of
        # stalled event loop, then `database is locked` -- which is worth
        # knowing before anyone adds a second long write to this store.
        #
        # It stays synchronous because it cannot be anything else. This is
        # the `ProgressFn` callback the downloader invokes from inside its
        # own call stack; making it awaitable means changing the
        # `Downloader` protocol and every implementation of it, to move a
        # write that is measured in microseconds. Note that this is not a
        # regression: before the store had per-thread connections, this
        # same call could wait behind a full table scan taken by a page
        # render, on the same loop, with a Python lock in the way as well.
        #
        # A monitoring/bookkeeping mechanism must never be able to fail the
        # thing it monitors: a transient store error here (busy database,
        # disk full, connection closing during shutdown) must not cost a
        # download that may otherwise complete cleanly. Errors are logged,
        # not raised -- this callback runs inside the downloader's call
        # stack, and raising would abort the download itself.
        try:
            self._store.update_progress(nzo_id, downloaded_bytes)
        except Exception:
            log.warning(
                "progress update failed for job %s (continuing download)",
                nzo_id,
                exc_info=True,
            )

    def _publish(self, staging: Path, stem: str) -> Path:
        """Publish by atomic rename. Requires one filesystem for both dirs."""
        # mkdir's own `mode` argument is masked by the process umask, so the
        # requested 775 is applied explicitly afterwards rather than trusted
        # to mkdir() to have set it.
        self._completed.mkdir(parents=True, exist_ok=True)
        self._completed.chmod(_DIR_MODE)
        video = staging / f"{stem}.mkv"
        if not video.exists():
            raise WorkerError(f"expected {video.name} in staging")
        final = self._completed / video.name
        sidecar_prefix = f"{stem}."
        moved: list[Path] = []
        try:
            # Sidecars (e.g. subtitles) travel with the video, but only ones
            # keyed by matching the video's stem exactly -- anything else in
            # staging is not part of this release and is left behind (swept
            # up with the rest of the staging directory afterwards). The
            # video itself is renamed last: sidecars land in completed/
            # first, so the video's atomic rename is the single moment this
            # job becomes visible/importable to Sonarr as a whole.
            for extra in sorted(staging.iterdir()):
                if extra == video:
                    continue
                if not extra.name.startswith(sidecar_prefix):
                    log.warning(
                        "skipping %s in staging: does not match stem %r",
                        extra.name,
                        stem,
                    )
                    continue
                dest = self._completed / extra.name
                extra.chmod(_FILE_MODE)
                extra.rename(dest)
                moved.append(dest)
            video.chmod(_FILE_MODE)
            video.rename(final)  # atomic: the video appears whole or not at all
        except Exception:
            # The video never made it across, so any sidecars already moved
            # must not linger in completed/ as orphaned, importable-looking
            # leftovers -- completed/ must reflect the video's presence.
            for dest in moved:
                dest.unlink(missing_ok=True)
            raise
        return final

    async def drain(self, timeout: float = 10.0) -> None:
        """Stop every download this worker started, and wait for it.

        Shutdown cancels the poll loop, but the jobs it dispatched are
        separate tasks and cancelling their parent does nothing to them.
        Without this they run on into `store.close()` and die with a
        JobStoreError at whatever point they had reached -- including the
        window between publishing a finished file into `completed/` and
        recording it, which leaves an imported file behind a row that says
        Downloading. The next start fails that row (correctly, it cannot
        know), Sonarr re-grabs the episode, and the first copy is orphaned.

        Cancelled rather than awaited to completion: a download runs for
        as long as a download runs, and shutdown cannot wait for one. A
        job cancelled mid-download stays Downloading, which is exactly the
        state `sweep_incomplete` reconciles on the next start -- the
        documented recovery path, and the same one a power cut takes.

        The remaining window is the few microseconds between `_publish`
        returning and `complete_async` being dispatched; it was previously
        the entire length of every download.
        """
        if not self._jobs:
            return
        jobs = list(self._jobs)
        for task in jobs:
            task.cancel()
        # Every outcome is swallowed: a cancelled job raises CancelledError,
        # and one that had already failed re-raises whatever killed it --
        # logged where it happened. Shutdown proceeds regardless, and the
        # timeout means a task that will not die cannot hold the service
        # open (its store writes are safe either way; see JobStore.close).
        await asyncio.wait(jobs, timeout=timeout)

    async def run_forever(self, poll_seconds: float = 2.0) -> None:
        self.sweep_incomplete()
        while True:
            # The poll body is guarded because a single bad row can otherwise
            # end downloading permanently: queued() raises JobStoreError if
            # any row carries a status literal it doesn't recognise, that
            # exception kills this task, and restarting does not help because
            # the offending row is still in the database. One unreadable job
            # must cost that job, not every future one. The sleep stays
            # outside the guard so a persistent failure still paces itself
            # (and so cancellation at shutdown is never swallowed).
            try:
                for job in await self._store.queued_async():
                    if job.nzo_id in self._in_flight:
                        continue
                    self._in_flight.add(job.nzo_id)
                    task = asyncio.create_task(self.run_job(job.nzo_id))
                    self._jobs.add(task)
                    task.add_done_callback(self._jobs.discard)
            except Exception:
                log.exception("worker poll failed; retrying next tick")
            await asyncio.sleep(poll_seconds)
