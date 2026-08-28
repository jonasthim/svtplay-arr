"""Client for the three Sonarr v3 endpoints this project talks to.

`/api/v3/series` and `/api/v3/episode` are what the resolver needs: Sonarr's
air dates are the only thing an SVT publication date can be matched against,
so without them nothing resolves and nothing is ever grabbed.

`/api/v3/system/status` is here for a different reason. Every other call in
this module is made on Sonarr's behalf, in the middle of serving a feed, and
its failure is indistinguishable from "no episodes matched" by the time
anyone looks. `status()` exists to be called *deliberately* -- by the
configuration page's Test connection button and by the background
`SonarrCanary` -- and to answer the four questions that failure could have
been: is anything there, is it Sonarr, does it accept this key, and is it
the Sonarr the operator meant.

**The series count is the answer to the last one.** Reachable, authenticated
and version-reporting are all satisfied by a Sonarr that is simply not the
one this service is supposed to feed -- a second instance, a test container,
a restored backup. Nothing but the size of the library it can see tells the
operator that apart, so `status()` pays for the extra request rather than
stopping at the cheap endpoint.

**Failures are classified, not just reported.** "Sonarr could not be
reached" covers a mistyped hostname, a wrong port, an unverifiable
certificate, a rejected key and a reverse proxy answering in Sonarr's place;
those five have nothing in common except that episodes stop arriving, and
five different things to go and change. `SonarrApiError.reason` is one of
the `REASON_*` values below, derived from the exception chain httpx hands
back, and every caller renders that rather than inventing its own reading.

**No message here ever interpolates the URL or the exception.** The API key
travels in an `X-Api-Key` header, and httpx's own exception strings do not
carry request headers -- but the `httpx.RequestError` hanging off the chain
holds the whole `Request`, headers included, so anything that renders an
httpx exception's `repr`, or reaches for `exc.request`, is one refactor away
from printing the key onto a page. The messages in `REASON_MESSAGES` are
fixed literals with nothing substituted into them but an HTTP status code,
so the guarantee holds by construction rather than by everyone remembering.
"""

import socket
import ssl
from dataclasses import dataclass
from datetime import date

import httpx

from svtplay_arr.models import SonarrEpisode

# Why a Sonarr call failed, in the terms that decide what the operator does
# next. These are the distinctions worth drawing because each one sends them
# somewhere different: the URL, the port, the certificate, the key, or
# whatever is answering in Sonarr's place.
#
# The URL is not an http(s) address at all -- httpx refuses before any
# connection is attempted. The likeliest typo of the lot: pasting
# "sonarr.lan:8989" straight out of a browser's address bar.
REASON_BAD_URL = "bad_url"
# The host does not resolve.
REASON_UNREACHABLE = "unreachable"
# It resolves, and nothing is listening on that port.
REASON_REFUSED = "refused"
# It resolves and answers, and the TLS handshake failed.
REASON_TLS = "tls"
# The connection could not be established and nothing in the exception chain
# says which of the three above it was. Its own reason rather than a guess:
# telling an operator the port refused them when the truth is unknown sends
# them to check something that may be perfectly correct.
REASON_CONNECT = "connect"
# It answers, eventually, but not soon enough.
REASON_TIMEOUT = "timeout"
# Sonarr answered and refused the key.
REASON_UNAUTHORIZED = "unauthorized"
# Something answered, and it does not behave like Sonarr's API.
REASON_NOT_SONARR = "not_sonarr"
# Sonarr answered with a status nothing above explains.
REASON_HTTP = "http"
# Anything else. Present so that an unclassified failure is still a
# classified *value*, rather than a `reason` of None that every caller has
# to guard.
REASON_UNKNOWN = "unknown"

