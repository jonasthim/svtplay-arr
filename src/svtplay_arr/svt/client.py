"""Client for the two SVT surfaces this project talks to.

This is the only module in the project that knows SVT exists: the Contento
GraphQL endpoint (series search *and* episode listing) and the per-video
stream/quality endpoint. An SVT API change should touch this file and
nothing else.

Episode listing quirk: this used to scrape the SVT Play show page, regex-
scanning the escaped React flight payload it embeds, because
`__NEXT_DATA__` is empty and the payload is not well-formed JSON on its
own. `detailsPageByPath` returns every field `SvtEpisode` carries as a
typed value, for the same one GET, at roughly 7% of the bytes (11 KB
against 170 KB for a currently-airing show; 47 KB against 370 KB for one
with thirteen seasons). What that buys, beyond the bytes, is the failure
mode: a field that disappears from SVT's schema now comes back as an
`errors` block this module turns into `SvtApiError`, where a regex whose
anchor text drifted returned `[]` and said nothing at all. The one
observed cost is that an unknown slug answers HTTP 200 with a null page
instead of a 404, which `list_episodes` translates back -- see there.

Cache-busting quirk: the SVT CDN was observed on 2026-08-24 returning a
*well-formed* response body belonging to a *different* GraphQL query than
the one requested. A structural check (an `errors` block, or a missing
`data` block) does not catch this, because the swapped body is itself
perfectly well-formed JSON for some other query. So every GraphQL request
here carries a per-request field alias (`q<nonce>: search(...)`), and
`_graphql` requires the response's `data` block to echo that exact alias
before trusting anything in it. A response for the wrong query simply
won't have the alias we asked for.

The alias is *also* the cache-buster, and it travels in `variables`, not
as a query parameter. The CDN keys on `(path, ua, variables)` and on
nothing else -- see the comment in `_graphql` for the measurements. A `cb`
query param, which is what this module sent until 2026-08-28, is not part
of that key and never busted anything.

Quality resolution quirk: `/video/{svt_id}` does NOT carry `resolution` or
`bitrate` fields on its `videoReferences` entries (despite earlier
assumptions) -- only `format`, `url`, `resolve`, `redirect`. The actual
quality ladder lives in the HLS master playlist one of those entries points
to, as `#EXT-X-STREAM-INF` lines with `RESOLUTION=WxH` and
`AVERAGE-BANDWIDTH=<bits/s>`. `resolve_quality` fetches that manifest and
picks the highest-resolution rendition from it.

Duration quirk: the same `/video/{svt_id}` body carries an exact
`contentDuration` (seconds), already fetched for the HLS lookup above --
this is materially more accurate than the show page's coarse "N min"
subheading, which was observed drifting up to ~19% off on real episodes.
`resolve_quality` reads it and threads it onto `QualityInfo.duration_s` so
size estimation in `resolver.py` can prefer it over the page's estimate.

Slug quirk: `search_series` does not return the play-page slug -- only
`svtId`/`name`/`item.__typename`. `derive_slug` reproduces SVT's own
title-to-slug convention (casefold, fold the common Swedish/Latin
diacritics, apostrophes dropped rather than turned into a separator,
everything else collapsed to a single `-`) closely enough to save typing in
the common case. It is offered to callers as a suggestion only, never as a
source of truth -- a title with no derivable characters at all falls back to
a marker string rather than an empty slug, so a broken suggestion is visibly
broken instead of silently blank.
"""

import json
import logging
import re
import time
import uuid
from dataclasses import replace
from datetime import date, datetime

import httpx

from svtplay_arr.models import QualityInfo, SvtEpisode, SvtSearchHit

GRAPHQL = "https://api.svt.se/contento/graphql"
VIDEO = "https://api.svt.se/video/{svt_id}"
UA_PARAM = "svtplaywebb-play-render-prod-client"
BROWSER_UA = "Mozilla/5.0"

log = logging.getLogger(__name__)

# SVT files not-yet-published episodes in a selection of their own. This is
# one of the two independent availability signals; see
# `_episode_from_teaser`.
_UPCOMING_SELECTION = "upcoming"
_EPISODE = "Episode"

