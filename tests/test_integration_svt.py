"""Opt-in test against the real SVT API.

Every other test in this suite runs against recorded fixtures. This is the
only one that talks to svt.se over the network, and it is the only evidence
in the whole project that `SvtClient` actually works against the live API
rather than against a snapshot of it. It requires a Swedish (or Sweden-VPN)
egress IP -- SVT geo-restricts -- and is excluded from the default run via
`addopts = "-m 'not integration'"` in pyproject.toml.

Run explicitly with:

    uv run pytest -m integration -v

This file used to carry a "leave at least 60 seconds between runs" warning,
because a repeated search inside the CDN's TTL failed with "response did not
echo requested alias". That was real, and it was our bug, not SVT's: the
cache key is `(path, ua, variables)`, so the `cb` *query parameter* the
client used to send was not in it and busted nothing, and the second request
got the first's body -- carrying the first's field alias, which the alias
check then correctly refused. Since 2026-08-28 the nonce travels inside
`variables` instead, which is in the key.
`test_real_repeated_search_is_not_served_a_stale_body` below is what proves
that live, and it is deliberately the *same* term twice in a row: run this
file back to back as fast as you like.

Known-good values, verified live on 2026-08-24:
  - "Gift vid första ögonkastet" has SVT series id `jpmQD3q`.
  - Episode `KZmQ5JY` resolves to 1920x1080 at AVERAGE-BANDWIDTH 3282010
    (3282 kbps) via its HLS master playlist.

Height is asserted exactly -- it's a stable property of a given encode.
Bitrate is asserted only as a sane lower bound, not the exact 3282 kbps
above: SVT can re-encode/re-ladder a title's HLS variants at any time (this
whole file exists because their API has no stability guarantee at all), and
a bitrate drift of that kind is not the kind of regression this test is
meant to catch -- a wrong/missing rendition, or the HLS-parsing path
breaking entirely, is.

  - `jgWYBgb` is "Trailer: Gift vid första ögonkastet XL", 75 seconds long
    (`contentDuration: 75`, ~31 MB as an mkv). It is the download target
    below precisely because it is short: this suite must be runnable on a
    whim, so nothing here may pull a 58-minute episode.

`resolve_quality` does not read resolution/bitrate from the `/video/{id}`
endpoint -- those fields don't exist there (see svt/client.py's module
docstring, "Quality resolution quirk"). It fetches the HLS master playlist
one of the endpoint's `videoReferences` points to and parses
`#EXT-X-STREAM-INF` lines for RESOLUTION/AVERAGE-BANDWIDTH. This test
exercises exactly that path -- and only ever fetches the manifest text, never
any actual media segment.
"""

from pathlib import Path

import httpx
import pytest

from svtplay_arr.downloader import SvtplayDlDownloader
from svtplay_arr.naming import release_title
from svtplay_arr.svt.client import SvtApiError, SvtClient

pytestmark = pytest.mark.integration

# "Trailer: Gift vid första ögonkastet XL" -- 75 seconds. Verified live on
# 2026-08-24. If SVT retires it, replace it with any other short clip from
# the same show page rather than reaching for a full episode.
SHORT_CLIP_SVT_ID = "jgWYBgb"

# The slug behind the captured fixtures; long-running, so it is still there.
SHOW_SLUG = "gift-vid-forsta-ogonkastet"


async def test_real_search_finds_the_show():
    async with httpx.AsyncClient(timeout=30) as http:
        hits = await SvtClient(http).search_series("gift vid första ögonkastet")
    assert any(h.svt_id == "jpmQD3q" for h in hits)


async def test_real_repeated_search_is_not_served_a_stale_body():
    """Two identical searches, back to back, well inside the 20s CDN TTL.

    Before the nonce moved into `variables` the second of these raised
    "response did not echo requested alias" every time -- fail-safe, but a
    spurious error, and it meant the alias check was firing on our own
    cache-busting failure rather than on the CDN swap it was built for.
    """
    async with httpx.AsyncClient(timeout=30) as http:
        client = SvtClient(http)
        first = await client.search_series("uppdrag granskning")
        second = await client.search_series("uppdrag granskning")
    assert first and second
    assert [h.svt_id for h in first] == [h.svt_id for h in second]


