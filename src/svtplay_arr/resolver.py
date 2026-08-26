"""Maps Sonarr's (tvdb_id, season, episode) onto exactly one SVT video.

This is the only module in the project that makes matching decisions.
Every rule here exists because of a real trap observed in live data on
2026-08-24 (see the traps documented in each branch below), and the design
is deliberately strict: return None on any doubt. A wrong answer here does
not fail safe on retry -- Sonarr runs with renameEpisodes=False, so the
title `resolve()` returns becomes the permanent filename in the media
library. Some episodes will stay Wanted and need manual intervention; that
is the intended trade, not a bug to be optimised away.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone

from svtplay_arr.models import QualityInfo, Release, SvtEpisode
from svtplay_arr.naming import release_guid, release_title
from svtplay_arr.sonarr import SonarrApiError
from svtplay_arr.svt.client import SvtApiError

log = logging.getLogger(__name__)

# Bitrate (kbps) -> bytes, used to advertise a plausible size to Sonarr.
_BYTES_PER_KBIT = 1000 / 8

# Used only when neither the video endpoint's exact contentDuration nor the
# show page's coarse "N min" subheading is available. A conservative,
# clearly-nonzero stand-in so the release isn't silently advertised at 0
# bytes (Sonarr uses size in quality-profile decisions) -- 30 minutes is a
# deliberately modest floor, not a guess at the real runtime.
_NOMINAL_DURATION_S = 1800


class Resolver:
    """Maps (tvdb_id, season, episode) onto one SVT video, or nothing.

    Returns None on any doubt. A wrong answer writes a permanently wrong
    filename into /mnt/tv, because renameEpisodes=False.
    """

    def __init__(self, mappings, sonarr, svt, tolerance_days: int = 1):
        self._mappings = mappings
        self._sonarr = sonarr
        self._svt = svt
        self._tolerance = tolerance_days

    async def resolve(
        self, tvdb_id: int, season: int, episode: int
    ) -> Release | None:
        mapping = self._mappings.for_tvdb(tvdb_id)
        if mapping is None:
            log.info("no mapping for tvdb %s", tvdb_id)
            return None

        try:
            series_id = await self._sonarr.series_id_for_tvdb(tvdb_id)
            if series_id is None:
                log.info("sonarr has no series for tvdb %s", tvdb_id)
                return None
            wanted = await self._sonarr.episode(series_id, season, episode)
        except SonarrApiError as exc:
            log.warning(
                "sonarr lookup failed for tvdb %s S%02dE%02d: %s",
                tvdb_id, season, episode, exc,
            )
            return None
        if wanted is None:
            log.info(
                "sonarr has no episode S%02dE%02d for series %s; "
                "possible mapping/season mismatch",
                season, episode, series_id,
            )
            return None
        if wanted.air_date is None:
            log.info(
                "S%02dE%02d has no confirmed air date yet, nothing to match",
                season, episode,
            )
            return None

        try:
            svt_episodes = await self._svt.list_episodes(mapping.svt_slug)
        except SvtApiError as exc:
            log.warning(
                "svt episode list failed for slug %r: %s", mapping.svt_slug, exc
            )
            return None

        candidates = [
            e for e in svt_episodes if self._matches(e, wanted.air_date, episode)
        ]
        if len(candidates) != 1:
            log.info(
                "S%02dE%02d: %d candidates, refusing to guess",
                season, episode, len(candidates),
            )
            return None

        chosen = candidates[0]
        try:
            quality = await self._svt.resolve_quality(chosen.svt_id)
        except SvtApiError as exc:
            log.warning(
                "quality resolution failed for svt_id %r: %s", chosen.svt_id, exc
            )
            return None
        if quality is None:
            log.info("no resolvable quality for svt_id %r", chosen.svt_id)
            return None

        title = release_title(
            mapping.series_title, season, episode, quality.label, wanted.title
        )
        return Release(
            guid=release_guid(chosen.svt_id, quality.label),
            title=title,
            svt_id=chosen.svt_id,
            quality=quality.label,
            size_bytes=self._estimate_size(chosen, quality),
            # The episode's own publication instant, not the fetch time:
            # pubDate drives Sonarr's age column, and a non-zero indexer
            # "Minimum Age" would otherwise reject every release forever.
            published=datetime.combine(
                chosen.published, time.min, tzinfo=timezone.utc
            ),
        )

    async def recent(
        self, within_days: int, today: date | None = None
    ) -> list[Release]:
        """Releases for episodes SVT published recently, across every mapping.

        This backs a bare `t=tvsearch` -- what Sonarr sends both for its
        save-time indexer test and for every RSS sync. Returning an empty
        channel made Sonarr reject the indexer outright, so it could not be
        added through the UI at all.

        Two matching rules are involved, and it is worth being precise about
        which is which. The *forward* rule lives in `resolve()` and is not
        duplicated here -- every release this returns comes from it, so the
        title and GUID are identical to a targeted search for the same
        episode. But finding which `(season, episode)` an SVT episode is
        requires a second, *reverse* rule, implemented in `_recent_for`.
        `resolve()` cannot check that one, because by the time it runs the
        season has already been chosen. The reverse rule therefore carries
        its own assumptions, stated at its use site.
        """
        today = today or date.today()
        cutoff = today - timedelta(days=within_days)
        # Memoize the read-only lookups for this sweep. Each candidate goes
        # through a full `resolve()`, which re-fetches the 171 KB show page
        # and Sonarr's series and episode lists -- data this method already
        # holds. Sonarr polls RSS every few minutes against an unofficial
        # API, so re-fetching it per candidate is the kind of traffic that
        # starts to look like scraping as the window widens.
        sweep = Resolver(
            self._mappings,
            _Memoized(self._sonarr, ("series_id_for_tvdb", "episodes", "episode")),
            _Memoized(self._svt, ("list_episodes",)),
            self._tolerance,
        )
        out: list[Release] = []
        for mapping in self._mappings.all():
            try:
                out.extend(await sweep._recent_for(mapping, cutoff, today))
            except Exception:
                # One broken mapping must not empty the whole feed -- an
                # empty feed is what Sonarr rejects an indexer over.
                log.exception(
                    "recent feed failed for tvdb %s; skipping that series",
                    mapping.tvdb_id,
                )
        return _capped(_deduped(out))

    async def _recent_for(
        self, mapping, cutoff: date, today: date
    ) -> list[Release]:
        svt_episodes = await self._svt.list_episodes(mapping.svt_slug)
        fresh = [
            e
            for e in svt_episodes
            if e.available
            and e.ordinal is not None
            and e.published is not None
            # Bounded at both ends. `available` already excludes upcoming
            # episodes, but "never offer an upcoming episode" is the
            # invariant with the worst consequence in this project -- a
            # failed grab blocklists a stable GUID, and it is still
            # blocklisted when the episode really airs -- so the date bound
            # is free defence in depth behind it.
            and cutoff <= e.published <= today
        ]
        if not fresh:
            return []

        series_id = await self._sonarr.series_id_for_tvdb(mapping.tvdb_id)
        if series_id is None:
            log.info("sonarr has no series for tvdb %s", mapping.tvdb_id)
            return []
        sonarr_episodes = await self._sonarr.episodes(series_id)

        out: list[Release] = []
        for svt_ep in fresh:
            # The reverse rule, and its assumption: an SVT ordinal always
            # denotes an episode of a numbered run, never a Sonarr special.
            # Season 0 is excluded because a special dated alongside the run
            # satisfies both signals -- same ordinal, same air date -- and
            # would claim the episode, writing a permanent S00Exx filename
            # for something that is really S15Exx. SVT-side specials cannot
            # reach here anyway: they parse with ordinal=None.
            wanted = [
                se
                for se in sonarr_episodes
                if se.season > 0
                and se.air_date is not None
                and se.episode == svt_ep.ordinal
                and abs((svt_ep.published - se.air_date).days) <= self._tolerance
            ]
            if len(wanted) != 1:
                log.info(
                    "svt_id %r: %d Sonarr episodes match, refusing to guess",
                    svt_ep.svt_id, len(wanted),
                )
                continue
            try:
                release = await self.resolve(
                    mapping.tvdb_id, wanted[0].season, wanted[0].episode
                )
            except Exception:
                # Per candidate, not per mapping: with a single mapping
                # configured, discarding the whole series' results is the
                # same as returning an empty feed.
                log.exception(
                    "resolve failed for svt_id %r; skipping that candidate",
                    svt_ep.svt_id,
                )
                continue
            if release is None:
                continue
            if release.svt_id != svt_ep.svt_id:
                # Holds structurally today -- svt_ep is in resolve()'s own
                # candidate set, so a unique forward match must be svt_ep.
                # Asserting it makes the coupling explicit and catches page
                # drift between the two list_episodes calls.
                log.warning(
                    "reverse match disagreed: started from %r, resolve gave %r",
                    svt_ep.svt_id, release.svt_id,
                )
                continue
            out.append(release)
        return out

    def _matches(self, ep: SvtEpisode, air_date, episode_number: int) -> bool:
        # Signal 0: it must actually be downloadable. 14 episodes on the
        # same listing page were flagged upcoming (a non-null
        # `upcomingOverlay`), including the *next* one; offering any of them
        # is a guaranteed failed grab, and a failed grab is permanent
        # because the release GUID is stable across searches.
        if not ep.available:
            return False
        # Signal 1: air date agreement, within tolerance.
        if ep.published is None:
            return False
        if abs((ep.published - air_date).days) > self._tolerance:
            return False
        # Signal 2: SVT's own ordinal within its run. NEVER SVT's season
        # number -- SVT labelled a run "Sasong 14" that Sonarr/TVDB call
        # season 15, and two episodes shared an air date (S15E01, S15E02
        # both 2026-08-23), so ordinal is what actually disambiguates.
        if ep.ordinal is None:
            return False
        return ep.ordinal == episode_number

    @staticmethod
    def _estimate_size(ep: SvtEpisode, quality: QualityInfo) -> int:
        # Prefer the video endpoint's exact contentDuration over the show
        # page's coarse "N min" subheading; only fall back further to a
        # nominal, clearly-logged value if neither is available. A 0-byte
        # advertised size is itself a silent failure -- Sonarr uses release
        # size in quality-profile decisions and could reject it outright.
        seconds = quality.duration_s
        if seconds is None:
            seconds = ep.duration_s
        if seconds is None:
            log.warning(
                "no duration available for svt_id %r; advertising nominal size",
                ep.svt_id,
            )
            seconds = _NOMINAL_DURATION_S
        return int(seconds * quality.bitrate_kbps * _BYTES_PER_KBIT)


_FEED_LIMIT = 100


def _deduped(releases: list[Release]) -> list[Release]:
    """One item per GUID. Two mapping rows may share an svt_slug (the table
    only rejects duplicate tvdb_id), which would otherwise emit the same
    GUID twice under different titles."""
    seen: set[str] = set()
    out: list[Release] = []
    for r in releases:
        if r.guid in seen:
            log.warning("duplicate release guid %r in feed; keeping first", r.guid)
            continue
        seen.add(r.guid)
        out.append(r)
    return out


def _capped(releases: list[Release]) -> list[Release]:
    """Honour the `limits max` the caps document advertises."""
    if len(releases) > _FEED_LIMIT:
        log.info("feed truncated to %d of %d releases", _FEED_LIMIT, len(releases))
    return releases[:_FEED_LIMIT]


class _Memoized:
    """Caches idempotent read calls for the life of one `recent()` sweep.

    Deliberately per-sweep and thrown away afterwards: SVT's listing changes
    between polls and a longer-lived cache would serve stale availability,
    which is the one thing that must never go stale here.
    """

    def __init__(self, inner, methods: tuple[str, ...]):
        self._inner = inner
        self._methods = methods
        self._cache: dict = {}

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in self._methods:
            return attr

        async def call(*args):
            key = (name, args)
            if key not in self._cache:
                self._cache[key] = await attr(*args)
            return self._cache[key]

        return call
