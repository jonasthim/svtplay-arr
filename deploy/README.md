# Deploying svtplay-arr

svtplay-arr impersonates a Newznab indexer and a SABnzbd download client so
Sonarr can grab SVT Play episodes through its normal usenet workflow. It
gets its own container or VM, keyed by its own hostname.

Hostnames and addresses in this document are placeholders — substitute your
own: `sonarr.example.internal` for Sonarr, `svtplay-arr.example.internal`
for this service, and `10.0.0.x` for a private address.

## Where this runs

- **Give it its own container or VM.** Do not co-locate it on Sonarr's host.
  svtplay-arr is a separate service with its own restart/upgrade lifecycle
  and its own filesystem requirements (see Mounts below) — sharing a host
  with Sonarr couples both unnecessarily.
- **The service itself belongs on your private network.** Sonarr and SVT are
  all it needs to reach, and nothing about the indexer or download-client
  side should be exposed. **The configuration page is the one exception** if
  you want remote access to it: publish it through an SSO-authenticating
  reverse proxy — see "The configuration page" below, including why Sonarr
  must never use that public hostname.
- Install under `/opt/svtplay-arr` with its own `.venv`, matching the path
  the systemd unit (`svtplay-arr.service`, this directory) expects.

## Mounts

Sonarr's completed-downloads directory is typically not local disk. A common
chain is:

    NFS export on your NAS (NFSv4.2)
      -> mounted on the hypervisor host
      -> bind-mounted into Sonarr's container as /mnt/usenet-completed

The container or VM running svtplay-arr needs that same export, mounted the
same way, with `incomplete/` and `completed/` as **sibling** subdirectories
of it:

    .../nzbget/completed/svtplay/incomplete  -> /downloads/incomplete
    .../nzbget/completed/svtplay/completed   -> /downloads/completed

**Neither directory may contain the other**, which is why they are siblings
under `svtplay/` rather than `incomplete/` living inside `completed/`.
Startup clears `incomplete/` — that is what stops a partial left by a crash
being mistaken for a live download — so a `completed/` nested inside it
would lose finished episodes on every restart. `create_app` checks this and
**refuses to start** if the two overlap; there is no degraded mode for it.

**`incomplete/` and `completed/` must be on the same filesystem.**
`worker.py` publishes a finished download by `Path.rename`, which is atomic
only within one filesystem. If the two dirs ever end up on different
filesystems (e.g. one bind-mounted from the export, the other left on local
disk), `rename` silently degrades to copy-then-delete, and Sonarr can import
a file that is only half-copied as a permanent, corrupt library entry. This
is exactly what `/health`'s `same_filesystem` field exists to catch —
**check it immediately after deployment**, before pointing Sonarr at this
service:

    curl localhost:9800/health
    # {"status": "ok", "same_filesystem": true, "worker_alive": true,
    #  "active_jobs": 0, "mappings": 3, "mappings_ever_loaded": true,
    #  "mappings_degraded": false,
    #  "svt": {"state": "ok", "degraded": false, "alive": true,
    #          "checked": 3, "failing": 0, "episodes_seen": 41,
    #          "last_success": "2026-08-27T09:00:00+00:00", ...},
    #  "sonarr": {"state": "ok", "degraded": false, "alive": true,
    #             "version": "4.0.10.2544", "series_count": 42, ...}}

If `same_filesystem` is `false`, `status` is `"degraded"`. Fix the mount
layout before proceeding — do not add the indexer/download client to Sonarr
while this is false.

`mappings_degraded: true` means `mappings.yaml` failed to load and the
service is serving the last table that did — the count in `mappings` is that
stale table, not the file. The feed keeps working; the file needs fixing.
`mappings: 0` is not itself reported as degraded (a fresh install has no
mappings yet) but it is the number to check when Sonarr rejects the indexer,
because an empty feed is what makes it do that.

