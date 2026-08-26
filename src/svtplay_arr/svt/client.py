"""Client for the three SVT surfaces this project talks to.

This is the only module in the project that knows SVT exists: the Contento
GraphQL endpoint (series search), the show page HTML (episode listing,
delegated to `svtplay_arr.svt.parser`), and the per-video stream/quality
endpoint. An SVT API change should touch this file and nothing else.

Cache-busting quirk: the SVT CDN was observed on 2026-08-24 returning a
*well-formed* response body belonging to a *different* GraphQL query than
the one requested. A structural check (an `errors` block, or a missing
`data` block) does not catch this, because the swapped body is itself
perfectly well-formed JSON for some other query. So every GraphQL request
here carries a cache-buster query param *and* a per-request field alias
(`q<nonce>: search(...)`), and `_graphql` requires the response's `data`
block to echo that exact alias before trusting anything in it. A response
for the wrong query simply won't have the alias we asked for.

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
import re
import time
import uuid

import httpx

from svtplay_arr.models import QualityInfo, SvtEpisode, SvtSearchHit
from svtplay_arr.svt.parser import parse_show_page

GRAPHQL = "https://api.svt.se/contento/graphql"
VIDEO = "https://api.svt.se/video/{svt_id}"
SHOW = "https://www.svtplay.se/{slug}"
UA_PARAM = "svtplaywebb-play-render-prod-client"
BROWSER_UA = "Mozilla/5.0"

# Preferred HLS variant to resolve quality from; any other "hls*" format is
# an acceptable fallback (they all point at equivalent quality ladders).
_PREFERRED_HLS_FORMAT = "hls-cmaf-avc"

_TAGS = re.compile(r"<[^>]+>")
_STREAM_INF_LINE = re.compile(r"^#EXT-X-STREAM-INF:(.*)$", re.MULTILINE)
_RESOLUTION = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_AVERAGE_BANDWIDTH = re.compile(r"AVERAGE-BANDWIDTH=(\d+)")

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
        try:
            r = await self._http.get(
                SHOW.format(slug=slug),
                headers={"User-Agent": BROWSER_UA, "Cache-Control": "no-cache"},
                follow_redirects=True,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SvtApiError(
                f"show page request for {slug!r} failed",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise SvtApiError(f"show page request for {slug!r} failed") from exc
        return parse_show_page(r.text)

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
            "variables": json.dumps(variables),
            "cb": _cache_buster(),
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


def _alias() -> str:
    return f"q{uuid.uuid4().hex[:8]}"


def _cache_buster() -> str:
    """The SVT CDN was observed serving a cached body for a different query."""
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
