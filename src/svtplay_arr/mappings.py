"""The tvdb_id -> SVT series table.

Sonarr sends `tvdbid` on every search; this module is how the service knows
which SVT programme is being asked about. Loading is intentionally strict
(a duplicate tvdb_id is rejected loudly, not silently last-wins) and
`suggest_mappings`/`main` only ever print candidate rows -- a wrong series
mapping is exactly the class of error the resolver refuses to make on its
own, so a human must confirm every row before it lands in mappings.yaml.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from svtplay_arr.models import Mapping
from svtplay_arr.yamlio import atomic_write_yaml, read_with_mtime

log = logging.getLogger(__name__)


class MappingTable:
    def __init__(self, mappings: dict[int, Mapping]):
        self._by_tvdb = mappings

    @classmethod
    def load(cls, path: Path) -> "MappingTable":
        if not path.exists():
            return cls({})

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"{path} is not valid YAML") from exc

        # An unrecognised top-level shape is a *failure*, not an empty
        # success. Returning cls({}) here used to look identical to "the
        # operator has no mappings", so ReloadingMappingTable installed it
        # over the last known-good table without a word in the log -- and
        # an empty feed is what makes Sonarr reject the indexer. Raising
        # instead puts the judgement in the one place that defines what a
        # valid mappings file is, and every caller already has the right
        # policy for a raise: _refresh keeps the last-good table and warns,
        # and the config page renders an error banner.
        if raw is None:
            raise ValueError(
                f"{path} is empty; expected a top-level 'series' list "
                "(use 'series: []' for no mappings)"
            )
        if not isinstance(raw, dict):
            raise ValueError(
                f"{path} does not hold a top-level mapping "
                f"(got {type(raw).__name__})"
            )
        if "series" not in raw:
            raise ValueError(f"{path} has no top-level 'series' key")

        entries = raw.get("series")
        # `series: []` is the one legitimate way to hold zero mappings --
        # it is exactly what deleting the last row through the config page
        # writes -- so it stays a successful load. `series:` with the rows
        # deleted underneath it (entries is None) is not: it is the shape a
        # hand-edit over SSH leaves behind, and it must be visible.
        if not isinstance(entries, list):
            raise ValueError(
                f"{path}: 'series' must be a list "
                f"(got {type(entries).__name__}; use 'series: []' for no "
                "mappings)"
            )

        out: dict[int, Mapping] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue  # malformed row: skip rather than crash

            tvdb_id_raw = entry.get("tvdb_id")
            svt_series_id = entry.get("svt_series_id")
            svt_slug = entry.get("svt_slug")
            series_title = entry.get("series_title")
            if (
                tvdb_id_raw is None
                or svt_series_id is None
                or svt_slug is None
                or series_title is None
            ):
                continue  # incomplete row: skip rather than crash

            try:
                tvdb_id = int(tvdb_id_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid tvdb_id {tvdb_id_raw!r} in {path}"
                ) from exc

            if tvdb_id in out:
                raise ValueError(f"duplicate tvdb_id {tvdb_id} in {path}")

            out[tvdb_id] = Mapping(
                tvdb_id=tvdb_id,
                svt_series_id=svt_series_id,
                svt_slug=svt_slug,
                series_title=series_title,
            )
        return cls(out)

    def for_tvdb(self, tvdb_id: int) -> Mapping | None:
        return self._by_tvdb.get(tvdb_id)

    def all(self) -> list[Mapping]:
        """Every mapped series. Used by the RSS feed, which has no tvdb_id
        to look up -- Sonarr sends a bare tvsearch with no parameters."""
        return list(self._by_tvdb.values())


async def suggest_mappings(sonarr, svt) -> list[dict]:
    """Propose tvdb->SVT rows by searching SVT for each Sonarr series title.

    Output is printed for a human to paste into mappings.yaml after checking.
    Nothing is written automatically: a wrong series mapping is exactly the
    class of error the resolver refuses to make on its own.
    """
    suggestions: list[dict] = []
    for series in await sonarr.all_series():
        if not isinstance(series, dict):
            continue
        title = series.get("title")
        tvdb_id = series.get("tvdbId")
        if not title or tvdb_id is None:
            continue

        hits = await svt.search_series(title)
        best = next((h for h in hits if h.typename in ("TvSeries", "TvShow")), None)
        if best is None:
            continue
        suggestions.append(
            {
                "tvdb_id": tvdb_id,
                "svt_series_id": best.svt_id,
                "svt_slug": "",  # human fills from the SVT Play URL
                "series_title": title,
                "svt_name": best.name,
            }
        )
    return suggestions


def main() -> None:
    """CLI: print candidate mapping rows as YAML for a human to check.

    Entry point `svtplay-arr-suggest-mappings`. Prints to stdout only; it never
    edits mappings.yaml.
    """
    import asyncio
    import os
    import sys

    import httpx
    import yaml as _yaml

    from svtplay_arr.config import Settings
    from svtplay_arr.sonarr import SonarrClient
    from svtplay_arr.svt.client import SvtClient

    settings = Settings.load(
        Path(os.environ.get("SVTPLAY_ARR_CONFIG", "/etc/svtplay-arr/config.yaml"))
    )

    async def run() -> list[dict]:
        async with httpx.AsyncClient(timeout=30.0) as http:
            return await suggest_mappings(
                SonarrClient(settings.sonarr_url, settings.sonarr_api_key, http),
                SvtClient(http, settings.svt_ua),
            )

    rows = asyncio.run(run())
    _yaml.safe_dump({"series": rows}, sys.stdout, allow_unicode=True, sort_keys=False)
    print(
        "\n# Check every row, fill in svt_slug from the SVT Play URL, "
        "then paste into mappings.yaml",
        file=sys.stderr,
    )


class MappingError(RuntimeError):
    """The requested mapping change would produce a file the loader rejects."""


_MAPPING_HEADER = [
    "managed by svtplay-arr",
    "",
    "series_title must match Sonarr's spelling exactly: Sonarr runs with",
    "renameEpisodes=False, so it becomes the permanent filename in the",
    "library. The config page copies it from Sonarr rather than typing it.",
]


def _rows(path: Path) -> list[dict]:
    raw, _ = read_with_mtime(path)
    if not isinstance(raw, dict):
        # A top-level document that isn't a mapping (e.g. a bare YAML list)
        # holds no rows we can recognise. MappingTable.load *rejects* that
        # shape rather than reading it as empty, so the two deliberately
        # differ here: the loader's job is to refuse to serve a file it
        # cannot understand, while the writer's job is to let the config
        # page repair one. Nothing recognisable is lost by rewriting it, and
        # atomic_write_yaml keeps the previous contents as `.bak`.
        return []
    series = raw.get("series")
    return list(series) if isinstance(series, list) else []


def _coerce_tvdb_id(value: object) -> int | None:
    """Mirror the loader's `int()` coercion, but never raise.

    A hand-edited or quoting-quirk file can hold `tvdb_id` as a YAML string
    ("288649" instead of 288649). Comparing raw values would silently miss
    that match; this makes the comparison tolerant the way the loader's own
    `int(tvdb_id_raw)` is, without the loader's "raise on garbage" behaviour
    -- a non-numeric value here just means "does not match", not "crash".
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _write_rows(path: Path, rows: list[dict], expected_mtime: float | None) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = [_MAPPING_HEADER[0] + f"; last written {stamp}"] + _MAPPING_HEADER[1:]
    atomic_write_yaml(
        path, {"series": rows}, header=header, expected_mtime=expected_mtime
    )


