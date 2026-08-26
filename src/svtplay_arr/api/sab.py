"""SABnzbd-emulating surface.

Sonarr talks to this module as if it were a real SABnzbd instance: it POSTs
an `.nzb` via `mode=addfile`, polls `mode=queue` for progress, and reads
`mode=history` to learn whether a job finished (and where) or failed (and
why). This shape -- not a blackhole client -- was chosen deliberately for
three reasons, each with a matching requirement here:

- A genuine queue percentage in Sonarr's Activity tab during a multi-GB
  download: `mode=queue` must report real bytes, which means the `.nzb`'s
  declared size has to survive `addfile` into `JobStore.create` intact
  (dropping it would make every job report 0% forever).
- Working failure reporting, so a failed grab re-searches instead of
  stalling: `mode=history` must expose `fail_message`.
- Remote path mapping, which needs a hostname a blackhole client doesn't
  have: `mode=history` must expose the final `storage` path, and
  `mode=get_config` must expose a `categories` list -- Sonarr derives
  `OutputRootFolders` from the configured category and refuses to save the
  download client when that category is missing.

Removal deserves its own note because it is easy to get wrong: SABnzbd has
no `mode=delete`. Jobs are removed with `mode=queue&name=delete&value=NZO_ID`
and `mode=history&name=delete&value=NZO_ID` (both also accept a
comma-separated list, or `all`), which is exactly what Sonarr's
`SabnzbdProxy.RemoveFromQueue`/`RemoveFromHistory` send.

This module speaks only SABnzbd's wire format -- no download logic, no
matching logic, no SVT knowledge. `JobStore` is the source of truth; this
just shapes its rows into what Sonarr expects.

Every route below is `async def`. `JobStore` serialises access to one shared
`sqlite3.Connection` behind a blocking `threading.Lock`; FastAPI would run a
non-async route in a threadpool thread, and that thread holding the lock
would stall the event loop -- and with it the download worker -- for as long
as the lock is held. See `store.py` for the corruption this lock was added
to fix.
"""

import logging
from pathlib import Path

# defusedxml, not stdlib ElementTree: this parses an uploaded file arriving
# over HTTP, so it must not be vulnerable to entity-expansion/DTD attacks.
# _parse_nzb below catches both ElementTree.ParseError (plain malformed XML)
# and ValueError (str.decode on bad bytes, and defusedxml's own
# EntitiesForbidden/DTDForbidden/... attack-prevention exceptions, which are
# ValueError subclasses) and re-raises a single _NzbRejected.
from defusedxml import ElementTree as ET
from fastapi import APIRouter, File, Query, UploadFile

from svtplay_arr.models import JobStatus
from svtplay_arr.store import JobStoreError

log = logging.getLogger(__name__)

_NS = "{http://www.newzbin.com/DTD/2003/nzb}"
# Real NZBs for a single episode are a few hundred bytes of XML; this caps
# well above that while still bounding how much untrusted upload the parser
# ever sees.
_MAX_NZB_BYTES = 64 * 1024
_DEFAULT_QUALITY = "WEBDL-1080p"
_TERMINAL = (JobStatus.COMPLETED, JobStatus.FAILED)

# Sonarr looks up its configured category in get_config's `categories` and
# derives OutputRootFolders (and therefore its remote path mapping) from that
# entry's `dir`, joined onto `misc.complete_dir`. TestCategory() runs on every
# save of the download client, and with no matching category it fails
# validation outright -- so omitting this makes the client unsaveable, not
# merely suboptimal. `dir` is deliberately empty: an empty relative dir means
# the category's full path IS complete_dir, which is where the worker
# publishes. It must not end in "*" either; Sonarr reads a trailing "*" as
# "job folders disabled" and fails validation for that too.
_CATEGORIES = [{"name": "*", "dir": ""}, {"name": "tv", "dir": ""}]


class _NzbRejected(Exception):
    """The uploaded .nzb could not be turned into a job: too large,
    malformed, hostile, or missing the metadata a job needs."""