async def test_real_episode_list_matches_the_captured_fixture_shape():
    """The live details page still answers, and still carries every field
    `SvtEpisode` is built from.

    Asserted structurally rather than by episode count: the count is a
    property of what SVT is currently offering, which changes weekly. What
    must not change is that a known-good slug yields episodes at all, that
    they have ids and urls, and that the upcoming ones are still separable
    -- which is the whole safety property.
    """
    async with httpx.AsyncClient(timeout=30) as http:
        episodes = await SvtClient(http).list_episodes(SHOW_SLUG)

    assert episodes, "an empty list here is the outage the canary exists for"
    assert all(e.svt_id and e.url for e in episodes)
    assert all(SHOW_SLUG in e.url for e in episodes)
    assert any(e.available for e in episodes)
    # Separability, live. Offline this is over-determined -- `selectionType`
    # and `upcomingOverlay` are perfectly correlated in every capture, so
    # each one alone reproduces the recorded answer. Only the real API can
    # show that the query still *asks* for enough to tell the two apart, and
    # without this the docstring above claims a safety property nothing here
    # exercises. This show has aired weekly for fourteen seasons; if it ever
    # has nothing upcoming, point this at another currently-airing show
    # rather than dropping the assertion.
    assert any(not e.available for e in episodes), (
        "no upcoming episodes came back at all -- either SVT changed what "
        "`addExtras: [upcoming]` returns, or the availability signals are "
        "no longer being asked for"
    )
    assert all(e.published is not None for e in episodes)
    # Exact seconds from `item.duration`, never the page's rounded minutes.
    assert any(e.duration_s % 60 for e in episodes if e.duration_s)


async def test_real_unknown_slug_reaches_callers_as_a_404():
    """SVT answers an unknown path with HTTP 200 and a null page, not a 404.

    `config_ui.check_mapping` branches on `status_code == 404` to tell an
    operator their slug does not exist; if that translation is ever dropped,
    every bad slug starts reporting as a generic SVT error instead.
    """
    async with httpx.AsyncClient(timeout=30) as http:
        with pytest.raises(SvtApiError) as excinfo:
            await SvtClient(http).list_episodes("no-such-show-at-all-xyz")
    assert excinfo.value.status_code == 404


async def test_real_quality_resolution():
    async with httpx.AsyncClient(timeout=30) as http:
        q = await SvtClient(http).resolve_quality("KZmQ5JY")
    assert q is not None
    assert q.height == 1080
    # Loose bound, not the exact 3282 kbps observed 2026-08-24: real enough
    # to catch a broken parse (e.g. bits/s leaking through unconverted, or
    # a near-zero/negative value) without failing on an ordinary re-encode.
    assert 500 <= q.bitrate_kbps <= 20_000


async def test_real_download_lands_at_exactly_the_name_we_asked_for(tmp_path: Path):
    """The only end-to-end check that SvtplayDlDownloader works at all.

    Every unit test replaces `get_media`, so nothing else in the suite proves
    the real library is driven correctly, that it produces an `.mkv` where we
    look for one, or that progress is reported.

    The stem deliberately comes from a series title containing a "?" --
    "Vem vet mest?" is real and currently airing. svtplay-dl's sanitize()
    strips "?" from the basename it writes, so before naming._sanitise
    matched its rule set the file landed under a name worker.py does not look
    for: DownloaderError, and a permanent blocklist entry for a stable GUID
    that is never retried. Running the real thing is the only way to know the
    two strings agree; a fake proves nothing about svtplay-dl's own
    rewriting.
    """
    staging = tmp_path / "SVTPLAY-integration"
    stem = release_title("Vem vet mest?", 1, 1, "WEBDL-1080p", None)
    assert "?" not in stem, "sanitising happens before svtplay-dl ever sees it"

    ticks: list[int] = []
    out = await SvtplayDlDownloader(poll_interval=0.5).download(
        SHORT_CLIP_SVT_ID,
        staging,
        stem,
        lambda done, total: ticks.append(done),
    )

    # The exact path worker.py's _publish() goes looking for.
    assert out == staging / f"{stem}.mkv"
    assert out.exists()
    assert out.stat().st_size > 0
    # svtplay-dl must not have written anything under a name we would not
    # recognise as belonging to this release (worker.py only carries across
    # sidecars whose name starts with the stem).
    assert all(child.name.startswith(stem) for child in staging.iterdir())
    # The mandatory final on_progress(size, size) call the SAB queue relies on
    # to ever reach 100%.
    assert ticks[-1] == out.stat().st_size
