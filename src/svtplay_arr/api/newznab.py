"""Newznab-compatible indexer surface.

Sonarr talks to this module as if it were a Usenet indexer: `t=caps` to
discover search parameters, `t=tvsearch` to look for a release, and a GET
on the returned `<link>` to fetch an `.nzb` it hands to the download
client. This module speaks only Newznab/XML -- all matching decisions live
in `svtplay_arr.resolver.Resolver`; this module just shapes whatever the
resolver returns (or doesn't) into the protocol Sonarr expects.

Three things here are load-bearing:

- `supportedParams` on `tv-search` MUST include `tvdbid`. Without it Sonarr
  falls back to `q,rid,season,ep` and searches by title, and the whole
  design (which assumes a series is identified exactly by TVDB id) collapses
  into fuzzy Swedish-title matching.
- A resolver failure must produce an empty result set, never an HTTP 500.
  A 500 can make Sonarr disable the indexer entirely; an empty channel just
  means "nothing found this search," which Sonarr already handles.
- `q` is a *filter over the feed*, never a matcher. Every release it can
  return has already come back from `Resolver`, so filtering can only
  remove items; it can never introduce one, and it can never produce a
  title -- and therefore a filename -- a targeted search would not have
  produced. It is also subordinate to `tvdbid`: a targeted search answers
  from the id alone and ignores `q` entirely, because Sonarr sends the
  series' English title in `q` while the mapping table holds SVT's Swedish
  one, and filtering an exact answer by that guess would drop correct
  grabs.
- The `<link>`/`<enclosure url>` a search result carries is derived from the
  incoming request, never from configuration. Sonarr fetches that URL from
  its own container to get the `.nzb`; a configured host is a guess about
  what is reachable from over there, and the previous guess -- the service's
  own bind address, `0.0.0.0` -- resolved on Sonarr's side to its own
  loopback with nothing listening. Every grab died at the `.nzb` fetch with
  nothing in our logs, because the request never arrived. `request.base_url`
  is the host Sonarr just reached us on, so it is reachable by construction.
"""

import logging
from email.utils import format_datetime
from urllib.parse import urlencode
from xml.sax.saxutils import escape

from fastapi import APIRouter, Query, Request, Response

from svtplay_arr.naming import series_prefix

log = logging.getLogger(__name__)

_CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="svtplay-arr"/>
  <limits max="100" default="100"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,tvdbid,season,ep"/>
    <movie-search available="no" supportedParams="q"/>
  </searching>
  <categories>
    <category id="5000" name="TV"/>
  </categories>
