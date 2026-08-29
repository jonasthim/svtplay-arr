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

import hashlib
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
    tmp_path: Path, extra_migrations
):
    """Not the deployed shape -- one service owns the file -- but a restart
    can overlap with the process it replaces, and an installer script can
    touch the database while the service is up. Whoever gets the write lock
    second must notice the version already moved rather than run the
    migration a second time.

    The migration installed here is deliberately **not** idempotent, and
    that is the whole point. The only migration this build ships is
    `CREATE INDEX IF NOT EXISTS`, which survives being run twice by
    accident -- so with the real set, deleting the version re-read inside
    `_apply`'s transaction leaves the entire suite green. With a realistic
    next migration (`ALTER TABLE ... ADD COLUMN`, which is what any future
    one will be), removing that guard makes three of six opens fail to
    start *and* leaves the database half-migrated behind a version stamp
    that says otherwise -- so every subsequent start re-runs it, hits
    `duplicate column name`, and refuses. Unrecoverable without hand-editing
    sqlite, on what may be the only copy of that history.
    """
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    ran: list[int] = []
    ran_lock = threading.Lock()

    def adds_a_column(conn):
        with ran_lock:
            ran.append(1)
        conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")

    extra_migrations({store_module.SCHEMA_VERSION + 1: adds_a_column})

    opened: list[JobStore] = []
    errors: list[Exception] = []

    def open_it():
        try:
            opened.append(JobStore(db))
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    for store in opened:
        _OPEN_STORES.append(store)

    assert errors == [], [repr(e) for e in errors]
    assert len(opened) == 6
    assert ran == [1], f"the migration ran {len(ran)} times, not once"
    assert _version(db) == store_module.SCHEMA_VERSION
    assert len(_rows(db)) == len(_HISTORY)
    columns = [r[1] for r in _raw(db, "PRAGMA table_info(jobs)")]
    assert columns.count("attempts") == 1


def test_six_opens_of_an_already_adopted_database_migrate_it_once(
    tmp_path: Path, extra_migrations
):
    """`_apply`'s version re-read, where it is the *only* thing guarding.

    The sibling test above starts from an unstamped database, so
    `_adopt_baseline`'s own re-read is in the path too and deleting only
    `_apply`'s guard slips through about one run in twenty-five. This is
    the shape with no second net: a database that is already adopted, with
    a migration pending, opened six times at once. Deleting the guard fails
    five of the six opens here, every time.

    It is also the shape the deployed installation will actually be in for
    its *second* schema bump -- adopted long ago, one new migration to run.
    """
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    _store(db).close()
    base = store_module.SCHEMA_VERSION
    assert _version(db) == base, "the fixture must already be adopted"

    ran: list[int] = []
    ran_lock = threading.Lock()

    def adds_a_column(conn):
        with ran_lock:
            ran.append(1)
        conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")

    extra_migrations({base + 1: adds_a_column})

    opened: list[JobStore] = []
    errors: list[Exception] = []

    def open_it():
        try:
            opened.append(JobStore(db))
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    for store in opened:
        _OPEN_STORES.append(store)

    assert errors == [], [repr(e) for e in errors]
    assert len(opened) == 6
    assert ran == [1], f"the migration ran {len(ran)} times, not once"
    assert _version(db) == base + 1
    columns = [r[1] for r in _raw(db, "PRAGMA table_info(jobs)")]
    assert columns.count("attempts") == 1
    assert len(_rows(db)) == len(_HISTORY)
    # Six starts racing for one copy: exactly one is written, and the rest
    # recognise it rather than retaking or refusing.
    assert _copy_path(db, base).is_file()
    assert list(tmp_path.glob("*.partial")) == []


