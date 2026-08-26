# svtplay-arr — Design

**Date:** 2026-08-24
**Status:** Implemented. Superseded in places; see the config UI design record
and the "Reversed after implementation" notes there.

> **What this is.** The design record written before svtplay-arr was built:
> what it had to do, which approaches were rejected and why, and which of
> SVT's and Sonarr's behaviours forced each rule. It is kept because the
> reasoning is the expensive part — several rules here look arbitrary until
> you know the trap they were written against.
>
> It is **not** a user guide and is not maintained as one. For installing and
> running the service, see `deploy/README.md`. Where this document and the
> code disagree, the code is right and this document records what was
> believed at the time.
>
> Hostnames, addresses and paths are placeholders.

## Purpose

Let Sonarr grab SVT Play content the same way it grabs anything else: as a
release from an indexer, downloaded by a download client, imported into
`/mnt/tv`. Quality profiles, cutoff-unmet upgrades, history and failure
handling all keep working, because nothing about the pipeline is special-cased.

The service impersonates two things Sonarr already knows how to talk to — a
Newznab indexer and a SABnzbd download client — and uses `svtplay-dl` to
actually fetch the media.

Radarr support is explicitly out of scope for the first build. The service is
structured so a `movie-search` endpoint can be added later without reshaping
anything.

## Context

The target environment, as established on 2026-08-24. These are the properties
the design assumes; verify the equivalents in your own setup before relying on
any of them.

### Sonarr

| Property | Value it was designed against |
| --- | --- |
| Version | Sonarr 4.0.19 |
| Root folder | A single TV root folder |
| `renameEpisodes` | **False** — load-bearing, see below |
| Completed-download handling | On, with `autoRedownloadFailed` enabled |
| Existing indexers | Newznab by direct URL, plus Torznab via Jackett |
| Prowlarr | Present but managing nothing — not in the path |
| Blackhole client | None |
| Remote path mappings | Keyed on the *download client's host* |

Adding a Newznab indexer by direct URL is the established pattern, and the one
this service targets. Prowlarr is not in the path and should not be introduced
into it: there is no tracker definition to sync, and it would only add a
component that can break the path.

### Storage

Sonarr's completed-downloads directory is typically not local disk. The chain
this was designed against:

```
NFS export on a NAS (NFSv4.2, sec=sys)
  → mounted on the hypervisor host
    → bind-mounted into Sonarr's unprivileged container as /mnt/usenet-completed
```

That export squashed every client identity — root included — to one
user/group for both stored ownership and the permission check. Where that is
the case, the consequences are:

- No `chown` is needed or wanted. Write as any uid; the server decides.
- Files should be mode 664, directories 775. Do not write 0600.
- Inside the container the directory **displays** as `65534:65534`
  (nobody:nogroup). This is unprivileged-container idmap cosmetics and is not
  what NFS enforces. Do not "fix" it.

### SVT

No official public API. The service depends on two reverse-engineered surfaces,
both of which can change without notice.

- **Contento GraphQL**, `https://api.svt.se/contento/graphql`, with
  `ua=svtplaywebb-play-render-prod-client`. `search(query: String!)` returns
  `SearchHit` objects carrying `svtId`, `name`, `title` and an `item` with
  `__typename` (`TvSeries` / `TvShow`).
- **Video resolution**, `https://api.svt.se/video/{svtId}`, returning HLS/DASH
  references and subtitle tracks. This is what `svtplay-dl` uses.

Quirks discovered on 2026-08-24, all of which cost real time:

- `__type` introspection is open; `__schema` is blocked. Type discovery is
  possible one type at a time.
- **The CDN returns cached responses across different query strings.** Two
  different GraphQL queries returned the same body. Requests need cache-busting
  or verification that the response matches the query sent.
- `urqlState` appears only on **video** pages. Show pages carry an escaped JSON
  flight payload in the HTML instead, and their `__NEXT_DATA__` is empty of
  content.
- Content carries `onlyAvailableInSweden: true`. Egress must be Swedish.
- `svtplay-dl` has no `--remux` flag; remuxing is default, `--no-remux` disables.

### The numbering problem

