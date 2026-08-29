# Configuration

Everything svtplay-arr reads, where it reads it from, and what happens if you
change it.

There are two files and one environment variable:

| | What it holds | Reloads? |
| --- | --- | --- |
| `config.yaml` | Service settings | **No** — restart required |
| `mappings.yaml` | The TVDB → SVT series table | **Yes**, on the next search |
| `$SONARR_API_KEY` | Overrides one setting | No |

`config.yaml` lives wherever `SVTPLAY_ARR_CONFIG` points, defaulting to
`/etc/svtplay-arr/config.yaml`. `mappings.yaml` lives wherever the
`mappings_file` setting points.

Both can be edited by hand or through the configuration page at `/config`. A
complete commented starting point for each ships in
[`deploy/config.example.yaml`](../deploy/config.example.yaml) and
[`deploy/mappings.example.yaml`](../deploy/mappings.example.yaml).

The descriptions below are the same ones the configuration page shows and the
same ones written into `config.yaml` as comments on every save — all three come
from one table in `src/svtplay_arr/config.py`, so they cannot drift apart.

---

## Settings (`config.yaml`)

Four keys are **required** and have no default: `sonarr_url`,
`sonarr_api_key`, `incomplete_dir`, `completed_dir`. The service will not
start without them.

Every other key may be omitted, in which case the service runs on the default
shown here. An omitted key is not "unset" — it is running at its default, which
is why the configuration page renders the default rather than an empty box.

**Every setting on this page needs a service restart to take effect.** After a
save, the configuration page carries a standing banner naming exactly which
settings differ between the file and what the running service booted with, so
the values on screen are never mistaken for the values in force.

### Connection

#### `sonarr_url`

**Required. No default.** Where Sonarr lives.

```yaml
sonarr_url: "http://sonarr.example.internal:8989"
```

Sonarr is this project's metadata oracle: the resolver asks Sonarr — not TVDB
— what a given `(tvdb_id, season, episode)` is, because Sonarr's numbering is
by definition the numbering the import will be filed under.

*Consequence of getting it wrong:* the service starts, `/health` reports `ok`,
and every search and RSS poll silently returns nothing. Leading or trailing
whitespace is stripped on save for exactly this reason — a leading space turns
the URL into a schemeless relative one and produces the same silent failure.
Check the logs for `SonarrApiError`.

#### `sonarr_api_key`

**Required. No default.** From Sonarr's Settings → General.

```yaml
sonarr_api_key: "0123456789abcdef0123456789abcdef"
```

**This value is rendered into the configuration page.** The field is masked
with a Show/Hide button, but understand what that buys you: the value is in
the page's HTML whichever way the button is set, so it is also in your
browser's cache, in your history, and in any screenshot of the page. Masking
stops the person behind you reading it and does nothing else. Network
isolation — or the authenticating proxy in front of the page, if you publish it
— is what protects it. See [SECURITY.md](../SECURITY.md).

Keep the file readable only by the service account (`chmod 640`).

A blank or whitespace-only key is refused on save. It would start the service
fine and report healthy, then fail every Sonarr call.