# Preferred HLS variant to resolve quality from; any other "hls*" format is
# an acceptable fallback (they all point at equivalent quality ladders).
_PREFERRED_HLS_FORMAT = "hls-cmaf-avc"

_TAGS = re.compile(r"<[^>]+>")
_STREAM_INF_LINE = re.compile(r"^#EXT-X-STREAM-INF:(.*)$", re.MULTILINE)
_RESOLUTION = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_AVERAGE_BANDWIDTH = re.compile(r"AVERAGE-BANDWIDTH=(\d+)")

# The ordinal rule, moved verbatim off the retired show-page scraper. SVT
# writes an episode's position either into the play URL (`/avsnitt-6`) or as
# a leading number in the teaser heading ("1. Tager du..?"), the latter
# optionally after a show-title prefix ("... XL: 3. Avslojandet").
_ORDINAL_TITLE = re.compile(r"(?:^|:\s*)(\d{1,3})[.\s]")
_ORDINAL_SLUG = re.compile(r"/(?:avsnitt-)(\d{1,3})(?:$|/)")

_SLUG_KEEP = "abcdefghijklmnopqrstuvwxyz0123456789"
_SLUG_FOLD = {"å": "a", "ä": "a", "ö": "o", "é": "e", "à": "a", "ü": "u"}
_SLUG_DROP = {"'", "’"}  # apostrophe / right single quote: dropped, not a separator
_NO_SLUG = "needs-manual-slug"