The `svt` block is the SVT canary. Everything else on `/health` reports on
*this* process, which is why an SVT page-format change could empty the feed
while every other field stayed green: the listing returns nothing, the resolver
returns nothing, Sonarr grabs nothing, and no existing check notices. Once an
hour the canary re-checks the mappings you actually have — no hardcoded show,
because a hardcoded slug rots — and reports:

- `"state": "svt"` — **none** of your mappings resolved. That points at SVT
  itself, not at any one show. Nothing will be grabbed until it is fixed.
- `"state": "series"` — some resolved and some did not. Those shows have
  ended, been re-slugged, or moved; `failing_series` names them and each is
  fixed by editing one row. This one deliberately leaves `status` at `"ok"`
  — see below.
- `"state": "unresolvable"` — every mapping resolved on SVT, and at least one
  of them can **never** produce a grab. `unresolvable_series` names each one,
  with a `reason` and a sentence saying which. Like `series`, this
  deliberately leaves `status` at `"ok"` — see below.
- `"state": "unknown"` — nothing has been checked since this process started.
  Deliberately *not* reported as `ok`, and it becomes `"stale"` (and degraded)
  if no check ever completes.
- `"alive": false` — the canary's own background task has died, so nothing is
  checking SVT. Reported the same way `worker_alive` is, and for the same
  reason: a monitoring task that quietly stopped monitoring must not look like
  one that is working.

`state` `svt` and `stale`, and `alive: false`, set the top-level `status` to
`"degraded"`. `series` does **not**, and that is deliberate: a show ends, SVT
retires the URL, nobody gets round to deleting the row, and a `status` that
went red for it would stay red — so within a week the check is background
noise, and the day SVT breaks the listing the `svt` state arrives on a channel
everyone has learned to ignore. One dead row does not stop anything else
working; it is reported in full (`failing`, `failing_series`) so you can alert
on it yourself if you want to, and it is rendered prominently on the
configuration page either way. `unresolvable` is scored the same way and for
a stronger version of the same reason: in the `no_ordinals` case there is
nothing to fix, so a red light over it would be permanent by construction.

**A mapping can be perfectly valid and still resolve nothing, forever.** The
slug is right, SVT answers, the episode list is full — and no episode in it
carries the ordinal the resolver matches on, so every one is refused. From
the feed that is indistinguishable from a series between seasons, which is
why the check now asks a second question of each mapping and reports it:

    #  "svt": {..., "unresolvable": 1, "resolvability_unknown": 0,
    #          "unresolvable_series": [
    #            {"tvdb_id": 253463, "series_title": "Uppdrag granskning",
    #             "svt_slug": "uppdrag-granskning", "reason": "no_ordinals",
    #             "note": "This mapping can never match anything. ..."}]}

Three reasons, because each sends you somewhere different:

| `reason` | What it means | What to do |
| --- | --- | --- |
| `no_ordinals` | No episode SVT lists carries an episode number, so every one is refused before any date is compared. | Nothing can fix it. Remove the row, or keep it knowing it is inert. |
| `no_air_date` | The numbers are there and no episode agrees with a Sonarr episode on both number and air date. | Check the row points at the right programme, and check `air_date_tolerance_days`. |
| `not_in_sonarr` | Sonarr's library has no series with this row's `tvdb_id`. | Remove the row, or add the series back to Sonarr. |

Zero matches is deliberately **not** the condition. Two shapes produce zero
matches and are not broken — Sonarr has no aired episode for the series yet,
and every SVT episode is still upcoming — and neither is reported. Both are
"nothing to compare *yet*".

The **Check** button on the Mappings view answers the same question. It
re-runs both halves live for one row: the slug, and whether its episodes can
match anything Sonarr has. It shares the verdict with the background check,
so it cannot tell you a row is fine while the page beside it says the row
resolves nothing.

