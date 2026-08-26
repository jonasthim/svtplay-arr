# svtplay-arr — Build notes

**Date:** 2026-08-24

> **What this is.** A design record of how svtplay-arr was actually put
> together: the module map, what each module is allowed to know, the
> constraints every module inherits, and the handful of findings that shaped
> the code and would otherwise be lost. It accompanies
> `2026-08-24-svtplay-arr-design.md`, which argues the design; this document
> records the shape it took.
>
> It is **not** a user guide and not a tutorial. For installing and running
> the service, see `deploy/README.md`. Where this and the code disagree, the
> code is right.

**Goal:** Let Sonarr grab SVT Play episodes as ordinary releases, by
impersonating a Newznab indexer and a SABnzbd download client and fetching
media with `svtplay-dl`.

**Architecture:** One Python service exposing two HTTP surfaces (Newznab +
SABnzbd emulation) backed by a resolver that maps Sonarr's `(tvdbid, season,
episode)` onto a specific SVT video, and a worker that downloads into an
`incomplete/` directory and publishes by atomic rename into `completed/` on
the same filesystem. Only the resolver knows about mapping; only the SVT
client knows about SVT.

**Stack:** Python 3.12+, FastAPI, uvicorn, httpx, PyYAML, stdlib `sqlite3`,
`svtplay-dl` (as a library), pytest + pytest-asyncio, `uv` for dependency
management.

## Constraints every module inherits

Taken from the design and true of the whole codebase, not of one module.

- **Never read SVT's season number.** SVT labelled the run in the worked
  example "Säsong 14"; Sonarr calls it Season 15. Episodes are identified by
  ordinal plus air date only.
- **Ambiguity returns empty.** Two candidates means no result. Never resolve
  by preference.
- **Release GUID is stable**, derived from `(svt_id, quality)`. A changing
  GUID defeats Sonarr's blocklist and causes an infinite grab → fail →
  regrab loop.
- **`t=caps` must advertise `supportedParams="q,tvdbid,season,ep"`.** Without
  `tvdbid`, Sonarr falls back to title search and the design collapses into
  fuzzy Swedish-title matching.
- **Release title and output filename are one string.** `renameEpisodes=False`
  means Sonarr keeps the file's name, not the release's.
- **Air date tolerance is ±1 day**, configurable.
- **`incomplete/` and `completed/` must be on the same filesystem.** Only an
  atomic rename publishes a file.
- **Files are mode 664, directories 775. Never `chown`** under an
  identity-squashing NFS export; set `UMask` instead.
- **Download concurrency defaults to 1.**
- **SVT parse failures return an empty result set, never HTTP 500.**
- **Upcoming episodes are never offered.** The reliable signal is a non-null
  `upcomingOverlay` on the teaser, never the heading text — see "Findings"
  below.
- **Subtitle sidecars are named to match the video stem exactly.**

## Module map

```
src/svtplay_arr/
  config.py          Settings loaded from YAML + env; the settings writer
  models.py          Frozen dataclasses shared by every layer
  naming.py          Release title / filename generation
  yamlio.py          Atomic YAML write + mtime read (added with the config UI)
  store.py           SQLite job store
  mappings.py        YAML mapping table (tvdb_id -> svt series), + writer
  sonarr.py          Sonarr API client (the metadata oracle)
  svt/
    parser.py        Show-page flight-payload parsing
    client.py        GraphQL search, episode listing, quality resolution
  resolver.py        The confidence gate
  downloader.py      Downloader protocol + svtplay-dl implementation
  worker.py          Job execution, atomic publish
  api/
    newznab.py       t=caps, t=tvsearch, nzb download
    sab.py           SABnzbd emulation
    config_ui.py     The configuration page (see the config UI design record)
  templates/         Jinja2 templates for the configuration page
  app.py             FastAPI wiring, /health
tests/
  fixtures/svt/      Captured SVT responses; see that directory's README