Worked example, "Gift vid första ögonkastet" (Sonarr series 70, tvdbId 288649):

- SVT's page labels the current run **"Säsong 14"** and frames it as an "XL"
  strand.
- TVDB and Sonarr call it **Season 15**, and S15 is the only monitored season.
- SVT episode titles are `"1 Tager du"`, `"2. Jag får kämpa…"`. TVDB episode
  titles are `"TBA"`.
- **S15E01 and S15E02 both carry airDate 2026-08-23.**
- **14** further episodes are listed with future dates and are not
  downloadable. Corrected 2026-08-24 against the captured page: 14 teasers
  carry a non-null `upcomingOverlay`, and only 13 of those overlays say
  "Kommer". The 14th is the *next* episode, flagged with a weekday
  ("Söndag") because it sits in the page's `"id":"upcoming"` module. The
  reliable signal is `upcomingOverlay` being non-null, never the heading
  text: matching "Kommer" misses exactly the episode a weekly grab asks for,
  and offering an upcoming episode costs it permanently (the stable GUID
  gets blocklisted on the failed grab and is still blocklisted when the
  episode really airs).

This single show defeats every simple matching rule, which is why the resolver
below is built the way it is.

## Architecture

One Python service on its own LXC. Two HTTP surfaces, one worker, one SQLite
store.

```
Sonarr host                         svtplay-arr host

  ?t=caps / ?t=tvsearch&tvdbid=&season=&ep=
      ├────────────────────────────►  Newznab API ──► Resolver ──► SVT client
      │  ◄── XML: 0 or 1 release                         │
      │                                                  ▼
  SABnzbd addfile / queue / history              Job store (SQLite)
      ├────────────────────────────►  SAB API           │
      │  ◄── queue, progress, storage path              ▼
      │                                          Download worker
      │                                          (svtplay-dl as library)
      │                                                  │
      │                        incomplete/ ──rename──► completed/
      │                        (same filesystem)
      ▼
  imports from completed/ → the TV root folder
```

### Components

Six units, each independently testable, each with one reason to change.

1. **SVT client** — the only component that knows SVT exists. Wraps GraphQL,
   the show-page flight payload, and video resolution. Owns every quirk listed
   above, including cache-busting.
2. **Resolver** — the only component that knows about mapping. Answers "given
   tvdbid, season, episode, is there an SVT episode I am confident is that one?"
   Returns a match or nothing.
3. **Newznab API** — `t=caps` and `t=tvsearch`.
4. **SAB API** — SABnzbd emulation: `version`, `get_config`, `addfile`, `queue`,
   `history`, and removal.
5. **Download worker** — imports `svtplay-dl` as a library rather than shelling
   out. Writes to `incomplete/`, publishes via atomic rename.
6. **Store** — SQLite: series mappings, jobs, history.

The seams that matter: only the Resolver knows mapping, only the SVT client
knows SVT. An SVT API change touches one file. A mapping rule change touches one
other.

## The Resolver

### Sonarr is the metadata oracle

The resolver asks **Sonarr**, not TVDB, what a given `(tvdbid, season, episode)`
is. Sonarr's episode records carry air dates, titles and monitored state, and
they are by definition the numbering Sonarr will import against. Querying TVDB
directly would introduce a second source of truth that can disagree with the one
that matters, and would need an API key the service otherwise does not require.

### Signals

Two independent signals, both required:

1. **Air date agreement** — SVT publication date within **±1 day** of Sonarr's
   `airDate` (configurable). SVT publishes at 02:00 local on the air date, and
   TVDB air dates are recorded without a timezone, so a one-day window absorbs
   the boundary without widening far enough to admit an adjacent weekly episode.
2. **Episode ordinal** — SVT's position in its run, from `positionInSeason` or
   parsed from title/slug (`"3. Avslöjandet"`, `avsnitt-6`).

Neither works alone. S15E01 and S15E02 share an air date, so date alone is
ambiguous. SVT labels the run "Säsong 14" against TVDB's 15, so any rule reading
SVT's season number walks into the trap.

### First commandment

**Never read SVT's season number.** It is not a reliable statement about TVDB
seasons. Mapping is series-level; within a series, episodes are identified by
ordinal plus date only.

