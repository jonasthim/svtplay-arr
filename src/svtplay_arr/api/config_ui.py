"""The configuration page.

Contains no matching logic and no SVT knowledge: it calls SvtClient and
SonarrClient the same way `suggest_mappings` does. That seam is what makes
it impossible for a UI change to alter what gets grabbed. The per-mapping
Check control (`_check_slug`, `_check_context`, `check_mapping`) is the one
place this module makes a live SVT call, and it does so the same way
`Resolver` does -- `svt.list_episodes(slug)` -- strictly on demand, never on
a page render, and never writing anything.
"""

import logging
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from svtplay_arr.config import (
    DANGEROUS_FIELDS,
    SETTING_FIELDS,
    ConfigError,
    Settings,
    effective_setting_values,
    grouped_setting_fields,
    save_settings,
    setting_defaults,
)
from svtplay_arr.mappings import MappingError, MappingTable, add_mapping, remove_mapping
from svtplay_arr.svt.client import SvtApiError, derive_slug
from svtplay_arr.yamlio import ConcurrentModification, read_with_mtime

log = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# The one place an outcome from `_check_slug`/`_check_context` picks a CSS
# class -- both response paths of the mapping Check control (the full-page
# re-render and the JSON the JS fetch consumes) read `css_class` off the
# same dict rather than each deciding independently, so they cannot pick
# different colours for the same result.
_CHECK_CSS_CLASS = {
    "found": "notice",
    "not_found": "warn",
    "error": "error",
    "unknown_mapping": "error",
}


async def _check_slug(svt, slug: str) -> dict:
    """The one computation behind the per-mapping Check control.

    Both of its response paths -- the no-JS form POST that re-renders the
    whole page, and the JS fetch that patches just one row -- call this and
    render its result verbatim. Neither ever re-derives the outcome itself;
    see the module docstring on why that split is the thing to avoid (the
    same reasoning as `compute_health`/`status_provider` in app.py: two
    places computing one fact is this project's most common defect).

    Calls `SvtClient.list_episodes` exactly as `Resolver` already does --
    this module still has no SVT knowledge of its own beyond "call the
    client, look at what came back".

    Read-only: never touches mappings.yaml, config.yaml or the job store.

    Never raises. `SvtApiError` -- a timeout, a connection failure, a
    malformed response, or a slug SVT 404s on -- becomes the "error" or
    "not_found" outcome below rather than propagating; anything else
    unexpected from the client is caught too, since a check must never be
    the thing that turns a page render into a 500.

    A "found" result proves only that the slug resolves to *some* show's
    episode list -- not that it is *this* show's. A valid slug for the
    wrong series looks identical, and the cost of believing it anyway is a
    permanently wrong filename in the library once Sonarr imports from it.
    The message says so explicitly; nothing here is worded as "this
    mapping is correct".
    """
    try:
        episodes = await svt.list_episodes(slug)
    except SvtApiError as exc:
        if exc.status_code == 404:
            return {
                "outcome": "not_found",
                "css_class": _CHECK_CSS_CLASS["not_found"],
                "episode_count": None,
                "message": (
                    f"SVT has nothing at slug {slug!r} (404 not found). "
                    "The slug is probably wrong, the show may have ended, "
                    "or SVT restructured its URLs. This does not confirm "
                    "or rule out anything about whether the mapping "
                    "otherwise points at the right show."
                ),
            }
        return {
            "outcome": "error",
            "css_class": _CHECK_CSS_CLASS["error"],
            "episode_count": None,
            "message": f"SVT could not be checked: {exc}",
        }
    except Exception as exc:
        # Not documented to raise anything else, but a check must not be
        # the one route that can 500 -- see the module's "never a 500" rule.
        log.warning("mapping check failed unexpectedly for slug %r", slug, exc_info=True)
        return {
            "outcome": "error",
            "css_class": _CHECK_CSS_CLASS["error"],
            "episode_count": None,
            "message": f"SVT could not be checked: {exc}",
        }

    if not episodes:
        return {
            "outcome": "not_found",
            "css_class": _CHECK_CSS_CLASS["not_found"],
            "episode_count": 0,
            # Three causes, not two. `parse_show_page` is a regex scan
            # over SVT's escaped payload, so a markup change on SVT's side
            # returns [] from a valid 200 for a correct slug -- and in that
            # outage the resolver goes quiet too, so the operator checks
            # every row and would be told the slug is probably wrong for
            # all of them. That points away from the parser, which is the
            # thing that needs fixing. The 404 branch above genuinely is
            # SVT saying "no such show" and keeps its own two causes.
            "message": (
                f"SVT returned no episodes for slug {slug!r}. The slug may "
                "be wrong, the show may have ended, or svtplay-arr may "
                "have failed to parse SVT's show page -- its parser reads "
                "SVT's own markup, so a change there empties this result "
                "for a slug that is perfectly correct. If every mapping "
                "checks empty, suspect the parser before the slugs."
            ),
        }

    n = len(episodes)
    return {
        "outcome": "found",
        "css_class": _CHECK_CSS_CLASS["found"],
        "episode_count": n,
        "message": (
            f"SVT lists {n} episode{'s' if n != 1 else ''} for slug "
            f"{slug!r}. This confirms the slug resolves -- it does not "
            "confirm this mapping points at the right show: a valid slug "
            "for the wrong series looks identical. Spot-check the series "
            "title and an episode against Sonarr yourself before trusting "
            "this mapping."
        ),
    }


