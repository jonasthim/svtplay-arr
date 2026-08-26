# Captured SVT Play responses

These five files are real responses from SVT Play, captured on **2026-08-24**.
The four API responses (search, episode list, video metadata, HLS manifest)
are committed unchanged, byte-for-byte. The show page is not: it is a
deliberately reduced excerpt of that capture — every retained byte is exactly
what SVT emitted, but the retained regions were spliced together with content
deleted from between them, so the file as a whole is no longer byte-valid as
SVT emitted it (see "Trimmed for the public repo" below). Together these
files are what makes the test suite run offline, and they are the reason SVT
API drift shows up as a fixture mismatch rather than as a production mystery.

| File | What it is |
| --- | --- |
| `search-gvfo-20260824.json` | A Contento GraphQL `search` response for the query "Gift vid första ögonkastet" — five hits, mixed `TvSeries` and `TvShow`. |
| `show-gvfo-20260824.html` | A trimmed excerpt of the SVT Play series page for that show (46K; the full capture was 167K — see "Trimmed for the public repo" below). Its `__NEXT_DATA__` is empty of content; the episode list lives in an HTML-escaped JSON payload elsewhere in the markup, which is what `svt/parser.py` scans. |
| `episodes-gvfo-20260824.json` | The 27 items found in that page, in capture order — the reference list used while the parser was being written. No test imports it, and its last column marks only the **13** teasers whose overlay says "Kommer", which is the count the parser was corrected away from (see below). Kept as the record of what the page contained, not as an expected result. |
| `video-KZmQ5JY-20260824.json` | The `api.svt.se/video/{svtId}` response for one episode: rights, variants, video references, subtitle references. |
| `hls-master-KZmQ5JY-20260824.m3u8` | That episode's HLS master manifest. Quality lives here, not in the video endpoint, which is why it had to be captured separately. |

## Provenance and licensing

This is **third-party content**: SVT Play page markup and API responses,
copyright Sveriges Television AB. It is retained here solely as recorded test
data, for interoperability testing against an API that has no specification to
test against. It is not part of the licensed work in `LICENSE`, and no media
is included — no video, no audio, no subtitle text, only metadata and
manifests describing where those things live on SVT's CDN.

They contain nothing about the author or the machine that captured them: no
cookies, no session or account identifiers, no request headers, no IP
addresses, no personal data. The only opaque identifiers present are SVT's own
content ids and CDN asset UUIDs, which are the same for every viewer.

## Do not tidy these

The tests assert against their exact shape, including things that look like
noise:

- The **27** items in the show page, of which **14** carry a non-null
  `upcomingOverlay`, and of those 14 only **13** say "Kommer" — the 14th is
  the next episode, flagged with a weekday instead. That single row is the
  reason the parser keys on `upcomingOverlay` rather than on heading text,
  and reformatting it away would remove the regression it exists to catch.
- Two episodes sharing an air date, which is what makes the resolver's
  ordinal signal load-bearing rather than decorative.
- The `<em>` tags SVT wraps around search-hit titles.

Re-capture rather than edit. If a fixture and the parser disagree, the fixture
is ground truth and the parser is wrong — that is the whole point of having
captured them.

## Trimmed for the public repo

`show-gvfo-20260824.html` originally carried the full captured page (171,294
bytes), including SVT's editorial episode synopses — copyrighted text that
has no business sitting in a public repository just because it happened to be
next to the JSON the parser actually reads. Ahead of the first public release
it was cut down to 46,993 bytes by *deleting* whole regions the parser never
touches; nothing that remains was reformatted, re-escaped, or rewritten — every
retained byte is exactly what SVT emitted, just fewer of them.

What was removed, in order:

- Everything in `<head>` except the bare `<head>`/`</head>` tags: viewport and
  OG meta, canonical/preload `<link>`s, the `debugInfo.js`/polyfill/webpack/
  chunk `<script src=…>` tags, and the inline Modernizr script.
- All `<link rel="preload">` image srcsets between `<body>` and the
  `window.SVTPlayEnv` script, and that `SVTPlayEnv` script itself (app config,
  not episode data).
- Inside the `window.URQL_DATA` flight payload — the one 106 KB `<script>`
  block `svt/parser.py` actually scans — nearly everything **before** the
  first `{"__typename":"Teaser"` occurrence: `mainCategories`, nav, and other
  module data the parser drops via `text.split(_TEASER_START)[1:]`
  regardless of what it contains. One 86-byte fragment from that region was
  kept — see "the page's show-hero heading" below.
- The **16** non-episode teasers that follow the 27 episode teasers in that
  same payload (`__typename` values like `Single`, plus the show's clips and
  "related" rail) — content the parser's regex already excludes by anchoring
  on `"__typename":"Episode"`, so removing the teaser objects outright changes
  nothing a test observes.
- `__NEXT_DATA__` (present but empty of episode content, per the table above)
  and everything else after the flight payload's closing `</script>`.

What was deliberately **kept**, byte-for-byte, and why:

- **All 27 episode teasers**, complete with their `description` synopsis
  text. `test_parses_all_episodes` and `test_list_episodes_parses_show_page`
  both assert `len(episodes) == 27` on the nose, and per this project's rule
  that a test never bends to fit a smaller fixture, no episode could be
  dropped without also being one this suite counts. Dropping any of the 27
  would have been the obvious next lever for cutting synopsis text; the exact
  counts below are why that lever wasn't available here.
- All **14** upcoming teasers, because `test_marks_upcoming_episodes_unavailable`
  asserts `len(upcoming) == 14` and singles out `8Dvo3wJ` by id; `egWP26b`
  specifically, because `test_next_episode_flagged_by_weekday_is_unavailable`
  depends on its overlay being the "Söndag"-not-"Kommer" case described above.
- The page's show-hero heading, as one 86-byte fragment kept from otherwise-deleted
  pre-teaser content: literally `"heading":"Gift vid första ögonkastet",
  "subHeading":"Nästa avsnitt sön 30 aug"`, immediately followed by episode
  0's own teaser (`KZmQ5JY`) with nothing in between. This is the exact
  object the historical bug crossed into: that heading/subHeading pair has no
  `item` of its own, so a `_TEASER` search run over the whole unsplit payload
  (instead of per-segment) skips straight past it to the *next* `"item":
  {"svtId":...}` in the text — which, with no other teaser left in between,
  is `KZmQ5JY`'s. Deleting this fragment (the first trim pass did, before it
  was caught) makes the crossing case vanish along with it: nothing precedes
  episode 0 with a heading/subHeading pair and no item, so an unsplit scan
  just lands on each teaser's own fields and the bug never reproduces.
  Verified by literally reintroducing the bug — replacing the per-segment
  `for segment in text.split(_TEASER_START)[1:]: m = _TEASER.search(segment)`
  loop with `for m in _TEASER.finditer(text)` — and confirming
  `test_first_episode_is_read_from_its_own_teaser` fails against this fixture
  exactly as the code comments describe (`KZmQ5JY` resolves to the hero's own
  heading, no ordinal, the hero's date instead of its own).
- `eakXp9m`'s literal subheading `"Igår 02:00 • 58 min"`, which
  `test_a_leap_day_in_one_teaser_does_not_fail_the_whole_page` locates and
  doctors with `str.replace` at test time.

If the fixture ever needs to grow again (a new field the parser starts
reading, a new edge case worth pinning), re-capture the page and re-trim by
the same method — deleting unread regions, never rewriting kept ones. Never
hand-edit the retained payload to add or shrink content.
