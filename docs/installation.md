# Installing svtplay-arr

A fresh container to a working service that Sonarr is grabbing from.

The install is one command. The parts that are not one command are the two
things a script cannot decide for you: **where your downloads live** (Step 1,
which can corrupt your library if you get it wrong) and **how Sonarr talks to
this service** (Step 5). Everything in between is
[`install.sh`](../install.sh).

This document is the ordered path. **[`deploy/README.md`](../deploy/README.md)**
is the operational reference behind it — the NFS and permissions detail, the
reverse-proxy notes, the known gaps — and it is linked from each step rather
than repeated here. "[Doing it by hand](#doing-it-by-hand)" at the bottom is
the same install without the script, for an unsupported platform or for
anyone who wants to know exactly what is being done to their host.

Every hostname and address below is a placeholder. Substitute your own.

## Before you start

- A **Swedish IP address**. SVT geo-restricts everything; without Swedish
  egress nothing here works.
- A host you have **root** on. Its own container or VM — see Step 0.
- A **Sonarr** instance you can reach, and its API key
  (Sonarr → Settings → General → Security → API Key).
- Access to the storage Sonarr imports completed downloads from.

You do **not** need Python. `install.sh` uses
[uv](https://docs.astral.sh/uv/), and uv brings its own interpreter — there is
no system Python version to satisfy, no `python3-venv`, nothing to keep in
step with your distribution. You do not need `ffmpeg` in advance either on a
Debian or Ubuntu host; the script installs it. On anything else it tells you
what is missing instead of guessing at package names for a distribution
nobody has tested this on.

## Step 0: give it its own host

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

## Step 1: the mount layout

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
`/health` checks it before it bites, and the installer prints the answer at
the end of every run (Step 3).

Siblings under a common parent satisfy both rules, which is why the layout
above looks the way it does.

If your export squashes identities (`mapall_user` / `all_squash`), do **not**
`chown` anything under the mount — see
[`deploy/README.md` § Mounts](../deploy/README.md#mounts) for why, and what to
do instead.

## Step 2: run the installer

Download it, read it, run it:

```sh
curl -fsSLO https://raw.githubusercontent.com/jonasthim/svtplay-arr/main/install.sh
less install.sh
sudo bash install.sh
```

The middle line is not a formality. This is a root-level installer for
unreviewed, self-hosted software; reading it first is the correct instinct and
the script is written to be read. If you would rather watch it decide before
it does anything, `--dry-run` prints every action and changes nothing — and
does not need root:

```sh
bash install.sh --dry-run
```

There is deliberately no `curl … | sudo bash` line here. Piping a URL into a
root shell is exactly what this script refuses to do to *you* when it installs
uv — see "How uv is installed" below — and it would be incoherent to argue
that on one line and ask for it on the next. Download it, read it, run it.

### What it does

**It detects whether this is an install or an upgrade** and does that; you run
the same command either way.

On a fresh host it:

1. checks the platform, and installs `ffmpeg`, `git` and `curl` with `apt` if
   they are missing (on a host without `apt` it stops and names what to
   install);
2. installs `uv` if it is not already there — pinned, downloaded, and verified
   against a checksum recorded in the script;
3. creates the `media` group and the `svtplay` system user. On a dedicated
   container the `media` group will not exist yet; that is the normal path
   here, not an edge case;
4. clones the source at a pinned ref into
   `/opt/svtplay-arr/releases/<commit>/` and builds its `.venv` there. uv
   downloads a suitable Python into `/opt/svtplay-arr/python/`;
5. writes `/etc/svtplay-arr/config.yaml` and `mappings.yaml` from the shipped
   examples — **only if they do not already exist** — as `0640`, owned
   `svtplay:media`, in a `0750` directory, because `config.yaml` holds your
   Sonarr API key;
6. installs the systemd unit, points `/opt/svtplay-arr/current` at the new
   release, and enables and starts the service;
7. polls `/health` and prints what it says.

The layout it leaves behind:

```
/opt/svtplay-arr/releases/<commit>/   one directory per installed commit,
                                      each with its own .venv
/opt/svtplay-arr/current -> releases/<commit>
/opt/svtplay-arr/python/              uv-managed interpreters, shared
/etc/svtplay-arr/config.yaml          yours; the installer never rewrites it
/etc/svtplay-arr/mappings.yaml        yours; the installer never rewrites it
/etc/systemd/system/svtplay-arr.service
```

The unit's `ExecStart` goes through the `current` symlink. That is what makes
an upgrade cheap to undo: the previous release is still on disk with its
environment intact, so rolling back is a symlink flip and a restart rather
than a rebuild.

### Options

| Option | What it does |
| --- | --- |
| `--dry-run` | Print every action, change nothing. Does not need root. |
| `--ref REF` | Install a specific branch, tag or commit. The default is the newest `vN.N.N` tag — see "Which version you get". |
| `--prefix DIR` | Install somewhere other than `/opt/svtplay-arr`. Must be a directory of svtplay-arr's own; see below. |
| `--config-dir DIR` | Configuration somewhere other than `/etc/svtplay-arr`. Same rule. |
| `--unit-dir DIR` | Unit directory other than `/etc/systemd/system`. |
| `--health-timeout N` | How long to wait for `/health` (default 90s). |
| `--keep N` | Old releases to keep for rollback (default 3). Each carries its own virtualenv, so this costs disk; `--keep 2` leaves one rollback target. |
| `--help` | The full list. |

Running it twice is safe. The second run finds the same commit already built,
active and *running*, and stops after re-checking `/health`. "Running" is
checked rather than assumed: a run interrupted between activating a release
and restarting the service leaves the two disagreeing, and the next run
finishes the job instead of declaring victory.

`--prefix` and `--config-dir` are the two flags to be careful with, and the
script is careful with them for you. It chmods and recursively chowns those
directories to `svtplay:media`, as root, so it **refuses** a shared system
directory (`/`, `/etc`, `/opt`, `/usr`, `/var`, `/home` and the like) and
refuses to adopt a directory that already exists, is not empty, and does not
look like an svtplay-arr installation. `--prefix /opt` would otherwise hand
every other application under `/opt` to the service account without a word.
`--repo` accepts `https://` and `file:///` only, because some git URLs are
commands.

### Which version you get

With no `--ref`, the installer targets the **newest `vN.N.N` tag** in the
repository, not the tip of `main`. A fresh install has nothing to roll back
to, so it should not be the thing that discovers a bad commit; upgrades follow
the same rule, which is what keeps "run the same command again" meaningful.

`--ref main` installs the development branch if you want it, and
`--ref v1.2.3` pins an exact release.

### How uv is installed

`install.sh` does not run `curl https://astral.sh/uv/install.sh | sh`. That
pipes whatever is served at that moment straight into a root shell: nothing is
pinned, nothing is verified, and there is no artifact left to audit
afterwards.

Instead it pins a uv version, downloads that exact release artifact from
GitHub, and checks it against a SHA-256 **recorded in the script itself** —
not one fetched from beside the tarball, which would only prove that the two
came from the same place. If they disagree the download is discarded and the
script stops without having changed anything. If uv is already on the host,
none of this happens.

## Step 3: read the health check

The installer ends with `/health` and prints the answer. You can ask again at
any time:

```sh
curl localhost:9800/health
```

```json
{"status": "ok", "same_filesystem": true, "worker_alive": true,
 "active_jobs": 0, "mappings": 0, "mappings_ever_loaded": false,
 "mappings_degraded": false}
```

| Field | What it means |
| --- | --- |
| `same_filesystem` | **`false` means stop.** Your two download directories are on different filesystems and publishing is no longer atomic. Fix the mount layout (Step 1) before continuing. The installer shouts about this for a reason. |
| `worker_alive` | The download worker task is running. `false` means it died; check the logs. |
| `mappings` | How many series the indexer is currently offering. |
| `mappings_ever_loaded` | `false` with `degraded` false is the fresh-install state: no mappings file yet. |
| `mappings_degraded` | `true` means `mappings.yaml` failed to load and the service is serving the last table that did. The feed keeps working; the file needs fixing. |

`status` is `"ok"` only when the filesystem check passes, the worker is alive,
and the mapping table is not degraded.

A freshly installed service comes up **degraded**, and that is expected:
`config.yaml` still holds the example's `/downloads/incomplete` and
`/downloads/completed`, which do not exist yet on your host. Step 4 is what
fixes it.

The installer knows the difference and says so. On the run that seeds
`config.yaml` it reports `same_filesystem: false` as expected and tells you
what to do; **any other time** — an upgrade, or an install onto configuration
you already had — it raises the full warning, because then it means your
directories really are on two filesystems. That distinction is the whole
point: a warning that fires on every first install is a warning nobody reads
by their third one, and this is the one warning that stands between you and a
permanently corrupt library entry.

## Step 4: configure it

Open `http://<host>:9800/config`, follow **Settings** in the nav bar, and
fill in the four keys that are required,
or edit `/etc/svtplay-arr/config.yaml` directly:

```yaml
sonarr_url: "http://sonarr.example.internal:8989"
sonarr_api_key: "your-sonarr-api-key"
incomplete_dir: "/downloads/incomplete"
completed_dir: "/downloads/completed"
```

Everything else has a default and may be omitted. The seeded file is a
complete, commented starting point covering every key the service
understands; [docs/configuration.md](configuration.md) documents each one and
its consequences.

**Keep the API key in this file, not in the unit file.** The `SONARR_API_KEY`
environment variable overrides the file if set, which means a key saved
through the configuration page would be written and then silently ignored. The
installed unit sets it to empty deliberately. See
[docs/configuration.md § SONARR_API_KEY](configuration.md#the-sonarr_api_key-environment-override).

Settings need a restart to take effect:

```sh
systemctl restart svtplay-arr
```

The configuration page shows a banner naming exactly which settings are
pending. Mappings are different — they are re-read while the service runs.

Then re-check `/health`. `same_filesystem` should now be `true`.

## Step 5: add a mapping

Sonarr identifies a series by TVDB id; SVT has its own id and slug and knows
nothing about TVDB. A mapping row is the hand-confirmed bridge, one per show.
See [the README](../README.md#mappings-the-part-you-would-not-guess) for why
this cannot be automatic.

**Through the configuration page** — open `http://<host>:9800/config`, follow
**Mappings** in the nav bar, choose *Add a series*, type a show title. The page searches SVT and lists Sonarr's
series beside the results; pick one of each and confirm. The `series_title` is
copied verbatim from Sonarr's record rather than typed, which matters because
it becomes the permanent filename.

**Or map the whole library at once** with *Find mappings* on the same page. It
searches SVT for every Sonarr series that is not mapped yet — under its own
title and Sonarr's alternate titles — and then decides on the **episodes**: it
reads each likely SVT programme's episode list and compares it against
Sonarr's, and saves only the ones where enough episodes actually correspond
(same air date within your tolerance, same episode number), where no other
candidate corresponds at all, and where no other series already claims that
programme. Everything less certain is listed for you to accept with one click,
showing how many episodes matched. Rows it writes are marked `source: auto` —
check them, since `series_title` is still the permanent filename.

**Or from the terminal**, if you would rather seed the file by hand:

```sh
SVTPLAY_ARR_CONFIG=/etc/svtplay-arr/config.yaml \
  /opt/svtplay-arr/current/.venv/bin/svtplay-arr-suggest-mappings
```

This runs the same sweep and **never writes the file**. Confident rows print to
stdout as pasteable YAML, with the slug already derived; everything needing a
decision prints to stderr.

Mappings are re-read while the service runs — adding a show takes effect on the
next search, with no restart.

**Add at least one mapping before Step 6.** Sonarr tests an indexer on save by
firing a search with no parameters, and rejects the indexer outright if the
result is empty. With no mappings, that is exactly what it gets.

Two checks worth doing while you are here:

```sh
curl 'localhost:9800/api/?t=caps'
# must contain: supportedParams="q,tvdbid,season,ep"

curl -s 'localhost:9800/api/?t=tvsearch' | grep -c '<item>'
# the recent-releases feed; this must not be zero
```

## Step 6: connect Sonarr

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
from Step 5 — add a mapping and try again. Nothing is broken.

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
subdirectory from Step 1. This mapping is what lets Sonarr actually find the
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

## Step 7: the first grab

**Do the first grab through Sonarr's Manual Import**, not automatic
completed-download handling. This is unreviewed, self-hosted software talking
to an undocumented API. Confirm that one episode lands in the right place with
the right name before trusting the automatic path with a whole season.

In Sonarr, search for a single episode of a mapped series, grab it, and watch
Activity → Queue. You should see a real percentage climbing. When it finishes,
check that the file landed in `completed/` under the expected name and that
Sonarr imported it where you expect.

## Upgrading

The same command:

```sh
sudo bash install.sh
```

It finds the existing installation and upgrades it. What that means in
practice:

- **Your configuration is never rewritten.** `config.yaml` and
  `mappings.yaml` are written only when they do not exist, and the script
  hashes both before it starts and again at the end of every upgrade, refusing
  to finish quietly if either changed. An installer that clobbers config is
  the worst thing this script could do, so it is not left to anyone's memory.
  (It does reassert the *directory's* `0750` mode and the files' ownership on
  every run — metadata, not content. If you have deliberately loosened either,
  it will be tightened again.)
- **The new release is built somewhere else.** It goes into a new
  `releases/<commit>/` directory with its own `.venv`; the running release is
  not modified. If dependencies do not resolve, the upgrade is abandoned
  before anything is switched over and the old version is still running.
- **A failure rolls back by itself.** If the service does not answer `/health`
  after the restart, or comes back degraded when it was healthy before, the
  script flips `current` back to the previous release, restores the previous
  unit if it changed one, restarts, confirms the old version answered, and
  exits non-zero telling you what happened. A failed upgrade leaves you
  running, not down.
- It reports the version before and after, and keeps the last three releases
  so a manual rollback is also just a symlink flip.
- It will **not** delete the release the service is currently running from,
  even if that release looks incomplete to it. It stops and says so instead.

An upgrade of a service that was **already** degraded — a mount is down, say —
is not rolled back. Rolling back would not fix the mount and would throw away
the upgrade.

If you installed by hand, from an earlier version of this document, your
checkout is at `/opt/svtplay-arr` with a `.venv` beside the source. The script
recognises that, installs the new release into `releases/` alongside it, and
repoints the unit at `current`. It leaves your old checkout exactly where it
is — that is what a rollback would restore. Once you are happy, the leftover
top-level entries (`src/`, `.venv/`, `.git/`, and the rest) can be deleted;
nothing points at them any more.

To pin a version, or to move back:

```sh
sudo bash install.sh --ref v1.2.3
```

Re-download `install.sh` from time to time; it is the part that is not
versioned with the release you install.

## Doing it by hand

Use this if `install.sh` will not run on your platform, or if you want to see
what it does in the terms the older instructions used. This is the same
install without the release layout: the code lives directly in
`/opt/svtplay-arr` and the unit points straight at its `.venv`, which is what
[`deploy/svtplay-arr.service`](../deploy/svtplay-arr.service) ships with.

```sh
# 1. OS prerequisites. A minimal Debian container has none of these, and
#    ffmpeg in particular fails at the END of a download rather than at
#    startup, which is confusing.
apt install git ffmpeg curl ca-certificates

# 2. uv, which will supply Python. Verify the download rather than piping it
#    into a shell; install.sh shows how, or use your distribution's package.
#    A system Python 3.12+ plus python3-venv also works if you prefer.

# 3. The service account. On a fresh container the media group does not
#    exist yet -- creating it is the normal path.
groupadd --system media
useradd --system --no-create-home --shell /usr/sbin/nologin --gid media svtplay

# 4. The code.
git clone https://github.com/jonasthim/svtplay-arr /opt/svtplay-arr
cd /opt/svtplay-arr
uv sync                 # or: python3 -m venv .venv && .venv/bin/pip install -e .
chown -R svtplay:media /opt/svtplay-arr

# 5. Configuration. config.yaml holds the Sonarr API key, which is why the
#    directory is not world- or group-readable.
mkdir -p /etc/svtplay-arr
cp deploy/config.example.yaml /etc/svtplay-arr/config.yaml
cp deploy/mappings.example.yaml /etc/svtplay-arr/mappings.yaml
chown -R svtplay:media /etc/svtplay-arr
chmod 750 /etc/svtplay-arr
chmod 640 /etc/svtplay-arr/*.yaml

# 6. The unit.
cp deploy/svtplay-arr.service /etc/systemd/system/svtplay-arr.service
systemctl daemon-reload
systemctl enable --now svtplay-arr

# 7. The health check, before touching Sonarr.
curl localhost:9800/health
```

None of the ownership steps are optional. Skip them and the service fails to
start with a `PermissionError` the first time it opens its job database or
reads its config, because every path involved is root-owned by default.

Two properties of the unit are load-bearing and must survive any edit you make
to it:

- `UMask=0002`, so every file it writes lands as `664` and every directory as
  `775` — matching what the rest of a media stack expects, without any
  `chown`. NFS exports commonly squash identities, which makes a `chown`
  inside a container cosmetic; the umask is what actually works.
- `StateDirectory=svtplay-arr`, which makes systemd create
  `/var/lib/svtplay-arr` owned `svtplay:media` before the process starts. That
  is where `db_path` defaults to, so no manual `mkdir` is needed unless you
  move it.

Upgrading a hand-built install is `git pull && uv sync && systemctl restart
svtplay-arr`, with no rollback if it goes wrong. That asymmetry is the main
argument for the script.

## If something goes wrong

| Symptom | Where to look |
| --- | --- |
| `install.sh` refuses to run | It needs root, and says what for. `bash install.sh --dry-run` shows what it would do without root and without changing anything. |
| `install.sh` says a package is missing | You are not on an apt host. Install exactly what it named and run it again; nothing has been changed. |
| An upgrade rolled itself back | The new version did not come up. You are still running the old one. `journalctl -u svtplay-arr` has the reason. |
| Sonarr rejects the indexer on save | Empty feed. Add a mapping (Step 5), then re-check `curl -s 'localhost:9800/api/?t=tvsearch' \| grep -c '<item>'`. |
| Searches always come back empty for one episode | The resolver refused. `journalctl -u svtplay-arr` logs the reason each time — no mapping, no air date, wrong ordinal, or ambiguity. |
| `/health` says `same_filesystem: false` | Step 1. Do not grab anything until this is fixed. |
| `/health` says `mappings_degraded: true` | `mappings.yaml` failed to load. The service is serving the last good table. Check the logs for the parse error; a `series:` key with no rows under it is the usual cause — write `series: []`. |
| Grabs fail at the `.nzb` fetch | Sonarr is reaching the service on a hostname it cannot fetch back from. See Step 6. |
| A download stalls at 0% forever | The `.nzb`'s declared size did not survive. Check the logs around `addfile`. |
| Downloads fail near the end | Missing `ffmpeg`. |
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