# One sentence per reason, and the only thing any caller renders.
#
# Fixed literals on purpose. Nothing from the request is substituted in --
# not the URL (an operator who pasted a key into it would then have it
# rendered back onto the page and into the logs), and not the underlying
# exception (which chains to an `httpx.RequestError` carrying the request's
# headers). The one thing appended is an HTTP status code, which is an int.
REASON_MESSAGES: dict[str, str] = {
    REASON_BAD_URL: (
        "The Sonarr URL is not a usable http:// or https:// address, so no "
        "request was made. It needs the scheme as well as the host and port."
    ),
    REASON_UNREACHABLE: (
        "The host in the Sonarr URL could not be resolved. Check the "
        "hostname, and that this machine can resolve it -- a name that works "
        "in your browser does not always work from the server."
    ),
    REASON_REFUSED: (
        "The host was reached and refused the connection: nothing is "
        "listening on that port. Check the port, and that Sonarr is running."
    ),
    REASON_TLS: (
        "The TLS handshake failed -- Sonarr's certificate could not be "
        "verified. Check the certificate, or use the plain http:// address "
        "if Sonarr is not actually behind TLS."
    ),
    REASON_CONNECT: (
        "The connection to that address could not be established. Check the "
        "host and port, and anything between this machine and Sonarr -- a "
        "firewall, a container network, or a VPN that is down."
    ),
    REASON_TIMEOUT: (
        "Sonarr did not answer in time. It may be starting up, overloaded, "
        "or behind something that is silently dropping the connection."
    ),
    REASON_UNAUTHORIZED: (
        "Sonarr answered and rejected the API key. Copy it again from "
        "Sonarr's Settings > General; it is not the same value as any other "
        "*arr's key."
    ),
    REASON_NOT_SONARR: (
        "Something answered at that address, but it did not answer like "
        "Sonarr's API. Check that the URL points at Sonarr itself, including "
        "any base path, rather than at a proxy, a login page, or a different "
        "service sharing the port."
    ),
    REASON_HTTP: (
        "Sonarr answered with an unexpected HTTP status, so nothing could be "
        "read from it."
    ),
    REASON_UNKNOWN: (
        "The request to Sonarr failed for a reason this service does not "
        "recognise. Check svtplay-arr's log."
    ),
}


