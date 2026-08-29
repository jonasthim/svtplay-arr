"""SQLite job store backing the SABnzbd queue/history endpoints.

Jobs live for the lifetime of a download: created Queued, moved through
Downloading as bytes arrive, and finally landing on Completed or Failed.
Status values are the `JobStatus` enum from `models.py` verbatim -- the SAB
endpoints a later task exposes to Sonarr depend on those exact strings.

Two structural facts about this module are worth reading before changing it.

**One connection per thread, and no global lock.** A `sqlite3.Connection` is
not safe to drive from several OS threads at once: its own statement cache
and cursor bookkeeping are unsynchronised, and concurrent `execute()`/
`commit()` calls on one connection hand readers back rows with torn columns
-- a NULL status, an empty stem, a size belonging to another row -- rather
than raising. That is a silent-wrong-data bug, and it was reproduced, not
theorised (see tests/test_store_concurrency.py, which still reproduces it on
demand). The fix used to be a `threading.Lock` around every operation, which
was correct but made the store a single-file queue: a page render's full
table scan and the download worker's next progress write took turns. Under
WAL -- already enabled, and now load-bearing rather than decorative --
concurrent readers and one writer are safe *given separate connections*, so
each thread gets its own and no store operation waits on another. There is
still one lock, per connection rather than global and therefore uncontended
between callers: it exists so `close()` cannot free a connection out from
under a query still running on the thread that owns it, which segfaults the
interpreter rather than raising (see `_use`). All connections are registered
so `close()` can find them; see `close` for why that registry is what keeps
the suite's warning gate green.

**The schema is versioned with `PRAGMA user_version`.** `_prepare_database`
runs once per store, before any per-thread connection is handed out. Version
1 is the schema as it shipped through v0.5.1, which stamped nothing -- so a
database already carrying job history reads as version 0 and is *adopted* at
the baseline rather than rebuilt. Migrations are then applied in order, each
in its own `BEGIN IMMEDIATE` transaction, so a failure leaves the database
exactly as it was rather than half-migrated. A database from a future
version is refused outright: a downgrade that silently kept writing would
corrupt data nothing in this build knows how to read.
"""

import asyncio
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from svtplay_arr.models import Job, JobStatus

log = logging.getLogger(__name__)

# Applied to every connection this module opens. It guards against
# SQLITE_BUSY from contention at the *engine* level -- another connection
# mid-write, a WAL checkpoint, another process -- which is the only kind of
# contention left now that nothing serialises callers in Python.
_BUSY_TIMEOUT_MS = 5000
# How long to keep trying to switch a database into WAL. Unlike ordinary
# lock contention this is *not* covered by busy_timeout: converting the
# journal mode needs an exclusive lock, and sqlite deliberately does not run
# the busy handler for it -- it fails immediately with "database is locked"
# instead. The conversion happens at most once in a database's life (every
# release since the store existed has opened it in WAL), and the only way to
# meet it is to open a database written by something else at the same moment
# as another process. Retrying is what turns that into a wait rather than a
# failed start.
_WAL_TIMEOUT_S = 5.0
_WAL_RETRY_S = 0.02
# How long `close()` waits for an operation still running on a connection
# before giving up on closing it. Every store operation here is one small
# statement, so reaching this at all means something is badly wrong -- and
# leaving a connection open costs a warning, while closing it under a live
# query costs the process.
_CLOSE_TIMEOUT_S = 5.0

# Version 1 is what every installation before schema versioning existed is
# running: the `jobs` table exactly as it was created below, with
# `user_version` left at sqlite's default of 0. Nothing here may ever be
# edited -- a baseline that changes is a baseline that disagrees with the
# databases it is supposed to describe. New columns and indexes go in
# `_MIGRATIONS` instead, which is also what runs them on a fresh install.
_BASELINE_VERSION = 1
_BASELINE_SCHEMA = """
CREATE TABLE jobs (
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


class _Registered(NamedTuple):
    """One thread's connection, and the lock `close()` waits on."""

    thread: threading.Thread
    conn: sqlite3.Connection
    lock: "threading.RLock"


