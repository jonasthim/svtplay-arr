# svtplay-arr Configuration UI — Design

**Date:** 2026-08-25
**Status:** Implemented, with several decisions reversed during and after
implementation — see "Reversed after implementation".
**Supersedes:** the "Mapping confirmation UX" open question in
`2026-08-24-svtplay-arr-design.md`, which said a UI would be added "only if
editing proves annoying". It did.

> **What this is.** The design record for svtplay-arr's configuration page:
> what it is for, what it deliberately refuses to do, and — in the last
> section — which of its own decisions were reversed once the thing existed,
> with the reasoning for each reversal. It is kept because a design document
> that records its own reversals is more useful than one pretending it got
> everything right first time.
>
> It is **not** a user guide. For running the page, see `deploy/README.md`.
>
> Hostnames and addresses are placeholders.

## Purpose

A configuration page for svtplay-arr, served by the service itself, covering
both series mappings and settings.

Before it, the only way to add a show was an SSH session to the service's
host, a hand-edited YAML file, and a `systemctl restart`. The mapping row
must carry an SVT id, a
slug, a TVDB id, and a `series_title` transcribed by hand — Swedish, with
diacritics — where a typo becomes a permanent wrong filename in the media
library, because Sonarr runs with `renameEpisodes=False`.

The page removes that transcription entirely: `series_title` is copied
verbatim from Sonarr's own series record, never typed.

## Constraints inherited from the service

These are not up for renegotiation in this design.

- **Sonarr runs with `renameEpisodes=False`.** `series_title` becomes the
  permanent filename. A wrong mapping is not a retry.
- **The resolver returns nothing on any doubt.** Nothing in this UI may
  loosen that, and nothing here participates in matching.
- **An empty feed makes Sonarr reject the indexer.** Any failure that could
  empty the mappings table must fail toward the last known-good state.
- **Every route is `async def`.** `JobStore` holds one `sqlite3.Connection`
  behind a blocking `threading.Lock`; FastAPI runs non-async routes in a
  threadpool, which was empirically shown to corrupt reads before the lock
  existed and would stall the event loop after it.

  > Superseded, and left as written because this is a record of what was
  > decided at the time. The lock is gone — the store now gives each thread
  > its own connection, which under WAL makes the corruption structurally
  > impossible rather than merely serialised away. The rule itself stands
  > for a different reason: these routes do network I/O to Sonarr and SVT on
  > the loop the download worker shares. See "The SABnzbd surface" in
  > `../how-it-works.md`.
- **Never emit an HTTP 500** where a degraded response will do.

## Decisions

| Decision | Rationale |
| --- | --- |
| Scope: mappings **and** settings | User's explicit choice over a mappings-only page |
| Auth: none in the application; network isolation only | Matches the rest of the stack and the service's existing posture. **Superseded at deployment** — see "Reversed after implementation" below |
| Server-rendered HTML, no JavaScript | The deploy path is `git pull` + `uv sync` + restart, with no node anywhere. A page used for two minutes a month should not add a build step. **Partly superseded 2026-08-25** — server-rendered HTML still carries every function; inline JavaScript is now permitted as progressive enhancement only, see "Reversed after implementation" below |
| Mappings hot-reload; settings need a restart | See "Reload model" — narrower than first sketched, deliberately |
| Jinja2 is the only new dependency | Already the conventional FastAPI templating choice |

## Architecture

New module `src/svtplay_arr/api/config_ui.py` plus templates under
`src/svtplay_arr/templates/`. Mounted at `/config`.

**It contains no matching logic and no SVT knowledge.** It calls the existing
`SvtClient.search_series` and `SonarrClient.all_series`, exactly as
`suggest_mappings` already does. That seam is what guarantees a UI change can
never alter what gets grabbed.

### Routes

All `async def`.

```
GET  /config                          settings form + mappings table
POST /config/settings                 save settings
GET  /config/mappings/new             "which show?" form
POST /config/mappings/search          SVT hits beside Sonarr series
POST /config/mappings                 create one mapping
POST /config/mappings/{tvdb_id}/delete  remove one
```

### The mapping flow

Two steps, plain form posts.