class SonarrApiError(RuntimeError):
    """Sonarr API request or response parsing failed.

    `reason` is one of the `REASON_*` values above and is always set --
    `REASON_UNKNOWN` where nothing better could be determined -- so a caller
    never has to distinguish "no reason" from "a reason of None".

    `status_code` is populated only when Sonarr (or whatever answered)
    produced an HTTP status, and is `None` for a failure that never got that
    far. Same contract as `SvtApiError.status_code`, for the same reason:
    this is the one module with HTTP knowledge about Sonarr, so no caller
    needs its own httpx unwrapping.

    The message never contains the API key. See the module docstring.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = REASON_UNKNOWN,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class SonarrStatus:
    """What one deliberate check of Sonarr learned.

    `series_count` is the field that matters: it is the only one of these
    that can tell the operator they are pointed at the *wrong* Sonarr rather
    than at no Sonarr.
    """

    version: str
    series_count: int


class SonarrClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient):
        self._base = base_url.rstrip("/")
        self._http = http
        self._headers = {"X-Api-Key": api_key}

    async def status(self) -> SonarrStatus:
        """Prove Sonarr is there, is Sonarr, accepts this key, and which one.

        Read-only, and deliberately two requests rather than one.
        `/api/v3/system/status` is the cheap half and settles reachable /
        authenticated / which version. It cannot settle *which Sonarr*, and
        that is the question a mistyped URL actually raises, so the series
        list is fetched too and only its length is kept.

        Raises `SonarrApiError` with a `reason` on every failure -- the same
        exception type the rest of this client raises, so nothing calling
        into Sonarr needs a second except clause.
        """
        body = await self._get_json("/api/v3/system/status")
        # A login page, a proxy error, another service on the port: all of
        # them can answer 200 with well-formed JSON. Sonarr's own status
        # always carries a version, so requiring one is what tells "the
        # right kind of thing answered" from "something answered".
        if not isinstance(body, dict) or not body.get("version"):
            raise _failure(REASON_NOT_SONARR)
        series = await self._get_json("/api/v3/series")
        if not isinstance(series, list):
            raise _failure(REASON_NOT_SONARR)
        return SonarrStatus(version=str(body["version"]), series_count=len(series))

    async def all_series(self) -> list[dict]:
        series = await self._get_json("/api/v3/series")
        if not isinstance(series, list):
            raise _failure(REASON_NOT_SONARR)
        return series

    async def series_id_for_tvdb(self, tvdb_id: int) -> int | None:
        for s in await self.all_series():
            if not isinstance(s, dict):
                continue
            if s.get("tvdbId") == tvdb_id:
                return s.get("id")
        return None

    async def episodes(self, series_id: int) -> list[SonarrEpisode]:
        """Every episode Sonarr knows about for a series.

        The RSS feed walks the match backwards -- it starts from an SVT
        episode and needs to find which (season, episode) Sonarr calls it --
        so it needs the whole list rather than one lookup.
        """
        payload = await self._get_json(
            "/api/v3/episode", params={"seriesId": series_id}
        )
        if not isinstance(payload, list):
            raise _failure(REASON_NOT_SONARR)

        out: list[SonarrEpisode] = []
        for e in payload:
            if not isinstance(e, dict):
                continue
            season = e.get("seasonNumber")
            number = e.get("episodeNumber")
            if not isinstance(season, int) or not isinstance(number, int):
                continue
            out.append(
                SonarrEpisode(
                    series_id=series_id,
                    season=season,
                    episode=number,
                    air_date=_parse_date(e.get("airDate")),
                    title=e.get("title") or "",
                )
            )
        return out

    async def episode(
        self, series_id: int, season: int, episode: int
    ) -> SonarrEpisode | None:
        """One episode, or None. A filter over `episodes()` rather than a
        second fetch-and-parse, so a Sonarr schema change has one place to
        be applied instead of two."""
        for e in await self.episodes(series_id):
            if e.season == season and e.episode == episode:
                return e
        return None

    async def _get_json(self, path: str, params: dict | None = None):
        """One GET, one classification, one exception type.

        Every Sonarr call goes through here so that the resolver, the config
        page and the background check all see the same `reason` for the same
        failure -- and so there is exactly one place where an httpx
        exception is turned into something renderable, which is what keeps
        the API key out of every message by construction.
        """
        try:
            r = await self._http.get(
                f"{self._base}{path}", headers=self._headers, params=params
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _failure(
                _status_reason(exc.response.status_code),
                status_code=exc.response.status_code,
            ) from exc
        except httpx.InvalidURL as exc:
            # Not an `httpx.HTTPError`, so it would otherwise escape this
            # client entirely and reach a caller as a bare exception.
            raise _failure(REASON_BAD_URL) from exc
        except httpx.HTTPError as exc:
            raise _failure(_transport_reason(exc)) from exc
        except ValueError as exc:
            # httpx does not funnel every malformed-URL failure through
            # `InvalidURL`: a base_url with no scheme can also surface as a
            # bare ValueError from deeper in the stack. It is the same
            # operator mistake, and this client's contract is that it raises
            # `SonarrApiError` and nothing else -- a bare ValueError reaching
            # a route would be the 500 this page is not allowed to produce.
            raise _failure(REASON_BAD_URL) from exc

        try:
            return r.json()
        except ValueError as exc:
            # A 200 that is not JSON is not Sonarr answering. Reported as
            # such rather than as a parse error, because "check what that
            # URL actually points at" is the action either way.
            raise _failure(REASON_NOT_SONARR) from exc


def _failure(reason: str, *, status_code: int | None = None) -> SonarrApiError:
    message = REASON_MESSAGES.get(reason, REASON_MESSAGES[REASON_UNKNOWN])
    if status_code is not None:
        message = f"{message} (HTTP {status_code})"
    return SonarrApiError(message, reason=reason, status_code=status_code)


def _status_reason(status_code: int) -> str:
    if status_code in (401, 403):
        return REASON_UNAUTHORIZED
    if status_code == 404:
        # Sonarr's own v3 API answers these paths. A 404 means whatever is
        # at that address does not have them -- a base path missing from the
        # URL, or a different service on the port.
        return REASON_NOT_SONARR
    return REASON_HTTP


def _transport_reason(exc: BaseException) -> str:
    """Read the shape of a transport failure off the exception chain.

    httpx collapses a refused port, an unresolvable name and an unverifiable
    certificate into one `ConnectError`, and only what it chains to tells
    them apart. Walking `__cause__`/`__context__` is how, and it is done
    here -- once -- rather than by each caller sniffing at message text.
    """
    if isinstance(exc, httpx.TimeoutException):
        return REASON_TIMEOUT
    if isinstance(exc, httpx.UnsupportedProtocol):
        return REASON_BAD_URL
    seen: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, ssl.SSLError):
            return REASON_TLS
        if isinstance(cursor, ConnectionRefusedError):
            return REASON_REFUSED
        if isinstance(cursor, socket.gaierror):
            return REASON_UNREACHABLE
        cursor = cursor.__cause__ or cursor.__context__
    if isinstance(exc, httpx.NetworkError):
        return REASON_CONNECT
    return REASON_UNKNOWN


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
