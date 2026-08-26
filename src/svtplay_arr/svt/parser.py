"""Parse an SVT Play show page into a list of episodes.

SVT Play show pages are Next.js apps: the `__NEXT_DATA__` script block is
empty because episode data loads client-side via a React Server Components
"flight" payload. That payload is itself a JSON string embedded in a
`<script>` tag, so its own quotes, slashes, and unicode characters are
backslash-escaped (`\\"`, `\\u002F`, ...) on top of the outer HTML/JSON
encoding. This module unescapes that text and regex-scans the result for
teaser objects, rather than trying to parse `__NEXT_DATA__` (which has
nothing in it) or the flight payload as structured JSON (it is not
well-formed on its own — it is interleaved with unrelated page chrome).
"""

import html as html_mod
import re
from datetime import date, timedelta

from svtplay_arr.models import SvtEpisode

# Each episode teaser in the flight payload looks like (whitespace added):
#   {"__typename":"Teaser","id":"...", ...
#     "heading":"...","subHeading":"...", ... "item":{"svtId":"...", ...
#     "urls":{"svtplay":"/video/...", ...}, ... "__typename":"Episode"},
#     "upcomingOverlay":null | {"heading":"Kommer", ...}}
# Unrelated linked content (other shows, singles) uses the same teaser shape
# but a different "__typename" (e.g. "Single"), so anchoring on
# '"__typename":"Episode"' excludes it.
#
# The payload is split on the teaser opener BEFORE matching, and _TEASER is
# run against one segment at a time. This is load-bearing, not tidiness: the
# pattern joins its anchors with `.*?` under re.S, and run against the whole
# page that lazy gap happily crosses an object boundary. The captured page
# has 104 `"heading":"` occurrences against only 43 teasers -- module
# headings, image descriptions and page chrome all use the same key -- so
# most headings have no `item` of their own and the scan runs on into the
# next teaser's. Measured before this split: match 0 spanned 5,261
# characters and married the page's show-hero heading to episode 1's svtId,
# giving KZmQ5JY episode 3's air date and no ordinal at all.
_TEASER_START = '{"__typename":"Teaser"'
_TEASER = re.compile(
    r'"heading":"(?P<heading>[^"]*)","subHeading":"(?P<sub>[^"]*)".*?'
    r'"item":\{"svtId":"(?P<svt_id>[A-Za-z0-9]+)".*?'
    r'"urls":\{"svtplay":"(?P<url>/video/[^"]+)".*?'
    r'"__typename":"Episode"',
    re.S,
)
# Availability is exactly `"upcomingOverlay":null` -- nothing else. The
# overlay's heading text is not a reliable signal: matching the literal
# "Kommer" missed `egWP26b` on the captured page, whose overlay heading is
# the weekday "Söndag" because it sits in the page's "id":"upcoming" /
# "name":"Kommande" module. That is the *next* episode, i.e. precisely the
# one a weekly grab asks for. Offering it means a guaranteed failed grab,
# and because release GUIDs are deliberately stable across searches, Sonarr
# blocklists that GUID and still has it blocklisted when the episode
# genuinely airs -- so the episode is lost permanently rather than merely
# retried late. 14 of the captured page's 43 teasers carry a non-null
# overlay; only 13 of them say "Kommer".
#
# The field is searched from the end of the teaser match, within this
# teaser's own segment (see _TEASER_START above), so it can never pick up
# a neighbouring teaser's overlay.
_UPCOMING_OVERLAY = re.compile(r'"upcomingOverlay":(?P<value>null|\{)')
_ORDINAL_TITLE = re.compile(r"(?:^|:\s*)(\d{1,3})[.\s]")
_ORDINAL_SLUG = re.compile(r"/(?:avsnitt-)(\d{1,3})(?:$|/)")
_DURATION = re.compile(r"(\d+)\s*min")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}


def parse_show_page(html: str, today: date | None = None) -> list[SvtEpisode]:
    """Extract episodes from an SVT Play show page.

    The page's __NEXT_DATA__ is empty; content arrives as an HTML/JSON-escaped
    flight payload, so this unescapes first and scans the result.

    `today` is the reference date used to resolve relative subheadings
    ("Igår"/"Idag") and to disambiguate the year for day+month subheadings
    that carry no year of their own. It defaults to `date.today()`; tests
    pass a fixed value so results stay deterministic regardless of when they
    run.
    """
    if today is None:
        today = date.today()
    text = html_mod.unescape(html).replace('\\"', '"').replace("\\u002F", "/")
    seen: set[str] = set()
    out: list[SvtEpisode] = []
    # [1:] drops everything before the first teaser opener (page chrome that
    # by definition belongs to no teaser).
    for segment in text.split(_TEASER_START)[1:]:
        m = _TEASER.search(segment)
        if m is None:
            continue
        svt_id = m.group("svt_id")
        if svt_id in seen:
            continue
        seen.add(svt_id)
        url = m.group("url")
        overlay = _UPCOMING_OVERLAY.search(segment, m.end())
        out.append(
            SvtEpisode(
                svt_id=svt_id,
                title=m.group("heading"),
                url=url,
                ordinal=_ordinal(m.group("heading"), url),
                published=_published(m.group("sub"), today),
                available=overlay is not None and overlay.group("value") == "null",
                duration_s=_duration(m.group("sub")),
            )
        )
    return out


def _ordinal(heading: str, url: str) -> int | None:
    m = _ORDINAL_SLUG.search(url)
    if m:
        return int(m.group(1))
    m = _ORDINAL_TITLE.search(heading)
    return int(m.group(1)) if m else None


_RELATIVE_DAYS = {"idag": 0, "igår": -1}


def _published(sub: str, today: date) -> date | None:
    lowered = sub.lower()

    # SVT renders the most recent episode's date as "Idag"/"Igår" instead of
    # a day+month pair, so those must be checked before (and instead of) the
    # day+month regex below.
    for word, offset in _RELATIVE_DAYS.items():
        if re.search(rf"\b{word}\b", lowered):
            return today + timedelta(days=offset)

    m = re.search(r"(\d{1,2})\s+([a-zåäö]{3})", lowered)
    if not m:
        return None
    month = _MONTHS.get(m.group(2))
    if month is None:
        return None
    day = int(m.group(1))
    year = today.year

    # Every date() below goes through _date_or_none. SVT's subheadings carry
    # no year, so the year is inferred -- and an inferred year can make a
    # real day+month pair impossible: "29 feb" read against a non-leap year
    # raises, as does rolling a leap day into a neighbouring non-leap year.
    # An unparseable subheading must cost that one date, not the entire page
    # parse: a raised ValueError here aborts parse_show_page for every
    # episode on the page and escapes SvtClient's SvtApiError contract as a
    # bare ValueError.
    candidate = _date_or_none(year, month, day)
    if candidate is None:
        # "29 feb" in a non-leap year. The nearest year that has the date at
        # all may be adjacent (2027 -> 2028); if neither neighbour works
        # either, there is no defensible answer and None already means
        # "unknown" to every caller.
        for adjacent in (year - 1, year + 1):
            rolled = _date_or_none(adjacent, month, day)
            if rolled is not None:
                return rolled
        return None

    if (candidate - today).days > 180:
        return _date_or_none(year - 1, month, day) or candidate
    if (today - candidate).days > 180:
        return _date_or_none(year + 1, month, day) or candidate
    return candidate


def _date_or_none(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _duration(sub: str) -> int | None:
    m = _DURATION.search(sub)
    return int(m.group(1)) * 60 if m else None