1. Type a show title.
2. The results page lists SVT matches (each giving `svt_series_id` and slug)
   beside Sonarr's series (each giving `tvdb_id` and `series_title`). Pick one
   of each and confirm.

`series_title` and `tvdb_id` are carried through as values from Sonarr's
record. The user never types either. The slug is taken from the SVT hit's URL.

A mapping whose `tvdb_id` already exists is rejected with an error naming the
existing row — the loader already rejects duplicates loudly, and the UI must
not be the thing that writes a file the loader will refuse.

## The write path

### Validate before writing

A save is refused if the result would not start the service:

- any required key missing
- `ensure_download_dirs_are_disjoint()` fails
- `dirs_share_filesystem()` is false — note this also returns false when
  either directory does not exist yet, so a path change is refused unless the
  target already exists. That is deliberate: the alternative is writing a
  config that starts a service which cannot publish.

These call the service's own functions, not a parallel copy. On refusal the
form redisplays with the error and **the file on disk is untouched**.

### Write atomically

Serialise to a temp file in the same directory, `fsync`, `chmod 0640`, then
`os.rename` over the target. Same reasoning as the worker's publish: a crash
or a full disk mid-write must not leave a truncated `config.yaml` that stops
the service booting.

The previous contents are kept as `<file>.bak`, so a bad save is one `mv` from
recovery without the UI.

### The secret is never rendered

**Superseded 2026-08-25 — see "Reversed after implementation" below. The key
is editable and rendered.**

`sonarr_api_key` is read from the existing file, preserved, and written back
unchanged. It is never sent to the browser, never redacted-and-displayed, and
never editable through the page. A secret that does not reach the browser
cannot leak from it. The `SONARR_API_KEY` environment override continues to
take precedence.

### Concurrent edits are refused, not merged

Each form carries the mtime of the file it writes — `config.yaml` for the
settings form, `mappings.yaml` for the mapping forms — as a hidden field. If
that file changed since the page was rendered, the save is refused with an
explanatory error. Two
tabs, or a hand edit over SSH while the page is open, would otherwise
silently discard one set of changes — and silent failures are this project's
established failure mode.

### Unrecognised keys are preserved

A settings save rewrites `config.yaml` from the parsed mapping, so any key
`Settings.load` does not recognise would otherwise be silently dropped. The
writer round-trips unknown top-level keys unchanged rather than discarding
them: a future version of the service may read a key this one does not, and
losing it on an unrelated save is the kind of quiet damage this project has
spent its whole build avoiding.

### Comments are lost, and the page says so

Rewriting YAML from parsed values destroys hand-written comments. This is a
real cost and is accepted, mitigated two ways: every write emits a generated
header (`# managed by svtplay-arr; last written <timestamp>`) and a one-line
comment per key, both from the same table that drives the form's help text.

### Dangerous fields state their consequence inline

Not generic help text. Specifically:

- `air_date_tolerance_days` — widening this makes episodes that share an air
  date ambiguous with their neighbours, and ambiguity makes the resolver
  return nothing at all. S15E01 and S15E02 share 2026-08-23 today.
- `incomplete_dir` / `completed_dir` — these are editable, per the decision to
  cover all settings, but changing them under a running worker orphans
  in-flight downloads, and they must remain on one filesystem or atomic
  publish silently degrades to copy-then-delete. Both are validated before
  write, and the form requires an explicit confirmation checkbox for a path
  change specifically.

## Reload model

**Mappings reload live. All settings require a restart.**

This is narrower than the alternative of hot-reloading the numeric settings
too, and the narrowing is deliberate. Making `rss_window_days` and
`air_date_tolerance_days` live means threading a mutable settings holder
through the resolver's constructor and the Newznab router, and updating every
test that builds them — real churn through the safety-critical module, to
make two set-once values changeable without a restart. The resolver is the
last place to accept incidental churn.

Mappings are the opposite case: they change whenever a new show is watched,
they are the reason this page exists, and they are already reached through a
two-method interface (`for_tvdb`, `all`).

`ReloadingMappingTable` wraps that same interface and re-reads when the
file's mtime changes.