def _migrate_2_index_created_at(conn: sqlite3.Connection) -> None:
    """Index `created_at`, which every listing orders by.

    `_all()` is `SELECT * FROM jobs ORDER BY created_at` and it is the hot
    read: Sonarr polls `mode=queue` and `mode=history` on a schedule, and
    the config page's Activity view reads the whole table per render.
    Without an index that ordering is a sort of the entire table each time.

    Additive and behaviour-preserving by construction -- an index changes
    no row and no query result, only how the same result is reached -- which
    is what makes it the right first migration to prove the runner on.
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")


# Keyed by the version each migration *produces*, applied in ascending key
# order. Every entry must be additive: this project ships to installations
# whose only copy of a download's history is the file being migrated, so a
# migration that drops or rewrites a column has to be reviewed as data loss,
# not as a schema change.
_MIGRATIONS = {
    2: _migrate_2_index_created_at,
}

# What this build writes and understands. Anything higher in a database on
# disk was written by a newer release and is refused; see `_prepare_database`.
SCHEMA_VERSION = max(_MIGRATIONS, default=_BASELINE_VERSION)


@contextmanager
def _transaction(conn: sqlite3.Connection):
    """One all-or-nothing unit of migration work.

    `BEGIN IMMEDIATE` rather than a plain `BEGIN`: the write lock is taken
    up front, so two processes opening the same database at once queue on
    it (bounded by `busy_timeout`) instead of one discovering half way
    through that it cannot upgrade its snapshot.

    Driven by hand because these transactions wrap DDL. Python's sqlite3
    only opens an implicit transaction around DML, so a `CREATE`/`ALTER`
    run under the default `isolation_level` would autocommit itself and a
    failure part-way through a multi-statement migration would be
    unrollbackable. The migration connection is opened in autocommit mode
    (`isolation_level=None`) precisely so these statements are the only
    transaction control in play.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA does not accept bound parameters, hence the interpolation; the
    # value is an int from this module's own table of migrations, never from
    # anything a caller supplies. It is written to the database header
    # inside the surrounding transaction, so it rolls back with the
    # migration it stamps rather than outliving one that failed.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _has_jobs_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
    ).fetchone()
    return row is not None


def _adopt_baseline(conn: sqlite3.Connection, db_path: Path) -> int:
    """Bring an unstamped database up to version 1 without touching rows.

    `user_version` 0 means one of two things and they must not be confused:
    a database this build has never seen (create the table), or a database
    from before schema versioning existed and full of real job history
    (create nothing, stamp it). There is one deployed installation of the
    second kind, and rebuilding its table would be exactly the data loss
    this mechanism exists to make impossible -- so the presence of the
    table, not the presence of the file, is what decides.
    """
    with _transaction(conn):
        # Re-read inside the write lock: two processes may have opened the
        # same database at once and the other may already have adopted it.
        current = _user_version(conn)
        if current != 0:
            return current
        existing = _has_jobs_table(conn)
        if not existing:
            conn.execute(_BASELINE_SCHEMA)
        _set_user_version(conn, _BASELINE_VERSION)
    # Logged at INFO because it happens exactly once per installation and
    # it is the only moment an operator can see that their existing job
    # history was recognised rather than replaced. A silent upgrade of a
    # database is the kind of thing that is only ever noticed by the person
    # whose history went missing.
    log.info(
        "job database %s: %s, now at schema version %d",
        db_path,
        "adopted the jobs table already there"
        if existing
        else "created a new jobs table",
        _BASELINE_VERSION,
    )
    return _BASELINE_VERSION


def _apply(conn: sqlite3.Connection, target: int) -> None:
    """Run one migration, or nothing at all."""
    try:
        with _transaction(conn):
            # Same re-read as `_adopt_baseline`, for the same reason: the
            # version was read before this transaction took the write lock.
            if _user_version(conn) != target - 1:
                return
            log.info("migrating the job database to schema version %d", target)
            _MIGRATIONS[target](conn)
            _set_user_version(conn, target)
    except Exception as exc:
        # The rollback has already happened in `_transaction`, so what the
        # operator is told is true of the file on disk: nothing changed,
        # and the previous version of the service still reads it.
        raise JobStoreError(
            f"migration to job database schema version {target} failed and was"
            " rolled back; the database is unchanged"
        ) from exc


def _enable_wal(conn: sqlite3.Connection, db_path: Path) -> None:
    """Put the database into WAL, waiting out anyone else converting it.

    WAL is a property of the database file, not of a connection: set once,
    every later connection inherits it. It is what makes per-thread
    connections worth having -- readers do not block behind the writer, so
    removing the store's global lock removed waiting rather than moving it
    into sqlite -- which is why failing to get it is fatal rather than a
    silent fall back to a mode where every reader and writer takes turns
    again.

    Asking for WAL when the database is already in WAL is a no-op that
    needs no exclusive lock, so the retry below only ever runs on the
    one-time conversion; see `_WAL_TIMEOUT_S`.
    """
    deadline = time.monotonic() + _WAL_TIMEOUT_S
    while True:
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError:
            mode = None
        if str(mode).lower() == "wal":
            return
        if time.monotonic() >= deadline:
            raise JobStoreError(
                f"could not switch the job database at {db_path} into WAL mode"
                f" (it is in {mode!r}); something else is holding it open."
            )
        time.sleep(_WAL_RETRY_S)


