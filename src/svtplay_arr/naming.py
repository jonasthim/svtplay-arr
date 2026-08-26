import hashlib

_PLACEHOLDER_TITLES = {"tba", "tbd", ""}

# Between every part of a release title. Shared with series_prefix() below,
# which has to produce a prefix release_title() is guaranteed to start with.
_SEPARATOR = " - "


def release_title(
    series_title: str,
    season: int,
    episode: int,
    quality: str,
    episode_title: str | None,
) -> str:
    """The Newznab release title AND the output filename stem.

    These must be one string: renameEpisodes=False means Sonarr keeps the
    downloaded file's name, so any divergence lands permanently in /mnt/tv.
    All string parts are sanitised to prevent path traversal attacks in the
    output filename, including the quality parameter.
    """
    parts = [_sanitise(series_title), f"S{season:02d}E{episode:02d}"]
    if episode_title and episode_title.strip().lower() not in _PLACEHOLDER_TITLES:
        parts.append(_sanitise(episode_title))
    parts.append(_sanitise(quality))
    return _SEPARATOR.join(parts)


def series_prefix(series_title: str) -> str:
    """The exact leading segment `release_title` gives every release of this
    series -- the sanitised series title plus the separator.

    Newznab's `q` filter is the only caller: a `Release` carries no series
    identity of its own, only the title, so "is this release one of that
    mapping's?" has to be answered from the title. Answering it here rather
    than in `api/newznab.py` is what keeps the answer true -- the separator
    and the sanitiser are this module's, and a filter that re-implemented
    either would start silently missing series the moment one changed.

    Note this sanitises, so it is the *release's* spelling, not the mapping
    table's: "Vem vet mest?" yields "Vem vet mest - ". A caller matching an
    operator's query should match the query against the mapping table's raw
    `series_title` and use this only to select the releases.
    """
    return _sanitise(series_title) + _SEPARATOR


def release_guid(svt_id: str, quality: str) -> str:
    """Stable across searches so Sonarr's blocklist suppresses failed releases.

    A GUID that changed between searches would make every re-search look like a
    brand new release, defeating the blocklist and looping grab->fail->regrab.
    Uses length-prefixed encoding to prevent delimiter collisions.
    """
    # Length-prefix each field to prevent collisions (e.g. A:B + C != A + B:C)
    message = f"{len(svt_id):04d}:{svt_id}:{len(quality):04d}:{quality}"
    digest = hashlib.sha1(message.encode("utf-8")).hexdigest()[:16]
    return f"svtplay-{digest}"


# svtplay-dl rewrites the output path it is given -- see sanitize() in
# svtplay_dl/utils/output.py. It deletes each of these from the basename...
_SVTPLAY_DL_BLOCKLIST = (":", "*", "?", '"', "<", ">", "|", "\0")
# ...and it does NOT touch path separators, because it only ever sanitises
# the basename. A separator left in the stem would put the file in a
# subdirectory of staging (or escape it entirely), where worker.py would not
# find it, so those are handled here and replaced rather than deleted --
# "A/B" should read as "A-B", not "AB".
_PATH_SEPARATORS = ("/", "\\")


def _sanitise(value: str) -> str:
    """Make `value` a fixed point of svtplay-dl's sanitize(), and safe as a
    path component.

    This is the whole reason release_title() can promise one string for both
    the release title and the filename. svtplay-dl silently rewrote anything
    it disliked, and the divergence was not cosmetic: worker.py looks for
    `<stem>.mkv` and raises DownloaderError when it is missing, and the
    release GUID is stable across searches, so Sonarr blocklisted that GUID
    and never retried. "Vem vet mest?" -- a real, currently-airing series --
    was enough to lose an episode permanently.
    """
    for bad in _PATH_SEPARATORS:
        value = value.replace(bad, "-")
    for bad in _SVTPLAY_DL_BLOCKLIST:
        value = value.replace(bad, "")
    # svtplay-dl collapses ".." to "." in a single non-recursive pass, so
    # "..." would come back as ".." from us and be collapsed again by it.
    # Looping to a fixed point is what makes its pass a guaranteed no-op.
    while ".." in value:
        value = value.replace("..", ".")
    # A trailing "." would meet the appended ".mkv" and become the ".." it
    # collapses -- which is why this strips dots as well as whitespace.
    return value.strip().rstrip(" .")