def _as_mtime(value) -> float | None:
    try:
        return float(str(value)) if value not in (None, "") else None
    except ValueError:
        return None


def _parse_expected_mtime(raw) -> tuple[float | None, str | None]:
    """Parse a submitted `expected_mtime` hidden field.

    Absent or empty legitimately means "no expected mtime" (the first-write
    case) and must keep working. A *present but unparseable* value is
    different: silently treating it as None would disable the staleness
    check it exists to provide, so a corrupted or tampered hidden field is
    refused (the second element is the error to show) instead of being
    treated as "no token at all".

    This is the one guard every write route needs -- settings save,
    mapping create, and mapping delete all accept an `expected_mtime` and
    must all refuse a corrupted one the same way, not just the route where
    the bug was first noticed.
    """
    expected = _as_mtime(raw)
    if raw not in (None, "") and expected is None:
        return None, (
            "expected_mtime is not a valid number; reload the page and "
            "try again."
        )
    return expected, None


def _parse_svt_selection(form: dict) -> tuple[str, str, str | None]:
    """Decompose the SVT pick out of a submitted create form.

    Returns `(svt_series_id, svt_slug, error)`. There are two ways the
    pick can arrive and exactly one place that knows the difference: the
    radio value encodes "svt_id|slug" as a single `svt` field, while the
    manual-entry fallback (Task 7, for when SVT search is unreachable)
    sends `svt_series_id` and `svt_slug` separately.

    Shared by `create_mapping`, which acts on the pick, and
    `_search_failure_response`, which has to redisplay it after a failed
    create. A second copy of this in the redisplay path is what made the
    radio case silently lose the operator's choice: it seeded the manual
    boxes only from the manual fields, so a radio-selected show came back
    as two empty boxes and a slug to re-transcribe off an SVT URL.

    Never raises; a malformed value becomes the returned `error`.
    """
    svt_value = str(form.get("svt", "") or "")
    if "|" in svt_value:
        # The slug half is server-derived and safe by construction, but
        # svt_id comes unvalidated from SVT's API -- partitioning on the
        # first "|" would silently fold a stray pipe in svt_id into the
        # slug instead of failing, so a value with anything other than
        # exactly one "|" is refused outright.
        parts = svt_value.split("|")
        if len(parts) != 2:
            return "", "", "Malformed SVT selection; pick the show again."
        svt_series_id, svt_slug = (p.strip() for p in parts)
        return svt_series_id, svt_slug, None
    # A human typed these, so trim stray whitespace before it becomes a
    # broken slug.
    return (
        str(form.get("svt_series_id", "") or "").strip(),
        str(form.get("svt_slug", "") or "").strip(),
        None,
    )


def _env_overrides_api_key() -> bool:
    """Is `$SONARR_API_KEY` set, and therefore beating config.yaml?

    `Settings.load` gives the environment precedence over the file, so a key
    saved through this page is written and then ignored: the save reports
    success, the restart banner promises the change will apply, and the
    service keeps calling Sonarr with the old value. Read per-request rather
    than captured at router-build time, so the page reflects the environment
    the running process actually has.
    """
    return bool(os.environ.get("SONARR_API_KEY"))