def test_adopting_a_database_another_process_already_moved_leaves_it_alone(
    tmp_path: Path,
):
    # The other re-read, in `_adopt_baseline`. The version is sampled before
    # the transaction takes the write lock, so by the time the lock is held
    # another process may have adopted the database *and* migrated it.
    # Without the re-read this stamps it back to the baseline, and every
    # migration since runs a second time on the next start -- which the
    # shipped `CREATE INDEX IF NOT EXISTS` survives and nothing else would.
    #
    # Driven directly rather than through a race, because "the other process
    # got further than we did" is not something two threads can be made to
    # do on demand.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    _store(db).close()
    assert _version(db) == store_module.SCHEMA_VERSION
    before = _rows(db)

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        returned = store_module._adopt_baseline(conn, db)
    finally:
        conn.close()

    assert returned == store_module.SCHEMA_VERSION, (
        "adoption reported the baseline for a database that had moved past it"
    )
    assert _version(db) == store_module.SCHEMA_VERSION
    assert _rows(db) == before


# --- What the operator sees -------------------------------------------


def test_adopting_an_existing_database_says_so_in_the_log(
    tmp_path: Path, caplog
):
    # This happens exactly once per installation, and it is the only moment
    # an operator can see that their job history was recognised rather than
    # replaced. A silent upgrade of a database is noticed only by whoever
    # loses one.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)

    with caplog.at_level("INFO", logger="svtplay_arr.store"):
        _store(db)

    messages = [r.getMessage() for r in caplog.records]
    assert any("adopted the jobs table already there" in m for m in messages), messages
    assert any(str(db) in m for m in messages), messages
    assert any(
        f"schema version {store_module.SCHEMA_VERSION}" in m for m in messages
    ), messages


def test_a_new_database_says_it_was_created(tmp_path: Path, caplog):
    db = tmp_path / "jobs.db"

    with caplog.at_level("INFO", logger="svtplay_arr.store"):
        _store(db)

    messages = [r.getMessage() for r in caplog.records]
    assert any("created a new jobs table" in m for m in messages), messages


def test_reopening_an_up_to_date_database_logs_nothing(tmp_path: Path, caplog):
    # Every restart after the first. A line on every start would train the
    # operator to ignore the one start where it matters.
    db = tmp_path / "jobs.db"
    _store(db).close()

    with caplog.at_level("INFO", logger="svtplay_arr.store"):
        _store(db)

    assert [r.getMessage() for r in caplog.records] == []


# --- The copy taken before the first migration ------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_path(db: Path, version: int) -> Path:
    return db.with_name(f"{db.name}.v{version}.bak")


def test_a_copy_is_taken_before_the_first_migration(tmp_path: Path):
    # Everything else here defends against a migration going *wrong*: each
    # is transactional and rolls back whole. This defends against one going
    # right and losing data anyway -- a migration that breaks the
    # additive-only convention, which no transaction can undo -- on a file
    # that is, for the installations this ships to, the only copy of that
    # history in existence. config.yaml has had a .bak on every write for
    # releases; the database holding the history had nothing.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)

    _store(db)

    copy = _copy_path(db, store_module._BASELINE_VERSION)
    assert copy.is_file(), f"no copy at {copy}"
    assert [tuple(r) for r in _raw(copy, "SELECT * FROM jobs ORDER BY nzo_id")] == [
        tuple(r) for r in _raw(db, "SELECT * FROM jobs ORDER BY nzo_id")
    ]


def test_the_copy_is_of_the_schema_it_is_named_for(tmp_path: Path):
    # Taken before the migration, so it is a database the *previous* build
    # can open -- which is the only thing that makes it useful to restore.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)

    _store(db)

    copy = _copy_path(db, store_module._BASELINE_VERSION)
    assert _version(copy) == store_module._BASELINE_VERSION
    assert _version(db) == store_module.SCHEMA_VERSION
    assert "idx_jobs_created_at" not in _indexes(copy)
    assert "idx_jobs_created_at" in _indexes(db)


def test_no_copy_is_taken_of_a_database_with_nothing_in_it(tmp_path: Path):
    # A fresh install has nothing to lose, and a .bak beside every new
    # install is clutter that teaches the operator to ignore the ones that
    # matter.
    db = tmp_path / "jobs.db"

    _store(db)

    assert list(tmp_path.glob("*.bak")) == []