def add_mapping(
    path: Path,
    *,
    tvdb_id: int,
    svt_series_id: str,
    svt_slug: str,
    series_title: str,
    expected_mtime: float | None,
) -> None:
    """Append one mapping row.

    Refuses a duplicate tvdb_id rather than writing a file `MappingTable.load`
    would reject -- the UI must never be the thing that breaks startup.
    Also refuses blank identifying fields: series_title becomes the
    permanent filename in the library, so a whitespace-only value is a
    landmine the loader's `is None` check would happily accept.
    """
    for field, value in (
        ("svt_series_id", svt_series_id),
        ("svt_slug", svt_slug),
        ("series_title", series_title),
    ):
        if not isinstance(value, str) or not value.strip():
            raise MappingError(f"{field} must not be blank")

    rows = _rows(path)
    for row in rows:
        if isinstance(row, dict) and _coerce_tvdb_id(row.get("tvdb_id")) == tvdb_id:
            raise MappingError(
                f"tvdb_id {tvdb_id} is already mapped to "
                f"{row.get('series_title')!r}; remove that row first"
            )
    rows.append(
        {
            "tvdb_id": tvdb_id,
            "svt_series_id": svt_series_id,
            "svt_slug": svt_slug,
            "series_title": series_title,
        }
    )
    _write_rows(path, rows, expected_mtime)


def remove_mapping(
    path: Path, tvdb_id: int, *, expected_mtime: float | None
) -> None:
    """Delete the one row for tvdb_id.

    A hand-edited file can hold two rows sharing a tvdb_id -- exactly the
    state `MappingTable.load` rejects. There is no principled way to guess
    which one the operator meant, so that ambiguity is refused rather than
    resolved by deleting both: concurrent modification is refused, not
    merged, and this is the same policy.
    """
    rows = _rows(path)
    matches = [
        r
        for r in rows
        if isinstance(r, dict) and _coerce_tvdb_id(r.get("tvdb_id")) == tvdb_id
    ]
    if not matches:
        raise MappingError(f"no mapping for tvdb_id {tvdb_id}")
    if len(matches) > 1:
        raise MappingError(
            f"{len(matches)} rows match tvdb_id {tvdb_id}; the file needs a "
            "manual fix before this can be removed automatically"
        )
    kept = [r for r in rows if r is not matches[0]]
    _write_rows(path, kept, expected_mtime)


