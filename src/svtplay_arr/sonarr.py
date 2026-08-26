from datetime import date

import httpx

from svtplay_arr.models import SonarrEpisode


class SonarrApiError(RuntimeError):
    """Sonarr API request or response parsing failed."""


class SonarrClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient):
        self._base = base_url.rstrip("/")
        self._http = http
        self._headers = {"X-Api-Key": api_key}

    async def all_series(self) -> list[dict]:
        try:
            r = await self._http.get(
                f"{self._base}/api/v3/series", headers=self._headers
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise SonarrApiError("series list request failed") from exc

        try:
            return r.json()
        except ValueError as exc:
            raise SonarrApiError("series list response was not valid JSON") from exc

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
        try:
            r = await self._http.get(
                f"{self._base}/api/v3/episode",
                headers=self._headers,
                params={"seriesId": series_id},
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise SonarrApiError("episode request failed") from exc

        try:
            payload = r.json()
        except ValueError as exc:
            raise SonarrApiError("episode response was not valid JSON") from exc

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


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