def test_an_existing_copy_is_kept_rather_than_overwritten(
    tmp_path: Path, extra_migrations
):
    # A failed migration leaves the database at the version it started at,
    # so the next start would take a second copy of the same state -- over
    # the top of the one that describes it. The first copy is the one to
    # keep.
    def refuses(conn):
        raise RuntimeError("not today")

    # The first pending migration fails, so the database stays at the
    # version it started at and the next start takes the same copy path
    # again. This is the only shape where that happens, and it is the shape
    # an operator retrying a failed upgrade is in.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    extra_migrations({store_module._BASELINE_VERSION + 1: refuses})

    with pytest.raises(JobStoreError):
        JobStore(db)
    copy = _copy_path(db, store_module._BASELINE_VERSION)
    assert copy.is_file()
    before = _sha256(copy)
    # Something only the first copy has, so an overwrite is detectable even
    # if the two copies would otherwise be byte-identical.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()

    with pytest.raises(JobStoreError):
        JobStore(db)

    assert _sha256(copy) == before, "the first copy was overwritten"
    assert [tuple(r) for r in _raw(copy, "SELECT * FROM jobs")], (
        "the kept copy must still hold the rows the database has since lost"
    )


def test_a_copy_that_cannot_be_written_stops_the_start(
    tmp_path: Path, monkeypatch
):
    # The one promise this project has made about the job database is that
    # history is not lost. Migrating without the copy that exists to make
    # that promise good is not a trade to take quietly -- and nothing has
    # been changed at the point this refuses, so the message can say so.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    before = _rows(db)

    def full_disk(conn, target):
        raise store_module.JobStoreError(
            f"could not write a copy of the job database to {target} before"
            " migrating it (disk full); nothing has been changed."
        )

    monkeypatch.setattr(store_module, "_vacuum_into", full_disk)

    with pytest.raises(JobStoreError) as exc:
        JobStore(db)

    message = str(exc.value)
    assert "copy of the job database" in message
    assert "nothing has been changed" in message
    # Not byte-identical: adoption stamps the version before the copy is
    # attempted, and adoption touches no row. The rows are what the promise
    # is about, and they are all still here.
    assert _rows(db) == before
    assert _version(db) == store_module._BASELINE_VERSION


