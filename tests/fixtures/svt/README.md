# Captured SVT Play responses

These files are real responses from SVT Play, captured on the dates in their
names and committed unchanged, byte-for-byte. Together they are what makes the
test suite run offline, and they are the reason SVT API drift shows up as a
fixture mismatch rather than as a production mystery.

Two captures, because two different things were being recorded.

## 2026-08-24 — the original capture

| File | What it is |
| --- | --- |
| `search-gvfo-20260824.json` | A Contento GraphQL `search` response for the query "Gift vid första ögonkastet" — five hits, mixed `TvSeries` and `TvShow`. Note the `<em>` tags SVT wraps around matched titles; stripping them is asserted. |
| `video-KZmQ5JY-20260824.json` | The `api.svt.se/video/{svtId}` response for one episode: rights, variants, video references, subtitle references. |
| `hls-master-KZmQ5JY-20260824.m3u8` | That episode's HLS master manifest. Quality lives here, not in the video endpoint, which is why it had to be captured separately. |

## 2026-08-28 — the episode listing, both sides of it

On 2026-08-28 the episode list moved off the SVT Play show page HTML and onto
the Contento `detailsPageByPath` GraphQL query. `svt/parser.py`, its
`show-gvfo-20260824.html` page capture and its `episodes-gvfo-20260824.json`
reference list all went with it.

The migration's central claim is that the two implementations produce the
same episodes. Proving that after deleting one of them needs both sides on
disk, so both were captured live, per show, minutes apart:

| File | What it is |
| --- | --- |
| `details-{show}-20260828.json` | Byte-for-byte what `SvtClient`'s shipped `detailsPageByPath` query received. Its `data` block is keyed by that request's random field alias — which is the cache-busting nonce too — so tests read the single value out of `data` rather than naming a key. |
| `scraped-{show}-20260828.json` | The retired scraper's own `SvtEpisode` output for the same show at the same moment, sorted by `svt_id`. Not an SVT response: the derived episode list, which is all the differential needs and is also the only part that could be committed (see "Provenance" below). |

`tests/test_svt_details_page.py` compares them field by field. Four shows,
chosen for the shapes that break things:

| Show | Why it is here | Episodes |
| --- | --- | --- |
| `gvfo` (`gift-vid-forsta-ogonkastet`) | Currently airing, with a 14-episode upcoming block and a special. The show the rest of the suite is built around. | 27 |
| `husdrommar` | Thirteen seasons — a deep back catalogue, where the scraper's inferred year went wrong. | 119 |
| `uppdrag-granskning` | Grouped by `productionPeriod` rather than `season`, no `/avsnitt-N` in its URLs, and episodes over an hour long. | 61 (scraper: 60) |
| `mitt-i-naturen` | Currently offers nothing at all — `associatedContent: []`. An empty list must stay a legitimate answer. | 0 |

## Do not tidy these

The tests assert against their exact shape, including things that look like
noise:

- The **27** episodes for `gvfo`, of which **14** are unavailable. One of
  those 14 is `egWP26b`, the *next* episode, whose `upcomingOverlay.heading`
  is the weekday `"Söndag"` and not `"Kommer"`. That single row is why
  availability keys on the overlay's existence and its selection rather than
  on its text, and reformatting it away would remove the regression it exists
  to catch.
- **Two `gvfo` episodes sharing an air date** (`KZmQ5JY` and `eakXp9m`, both
  2026-08-23), which is what makes the resolver's ordinal signal load-bearing
  rather than decorative.
- `KBMY9zX`, a special whose `item.number` is 1 but whose ordinal is `None`.
  `resolver.py::_recent_for` relies on specials having no ordinal.
- `Ky2mZPn` in `details-uppdrag-granskning-20260828.json` and **not** in
  `scraped-uppdrag-granskning-20260828.json`: a real, published episode the
  scraper silently skipped. It is the whole of that show's only-in-GraphQL
  set, and the test pins it as exactly that.
- The `<em>` tags SVT wraps around search-hit titles.

Re-capture rather than edit. If a fixture and the client disagree, the fixture
is ground truth and the client is wrong — that is the whole point of having
captured them.

The `scraped-*` files are a historical record and can never be re-captured:
the code that produced them is gone. If they ever need to change, the change
is to delete them and the differential together, once it has stopped earning
its keep.

## Provenance and licensing

This is **third-party content**: SVT Play API responses, copyright Sveriges
Television AB. It is retained here solely as recorded test data, for
interoperability testing against an API that has no specification to test
against. It is not part of the licensed work in `LICENSE`, and no media is
included — no video, no audio, no subtitle text, only metadata and manifests
describing where those things live on SVT's CDN.

The `details-*` queries deliberately request no synopsis text. This is why
they could be committed whole where the show page capture they replaced could
not: that file originally carried 171,294 bytes including SVT's editorial
episode descriptions, had to be cut to 46,993 by deleting the regions the
parser never read, and still carried the synopses of all 27 episodes because
the tests counted them. The GraphQL responses carry episode titles, ids,
durations, timestamps and URLs, and nothing else — 7.5 KB where the trimmed
page was 47 KB, for strictly more information.

They contain nothing about the author or the machine that captured them: no
cookies, no session or account identifiers, no request headers, no IP
addresses, no personal data. The only opaque identifiers present are SVT's own
content ids and the per-request field alias, which is a random nonce generated
by this client.

## Re-capturing

`details-*` files are one GET each against
`api.svt.se/contento/graphql` with `SvtClient`'s own query — the honest way to
produce one is to run `SvtClient.list_episodes` behind an httpx response event
hook and write what it received, so the fixture is provably what the shipped
query asks for. A Swedish egress IP is required; SVT geo-restricts.

Counts in the table above will not survive re-capture: what SVT offers changes
weekly. The tests that pin them are pinning *that capture*, which is the
point. `tests/test_integration_svt.py` is the one that asserts against live
SVT, and it deliberately asserts structure rather than counts.