### Confidence gate

Return empty unless all hold:

- A mapping row exists for the tvdbid. Never guess a series.
- Both signals agree.
- The match is **unique**. Two candidates means ambiguity, and ambiguity is
  failure — never resolved by preference.
- The episode is **available**, not flagged upcoming.

Anything else returns an empty result set and the episode stays Wanted. This is
a deliberate choice: some episodes will need manual intervention. That is
preferred over any chance of writing a permanently wrong filename into the
library.

### Release title is the filename

With `renameEpisodes=False`, Sonarr keeps the **downloaded file's** name, not the
release title. These are two strings that must not diverge, so the resolver
generates exactly one, used as both the Newznab release title and the worker's
output filename:

```
Gift vid första ögonkastet - S15E03 - WEBDL-1080p
```

This follows the configured naming format, omitting the episode title while TVDB
reports "TBA". This exact shape passed a manual-import preview with zero
rejections on 2026-08-24.

### Quality is resolved, not assumed

Available quality is queried from SVT at search time and cached, so the indexer
never advertises a quality the download will not deliver — which, since the
release title becomes the filename, would otherwise bake a lie into the library.

## Wire surfaces

### Newznab

`t=caps` **must** advertise:

```xml
<tv-search available="yes" supportedParams="q,tvdbid,season,ep"/>
```

This is load-bearing. Without `tvdbid` in `supportedParams`, Sonarr falls back to
the default `q,rid,season,ep` and searches by title, and the entire design — which
assumes the series is identified exactly — collapses into fuzzy Swedish-title
matching.

`t=tvsearch` returns zero or one release. The download link points back at this
service and yields a synthetic `.nzb` carrying the SVT id, resolved filename and
quality. The `.nzb` must be well-formed NZB XML — Sonarr passes it to the
download client rather than parsing it deeply, but it is written to disk and
must not be an arbitrary blob. The SVT id travels in the `<meta>` block.

**Release GUIDs are stable**, derived from `(svtId, quality)`. This is
load-bearing too. On failure Sonarr blocklists the release and
`autoRedownloadFailed=True` triggers a re-search. If the GUID changed between
searches, the blocklist would never match, and the result is an infinite
grab → fail → regrab loop. Stable GUIDs make Sonarr's own blocklist do the right
thing without any cooperation from this service.

### SABnzbd emulation

Chosen over a blackhole client for three concrete reasons:

- Real queue and progress in Sonarr's Activity tab during a multi-GB download.
- Working failure reporting, so failed grabs mark failed and re-search instead
  of stalling in a queue slot that never moves.
- **Remote path mapping works.** RPMs are keyed on the download client's *host*.
  A blackhole client has no host, so no RPM can ever apply to one and the
  completed folder would have to sit at an identical path on both sides. A SAB
  client reports its hostname, so the existing RPM pattern applies unchanged.

Modes implemented: `version`, `get_config`, `addfile`, `queue`, `history`.

Removal is **not** a mode. Corrected 2026-08-24 against SABnzbd's API docs
and Sonarr's own `SabnzbdProxy`: there is no `mode=delete`. Jobs are removed
with `mode=queue&name=delete&value=NZO_ID` (terminate a running job) and
`mode=history&name=delete&value=NZO_ID` (remove the finished row), both also
accepting a comma-separated list or `all`. `get_config` must also return a
`categories` list — Sonarr derives `OutputRootFolders` from the configured
category and refuses to save the download client without it.

## Paths

Write into a `svtplay/` subdirectory of the **existing** completed-downloads
export that Sonarr already sees.

This keeps blast radius minimal: **no mount changes on Sonarr's host**.
Sonarr-side changes are exactly three — a new indexer, a new download client,
and one remote path mapping keyed to the new host, mirroring the existing
usenet client's entry.

A dedicated SVT dataset would be conceptually tidier but requires a new bind
mount on Sonarr's container. Not worth reconfiguring a working container for
aesthetics.

`incomplete/` and `completed/` must be on the **same filesystem**, or the
atomic rename degrades to a copy and reintroduces the partial-file race.

## Error handling

