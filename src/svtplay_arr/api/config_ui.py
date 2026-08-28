"""The configuration page.

Four server-rendered views behind a nav bar -- Status, Mappings, Activity
and Settings, listed in `VIEWS` -- with Status served from `/config`
itself.
That URL is documented, deployed, and the published SSO resource points at
it; the restructure changed which view answers there, never whether one
does. There is no client-side routing and no build step: every link is an
ordinary GET and every control is an ordinary form POST.

The landing view is Status rather than the settings form because an
operator opens this page to ask "is it working" far more often than to
change a setting -- and a setting needs a service restart before it does
anything, so the form is the one thing here that is never urgent.

Contains no matching logic and no SVT knowledge: it calls SvtClient and
SonarrClient the same way `discovery.sweep_for_mappings` does. That seam is
what makes it impossible for a UI change to alter what gets grabbed.

Two controls here make live calls, both strictly on demand and never on a
page render. The per-mapping Check control (`_check_slug`, `_check_match`,
`_check_context`, `check_mapping`) calls `svt.list_episodes(slug)` the same
way `Resolver` does, and then asks the same question `Resolver` asks next:
can those episodes match any of Sonarr's? That second half is
`canary.check_resolvability`, shared verbatim with the background check, so
this control and the verdict rendered beside it on the page cannot answer
the same question differently -- it used to answer only the slug half, and
so told an operator investigating "this mapping resolves nothing" that SVT
lists 61 episodes for it. It writes nothing: three read-only GETs. The Find
mappings sweep
(`discover`) searches SVT for each unmapped series and then reads the
episode lists of the few candidates it finds -- the same `list_episodes`
call, and it is the one route that writes without a per-row confirmation.
It may write only what `discovery.corroborated_match` approved, and that
gate lives in `discovery.py`, not here. This module holds no matching rule
of its own, here as everywhere else.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
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
from svtplay_arr.canary import (
    UNDETERMINED_SONARR_UNAVAILABLE,
    Resolvability,
    check_resolvability,
)
from svtplay_arr.discovery import sweep_for_mappings
from svtplay_arr.mappings import (
    MappingError,
    MappingTable,
    add_mapping,
    add_mappings,
    remove_mapping,
)
from svtplay_arr.models import SOURCE_AUTO, JobStatus
from svtplay_arr.sonarr import REASON_UNKNOWN, SonarrApiError
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
# What one "Find mappings" click may cost SVT. The sweep now corroborates
# candidates against their episode lists rather than trusting a title, so a
# series costs up to a few searches plus an episode-list fetch per checked
# candidate -- see `discovery`'s own bounds, which these mirror.
# Module-level rather than defaults on the call, so the bound is stated in
# one visible place -- and so a test can shrink it without reaching inside
# the router closure.
_SWEEP_CONCURRENCY = 4
_SWEEP_CAP = 200
_SWEEP_REQUEST_BUDGET = 600

# The four views, their nav labels and their paths, in nav order. One
# list, read by the nav bar, by the routes that render each view and by
# the tests that walk them -- rather than a copy per surface that can
# drift into a nav link pointing at a view that no longer exists.
#
# Status is first *and* lives at /config itself. That URL is documented,
# deployed, and the published SSO resource points at it; it stays the
# entry point, and what changed is only which view it serves. An operator
# opens this page to ask "is it working" far more often than to change a
# setting -- and settings need a restart anyway, so the form is the one
# thing here that is never urgent.
VIEWS = (
    ("status", "Status", "/config"),
    ("mappings", "Mappings", "/config/mappings"),
    ("activity", "Activity", "/config/activity"),
    ("settings", "Settings", "/config/settings"),
)

# How many finished jobs the Status view's summary shows. The job store
# keeps history until Sonarr deletes it, so the landing view shows a
# bounded summary and the Activity view has the rest.
_STATUS_RECENT_LIMIT = 5

# The store's own spelling of a failed job, read off the enum the SAB
# endpoints depend on rather than written out again here. The templates
# are handed this value; they hold no copy of the string.
_FAILED = JobStatus.FAILED.value

_CHECK_CSS_CLASS = {
    "found": "notice",
    "not_found": "warn",
    "error": "error",
    "unknown_mapping": "error",
    # The slug resolves and the mapping still cannot produce a grab. Amber
    # rather than red for the same reason the finding is amber everywhere
    # else: one dead row does not stop anything else working, and in the
    # no-ordinal case there is nothing to fix, so a red result would be
    # permanent -- see DEGRADED_STATES in canary.py.
    "resolves_nothing": "warn",
    # The slug resolves and the other half could not be answered, because
    # Sonarr could not be asked. Amber, because the one thing this result
    # must never do is read as an all-clear about a question nobody
    # answered.
    "match_unchecked": "warn",
}

# How long the Check control waits on Sonarr for the matching half. Well
# short of a browser's patience, and short of the SVT half's own timeout
# budget: an operator pressing Check on a page that is already telling them
# something is wrong should not be left staring at a spinner because Sonarr
# is the thing that is wrong.
_CHECK_SONARR_TIMEOUT_S = 15.0

# The same idea for the Sonarr Test connection control: one dict, read by
# both the no-JS full-page re-render and the JSON the fetch consumes, so
# they cannot paint the same outcome different colours.
_SONARR_TEST_CSS_CLASS = {
    "ok": "notice",
    "failed": "error",
    "incomplete": "warn",
    "unavailable": "warn",
}

# How long the Test control waits for Sonarr. Shorter than the shared httpx
# client's own 30s, because this one is bounded by a human sitting in front
# of a page that has not finished loading -- and every route here is
# `async def`, so an unbounded await would hold the event loop's attention
# on one click for half a minute.
_SONARR_TEST_TIMEOUT_S = 10.0


async def _check_slug(svt, slug: str) -> tuple[dict, list | None]:
    """The SVT half of the per-mapping Check control.

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

    Returns the result *and* the episodes it read, or `None` for the
    episodes when there are none to compare. The list is what
    `_check_context` hands to `canary.check_resolvability` for the other
    half of the answer, and it is deliberately kept out of the result dict:
    that dict is serialised straight to JSON for the page's fetch, and a
    list of dataclasses in it would be a 500 on the one control an operator
    presses when something is already wrong.
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
            }, None
        return {
            "outcome": "error",
            "css_class": _CHECK_CSS_CLASS["error"],
            "episode_count": None,
            "message": f"SVT could not be checked: {exc}",
        }, None
    except Exception as exc:
        # Not documented to raise anything else, but a check must not be
        # the one route that can 500 -- see the module's "never a 500" rule.
        log.warning("mapping check failed unexpectedly for slug %r", slug, exc_info=True)
        return {
            "outcome": "error",
            "css_class": _CHECK_CSS_CLASS["error"],
            "episode_count": None,
            "message": f"SVT could not be checked: {exc}",
        }, None

    if not episodes:
        return {
            "outcome": "not_found",
            "css_class": _CHECK_CSS_CLASS["not_found"],
            "episode_count": 0,
            # Two likely causes and one unlikely one, and the unlikely
            # one is why the last sentence is here. An SVT schema change
            # now arrives as an `errors` block, which becomes the "error"
            # outcome above rather than this one -- but a *semantic*
            # change (episodes moving out of `associatedContent`) would
            # still empty this for a slug that is perfectly correct, and
            # in that outage the resolver goes quiet too, so the operator
            # checks every row and is told each slug is probably wrong.
            # Every row checking empty at once is the tell, and it points
            # away from the slugs.
            "message": (
                f"SVT returned no episodes for slug {slug!r}. The slug is "
                "probably wrong, or the show has ended and SVT is no "
                "longer offering it. If *every* mapping checks empty, "
                "suspect SVT rather than the slugs: svtplay-arr reads an "
                "undocumented SVT API, and a change at their end can empty "
                "this result for slugs that are all perfectly correct."
            ),
        }, None

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
    }, list(episodes)