</caps>
"""


def build_newznab_router(resolver, rss_window_days: int, mappings) -> APIRouter:
    """Build the `/api` router. `resolver` is any object with async
    `resolve(tvdb_id, season, episode) -> Release | None` and
    `recent(within_days, today=None) -> list[Release]` -- normally a
    `svtplay_arr.resolver.Resolver`, but tests may fake it.

    `rss_window_days` is required rather than defaulted: the default lives
    in `Settings` alone, so there is only one number to change.

    `mappings` is any object with `all() -> list[Mapping]` -- the same live
    table the resolver reads from, so a series added through the config
    page is findable by `q` with no restart. It is passed in rather than
    reached for through the resolver because this module must not depend on
    the resolver's internals; note it is used ONLY to read `series_title`
    for the text filter, never to decide what a search resolves to.
    """
    router = APIRouter(prefix="/api")

    @router.get("/")
    async def newznab(
        request: Request,
        t: str = Query(...),
        q: str | None = None,
        tvdbid: int | None = None,
        season: int | None = None,
        ep: int | None = None,
    ) -> Response:
        if t == "caps":
            return Response(content=_CAPS, media_type="application/xml")
        if t not in _SEARCH_FUNCTIONS:
            return Response(content=_feed([]), media_type="application/xml")
        # A blank `q` is no query at all, not a query that matches nothing.
        # Clients send a bare `&q=` on an unfiltered search, and reading
        # that as a filter would empty the channel -- which is what makes
        # Sonarr reject an indexer outright.
        query = (q or "").strip()
        targeted = tvdbid is not None and season is not None and ep is not None
        try:
            if targeted:
                # `q` is deliberately not consulted here; see the module
                # docstring. The id is exact, the text is a guess at it.
                release = await resolver.resolve(tvdbid, season, ep)
                releases = [] if release is None else [release]
            else:
                # A bare tvsearch is Sonarr's save-time indexer test AND its
                # RSS sync. Returning an empty channel here makes Sonarr
                # reject the indexer outright -- it cannot be added via the
                # UI at all -- so this must answer with real releases.
                releases = await resolver.recent(rss_window_days)
                if query:
                    # Narrowing an already-resolved feed. An empty result
                    # IS the right answer to an explicit query -- but only
                    # to an explicit one, which is why `query` is checked
                    # rather than `q`.
                    releases = _matching_query(releases, mappings, query)
        except Exception:
            # Fail safe, never 500: a 500 can make Sonarr disable the indexer.
            log.exception(
                "search failed (tvdb=%s season=%s ep=%s q=%r)",
                tvdbid, season, ep, q,
            )
            releases = []
        # str(request.base_url) always ends in "/"; _item joins with its own.
        base = str(request.base_url).rstrip("/")
        items = [_item(r, base) for r in releases]
        return Response(content=_feed(items), media_type="application/xml")

    @router.get("/nzb/{guid}")
    async def nzb(
        guid: str, svt_id: str, stem: str, quality: str, size: int = 0
    ) -> Response:
        return Response(
            content=_nzb(svt_id, stem, quality, size),
            media_type="application/x-nzb",
        )

    return router


# The two `t` values the caps document advertises as available="yes". A
# `t` outside this set answers with an empty channel, `q` or no `q`.
_SEARCH_FUNCTIONS = ("search", "tvsearch")


def _matching_query(releases: list, mappings, query: str) -> list:
    """The subset of `releases` belonging to a series whose mapped title
    contains `query`, case-insensitively.

    Matched against the mapping table's `series_title` rather than the
    release title because the two legitimately differ: a release title is
    also a filename, so `naming` strips characters like "?" from it. An
    operator searching for "Vem vet mest?" -- the spelling the config page
    shows them, and Sonarr's own -- would otherwise find nothing.

    Substring, case-insensitive, and nothing more. Fuzzy matching would be
    a second matching rule in a project that deliberately has exactly one,
    and it belongs nowhere near a module whose job is wire format.
    """
    needle = query.casefold()
    prefixes = tuple(
        series_prefix(m.series_title).casefold()
        for m in mappings.all()
        if needle in m.series_title.casefold()
    )
    if not prefixes:
        return []
    return [r for r in releases if r.title.casefold().startswith(prefixes)]


def _feed(items: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">\n'
        "<channel>\n<title>svtplay-arr</title>\n"
        + "".join(items)
        + "</channel>\n</rss>\n"
    )


def _item(release, base_url: str) -> str:
    link = f"{base_url}/api/nzb/{release.guid}?" + urlencode(
        {
            "svt_id": release.svt_id,
            "stem": release.title,
            "quality": release.quality,
            "size": release.size_bytes,
        }
    )
    return (
        "<item>\n"
        f"<title>{escape(release.title)}</title>\n"
        f'<guid isPermaLink="false">{escape(release.guid)}</guid>\n'
        f"<link>{escape(link)}</link>\n"
        f"<pubDate>{format_datetime(release.published)}</pubDate>\n"
        f'<enclosure url="{escape(link)}" length="{release.size_bytes}"'
        ' type="application/x-nzb"/>\n'
        '<newznab:attr name="category" value="5000"/>\n'
        f'<newznab:attr name="size" value="{release.size_bytes}"/>\n'
        "</item>\n"
    )


def _nzb(svt_id: str, stem: str, quality: str, size: int) -> str:
    """Well-formed NZB carrying the SVT id in its meta block.

    Sonarr writes this to disk and hands it to the download client, so it must
    be real NZB XML rather than an arbitrary blob.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n'
        "<head>\n"
        f'<meta type="svt_id">{escape(svt_id)}</meta>\n'
        f'<meta type="stem">{escape(stem)}</meta>\n'
        f'<meta type="quality">{escape(quality)}</meta>\n'
        f'<meta type="size">{int(size)}</meta>\n'
        "</head>\n</nzb>\n"
    )