def _changed_setting_fields(existing: dict, submitted: dict[str, str]) -> list:
    """Which `SETTING_FIELDS` actually changed, for the post-save notice.

    Compares the effective on-disk value (read *before* the save, falling
    back to `Settings`' own default for a key the file omits -- the value
    the form rendered) against what was submitted, using each field's
    declared `kind` so an int written in a different but equal textual form
    (e.g. re-submitting an unchanged field) never reads as a change.

    Returns `SettingField`s, and the caller renders only their `.label`, so
    no setting's *value* -- `sonarr_api_key` included, now that it is one of
    `SETTING_FIELDS` -- can reach the notice.

    Never raises: a value that fails to compare (e.g. a non-numeric
    existing value in a hand-edited file) is conservatively treated as
    changed rather than silently dropped from the notice.
    """
    changed = []
    env_wins = _env_overrides_api_key()
    defaults = setting_defaults()
    for f in SETTING_FIELDS:
        if f.key not in submitted:
            continue
        # "Sonarr API key changed; restart svtplay-arr to apply" is a lie
        # when $SONARR_API_KEY is set: the value was written, but the
        # environment still wins after any number of restarts. The field's
        # own warning states the real situation; the notice must not talk
        # over it with a promise nothing can keep.
        if f.key == "sonarr_api_key" and env_wins:
            continue
        # Compare what save_settings will actually store, which is the
        # stripped value -- otherwise re-submitting an unchanged field with
        # a stray space reads as a change and the notice tells the operator
        # to restart for nothing.
        new = str(submitted[f.key]).strip()
        # Against the *effective* old value, matching what the form
        # rendered. A key config.yaml omits is running at its default, and
        # the form now shows that default -- so submitting it back writes
        # the key for the first time without changing anything the service
        # does. Reading `existing` alone here would report all three int
        # fields as changed on the very first save from the live
        # deployment, and raise a restart banner for a genuine no-op.
        # `.get` with no default, so a key with neither a file value nor a
        # dataclass default (sonarr_api_key) stays None and still compares
        # as changed.
        old = existing.get(f.key, defaults.get(f.key))
        try:
            if old is None:
                same = False
            elif f.kind == "int":
                same = int(str(old)) == int(new)
            else:
                same = str(old) == new
        except (TypeError, ValueError):
            same = False
        if not same:
            changed.append(f)
    return changed


