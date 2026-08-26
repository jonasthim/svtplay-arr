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

These three are read from `config.yaml` but are deliberately not offered on the
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

#### `svt_ua`

**Default: `svtplaywebb-play-render-prod-client`.**

The client identifier sent to SVT's GraphQL API as its `ua` query parameter.

This is not on the configuration page because a wrong value here makes every
SVT call fail — silently, from the operator's point of view — and it exists as
an escape hatch in case SVT ever stops accepting the default, not as a knob
anyone should be turning. Leave it out of your config file unless you have a
specific reason.

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
Sonarr series that is not mapped yet, saves the rows it is certain about, and
lists everything else for you to decide with one click each.

"Certain" means **both** of these, and nothing less:

1. The Sonarr series title and the SVT programme name are **identical** once
   casefolded and whitespace-collapsed. A trailing `(2019)` is stripped from
   **Sonarr's** title only — carrying TVDB's disambiguating year is a fact
   about Sonarr's data, not a statement that a year in a title is noise.
   Stripping it from SVT's name too would make `Big Brother (2019)` match a
   programme actually named `Big Brother (2020)`.
2. **Exactly one** SVT programme (type `TvSeries` or `TvShow`) matches that
   way.
3. **No other series already claims that SVT programme.** Two mappings on one
   programme answer a search for either with episodes of the same show. An
   original and its year-tagged reboot both normalise to the same Sonarr-side
   title, so this is the rule that stops both being written; the second is
   listed under *Already claimed by another series*, one click from being
   accepted deliberately if that is what you want.

Several candidates, a near miss, or nothing found are all surfaced, never
written. Diacritics are deliberately **not** folded during the comparison:
Swedish titles are distinguished by å/ä/ö, and folding them would manufacture
exact matches between genuinely different shows — which is the one error this
rule exists to prevent. A series whose TVDB title differs from SVT's Swedish
title will therefore not match automatically; it becomes a suggestion.

Practical notes:

- Series that already have a row are skipped without an SVT search.
- The whole batch is written in **one** atomic write, or not at all.
- Rows land marked `source: auto`, and the mappings table shows them with an
  **Auto-matched** badge beside the series title, explained in one line under
  the table. Check them — `series_title` is still the permanent filename — and
  remove any that look wrong. Rows with no `source` field, which is every row
  in a file written before this feature, are hand-confirmed and carry no
  badge.
- A suggestion you accept by hand is an ordinary mapping and stays `manual`.
- Bounded at 4 concurrent SVT searches and **200 searches per run**. Hitting
  that limit is reported on the page rather than silently truncating your
  library; run it again to continue, since this run's rows are now mapped and
  skipped.
- If `mappings.yaml` will not parse, nothing is searched and nothing is
  written. Fix the file first.

### The same sweep from the terminal

```sh
SVTPLAY_ARR_CONFIG=/etc/svtplay-arr/config.yaml \
  /opt/svtplay-arr/.venv/bin/svtplay-arr-suggest-mappings
```

Runs exactly the sweep above and **never writes `mappings.yaml`**. Confident
rows go to stdout as pasteable YAML — slug derived, `source: auto` included, so
they are byte-for-byte what the page would have written. Everything needing a
decision goes to stderr, so `> rows.yaml` cannot sweep the undecided part into
a file.

---

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
