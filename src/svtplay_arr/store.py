"""SQLite job store backing the SABnzbd queue/history endpoints.

Jobs live for the lifetime of a download: created Queued, moved through
Downloading as bytes arrive, and finally landing on Completed or Failed.
Status values are the `JobStatus` enum from `models.py` verbatim -- the SAB
endpoints a later task exposes to Sonarr depend on those exact strings.
"""

import sqlite3
import threading
import uuid
from pathlib import Path

from svtplay_arr.models import Job, JobStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    nzo_id TEXT PRIMARY KEY,
    svt_id TEXT NOT NULL,
    stem TEXT NOT NULL,
    quality TEXT NOT NULL,
    status TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    storage_path TEXT,
    fail_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class JobStoreError(RuntimeError):
    """The job database could not be read or written."""


class JobStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # WAL and busy_timeout below solve SQLite-*engine*-level lock
        # contention (SQLITE_BUSY, readers blocking behind a writer) --
        # they say nothing about whether it is safe to drive one Python
        # sqlite3.Connection object from multiple OS threads at once. It
        # isn't: the connection's own transaction/cursor bookkeeping is not
        # thread-safe, and concurrent execute()/commit() calls on it from
        # different threads have been observed (see tests/test_store.py's
        # concurrency regression test) to hand back rows with corrupted
        # columns -- not a raised error, silently wrong data. This lock is
        # what actually makes check_same_thread=False safe to use; do not
        # remove it because WAL is already in place, the two are unrelated
        # guarantees. Every operation here is one small statement, so the
        # serialisation this costs is negligible.
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # WAL's non-blocking-reader property isn't doing load-bearing
            # work today -- the lock above already serialises every access
            # to this one connection, so there is never a writer for a
            # reader to be blocked behind. It is kept anyway: it costs
            # nothing, and it is what would matter if this ever moves to
            # per-thread connections instead of one shared lock. busy_timeout
            # likewise guards against SQLITE_BUSY from contention outside
            # this connection's own callers (another process, a WAL
            # checkpoint) -- callers of this class never contend with each
            # other, since the lock rules that out.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not open job database at {db_path}") from exc

    def close(self) -> None:
        """Release the sqlite connection.

        Unclosed, the connection surfaces as `ResourceWarning: unclosed
        database` whenever the garbage collector happens to reach it --
        attributed to whatever code was running at that moment, which is
        why it read as noise for so long. The suite raised 33 of them and a
        `-W error` run failed, so CI could not enforce zero warnings at all;
        that is the real cost, since a warning gate that is off catches
        nothing else either.

        Calls after this raise `JobStoreError` like every other failure in
        this class -- sqlite's own `ProgrammingError` is a `sqlite3.Error`,
        so the existing wrapping already converts it. That matters for the
        SAB routes, which degrade on `JobStoreError` specifically; a
        different exception type would reach Sonarr as a 500.

        Idempotent: the app's shutdown and a test fixture may both close the
        same store, and neither should have to know about the other.
        """
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - sqlite rarely fails here
            raise JobStoreError("could not close the job database") from exc

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def create(
        self, svt_id: str, stem: str, quality: str, size_bytes: int
    ) -> Job:
        nzo_id = f"SVTPLAY-{uuid.uuid4().hex[:12]}"
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO jobs (nzo_id, svt_id, stem, quality, status, size_bytes)"
                    " VALUES (?,?,?,?,?,?)",
                    (nzo_id, svt_id, stem, quality, JobStatus.QUEUED.value, size_bytes),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not create job for {svt_id!r}") from exc
        job = self.get(nzo_id)
        if job is None:
            raise JobStoreError(f"job {nzo_id} vanished immediately after insert")
        return job

    def get(self, nzo_id: str) -> Job | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE nzo_id = ?", (nzo_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not read job {nzo_id!r}") from exc
        return _to_job(row) if row is not None else None

    def update_progress(self, nzo_id: str, downloaded_bytes: int) -> None:
        try:
            with self._lock:
                # The status filter is load-bearing, not defensive fluff: a
                # real downloader keeps reporting progress -- including one
                # mandatory final on_progress(size, size) call right before
                # it returns -- for as long as it is running, with no way to
                # know a job was independently terminated (e.g. by
                # mode=delete) while it was in flight. Without this filter,
                # the very next progress tick silently resurrects a Failed
                # or Completed row back to Downloading, and worker.py's
                # post-download re-check would then see a job that looks
                # like it's still running and publish it anyway. Bookkeeping
                # must never be able to un-terminate a job, so this only
                # ever moves a row that is currently Queued or Downloading;
                # a terminal row is left untouched (silently -- this is a
                # legitimate no-op, not an error).
                self._conn.execute(
                    "UPDATE jobs SET downloaded_bytes = ?, status = ?"
                    " WHERE nzo_id = ? AND status IN (?, ?)",
                    (
                        downloaded_bytes,
                        JobStatus.DOWNLOADING.value,
                        nzo_id,
                        JobStatus.QUEUED.value,
                        JobStatus.DOWNLOADING.value,
                    ),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not update progress for {nzo_id!r}") from exc

    def complete(self, nzo_id: str, storage_path: str) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE jobs SET status = ?, storage_path = ? WHERE nzo_id = ?",
                    (JobStatus.COMPLETED.value, storage_path, nzo_id),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not complete job {nzo_id!r}") from exc

    def fail(self, nzo_id: str, message: str) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE jobs SET status = ?, fail_message = ? WHERE nzo_id = ?",
                    (JobStatus.FAILED.value, message, nzo_id),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not fail job {nzo_id!r}") from exc

    def delete(self, nzo_id: str) -> bool:
        """Remove a row outright. Returns True if a row was actually removed.

        This is what `mode=history&name=delete` needs: SABnzbd's history
        delete removes the entry, it does not mark it failed. Failing it
        instead would leave the row in history forever, so Sonarr's cleanup
        after failed-download handling would never shrink anything and
        history would grow without bound.
        """
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "DELETE FROM jobs WHERE nzo_id = ?", (nzo_id,)
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not delete job {nzo_id!r}") from exc
        return cursor.rowcount > 0

    def queued(self) -> list[Job]:
        return [j for j in self._all() if j.status is JobStatus.QUEUED]

    def all_active(self) -> list[Job]:
        return [
            j
            for j in self._all()
            if j.status in (JobStatus.QUEUED, JobStatus.DOWNLOADING)
        ]

    def history(self) -> list[Job]:
        return [
            j
            for j in self._all()
            if j.status in (JobStatus.COMPLETED, JobStatus.FAILED)
        ]

    def _all(self) -> list[Job]:
        # Deliberately filters in Python rather than via SQL `WHERE status IN
        # (...)`: an unrecognised status literal would silently match none of
        # the listing queries and the job would vanish from every endpoint
        # Sonarr polls. Decoding every row here means a corrupted status
        # raises loudly (see _to_job) instead of a job disappearing unseen.
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at"
                ).fetchall()
        except sqlite3.Error as exc:
            raise JobStoreError("could not query jobs") from exc
        return [_to_job(r) for r in rows]


def _to_job(row: sqlite3.Row) -> Job:
    try:
        return _row_to_job(row)
    except ValueError as exc:
        raise JobStoreError(
            f"job {row['nzo_id']!r} has corrupted status {row['status']!r}"
        ) from exc


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        nzo_id=row["nzo_id"],
        svt_id=row["svt_id"],
        stem=row["stem"],
        quality=row["quality"],
        status=JobStatus(row["status"]),
        size_bytes=row["size_bytes"],
        downloaded_bytes=row["downloaded_bytes"],
        storage_path=row["storage_path"],
        fail_message=row["fail_message"],
    )