```

### What each module is allowed to know

The seams are the design. Stated as dependencies, so a change lands in one
place:

- **`models.py`** depends on nothing. Frozen dataclasses only.
- **`naming.py`** depends on nothing. `release_title(...)` returns the one
  string that is simultaneously the Newznab release title and the worker's
  output filename stem; `release_guid(svt_id, quality)` is the stable GUID.
  Both the resolver and the worker call `release_title`, and that is the
  point — there is one generator, so the two strings cannot diverge.
- **`svt/parser.py`** turns a captured show page into `SvtEpisode`s. It is the
  only place that knows the page's shape.
- **`svt/client.py`** is the only component that knows SVT exists: GraphQL
  search, episode listing via the parser, and quality resolution. An SVT API
  change lands here and nowhere else.
- **`sonarr.py`** is the metadata oracle client — Sonarr, not TVDB, defines
  what `(season, episode)` means, because Sonarr's numbering is by definition
  the numbering the import will be filed under.
- **`mappings.py`** owns the tvdb_id → SVT series table, its writer, and
  `suggest_mappings`, which *prints* candidate rows and never writes them.
- **`store.py`** owns the SQLite job table and nothing else.
- **`resolver.py`** is the only component that knows about mapping. It answers
  one question — "given tvdbid, season, episode, is there an SVT episode I am
  confident is that one?" — and returns a match or nothing.
- **`downloader.py`** defines the `Downloader` protocol, the real
  `svtplay-dl` implementation, and the progress-aware fake the worker tests
  need.
- **`worker.py`** executes jobs and publishes by atomic rename.
- **`api/*`** are wire surfaces only. They translate; they do not decide.

## Findings that shaped the code

These cost real time to discover and are the reason several pieces look the
way they do.

**The show page's `__NEXT_DATA__` is empty of content.** Episode data is
client-loaded, and lives in an HTML-escaped JSON flight payload elsewhere in
the markup. The parser therefore unescapes and scans rather than parsing
`__NEXT_DATA__`, which is why it looks less principled than it should.

**The SVT CDN was observed returning a cached response body belonging to a
different GraphQL query.** Two different queries came back with the same
body. Every request therefore carries a cache-buster, and the response is
validated against what was asked for; a mismatch raises `SvtApiError` rather
than being quietly believed.

**Upcoming episodes are detected by `upcomingOverlay`, not by heading text.**
In the worked example, 14 teasers carried a non-null `upcomingOverlay` and
only 13 of those overlays said "Kommer" — the 14th was the *next* episode,
flagged with a weekday because it sat in the page's `"upcoming"` module.
Matching on the word misses exactly the episode a weekly grab asks for, and
offering an upcoming episode costs it permanently: the stable GUID is
blocklisted on the failed grab and is still blocklisted when the episode
really airs.

**Fixtures are ground truth, not the code.** The captured pages are real
responses. When the parser and a fixture disagree, the fixture wins and the
regexes get adjusted — the whole point of capturing them is that SVT API
drift surfaces as a fixture mismatch rather than as a production mystery.

**`svtplay-dl`'s library API is not stable across releases.** The real
downloader is exercised only by the opt-in integration test; if
`setup_defaults` / `get_media` differ in an installed version, check the
installed library rather than assuming the call shape.

**A synchronous fake cannot test an asynchronous protocol.** The worker is
long-running and Sonarr polls `mode=queue` *during* the download, so a fake
that completes instantly would make every queue test pass while proving
nothing. This mistake has been made before on another project — 85 green
tests over a coordinator that never waited for replies and could never have
worked against real hardware. `FakeDownloader` therefore models progress over
simulated time, and a test asserts that intermediate states are observable.

**The uploaded `.nzb` is parsed with `defusedxml`, not stdlib
`ElementTree`.** It arrives on an HTTP endpoint, so it must not be
vulnerable to entity-expansion attacks, whoever is nominally allowed to
reach the endpoint.

**Both routers needed a route registered at `""` as well as `"/"`.** Without
it, `/api` 307-redirects to `/api/`, and not every client follows that the
way you would hope.

## Verifying a deployment

The order matters — each step is cheaper to debug than the one after it.

1. The unit suite passes (`uv run --extra dev pytest -q`), with zero
   warnings. The integration suite (`-m integration`) is opt-in and needs a
   Swedish IP.
2. `curl localhost:9800/health` reports `same_filesystem: true`.
3. `curl 'localhost:9800/api/?t=caps'` contains `tvdbid` in
   `supportedParams`.
4. `curl 'localhost:9800/api/?t=tvsearch&tvdbid=<id>&season=<n>&ep=<n>'`
   returns exactly one item, titled as the file will be named.
5. Only then add the indexer, download client and remote path mapping in
   Sonarr — and use **Manual Import** for the first grab, before trusting
   automatic completed-download handling with a whole season.