The comparison costs one Sonarr episode-list read per mapping per round, plus
one series-list read for the whole round, spread out at the same pace as the
SVT requests. A Sonarr that is not answering degrades **only** this half:
`resolvability_unknown` counts the rows it could not decide, `resolvability_
error` says why, and `unresolvable` stays at 0 rather than reporting a clean
sweep. Everything the `svt` block says about SVT is unaffected, and the
`sonarr` block below is already red in that case.

`svt_canary_interval_minutes` in `config.yaml` (default 60) is how to slow the
check down; it is not on the settings page.

The `sonarr` block is the same check on the other dependency, and it existed
nowhere until 2026-08-28: `/health` reported `ok` through a completely wrong
`sonarr_url` or a mistyped `sonarr_api_key`, because nothing in this service
had ever asked Sonarr a question. That matters more than the SVT gap, not
less — the resolver matches SVT episodes against *Sonarr's* air dates, so
with Sonarr unreachable every search and every RSS poll returns nothing.

Once an hour it calls `/api/v3/system/status` and the series list, and
reports:

- `"state": "ok"` — with `version` and `series_count`. **Check the count**:
  reachable and authenticated are both satisfied by a Sonarr that simply is
  not the one this service is meant to feed, and the size of the library is
  the only field that tells those apart.
- `"state": "sonarr"` — the last check failed. `last_error_reason` says which
  shape (`unauthorized`, `refused`, `unreachable`, `tls`, `not_sonarr`,
  `bad_url`, `timeout`, `http`, `connect`, `unknown`) and `last_error` is a
  sentence saying what to go and change. Each of those is a different
  afternoon.
- `"state": "unknown"` — nothing checked since this process started.
  Deliberately not `ok`, and it becomes `"stale"` (and degraded) if no check
  ever completes.
- `"alive": false` — the check's own background task has died.

Unlike SVT's `series`, **every one of those degrades `status`**. There is no
"one show ended" equivalent here: Sonarr answers or nothing can be grabbed at
all, which is what a red light is for. There is no interval setting either —
that escape hatch exists for SVT because its API is unofficial and this
project has no right to hammer it, and none of that applies to your own
Sonarr, which the resolver already calls several times an hour.

The configuration page's **Settings → Test connection** button is the
on-demand half of the same thing. It tests the values *currently in the
form*, before you save them and before the restart that would apply them, so
you can find out that a key is wrong while you still have it on the
clipboard. It writes nothing.

**If the NFS export squashes identities, do not `chown` anything under this
mount.** An export configured with `mapall_user` / `mapall_group` (or
`all_squash`) maps *every* client identity — including root — to one
user/group on the server side. Whatever UID/GID a `ls -l` inside the
container shows you (commonly `65534:65534`, nobody/nogroup) is
unprivileged-container idmap cosmetics local to that container's view of the
mount; it says nothing about what actually lands on the server, and `chown`
from inside the container cannot change it. Instead, set `UMask=0002` in the
systemd unit (already set below) so svtplay-arr's own writes land as files
`664` / dirs `775`, matching what the rest of the media stack expects.

## Installing the service

**[`install.sh`](../install.sh) in the repository root does all of this**, and
is the supported way to install and to upgrade — it also gives you a versioned
release layout with automatic rollback, which the manual steps below do not.
See [docs/installation.md](../docs/installation.md). What follows is the same
install by hand: the reference for what the script is doing, and the path for
a platform it will not run on.

The unit runs as `User=svtplay Group=media`, not root. Nothing in the steps
below is optional — skip the ownership steps and the service will fail to
start with a `PermissionError` the first time it tries to open its job
database or read its config, because a stock container image does not have
an `svtplay` user, and every path below is root-owned by default.

0. Install OS prerequisites. A minimal Debian container has none of these,
   and the service fails in confusing ways without them — `ffmpeg` in
   particular is what `svtplay-dl` shells out to for muxing, so downloads
   fail late rather than at startup:

       apt install git ffmpeg curl ca-certificates python3-venv

