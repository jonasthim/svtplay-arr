import pathlib

# The real function svtplay-dl applies to the output path it is handed. It
# is the authority on what the file ends up being called, so the tests below
# assert against it rather than against a copy of its rules that could drift.
from svtplay_dl.utils.output import sanitize

from svtplay_arr.naming import release_guid, release_title, series_prefix

SERIES = "Gift vid första ögonkastet"

# Titles that exercise every character svtplay-dl's sanitize() rewrites, plus
# the path separators it does NOT (it only ever touches the basename, so a
# separator would silently create a subdirectory or escape the staging dir).
_HOSTILE_TITLES = [
    "Vem vet mest?",
    '1. Tager du..?',
    'Rock & Roll "Special" <Edition>',
    "Uppdrag: granskning",
    "A/B",
    "A\\B",
    "Either|Or",
    "Star*",
    "Trailing dot.",
]


def test_title_omits_tba_episode_title():
    assert release_title(SERIES, 15, 3, "WEBDL-1080p", "TBA") == (
        "Gift vid första ögonkastet - S15E03 - WEBDL-1080p"
    )


def test_title_omits_missing_episode_title():
    assert release_title(SERIES, 15, 3, "WEBDL-1080p", None) == (
        "Gift vid första ögonkastet - S15E03 - WEBDL-1080p"
    )


def test_title_includes_real_episode_title():
    assert release_title(SERIES, 15, 3, "WEBDL-1080p", "Avslöjandet") == (
        "Gift vid första ögonkastet - S15E03 - Avslöjandet - WEBDL-1080p"
    )


def test_title_pads_season_and_episode_to_two_digits():
    assert release_title("X", 1, 2, "WEBDL-720p", None) == "X - S01E02 - WEBDL-720p"


def test_title_strips_path_separators():
    assert "/" not in release_title("A/B", 1, 1, "WEBDL-1080p", None)


def test_guid_is_stable_for_same_inputs():
    assert release_guid("KZmQ5JY", "WEBDL-1080p") == release_guid(
        "KZmQ5JY", "WEBDL-1080p"
    )


def test_guid_differs_by_quality():
    assert release_guid("KZmQ5JY", "WEBDL-1080p") != release_guid(
        "KZmQ5JY", "WEBDL-720p"
    )


def test_title_sanitises_quality_path_separator():
    result = release_title("X", 1, 1, "WEBDL-1080p/../../etc", None)
    assert "/" not in result
    assert "\\" not in result


def test_guid_handles_colon_collision():
    # Verify that the colon delimiter collision is fixed
    assert release_guid("A:B", "C") != release_guid("A", "B:C")


# --- release title and on-disk filename must be one string ----------------
#
# renameEpisodes=False means Sonarr keeps whatever the downloader named the
# file, so if svtplay-dl rewrites the name we asked for, the release title
# and the filename diverge permanently in /mnt/tv. Worse, worker.py looks for
# `<stem>.mkv` and raises DownloaderError when it is absent -- and because
# the release GUID is stable across searches, Sonarr blocklists that GUID and
# never retries. A real series is enough to trigger it: "Vem vet mest?"
# contains a "?", which svtplay-dl strips.


def test_title_drops_characters_svtplay_dl_would_strip():
    title = release_title("Vem vet mest?", 4, 2, "WEBDL-1080p", None)
    assert title == "Vem vet mest - S04E02 - WEBDL-1080p"


def test_release_title_is_unchanged_by_svtplay_dl_sanitize():
    """The one assertion that actually matters, made against svtplay-dl's own
    sanitize() rather than a restatement of its rules: the path we hand it
    must come back identical, for every part of the name and for the ".mkv"
    suffix the downloader appends."""
    staging = pathlib.Path("/downloads/incomplete/SVTPLAY-0123456789ab")
    for series in _HOSTILE_TITLES:
        for episode_title in [None, *_HOSTILE_TITLES]:
            for quality in ["WEBDL-1080p", 'WEBDL-1080p/../../etc', "WEBDL:1080p"]:
                stem = release_title(series, 15, 1, quality, episode_title)
                wanted = staging / f"{stem}.mkv"
                assert sanitize(wanted) == wanted, (
                    f"svtplay-dl would rewrite {wanted.name!r}"
                )


def test_title_never_contains_a_path_separator():
    for series in _HOSTILE_TITLES:
        stem = release_title(series, 1, 1, "WEBDL-1080p", series)
        assert "/" not in stem
        assert "\\" not in stem


def test_series_prefix_is_exactly_what_release_title_starts_with():
    # The Newznab `q` filter selects releases by this prefix. If the two ever
    # disagree -- a changed separator, a changed sanitiser -- the filter
    # silently returns nothing for every series, and a search that found a
    # show yesterday finds nothing today with no error anywhere.
    for series_title in (
        "Gift vid första ögonkastet",
        "Vem vet mest?",          # "?" is stripped from the filename
        "Solsidan: Special",      # ":" likewise
        "Rapport / Aktuellt",     # "/" becomes "-"
        "Mysteriet...",           # ".." collapses
    ):
        title = release_title(series_title, 1, 2, "WEBDL-1080p", "Avsnitt 2")
        assert title.startswith(series_prefix(series_title)), series_title