**Its failure mode is load-bearing:** if the file becomes invalid, it keeps
serving the last known-good table and logs at warning. It must never return
empty because of a parse error — an empty feed is what makes Sonarr reject
the indexer, a defect this project already shipped once.

Settings changes show a persistent "restart required to apply" banner naming
the pending fields, so the page never implies a change took effect when it
did not.

## Error handling

- Validation failure → form redisplayed with the error; file untouched.
- SVT or Sonarr unreachable during a search → the search page shows the error
  and offers a manual-entry fallback. A search outage must not block adding a
  mapping.
- Any unexpected exception in a config route → a rendered error page, never an
  unhandled 500. The Newznab and SAB routes are untouched by this module and
  keep their existing behaviour.
- The config UI cannot affect the worker, the download pipeline, or matching.

## Testing

Derived from real artifacts, never from this document's description of them —
the failure mode that produced four Criticals during the service's build.

| Case | Assertion |
| --- | --- |
| Save fails validation | File byte-identical afterwards |
| Successful save | Atomic; no truncated intermediate state observable |
| Permissions | Mode stays `0640` after write |
| Backup | `.bak` holds the previous contents |
| Concurrent modification | Refused, not silently overwritten |
| Secret | ~~`sonarr_api_key` absent from every rendered response body~~ — reversed 2026-08-25; the config page renders it by design |
| Secret | `sonarr_api_key` absent from `/health` and from every Newznab and SAB response |
| Secret | ~~Rendered in the config form, masked, with a pure-CSS reveal and no JavaScript~~ — superseded 2026-08-25; the reveal is a JavaScript Show/Hide button inside the field, not pure CSS |
| Secret | The *effective* key (an `$SONARR_API_KEY` override) never reaches the page — only the file's own value |
| Secret | Never in the settings-saved notice or the pending-restart banner, which render field labels only |
| Secret | A blank or whitespace-only submitted key is refused; the file is byte-identical afterwards |
| Secret | Unchanged in the file after a settings save that does not submit it |
| Round-trip | A config written by the UI loads with the real `Settings.load` |
| `series_title` fidelity | Byte-identical to Sonarr's, diacritics included |
| Duplicate `tvdb_id` | Rejected with an error naming the existing row |
| Reloading table | Picks up a changed file without a restart |
| Reloading table | Invalid file → last-good retained, warning logged, never empty |
| Isolation | Existing resolver tests pass unchanged |

## Reversed after implementation

**Public exposure behind an SSO reverse proxy, decided 2026-08-25 at
deployment.** This document twice ruled it out. The author reversed that
immediately after the branch was merged, and published the page at a public
hostname behind an SSO-authenticating reverse proxy.

Nothing in the implementation changed. The page still has no authentication
of its own, which is precisely why the reversal matters: SSO is now the only
thing between the public internet and a page that can rewrite `config.yaml`
and delete series mappings. The published resource is whole-origin with no
bypass rules and no `/health` carve-out, and `deploy/README.md` carries the
operational detail — including the proxy behaviours worth knowing about (SSO
accepted on update but not on create; a write response that reports a state
it did not persist, so it needs confirming with a separate read; and a
per-service firewall between the proxy's network and the service's that
surfaces as an authenticated 502 reading exactly like "service down") — and
why Sonarr must never use the public hostname.

The internal-only reasoning below was sound for the posture it assumed. It
is recorded as superseded rather than deleted, because the argument still
holds for anyone considering exposing the *rest* of the API the same way.

**`sonarr_api_key` is editable and rendered, decided 2026-08-25.** This
document ruled that out twice — "The secret is never rendered" above, and
the Out of scope list below. The author reversed it after the SSO exposure
was in place, and chose the masked-with-reveal form over redaction.

The reasoning: the key is configuration like everything else on this page,
and leaving it out made it the one setting that required SSH — which is the
exact asymmetry the page exists to remove. Excluding it did not make the
page's threat model better, only less honest about it: the page can already
rewrite `config.yaml` and delete every mapping, so anyone who gets past the
gate can already break the service, and the key on top of that changes
little.