See also [the environment override](#the-sonarr_api_key-environment-override)
below — it is the one thing that can make a key saved here have no effect.

### Storage

Both of these are **dangerous fields**: the configuration page flags them and
requires an explicit confirmation checkbox before it will write a path change,
because changing them under a running worker orphans in-flight downloads.

#### `incomplete_dir`

**Required. No default.** Downloads in progress.

```yaml
incomplete_dir: "/downloads/incomplete"
```

Must be on the same filesystem as `completed_dir`, or publishing stops being
atomic and a half-copied file can be imported.

**This directory is emptied on every startup.** That is deliberate: it is what
stops a partial file left behind by a crash from being mistaken for a live
download. Do not put anything else in it.

#### `completed_dir`

**Required. No default.** Finished files, where Sonarr imports from.

```yaml
completed_dir: "/downloads/completed"
```

Must be on the same filesystem as `incomplete_dir` and **must not contain
it** — see above for what the startup sweep would do to it.

Two checks enforce this pair:

- **Overlap** — if either directory contains the other, or they are the same
  directory, the service **refuses to start**. There is no degraded mode; the
  consequence is deleting your library once per restart.
- **Same filesystem** — publishing is a `rename()`, which is atomic only within
  one filesystem. Across filesystems it silently degrades to copy-then-delete
  and Sonarr can import a half-copied file as a permanent, corrupt library
  entry. This one does **not** stop startup; it surfaces as
  `same_filesystem: false` and `status: "degraded"` on `/health`. That endpoint
  is the only thing in the service that checks for it, which is why the
  installation guide tells you to look at it before pointing Sonarr anywhere.

A save through the configuration page is refused if either check fails —
including when a directory does not exist yet, since the filesystem check
cannot pass for a path that is not there. That is deliberate: the alternative
is writing a config that starts a service which cannot publish.

### Matching

#### `air_date_tolerance_days`

**Default: `1`.** Minimum: `0`.

```yaml
air_date_tolerance_days: 1
```

How far an SVT publication date may sit from Sonarr's air date and still count
as agreement.

One day is not arbitrary. SVT publishes at 02:00 local time on the air date,
and TVDB air dates are recorded without a timezone, so a one-day window
absorbs that boundary without widening far enough to admit the adjacent weekly
episode.

**This is a dangerous field, and it fails in the direction people do not
expect.** Widening it does not make matching more forgiving — it makes episodes
that share an air date ambiguous with their neighbours, and ambiguity makes
the resolver return *nothing at all*. If episodes stopped resolving after you
raised this, lower it back.

#### `rss_window_days`

**Default: `7`.** Minimum: `1`.

```yaml
rss_window_days: 7
```

How far back the recent-releases feed looks. This is what Sonarr's RSS Sync
polls — every few minutes — and also what it fires when you save the indexer.

Each poll costs one show-page fetch per mapped series, plus two SVT requests
per candidate episode inside the window (the video endpoint and its HLS
manifest, which are genuinely per-episode). That per-candidate cost against an
unofficial API is what keeps the window small: a daily show at a 30-day window
would be roughly 60 requests every few minutes.

The feed is capped at 100 releases regardless of this setting.

#### `max_concurrent_downloads`

**Default: `1`.** Minimum: `1`.

```yaml
max_concurrent_downloads: 1
```

How many downloads run in parallel.

The default is 1 on purpose. Parallel fetches against an unofficial media API
are a good way to earn a 403, and there is no reason to hammer SVT. Values
below 1 are refused on save because they would stop the service booting.

---

## Settings not on the configuration page

These four are read from `config.yaml` but are deliberately not offered on the
form. They are still ordinary settings; edit the file and restart.

#### `mappings_file`

**Default: `/etc/svtplay-arr/mappings.yaml`.** Where the TVDB → SVT series
table lives. See [the mappings file](#the-mappings-file-mappingsyaml) below.

#### `db_path`

**Default: `/var/lib/svtplay-arr/jobs.db`.** The SQLite database holding the
download queue and history that the SABnzbd surface reports to Sonarr.

The packaged systemd unit sets `StateDirectory=svtplay-arr`, which makes
systemd create `/var/lib/svtplay-arr` owned by the service account before the
process starts. If you move `db_path` elsewhere, you must create and chown that
directory yourself.

This database holds only in-flight and completed job rows. Deleting it loses
Sonarr's view of queue and history, not any downloaded media.

A `db_path` on an NFS or CIFS mount works, with a caveat worth knowing: sqlite
cannot use WAL there, so svtplay-arr logs a warning at start and runs in the
slower mode every release before v0.6 used — reads and the download worker's
writes block each other rather than running side by side. It is correct either
way. A local path is the better choice if you have one.

Its schema is versioned (sqlite's `PRAGMA user_version`) and upgraded in place
on start. Nothing is required of you: a database from an earlier release is
recognised and kept, never rebuilt, and each upgrade step runs in a transaction
so a failure leaves the file exactly as it was. The first start after upgrading
logs one line per step, for example:

```
job database /var/lib/svtplay-arr/jobs.db: adopted the jobs table already there, now at schema version 1
migrating the job database to schema version 2
```

Later starts log nothing, because there is nothing to do.

Before the first migration touches a database that has rows in it, a copy is
written beside it — `jobs.db.v1.bak` for a database at schema version 1 — and
that copy is never overwritten by a later one. It is a `VACUUM INTO` snapshot,
so it is a complete, openable database at the schema the previous release
understood: to undo an upgrade, stop the service, move the copy over `jobs.db`,
and reinstall the older release. Copies are not pruned; they are small, and
deleting one is safe once you no longer want to go back to that version. A
fresh install with an empty database gets no copy, and a start that cannot
write the copy stops rather than migrating without it.

Downgrading is the one case that stops the service. A database written by a
newer release is refused before anything at all is written to it, rather than
used in a shape this build does not understand:

```
job database at /var/lib/svtplay-arr/jobs.db is at schema version 3, but this build of svtplay-arr understands version 2. It was written by a newer release; nothing has been changed. Upgrade svtplay-arr again, or point svtplay-arr at a database written by this version.
```

Nothing has been changed and nothing has been lost — reinstalling the newer
release brings the installation back exactly as it was.

If the newer release exited cleanly the file is untouched down to the byte. If
it *crashed*, it left a `-wal` sidecar behind, and opening the database at all
— by svtplay-arr, by the `sqlite3` shell, by any reader — checkpoints that into
`jobs.db` and changes it. Every row and the version stamp survive; there is no
build that could avoid this, because it is what opening a WAL database means.

#### `svt_ua`

**Default: `svtplaywebb-play-render-prod-client`.**

The client identifier sent to SVT's GraphQL API as its `ua` query parameter.

This is not on the configuration page because a wrong value here makes every
SVT call fail — silently, from the operator's point of view — and it exists as
an escape hatch in case SVT ever stops accepting the default, not as a knob
anyone should be turning. Leave it out of your config file unless you have a
specific reason.

#### `svt_canary_interval_minutes`

**Default: `60`.**

How often the SVT canary re-checks your mappings — see
[Is SVT still working?](#is-svt-still-working) below.

It is not on the configuration page for the same reason `svt_ua` is not: it is
an escape hatch, not a knob. Turning it *up* is how you reduce load on SVT's
unofficial API if you are on a metered or rate-limited connection. Turning it
down buys nothing — the failures it detects last until a human fixes them, so
checking more often only means finding out the same thing more times.

It is floored at one minute when the service starts, because `0` in a
hand-edited file would otherwise become a loop firing at SVT as fast as it can
answer.

## There is no listen host or port setting

Deliberately. The systemd unit passes `--host` and `--port` straight to
uvicorn:

```
ExecStart=/opt/svtplay-arr/.venv/bin/uvicorn \
  --factory svtplay_arr.app:create_app_from_env \
  --host 0.0.0.0 --port 9800
```

A `listen_host` setting would never have bound anything. It existed once only
to build the download link handed to Sonarr, and it was the direct cause of a
bug where every grab failed: a `listen_host: "0.0.0.0"` line copied from the
deployment docs became the host Sonarr was told to fetch the `.nzb` from, which
resolved on Sonarr's side to its own loopback with nothing listening. The link
is now derived from the host Sonarr actually reached the service on, so it is
reachable by construction, and the setting is gone.

---

## The `SONARR_API_KEY` environment override

If the environment variable `SONARR_API_KEY` is set to a non-empty value, it
**takes precedence over `sonarr_api_key` in `config.yaml`**.

**The recommended deployment leaves it unset.** The packaged systemd unit sets
it to empty on purpose:

```
Environment=SONARR_API_KEY=
```

The reason is a failure mode this project has a name for. With the variable
set, a key saved through the configuration page is written to the file and then
silently ignored: the page says saved, the banner says restart, you restart,
and the service goes on authenticating with the old value. Nothing anywhere
tells you why.

The configuration page defends against this as best it can — it detects an
active override, warns beside the field, and suppresses the restart banner for
that one field rather than promising a restart that cannot apply it. But the
fix is to unset the variable and keep the key in `config.yaml`, where the page
can actually manage it.

There is one worse case. If the variable is set **and** `config.yaml` has no
key of its own, the field on the page renders empty, the browser posts an empty
value, and the blank-key check refuses **every** settings save — including
changes to unrelated fields, with an error about API keys. The page says so
when it is in that state. Unset the variable, or paste the key into the field
once to unblock saving.

Note that saving through the page never writes an environment-supplied key to
disk. The value is used only to satisfy validation, so the secret does not move
from the environment into a file on its own.

## The `SVTPLAY_ARR_CONFIG` environment variable

Where to find `config.yaml`. Defaults to `/etc/svtplay-arr/config.yaml`. Used
both by the service and by the `svtplay-arr-suggest-mappings` console script.

---

## The mappings file (`mappings.yaml`)

The TVDB → SVT series table. One row per show, either confirmed by a human or
written by the **Find mappings** sweep on the configuration page under a
deliberately narrow confidence rule (see [below](#find-mappings-the-automatic-sweep)).
[The README](../README.md#mappings-the-part-you-would-not-guess) explains why
the general case cannot be derived automatically.

Unlike `config.yaml`, this file is **re-read while the service runs**. Adding a
series takes effect on the next search or RSS poll, with no restart.

### Format

```yaml
series:
  - tvdb_id: 288649
    svt_series_id: jpmQD3q
    svt_slug: gift-vid-forsta-ogonkastet
    series_title: Gift vid första ögonkastet
```

| Field | What it is |
| --- | --- |
| `tvdb_id` | The TVDB id Sonarr uses for the series. This is the key Sonarr searches by and the only thing the resolver looks a series up by. Take it from Sonarr, not from TVDB's website. |
| `svt_series_id` | SVT's own id for the series, from the SVT Play URL or from the configuration page's search results. |
| `svt_slug` | The slug in the SVT Play series URL — the `gift-vid-forsta-ogonkastet` in `https://www.svtplay.se/gift-vid-forsta-ogonkastet`. This is what the episode list is fetched with. |
| `series_title` | Sonarr's spelling of the series title, **exactly**. |
| `source` | Optional. `auto` on a row written by the Find mappings sweep; absent (meaning `manual`) on every row a human confirmed. Nothing resolves or downloads differently because of it — it exists so a mapping nobody confirmed can be found, audited and reverted as a group. |

### `series_title` is the filename

It becomes the output filename stem. With Sonarr's `renameEpisodes` disabled —
which is what this project is built against — Sonarr keeps the downloaded
file's name and will never fix a mismatch afterwards. It is permanent for that
file, diacritics included.

Copy it from Sonarr; never type it. The configuration page removes the
transcription step entirely by taking the string verbatim out of Sonarr's own
series record.

### Rules the loader enforces

- **The file must always hold a top-level `series` list.** Deleting the rows
  but leaving `series:` behind is *not* how to empty it — that shape is
  refused. Write `series: []` for genuinely no mappings, which is exactly what
  the configuration page writes when you remove the last row.
- **Two rows may not share a `tvdb_id`.** The loader refuses the whole file
  rather than guessing which one you meant.
- A row missing any of the four required fields is skipped rather than crashing
  the load; a `tvdb_id` that is not a number rejects the file.
- **A field the loader has never heard of is ignored, not refused.** It reads
  only the keys it knows, which is what lets a newer version add one (`source`
  is the first) without an older version emptying the feed over it. An
  unusable `source` value reads as `manual` and is never a load failure; an
  unrecognised one is preserved verbatim rather than relabelled.

### What happens when it is invalid

The service keeps serving **the last table that loaded successfully**, and logs
a warning. It does not fall back to an empty table, because an empty feed is
what makes Sonarr reject the indexer outright — a defect this project shipped
once already.

`/health` is where this becomes visible:

- `mappings_degraded: true` — the file on disk failed to load, and `mappings`
  describes the stale table being served in its place. The feed is unaffected;
  fix the file.
- `mappings_ever_loaded: false` with `mappings_degraded: false` — the
  fresh-install state. No mappings file exists yet.
- `mappings_ever_loaded: false` with `mappings_degraded: true` — the file is
  invalid and none ever loaded. The feed really *is* empty.

`mappings: 0` is not itself reported as degraded (a fresh install legitimately
has no mappings) but it is the number to look at when Sonarr rejects the
indexer.

Reload detection is based on the file's modification time. On storage with
coarse timestamps, a fix written within the same timestamp tick as the broken
write it replaces can go unnoticed until the next distinguishable change.

### Find mappings: the automatic sweep

The **Find mappings** button on the configuration page searches SVT for every
Sonarr series that is not mapped yet, saves the rows the *episodes* confirm,
and lists everything else for you to decide with one click each.

**The title is only the search query.** It used to be the decision, and that
was a proxy that failed in both directions: it could never map `Married at
First Sight Sweden` to `Gift vid första ögonkastet`, and it treated two
different programmes both named `Vem vet mest?` as the same show. What decides
now is whether the episodes line up.

#### How a candidate is found

Each unmapped series is searched for under its own Sonarr title **and** the
`alternateTitles` Sonarr carries — TVDB usually keeps the original-language
title there, which for a Swedish show is very often exactly SVT's name.
Queries are deduplicated (`Solsidan` and `Solsidan (2019)` are one search),
capped per series, and unambiguous scene release names among the alternates
(`Solsidan.S15`, `Solsidan.2019.1080p.WEB`) are dropped rather than searched
for — SVT's search is a title search and will not match one, so spending a
query on it also costs the useful alternate below it its turn. Only alternates
carrying an actual scene marker — a season tag, a release year, a resolution, a
source or language tag — are dropped, so real titles like `S.H.I.E.L.D.` and
`9.1.1` are still searched for. Every programme returned is a candidate; the few most
promising are the ones actually checked, with a name identical to one of the
series' titles ranked first — not because that makes it right, but because if
two programmes share a name it is those two whose episodes most need
comparing.

#### How a candidate is confirmed

For each candidate the sweep reads that programme's SVT episode list and your
series' episode list from Sonarr, and counts the episodes that correspond
under **the resolver's own rule** — the one in `src/svtplay_arr/matching.py`,
imported rather than restated, at your configured `air_date_tolerance_days`:

- available on SVT (not flagged upcoming),
- published within the tolerance of Sonarr's air date,
- at the same episode number as SVT's ordinal within its run.

An episode counts only when the correspondence is one-to-one in both
directions, so a Sonarr special dated alongside the run or an SVT rerun listed
twice can never inflate the count. Sonarr season 0 is excluded for the same
reason.

A row is written without you confirming it only when **all** of these hold:

1. **Exactly one** candidate corroborates.
2. It corroborates on at least **3** uniquely-matching episodes. Two is
   reachable by coincidence — two weekly shows in the same broadcast slot share
   an air date at episode 1 and again at episode 2 with nothing else in common.
3. **Every other candidate that was checked corroborates on zero.** Not "fewer
   than the winner": a rival that partly agrees has been out-scored, not ruled
   out, and out-scoring is the reasoning that writes permanent filenames for
   the wrong show.
4. **No other series already claims that SVT programme.** Two mappings on one
   programme answer a search for either with episodes of the same show. The
   second is listed under *Already claimed by another series*, one click from
   being accepted deliberately if that is what you want.

**Short runs.** A series that can never reach three — a two-part documentary —
would otherwise be refused forever, which would be the old rule's failure in a
new shape. So when the run is short **on both sides** — fewer than 3 episodes
available on SVT to compare, *and* fewer than 3 episodes with air dates in
Sonarr — *all* of them must correspond and there must be at least **2**. Never
one: a single shared air date at episode 1 is a coincidence any weekly show
produces.

Both sides, because SVT's side alone was not a safe test. A returning
15-season series whose candidate happens to list only two episodes has a
denominator of two, which would drop it into the weak branch and write it on
exactly the coincidence the floor of three exists to refuse — while the
*correct* eight-episode programme for that same series had to clear three.

Sonarr's side counts episodes that *have a date*, not episodes that have aired
by today, because **nothing in this decision reads the system clock**. A clock
running behind under-counts what has aired and would reopen the hole above, and
a container starting before NTP settles is an ordinary event. The practical
consequence: Sonarr schedules a whole season the day it is announced, so a
brand-new show is auto-mapped once its **third** episode is published rather
than its second. That is a delay of one week, not a refusal — and you can
always map it by hand in the meantime.

**No evidence is not confidence.** A series Sonarr knows about that has not
aired, or that SVT has not published, gives nothing to compare — so it is
surfaced for a decision and never written. The same goes for a candidate whose
episode list could not be read: an unchecked candidate is not a refuted one,
so an SVT outage part-way through a series' candidates refuses that series
outright rather than writing on whichever candidate happened to answer.

**Where the guarantee stops.** Rule 3 says "every other candidate *that was
checked*", and only the first 3 ranked candidates are checked. A rival ranked
4th or below is unexamined and does not block a write. It is tempting to argue
this is safe because same-named candidates rank first — but the premise of this
whole design is that the right programme's name often differs from Sonarr's, so
a dangerous rival need not share a name either. What actually bounds it is that
such a rival would have to clear the evidence threshold against the same series
*and* have been returned by SVT below one that already clears it. If you are
mapping a show you know SVT lists several times, check it by hand.

#### What you see for everything else

Every candidate on the results page carries its evidence — "2 of 8 episodes
matched", or "no episodes to compare", or "SVT's episode list could not be
read". That count is the thing you need in order to decide; the heading is
not.

Practical notes:

- Series that already have a row are skipped without any request.
- The whole batch is written in **one** atomic write, or not at all.
- Rows land marked `source: auto`, and the mappings table shows them with an
  **Auto-matched** badge beside the series title, explained in one line under
  the table. Check them — `series_title` is still the permanent filename, and
  still comes only from Sonarr's record — and remove any that look wrong. Rows
  with no `source` field, which is every row in a file written before this
  feature, are hand-confirmed and carry no badge.
- A suggestion you accept by hand is an ordinary mapping and stays `manual`.
- Bounded at 4 concurrent **series**, **200 series per run**, at most 3
  searches and 3 episode-list reads per series, and **600 SVT requests per run
  in total**. The concurrency bound is on series rather than on individual
  requests deliberately: it makes the run depth-first, so a budget that runs
  out leaves the tail of your library unexamined rather than leaving every
  series half-checked and writing nothing at all. Whichever limit bites is
  reported on the page rather than silently truncating your library — a
  partial sweep that reads as a complete one is the failure that matters here.
  Run it again to continue, since this run's rows are now mapped and skipped.
- Widening `air_date_tolerance_days` widens what the sweep will corroborate on,
  not just what the resolver will match. That is deliberate — they are the same
  rule — but it means a generous tolerance makes automatic mapping more willing
  as well as episode matching more willing. The default of 1 is the value both
  were designed around.
- If `mappings.yaml` will not parse, nothing is searched and nothing is
  written. Fix the file first.

### The same sweep from the terminal

```sh
SVTPLAY_ARR_CONFIG=/etc/svtplay-arr/config.yaml \
  /opt/svtplay-arr/current/.venv/bin/svtplay-arr-suggest-mappings
```

(`current/` is the release symlink `install.sh` maintains; a hand-built
install has the venv directly at `/opt/svtplay-arr/.venv`.)

Runs exactly the sweep above, at the same configured `air_date_tolerance_days`,
and **never writes `mappings.yaml`**. Corroborated rows go to stdout as
pasteable YAML — slug derived, `source: auto` included, so
they are byte-for-byte what the page would have written. Everything needing a
decision goes to stderr, so `> rows.yaml` cannot sweep the undecided part into
a file.

---

## Is SVT still working?

This service is built to refuse on doubt and return nothing. That is why your
library is safe — and it is also what makes a failure and a quiet week look
identical from the outside.

If SVT changes what its API returns, the listing finds no episodes, the resolver
returns nothing, the feed goes empty, and Sonarr grabs nothing. Every other
field on `/health` keeps saying `ok`, because every other field is about *this*
process: the worker, the job store, the mappings table, the filesystem. None of
them has ever known whether SVT is there. The episode listing reads an
undocumented API with no stability guarantee, so it breaking is a *when*, not
an *if*.

The canary closes that gap. Roughly once an hour it re-checks **the mappings
you actually have** — not a hardcoded show, because a hardcoded show ends, gets
re-slugged, and then reports a failure that is about the fixture rather than
the service. Checking your own rows answers "do my mappings still work" as a
side effect.

It reads and never writes. The only call it makes is the same read-only episode
listing the resolver already makes, and it never touches `mappings.yaml`,
`config.yaml`, the job store, or Sonarr.

### A show that answers with no episodes counts as a failure

This is the point of the whole thing, and it covers the one break that does
*not* announce itself. A field vanishing from SVT's API now comes back as a
named error — svtplay-arr asks for its fields by name, so SVT says which one
it no longer has, and the canary reports that by name on its next round. What
stays silent is a *semantic* change: the response is still valid and no longer
means what it did. Then the request still succeeds — HTTP 200, a real
answer — with simply nothing in it svtplay-arr recognises as an episode.
Counting that as "SVT answered, so we are fine" would report `ok` through
precisely the outage the check exists to catch.

### The two failure shapes

They need different actions, so they are reported differently:

| `svt.state` | What it means | What to do |
| --- | --- | --- |
| `ok` | Every mapping resolved. | Nothing. |
| `svt` | **None** of them did. | This is SVT itself, not any one show. Nothing will be grabbed until it is fixed. |
| `series` | Some did, some did not. | Those shows ended, were re-slugged, or moved. `failing_series` names them; fix one row each. Everything else keeps working. |
| `no_mappings` | Nothing to check. | Nothing — a fresh install legitimately has no mappings. |
| `unknown` | No check has completed since the service started. | Wait. It is deliberately not reported as `ok`. |
| `stale` | No check has completed for three intervals. | Something is wrong with the check itself; look in the log. |
| `unavailable` | The check's own state could not be read. | Look in the log. |

`svt.alive` is separate, and is reported the same way `worker_alive` is: a
monitoring task that quietly stopped monitoring must not look like one that is
working. `false` means nothing is checking SVT at all — restart the service.

### Which findings turn the light red

`svt` and `stale`, and `alive: false`, set `/health`'s top-level `status` to
`"degraded"`. Each means the same thing at bottom: **nothing is being grabbed,
or nothing currently knows whether it is**, and you cannot fix the cause by
editing a row.

Two states deliberately do *not*, and both exclusions are load-bearing:

- **`series`.** One dead mapping is real and it is yours to fix, but it does not
  stop anything else working — and if it held `status` red until you got round
  to deleting the row, it would hold it red for weeks. A monitoring check that
  is permanently red is one you stop reading, and the day SVT breaks the listing
  the `svt` state would arrive on exactly that channel. This project has shipped
  that mistake once already, as an installer warning that fired on every fresh
  install. It is still reported in full (`failing`, `failing_series`), so you can
  alert on it yourself if you prefer, and it is rendered prominently on the
  configuration page regardless.
- **`unknown`.** For the first interval after a restart nothing is known to be
  *wrong*, and a check that reported degraded on every boot is one you would
  learn to ignore for the same reason. It cannot sit there quietly forever,
  because it becomes `stale` if it never resolves.

Every state with a finding — `svt`, `series`, `stale`, `unavailable`, and a dead
canary task — shows up on the configuration page's status strip, red for the
ones above and amber for `series`.

### What it reports

Alongside `state` and `alive`: `degraded` (does this turn the top-level light
red) and `needs_attention` (is there a finding at all — a superset, and the gap
between them is `series`), `checked` and `failing` (the counts that
separate the two shapes), `episodes_seen`, `last_checked` and `last_success`
with their ages in seconds, `last_error` with `last_error_at`, and
`failing_series` — up to five failing rows by name and slug, with
`failing_series_truncated` when there are more.

`last_success` survives a later failure on purpose. "SVT worked an hour ago and
is failing now" and "SVT has never been confirmed working since this service
started" call for different reactions.

### It is in memory, and resets on restart

What this answers is "is SVT answering *now*", which a restart genuinely
invalidates: a success recorded by the process that died proves nothing about
the one that replaced it. So after a restart the state is `unknown` until the
first check completes — stated explicitly rather than implied by a blank field.

### Load on SVT

With N mappings and the default hourly interval that is N requests per hour.
They are staggered a couple of seconds apart and at most two are ever in flight
at once, so a large library never arrives as a burst; each request has its own
timeout, so a slow or hanging SVT costs one check and can neither stall the
loop nor affect downloads. See `svt_canary_interval_minutes` above to slow it
down further.

## Is Sonarr still working?

The same gap, on the dependency that matters more. Before 2026-08-28 nothing
in this service ever asked Sonarr a question outside serving a request, so
`/health` reported `ok` through a completely wrong `sonarr_url` or a mistyped
`sonarr_api_key` — and the only symptom was that episodes stopped arriving.
The resolver matches SVT episodes against *Sonarr's* air dates, so with
Sonarr unreachable every search and every RSS poll silently returns nothing.

Two things made that worse. The API key is editable through the
configuration page, so it can be mistyped and saved; and settings need a
restart, so there is a real window where the file is right and the running
service is not.

There are now two answers, because there are two questions.

### The background check: is what is running still working?

`SonarrCanary` (`src/svtplay_arr/canary.py`) calls `/api/v3/system/status`
and the series list once an hour, using the settings the service actually
booted with, and reports under `sonarr` on `/health` and as the **Sonarr**
chip on every configuration-page view:

- `ok`, with the version and `series_count`. **Look at the count.** Reachable
  and authenticated are both satisfied by a Sonarr that is simply not the one
  this service is meant to feed — a second instance, a test container, a
  restored backup — and the size of the library is the only field that tells
  those apart.
- `sonarr` — the last check failed. `last_error_reason` says which shape and
  `last_error` says what to change.
- `unknown` — nothing checked since this process started. Deliberately not
  reported as `ok`; it becomes `stale` (and degraded) if no check ever
  completes.
- `alive: false` — the check's own background task has died, reported the way
  `worker_alive` is and for the same reason.

**Every one of those degrades the top-level `status`**, unlike SVT's `series`
shape. There is no "one show ended" equivalent: Sonarr answers or nothing can
be grabbed at all.

There is no interval setting. `svt_canary_interval_minutes` exists because
SVT's API is unofficial and this project has no right to hammer it; your own
Sonarr is a different case, and the resolver already calls it several times an
hour on every RSS poll.

### The Test connection button: would these values work?

**Settings → Test connection** answers the other question, on demand. It
tests **the values currently in the form** — not the file, and not what the
service booted with — because that is what you mean when you click it: you
have just typed a key and want to know before saving it. The file could not
answer that until after a save, and after a save it still could not, because
settings need a restart. An unmodified form holds exactly the effective
on-disk values, so testing it unchanged *is* testing the file.

(One exception, mirroring `Settings.load`: where `$SONARR_API_KEY` is set, the
environment beats the file after any restart, so that is the key tested — and
the result says so.)

It reports the version and the series count on success, and on failure says
which of these it was, because they send you somewhere different:

| Reason | What to change |
| --- | --- |
| `bad_url` | The URL is not an `http://`/`https://` address at all |
| `unreachable` | The hostname does not resolve *from the server* |
| `refused` | Nothing is listening on that port |
| `tls` | The certificate will not verify |
| `connect` | The connection failed for some other reason — firewall, container network, VPN |
| `timeout` | Something accepted the connection and said nothing |
| `unauthorized` | Sonarr rejected the key |
| `not_sonarr` | Something answered and it is not Sonarr's API — a proxy, a login page, a missing base path |
| `http` | Sonarr answered with an unexpected status |

It writes nothing — two GETs against Sonarr and no more — and it works with
JavaScript off, as an ordinary form post that re-renders the page with the
result and everything you typed still in the boxes.

The API key never appears in the result, in an error message, in a log line,
in `/health` or on the status strip. The messages are fixed sentences with
nothing substituted into them but an HTTP status code.

## How saves through the configuration page behave

Worth knowing whichever way you edit, because the two files interact.

- **Writes are atomic, with a backup.** The new content is written to a temp
  file in the same directory, fsynced and chmoded to `0640` before the target
  is touched at all, and the previous contents are kept as `config.yaml.bak` /
  `mappings.yaml.bak`. A bad save is one `mv` from recovery, and a crash
  mid-write can never leave a truncated file that stops the service booting.
- **A save that would break startup is refused, and the file is left
  untouched.** Missing required key, blank required value, nested download
  directories, directories on different filesystems, a non-numeric or
  out-of-range number — all refused before anything is written. The checks are
  the service's own startup checks, not a parallel copy that could drift.
- **Concurrent edits are refused, not merged.** Each form carries the
  modification time of the file it writes. If the file changed since the page
  was rendered — a second tab, or a hand edit over SSH — the save is refused
  with an explanation rather than silently discarding one set of changes.
- **Unrecognised keys are preserved.** A settings save rewrites `config.yaml`
  from parsed values, and any top-level key this version does not understand is
  round-tripped unchanged rather than dropped.
- **Comments are not preserved.** Rewriting from parsed values destroys
  hand-written comments. Every write emits a generated header and one comment
  per key, from the same table that drives the form's help text — but your own
  comments are gone after the first save through the page.