def build_config_router(
    config_path: Path, mappings_path: Path, svt, sonarr, booted=None,
    status_provider=None,
) -> APIRouter:
    """`booted` is the `Settings` the service actually started with.

    Without it the page can only show what is on disk, which after a save
    is not what the service is running -- see `_pending_restart_fields`. It
    is optional so a router built without it (as most tests do) degrades to
    showing no banner rather than failing to build.

    `status_provider`, if given, is a zero-argument callable returning the
    same dict `/health` returns (see `app.compute_health`). It is the
    *only* thing this module knows about worker/store/mapping-table state:
    deliberately a bare callable, not `Worker`/`JobStore`/`Settings`
    objects, so this module never needs to import them -- a UI change here
    cannot reach into the download pipeline. There must be exactly one
    place that computes these facts; this module only ever renders what it
    is handed, never recomputes any of it.

    Optional, and never allowed to fail the page: no provider at all
    degrades to not rendering the status strip (as most tests do, since
    they build a router directly without a running worker/store to report
    on), and a provider that raises is caught and rendered as "status
    unavailable" rather than a 500 -- the config page must be at least as
    forgiving as `/health` itself.
    """
    router = APIRouter(prefix="/config")

    def _pending_restart_fields() -> list:
        """Settings changed on disk that the running service has not picked up.

        Settings need a restart, so between a save and that restart the
        page would otherwise show the new values with nothing to say the
        service is still using the old ones. The scenario the spec's
        "persistent banner" requirement exists for: change
        air_date_tolerance_days from 1 to 3, get distracted, never restart,
        and next week read `3` off this page while reasoning about resolver
        behaviour that is actually running with `1`.

        The comparison goes through the real `Settings.load`, not a
        hand-rolled read of the raw dict: that is the only way a field
        absent from the file compares equal to the default the service
        booted with, without this module keeping its own copy of every
        default and drifting from the loader's.

        Only `SETTING_FIELDS` keys are consulted, and only their `.label` is
        ever rendered, so no setting's value reaches the banner --
        `sonarr_api_key`, editable here since 2026-08-25, included.

        The key *is* compared, though: editing it and not restarting is the
        same stale-value trap as any other setting, and a worse one, since
        the running service goes on authenticating with the old key. The one
        exception is an active `$SONARR_API_KEY` override, where file and
        booted value differ permanently and no restart can reconcile them --
        naming the field there would leave a banner that cannot be cleared,
        telling the operator to do something that will not work.

        Never raises: a config file that will not load is already reported
        by `_index`'s own guard, and a banner is not worth failing a page
        over.
        """
        if booted is None:
            return []
        try:
            on_disk = Settings.load(config_path)
        except Exception:
            log.warning(
                "could not load %s to check for pending settings changes; "
                "rendering the page without the restart banner",
                config_path,
                exc_info=True,
            )
            return []
        pending = []
        env_wins = _env_overrides_api_key()
        for f in SETTING_FIELDS:
            if f.key == "sonarr_api_key" and env_wins:
                continue
            try:
                if getattr(on_disk, f.key) != getattr(booted, f.key):
                    pending.append(f)
            except AttributeError:
                continue
        return pending

    def _mappings_mtime() -> float | None:
        """The mtime to round-trip as a form's concurrency-check field.

        stat, not a full parse, so a malformed mappings.yaml cannot raise
        from the one call that only ever wanted a timestamp -- and the
        OSError guard covers the unreadable case (`chmod 000`, a
        permissions change on the directory) that `exists()` alone does
        not. Every route that renders a mappings form goes through here, so
        there is one place for this to be right rather than one per route.
        """
        try:
            return mappings_path.stat().st_mtime if mappings_path.exists() else None
        except OSError:
            return None

    def _status_strip_context() -> dict:
        """The `health`/`status_unavailable` pair the template needs.

        Never raises: a broken status_provider must not take the whole
        config page down with it (the page must be at least as forgiving as
        `/health`, which has the same rule for its own internals). Rendering
        the page with no strip, or with an explicit "status unavailable", is
        always preferable to a 500 here.
        """
        if status_provider is None:
            return {"health": None, "status_unavailable": False}
        try:
            return {"health": status_provider(), "status_unavailable": False}
        except Exception:
            log.exception(
                "status_provider failed; rendering the config page without "
                "the status strip"
            )
            return {"health": None, "status_unavailable": True}

    async def _check_context(tvdb_id: int) -> dict:
        """Look up the mapping for `tvdb_id` and run `_check_slug` on it.

        The one thing standing between the route handlers and `_check_slug`:
        it turns a path parameter into the slug that function wants, and
        handles the two ways that lookup itself can fail (an unreadable/
        invalid mappings.yaml, or a `tvdb_id` with no mapping) -- neither of
        which is a reason to call SVT, so this is also where "there is
        nothing to check" is decided before any network call happens.

        Never raises, for the same reason as `_check_slug`: a check must
        never turn into a 500.
        """
        try:
            mapping = MappingTable.load(mappings_path).for_tvdb(tvdb_id)
        except Exception as exc:
            log.warning(
                "mappings file %s is invalid while checking tvdb_id %s",
                mappings_path, tvdb_id, exc_info=True,
            )
            return {
                "tvdb_id": tvdb_id,
                "outcome": "error",
                "css_class": _CHECK_CSS_CLASS["error"],
                "episode_count": None,
                "message": f"could not check: {mappings_path} is invalid: {exc}",
            }
        if mapping is None:
            return {
                "tvdb_id": tvdb_id,
                "outcome": "unknown_mapping",
                "css_class": _CHECK_CSS_CLASS["unknown_mapping"],
                "episode_count": None,
                "message": f"No mapping exists for tvdb_id {tvdb_id}.",
            }
        result = await _check_slug(svt, mapping.svt_slug)
        return {"tvdb_id": tvdb_id, **result}

    def _index(request: Request, error=None, notice=None, submitted=None, check=None):
        errors = [error] if error else []

        try:
            raw, config_mtime = read_with_mtime(config_path)
        except Exception as exc:
            # A malformed config.yaml must render the page with an error,
            # not propagate: config.yaml is the file a human is most likely
            # to have hand-edited, since that's the entire premise of the
            # settings form below.
            log.warning(
                "config file %s is invalid; rendering the page with an "
                "error instead of failing the request",
                config_path,
                exc_info=True,
            )
            raw, config_mtime = {}, None
            errors.append(f"{config_path} is invalid: {exc}")

        mappings_mtime = _mappings_mtime()

        # Distinguishes "the file says there are no mappings" from "the
        # file could not be read", which are the same empty list here and
        # must never render as the same sentence: while this load fails the
        # running service may still be serving a last known-good table, so
        # telling the operator that nothing is offered to Sonarr can be
        # false, and reads as an invitation to restart -- which is the one
        # action that would make it true.
        mappings_unavailable = False
        try:
            mappings = MappingTable.load(mappings_path).all()
        except Exception as exc:
            log.warning(
                "mappings file %s is invalid; rendering the page with an "
                "error instead of failing the request",
                mappings_path,
                exc_info=True,
            )
            mappings = []
            mappings_unavailable = True
            errors.append(f"{mappings_path} is invalid: {exc}")

        # After a rejected save, redisplay what the operator typed rather
        # than what is still on disk -- a save refused over one bad field
        # must not also discard every other field they filled in. Only
        # SETTING_FIELDS keys are ever consulted here, so a `submitted`
        # dict built from raw form data can never smuggle the API key (or
        # anything else) into the page.
        #
        # The fallback is the *effective* value, not the file's literal
        # contents: a key config.yaml omits is not unset, the service is
        # running on its default, and the form has to render that. Rendering
        # it blank is what made every save on the live deployment fail --
        # see effective_setting_values.
        effective = effective_setting_values(raw)
        values = {
            f.key: (submitted or {}).get(f.key, effective[f.key])
            for f in SETTING_FIELDS
        }

        # Rendered on every GET, not just the POST that caused it -- a page
        # that shows the new value with no sign the service is still using
        # the old one is exactly the "implies a change took effect when it
        # did not" the spec's Reload model rules out.
        # Called once and reused: it invokes status_provider, and the
        # mappings row below needs the same dict the strip renders.
        status_context = _status_strip_context()

        # Deliberately tri-state, and the template branches on all three.
        # "The service keeps serving its last good table" is true after a
        # successful load and false on a fresh boot whose file was already
        # broken -- where nothing was ever loaded, nothing is offered to
        # Sonarr, and Sonarr will reject the indexer. Saying the
        # reassuring thing there is the same defect as the "Nothing will be
        # offered to Sonarr" row this replaced, merely inverted: it offers
        # comfort exactly where urgency is needed.
        #
        # None means "no status dict to ask" -- no provider (as most
        # routers are built), or one that raised. The page then hedges and
        # asserts neither, because a last-good table that cannot be
        # confirmed must never be claimed. Read off /health's own dict
        # rather than recomputed here; this module renders health facts, it
        # never derives them.
        health = status_context.get("health")
        mappings_ever_loaded = None
        if isinstance(health, dict) and health.get("mappings_ever_loaded") is not None:
            mappings_ever_loaded = bool(health["mappings_ever_loaded"])

        pending = _pending_restart_fields()
        pending_notice = (
            "Restart svtplay-arr to apply: "
            + ", ".join(f.label for f in pending)
            + ". The running service is still using the previous values."
        ) if pending else None

        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "sections": grouped_setting_fields(),
                "dangerous_fields": DANGEROUS_FIELDS,
                "values": values,
                "mappings": mappings,
                # True only when the load above raised, never when the file
                # legitimately holds `series: []` -- see the guard there.
                "mappings_unavailable": mappings_unavailable,
                # True / False / None; only consulted when the above is
                # True. See where it is computed for why None is a state.
                "mappings_ever_loaded": mappings_ever_loaded,
                "config_mtime": "" if config_mtime is None else config_mtime,
                "mappings_mtime": "" if mappings_mtime is None else mappings_mtime,
                "error": "; ".join(errors) if errors else None,
                "notice": notice,
                "pending": pending_notice,
                # Drives the warning beside the API key field. Without it a
                # save on an env-overridden deployment looks like it worked.
                "env_overrides_api_key": _env_overrides_api_key(),
                # Set only by the no-JS Check form POST, and only for the
                # one row it was submitted for -- see the check route below.
                # `_index` never computes this itself and this function
                # never calls SVT: every other caller (every GET, every
                # other POST) passes no `check` at all, which is what keeps
                # the Check control from ever firing on a page load.
                "check": check,
                **status_context,
            },
        )

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return _index(request)

    @router.post("/settings", response_class=HTMLResponse)
    async def save(request: Request):
        form = dict(await request.form())
        raw_mtime = form.pop("expected_mtime", None)
        confirmed = str(form.pop("confirm_paths", "")) == "yes"
        submitted = {k: str(v) for k, v in form.items()}

        expected, mtime_error = _parse_expected_mtime(raw_mtime)
        if mtime_error is not None:
            return _index(request, error=mtime_error, submitted=submitted)

        # The spec requires an explicit confirmation for a path change
        # specifically: changing these under a running worker orphans
        # in-flight downloads, and it is the most destructive thing this
        # page can do.
        try:
            existing, _ = read_with_mtime(config_path)
        except Exception as exc:
            # Same reasoning as _index's identical guard: config.yaml is
            # the file a human is most likely to have hand-edited, so a
            # save attempt over a currently-broken file must render an
            # error, not 500 -- that would leave the operator unable to
            # use the one form that can fix it.
            log.warning(
                "config file %s is invalid while checking for a path "
                "change; rendering the page with an error instead of "
                "failing the request",
                config_path,
                exc_info=True,
            )
            return _index(
                request,
                error=f"{config_path} is invalid: {exc}",
                submitted=submitted,
            )

        # `.strip()` because save_settings stores the stripped value: the
        # comparison has to be against what will actually be written, or
        # re-submitting an unchanged directory with a stray space demands
        # the confirmation checkbox -- the page's most alarming message --
        # for a no-op. Third of three sibling comparison sites; the other
        # two are config.save_settings and _changed_setting_fields above.
        changed_paths = [
            f.key
            for f in SETTING_FIELDS
            if f.kind == "path"
            and f.key in submitted
            and submitted[f.key].strip() != str(existing.get(f.key, ""))
        ]
        if changed_paths and not confirmed:
            return _index(
                request,
                error=(
                    "Changing "
                    + ", ".join(changed_paths)
                    + " orphans in-flight downloads. Tick the confirmation "
                    "box to proceed."
                ),
                submitted=submitted,
            )

        try:
            save_settings(config_path, submitted, expected_mtime=expected)
        except ConcurrentModification as exc:
            return _index(request, error=str(exc), submitted=submitted)
        except ConfigError as exc:
            return _index(request, error=str(exc), submitted=submitted)
        except Exception as exc:
            # Never a 500 -- render the problem instead.
            log.exception("settings save failed")
            return _index(
                request,
                error=f"could not save settings: {exc}",
                submitted=submitted,
            )
        default_notice = (
            "Settings saved. Restart svtplay-arr to apply them "
            "(mappings apply immediately; settings do not)."
        )
        try:
            changed = _changed_setting_fields(existing, submitted)
        except Exception:
            # The save already succeeded and is not to be undone by a
            # notice-wording bug -- fall back to the generic notice rather
            # than a 500.
            log.exception("could not compute changed settings fields")
            return _index(request, notice=default_notice)

        if not changed:
            notice = "Settings saved unchanged."
        else:
            names = ", ".join(f.label for f in changed)
            notice = (
                f"Settings saved. {names} changed; restart svtplay-arr to "
                "apply (mappings apply immediately; settings do not)."
            )
        return _index(request, notice=notice)

    @router.get("/mappings/new", response_class=HTMLResponse)
    async def new_mapping(request: Request):
        return _TEMPLATES.TemplateResponse(
            request, "mapping_new.html", {"error": None, "notice": None}
        )

    async def _build_search_context(query: str):
        """Re-run the SVT search and Sonarr listing for `query`.

        Returns `(svt_hits, sonarr_series, mappings_mtime, error)`. Never
        raises: every failure inside becomes the returned `error` string
        (the same wording `POST /config/mappings/search` has always shown)
        rather than propagating, so a caller rebuilding the picker after a
        failed create can choose to discard `error` in favour of the one
        that actually caused the create to fail -- see
        `_search_failure_response`, the only other caller.
        """
        error = None

        svt_hits = []
        try:
            for hit in await svt.search_series(query):
                svt_hits.append(
                    {
                        "svt_id": hit.svt_id,
                        "name": hit.name,
                        # SVT's search does not return the slug; derive the
                        # conventional one so the common case needs no typing.
                        "slug": derive_slug(hit.name),
                    }
                )
        except Exception as exc:
            log.warning("SVT search failed for %r: %s", query, exc)
            error = (
                f"SVT search failed ({exc}). Enter the id and slug by hand "
                "from the show's SVT Play URL."
            )

        sonarr_series = []
        try:
            needle = query.casefold()
            sonarr_series = [
                s
                for s in await sonarr.all_series()
                if isinstance(s, dict) and needle in str(s.get("title", "")).casefold()
            ]
        except Exception as exc:
            log.warning("Sonarr series list failed: %s", exc)
            error = (error + " ") if error else ""
            error += f"Sonarr lookup failed ({exc})."

        # This used to be a bare `read_with_mtime(mappings_path)`, which
        # parses the file it only needed to stat: a malformed or unreadable
        # mappings.yaml turned this route into a 500 while GET /config
        # rendered the same file as a 200 with an error banner. The guard
        # and the banner both belong on every route that touches the file,
        # not just the one where the problem was first noticed.
        try:
            MappingTable.load(mappings_path)
        except Exception as exc:
            log.warning(
                "mappings file %s is invalid; rendering the search page "
                "with an error instead of failing the request",
                mappings_path,
                exc_info=True,
            )
            error = (error + " ") if error else ""
            error += f"{mappings_path} is invalid: {exc}"

        return svt_hits, sonarr_series, _mappings_mtime(), error

    async def _search_failure_response(request: Request, form: dict, error: str):
        """Re-render the search-results page after a failed mapping create.

        The operator already has a problem -- the create failed -- so the
        page must not also throw away the SVT/Sonarr picker they just
        filled in. This re-runs the search server-side from the original
        query (round-tripped through the confirm form as a hidden `q`
        field) instead of trusting anything about SVT's results from the
        form: see the module docstring for why results never travel as
        markup.

        `error` -- the reason the *create* failed -- is always what gets
        shown, never whatever the re-run itself produces. The re-run calls
        the same two services that likely just caused the create to fail,
        so surfacing its own "SVT search failed" instead of e.g. "tvdbId
        already mapped" would bury the actual cause under a secondary one.

        Falls back to the index page with `error` if there is no query to
        search with, or if rebuilding the results blows up outright --
        losing the real cause behind a "search failed" banner would be
        worse than today's behaviour of discarding the picker.
        """
        query = str(form.get("q", "") or "").strip()
        # Decomposed through the same function `create_mapping` acts on, so
        # a radio-selected show ("svt_id|slug" in one field) seeds the
        # manual boxes too. Whether the re-run below returns hits decides
        # which of the two controls actually renders, and the operator's
        # pick has to survive either way -- SVT flapping between the search
        # and the create is precisely when it flips.
        selected_svt_series_id, selected_svt_slug, _svt_error = (
            _parse_svt_selection(form)
        )
        if not query:
            return _index(request, error=error)
        try:
            svt_hits, sonarr_series, mappings_mtime, _rerun_error = (
                await _build_search_context(query)
            )
        except Exception:
            log.exception(
                "could not rebuild the search results after a failed "
                "mapping create; falling back to the index page"
            )
            return _index(request, error=error)
        return _TEMPLATES.TemplateResponse(
            request,
            "mapping_search.html",
            {
                "query": query,
                "svt_hits": svt_hits,
                "sonarr_series": sonarr_series,
                "mappings_mtime": "" if mappings_mtime is None else mappings_mtime,
                "error": error,
                "notice": None,
                # The operator's picks, so a duplicate-tvdb_id or similar
                # error lets them change just the one thing that was wrong
                # instead of re-selecting everything from scratch.
                "selected_svt": str(form.get("svt", "") or ""),
                "selected_svt_series_id": selected_svt_series_id,
                "selected_svt_slug": selected_svt_slug,
                "selected_sonarr": str(form.get("sonarr", "") or ""),
                **_status_strip_context(),
            },
        )

    @router.post("/mappings/search", response_class=HTMLResponse)
    async def search(request: Request):
        form = dict(await request.form())
        query = str(form.get("q", "")).strip()

        if not query:
            # An empty needle matches every Sonarr title by substring, which
            # would dump the operator's whole library onto the page instead
            # of narrowing anything -- refuse before either client is called.
            return _TEMPLATES.TemplateResponse(
                request,
                "mapping_new.html",
                {"error": "Enter a show title to search.", "notice": None},
            )

        svt_hits, sonarr_series, mappings_mtime, error = (
            await _build_search_context(query)
        )
        return _TEMPLATES.TemplateResponse(
            request,
            "mapping_search.html",
            {
                "query": query,
                "svt_hits": svt_hits,
                "sonarr_series": sonarr_series,
                # `is None`, not `or ""`: a legitimate mtime of 0.0 would
                # render as "" under `or`, and the write route reads an
                # empty hidden field as "no expected mtime", silently
                # skipping the concurrency check. Same test _index uses.
                "mappings_mtime": "" if mappings_mtime is None else mappings_mtime,
                "error": error,
                "notice": None,
                "selected_svt": None,
                "selected_svt_series_id": None,
                "selected_svt_slug": None,
                "selected_sonarr": None,
                # The search-results page is where an operator troubleshoots
                # after a failed create, so it needs the same strip /config
                # has -- a degraded mappings table or a dead worker is at
                # least as relevant here as it is there.
                **_status_strip_context(),
            },
        )

    @router.post("/mappings", response_class=HTMLResponse)
    async def create_mapping(request: Request):
        form = dict(await request.form())
        expected, mtime_error = _parse_expected_mtime(form.get("expected_mtime"))
        if mtime_error is not None:
            return await _search_failure_response(request, form, mtime_error)

        svt_series_id, svt_slug, svt_error = _parse_svt_selection(form)
        if svt_error is not None:
            return await _search_failure_response(request, form, svt_error)
        if not svt_series_id or not svt_slug:
            return await _search_failure_response(
                request, form, "Pick an SVT show, or enter its id and slug."
            )

        try:
            tvdb_id = int(str(form.get("sonarr", "")))
        except (TypeError, ValueError):
            return await _search_failure_response(request, form, "Pick a Sonarr series.")

        # series_title must come from Sonarr's record, never from the form.
        try:
            series = await sonarr.all_series()
        except Exception as exc:
            log.warning("Sonarr lookup failed while creating a mapping: %s", exc)
            return await _search_failure_response(
                request, form, f"Sonarr lookup failed ({exc}); not saved."
            )
        match = next(
            (
                s
                for s in series
                if isinstance(s, dict) and s.get("tvdbId") == tvdb_id
            ),
            None,
        )
        if match is None:
            return await _search_failure_response(
                request,
                form,
                f"Sonarr has no series with tvdbId {tvdb_id}; not saved.",
            )

        try:
            add_mapping(
                mappings_path,
                tvdb_id=tvdb_id,
                svt_series_id=svt_series_id,
                svt_slug=svt_slug,
                series_title=str(match.get("title") or ""),
                expected_mtime=expected,
            )
        except (MappingError, ConcurrentModification) as exc:
            return await _search_failure_response(request, form, str(exc))
        except Exception as exc:
            log.exception("mapping create failed")
            return await _search_failure_response(
                request, form, f"could not save mapping: {exc}"
            )
        return _index(request, notice=f"Added {match.get('title')!r}. "
                                      "Mappings apply immediately.")

    @router.post("/mappings/{tvdb_id}/delete", response_class=HTMLResponse)
    async def delete_mapping(request: Request, tvdb_id: int):
        form = dict(await request.form())
        expected, mtime_error = _parse_expected_mtime(form.get("expected_mtime"))
        if mtime_error is not None:
            return _index(request, error=mtime_error)
        try:
            remove_mapping(
                mappings_path, tvdb_id,
                expected_mtime=expected,
            )
        except (MappingError, ConcurrentModification) as exc:
            return _index(request, error=str(exc))
        except Exception as exc:
            log.exception("mapping delete failed")
            return _index(request, error=f"could not remove mapping: {exc}")
        return _index(request, notice=f"Removed mapping for tvdbId {tvdb_id}.")

    @router.post("/mappings/{tvdb_id}/check", response_class=HTMLResponse)
    async def check_mapping(request: Request, tvdb_id: int):
        """The per-mapping Check control.

        Strictly on demand: this route is the *only* place `_check_slug`
        (and therefore SVT) is ever called. `_index` takes a `check`
        argument but never fills it in itself, so nothing on GET /config,
        or on any other POST route, can call SVT -- see
        `test_check_never_runs_on_page_load` and
        `test_check_never_runs_on_any_other_post_either`.

        Two response shapes over one computation (`_check_context`): a
        request asking for JSON -- the JS enhancement's `fetch`, which sets
        `Accept: application/json` itself -- gets back just the result, so
        it can patch one row without a page reload. Anything else (a plain
        browser form POST, JS disabled) gets the full page re-rendered with
        the same result shown inline for that row. Both branches render
        `_check_context`'s return value verbatim; neither recomputes it.

        `tvdb_id` is a path-typed int, so a non-integer path segment never
        reaches this function at all -- FastAPI answers 422 before this
        body runs, which is a handled result, not a 500.
        """
        result = await _check_context(tvdb_id)
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(result)
        return _index(request, check=result)

    return router
