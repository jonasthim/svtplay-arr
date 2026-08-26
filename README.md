# svtplay-arr

Lets [Sonarr](https://sonarr.tv) manage television from **SVT Play**, the
Swedish public broadcaster's streaming service, by pretending to be the two
things Sonarr already knows how to talk to: a Newznab indexer and a SABnzbd
download client.

Sonarr searches it like any indexer, grabs from it like any download client,
and imports the result into your library the normal way. Nothing about the
pipeline is special-cased, so quality profiles, monitoring, history and
failed-download handling all keep working.

---

## Before you read further: you need a Swedish IP address

SVT Play geo-restricts its content — every item carries
`onlyAvailableInSweden: true`. This service, and the `svtplay-dl` it uses to
fetch media, must run somewhere with Swedish egress. Outside Sweden nothing
here will work, and no part of this project attempts to change that.

---

## The problem

Sonarr automates a TV library: you tell it which shows you want, and it
watches indexers for new episodes, grabs them, renames and files them.

SVT Play is free, public-service Swedish television. Anyone in Sweden can
watch it in a browser at no cost. But there is no usenet indexer or torrent
tracker behind it, and Sonarr only knows how to ask an indexer. So the one
source of Swedish TV that is *legitimately free to watch* was the one source
Sonarr could not manage — episodes had to be fetched by hand and filed by
hand, forever outside the system that manages everything else.

`svtplay-dl` has been able to download from SVT Play for years. What was
missing was something that speaks indexer to Sonarr, so that the download
becomes an ordinary grab rather than a manual chore.

## Is this for you?

Probably, if all of these are true:

- You run Sonarr, and you watch shows that are on SVT Play.
- You are in Sweden (or the machine running this is).
- You are comfortable running a small self-hosted Python service on your own
  network — a container or VM, a systemd unit, a YAML config file.

Probably not, if:

- You want Radarr / movies. Not supported (see [Limitations](#limitations-and-status)).
- You want something that works with no configuration. Each show needs a
  hand-confirmed mapping row before it can be grabbed (see
  [Mappings](#mappings-the-part-you-would-not-guess)).
- You want to expose this to the internet without putting an authenticating
  proxy in front of it. Don't.

## How it works

The service runs on its own host and speaks two HTTP protocols back to
Sonarr. It does not talk to Sonarr first; Sonarr always initiates.

```
   Sonarr                              svtplay-arr                    SVT

   search for a new episode
   ?t=tvsearch&tvdbid=&season=&ep=
       ├──────────────────────────►  Newznab surface
       │                                   │
       │                                   ▼
       │                               Resolver ──── "which SVT video
       │                                   │          is this episode?"
       │                                   ├──────────────────────────►  show page
       │                                   │  ◄────────────────────────  episode list
       │                                   ├──────────────────────────►  /video/{id}
       │  ◄─── XML: 0 or 1 release ────────┘  ◄─────────  HLS manifest (quality)
       │
   grab it: GET the release's link
       ├──────────────────────────►  a synthetic .nzb carrying the SVT id
       │
   hand the .nzb to the download client
   POST mode=addfile
       ├──────────────────────────►  SABnzbd surface ──► job queue (SQLite)
       │                                                       │
   poll mode=queue for progress                                ▼
       ├──────────────────────────►                    Download worker
       │  ◄─── percentage, bytes                       runs svtplay-dl ──► media
       │                                                       │
       │                                incomplete/ ──rename──► completed/
       │                                        (one filesystem, atomic)
   read mode=history: done, and where
       ├──────────────────────────►
       │  ◄─── storage path
       ▼
   import from completed/ into the TV library
```

In words:

1. **The fake indexer.** Sonarr asks "do you have `tvdbid=288649`, season 15,
   episode 3?" in the Newznab protocol. The service answers with zero or one
   release, in the RSS/XML shape Newznab defines. A release is not a file — it
   is a title, a size, and a link.
2. **The fake download client.** Sonarr fetches that link and gets a small
   synthetic `.nzb` file with SVT's video id in it, then POSTs that `.nzb` to
   what it believes is a SABnzbd instance. The service accepts it, creates a
   job, and reports genuine progress while it runs.
3. **The actual download.** A worker calls
   [`svtplay-dl`](https://svtplay-dl.se) with the SVT id, writing into an
   `incomplete/` directory. When it finishes, the file is published into
   `completed/` by a single atomic rename, so a half-written file can never be
   visible to Sonarr.
4. **The import.** Sonarr's normal completed-download handling picks the file
   up from `completed/` and files it, exactly as it would a usenet download.

There is no SVT API to speak of. SVT publishes no documented public API; the
service reads the GraphQL endpoint their own web player uses, scrapes the
episode list out of the show page's markup, and reads the available quality
out of an HLS manifest. All of that lives in one module
(`src/svtplay_arr/svt/client.py`) precisely because it can change without
notice.

## Mappings: the part you would not guess

This is the one concept that trips up everyone reading about this project for
the first time.

Sonarr identifies a series by its **TVDB id** and asks for episodes by
**season and episode number**. SVT knows nothing about any of that. SVT has
its own opaque series id and a URL slug, its episodes carry no TVDB id, and
its numbering does not line up with TVDB's.

That last point is not theoretical. On the show this project was built
against:

- SVT's page labels the current run **"Säsong 14"**. TVDB and Sonarr call the
  same run **Season 15**.
- Two episodes, S15E01 and S15E02, share the same air date.
- Fourteen further episodes are listed on the page but not yet downloadable.

So there is no automatic way to know that the thing Sonarr calls
`tvdbid=288649` is the thing SVT calls `jpmQD3q` /
`gift-vid-forsta-ogonkastet`. A **mapping** is that bridge, written down once
per show, by a human:

```yaml
series:
  - tvdb_id: 288649
    svt_series_id: jpmQD3q
    svt_slug: gift-vid-forsta-ogonkastet
    series_title: Gift vid första ögonkastet
```

Adding a show means adding one of these rows — through the configuration page
(which searches SVT for you and copies the title straight out of Sonarr), or
by hand.

**Find mappings** on the configuration page sweeps your whole Sonarr library
at once: it searches SVT for every series that is not mapped yet and saves the
rows it is certain about. "Certain" is deliberately narrow — the Sonarr title
and the SVT programme name must be *identical* once casefolded, and exactly one
SVT programme may match. A trailing `(2019)` is stripped from **Sonarr's** title
only, because carrying TVDB's disambiguating year is a fact about Sonarr's data;
stripping it from SVT's name too would make `Big Brother (2019)` and `Big
Brother (2020)` the same show. And no two series may be mapped to one SVT
programme, so an original and its year-tagged reboot cannot both claim it. Anything less —
several candidates, a near miss, nothing found — is listed for you to decide,
one click each, and never written. A wrong series mapping is exactly the
mistake this project refuses to make on its own, and one level larger than a
wrong episode match: it makes *every* episode of that show a permanently wrong
filename.

Rows written that way carry `source: auto` in the file and an **Auto-matched**
badge in the mappings table, so a mapping nobody confirmed is never
indistinguishable from one you picked yourself — in the file or on the page. Diacritics
are deliberately *not* folded during the comparison: Swedish titles are
distinguished by å/ä/ö, and folding them would manufacture exact matches
between genuinely different shows.

`svtplay-arr-suggest-mappings` runs the same sweep from a terminal and prints
what it would write, without writing anything.

Mappings are re-read while the service runs, so adding a show takes effect on
the next search with no restart.

## Why it refuses rather than guesses

**This is the most important design fact in the project.**

Given a mapped series, the resolver still has to decide *which SVT video* is
the episode Sonarr asked for. It requires **two independent signals to agree**:

1. **Air date.** SVT's publication date must be within a tolerance (default
   ±1 day) of the air date Sonarr holds for that episode.
2. **SVT's own ordinal** — the episode's position within its run, parsed from
   SVT's data. Never SVT's season number, which as shown above is simply not
   a statement about TVDB seasons.

Plus: the episode must actually be available (not flagged as upcoming), and
the match must be **unique**. Two plausible candidates is not a tie to be
broken by preference — it is ambiguity, and ambiguity returns nothing.

If any of that fails, the search comes back empty and the episode stays
Wanted in Sonarr until a human intervenes.

That looks unhelpfully strict until you know why. Sonarr is expected to run
with **`renameEpisodes` disabled**, which means Sonarr keeps the *downloaded
file's* name rather than imposing its own. The title this service puts on a
release therefore becomes the permanent filename in your library. And because
release GUIDs here are stable across searches — deliberately, so that Sonarr's
blocklist works — a bad grab is not something a retry fixes.

So the trade is explicit and it is not a bug: **some episodes will need manual
intervention, and that is preferred over any chance of writing a permanently
wrong filename into the library.** Every rule in
`src/svtplay_arr/resolver.py` exists because of a specific trap observed in
real SVT data; each one is commented with the trap it was written against.

## The configuration page

The service serves its own configuration page at `/config`. It manages the
mapping table (search SVT, pick a show, pick the matching Sonarr series,
confirm) and the settings file. Mapping changes apply immediately; setting
changes need a restart, and the page says so with a banner naming exactly
which settings are pending.

**The page has no authentication of its own,** and it can rewrite the
service's configuration and delete mappings. It expects to sit on a trusted
network, or behind a reverse proxy that authenticates in front of it. It also
renders the Sonarr API key into the page by deliberate choice. See
[SECURITY.md](SECURITY.md) before you expose it anywhere.

## Requirements

- **A Swedish IP address.** See the top of this file.
- **Python 3.12 or newer.**
- **`ffmpeg`**, which `svtplay-dl` shells out to for muxing. Without it,
  downloads fail late rather than at startup.
- **Sonarr**, reachable from this service, with an API key. Developed and run
  against Sonarr v4 (4.0.19); other versions are untested.
- A host to run it on — its own container or VM is recommended, not
  co-located with Sonarr.
- Access to Sonarr's completed-downloads storage, with an `incomplete/` and a
  `completed/` directory as **siblings on one filesystem**. This one is not
  negotiable; see [docs/installation.md](docs/installation.md) for why.

## Quick start

The full walkthrough — service user, systemd unit, mount layout, connecting
Sonarr — is in **[docs/installation.md](docs/installation.md)**, and the
operational deployment reference is **[deploy/README.md](deploy/README.md)**.
The short version, to see it run:

```sh
git clone https://github.com/jonasthim/svtplay-arr
cd svtplay-arr
uv sync                          # or: python -m venv .venv && .venv/bin/pip install -e .

cp deploy/config.example.yaml config.yaml
cp deploy/mappings.example.yaml mappings.yaml
# Edit config.yaml: sonarr_url, sonarr_api_key, incomplete_dir, completed_dir,
# and point mappings_file/db_path somewhere writable.

SVTPLAY_ARR_CONFIG=config.yaml uv run uvicorn \
  --factory svtplay_arr.app:create_app_from_env --host 127.0.0.1 --port 9800
```

Then, in another terminal:

```sh
curl localhost:9800/health
# {"status":"ok","same_filesystem":true,"worker_alive":true,"active_jobs":0,
#  "mappings":1,"mappings_ever_loaded":true,"mappings_degraded":false}

curl 'localhost:9800/api/?t=caps'      # must contain tvdbid in supportedParams
```

If `same_filesystem` is `false`, stop and fix the directory layout before
going any further — that field exists to catch the one mistake that can
corrupt your library.

Open `http://localhost:9800/config` to add a mapping, then follow
[docs/installation.md](docs/installation.md) to add the indexer and download
client in Sonarr.

## Documentation

| Document | What it covers |
| --- | --- |
| [docs/installation.md](docs/installation.md) | Fresh container to working service, and connecting Sonarr |
| [docs/configuration.md](docs/configuration.md) | Every setting, its default and its consequences; the mappings file format |
| [docs/how-it-works.md](docs/how-it-works.md) | The request flow, the resolver, the wire surfaces — for contributors |
| [deploy/README.md](deploy/README.md) | The operational deployment reference |
| [docs/design/](docs/design/) | The design records: why each strange thing is the way it is |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and this project's (strict) testing culture |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability, and the honest security posture |

## Limitations and status

Read this section. It is the honest one.

- **SVT's API is unofficial, undocumented and reverse-engineered.** It has no
  stability guarantee of any kind. It can change shape or behaviour without
  notice and break this service, and there is no way to find out in advance.
  Known quirks already worked around include a CDN that returns cached
  response bodies belonging to *different* queries, and quality information
  living in an HLS manifest rather than in the video endpoint.
- **Sonarr only. No Radarr, no movies.** The Newznab surface advertises
  `movie-search available="no"`. This is deferred, not designed out, but it is
  not implemented and nothing about it is tested.
- **The configuration page has no authentication.** It expects a trusted
  network or an authenticating proxy in front of it. See
  [SECURITY.md](SECURITY.md).
- **Neither do the indexer and download-client surfaces.** Sonarr's forms
  require an API key field, but this service never checks one; any
  placeholder value satisfies Sonarr. Every request is accepted
  unauthenticated. This is acceptable only on a private network.
- **This has been run in one person's homelab, against one Sonarr version,
  against a handful of shows.** It is not battle-tested software. Expect
  rough edges, and do your first grab through Sonarr's Manual Import rather
  than trusting automatic handling with a whole season.
- **Interrupted downloads are never resumed, only re-grabbed.** `svtplay-dl`
  has no resume. A restart mid-download discards the partial and marks the
  job failed, which (with `autoRedownloadFailed` on) makes Sonarr re-search
  by itself — but from zero.
- **Episodes that do not resolve stay Wanted.** By design; see above. The
  service logs why each time it refuses.
- **The mapping table is manual.** One hand-confirmed row per show.
- **`incomplete/` and `completed/` must be siblings on one filesystem.** The
  service refuses to start if one contains the other, and reports `degraded`
  on `/health` if they are on different filesystems.

## Legal and usage

SVT Play is free to watch in Sweden, and this tool automates something a
person could already do by hand in a browser.

It is intended for personal use of content you are entitled to access. You are
responsible for complying with SVT's terms of service and with the law where
you are. Downloaded material is not yours to redistribute.

This project is not affiliated with, endorsed by, or connected to Sveriges
Television (SVT), the Sonarr project, or the `svtplay-dl` project.

## Licence and credits

svtplay-arr is released under the [MIT Licence](LICENSE).

The actual downloading is done by **[`svtplay-dl`](https://svtplay-dl.se)**,
which is a separate project by Johan Andersson, also MIT-licensed. It is used
as a Python library — svtplay-arr imports it and calls it on a worker thread
— and it, not this project, is what knows how to fetch media from SVT. This
project would not exist without it.

Recorded SVT Play responses under `tests/fixtures/svt/` are third-party
content, copyright Sveriges Television AB, retained solely as test data. They
are not part of the licensed work; see that directory's README.
