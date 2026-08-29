"""Schema versioning for the job database.

There are installations of this service with real download history in them,
and until now the schema was whatever the running build happened to create:
`CREATE TABLE IF NOT EXISTS`, no version stamp, no upgrade path. Any change
to the table would have broken every existing installation with nothing to
detect it and nothing to recover it.

These tests pin the mechanism rather than any particular schema change. The
properties that matter are the ones an operator's data depends on: a
database that already has history is *adopted*, never rebuilt; a migration
that fails leaves the file exactly as it was; and a database written by a
newer release is refused rather than written to in a shape it does not
understand.
"""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from svtplay_arr import store as store_module
from svtplay_arr.models import JobStatus
from svtplay_arr.store import JobStore, JobStoreError

# Every store a test opens, closed by the autouse fixture below. See
# JobStore.close for why an unclosed connection is a warning-gate failure
# rather than a tidiness issue.
_OPEN_STORES: list[JobStore] = []


@pytest.fixture(autouse=True)
def _close_open_stores():
    yield
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


def _store(db: Path) -> JobStore:
    store = JobStore(db)
    _OPEN_STORES.append(store)
    return store


# The schema exactly as it shipped through v0.5.1: created with `IF NOT
# EXISTS` by whichever build ran first, and never stamped with a version.
# Written out here rather than imported so this test keeps describing what
# is actually on the deployed box even if the module's baseline is edited.
_PRE_VERSIONING_SCHEMA = """
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

_HISTORY = [
    ("SVTPLAY-aaaaaaaaaaaa", "KZmQ5JY", "Skavlan - S15E01 - WEBDL-1080p",
     "WEBDL-1080p", "Completed", 1_400_000_000, 1_400_000_000,
     "/downloads/completed/Skavlan - S15E01 - WEBDL-1080p.mkv", None,
     "2026-01-02 03:04:05"),
    ("SVTPLAY-bbbbbbbbbbbb", "abc1234", "Uppdrag granskning - S24E03 - WEBDL-1080p",
     "WEBDL-1080p", "Failed", 900_000_000, 12_345, None,
     "svtplay-dl failed", "2026-02-03 04:05:06"),
    ("SVTPLAY-cccccccccccc", "def5678", "Vetenskapens värld - S37E02 - WEBDL-720p",
     "WEBDL-720p", "Queued", 700_000_000, 0, None, None,
     "2026-03-04 05:06:07"),
]


def _pre_versioning_database(db: Path, rows=_HISTORY) -> None:
    """A database exactly as an installation running v0.5.1 has it."""
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_PRE_VERSIONING_SCHEMA)
        conn.executemany(
            "INSERT INTO jobs (nzo_id, svt_id, stem, quality, status, size_bytes,"
            " downloaded_bytes, storage_path, fail_message, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()


def _raw(db: Path, sql: str, *args):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _version(db: Path) -> int:
    return _raw(db, "PRAGMA user_version")[0][0]


def _rows(db: Path) -> list[tuple]:
    return [tuple(r) for r in _raw(db, "SELECT * FROM jobs ORDER BY nzo_id")]


def _indexes(db: Path) -> set[str]:
    return {
        r[0]
        for r in _raw(
            db, "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
        if r[0] is not None
    }


@pytest.fixture
def extra_migrations(monkeypatch):
    """Install migrations on top of the real ones, for this test only.

    The runner has to be provable on migrations that do not exist yet --
    including one that fails -- and the real table only ever grows, so the
    alternative would be shipping a broken migration to have something to
    test with.
    """

    def install(by_version: dict):
        migrations = dict(store_module._MIGRATIONS)
        migrations.update(by_version)
        monkeypatch.setattr(store_module, "_MIGRATIONS", migrations)
        monkeypatch.setattr(store_module, "SCHEMA_VERSION", max(migrations))

    return install


# --- A fresh install --------------------------------------------------


def test_a_new_database_is_created_at_the_current_schema_version(tmp_path: Path):
    db = tmp_path / "jobs.db"
    s = _store(db)
    s.create("a", "stem", "WEBDL-1080p", 1)

    assert _version(db) == store_module.SCHEMA_VERSION


def test_the_migration_runner_runs_on_a_fresh_install_too(tmp_path: Path):
    # A migration runner nothing has ever run is not a migration runner. The
    # baseline deliberately stops at version 1 and every later change --
    # including the index below -- is applied by the same code path on a
    # brand new database as on the deployed one, so the path that matters
    # for an upgrade is exercised by every install and every test run.
    assert "CREATE INDEX" not in store_module._BASELINE_SCHEMA.upper()

    db = tmp_path / "jobs.db"
    _store(db)

    assert "idx_jobs_created_at" in _indexes(db)


# --- An installation that already has history -------------------------


def test_an_unversioned_database_is_adopted_at_the_baseline(tmp_path: Path):
    # The whole point. user_version 0 means either "never seen before" or
    # "written by every build before this one"; only the presence of the
    # table can tell them apart, and getting it wrong on the second means
    # recreating a table that holds someone's download history.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    before = _rows(db)

    s = _store(db)

    assert _version(db) == store_module.SCHEMA_VERSION
    assert _rows(db) == before


def test_an_unversioned_database_keeps_every_job_readable(tmp_path: Path):
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)

    s = _store(db)
    jobs = {j.nzo_id: j for j in s.all_jobs()}

    assert len(jobs) == len(_HISTORY)
    done = jobs["SVTPLAY-aaaaaaaaaaaa"]
    assert done.status is JobStatus.COMPLETED
    assert done.storage_path.endswith("Skavlan - S15E01 - WEBDL-1080p.mkv")
    assert done.created_at == "2026-01-02 03:04:05"
    failed = jobs["SVTPLAY-bbbbbbbbbbbb"]
    assert failed.status is JobStatus.FAILED
    assert failed.fail_message == "svtplay-dl failed"
    # And the queue Sonarr polls still has the job that was in it.
    assert [j.nzo_id for j in s.queued()] == ["SVTPLAY-cccccccccccc"]


def test_an_unversioned_database_is_still_writable_after_adoption(tmp_path: Path):
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)

    s = _store(db)
    fresh = s.create("new", "New - S01E01 - WEBDL-1080p", "WEBDL-1080p", 5)
    s.complete(fresh.nzo_id, "/downloads/completed/New - S01E01 - WEBDL-1080p.mkv")

    assert s.get(fresh.nzo_id).status is JobStatus.COMPLETED
    assert len(s.all_jobs()) == len(_HISTORY) + 1


def test_reopening_an_up_to_date_database_changes_nothing(tmp_path: Path):
    db = tmp_path / "jobs.db"
    first = _store(db)
    first.create("a", "stem", "WEBDL-1080p", 1)
    first.close()
    before = _rows(db)
    before_version = _version(db)

    _store(db)

    assert _version(db) == before_version
    assert _rows(db) == before


def test_an_empty_file_is_treated_as_a_new_database(tmp_path: Path):
    # `touch`ed by an installer, or left behind by a disk that filled up
    # mid-create. There is no table to adopt, so the baseline is created.
    db = tmp_path / "jobs.db"
    db.touch()

    s = _store(db)
    job = s.create("a", "stem", "WEBDL-1080p", 1)

    assert s.get(job.nzo_id) is not None
    assert _version(db) == store_module.SCHEMA_VERSION


# --- A migration that fails -------------------------------------------


def test_a_failing_migration_leaves_the_database_exactly_as_it_was(
    tmp_path: Path, extra_migrations
):
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    _store(db).close()
    before_rows = _rows(db)
    before_version = _version(db)
    before_indexes = _indexes(db)

    def half_way(conn):
        # A realistic failure: the schema change lands, the backfill that
        # goes with it does not. Both must be rolled back together or the
        # column exists with nothing in it and the next start migrates from
        # a state no version describes.
        conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        conn.execute("DELETE FROM jobs")
        raise RuntimeError("backfill blew up")

    extra_migrations({before_version + 1: half_way})

    with pytest.raises(JobStoreError) as exc:
        JobStore(db)

    assert "rolled back" in str(exc.value)
    assert _version(db) == before_version, "a failed migration must not stamp"
    assert _rows(db) == before_rows, "a failed migration must not lose rows"
    assert _indexes(db) == before_indexes, "a failed migration must not add DDL"


def test_a_later_migration_failing_keeps_the_earlier_one(
    tmp_path: Path, extra_migrations
):
    # Migrations are individually transactional, not collectively: one that
    # already committed stays committed, and the version stamp says exactly
    # how far the database got so the next start resumes from there.
    db = tmp_path / "jobs.db"
    base = store_module.SCHEMA_VERSION

    def adds_a_column(conn):
        conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")

    def explodes(conn):
        conn.execute("ALTER TABLE jobs ADD COLUMN nonsense TEXT")
        raise RuntimeError("no")

    _store(db).close()  # create at the real current version first
    extra_migrations({base + 1: adds_a_column, base + 2: explodes})

    with pytest.raises(JobStoreError):
        JobStore(db)

    assert _version(db) == base + 1
    columns = {r[1] for r in _raw(db, "PRAGMA table_info(jobs)")}
    assert "attempts" in columns
    assert "nonsense" not in columns


def test_migrations_are_applied_in_ascending_order(tmp_path: Path, extra_migrations):
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    order: list[int] = []

    extra_migrations({
        store_module.SCHEMA_VERSION + 2: lambda conn: order.append(2),
        store_module.SCHEMA_VERSION + 1: lambda conn: order.append(1),
    })

    _store(db)

    assert order == [1, 2]


# --- A database from the future ---------------------------------------


def test_a_newer_database_is_refused_with_a_clear_message(tmp_path: Path):
    db = tmp_path / "jobs.db"
    _store(db).close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    with pytest.raises(JobStoreError) as exc:
        JobStore(db)

    message = str(exc.value)
    # It must name both numbers and say what to do. An operator meeting
    # this has downgraded the service, usually by reinstalling an older
    # release, and the message is the only thing that will tell them so.
    assert "99" in message
    assert str(store_module.SCHEMA_VERSION) in message
    assert "newer release" in message
    assert "nothing has been changed" in message


def test_a_newer_database_keeps_every_row(tmp_path: Path):
    # A refusal that lost data would be worse than the corruption it
    # prevents: reinstalling the newer release has to recover the
    # installation exactly as it was.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    _store(db).close()
    before = _rows(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    with pytest.raises(JobStoreError):
        JobStore(db)

    assert _rows(db) == before
    assert _version(db) == 99, "the refusal must not restamp the database"


def test_a_refused_database_leaves_no_store_behind(tmp_path: Path):
    # The store must not be usable at all after a refusal -- a half-open
    # store would be a store writing rows in a shape the newer release
    # cannot read. `JobStore()` raising is what guarantees that, and the
    # suite's own leak guard is what would catch a connection left open.
    db = tmp_path / "jobs.db"
    _store(db).close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    with pytest.raises(JobStoreError):
        JobStore(db)


# --- Migrating while something else is reading ------------------------


def test_readers_during_a_migration_never_see_a_half_migrated_database(
    tmp_path: Path, extra_migrations
):
    # The service restarts while something else -- a second process, a
    # `sqlite3` shell, an operator's backup script -- is reading the
    # database. Under WAL the reader keeps its snapshot for the length of
    # the migration and sees the new state only once it commits, so there
    # is no moment at which a reader can observe the table part-changed.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    _store(db).close()
    expected = len(_HISTORY)

    started = threading.Event()
    finish = threading.Event()
    seen: list[int] = []
    errors: list[Exception] = []

    def slow(conn):
        conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        started.set()
        finish.wait(timeout=5)

    extra_migrations({store_module.SCHEMA_VERSION + 1: slow})

    def read():
        deadline = time.monotonic() + 5
        while not finish.is_set() and time.monotonic() < deadline:
            try:
                rows = _raw(db, "SELECT nzo_id, status, stem FROM jobs")
                seen.append(len(rows))
                for row in rows:
                    if row[1] is None or row[2] is None:
                        raise AssertionError(f"torn row during migration: {row!r}")
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        migrator = threading.Thread(target=lambda: _store(db))
        migrator.start()
        assert started.wait(timeout=5), "the migration never started"
        # Let the reader run for a moment while the migration is open.
        time.sleep(0.05)
    finally:
        finish.set()
        migrator.join(timeout=5)
        reader.join(timeout=5)

    assert errors == []
    assert seen, "the reader never got a read in"
    assert set(seen) == {expected}, seen
    assert _version(db) == store_module.SCHEMA_VERSION


def test_two_stores_opening_the_same_database_at_once_migrate_it_once(
    tmp_path: Path,
):
    # Not the deployed shape -- one service owns the file -- but a restart
    # can overlap with the process it replaces, and an installer script can
    # touch the database while the service is up. Whoever gets the write
    # lock second must notice the version already moved rather than run the
    # migration a second time.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    opened: list[JobStore] = []
    errors: list[Exception] = []

    def open_it():
        try:
            opened.append(JobStore(db))
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    for store in opened:
        _OPEN_STORES.append(store)

    assert errors == [], [repr(e) for e in errors]
    assert len(opened) == 4
    assert _version(db) == store_module.SCHEMA_VERSION
    assert len(_rows(db)) == len(_HISTORY)
