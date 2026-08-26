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


@dataclass(frozen=True)
class Mapping:
    tvdb_id: int
    svt_series_id: str
    svt_slug: str
    series_title: str         # exactly as Sonarr spells it


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
