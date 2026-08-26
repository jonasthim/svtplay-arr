# Installing svtplay-arr

A walkthrough from a fresh container to a working service that Sonarr is
grabbing from.

This document is the ordered path. **[`deploy/README.md`](../deploy/README.md)**
is the operational reference behind it — the NFS and permissions detail, the
reverse-proxy notes, the known gaps — and it is linked from each step rather
than repeated here. Read this to get running; read that when something is
unusual about your environment.

Every hostname and address below is a placeholder. Substitute your own.

## Before you start

- A **Swedish IP address**. SVT geo-restricts everything; without Swedish
  egress nothing here works.
- **Python 3.12+** on the host that will run this.
- A **Sonarr** instance you can reach, and its API key
  (Sonarr → Settings → General → Security → API Key).
- Access to the storage Sonarr imports completed downloads from.

## Step 1: give it its own host

Put svtplay-arr on its own container or VM. Do not co-locate it on Sonarr's
host: it is a separate service with its own restart and upgrade lifecycle and
its own filesystem requirements, and sharing a host couples the two for no
benefit.

The service belongs on your private network. Sonarr and SVT are all it needs
to reach, and nothing about the indexer or download-client surfaces should be
exposed to the internet — none of them authenticate. The configuration page is
the one thing you might want to reach from elsewhere; if you do, put an
authenticating reverse proxy in front of it and read
[`deploy/README.md` § The configuration page](../deploy/README.md#the-configuration-page)
first, along with [SECURITY.md](../SECURITY.md).

## Step 2: the mount layout

This step is the one that can corrupt your library if you get it wrong, so do
it before anything else.

Sonarr's completed-downloads directory is usually not local disk. A typical
chain:

```
NFS export on a NAS
  -> mounted on the hypervisor host
    -> bind-mounted into Sonarr's container as /mnt/usenet-completed
```

The svtplay-arr host needs that same export, mounted the same way, with
`incomplete/` and `completed/` as **sibling** directories inside it:

```
.../completed/svtplay/incomplete   ->  /downloads/incomplete
.../completed/svtplay/completed    ->  /downloads/completed
```

Two rules, both load-bearing:

**Neither directory may contain the other.** Startup clears `incomplete/` —
that is what stops a partial left behind by a crash from being mistaken for a
live download and imported. If `completed/` sat inside `incomplete/`, that
sweep would delete your finished episodes, silently, once per restart. The
service checks this at startup and **refuses to start** if they overlap. There
is no degraded mode.

**They must be on the same filesystem.** A finished download is published into
`completed/` with a single `rename()`, which is atomic within one filesystem:
the file appears whole or not at all, so Sonarr can never import a
half-written file. Across filesystems, `rename()` silently degrades to
copy-then-delete and that guarantee is gone — Sonarr can import a partial file
as a permanent, corrupt library entry. Nothing detects this after the fact.
`/health` checks it before it bites (Step 7).

Siblings under a common parent satisfy both rules, which is why the layout
above looks the way it does.

If your export squashes identities (`mapall_user` / `all_squash`), do **not**
`chown` anything under the mount — see
[`deploy/README.md` § Mounts](../deploy/README.md#mounts) for why, and what to
do instead.

## Step 3: OS prerequisites

A minimal Debian container has none of these, and the service fails in
confusing ways without them. `ffmpeg` in particular is what `svtplay-dl` uses
to mux, so a missing `ffmpeg` fails at the end of a download rather than at
startup:

```sh
apt install git ffmpeg curl ca-certificates python3-venv
```

Step 5 uses [uv](https://docs.astral.sh/uv/) to install the code, which is not
part of any stock container image. Install it now, with its official
installer:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
```

(Step 5 also documents a plain `venv` alternative if you would rather not add
uv.)

## Step 4: the service user

The unit runs as `User=svtplay Group=media`, not root. On a dedicated
container the `media` group will not already exist — it is a host and
NFS-export concept, not something a fresh container inherits — so creating it
is the normal path here:

```sh
groupadd --system media
useradd --system --no-create-home --shell /usr/sbin/nologin --gid media svtplay
```

None of the ownership steps below are optional. Skip them and the service
fails to start with a `PermissionError` the first time it opens its job
database or reads its config, because every path involved is root-owned by
default.

## Step 5: install the code

```sh
git clone https://github.com/jonasthim/svtplay-arr /opt/svtplay-arr
cd /opt/svtplay-arr
uv sync
chown -R svtplay:media /opt/svtplay-arr
```

`/opt/svtplay-arr` is the path the packaged systemd unit expects. `uv sync`
builds `/opt/svtplay-arr/.venv`; if you would rather not use
[uv](https://docs.astral.sh/uv/), `python3 -m venv .venv && .venv/bin/pip
install -e .` produces the same thing at the same path.

## Step 6: configuration files

```sh
mkdir -p /etc/svtplay-arr
cp deploy/config.example.yaml /etc/svtplay-arr/config.yaml
cp deploy/mappings.example.yaml /etc/svtplay-arr/mappings.yaml
chown -R svtplay:media /etc/svtplay-arr
chmod 750 /etc/svtplay-arr
chmod 640 /etc/svtplay-arr/*.yaml
```

`config.yaml` holds the Sonarr API key, which is why the directory is not
world- or group-readable.

Edit `config.yaml`. Only four keys are required:

```yaml
sonarr_url: "http://sonarr.example.internal:8989"
sonarr_api_key: "your-sonarr-api-key"
incomplete_dir: "/downloads/incomplete"
completed_dir: "/downloads/completed"
```

Everything else has a default and may be omitted. The example file is a
complete, commented starting point covering every key the service understands;
[docs/configuration.md](configuration.md) documents each one and its
consequences.

**Keep the API key in this file, not in the unit file.** The
`SONARR_API_KEY` environment variable overrides the file if set, which means a
key saved through the configuration page would be written and then silently
ignored. The packaged unit sets it to empty deliberately. See
[docs/configuration.md § SONARR_API_KEY](configuration.md#the-sonarr_api_key-environment-override).

Then edit `mappings.yaml`, or leave the example row in place for now and add
your own through the configuration page in Step 8. The file must always hold a
top-level `series` list; write `series: []` for genuinely no mappings.

## Step 7: the systemd unit and the first health check

```sh
cp deploy/svtplay-arr.service /etc/systemd/system/svtplay-arr.service
systemctl daemon-reload
systemctl enable --now svtplay-arr
```

The unit runs `uvicorn --factory svtplay_arr.app:create_app_from_env`, reading
`SVTPLAY_ARR_CONFIG` (default `/etc/svtplay-arr/config.yaml`). Two settings on
it matter:

- `UMask=0002`, so every file it writes lands as `664` and every directory as
  `775` — matching what the rest of a media stack expects, without any
  `chown`.
- `StateDirectory=svtplay-arr`, which makes systemd create
  `/var/lib/svtplay-arr` owned `svtplay:media` before the process starts. That
  is where `db_path` defaults to, so no manual `mkdir` is needed unless you
  move it.

**Now check `/health`, before you touch Sonarr:**

```sh
curl localhost:9800/health
```

```json
{"status": "ok", "same_filesystem": true, "worker_alive": true,
 "active_jobs": 0, "mappings": 1, "mappings_ever_loaded": true,
 "mappings_degraded": false}
```

What to look at:

| Field | What it means |
| --- | --- |
| `same_filesystem` | **`false` means stop.** Your two download directories are on different filesystems and publishing is no longer atomic. Fix the mount layout (Step 2) before continuing. |
| `worker_alive` | The download worker task is running. `false` means it died; check the logs. |
| `mappings` | How many series the indexer is currently offering. |
| `mappings_ever_loaded` | `false` with `degraded` false is the fresh-install state: no mappings file yet. |
| `mappings_degraded` | `true` means `mappings.yaml` failed to load and the service is serving the last table that did. The feed keeps working; the file needs fixing. |

`status` is `"ok"` only when the filesystem check passes, the worker is alive,
and the mapping table is not degraded.

Two more checks worth doing while you are here:

```sh
curl 'localhost:9800/api/?t=caps'
# must contain: supportedParams="q,tvdbid,season,ep"

curl -s 'localhost:9800/api/?t=tvsearch' | grep -c '<item>'
# the recent-releases feed; see Step 9 for why this must not be zero
```

## Step 8: add a mapping

Sonarr identifies a series by TVDB id; SVT has its own id and slug and knows
nothing about TVDB. A mapping row is the hand-confirmed bridge, one per show.
See [the README](../README.md#mappings-the-part-you-would-not-guess) for why
this cannot be automatic.

**Through the configuration page** — open `http://<host>:9800/config`, choose
*Add mapping*, type a show title. The page searches SVT and lists Sonarr's
series beside the results; pick one of each and confirm. The `series_title` is
copied verbatim from Sonarr's record rather than typed, which matters because
it becomes the permanent filename.

**Or from the terminal**, if you would rather seed the file:

```sh
SVTPLAY_ARR_CONFIG=/etc/svtplay-arr/config.yaml \
  /opt/svtplay-arr/.venv/bin/svtplay-arr-suggest-mappings
```

This searches SVT for every series title in your Sonarr and prints candidate
rows as YAML to stdout. It **never writes the file**. Check every row, fill in
`svt_slug` by hand from the SVT Play URL (the tool leaves it blank), and paste
what survives into `mappings.yaml`.

Mappings are re-read while the service runs — adding a show takes effect on the
next search, with no restart. Settings are not; those need
`systemctl restart svtplay-arr`, and the configuration page shows a banner
naming exactly which ones are pending.

**Add at least one mapping before Step 9.** Sonarr tests an indexer on save by
firing a search with no parameters, and rejects the indexer outright if the
result is empty. With no mappings, that is exactly what it gets.

## Step 9: connect Sonarr

Three changes in Sonarr, plus one setting to check. Add svtplay-arr **directly
to Sonarr, not through Prowlarr** — there is no tracker definition to sync, and
Prowlarr only adds a component that can break the path.

Use the service's **internal** address in all three. If you have published the
configuration page through a proxy, Sonarr must still not use that hostname:
the download link handed to Sonarr is built from the host Sonarr used to reach
the service, so a proxied hostname sends the `.nzb` fetch into the SSO wall and
every grab fails in a way that looks exactly like SVT breaking. See
[`deploy/README.md` § The configuration page](../deploy/README.md#the-configuration-page).

### 1. Indexer

Settings → Indexers → Add → **Newznab**:

| Field | Value |
| --- | --- |
| URL | `http://svtplay-arr.example.internal:9800` |
| API Path | `/api` |
| API Key | any placeholder, e.g. `unused` |
| Categories | `5000` (TV) |

The API Key field is required by Sonarr's form but this service never checks
one. Any value saves. Don't go looking for a real key; there isn't one.

If the save fails with *"Query successful, but no results in the configured
categories were returned from your indexer"*, that is the empty-feed rejection
from Step 8 — add a mapping and try again. Nothing is broken.

**Leave RSS Sync on.** That same parameterless query is what Sonarr polls for
new episodes, so a new episode is grabbed within one poll of SVT publishing
it. How far back the feed looks is `rss_window_days` (default 7); each poll
costs SVT requests per candidate in that window, which is why the window is
small. See [docs/configuration.md](configuration.md#rss_window_days).

### 2. Download client

Settings → Download Clients → Add → **SABnzbd**:

| Field | Value |
| --- | --- |
| Host | `svtplay-arr.example.internal` |
| Port | `9800` |
| URL Base | `/sabnzbd` |
| API Key | any placeholder |
| Category | `tv` |

### 3. Remote path mapping

Settings → Download Clients → Remote Path Mappings → Add:

| Field | Value |
| --- | --- |
| Host | `svtplay-arr.example.internal` |
| Remote Path | `/downloads/completed/` |
| Local Path | `/mnt/usenet-completed/svtplay/completed/` |

The local path is Sonarr's own view of the same export, plus the `completed/`
subdirectory from Step 2. This mapping is what lets Sonarr actually find the
files svtplay-arr publishes. Sonarr's completed-download handling must be on,
with `autoRedownloadFailed` enabled.

No root-folder change is needed; this only adds a second source of grabs into
your existing library layout.

### 4. Check `renameEpisodes`

Settings → Media Management → Episode Naming → **Rename Episodes**.

This project is built and tested against **Rename Episodes off**, which means
Sonarr keeps the downloaded file's own name. svtplay-arr therefore generates
one string that is simultaneously the release title and the output filename,
so the two cannot diverge:

```
Gift vid första ögonkastet - S15E03 - WEBDL-1080p
```

This is also the reason the resolver refuses to guess: with renaming off,
the name svtplay-arr chooses is permanent, and a wrong match is not something
a retry fixes. See
[the README](../README.md#why-it-refuses-rather-than-guesses).

If you run with renaming **on**, Sonarr will rename imports to your own format
and the strictness above buys you less — but nothing has been tested in that
configuration, and the `series_title` in each mapping row is still what the
downloaded file is named before Sonarr sees it.

## Step 10: the first grab

**Do the first grab through Sonarr's Manual Import**, not automatic
completed-download handling. This is unreviewed, self-hosted software talking
to an undocumented API. Confirm that one episode lands in the right place with
the right name before trusting the automatic path with a whole season.

In Sonarr, search for a single episode of a mapped series, grab it, and watch
Activity → Queue. You should see a real percentage climbing. When it finishes,
check that the file landed in `completed/` under the expected name and that
Sonarr imported it where you expect.

## If something goes wrong

| Symptom | Where to look |
| --- | --- |
| Sonarr rejects the indexer on save | Empty feed. Add a mapping (Step 8), then re-check `curl -s 'localhost:9800/api/?t=tvsearch' \| grep -c '<item>'`. |
| Searches always come back empty for one episode | The resolver refused. `journalctl -u svtplay-arr` logs the reason each time — no mapping, no air date, wrong ordinal, or ambiguity. |
| `/health` says `same_filesystem: false` | Step 2. Do not grab anything until this is fixed. |
| `/health` says `mappings_degraded: true` | `mappings.yaml` failed to load. The service is serving the last good table. Check the logs for the parse error; a `series:` key with no rows under it is the usual cause — write `series: []`. |
| Grabs fail at the `.nzb` fetch | Sonarr is reaching the service on a hostname it cannot fetch back from. See Step 9. |
| A download stalls at 0% forever | The `.nzb`'s declared size did not survive. Check the logs around `addfile`. |
| Downloads fail near the end | Missing `ffmpeg` (Step 3). |
| An in-flight download vanished after a restart | Expected. Partials are discarded and the job is failed so Sonarr re-searches; `svtplay-dl` has no resume. See [`deploy/README.md` § Known gaps](../deploy/README.md#known-gaps). |

Settings changes need `systemctl restart svtplay-arr`. Mapping changes do not.

## Next

- [docs/configuration.md](configuration.md) — every setting and its
  consequences
- [docs/how-it-works.md](how-it-works.md) — what actually happens between a
  search and a file
- [`deploy/README.md`](../deploy/README.md) — the operational reference,
  including publishing the configuration page behind a proxy
- [SECURITY.md](../SECURITY.md) — what you are exposing, and to whom