async def _sonarr_test(
    probe, url: str, api_key: str, *, trusted_urls=(),
) -> dict:
    """The one computation behind the Test connection control.

    Both of its response paths -- the no-JS form POST that re-renders the
    settings view, and the JS fetch that patches one line -- call this and
    render its result verbatim, exactly as the mapping Check control does.

    **It tests the values in the form, not the values in the file**, and
    that is the deliberate choice. The operator clicks this having just
    typed a key, to find out whether it is right *before* saving it; the
    file cannot answer that until after a save, and since settings need a
    restart the file is not what the service is running either. An
    unmodified form renders exactly the effective on-disk values, so
    testing the form with nothing changed is testing the file. What the
    *running* service is using is a third question, and the background
    `SonarrCanary` is what answers it.

    The form already posts the key on every save, so reading it here is no
    new exposure.

    **`$SONARR_API_KEY` is substituted only for a URL this host is already
    configured for**, and `trusted_urls` is that set -- the URL the service
    booted with, and the one currently on disk. Both halves of that rule
    are load-bearing and neither is obvious:

    * *Substituting at all.* `Settings.load` gives the environment
      precedence over the file, so on such a deployment the typed key is
      not what any restart would use. Testing it would report on a value
      that can never take effect.
    * *Only for a configured URL.* The URL comes from the submitted form
      and the key would come from the environment, so an unrestricted
      substitution sends a secret the page deliberately never renders to
      whatever host the request body names -- immediately, with no config
      write, no restart and no log line carrying the value. There is no
      CSRF token on this form and no Origin check anywhere in this service
      (it relies entirely on the gateway in front of it), so that is
      reachable cross-site, and `SECURITY.md` publishes the opposite as a
      guarantee. A URL the operator has already committed to on this host
      is one the environment's key is sent to on every RSS poll already; a
      URL typed into a form and not saved is not.

    When the URL is not trusted the *submitted* key is tested instead, and
    the result says so plainly -- including that the environment's key, not
    this one, is what a restart would actually use against it.

    Read-only: two GETs against Sonarr and nothing else. It never touches
    config.yaml, mappings.yaml or the job store.

    Never raises. A check that could turn a page render into a 500 would be
    a worse control than no control.
    """
    if probe is None:
        return _sonarr_test_result(
            "unavailable",
            "Connection testing is not available in this deployment.",
        )

    url = (url or "").strip()
    env_key = (os.environ.get("SONARR_API_KEY") or "").strip()
    # Mirrors Settings.load and save_settings -- but only towards a host
    # this deployment is already configured to send that key to. See the
    # docstring; this one condition is the whole difference between a
    # convenience and an exfiltration route.
    trusted = bool(env_key) and _is_trusted_url(url, trusted_urls)
    key = env_key if trusted else (api_key or "").strip()
    used_env = trusted
    # The environment is set, and the operator is testing somewhere else.
    # The result has to say both that this is not the key that was sent and
    # that it is not the key a restart would use, or it is misleading in one
    # direction or the other.
    env_ignored = bool(env_key) and not trusted
    if not url or not key:
        # Refused before any request: there is nothing to test, and saying
        # "Sonarr rejected the key" about a blank field would send the
        # operator to Sonarr for a problem that is on this page.
        return _sonarr_test_result(
            "incomplete",
            "Fill in both the Sonarr URL and the API key, then test again. "
            "Nothing was sent."
            + (
                " The $SONARR_API_KEY value is only used to test the URL "
                "this service is already configured for, so it cannot "
                "stand in for an empty key against a different one."
                if env_ignored else ""
            ),
        )

    # Which key went out, said once and appended to every outcome that
    # actually sent one. A failure needs this at least as much as a success
    # does: "Sonarr rejected the API key" is misleading if the operator
    # believes the environment's key was the one tried.
    if used_env:
        key_note = (
            " (tested with $SONARR_API_KEY, which overrides the value in "
            "this form)"
        )
    elif env_ignored:
        key_note = (
            " (tested with the key in this form. $SONARR_API_KEY is set, and "
            "was not sent: it is only used for the Sonarr URL this service is "
            "already configured for, and this is a different one. Note that "
            "after a restart the environment's key is what would be used "
            "against this URL, not the one tested here.)"
        )
    else:
        key_note = ""

    try:
        status = await asyncio.wait_for(
            probe(url, key), timeout=_SONARR_TEST_TIMEOUT_S
        )
    except TimeoutError:
        # `asyncio.TimeoutError` is this same builtin on 3.11+. A Sonarr
        # that accepts the connection and then says nothing must not hold a
        # page render open -- this is the bound that stops it.
        return _sonarr_test_result(
            "failed",
            f"Sonarr did not answer within {_SONARR_TEST_TIMEOUT_S:g} seconds. "
            "It may be starting up, overloaded, or behind something that is "
            "silently dropping the connection." + key_note,
            reason="timeout",
        )
    except SonarrApiError as exc:
        # `str(exc)` is one of `sonarr.REASON_MESSAGES` -- a fixed literal
        # naming the failure shape, with no URL and no key in it. Nothing
        # here re-derives what went wrong; see sonarr.py.
        return _sonarr_test_result(
            "failed", str(exc) + key_note, reason=exc.reason
        )
    except Exception:
        # A check must not be the one control that can 500. This module's
        # own words rather than the exception's, so an unexpected type
        # cannot smuggle its message -- or anything it is carrying -- onto
        # the page.
        log.warning("Sonarr connection test failed unexpectedly", exc_info=True)
        return _sonarr_test_result(
            "failed",
            "The connection test failed unexpectedly. Check svtplay-arr's "
            "log." + key_note,
            reason=REASON_UNKNOWN,
        )

    count = getattr(status, "series_count", None)
    version = getattr(status, "version", None)
    which_key = key_note
    if count == 0:
        # Not a failure. A Sonarr with an empty library is correctly
        # configured -- but it is also what the wrong Sonarr looks like, so
        # the wording says both rather than picking one.
        message = (
            f"Sonarr {version} answered and accepted the key{which_key}, and "
            "reports 0 series in its library. That is correct for a new "
            "Sonarr, and is also what pointing at the wrong one looks like: "
            "there is nothing here for svtplay-arr to map to yet."
        )
    else:
        message = (
            f"Sonarr {version} answered and accepted the key{which_key}, and "
            f"reports {count} series in its library. Check that number "
            "against the Sonarr you meant -- a different Sonarr answers this "
            "just as happily."
        )
    return _sonarr_test_result(
        "ok", message, version=version, series_count=count
    )


