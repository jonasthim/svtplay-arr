from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


@dataclass(frozen=True)
class SvtSearchHit:
    svt_id: str
    name: str
    typename: str  # "TvSeries" | "TvShow"


@dataclass(frozen=True)
class SvtEpisode:
    svt_id: str
    title: str
    url: str                  # "/video/KZmQ5JY/gift-vid-forsta-ogonkastet/1-tager-du"
    ordinal: int | None       # position within SVT's run, NOT a season number
    published: date | None
    available: bool           # True only when upcomingOverlay is null
    duration_s: int | None


@dataclass(frozen=True)
class SonarrEpisode:
    series_id: int
    season: int
    episode: int
    air_date: date | None
    title: str                # often "TBA"


@dataclass(frozen=True)
class QualityInfo:
    label: str                # "WEBDL-1080p"
    height: int
    bitrate_kbps: int
    duration_s: int | None    # exact contentDuration from the video endpoint; None if absent


@dataclass(frozen=True)
class Release:
    guid: str
    title: str                # also the output filename stem
    svt_id: str
    quality: str
    size_bytes: int
    published: datetime


# How a mapping row came to exist. Written into the file only when it is
# not the default, so a hand-confirmed row and an operator's existing file
# stay byte-for-byte what they always were, and the one thing worth
# recording -- that nobody confirmed this row -- is the thing that shows up.
SOURCE_MANUAL = "manual"   # a human picked it on the config page (or by hand)
SOURCE_AUTO = "auto"       # written by the Find mappings sweep's confidence gate


@dataclass(frozen=True)
class Mapping:
    tvdb_id: int
    svt_series_id: str
    svt_slug: str
    series_title: str         # exactly as Sonarr spells it
    # Provenance, so a guessed mapping and a hand-confirmed one are never
    # indistinguishable later and the sweep's output can be audited or
    # reverted as a group. Defaulted, because every row written before this
    # field existed -- and every row a human writes by hand -- legitimately
    # has no `source` key at all.
    source: str = SOURCE_MANUAL


class JobStatus(str, Enum):
    QUEUED = "Queued"
    DOWNLOADING = "Downloading"
    COMPLETED = "Completed"
    FAILED = "Failed"


@dataclass
class Job:
    nzo_id: str
    svt_id: str
    stem: str                 # filename without extension
    quality: str
    status: JobStatus
    size_bytes: int
    downloaded_bytes: int
    storage_path: str | None
    fail_message: str | None
    # sqlite's `datetime('now')`: UTC, second resolution, as a string.
    # Defaulted so nothing that builds a Job without one breaks, and
    # nullable because a row written before the column had a default
    # legitimately has none.
    created_at: str | None = None