def build_sab_router(store, completed_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/sabnzbd")

    @router.get("/api")
    async def sab_get(
        mode: str = Query(...),
        name: str | None = None,
        value: str | None = None,
    ):
        if mode == "version":
            return {"version": "4.3.0"}
        if mode == "get_config":
            return _get_config(completed_dir)
        # Removal is a sub-action of queue/history, keyed by `name`, not a
        # mode of its own -- there is no `mode=delete` in SABnzbd. Declaring
        # `name` is what makes this reachable at all: without it, Sonarr's
        # `mode=queue&name=delete&value=X` fell through to the listing branch
        # below and answered with the queue while deleting nothing.
        if mode in ("queue", "history") and name == "delete":
            return _delete(store, mode, value)
        if mode == "queue":
            slots = _slots(store.all_active, _queue_slot)
            return {"queue": {"paused": False, "slots": slots}}
        if mode == "history":
            return {"history": {"slots": _slots(store.history, _history_slot)}}
        return {"status": False, "error": f"unsupported mode {mode!r}"}

    @router.post("/api")
    async def sab_post(mode: str = Query(...), name: UploadFile | None = File(None)):
        if mode != "addfile" or name is None:
            return {"status": False, "error": "unsupported"}
        try:
            raw = await name.read(_MAX_NZB_BYTES + 1)
            if len(raw) > _MAX_NZB_BYTES:
                raise _NzbRejected("nzb exceeds max upload size")
            meta = _parse_nzb(raw)
            job = store.create(
                svt_id=_require(meta, "svt_id"),
                stem=_require(meta, "stem"),
                quality=meta.get("quality") or _DEFAULT_QUALITY,
                size_bytes=_parse_size(meta.get("size")),
            )
        except _NzbRejected as exc:
            log.warning("addfile rejected %r: %s", name.filename, exc)
            return {"status": False, "error": str(exc)}
        except JobStoreError:
            # Never 500 to Sonarr: a 500 here can make it disable the
            # client. A failed add just means this grab silently doesn't
            # progress, which Sonarr's own stall detection can recover from.
            log.exception("addfile: could not create job for %r", name.filename)
            return {"status": False, "error": "could not queue job"}
        except Exception:
            # Catch-all at the router boundary: the never-500 constraint is
            # absolute, and reading an in-flight multipart upload can fail
            # for reasons that are neither malformed input nor a store
            # error -- e.g. the client disconnects mid-upload.
            log.exception("addfile: unexpected error handling %r", name.filename)
            return {"status": False, "error": "unexpected error"}
        return {"status": True, "nzo_ids": [job.nzo_id]}

    return router


def _get_config(completed_dir: Path) -> dict:
    return {
        "config": {
            "misc": {
                "complete_dir": str(completed_dir),
                "download_dir": str(completed_dir),
            },
            "categories": _CATEGORIES,
        }
    }


def _slots(source, to_slot) -> list[dict]:
    try:
        jobs = source()
    except JobStoreError:
        # A store read failure must degrade to an empty list, not a 500 --
        # Sonarr treats a 500 from its download client far worse than a
        # temporarily-empty queue/history.
        log.exception("%s failed; reporting empty", source.__qualname__)
        return []
    return [to_slot(j) for j in jobs]


def _delete(store, mode: str, value: str | None) -> dict:
    """Handle `mode=queue|history&name=delete&value=...`.

    `value` is one nzo_id, a comma-separated list of them, or the literal
    `all` -- all three are legal SABnzbd and all three are accepted here.

    The two modes do genuinely different things, matching SABnzbd:
    queue-delete terminates a job that is still running (Sonarr's remedy for
    a stuck queue item; with autoRedownloadFailed on, that is what triggers
    the re-search), while history-delete removes the finished row entirely
    (Sonarr's cleanup after failed-download handling).

    Both are filtered by `_delete_targets` to jobs that exist and are on the
    right side of the queue/history split, so a delete can neither invent a
    job nor flip an already-Completed one to Failed after its file has
    landed in completed/ and possibly been imported.
    """
    if not value:
        return {"status": False, "error": "missing value"}
    try:
        targets = _delete_targets(store, mode, value)
        for nzo_id in targets:
            if mode == "queue":
                store.fail(nzo_id, "removed from queue")
            else:
                store.delete(nzo_id)
    except JobStoreError:
        log.exception("%s delete failed for value %r", mode, value)
        return {"status": False, "error": "could not delete job"}
    # Deleting something that isn't there is not an error in SABnzbd, and
    # answering with an error would make Sonarr treat the download client as
    # broken over a job it had already given up on anyway.
    return {"status": True, "nzo_ids": targets}


def _delete_targets(store, mode: str, value: str) -> list[str]:
    want_terminal = mode == "history"
    if value.strip() == "all":
        source = store.history if want_terminal else store.all_active
        return [job.nzo_id for job in source()]

    targets: list[str] = []
    for nzo_id in (part.strip() for part in value.split(",")):
        if not nzo_id:
            continue
        job = store.get(nzo_id)
        if job is None:
            log.info("%s delete: no such job %r, ignoring", mode, nzo_id)
            continue
        if (job.status in _TERMINAL) is not want_terminal:
            # A queue-delete for a job that already finished, or a
            # history-delete for one still running. Either way this id is on
            # the other side of the queue/history split and must be left
            # alone -- failing a Completed job here would report a failed
            # download for a file already sitting in completed/.
            log.info(
                "%s delete: job %r is %s, ignoring",
                mode, nzo_id, job.status.value,
            )
            continue
        targets.append(nzo_id)
    return targets


def _require(meta: dict[str, str], key: str) -> str:
    value = meta.get(key)
    if not value:
        raise _NzbRejected(f"nzb missing required meta {key!r}")
    return value


def _parse_size(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    # A negative size is nonsensical and would flow straight into the
    # mb/mbleft math in _queue_slot. addfile parses untrusted input -- our
    # own newznab router never emits a negative size, but the guard is
    # cheap and this module is defensive elsewhere for the same reason.
    return value if value > 0 else 0


def _parse_nzb(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError) as exc:
        # ET.ParseError (a SyntaxError, not a ValueError) covers merely
        # malformed XML; ValueError covers UnicodeDecodeError and
        # defusedxml's own EntitiesForbidden/DTDForbidden/... attack-
        # prevention exceptions. Both collapse to one exception type
        # leaving this function.
        raise _NzbRejected("malformed or hostile nzb") from exc
    out: dict[str, str] = {}
    # Sonarr writes the namespaced form Task 10's newznab router emits;
    # tolerate the bare tag too since real-world SAB clients see both.
    for meta in list(root.iter(f"{_NS}meta")) + list(root.iter("meta")):
        attrib = meta.attrib
        if not isinstance(attrib, dict):
            continue
        type_ = attrib.get("type")
        if isinstance(type_, str):
            out.setdefault(type_, meta.text or "")
    return out


def _queue_slot(job) -> dict:
    # Clamped to 100: svtplay-dl's remux step writes the .ts and the
    # resulting .mkv to staging at the same time, so downloaded_bytes (which
    # the worker reports from bytes on disk) routinely exceeds the nzb's
    # advertised size_bytes mid-download. Unclamped, Sonarr's Activity tab
    # would show a percentage above 100.
    pct = 0 if not job.size_bytes else min(int(job.downloaded_bytes * 100 / job.size_bytes), 100)
    mb = job.size_bytes / 1_048_576
    mb_done = job.downloaded_bytes / 1_048_576
    return {
        "nzo_id": job.nzo_id,
        "filename": job.stem,
        "status": job.status.value,
        "percentage": str(pct),
        "mb": f"{mb:.2f}",
        "mbleft": f"{max(mb - mb_done, 0):.2f}",
        "timeleft": "0:00:00",
        "cat": "tv",
    }


def _history_slot(job) -> dict:
    return {
        "nzo_id": job.nzo_id,
        "name": job.stem,
        "status": job.status.value,
        "storage": job.storage_path or "",
        "fail_message": job.fail_message or "",
        "category": "tv",
        "bytes": job.size_bytes,
    }