class ReloadingMappingTable:
    """A MappingTable that re-reads when the file's mtime changes.

    Mappings are the one thing the config page changes often, so requiring a
    restart to add a show would remove most of the point of having a page.

    On any load failure -- unreadable, unparseable, or a top-level shape
    `MappingTable.load` does not recognise -- it keeps serving the last
    known-good table and logs. Returning empty instead would empty the RSS
    feed, and an empty feed is what makes Sonarr reject the indexer
    outright. The one file that legitimately yields zero mappings is
    `series: []`; that loads successfully and is applied like any other
    change.

    Reload detection is mtime-based: a fix written within the same
    filesystem timestamp tick as the broken write it replaces is invisible
    until the next distinguishable change, since the mtime comparison in
    `_refresh` sees no difference. Unlikely on ext4's nanosecond mtimes, but
    real on coarser-mtime storage (e.g. some network filesystems).
    """

    def __init__(self, path: Path):
        self._path = path
        self._table = MappingTable({})
        self._mtime: float | None = None
        self._ever_loaded = False  # was any load ever successful?
        # Two independent failures, tracked separately because they clear
        # differently.
        #
        # `_load_failed`: the file was reached but would not load. Only a
        # change to the file can fix that, so staying set across an
        # unchanged file is correct.
        #
        # `_stat_failed`: the file could not be reached at all -- deleted,
        # or a permissions change on the directory above it. That clears
        # the moment a stat succeeds, even with the file byte- and
        # mtime-identical, because nothing about the file was ever wrong.
        #
        # Collapsing the two into one flag left a cleared permissions
        # problem reporting degraded forever: the mtime-equal early return
        # in `_refresh` never reaches the code that resets it. A /health
        # that cries wolf on a healthy service is worth no more than the
        # silent one it replaced.
        self._load_failed = False
        self._stat_failed = False
        self._refresh()

    @property
    def _degraded(self) -> bool:
        """Is what `all()` serves something other than what the file says?"""
        return self._load_failed or self._stat_failed

    def _refresh(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            # No file at all. On a fresh install that is the legitimate
            # starting state -- there are no mappings yet and the config
            # page is how they get made -- so it is not a degrade. After a
            # successful load it means the file was deleted underneath us,
            # which is.
            if self._ever_loaded and not self._stat_failed:
                log.warning(
                    "mappings file %s has disappeared; keeping the last "
                    "good table (%d series)",
                    self._path,
                    len(self._table.all()),
                )
                self._stat_failed = True
            return
        except OSError:
            # Not the file itself but the path to it (e.g. a directory
            # permissions change). Log once per transition, not per request.
            if not self._stat_failed:
                log.warning(
                    "mappings file %s could not be stat'd; keeping the "
                    "last good table (%d series)",
                    self._path,
                    len(self._table.all()),
                    exc_info=True,
                )
                self._stat_failed = True
            return
        # The stat succeeded, so whatever kept us from reaching the file is
        # over. This has to clear *before* the mtime-equal return below:
        # restoring a directory's permissions leaves the file untouched, so
        # the mtime still matches and the early return fires, and anything
        # downstream of it never runs again.
        if self._stat_failed:
            log.info(
                "mappings file %s is reachable again; serving %d series",
                self._path,
                len(self._table.all()),
            )
            self._stat_failed = False
        if mtime == self._mtime:
            return
        try:
            self._table = MappingTable.load(self._path)
            self._mtime = mtime
            self._ever_loaded = True
            self._load_failed = False
        except Exception:
            self._load_failed = True
            if self._ever_loaded:
                # A real degrade: self._table still holds the last good
                # load, so the feed genuinely is unaffected.
                log.warning(
                    "mappings file %s is invalid; keeping the last good "
                    "table (%d series). Fix the file; the feed is "
                    "unaffected until then.",
                    self._path,
                    len(self._table.all()),
                    exc_info=True,
                )
            else:
                # No prior good load exists -- self._table is still the
                # empty table from __init__, so the feed IS currently
                # empty. Saying otherwise here would tell an operator at
                # boot that everything is fine while the indexer serves
                # zero results.
                log.error(
                    "mappings file %s is invalid and no valid mappings "
                    "have ever loaded; the feed is currently EMPTY. Fix "
                    "the file to populate it.",
                    self._path,
                    exc_info=True,
                )
            self._mtime = mtime  # don't retry-log on every request

    def for_tvdb(self, tvdb_id: int) -> Mapping | None:
        self._refresh()
        return self._table.for_tvdb(tvdb_id)

    def all(self) -> list[Mapping]:
        self._refresh()
        return self._table.all()

    def status(self) -> dict:
        """What `/health` reports about this table.

        `ever_loaded` false with `degraded` false is the fresh-install
        state: no mappings file exists yet. `degraded` true means the file
        on disk failed to load and `count` describes the last known-good
        table being served in its place -- the feed is unaffected, but a
        human needs to know, because nothing else will tell them.
        """
        self._refresh()
        return {
            "ever_loaded": self._ever_loaded,
            "degraded": self._degraded,
            "count": len(self._table.all()),
        }