def _prepare_database(db_path: Path) -> None:
    """Create, adopt or migrate the database at `db_path`.

    Runs on a connection of its own, opened in autocommit mode so
    `_transaction` is the only thing that begins or ends a transaction, and
    closed before any per-thread connection is handed out.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        _enable_wal(conn, db_path)
        version = _user_version(conn)
        if version > SCHEMA_VERSION:
            # Refused before anything is written, and deliberately fatal:
            # this build does not know what the newer release changed, and
            # writing rows in the shape it expects could corrupt data the
            # newer one would otherwise still read. Nothing is deleted, so
            # reinstalling the newer release recovers the installation
            # exactly as it was.
            raise JobStoreError(
                f"job database at {db_path} is at schema version {version}, but"
                f" this build of svtplay-arr understands version"
                f" {SCHEMA_VERSION}. It was written by a newer release;"
                " nothing has been changed. Upgrade svtplay-arr again, or"
                " point svtplay-arr at a database written by this version."
            )
        if version == 0:
            version = _adopt_baseline(conn, db_path)
        for target in range(version + 1, SCHEMA_VERSION + 1):
            _apply(conn, target)
    finally:
        conn.close()


class JobStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        # Where a thread finds its own connection. Nothing else may hand a
        # connection from one thread to another: that is the entire defence
        # against the corruption described in this module's docstring, and
        # it is why `_connection` is the only place `sqlite3.connect` is
        # called after startup.
        self._local = threading.local()
        # Guards the registry below and the closed flag -- not the database.
        # It is held for the length of a list append, never for the length
        # of a query, so no store operation ever waits on another one.
        self._registry_lock = threading.Lock()
        self._connections: list[_Registered] = []
        self._closed = False
        try:
            _prepare_database(db_path)
            # Opened eagerly so a database that cannot be used fails here,
            # in the caller that built the store, rather than on whichever
            # request first touched it.
            self._connection()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not open job database at {db_path}") from exc

    def _checkout(self) -> tuple[sqlite3.Connection, threading.RLock]:
        """This thread's connection and its lock, opening one on first use."""
        if self._closed:
            raise JobStoreError("the job database is closed")
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn, self._local.lock
        # check_same_thread=False is not permission to share this
        # connection -- the thread-local above is what guarantees nothing
        # does. It is needed only so `close()` can release connections
        # belonging to threads other than the one shutting the store down;
        # sqlite3's own check covers close() too, and without this the
        # app's shutdown could not close the connections its request
        # threads opened.
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        except BaseException:  # pragma: no cover - sqlite rarely fails here
            # Nothing has registered this connection yet, so nothing else
            # would ever close it.
            conn.close()
            raise
        # Reentrant so an operation implemented in terms of another one --
        # `queued()` on `_all()`, say -- cannot deadlock against itself on
        # the thread that already holds it.
        lock = threading.RLock()
        with self._registry_lock:
            if self._closed:
                # Closed while this connection was being opened. Registering
                # it now would mean nothing ever closes it.
                conn.close()
                raise JobStoreError("the job database is closed")
            self._release_finished_threads()
            self._connections.append(
                _Registered(threading.current_thread(), conn, lock)
            )
        self._local.conn = conn
        self._local.lock = lock
        return conn, lock

    def _connection(self) -> sqlite3.Connection:
        """This thread's connection, opening one the first time it asks."""
        return self._checkout()[0]

    @contextmanager
    def _use(self):
        """This thread's connection, for the length of one operation.

        The lock is per *connection*, and a connection has exactly one user
        thread, so in the ordinary path it is uncontended -- it is not the
        global lock this store used to serialise everything through, and no
        store operation ever waits on another one because of it.

        It exists for `close()`, which is the one thing that touches a
        connection from a thread other than its owner. Without it, closing
        the store while an operation was in flight on another thread frees
        a `sqlite3.Connection` out from under a running query, which
        segfaults the interpreter rather than raising. That is not
        hypothetical: `asyncio.to_thread` does not stop the worker thread
        when the awaiting task is cancelled, so the app's shutdown --
        cancel the worker task, then close the store -- reliably produced
        exactly that race, roughly one run of the test suite in five.
        """
        conn, lock = self._checkout()
        with lock:
            yield conn

    def _release_finished_threads(self) -> None:
        """Close connections whose thread has ended.

        A thread's `threading.local` storage is discarded when the thread
        dies, so without the registry its connection would be collected by
        the garbage collector instead of closed -- which is precisely the
        `ResourceWarning: unclosed database` that `close()` exists to
        prevent. With the registry it is merely idle, and a dead thread
        cannot be mid-query, so closing it here is safe. Callers that keep
        the store for the process lifetime (the worker, the request
        threadpool) reuse long-lived threads and reach this rarely; it
        exists so a caller that spawns threads does not accumulate them.
        """
        live: list[_Registered] = []
        for entry in self._connections:
            # The lock is taken without waiting, so the invariant `close()`
            # rests on holds everywhere: a connection is never closed except
            # while its own lock is held. A dead thread cannot be holding
            # it, so this only ever fails in a shape that should be left
            # alone anyway.
            if entry.thread.is_alive() or not entry.lock.acquire(blocking=False):
                live.append(entry)
                continue
            try:
                entry.conn.close()
            except sqlite3.Error:  # pragma: no cover - sqlite rarely fails here
                live.append(entry)
            finally:
                entry.lock.release()
        self._connections = live

    def close(self) -> None:
        """Release every connection this store opened.

        Unclosed, a connection surfaces as `ResourceWarning: unclosed
        database` whenever the garbage collector happens to reach it --
        attributed to whatever code was running at that moment, which is
        why it read as noise for so long. The suite raised 33 of them and a
        `-W error` run failed, so CI could not enforce zero warnings at all;
        that is the real cost, since a warning gate that is off catches
        nothing else either. With one connection per thread there are now
        several to release rather than one, which is what the registry is
        for -- a thread-local alone would leave every connection but the
        closing thread's to the garbage collector.

        Calls after this raise `JobStoreError` like every other failure in
        this class -- both the closed flag below and sqlite's own
        `ProgrammingError` (a `sqlite3.Error`, so the existing wrapping
        converts it) produce that type. It matters for the SAB routes,
        which degrade on `JobStoreError` specifically; a different exception
        type would reach Sonarr as a 500.

        Idempotent: the app's shutdown and a test fixture may both close the
        same store, and neither should have to know about the other.
        """
        failures: list[sqlite3.Error] = []
        with self._registry_lock:
            # Set first: a caller that reaches `_checkout` while this is
            # running must be told the store is closed rather than handed a
            # connection about to be closed underneath it.
            self._closed = True
            connections = self._connections
            self._connections = []
        for entry in connections:
            # Waits out an operation still in flight on that connection's
            # own thread -- see `_use` for why closing under one segfaults
            # rather than raising. In the ordinary shutdown this is
            # uncontended; the case it covers is a store call left running
            # on a threadpool thread by a cancelled `asyncio.to_thread`.
            held = entry.lock.acquire(timeout=_CLOSE_TIMEOUT_S)
            try:
                if not held:
                    # Deliberately not closed. An unclosed connection costs
                    # a `ResourceWarning` at some later collection; closing
                    # one mid-query costs the process.
                    log.warning(
                        "job database connection for %s is still busy after"
                        " %.0fs; leaving it open rather than closing it under"
                        " a query in flight",
                        entry.thread.name,
                        _CLOSE_TIMEOUT_S,
                    )
                    continue
                entry.conn.close()
            except sqlite3.Error as exc:  # pragma: no cover - rarely fails
                failures.append(exc)
            finally:
                if held:
                    entry.lock.release()
        if failures:  # pragma: no cover - sqlite rarely fails here
            raise JobStoreError("could not close the job database") from failures[0]

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def create(
        self, svt_id: str, stem: str, quality: str, size_bytes: int
    ) -> Job:
        nzo_id = f"SVTPLAY-{uuid.uuid4().hex[:12]}"
        try:
            with self._use() as conn:
                conn.execute(
                    "INSERT INTO jobs"
                    " (nzo_id, svt_id, stem, quality, status, size_bytes)"
                    " VALUES (?,?,?,?,?,?)",
                    (nzo_id, svt_id, stem, quality, JobStatus.QUEUED.value, size_bytes),
                )
                conn.commit()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not create job for {svt_id!r}") from exc
        job = self.get(nzo_id)
        if job is None:
            raise JobStoreError(f"job {nzo_id} vanished immediately after insert")
        return job

    def get(self, nzo_id: str) -> Job | None:
        try:
            with self._use() as conn:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE nzo_id = ?", (nzo_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not read job {nzo_id!r}") from exc
        return _to_job(row) if row is not None else None

    def update_progress(self, nzo_id: str, downloaded_bytes: int) -> None:
        try:
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
            with self._use() as conn:
                conn.execute(
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
                conn.commit()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not update progress for {nzo_id!r}") from exc

    def complete(self, nzo_id: str, storage_path: str) -> None:
        try:
            with self._use() as conn:
                conn.execute(
                    "UPDATE jobs SET status = ?, storage_path = ? WHERE nzo_id = ?",
                    (JobStatus.COMPLETED.value, storage_path, nzo_id),
                )
                conn.commit()
        except sqlite3.Error as exc:
            raise JobStoreError(f"could not complete job {nzo_id!r}") from exc

    def fail(self, nzo_id: str, message: str) -> None:
        try:
            with self._use() as conn:
                conn.execute(
                    "UPDATE jobs SET status = ?, fail_message = ? WHERE nzo_id = ?",
                    (JobStatus.FAILED.value, message, nzo_id),
                )
                conn.commit()
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
            with self._use() as conn:
                cursor = conn.execute("DELETE FROM jobs WHERE nzo_id = ?", (nzo_id,))
                conn.commit()
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

    def all_jobs(self) -> list[Job]:
        """Every row, oldest first, whatever its status.

        For a caller that needs the queue *and* the history in one render
        -- the config page's Activity view -- and would otherwise scan the
        same table twice for one page. Partitioning one read in Python
        costs nothing at this size and is one fewer thing between a page
        load and a download.

        Raises `JobStoreError` like every other read here, and that is
        load-bearing rather than incidental: "nothing has failed" and
        "the store cannot be read" are different answers, and only a
        caller handed a failure can tell them apart.
        """
        return self._all()

    def _all(self) -> list[Job]:
        # Deliberately filters in Python rather than via SQL `WHERE status IN
        # (...)`: an unrecognised status literal would silently match none of
        # the listing queries and the job would vanish from every endpoint
        # Sonarr polls. Decoding every row here means a corrupted status
        # raises loudly (see _to_job) instead of a job disappearing unseen.
        try:
            with self._use() as conn:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at"
                ).fetchall()
        except sqlite3.Error as exc:
            raise JobStoreError("could not query jobs") from exc
        return [_to_job(r) for r in rows]

    # --- The same operations, for callers on the event loop ---------------
    #
    # Every route in this service is `async def` and stays that way: they
    # do network I/O to Sonarr and SVT, which is reason enough on its own.
    # But a coroutine that calls a method above directly runs a blocking
    # sqlite read or write *on the event loop* -- the loop the download
    # worker also runs on -- so a page render or a Sonarr poll can stall a
    # download, and a download's write can stall a poll.
    #
    # These mirrors are the seam. Each hops onto a worker thread, where it
    # gets that thread's own connection and blocks nobody. There is one for
    # every public operation above rather than for the handful that happen
    # to have an async caller today: a partial set is a rule with
    # exceptions to remember, and the next operation added would quietly
    # get none. A test sweeps for completeness so it stays that way.

    async def create_async(
        self, svt_id: str, stem: str, quality: str, size_bytes: int
    ) -> Job:
        return await asyncio.to_thread(
            self.create, svt_id, stem, quality, size_bytes
        )

    async def get_async(self, nzo_id: str) -> Job | None:
        return await asyncio.to_thread(self.get, nzo_id)

    async def update_progress_async(
        self, nzo_id: str, downloaded_bytes: int
    ) -> None:
        await asyncio.to_thread(self.update_progress, nzo_id, downloaded_bytes)

    async def complete_async(self, nzo_id: str, storage_path: str) -> None:
        await asyncio.to_thread(self.complete, nzo_id, storage_path)

    async def fail_async(self, nzo_id: str, message: str) -> None:
        await asyncio.to_thread(self.fail, nzo_id, message)

    async def delete_async(self, nzo_id: str) -> bool:
        return await asyncio.to_thread(self.delete, nzo_id)

    async def queued_async(self) -> list[Job]:
        return await asyncio.to_thread(self.queued)

    async def all_active_async(self) -> list[Job]:
        return await asyncio.to_thread(self.all_active)

    async def history_async(self) -> list[Job]:
        return await asyncio.to_thread(self.history)

    async def all_jobs_async(self) -> list[Job]:
        return await asyncio.to_thread(self.all_jobs)


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
        # Written by the schema's own default and, until the config page
        # grew an Activity view, never read by anything. A list of stems
        # with no times against them does not answer "why didn't that
        # episode arrive?".
        created_at=row["created_at"],
    )
