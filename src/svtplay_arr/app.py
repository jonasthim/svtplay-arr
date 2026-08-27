"""Composition root: wires config, clients, store and worker into one app.

This module only assembles the pieces documented in each of their own
modules -- it does not itself decide how a release is matched, how a job is
downloaded, or how the Newznab/SAB wire formats look. If you find yourself
adding that kind of logic here, it belongs in one of those modules instead.

`create_app` refuses to start when `incomplete_dir` and `completed_dir`
overlap: startup clears `incomplete_dir`, so a `completed_dir` inside it
would be deleted on every restart.

`/health` exists specifically to surface `Settings.dirs_share_filesystem()`.
`worker.py` publishes a finished download into `completed_dir` via
`Path.rename`, which is atomic only within one filesystem; across
filesystems it silently degrades to copy-then-delete and Sonarr can import a
half-copied file as a permanent, corrupt library entry. `/health` is the
only thing in the service that checks for that split before it bites.

It carries the mapping table's state for the same reason. An invalid
mappings.yaml used to raise inside `create_app`, so uvicorn exited and
`Restart=on-failure` made it loud; `ReloadingMappingTable` now degrades to
the last known-good table instead, which is right for the feed but leaves
the service up and quietly stale. `/health` is where that becomes visible.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from svtplay_arr.api.config_ui import build_config_router
from svtplay_arr.api.newznab import build_newznab_router
from svtplay_arr.api.sab import build_sab_router
from svtplay_arr.canary import SvtCanary, unavailable_status
from svtplay_arr.config import Settings
from svtplay_arr.downloader import SvtplayDlDownloader
from svtplay_arr.mappings import ReloadingMappingTable
from svtplay_arr.resolver import Resolver
from svtplay_arr.sonarr import SonarrClient
from svtplay_arr.store import JobStore, JobStoreError
from svtplay_arr.svt.client import SvtClient
from svtplay_arr.worker import Worker

log = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO)

_DEFAULT_CONFIG_PATH = "/etc/svtplay-arr/config.yaml"


def create_app(settings: Settings) -> FastAPI:
    # Before anything is opened or created: startup clears incomplete_dir, so
    # a completed_dir nested inside it (or equal to it) would lose the
    # library on every restart. Nothing downstream can recover from that, so
    # it must stop the app coming up at all.
    settings.ensure_download_dirs_are_disjoint()
    http = httpx.AsyncClient(timeout=30.0)
    store = JobStore(settings.db_path)
    # Hoisted to named locals so the resolver and the config router share
    # one instance each -- two clients would mean two connection pools for
    # no reason.
    sonarr_client = SonarrClient(settings.sonarr_url, settings.sonarr_api_key, http)
    svt_client = SvtClient(http, settings.svt_ua)
    # Named so /health can report on the very table the resolver serves
    # from, rather than a second one loaded for the occasion.
    mapping_table = ReloadingMappingTable(settings.mappings_file)
    resolver = Resolver(
        mapping_table,
        sonarr_client,
        svt_client,
        settings.air_date_tolerance_days,
    )
    worker = Worker(
        store,
        SvtplayDlDownloader(),
        settings.incomplete_dir,
        settings.completed_dir,
        settings.max_concurrent_downloads,
    )
    # The one thing in this service that knows whether SVT is still there.
    # Handed `mapping_table.all` -- the live table the resolver serves from,
    # not a second copy -- so it checks exactly the rows the feed offers,
    # and picks up a mapping added through the config page on its next round
    # with no restart. It calls `SvtClient.list_episodes` and nothing else;
    # see canary.py for why it is the operator's own mappings rather than a
    # hardcoded show, and why zero episodes counts as a failure.
    #
    # The interval is floored at a minute: config.yaml is hand-editable, and
    # `svt_canary_interval_minutes: 0` would otherwise become a loop firing
    # at SVT's unofficial API as fast as it can answer.
    canary = SvtCanary(
        mapping_table.all,
        svt_client,
        interval_s=max(1, settings.svt_canary_interval_minutes) * 60.0,
    )

    # Set by the lifespan below once the background tasks are created, and
    # read by /health via `nonlocal`. Plain module-level variables would leak
    # state across the multiple apps a single test session creates; closures
    # over locals keep each create_app() call's health check scoped to its
    # own worker and its own canary.
    worker_task: asyncio.Task | None = None
    canary_task: asyncio.Task | None = None

    def compute_health() -> dict:
        """The one computation behind both `/health` and the config page's
        status strip.

        This project has repeatedly been bitten by two places computing the
        same fact and drifting apart -- it is the single most common defect
        class on this branch. A status strip that disagreed with `/health`
        would be worse than no strip at all, because the operator would
        trust the one in front of them. So there is exactly one place this
        is computed; `/health` and `build_config_router`'s status provider
        both call it and return/render its result verbatim rather than each
        recomputing it.

        A monitoring endpoint -- and, by the same argument, the page an
        operator is staring at -- must never be able to crash the app or the
        worker it reports on: active_jobs comes from the same JobStore the
        worker itself relies on, so a transient store error here is
        reported as a degraded-but-200 response rather than surfaced as a
        500 -- this is Sonarr-facing infrastructure too (some Sonarr
        health-check setups poll `/health`), and the "never emit a 500
        where a degraded response will do" rule applies here as much as it
        does to the Newznab/SAB routes.
        """
        worker_alive = worker_task is not None and not worker_task.done()
        try:
            active_jobs = len(store.all_active())
        except Exception:
            log.exception("/health: could not read active jobs")
            active_jobs = None

        # Before the config page existed, an invalid mappings.yaml raised
        # inside create_app, uvicorn exited, and Restart=on-failure made it
        # loud. ReloadingMappingTable now degrades to the last known-good
        # table instead -- right for the feed, but it leaves the service up
        # and serving a stale table. A service that is up, reporting
        # healthy, and grabbing nothing is this project's named failure
        # mode, and surfacing exactly that is why /health exists.
        try:
            mappings = mapping_table.status()
        except Exception:
            log.exception("/health: could not read the mapping table's status")
            mappings = {"ever_loaded": False, "degraded": True, "count": None}

        # The one thing here that is about the world outside this process.
        # Everything above reports on the service itself, which is exactly
        # why an SVT format change could empty the feed while every field
        # above stayed green -- see canary.py. `alive` is folded in on the
        # same precedent as `worker_alive`: a monitoring task that silently
        # stopped monitoring must not look like one that is working, or the
        # canary becomes a second silence rather than the end of the first.
        try:
            svt = canary.status()
        except Exception:
            log.exception("/health: could not read the SVT canary's status")
            svt = unavailable_status()
        svt["alive"] = canary_task is not None and not canary_task.done()
        if not svt["alive"]:
            svt["degraded"] = True

        same_fs = settings.dirs_share_filesystem()
        status = (
            "ok"
            if (
                same_fs
                and worker_alive
                and not mappings["degraded"]
                and not svt["degraded"]
            )
            else "degraded"
        )
        return {
            "status": status,
            "same_filesystem": same_fs,
            "worker_alive": worker_alive,
            "active_jobs": active_jobs,
            # Is SVT still there, does the parser still work, and do the
            # operator's own mappings still resolve? Added, never folded
            # into an existing field: Sonarr health-check setups may poll
            # this endpoint, so every key above keeps its name and type.
            "svt": svt,
            # How many series the feed is currently offering. Zero is not
            # itself reported as degraded -- a fresh install legitimately
            # has no mappings yet -- but it is the number to look at when
            # Sonarr is rejecting the indexer.
            "mappings": mappings["count"],
            "mappings_ever_loaded": mappings["ever_loaded"],
            # True means the file on disk failed to load and "mappings"
            # above describes the last known-good table serving in its
            # place. Fix the file; the feed is unaffected until then.
            "mappings_degraded": mappings["degraded"],
        }

    async def _stop(task: asyncio.Task, what: str) -> None:
        """Cancel a background task and await it, whatever state it is in.

        Shared by the worker and the canary so their shutdowns cannot drift.
        A task may already have died from an unrelated exception before
        shutdown began (see /health's `worker_alive` and `svt.alive` checks
        above) -- cancel() is then a no-op and awaiting it re-raises that
        original failure. It was already logged where it happened; shutdown
        must still proceed and close the HTTP client regardless.
        """
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("%s task had already failed at shutdown", what)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal worker_task, canary_task
        tasks: list[tuple[asyncio.Task, str]] = []
        try:
            # Must run before any job is dispatched: this is what removes
            # anything a crash left behind in incomplete/ so a stale partial
            # can never be mistaken for a fresh download in progress. Its
            # own ignore_errors=True/missing_ok=True guards don't cover
            # iterdir() itself, so a real OSError (e.g. a permissions
            # problem) can still escape here -- this must not leak `http`
            # unclosed if it does.
            worker.sweep_incomplete()
            worker_task = asyncio.create_task(worker.run_forever())
            tasks.append((worker_task, "worker"))
            # Started after the worker and stopped alongside it. It drives
            # the same `http` client the routes do, so it must be cancelled
            # and awaited before that client is closed below -- otherwise a
            # round in flight at shutdown would raise into a closed client.
            canary_task = asyncio.create_task(canary.run_forever())
            tasks.append((canary_task, "SVT canary"))
            app.state.svt_canary_task = canary_task
        except Exception:
            # Both of these were opened by create_app, before this lifespan
            # was entered, so nothing else will ever release them if startup
            # dies here. Anything already started is stopped first, so a
            # failure part-way through startup cannot leave a task running
            # against resources this is about to close.
            for task, what in tasks:
                await _stop(task, what)
            await http.aclose()
            store.close()
            raise
        try:
            yield
        finally:
            for task, what in tasks:
                await _stop(task, what)
            await http.aclose()
            # Last, and deliberately after the worker task has been both
            # cancelled and awaited above: the worker writes job progress
            # through this store from its own task, and closing it while
            # that task was still running would turn its next write into a
            # JobStoreError. By here nothing holds it -- the routes stopped
            # being served before shutdown began -- so this is the one point
            # at which it is safe. It is the app's own store, opened in
            # create_app and never handed out, so closing it here cannot
            # surprise another owner.
            try:
                store.close()
            except JobStoreError:
                # Shutdown must not be able to fail loudly over a database
                # that is being discarded anyway.
                log.exception("could not close the job store at shutdown")

    app = FastAPI(lifespan=lifespan)
    # Exposed for diagnostics and so tests can observe the live mapping
    # table the resolver actually reads from, rather than a copy -- e.g.
    # confirming a mapping added on disk is visible through the resolver
    # itself, with no restart, as distinct from the config page's own
    # (separately loaded) view of mappings.yaml.
    app.state.resolver = resolver
    # Exposed for the same reason: the lifespan closes this store at
    # shutdown, and that is only observable from outside if the very store
    # the app uses is reachable.
    app.state.job_store = store
    # Exposed so a test can drive one round of the *app's own* canary --
    # the same object /health reports on, not a second one built for the
    # occasion -- rather than waiting out its startup delay and then an
    # hour. `app.state.svt_canary_task` is set by the lifespan above, and is
    # what makes "the canary task died" observable from outside.
    app.state.svt_canary = canary
    app.state.svt_canary_task = None
    # The mapping table is passed alongside the resolver (which reads the
    # same instance) because the Newznab module needs `series_title` for
    # its `q` filter and must not reach into the resolver's internals.
    app.include_router(
        build_newznab_router(resolver, settings.rss_window_days, mapping_table)
    )
    app.include_router(build_sab_router(store, settings.completed_dir))
    app.include_router(
        build_config_router(
            config_path=settings.config_path,
            mappings_path=settings.mappings_file,
            svt=svt_client,
            sonarr=sonarr_client,
            # The Settings the service actually booted with. Settings need
            # a restart, so this is the only thing that can tell the page
            # whether what is on disk is what is running -- without it a
            # GET after a save shows the new values with nothing to say the
            # service is still using the old ones.
            booted=settings,
            # A zero-argument callable returning the same structure
            # `/health` returns, so the config UI module never needs to
            # import Worker, JobStore or Settings -- see compute_health's
            # docstring above for why this is the *only* place that
            # computes these facts.
            status_provider=compute_health,
        )
    )

    @app.get("/health")
    async def health():
        return compute_health()

    return app


def create_app_from_env() -> FastAPI:
    """Entry point for `uvicorn --factory svtplay_arr.app:create_app_from_env`.

    Config path comes from SVTPLAY_ARR_CONFIG, defaulting to the packaged
    location used by the systemd unit in deploy/.
    """
    config_path = Path(os.environ.get("SVTPLAY_ARR_CONFIG", _DEFAULT_CONFIG_PATH))
    return create_app(Settings.load(config_path))