def test_a_copy_that_failed_leaves_nothing_that_looks_like_one(tmp_path: Path):
    """The scenario the copy exists for, all the way through.

    The disk fills part way into `VACUUM INTO`. The first start must refuse
    and change nothing -- and then the *second* start must not find the
    wreckage of the first, decide a copy already exists, and migrate
    unprotected while saying so in the log. Reproduced against an earlier
    version of this code on 6003 rows of real history: the surviving file
    answered `no such table: jobs`.

    `RLIMIT_FSIZE` rather than a stubbed-out failure, because the property
    is about what a genuinely half-written copy leaves on disk.
    """
    import resource
    import subprocess
    import sys
    import textwrap

    db = tmp_path / "jobs.db"
    _pre_versioning_database(db, rows=[
        (f"SVTPLAY-{i:012d}", f"svt{i}", f"Show {i} - S01E01 - WEBDL-1080p",
         "WEBDL-1080p", "Completed", 1000 + i, 1000 + i, f"/c/{i}.mkv", None,
         "2026-01-02 03:04:05")
        for i in range(4000)
    ])
    del resource  # the limit is applied in the child, below

    script = textwrap.dedent(
        f"""
        import resource, sys
        from pathlib import Path
        # Small enough that the copy of a 4000-row database cannot finish,
        # large enough that the database itself still opens and migrates.
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024, 64 * 1024))
        from svtplay_arr.store import JobStore, JobStoreError
        try:
            JobStore(Path({str(db)!r}))
        except JobStoreError as exc:
            print("refused:", exc)
            sys.exit(0)
        print("started anyway")
        sys.exit(1)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The copy specifically, not some other failure the file-size limit
    # happened to cause first.
    assert "could not write a copy of the job database" in proc.stdout, proc.stdout

    # Nothing that a later start could mistake for a copy.
    copy = _copy_path(db, store_module._BASELINE_VERSION)
    assert not copy.exists(), f"a half-written copy was left at {copy}"

    # ...and the retry, with room this time, takes a real one and migrates.
    _store(db)

    assert copy.is_file()
    assert _version(copy) == store_module._BASELINE_VERSION
    assert len(_raw(copy, "SELECT nzo_id FROM jobs")) == 4000
    assert _version(db) == store_module.SCHEMA_VERSION


@pytest.mark.parametrize(
    "name,make",
    [
        ("truncated", lambda p, src: p.write_bytes(src.read_bytes()[:512])),
        ("empty", lambda p, src: p.write_bytes(b"")),
        ("not a database", lambda p, src: p.write_bytes(b"nope" * 400)),
    ],
)
def test_a_file_that_is_not_a_database_is_never_taken_for_a_copy(
    tmp_path: Path, name: str, make
):
    # Each of these was accepted by `is_file()` and reported as "keeping
    # it". sqlite treats all three differently on its own -- a zero-length
    # file it silently overwrites, non-database bytes it calls "file is not
    # a database" -- so relying on `VACUUM INTO`'s own guard would not have
    # covered them either.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    copy = _copy_path(db, store_module._BASELINE_VERSION)
    make(copy, db)
    before = copy.read_bytes()

    with pytest.raises(JobStoreError) as exc:
        JobStore(db)

    assert "not a usable copy" in str(exc.value), str(exc.value)
    assert copy.read_bytes() == before, "the file in the way was overwritten"
    assert _version(db) == store_module._BASELINE_VERSION, "it migrated anyway"


def test_a_symlink_at_the_copy_path_is_neither_followed_nor_overwritten(
    tmp_path: Path,
):
    # `is_file()` follows a symlink, so the "copy" could be a file belonging
    # to something else -- and the keep-it branch would then leave the
    # migration unprotected while pointing at someone else's database.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    elsewhere = tmp_path / "somebody-elses.db"
    _pre_versioning_database(elsewhere)
    conn = sqlite3.connect(elsewhere)
    conn.execute(f"PRAGMA user_version = {store_module._BASELINE_VERSION}")
    conn.close()
    before = elsewhere.read_bytes()
    _copy_path(db, store_module._BASELINE_VERSION).symlink_to(elsewhere)

    with pytest.raises(JobStoreError) as exc:
        JobStore(db)

    assert "not a usable copy" in str(exc.value)
    assert elsewhere.read_bytes() == before


def test_a_copy_of_a_different_schema_version_is_not_kept(tmp_path: Path):
    # A real database, but not of the version this name claims. Restoring it
    # would hand the older build a schema it does not understand.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    copy = _copy_path(db, store_module._BASELINE_VERSION)
    _pre_versioning_database(copy)
    conn = sqlite3.connect(copy)
    conn.execute("PRAGMA user_version = 97")
    conn.close()

    with pytest.raises(JobStoreError) as exc:
        JobStore(db)

    assert "not a usable copy" in str(exc.value)


def test_no_partial_copy_is_left_behind_by_a_successful_one(tmp_path: Path):
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)

    _store(db)

    assert list(tmp_path.glob("*.partial")) == []
    assert _copy_path(db, store_module._BASELINE_VERSION).is_file()


# --- Refusing a newer database changes nothing ------------------------


def test_refusing_a_newer_database_does_not_change_one_byte(tmp_path: Path):
    # The refusal message and docs/configuration.md both promise "nothing
    # has been changed", and an operator who reads that is about to restore
    # or downgrade on the strength of it. Enabling WAL rewrites the database
    # header, so doing that before the version check made the promise false
    # for exactly the databases most likely to meet it: one written by a
    # newer release, on a host that had never opened it in WAL.
    db = tmp_path / "jobs.db"
    _pre_versioning_database(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    assert _raw(db, "PRAGMA journal_mode")[0][0] != "wal", (
        "the fixture must not already be in WAL, or this proves nothing"
    )
    before = _sha256(db)

    with pytest.raises(JobStoreError):
        JobStore(db)

    assert _sha256(db) == before
    assert list(tmp_path.glob("*.bak")) == []