def _is_trusted_url(url: str, trusted_urls) -> bool:
    """Is `url` one this deployment is already configured to talk to?

    Deliberately strict: whitespace and a trailing slash are normalised
    away (`SonarrClient` strips the latter itself, so the two spellings are
    genuinely the same host), and nothing else is. No case folding, no
    scheme or port defaulting, no DNS -- every one of those would widen the
    set of URLs the environment's key is handed to, and being wrong in that
    direction is the failure this function exists to prevent. Being wrong
    the other way costs nothing: the submitted key is tested instead, and
    the result says so.
    """
    needle = (url or "").strip().rstrip("/")
    if not needle:
        return False
    return any(
        needle == str(candidate or "").strip().rstrip("/")
        for candidate in trusted_urls
    )


def _sonarr_test_result(
    outcome: str, message: str, *, reason: str | None = None,
    version: str | None = None, series_count: int | None = None,
) -> dict:
    """One shape for every outcome, so the two response paths and the JS
    that consumes the JSON all branch on the same keys."""
    return {
        "outcome": outcome,
        "css_class": _SONARR_TEST_CSS_CLASS.get(outcome, "error"),
        # Which failure shape, for anything that wants to branch without
        # matching on prose. One of `sonarr.REASON_*`, or None on success.
        "reason": reason,
        "version": version,
        "series_count": series_count,
        "message": message,
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


def _recent_jobs(activity, limit: int = _STATUS_RECENT_LIMIT) -> list | None:
    """The Status view's bounded summary of finished jobs.

    Failures first, then everything else, each in the order the provider
    gave. Deliberately *not* simply "the most recent N": a failed grab
    that happened before five later successes is precisely the row an
    operator opens the landing view to find, and a straight recency cut is
    what would hide it behind them. The full list, in order, is one click
    away on the Activity view.

    `None` in, `None` out -- an unreadable store must stay distinguishable
    from a store with nothing in it all the way to the template.
    """
    if not isinstance(activity, dict):
        return None
    history = activity.get("history") or []
    failed = [j for j in history if j.get("status") == _FAILED]
    rest = [j for j in history if j.get("status") != _FAILED]
    return (failed + rest)[:limit]


def build_config_router(
    config_path: Path, mappings_path: Path, svt, sonarr, booted=None,
    status_provider=None, activity_provider=None,
    mapping_state_provider=None, sonarr_probe=None,
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

    `sonarr_probe`, if given, is `async (url, api_key) -> SonarrStatus`
    raising `SonarrApiError` -- what the Test connection button calls. A
    bare callable rather than a client, for the same reason as every other
    seam here: this module builds no HTTP client and holds no Sonarr
    knowledge of its own. Optional, and absent it the button is not
    rendered at all, because a control that cannot do anything is worse
    than no control (the same argument the Show/Hide button is built by
    script rather than by the server).

    `mapping_state_provider`, if given, is a zero-argument callable
    returning `SvtCanary.per_mapping()` -- one dict per mapping saying when
    it was last checked, when it last succeeded, how many episodes were
    seen and what the last error was. It is what makes a dead mapping
    visible on arrival in the Mappings view instead of something the
    operator has to go and press Check to discover.
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

    def _configured_sonarr_urls() -> tuple[str, ...]:
        """The Sonarr URLs this host has already committed to.

        The *only* consumer is the `$SONARR_API_KEY` substitution in
        `_sonarr_test`, and the whole point is to keep that key from being
        sent to a host named by a request body. Two entries, because both
        are values the operator wrote here themselves:

        * what the service booted with -- the environment's key already
          goes there on every RSS poll, so testing it adds no exposure;
        * what is on disk now -- which is what the settings form renders by
          default, so an unmodified form still tests the effective key, and
          what the next restart will use.

        Never raises: a config file that will not load contributes nothing,
        which fails towards testing the submitted key rather than towards
        sending the environment's somewhere new.
        """
        urls = []
        booted_url = getattr(booted, "sonarr_url", None)
        if booted_url:
            urls.append(str(booted_url))
        try:
            raw, _ = read_with_mtime(config_path)
            on_disk = raw.get("sonarr_url")
        except Exception:
            log.warning(
                "could not read %s while deciding whether the environment's "
                "Sonarr key may be used for a connection test; testing the "
                "submitted key instead",
                config_path,
                exc_info=True,
            )
            on_disk = None
        if on_disk:
            urls.append(str(on_disk))
        return tuple(urls)

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

    async def _status_strip_context() -> dict:
        """The `health`/`status_unavailable` pair the template needs.

        `await asyncio.to_thread(...)`, not a plain call, and that is the
        one interesting decision on this page. Every route here is
        `async def` (enforced by a test) because `JobStore` drives one
        `sqlite3.Connection` behind a blocking `threading.Lock` and a
        threadpool thread holding that lock stalls things it should not.
        But `async def` only moves the problem: `compute_health` reads
        `store.all_active()`, so calling it inline runs a blocking sqlite
        read *on the event loop* -- the same loop the download worker runs
        on. If the worker is mid-write and holding the lock, rendering this
        page stops the downloads it is reporting on.

        `to_thread` is what makes both rules true at once: the route stays
        a coroutine, and the blocking read happens on a worker thread,
        which is exactly what `check_same_thread=False` plus that lock
        exist to make safe. The cost is one thread hop per render, on a
        page a human loads by hand.

        (`compute_health` also reads `Task.done()` on the worker and canary
        tasks from that thread. That is a plain attribute read on an
        asyncio object -- it can be a moment stale, which for a liveness
        chip it already was, and it cannot tear.)

        Never raises: a broken status_provider must not take the whole
        config page down with it (the page must be at least as forgiving as
        `/health`, which has the same rule for its own internals). Rendering
        the page with no strip, or with an explicit "status unavailable", is
        always preferable to a 500 here.
        """
        if status_provider is None:
            return {"health": None, "status_unavailable": False}
        try:
            health = await asyncio.to_thread(status_provider)
        except Exception:
            log.exception(
                "status_provider failed; rendering the config page without "
                "the status strip"
            )
            return {"health": None, "status_unavailable": True}
        return {"health": health, "status_unavailable": False}

    async def _activity_context() -> dict:
        """What the job store currently holds, or why it could not be read.

        Three states, and the templates branch on all three, because
        collapsing any two of them is the specific defect this view exists
        to avoid:

        * a dict -- the store answered. Empty lists inside it genuinely
          mean nothing is in flight and nothing has finished.
        * `activity_unavailable` -- the read raised. Nothing is known.
          Rendering this as "nothing has happened" would tell an operator
          whose store is broken that no download has ever failed.
        * neither -- no provider was wired in at all (every router built
          without one, as most tests do). Also not "nothing happened".

        `to_thread` for the same reason as `_status_strip_context`, and
        more so: this is the larger of the two reads, and it is a second
        one on every render of the pages that show it.
        """
        if activity_provider is None:
            return {"activity": None, "activity_unavailable": False}
        try:
            activity = await asyncio.to_thread(activity_provider)
        except Exception:
            log.exception(
                "activity_provider failed; rendering the page with activity "
                "reported as unreadable rather than as empty"
            )
            return {"activity": None, "activity_unavailable": True}
        return {"activity": activity, "activity_unavailable": False}

    def _mapping_state_context() -> dict:
        """Per-mapping canary state, keyed by tvdb_id.

        In-memory in the canary, so no thread hop: this reads a dict the
        canary already built, it does not touch SVT or the disk. The Check
        control is still the only thing on this page that calls SVT.

        Same three states as `_activity_context`, and the same reason: a
        mapping with no state is rendered as "not checked yet", never as
        "fine". `SvtCanary.per_mapping` already returns a row for a mapping
        it has not reached, with `ok: None`; this guard covers the provider
        itself failing or being absent.
        """
        if mapping_state_provider is None:
            return {"mapping_states": None, "mapping_states_unavailable": False}
        try:
            rows = mapping_state_provider() or []
            states = {}
            for row in rows:
                try:
                    states[int(row["tvdb_id"])] = row
                except (KeyError, TypeError, ValueError):
                    continue
        except Exception:
            log.exception(
                "mapping_state_provider failed; rendering the mappings "
                "without their check state rather than as unchecked"
            )
            return {"mapping_states": None, "mapping_states_unavailable": True}
        return {"mapping_states": states, "mapping_states_unavailable": False}

    def _load_mappings() -> tuple[list, bool, str | None]:
        """The mapping rows, and whether the file could be read at all.

        Distinguishes "the file says there are no mappings" from "the file
        could not be read", which are the same empty list here and must
        never render as the same sentence: while this load fails the
        running service may still be serving a last known-good table, so
        telling the operator that nothing is offered to Sonarr can be
        false, and reads as an invitation to restart -- which is the one
        action that would make it true.
        """
        try:
            return MappingTable.load(mappings_path).all(), False, None
        except Exception as exc:
            log.warning(
                "mappings file %s is invalid; rendering the page with an "
                "error instead of failing the request",
                mappings_path,
                exc_info=True,
            )
            return [], True, f"{mappings_path} is invalid: {exc}"

    def _mappings_ever_loaded(health) -> bool | None:
        """Tri-state, and the templates branch on all three.

        "The service keeps serving its last good table" is true after a
        successful load and false on a fresh boot whose file was already
        broken -- where nothing was ever loaded, nothing is offered to
        Sonarr, and Sonarr will reject the indexer. Saying the reassuring
        thing there is the same defect as the "Nothing will be offered to
        Sonarr" row this replaced, merely inverted: it offers comfort
        exactly where urgency is needed.

        None means "no status dict to ask" -- no provider (as most routers
        are built), or one that raised. The page then hedges and asserts
        neither, because a last-good table that cannot be confirmed must
        never be claimed. Read off /health's own dict rather than
        recomputed here; this module renders health facts, it never
        derives them.
        """
        if isinstance(health, dict) and health.get("mappings_ever_loaded") is not None:
            return bool(health["mappings_ever_loaded"])
        return None

    async def _chrome(view: str, errors=None, notice=None) -> dict:
        """Everything base.html renders around a view's own content.

        The nav bar, the status strip, the canary's attention banner and
        the three message banners. Built once here rather than per view,
        so a view added later cannot arrive without a nav bar or without
        the strip.

        The pending-restart banner is deliberately on every view, not only
        on Settings. It says the running service is not using what is on
        disk, which is a fact about whether the service is working -- the
        question the Status view exists to answer.
        """
        status_context = await _status_strip_context()
        pending = _pending_restart_fields()
        pending_notice = (
            "Restart svtplay-arr to apply: "
            + ", ".join(f.label for f in pending)
            + ". The running service is still using the previous values."
        ) if pending else None
        messages = [e for e in (errors or []) if e]
        return {
            "view": view,
            "views": VIEWS,
            "error": "; ".join(messages) if messages else None,
            "notice": notice,
            "pending": pending_notice,
            **status_context,
        }

    async def _status_view(request: Request, error=None, notice=None):
        """The landing view: is it working?

        Everything on it is a fact somebody else computed -- /health's own
        dict, the canary's own per-mapping state, the job store's own rows.
        Nothing here derives a verdict of its own.
        """
        errors = [error] if error else []
        mappings, mappings_unavailable, load_error = _load_mappings()
        if load_error:
            errors.append(load_error)
        chrome = await _chrome("status", errors, notice)
        activity = await _activity_context()
        return _TEMPLATES.TemplateResponse(
            request,
            "status.html",
            {
                "mappings": mappings,
                "mappings_unavailable": mappings_unavailable,
                "mappings_ever_loaded": _mappings_ever_loaded(chrome.get("health")),
                "recent": _recent_jobs(activity["activity"]),
                "failed_status": _FAILED,
                **_mapping_state_context(),
                **activity,
                **chrome,
            },
        )

    async def _activity_view(request: Request, error=None, notice=None):
        chrome = await _chrome("activity", [error] if error else [], notice)
        activity = await _activity_context()
        return _TEMPLATES.TemplateResponse(
            request,
            "activity.html",
            {"failed_status": _FAILED, **activity, **chrome},
        )

    async def _mappings_view(
        request: Request, error=None, notice=None, check=None
    ):
        errors = [error] if error else []
        mappings, mappings_unavailable, load_error = _load_mappings()
        if load_error:
            errors.append(load_error)
        chrome = await _chrome("mappings", errors, notice)
        mappings_mtime = _mappings_mtime()
        return _TEMPLATES.TemplateResponse(
            request,
            "mappings.html",
            {
                "mappings": mappings,
                # True only when the load above raised, never when the file
                # legitimately holds `series: []` -- see the guard there.
                "mappings_unavailable": mappings_unavailable,
                # True / False / None; only consulted when the above is
                # True. See where it is computed for why None is a state.
                "mappings_ever_loaded": _mappings_ever_loaded(chrome.get("health")),
                "mappings_mtime": "" if mappings_mtime is None else mappings_mtime,
                # The sweep's own bounds, rendered rather than described
                # in prose: the page says what one Find mappings click
                # costs, and it cannot promise a bound this module does
                # not actually pass to `sweep_for_mappings`.
                "sweep_cap": _SWEEP_CAP,
                "sweep_request_budget": _SWEEP_REQUEST_BUDGET,
                # Set only by the no-JS Check form POST, and only for the
                # one row it was submitted for -- see the check route below.
                # No view ever fills this in itself and none of them calls
                # SVT: every GET, and every other POST, passes no `check` at
                # all, which is what keeps the Check control from ever
                # firing on a page load.
                "check": check,
                **_mapping_state_context(),
                **chrome,
            },
        )

    async def _settings_view(
        request: Request, error=None, notice=None, submitted=None,
        sonarr_test=None,
    ):
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
        chrome = await _chrome("settings", errors, notice)
        return _TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "sections": grouped_setting_fields(),
                "dangerous_fields": DANGEROUS_FIELDS,
                "values": values,
                "config_mtime": "" if config_mtime is None else config_mtime,
                # Drives the warning beside the API key field. Without it a
                # save on an env-overridden deployment looks like it worked.
                "env_overrides_api_key": _env_overrides_api_key(),
                # Whether to render the Test connection control at all. A
                # router built without a probe (as most tests are) gets a
                # page with no button rather than a button that cannot
                # answer.
                "sonarr_test_enabled": sonarr_probe is not None,
                # Set only by the no-JS test POST below. No view fills this
                # in itself and no GET passes one, which is what keeps the
                # control from ever firing on a page load.
                "sonarr_test": sonarr_test,
                **chrome,
            },
        )

    async def _check_match(mapping, episodes) -> Resolvability:
        """Can this mapping actually produce a grab? Never raises.

        The verdict is `canary.check_resolvability` -- the *same* function
        the background check calls -- so this control and the finding
        rendered beside it on the page cannot answer the same question
        differently. All that happens here is the one series lookup
        `Resolver.resolve` also makes, guarded and bounded.

        Read-only: `series_id_for_tvdb` and `episodes` are both GETs.
        """
        try:
            series_id = await asyncio.wait_for(
                sonarr.series_id_for_tvdb(mapping.tvdb_id),
                timeout=_CHECK_SONARR_TIMEOUT_S,
            )
        except TimeoutError:
            return Resolvability(
                None,
                UNDETERMINED_SONARR_UNAVAILABLE,
                f"Sonarr did not answer within {_CHECK_SONARR_TIMEOUT_S:g}s.",
            )
        except SonarrApiError as exc:
            # One of sonarr.REASON_MESSAGES: a fixed literal carrying no
            # URL and no API key. See that module's docstring.
            return Resolvability(
                None, UNDETERMINED_SONARR_UNAVAILABLE, str(exc)
            )
        except Exception:
            # This module's own words, not the exception's -- an
            # unexpected type must not be able to smuggle anything onto
            # the page it is rendered on.
            log.warning(
                "could not look up the Sonarr series for tvdb_id %s while "
                "checking a mapping", mapping.tvdb_id, exc_info=True,
            )
            return Resolvability(
                None,
                UNDETERMINED_SONARR_UNAVAILABLE,
                "Sonarr's series list could not be read. Check "
                "svtplay-arr's log.",
            )
        return await check_resolvability(
            sonarr,
            episodes,
            tvdb_id=mapping.tvdb_id,
            series_id=series_id,
            slug=mapping.svt_slug,
            # The tolerance the service actually booted with, so this
            # answers at the same air-date window the resolver will later
            # match at. A router built without `booted` (as tests do)
            # falls back to `Settings`' own default rather than to a
            # literal of its own -- the same rule the sweep follows.
            tolerance_days=_check_tolerance(),
            today=datetime.now(timezone.utc).date(),
            timeout_s=_CHECK_SONARR_TIMEOUT_S,
        )

    def _check_tolerance() -> int:
        configured = getattr(booted, "air_date_tolerance_days", None)
        return (
            Settings.air_date_tolerance_days if configured is None
            else configured
        )

    async def _check_context(tvdb_id: int) -> dict:
        """Look up the mapping for `tvdb_id` and check it, both halves.

        The one thing standing between the route handlers and the check:
        it turns a path parameter into the mapping the check wants, and
        handles the two ways that lookup itself can fail (an unreadable/
        invalid mappings.yaml, or a `tvdb_id` with no mapping) -- neither
        of which is a reason to call SVT, so this is also where "there is
        nothing to check" is decided before any network call happens.

        **Two halves, because the slug half alone reads as an all-clear.**
        Until 2026-08-28 this control answered only "does the slug still
        return episodes". A mapping can pass that on every press of its
        life and never produce a single grab -- `uppdrag-granskning`
        returns 61 episodes, none carrying an ordinal, so every one is
        refused. So an operator who saw "this mapping resolves nothing" on
        the page and pressed Check on that very row to investigate was
        told *SVT lists 61 episodes for slug 'uppdrag-granskning'*: two
        surfaces, one mapping, opposite answers, and the reassuring one
        was the one they asked for directly.

        That is the same defect shape as a Status view reading an
        unreadable canary as "nothing failing", and it is closed by
        sharing `canary.check_resolvability` rather than by wording -- two
        implementations of one verdict can drift, a shared one cannot.

        Never raises: a check must never turn into a 500.
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
                "resolves": None,
                "unresolvable_reason": None,
                "message": f"could not check: {mappings_path} is invalid: {exc}",
            }
        if mapping is None:
            return {
                "tvdb_id": tvdb_id,
                "outcome": "unknown_mapping",
                "css_class": _CHECK_CSS_CLASS["unknown_mapping"],
                "episode_count": None,
                "resolves": None,
                "unresolvable_reason": None,
                "message": f"No mapping exists for tvdb_id {tvdb_id}.",
            }
        result, episodes = await _check_slug(svt, mapping.svt_slug)
        # Tri-state, and present on every result so nothing rendering this
        # has to tell a missing key from an unanswered question.
        result = {
            "tvdb_id": tvdb_id,
            "resolves": None,
            "unresolvable_reason": None,
            **result,
        }
        if episodes is None:
            # The slug half already failed. There is nothing to compare,
            # and asking Sonarr would cost a request to learn nothing.
            return result
        return {**result, **_folded(await _check_match(mapping, episodes), result)}

    def _folded(verdict: Resolvability, result: dict) -> dict:
        """Fold the matching verdict into the slug half's result.

        One place, so the JSON and the rendered page cannot describe the
        same verdict differently, and so the colour and the wording are
        decided together rather than by two branches that can disagree
        about which is worse.
        """
        if verdict.is_finding:
            return {
                "outcome": "resolves_nothing",
                "css_class": _CHECK_CSS_CLASS["resolves_nothing"],
                "resolves": False,
                "unresolvable_reason": verdict.reason,
                # The finding leads. The slug fact follows as the reason
                # this was invisible, rather than as the headline it used
                # to be -- an operator who pressed Check on a row already
                # flagged as resolving nothing must not read the first
                # sentence and stop.
                "message": (
                    f"{verdict.note} The slug itself is fine -- SVT lists "
                    f"{result['episode_count']} episode"
                    f"{'s' if result['episode_count'] != 1 else ''} for it "
                    "-- which is exactly why this is invisible from the "
                    "slug alone."
                ),
            }
        if verdict.resolves:
            return {
                "resolves": True,
                "message": f"{result['message']} {verdict.note}",
            }
        # Undetermined. Sonarr not answering is a real gap and is coloured
        # as one; "nothing to compare yet" is not -- a control that cried
        # wolf over a series between seasons would be the same noise this
        # finding exists to avoid, on the button the operator pressed on
        # purpose. Either way the sentence says plainly that the second
        # half was not settled, so neither reads as an all-clear.
        unchecked = verdict.reason == UNDETERMINED_SONARR_UNAVAILABLE
        return {
            "outcome": "match_unchecked" if unchecked else result["outcome"],
            "css_class": (
                _CHECK_CSS_CLASS["match_unchecked"] if unchecked
                else result["css_class"]
            ),
            "message": (
                f"{result['message']} Whether its episodes can actually "
                f"match anything Sonarr has was not settled: {verdict.note}"
            ),
        }

    # `/config` keeps serving the entry point it always did; what changed
    # is which view it is. It is documented, deployed and the published SSO
    # resource points at it, so the other three views live beneath it
    # rather than beside it.
    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return await _status_view(request)

    @router.get("/mappings", response_class=HTMLResponse)
    async def mappings_page(request: Request):
        return await _mappings_view(request)

    @router.get("/activity", response_class=HTMLResponse)
    async def activity_page(request: Request):
        return await _activity_view(request)

    # Same path as the POST that writes it, so the form posts to where it
    # is served from and every existing `POST /config/settings` -- the
    # deployed one included -- is untouched.
    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return await _settings_view(request)

    @router.post("/settings", response_class=HTMLResponse)
    async def save(request: Request):
        form = dict(await request.form())
        raw_mtime = form.pop("expected_mtime", None)
        confirmed = str(form.pop("confirm_paths", "")) == "yes"
        submitted = {k: str(v) for k, v in form.items()}

        expected, mtime_error = _parse_expected_mtime(raw_mtime)
        if mtime_error is not None:
            return await _settings_view(
                request, error=mtime_error, submitted=submitted
            )

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
            return await _settings_view(
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
            return await _settings_view(
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
            return await _settings_view(
                request, error=str(exc), submitted=submitted
            )
        except ConfigError as exc:
            return await _settings_view(
                request, error=str(exc), submitted=submitted
            )
        except Exception as exc:
            # Never a 500 -- render the problem instead.
            log.exception("settings save failed")
            return await _settings_view(
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
            return await _settings_view(request, notice=default_notice)

        if not changed:
            notice = "Settings saved unchanged."
        else:
            names = ", ".join(f.label for f in changed)
            notice = (
                f"Settings saved. {names} changed; restart svtplay-arr to "
                "apply (mappings apply immediately; settings do not)."
            )
        return await _settings_view(request, notice=notice)

    @router.post("/settings/test", response_class=HTMLResponse)
    async def test_sonarr_connection(request: Request):
        """The Test connection control.

        Strictly on demand, and read-only: this route is the only place
        `sonarr_probe` is ever called, it writes nothing, and no GET can
        reach it.

        A second submit button in the settings form, via `formaction`, so
        with JavaScript off it is an ordinary form POST that re-renders the
        settings view with the result -- and with the operator's typed
        values still in the boxes, because they have not saved yet and this
        must not be the thing that loses their work.

        Two response shapes over one computation, exactly as the mapping
        Check control does it: a request asking for JSON (the fetch
        enhancement sets `Accept: application/json` itself) gets the result
        dict alone, anything else gets the whole page. Both render
        `_sonarr_test`'s return value verbatim.
        """
        form = dict(await request.form())
        submitted = {
            f.key: str(form[f.key]) for f in SETTING_FIELDS if f.key in form
        }
        result = await _sonarr_test(
            sonarr_probe,
            submitted.get("sonarr_url", ""),
            submitted.get("sonarr_api_key", ""),
            trusted_urls=_configured_sonarr_urls(),
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(result)
        return await _settings_view(
            request, submitted=submitted, sonarr_test=result
        )

    @router.get("/mappings/new", response_class=HTMLResponse)
    async def new_mapping(request: Request):
        return _TEMPLATES.TemplateResponse(
            request, "mapping_new.html", await _chrome("mappings")
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
            return await _mappings_view(request, error=error)
        try:
            svt_hits, sonarr_series, mappings_mtime, _rerun_error = (
                await _build_search_context(query)
            )
        except Exception:
            log.exception(
                "could not rebuild the search results after a failed "
                "mapping create; falling back to the index page"
            )
            return await _mappings_view(request, error=error)
        return _TEMPLATES.TemplateResponse(
            request,
            "mapping_search.html",
            {
                "query": query,
                "svt_hits": svt_hits,
                "sonarr_series": sonarr_series,
                "mappings_mtime": "" if mappings_mtime is None else mappings_mtime,
                # The operator's picks, so a duplicate-tvdb_id or similar
                # error lets them change just the one thing that was wrong
                # instead of re-selecting everything from scratch.
                "selected_svt": str(form.get("svt", "") or ""),
                "selected_svt_series_id": selected_svt_series_id,
                "selected_svt_slug": selected_svt_slug,
                "selected_sonarr": str(form.get("sonarr", "") or ""),
                # The search-results page is a Mappings-view page: it is
                # reached from there and it writes there, so the nav says so.
                **await _chrome("mappings", [error]),
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
                await _chrome("mappings", ["Enter a show title to search."]),
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
                "selected_svt": None,
                "selected_svt_series_id": None,
                "selected_svt_slug": None,
                "selected_sonarr": None,
                # The search-results page is where an operator troubleshoots
                # after a failed create, so it needs the same strip the
                # Status view has -- a degraded mappings table or a dead
                # worker is at least as relevant here as it is there.
                **await _chrome("mappings", [error]),
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
        return await _mappings_view(
            request,
            notice=f"Added {match.get('title')!r}. Mappings apply immediately.",
        )

    @router.post("/mappings/discover", response_class=HTMLResponse)
    async def discover(request: Request):
        """The Find mappings sweep.

        Walks Sonarr's library, asks SVT about each unmapped series and
        its alternate titles, corroborates the few most promising
        candidates against the series' own episodes, writes the rows
        `discovery.corroborated_match` approved, and surfaces everything
        else -- with its evidence -- for one click. A plain form POST with
        no JavaScript anywhere in the path: the sweep works with JS off,
        like every other control on this page.

        The order of operations is the safety argument, and each step
        exists because skipping it would allow a bad write:

        1. `expected_mtime` is parsed first, so a corrupted concurrency
           token costs SVT nothing -- the alternative is a full sweep
           followed by a refused write.
        2. mappings.yaml is loaded. If it will not parse, nothing is
           searched and nothing is written: without knowing what is already
           mapped, the sweep would re-search the whole library and could
           append a duplicate row on top of a file it could not read.
        3. The sweep runs and returns a value. It writes nothing itself, so
           a Sonarr or SVT outage part-way through leaves no partial file
           to clean up -- there is simply nothing to write.
        4. One atomic write for the whole batch, honouring the same
           concurrency check every other write route uses. Refused for any
           reason means refused entirely; the matches are then shown as
           found-but-not-saved rather than quietly dropped.

        `series_title` comes from Sonarr's own record inside the sweep and
        from nowhere else. Nothing in the submitted form reaches the file --
        the form carries only `expected_mtime`.

        Never a 500: every failure renders the config page with an error.
        """
        form = dict(await request.form())
        expected, mtime_error = _parse_expected_mtime(form.get("expected_mtime"))
        if mtime_error is not None:
            return await _mappings_view(request, error=mtime_error)

        try:
            existing = MappingTable.load(mappings_path).all()
        except Exception as exc:
            log.warning(
                "mappings file %s is invalid; refusing to sweep over it",
                mappings_path, exc_info=True,
            )
            return await _mappings_view(
                request,
                error=(
                    f"{mappings_path} is invalid: {exc}. Nothing was "
                    "searched and nothing was written -- fix the file first."
                ),
            )

        try:
            sweep = await sweep_for_mappings(
                sonarr, svt,
                # The whole table, not a set of tvdb ids: the sweep
                # derives both "already mapped, do not search" and
                # "already claims that SVT programme" from it, and a
                # caller must not be able to supply one and forget the
                # other.
                existing_mappings=existing,
                concurrency=_SWEEP_CONCURRENCY,
                cap=_SWEEP_CAP,
                request_budget=_SWEEP_REQUEST_BUDGET,
                # The tolerance the service actually booted with, so the
                # sweep corroborates at the same air-date window the
                # resolver will later match at. A router built without
                # `booted` (as tests do) passes None and the sweep falls
                # back to `Settings`' own default rather than to a literal
                # of its own.
                tolerance_days=getattr(booted, "air_date_tolerance_days", None),
            )
        except Exception as exc:
            log.warning("mapping sweep failed", exc_info=True)
            return await _mappings_view(
                request,
                error=f"Could not search for mappings ({exc}); nothing was written.",
            )

        written = ()
        write_error = None
        if sweep.confident:
            try:
                add_mappings(
                    mappings_path,
                    [
                        {
                            "tvdb_id": m.tvdb_id,
                            "svt_series_id": m.svt_series_id,
                            "svt_slug": m.svt_slug,
                            # Sonarr's own spelling, carried through the
                            # sweep untouched. Never SVT's name, and never
                            # anything from the form.
                            "series_title": m.series_title,
                            "source": SOURCE_AUTO,
                        }
                        for m in sweep.confident
                    ],
                    expected_mtime=expected,
                )
                written = sweep.confident
            except (MappingError, ConcurrentModification) as exc:
                write_error = str(exc)
            except Exception as exc:
                log.exception("mapping sweep write failed")
                write_error = f"could not save the mappings found: {exc}"

        # Re-read once, after the write. The accept buttons on the result
        # page carry this as their concurrency token, so it has to be the
        # file's mtime *now* -- seeded from before the sweep, every
        # one-click accept would be refused as a concurrent modification
        # caused by the sweep's own write.
        mtime_after_write = _mappings_mtime()
        return _TEMPLATES.TemplateResponse(
            request,
            "mapping_discover.html",
            {
                "sweep": sweep,
                # Deliberately separate from `sweep.confident`: what the
                # gate approved and what actually reached the file are two
                # facts, and after a refused write they differ. Saying
                # "mapped" for a batch that was rejected would be the worst
                # lie this page could tell.
                "written": written,
                "write_error": write_error,
                "mappings_mtime": (
                    "" if mtime_after_write is None else mtime_after_write
                ),
                # base.html renders the chrome's `error` as the page
                # banner from this same string; the template branches on
                # `write_error` to decide whether the confident matches are
                # shown as saved or as found-but-not-saved.
                **await _chrome("mappings", [write_error]),
            },
        )

    @router.post("/mappings/{tvdb_id}/delete", response_class=HTMLResponse)
    async def delete_mapping(request: Request, tvdb_id: int):
        form = dict(await request.form())
        expected, mtime_error = _parse_expected_mtime(form.get("expected_mtime"))
        if mtime_error is not None:
            return await _mappings_view(request, error=mtime_error)
        try:
            remove_mapping(
                mappings_path, tvdb_id,
                expected_mtime=expected,
            )
        except (MappingError, ConcurrentModification) as exc:
            return await _mappings_view(request, error=str(exc))
        except Exception as exc:
            log.exception("mapping delete failed")
            return await _mappings_view(
                request, error=f"could not remove mapping: {exc}"
            )
        return await _mappings_view(
            request, notice=f"Removed mapping for tvdbId {tvdb_id}."
        )

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
        return await _mappings_view(request, check=result)

    return router