- **Partial files never reach `completed/`.** Only the atomic same-dataset rename
  publishes. `incomplete/` is swept on startup so a crash cannot leave something
  importable.
- **SVT API drift fails safe.** A parse failure returns an *empty result set*,
  not a 500 — Sonarr sees "no releases" rather than a broken indexer it might
  disable. Logged loudly, surfaced on `/health`, so it is found by monitoring
  rather than by silent absence of downloads.
- **Failures are reported**, landing in SAB history as `Failed` with a real
  `fail_message`.
- **Download concurrency defaults to 1.** Parallel fetches against an
  unofficial media API are a good way to earn a 403; there is no reason to
  hammer SVT.
- **Subtitle sidecars** are named to match the video stem exactly. This is the
  detail most likely to silently regress and gets an explicit test.

## Testing

Strict TDD, pytest, every external boundary behind an interface: SVT client,
Sonarr client, downloader, clock, filesystem.

**Fixtures are recorded from real 2026-08-24 responses** (show page, parsed
episode list, GraphQL search). The suite runs offline, and SVT API drift surfaces
as a fixture mismatch rather than a production mystery.

**Resolver tests are table-driven from the real traps:**

| Case | Expected |
| --- | --- |
| SVT "Säsong 14" vs Sonarr S15 | Resolves to S15; SVT season label ignored |
| S15E01/E02 share airDate 2026-08-23 | Ordinal disambiguates |
| 14 episodes with a non-null `upcomingOverlay` | Never offered |
| Deliberately ambiguous pair | Returns empty |

**Asynchrony is tested as asynchrony.** A mocked transport that answers
synchronously cannot test an asynchronous protocol. That mistake has been made
before on another project: 85 green tests over a coordinator that never waited
for replies and could never have worked against real hardware. The same trap
exists here: the worker is long-running and Sonarr polls `mode=queue`
*during* it. The fake
downloader models progress over simulated time, and a test asserts the queue
reports intermediate states, not just a terminal one.

**Contract tests on both wire surfaces**, specifically that `t=caps` advertises
`tvdbid` in `supportedParams`, and that SAB emulation answers the modes Sonarr
actually calls.

**One opt-in integration test** hits SVT for real and downloads a short clip.
Skipped by default; needs a Swedish IP and real bandwidth.

## Deployment

- Its own container or VM. Deliberately **not** co-located with Sonarr: that
  host is clean and single-purpose, and an extra daemon with its own restart
  and upgrade lifecycle is better isolated.
- systemd unit; NFS mount of the completed-downloads export.
- **Private network only.** No public exposure, no SSO integration — only
  Sonarr talks to it, and exposing a service that shells out to a media
  downloader to the public internet has no upside.

  *Reversed after implementation*: the configuration page (a later design,
  see `2026-08-25-config-ui-design.md`) was published behind an
  SSO-authenticating reverse proxy. The argument above still holds for the
  indexer and download-client surfaces, which remain unexposed.

## Decisions taken

| Decision | Rationale |
| --- | --- |
| Python | `svtplay-dl` is Python and can be imported as a library rather than shelled out — real exceptions instead of parsed stdout |
| Series first, movies later | Movies are strictly easier (imdbid/tmdbid, one file, no episode mapping); prove the hard part first |
| Ambiguity returns empty | A permanently wrong filename in `/mnt/tv` costs more than a Wanted episode |
| SAB emulation over blackhole | Queue visibility, failure reporting, and RPM support |
| Mappings as YAML | Hand-edited, seeded from Sonarr's API. A UI is added only if editing proves annoying |
| Internal-only | Only Sonarr is a client |

The last two were both revisited; see `2026-08-25-config-ui-design.md`.

## Open questions

- **Mapping confirmation UX.** Starting with hand-edited YAML seeded from
  Sonarr's series list. Revisit if it proves tedious at scale.
- **Repository name.** `plex-svt-play` predates the design; the service targets
  Sonarr, not Plex. Rename before this gets deployment references.

## Out of scope

- Radarr / movie search (deferred, not designed out)
- Live SVT channels via M3U/XMLTV tuner — a separate concern
- Plex integration; Plex sees the files as ordinary library items
- Any Prowlarr involvement