1. Create the system user and its group. On a dedicated container the
   `media` group will **not** already exist — it is a host and NFS-export
   concept, not something a fresh container inherits — so creating it is the
   normal path here, not an edge case:

       groupadd --system media    # normal on a fresh container
       useradd --system --no-create-home --shell /usr/sbin/nologin \
         --gid media svtplay

   `media` is the shared group the rest of the stack uses; it is what the
   NFS export's local-side files and `UMask=0002` writes are meant to match.

2. `git clone`/copy the repo to `/opt/svtplay-arr`, then `uv sync` (or
   equivalent) to build `/opt/svtplay-arr/.venv`. Once built:

       chown -R svtplay:media /opt/svtplay-arr

   (svtplay only needs read+execute here; owning it outright is simplest and
   matches how the other directories below are handled.)
3. Write `/etc/svtplay-arr/config.yaml` (start from
   `deploy/config.example.yaml`, below) and `/etc/svtplay-arr/mappings.yaml`
   (start from `deploy/mappings.example.yaml`; see Mappings below), then lock them down
   to the service account — `config.yaml` holds the Sonarr API key, so this
   directory should not be world- or group-readable. (`SONARR_API_KEY` still
   overrides the file if set, but leave it unset: the configuration page can
   edit the key in `config.yaml` and cannot edit the environment. See "The
   configuration page" below.)

       mkdir -p /etc/svtplay-arr
       chown -R svtplay:media /etc/svtplay-arr
       chmod 750 /etc/svtplay-arr
       chmod 640 /etc/svtplay-arr/*.yaml

   `deploy/config.example.yaml` is a complete, commented starting point —
   every key `Settings.load` understands, each with its default and the
   same help text the configuration page shows:

       cp deploy/config.example.yaml /etc/svtplay-arr/config.yaml

   Only four keys are required: `sonarr_url`, `sonarr_api_key`,
   `incomplete_dir` and `completed_dir`. Keep the API key in this file
   rather than in the unit file — see "The configuration page" below.

   There is deliberately no listen host/port key. The unit passes
   `--host 0.0.0.0 --port 9800` straight to uvicorn (step 4), and the
   download links handed to Sonarr are built from the hostname Sonarr used
   to reach this service, not from configuration.

4. Install `deploy/svtplay-arr.service` to
   `/etc/systemd/system/svtplay-arr.service`, then:

       systemctl daemon-reload
       systemctl enable --now svtplay-arr

   The unit runs `uvicorn --factory svtplay_arr.app:create_app_from_env`,
   which reads `SVTPLAY_ARR_CONFIG` (defaulting to
   `/etc/svtplay-arr/config.yaml`) to build the app. `UMask=0002` is set on
   the unit itself so it applies to every file the process creates, not just
   the ones under the NFS mount. The unit also sets `StateDirectory=svtplay-arr`,
   which makes systemd create `/var/lib/svtplay-arr` owned `svtplay:media`
   before the process starts — this is where `Settings.db_path` defaults to
   (`/var/lib/svtplay-arr/jobs.db`, matching the example above); no manual
   `mkdir`/`chown` is needed for it as long as `db_path` isn't overridden to
   point somewhere else.

5. Check `/health` (see Mounts above) before touching Sonarr.

## Mappings

`/etc/svtplay-arr/mappings.yaml` is the tvdb_id -> SVT series table the
resolver depends on for every search Sonarr sends it.
`deploy/mappings.example.yaml` is a commented starting point; the shape is:

    series:
      - tvdb_id: 288649
        svt_series_id: jpmQD3q
        svt_slug: gift-vid-forsta-ogonkastet
        series_title: Gift vid första ögonkastet

The file must always hold a top-level `series` list. Deleting the rows but
leaving `series:` behind is *not* the way to empty it — that shape is
refused, `/health` reports `mappings_degraded: true`, and the service keeps
serving the last table that loaded. Write `series: []` for genuinely no
mappings (which is what the config page writes when the last row is
removed).

`series_title` must match Sonarr's spelling for that series **exactly** — it
becomes the output filename stem, and Sonarr's `renameEpisodes=False`
setting means Sonarr will never fix a mismatch after the fact; it's
permanent for that file.

Rather than hand-writing rows, use **Find mappings** on the configuration
page. It searches SVT for every Sonarr series that is not mapped yet and
saves the rows where the Sonarr title and the SVT programme name are
identical (casefolded and whitespace-collapsed; a trailing `(2019)` is
stripped from Sonarr's title only), exactly one SVT programme matches, and
no other series already claims that programme. Anything less certain — several
candidates, a near miss, nothing found — is listed for you to accept with
one click, never written. Rows it writes carry `source: auto` so they can
be told apart from ones you confirmed yourself.

The same sweep runs from the install directory, writing nothing at all (it
needs the same config/env as the service):

    SVTPLAY_ARR_CONFIG=/etc/svtplay-arr/config.yaml \
      /opt/svtplay-arr/current/.venv/bin/svtplay-arr-suggest-mappings

(`current/` is the release symlink `install.sh` maintains; drop it for a
hand-built install, where the venv sits directly at `/opt/svtplay-arr/.venv`.)

Confident rows go to stdout as pasteable YAML — slug derived, byte-for-byte
what the page would have written; everything needing a decision goes to
stderr. It **never writes `mappings.yaml`**. A wrong mapping is the class of
mistake this project refuses to make on a guess: it makes every episode of
that show a permanently wrong filename.

## The configuration page

Series mappings and settings, served by the service itself. Two ways in:

- `http://10.0.0.x:9800/config` — direct, from your private network. This is
  all you need if you only ever configure it from home.
- Optionally, a public hostname published through a reverse proxy that
  authenticates in front of it (SSO). Everything below is about that case.

**The page has no authentication of its own.** It can rewrite `config.yaml`
and delete series mappings, and anything on the same network can reach it
directly with no credentials — the same posture as the rest of the API. If
you publish it, the proxy's SSO is therefore the only thing standing between
the public internet and a page that reconfigures the service. Publish the
**whole origin**: `/`, `/config`, `/api` and `/sabnzbd` all behind SSO, with **no bypass
rules, no public path exceptions, and no `/health` carve-out**. Do not add
one. If external health monitoring is wanted, solve it deliberately rather
than by punching a hole in the only gate this page has.

Two proxy lessons worth stating in general terms, because both cost real
time and neither is written down anywhere obvious:

- **Some proxies will not accept an `sso` flag when a resource is
  *created*, only when it is updated** — and a write response can come back
  reporting the flag unset (or unchanged) while having persisted it, or the
  reverse. So: create the resource **with no target**, so it cannot serve
  the page unprotected while you work; enable SSO in a separate update;
  **confirm it with an independent read rather than trusting the write
  response**; and only then attach the target.
- **A per-service firewall between the proxy's network and the service's
  network produces an authenticated 502.** If the proxy nodes sit on a
  different network segment from the service, publishing the resource is not
  enough — that segment needs an explicit allow rule to the service's
  host and port. Without it the resource exists, SSO works, and an
  authenticated user gets a 502 that reads exactly like the service being
  down when it is the path that is blocked.

A proxy-layer health check is deliberately not used here — the service's own
`/health` is the signal, and proxy-layer checks flap.

**Sonarr must keep using the internal address, never the public hostname.**
This one is not obvious and it is expensive. The service builds the `.nzb`
download link it hands Sonarr *from the incoming request's host* — a
deliberate fix for an earlier bug where a hardcoded host silently made every
grab fail. If Sonarr ever reaches the service through the public hostname,
the link it gets back points there too, the fetch goes through the proxy, and
it hits the SSO wall. Every grab fails, and it looks exactly like SVT changed
something. Pin Sonarr's indexer and download-client entries to the internal
address and leave them that way.

**Mappings apply immediately.** Adding a series through the page takes effect
on the next search or RSS poll with no restart.

**Settings need a restart.** The page carries a standing banner naming every
setting that differs between the file and what the running service booted
with, on every visit and not just the save that caused it — so the values
shown are never mistaken for the values in effect. `systemctl restart
svtplay-arr` applies them and the banner clears.

**A setting absent from `config.yaml` shows the default the service is
running on, not an empty box.** A file carrying only the four required keys
leaves `air_date_tolerance_days`, `rss_window_days` and
`max_concurrent_downloads` coming from `Settings`' own defaults. Until
2026-08-25 the page rendered those three blank, the browser posted an empty
string for each, and the save was refused with `'' is not a whole number` —
which made **every** settings save impossible on such an install, including
ones that only touched an unrelated field. Saving now writes those keys out
explicitly at the values already in force; that is a change to the file but
not to what the service does, so the notice says "saved unchanged" and no
restart banner appears.

**The page is usable on a phone.** Below a narrow breakpoint the mappings
table stops being a table and each mapping renders as a stacked block with
labelled lines and full-width controls. It is a CSS media query and nothing
else, so it works with JavaScript off like the rest of the page.

**The series title is copied from Sonarr, never typed.** That string becomes
the permanent filename, because Sonarr runs with `renameEpisodes=False`, so
the page removes the transcription step rather than validating it.

**The Sonarr API key is editable here, and it is rendered into the page.**
Reversed on 2026-08-25: it used to be the one setting that needed SSH, which
is the asymmetry this page exists to remove. The field is masked, with a
Show/Hide button inside it, but understand what that is — the value is in the
page's HTML whichever way that button is set, so it is also in your browser's
cache, in your history, and in any screenshot of this page. Masking stops the
person behind you reading it; it does nothing else. (The button needs
JavaScript, so with JavaScript off the field simply stays masked and stays
editable — which changes none of the above.) **Network isolation, or the SSO in
front of the page if you publish it, is what protects the value**, exactly as
it is what protects the rest of the page.

**Keep the key in `config.yaml`, not in the unit file.**
`Settings.load` gives `SONARR_API_KEY` precedence over the file, so with the
variable set a key saved through the page is written and then silently
ignored — the page says saved, the banner says restart, and the service goes
on using the old value. The page detects that and warns beside the field, and
it suppresses the restart banner for that field rather than promise a restart
that cannot apply it. If you ever put the key back into
`svtplay-arr.service`, expect that warning and edit the file instead.

Worse, if the variable is set **and** `config.yaml` has no key of its own,
the field renders empty, the browser posts an empty value, and the blank
check refuses **every** settings save — including changes to unrelated
fields, with an error about API keys. The page says so when it is in that
state. Unset the variable, or paste the key into the field once to unblock
saving.

A blank or whitespace-only key is refused: it starts the service fine and
reports healthy, then fails every Sonarr call.

**What the page will not do:** restart the service.

Every save writes atomically and keeps the previous file as `<name>.bak`, so a
bad change is one `mv` from recovery. A save that would stop the service
booting — nested download directories, directories on different filesystems,
a non-numeric window — is refused and the file is left untouched.

Note the page rewrites YAML from parsed values, so hand-written comments in
`config.yaml` are replaced by generated ones on the first save.

## Sonarr configuration (three changes)

Add svtplay-arr **directly to Sonarr** as a Newznab indexer, not through
Prowlarr. Prowlarr has nothing to contribute here — there is no tracker
definition to sync, this service is reachable only from your own network,
and routing it through Prowlarr only adds a component that can break the
path:

1. **Indexer** → Settings → Indexers → add Newznab, URL
   `http://svtplay-arr.example.internal:9800`, API path `/api`, categories
   `5000` (TV). Use whatever hostname or address the service is actually
   reachable at — the point is a direct URL. The API Key field is required
   by the form but this service never checks it; any placeholder saves.

   Sonarr tests an indexer on save by firing a bare `t=tvsearch` with no
   `tvdbid`, and **rejects it outright if the channel comes back empty**
   ("Query successful, but no results in the configured categories were
   returned from your indexer"). svtplay-arr answers that query with its
   recent-releases feed, so a normal UI add works. If it ever returns
   nothing — no mapping rows, or nothing published inside
   `rss_window_days` — the save will fail for that reason and not because
   anything is broken. Add a mapping first.

   **RSS Sync is worth leaving on.** That same bare query is what Sonarr
   polls for new episodes, so with it enabled a new episode is grabbed
   within one poll of SVT publishing it. `rss_window_days` (default 7)
   controls how far back the feed looks. Each poll costs one show-page
   fetch plus two SVT requests per candidate in the window (the video
   endpoint and its HLS manifest, which are genuinely per-episode); the
   page and Sonarr's lists are fetched once per sweep, not per candidate.
   That per-candidate cost against an unofficial API is what keeps the
   window small — a daily show at a 30-day window would be roughly 60
   requests every few minutes.
   For the very first grab, though, follow the Manual Import advice at the
   end of this document — turn RSS on once you have seen one import land
   correctly.
2. **Download client** → Settings → Download Clients → add SABnzbd, host
   `svtplay-arr.example.internal`, port `9800`, URL base `/sabnzbd`, category
   `tv`.
3. **Remote path mapping** → Settings → Download Clients → Remote Path
   Mappings → host `svtplay-arr.example.internal`, remote
   `/downloads/completed/`, local `/mnt/usenet-completed/svtplay/completed/`
   (Sonarr's own view of the same export, plus the `completed/` subdirectory
   from the Mounts layout above). Sonarr's completed-download handling must
   be on, with `autoRedownloadFailed` enabled; this mapping is what lets it
   actually find and import the files svtplay-arr publishes.

No root-folder change is needed — this only adds a second source of grabs
into the existing library layout.

Both of Sonarr's forms above require an API key field to be filled in and
will refuse to save without one — but `api/newznab.py` and `api/sab.py`
never read or check an API key at all; svtplay-arr accepts every request
unauthenticated. Any placeholder value (e.g. `unused`) satisfies Sonarr's
form validation. Don't spend time hunting for a "real" key; there isn't
one. (This is acceptable only because the service stays on your private
network with no exposure beyond it, per "Where this runs" above.)

**Do the first grab via Sonarr's Manual Import**, not automatic completed-
download handling. This is unreviewed, self-hosted software talking to an
unofficial API; confirm one episode lands in the right place with the right
name before trusting the automatic path for the rest of a season.

## Known gaps

**An interrupted download is never resumed, only re-grabbed.** If the
service restarts mid-download, `Worker.sweep_incomplete()` deletes the
partial from `incomplete/` and marks the job `Failed` with "interrupted by
restart". With `autoRedownloadFailed` enabled, Sonarr sees the
failure in `mode=history` and re-searches the episode by itself, so this
recovers without intervention — but it restarts the download from zero.
svtplay-dl has no resume, so there is nothing to continue.

*Symptom:* after a restart of this service, an in-flight item disappears
from Sonarr's queue, appears as failed in its history, and is grabbed again
shortly afterwards.

*If you ever do need to clear an item by hand,* remove it from Sonarr's
Activity → Queue; Sonarr calls `mode=queue&name=delete&value=<nzo_id>`,
which fails the job and triggers the same re-search.

**SVT's API is unofficial and reverse-engineered**, not a documented public
API (see the module docstring in `src/svtplay_arr/svt/client.py` for the
specific quirks already found and worked around — a CDN cache-mismatch bug,
and quality living in an HLS manifest rather than the video endpoint). It can
change shape or behavior without notice at any time. `/health` and the
service logs are the only way this will be noticed if it does — there is no
external monitoring of SVT's API surface. Check the logs if grabs stop
finding matches or resolving quality even though the series is in
`mappings.yaml`.
