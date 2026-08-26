"""Opt-in test against the real SVT API.

Every other test in this suite runs against recorded fixtures. This is the
only one that talks to svt.se over the network, and it is the only evidence
in the whole project that `SvtClient` actually works against the live API
rather than against a snapshot of it. It requires a Swedish (or Sweden-VPN)
egress IP -- SVT geo-restricts -- and is excluded from the default run via
`addopts = "-m 'not integration'"` in pyproject.toml.

Run explicitly with:

    uv run pytest -m integration -v

**Leave at least 60 seconds between runs**, or `test_real_search_finds_the_show`
will fail with "response did not echo requested alias". That is not a bug in
this repository -- it is the SVT CDN quirk `svt/client.py` documents,
measured on 2026-08-24: the GraphQL response carries
`cache-control: public, max-age=60`, and the cache key includes `variables`
but ignores both `query` and the `cb` cache-buster. Repeat the *same* search
term inside that minute and the CDN replays the previous response, whose
embedded field alias is the previous request's -- which is exactly what
`_graphql`'s alias check exists to catch, so it raises rather than trusting
it. A different search term is served fresh, which is why
`svtplay-arr-suggest-mappings` (one distinct title per series) is unaffected
in practice, and why the resolver never sees this at all (it uses the show
page and the video endpoint, not GraphQL). Sending the query by POST instead
of GET was observed to sidestep the cache entirely, if this is ever worth
fixing properly.

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
from svtplay_arr.svt.client import SvtClient

pytestmark = pytest.mark.integration

# "Trailer: Gift vid första ögonkastet XL" -- 75 seconds. Verified live on
# 2026-08-24. If SVT retires it, replace it with any other short clip from
# the same show page rather than reaching for a full episode.
SHORT_CLIP_SVT_ID = "jgWYBgb"


async def test_real_search_finds_the_show():
    async with httpx.AsyncClient(timeout=30) as http:
        hits = await SvtClient(http).search_series("gift vid första ögonkastet")
    assert any(h.svt_id == "jpmQD3q" for h in hits)


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