The consequence was stated before the decision, and accepted: the value now
sits in HTML delivered over the public internet, in browser cache, in
history, and in any screenshot of the page. Masking is a shoulder-surfing
measure and nothing more — the value is in the page source whether or not
the reveal is clicked, and the reveal is a checkbox plus a `:checked ~`
rule, so it held the no-JavaScript constraint as it then stood (the deploy
path is `git pull` + `uv sync` + restart, with no node anywhere). The
constraint has since been narrowed — see the JavaScript entry below — but
this control is unaffected: it is not enhancement, it is the whole
mechanism, and it stays pure CSS.

What protects the value now is the SSO in front of the site and the `0640`
config file, not the page's silence. Two things changed with it:

- **A blank key is refused.** Making the field editable made "save an empty
  key" reachable from a browser for the first time, and a blank key starts
  the service fine, reports healthy, and fails every Sonarr call.
- **The page warns when `$SONARR_API_KEY` is set.** `Settings.load` gives
  the environment precedence, so a key saved through the page would be
  written and then silently ignored — success notice, restart banner, old
  value still in use. That is this project's signature failure, so the page
  states it beside the field, and suppresses the restart banner for that one
  field while the override is active rather than promising a restart that
  cannot apply it. The recommended deployment keeps the key in `config.yaml`
  and leaves `$SONARR_API_KEY` unset, so the warning should not normally
  appear.

**JavaScript is permitted, as progressive enhancement only, decided
2026-08-25.** This document ruled it out twice — the Decisions table above
and the Out of scope list below. The branch ships one inline `<script>` in
`base.html`, and the ruling is reversed for that shape of JavaScript and no
other.

The reasoning: the constraint was never really about JavaScript. It was
about the deploy path, which is `git pull` + `uv sync` + restart with no
node anywhere, and about a page used for two minutes a month not earning a
build step. An inline script costs none of that. What the original wording
did buy — a page that keeps working when the script does not — is worth
keeping on its own terms, so it is now stated as a condition rather than
implied by a ban.

The conditions, each of them honoured by what shipped and pinned by tests:

- **Inline only.** No separate `.js` file, no bundler, no CDN, no
  `<script src>`. The script travels in the template, so there is nothing
  to build and nothing to fetch from a third party — which also keeps the
  page's one origin the only origin, behind the SSO.
- **Everything works with JavaScript disabled.** Every control the script
  touches is a plain form that already works as a full page round trip.
  The per-mapping Check is a `<form method="post">` whose response is the
  whole page re-rendered with the result inline; the script only swaps that
  for a `fetch` that patches one row. The mapping filter input is the
  reverse case: the server never renders it at all, and the script creates
  it, because a filter box that cannot filter is worse than no filter box.
- **The CSS-only reveal stays CSS-only.** The `sonarr_api_key` reveal is a
  nameless checkbox and a `:checked + +` sibling chain. It is not an
  enhancement on top of something that works — it *is* the control — so it
  must keep working with the script absent, blocked or broken. The script
  never mentions the reveal or the field, and a test asserts that it does
  not, along with there being no inline event-handler attribute and no
  `javascript:` URI anywhere on the page.
- **No JavaScript may become load-bearing.** Anything a function depends on
  belongs on the server. Adding a control that only works with the script
  running would reverse this decision again, not extend it.

One consequence is worth recording: the enhancement introduced the first
place where a server-supplied string is written into the DOM by script.
That write is `textContent`, never `innerHTML`, and the response it writes
is checked (`r.ok`, and that the body is actually a check result) before
it is painted — an SSO JSON 401 or a proxy error body parses perfectly well
otherwise. Both are pinned by tests, because a page on the public internet
that accepts an unvalidated `svt_slug` is one careless edit away from a
sink.

## Out of scope

- Authentication *inside the application* (the SSO layer sits in front of it,
  and the page itself remains unauthenticated — see above)
- ~~Editing `sonarr_api_key`~~ — reversed 2026-08-25, see above
- Any build step, any bundler, any CDN, any node in the deploy path
- ~~Any JavaScript~~ — reversed 2026-08-25 for *progressive enhancement
  only*, see above. JavaScript that any function depends on remains out of
  scope
- Restarting the service from the page
- Queue, history or diagnostics views — a separate concern if wanted later
- Radarr
