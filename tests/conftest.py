"""Suite-wide guards.

Currently one: no test may leave a `JobStore` open.

`create_app` opens the job store immediately, before any lifespan runs, and
the lifespan is what closes it. A test that builds an app and never starts a
`TestClient` therefore leaks a `sqlite3.Connection`, whose finalizer raises
during whatever garbage collection happens to reach it. pytest reports that
as a `PytestUnraisableExceptionWarning` -- which under `-W error` fails the
session, attributed to no test in particular, and only once the suite has
grown enough for the collection to happen before the run ends.

That is exactly the kind of failure this project refuses elsewhere: silent
until it is not, and then blamed on the wrong thing. One such leak sat in
`test_config.py` for the whole life of the canary feature and surfaced only
when unrelated tests were added around it. This turns it into a named
failure in the test that caused it.
"""

import traceback

import pytest

from svtplay_arr import store as _store

_LEAK_HINT_FRAMES = 6

_open: dict[int, str] = {}
_orig_init = _store.JobStore.__init__
_orig_close = _store.JobStore.close


def _tracked_init(self, db_path):
    _orig_init(self, db_path)
    # Where it was opened, so the failure below names the line to fix
    # rather than only the test that noticed.
    _open[id(self)] = "".join(traceback.format_stack()[-_LEAK_HINT_FRAMES:-1])


def _tracked_close(self):
    _open.pop(id(self), None)
    return _orig_close(self)


_store.JobStore.__init__ = _tracked_init
_store.JobStore.close = _tracked_close


@pytest.fixture(autouse=True)
def _no_leaked_job_stores(request):
    """Fail a test that opens a job store and does not close it.

    Scoped to stores opened *during* this test, so a fixture holding one
    open across several is not reported by each of them in turn.
    """
    before = set(_open)
    yield
    leaked = set(_open) - before
    for key in leaked:
        # Dropped from the registry either way: this has already been
        # reported once, and leaving it in would make every later test in
        # the session fail for someone else's store.
        where = _open.pop(key)
    if leaked:
        pytest.fail(
            f"{request.node.nodeid} left {len(leaked)} JobStore(s) open. "
            "The sqlite connection is then closed by a finalizer, which "
            "raises during a later garbage collection and fails the session "
            "under -W error, blaming whichever test triggered it. Close it "
            "explicitly, or drive the app through `with TestClient(app)` so "
            "the lifespan does.\nOpened at:\n" + where
        )