class SvtApiError(RuntimeError):
    """SVT returned something we could not trust or parse.

    `status_code` is populated only when the failure was an HTTP status
    (e.g. 404 for a slug that does not exist) and is `None` for every other
    cause -- a network failure, a timeout, or a malformed response all have
    no status code to report. This is the one place that knows how to pull
    a status out of the underlying httpx exception, so a caller (the config
    page's mapping check, currently the only one that cares) never needs
    its own httpx-specific unwrapping and this module stays the only place
    with SVT/HTTP knowledge.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SvtClient:
    def __init__(self, http: httpx.AsyncClient, ua_param: str = UA_PARAM):
        self._http = http
        self._ua_param = ua_param

    async def search_series(self, query: str) -> list[SvtSearchHit]:
        alias = _alias()
        raw_hits = await self._graphql(_search_query(alias), alias, {"q": query})
        hits: list[SvtSearchHit] = []
        for h in raw_hits or []:
            if not isinstance(h, dict):
                continue
            svt_id = h.get("svtId")
            name = h.get("name")
            item = h.get("item")
            typename = item.get("__typename") if isinstance(item, dict) else None
            if not svt_id or not name or not typename:
                continue  # malformed/promotional entry: skip rather than crash
            hits.append(
                SvtSearchHit(
                    svt_id=svt_id, name=_TAGS.sub("", name), typename=typename
                )
            )
        return hits

    async def list_episodes(self, slug: str) -> list[SvtEpisode]:
        """Every episode SVT currently lists for `slug`, available or not.

        One GET. An empty list is a legitimate answer -- a show whose run
        has ended returns `associatedContent: []` -- which is why the
        canary, not this, is what treats zero episodes as a failure.
        """
        alias = _alias()
        page = await self._graphql(
            _details_page_query(alias), alias, {"path": f"/{slug}"}
        )
        if page is None:
            # SVT answers an unknown path with HTTP 200 and a null page,
            # where the show page it replaced answered 404. The config
            # page's Check control branches on `status_code == 404` to tell
            # an operator their slug does not exist, so the shape callers
            # already depend on is reconstructed here rather than leaving
            # that branch unreachable.
            raise SvtApiError(
                f"SVT has no page for slug {slug!r}", status_code=404
            )
        if not isinstance(page, dict):
            raise SvtApiError(
                f"details page for {slug!r} was not an object: {type(page).__name__}"
            )
        return episodes_from_details_page(page)

    async def resolve_quality(self, svt_id: str) -> QualityInfo | None:
        try:
            r = await self._http.get(
                VIDEO.format(svt_id=svt_id),
                headers={"User-Agent": BROWSER_UA},
                params={"cb": _cache_buster()},
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise SvtApiError(f"video endpoint request for {svt_id!r} failed") from exc

        try:
            body = r.json()
        except ValueError as exc:
            raise SvtApiError("video endpoint response was not valid JSON") from exc

        duration_s = body.get("contentDuration")
        if not isinstance(duration_s, int):
            duration_s = None  # missing/malformed: caller falls back

        manifest_url = _pick_hls_url(body.get("videoReferences") or [])
        if manifest_url is None:
            return None  # no HLS rendition offered: fail safe, stay Wanted

        try:
            m = await self._http.get(manifest_url, headers={"User-Agent": BROWSER_UA})
            m.raise_for_status()
        except httpx.HTTPError:
            return None  # manifest unreachable: fail safe, stay Wanted

        return _best_quality(m.text, duration_s)

    async def _graphql(self, query: str, alias: str, variables: dict) -> object:
        params = {
            "ua": self._ua_param,
            "query": query,
            # The nonce rides *inside* `variables`, and the alias is what it
            # is set to. Measured live 2026-08-28: the CDN's cache key is
            # `(path, ua, variables)` -- the query document is not in it and
            # neither is a `cb` query parameter, so the one this used to
            # send bought exactly nothing. Two different query texts with
            # identical variables were served each other's bodies; a `cb`
            # param varied across three requests changed nothing; an extra,
            # *undeclared* variable (which the server silently ignores) got
            # a fresh body 3/3, against a fixed-value control that did not.
            #
            # Setting it to the alias rather than to a second independent
            # nonce is deliberate: the alias is already unique per request,
            # and putting it in the cache key means a cached body can only
            # ever be one that asked for the same alias -- so the echo check
            # below and the cache-buster become one mechanism instead of two
            # that have to agree. Before this, the buster did not work at
            # all and the echo check therefore failed *closed* on any repeat
            # of the same query within the 20s TTL: a spurious SvtApiError.
            "variables": json.dumps({**variables, "cb": alias}),
        }
        try:
            r = await self._http.get(
                GRAPHQL,
                params=params,
                headers={"User-Agent": BROWSER_UA, "Cache-Control": "no-cache"},
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise SvtApiError("GraphQL request failed") from exc

        try:
            payload = r.json()
        except ValueError as exc:
            raise SvtApiError("GraphQL response was not valid JSON") from exc

        if not isinstance(payload, dict):
            raise SvtApiError("GraphQL response was not a JSON object")
        if "errors" in payload:
            raise SvtApiError(str(payload["errors"]))
        data = payload.get("data")
        if not isinstance(data, dict) or alias not in data:
            # The CDN was observed serving a well-formed body for a
            # *different* query. Requiring our own alias to be echoed back
            # is what catches that, where checking for `data`/`errors`
            # alone would not.
            raise SvtApiError(
                f"response did not echo requested alias {alias!r}; "
                "possible stale/mismatched CDN body"
            )
        return data[alias]


def _search_query(alias: str) -> str:
    return (
        "query($q: String!){"
        + alias
        + ": search(query:$q){svtId name title episodeTitle isGenre "
        "item{__typename}}}"
    )


def _details_page_query(alias: str) -> str:
    """The show's episode listing, as one `detailsPageByPath` document.

    Every field here is read by `episodes_from_details_page`; nothing is
    requested speculatively. In particular `item.number` is *not* asked
    for. It exists, it is populated for every episode of shows the old
    scraper could not derive an ordinal for at all, and adopting it is the
    obvious next step -- but it is also populated for specials, where the
    scraper correctly produced `None`, and `resolver.py::_recent_for`
    states that as an assumption it relies on. Changing the ordinal is a
    safety decision that deserves its own change and its own tests, not a
    side effect of changing transport. So the ordinal keeps coming from
    `heading` and `urls.svtplay` via `_ordinal`, exactly as before.

    `exclude: [clips, related]` rather than `include:` because `include`
    and `addExtras` cannot be combined ("addExtras and include cannot be
    combined"), and `addExtras: [upcoming]` is not optional: without it
    `associatedContent` returns only *available* episodes, and the
    unavailable ones simply vanish -- which would make an unaired episode
    indistinguishable from one SVT has never mentioned rather than
    something to refuse.

    `upcomingOverlay` is selected for its `heading` only because GraphQL
    requires a selection set on an object type; the text is never read.
    Null versus non-null is the whole signal, and matching the text is the
    bug this replaced (SVT labels the *imminent* episode by weekday, not
    "Kommer").
    """
    return (
        "query($path: String!){" + alias + ": detailsPageByPath(path:$path){"
        "associatedContent(exclude:[clips,related],addExtras:[upcoming]){"
        "selectionType "
        "items{heading upcomingOverlay{heading} "
        "item{__typename ... on Episode{"
        "svtId duration validFrom urls{svtplay}}}} "
        "itemsPaginated(pagination:{limit:1,offset:0}){totalSize}"
        "}}}"
    )


def episodes_from_details_page(page: dict) -> list[SvtEpisode]:
    """Build the episode list from a `detailsPageByPath` body.

    Split out from `list_episodes` so the mapping can be exercised against
    captured responses without a transport in the way -- which is what the
    differential test against the retired scraper's recorded output does.
    """
    by_id: dict[str, SvtEpisode] = {}
    for selection in page.get("associatedContent") or []:
        if not isinstance(selection, dict):
            continue
        _refuse_a_truncated_selection(selection)
        upcoming = selection.get("selectionType") == _UPCOMING_SELECTION
        for teaser in selection.get("items") or []:
            episode = _episode_from_teaser(teaser, upcoming)
            if episode is None:
                continue
            already = by_id.get(episode.svt_id)
            if already is None:
                by_id[episode.svt_id] = episode
            elif already.available and not episode.available:
                # An episode listed in two selections is unavailable if it
                # is unavailable in either. First-wins would decide this by
                # document order, and seasons come before "Kommande" -- so
                # first-wins resolves it the dangerous way, and the cost of
                # offering an unaired episode is a stable GUID blocklisted
                # before it airs and never retried.
                by_id[episode.svt_id] = replace(already, available=False)
    return list(by_id.values())


def _refuse_a_truncated_selection(selection: dict) -> None:
    """A short selection must be an error, never a short list.

    Measured 2026-08-28 across four shows and 47 selections: `items`
    returns each one whole, and its length equals `itemsPaginated.
    totalSize` every time -- 119 episodes of one show arrived in a single
    response. If SVT ever imposes a default cap, the episodes past it
    become unreachable with nothing to say so, which is exactly the silent
    miss the canary was built for and cannot see.

    A *missing* `totalSize` is logged, not raised, and the asymmetry is
    deliberate. The failure this guards against is per-selection and
    partial: some episodes of one show go missing, and the rest still
    work. This guard's own failure would be global and total -- if SVT
    makes `totalSize` nullable or moves `itemsPaginated`, then every
    selection of every mapping raises, `list_episodes` fails everywhere at
    once, the feed empties, and Sonarr rejects the indexer outright for
    returning no results in the configured categories. This project has
    shipped an empty feed once already and it is much the worse outcome:
    nothing is grabbed, and the operator can do nothing about the cause. A
    total we cannot read costs a log line; only a total that disagrees
    costs the request.
    """
    items = selection.get("items")
    paginated = selection.get("itemsPaginated")
    total = paginated.get("totalSize") if isinstance(paginated, dict) else None
    if not isinstance(total, int):
        log.warning(
            "SVT details page selection carried no itemsPaginated.totalSize, "
            "so a truncated selection can no longer be told from a complete "
            "one; taking its %d item(s) as complete",
            len(items or []),
        )
        return
    if len(items or []) != total:
        raise SvtApiError(
            f"details page selection returned {len(items or [])} of "
            f"{total} items; SVT appears to be paginating"
        )


def _episode_from_teaser(teaser: object, upcoming_selection: bool) -> SvtEpisode | None:
    """One teaser as an `SvtEpisode`, or None if it is not an episode.

    Trailers, singles and linked series share the teaser shape, so
    `__typename` is what excludes them -- a field comparison where the
    scraper anchored a regex on the same string appearing as text.

    Availability is over-determined on purpose. `upcoming_selection` and a
    non-null `upcomingOverlay` are independent signals for the same fact,
    and SVT would have to break both at once to hand back an unaired
    episode as available. The old scraper had one anchor.
    """
    if not isinstance(teaser, dict):
        return None
    item = teaser.get("item")
    if not isinstance(item, dict) or item.get("__typename") != _EPISODE:
        return None
    svt_id = item.get("svtId")
    urls = item.get("urls")
    url = urls.get("svtplay") if isinstance(urls, dict) else None
    if not svt_id or not url:
        return None  # malformed entry: skip it rather than fail the show
    heading = teaser.get("heading") or ""
    duration = item.get("duration")
    return SvtEpisode(
        svt_id=svt_id,
        title=heading,
        url=url,
        ordinal=_ordinal(heading, url),
        published=_published(item.get("validFrom")),
        available=not upcoming_selection and teaser.get("upcomingOverlay") is None,
        duration_s=duration if isinstance(duration, int) else None,
    )


def _ordinal(heading: str, url: str) -> int | None:
    """SVT's position within its own run -- never a season number.

    Unchanged, deliberately, from the scraper this replaced: the same rule
    over the same two strings, both of which the details page carries. The
    ordinal is what disambiguates when SVT labels a run "Sasong 14" that
    TVDB calls season 15 and two episodes share an air date, so it is one
    of the resolver's two signals and moving it would move what matches.
    """
    m = _ORDINAL_SLUG.search(url)
    if m:
        return int(m.group(1))
    m = _ORDINAL_TITLE.search(heading)
    return int(m.group(1)) if m else None


def _published(valid_from: object) -> date | None:
    """The publication date, in SVT's own timezone.

    `validFrom` is ISO 8601 carrying SVT's offset, e.g.
    `2026-08-23T02:00:00+02:00`. The date is taken from the offset-aware
    value as-is: normalising to UTC first would move an episode published
    at `00:30+02:00` onto the previous day, and the air date is one of the
    resolver's two signals.
    """
    if not isinstance(valid_from, str):
        return None
    try:
        return datetime.fromisoformat(valid_from).date()
    except ValueError:
        return None


def _alias() -> str:
    return f"q{uuid.uuid4().hex[:8]}"


def _cache_buster() -> str:
    """A per-request nonce for the `/video/{svt_id}` REST endpoint.

    GraphQL does not use this: its cache key is `(path, ua, variables)`, so
    its nonce has to be a variable and the alias serves as one. See
    `_graphql`.
    """
    return str(int(time.time() * 1000))


def derive_slug(name: str) -> str:
    """SVT's conventional URL slug for a show title.

    A convenience only: a caller-facing form is expected to let the value be
    corrected, and show it before saving. Never returns an empty string --
    a title with no derivable characters at all (no ASCII letters/digits and
    no diacritic in the fold table) yields `_NO_SLUG`, a marker that is
    visibly wrong rather than a blank that would silently pass through.
    """
    out = []
    for ch in name.casefold():
        if ch in _SLUG_DROP:
            continue
        ch = _SLUG_FOLD.get(ch, ch)
        if ch in _SLUG_KEEP:
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or _NO_SLUG


def _pick_hls_url(refs: list) -> str | None:
    preferred = None
    fallback = None
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        fmt = ref.get("format")
        url = ref.get("url")
        if not fmt or not url:
            continue
        if fmt == _PREFERRED_HLS_FORMAT and preferred is None:
            preferred = url
        elif fmt.startswith("hls") and fallback is None:
            fallback = url
    return preferred or fallback


def _best_quality(manifest: str, duration_s: int | None) -> QualityInfo | None:
    best = None  # (height, bitrate_kbps)
    for attrs in _STREAM_INF_LINE.findall(manifest):
        res = _RESOLUTION.search(attrs)
        bw = _AVERAGE_BANDWIDTH.search(attrs)
        if res is None or bw is None:
            continue
        height = int(res.group(2))
        bitrate_kbps = int(bw.group(1)) // 1000
        if best is None or height > best[0]:
            best = (height, bitrate_kbps)
    if best is None:
        return None
    height, bitrate_kbps = best
    return QualityInfo(
        label=f"WEBDL-{height}p",
        height=height,
        bitrate_kbps=bitrate_kbps,
        duration_s=duration_s,
    )
