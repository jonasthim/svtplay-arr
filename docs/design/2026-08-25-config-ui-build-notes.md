# svtplay-arr Configuration UI — Build notes

**Date:** 2026-08-25

> **What this is.** A design record of how the configuration page was put
> together: which modules it added, which existing ones it touched, the
> constraints they all inherit, and the notes worth keeping from the build.
> It accompanies `2026-08-25-config-ui-design.md`, which argues the design
> and records the decisions that were later reversed.
>
> It is **not** a user guide. For running the page, see `deploy/README.md`.
> Where this and the code disagree, the code is right.

**Goal:** A server-rendered configuration page at `/config` that manages
series mappings and settings, so adding a show no longer means SSH, a
hand-edited YAML file and a restart.

**Architecture:** A `config_ui` router with Jinja2 templates, backed by two
writers that share one atomic-write helper. The router contains no matching
and no SVT knowledge — it calls the existing `SvtClient` and `SonarrClient`.
Mappings reload live via an mtime-watching wrapper; settings changes show a
restart-required banner.

**Stack:** Python 3.12+, FastAPI, Jinja2 (added here), PyYAML, pytest.

## Constraints every part of this inherits

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
- **`series_title` is copied verbatim from Sonarr's record, never typed.** It
  becomes the permanent filename — Sonarr runs with `renameEpisodes=False`,
  so a typo is not a retry.
- **Never emit an HTTP 500** where a degraded response will do.
- **A failure must never empty the mappings table.** An empty feed makes
  Sonarr reject the indexer — a defect this project shipped once already.
- **Validation runs the service's own functions** —
  `ensure_download_dirs_are_disjoint()` and `dirs_share_filesystem()` — never
  a parallel copy that can drift from what actually refuses to boot.
- **Writes are atomic**: temp file in the same directory, `fsync`,
  `chmod 0640`, `os.rename`. Previous contents kept as `<file>.bak`.
- **Unrecognised top-level keys are round-tripped unchanged**, never dropped.
  A future version may read a key this one does not, and losing it on an
  unrelated save is exactly the quiet damage this project spends its effort
  avoiding.
- **Concurrent modification is refused, not merged**, via an mtime carried in
  each form.
- **Nothing here participates in matching.** The existing resolver tests must
  pass unchanged; that is the check that the seam held.
- **The page cannot restart the service, alter the worker, or touch the
  download pipeline.**

The `sonarr_api_key` constraint originally listed here — never sent to the
browser, never rendered, never editable — was reversed during deployment.
See "Reversed after implementation" in the design record for the reasoning
and for the two protections that came with the reversal.

## What was added and what was touched

```
src/svtplay_arr/
  yamlio.py              NEW  atomic write, backup, mtime conflict detection
  config.py              MOD  SETTING_FIELDS, save_settings()
  mappings.py            MOD  add_mapping(), remove_mapping(),
                              ReloadingMappingTable
  api/config_ui.py       NEW  the routes
  templates/
    base.html            NEW  shared shell
    index.html           NEW  settings form + mappings table
    mapping_new.html     NEW  "which show?" form
    mapping_search.html  NEW  SVT hits beside Sonarr series
  app.py                 MOD  mount the router, use ReloadingMappingTable
pyproject.toml           MOD  jinja2
tests/
  test_yamlio.py         NEW
  test_config_writer.py  NEW
  test_mappings_writer.py NEW
  test_config_ui.py      NEW
deploy/README.md         MOD  document the page
```

`yamlio.py` exists so that the two writers share one implementation of the
risky part — the atomic replace, the backup, the mtime check. `config.py` and
`mappings.py` each own their own round-trip, so a schema change has exactly
one place to be applied.

`SETTING_FIELDS` drives both the rendered form and the comments written into
`config.yaml`, so the explanation a user reads on the page and the one left in
the file cannot drift apart.

## Notes worth keeping

**The backup filename is `config.yaml.bak`, not `config.bak`.**
`path.with_suffix(path.suffix + ".bak")` is what produces that, and a test
asserts the exact name — a `.bak` file the operator cannot guess is not a
recovery path.

**`ReloadingMappingTable` needed a module logger before it needed anything
else.** `mappings.py` had none. The class logs on its degrade-to-last-good
path, and a `NameError` there would convert a *handled* parse error into an
unhandled exception raised inside the resolver's lookup — turning the one
failure mode the design most wanted to survive into the worst one.

**`create_app` builds one `SonarrClient` and one `SvtClient`, hoisted to
named locals.** They were previously constructed inline inside the
`Resolver(...)` call; the config router needs them too, and two clients would
mean two connection pools for no reason.

**`Settings` gained a `config_path` field**, set by `Settings.load`, because
the page has to know which file it was loaded from in order to write it back.

**Register the router at `""` as well as `"/"`**, or `/config`
307-redirects to `/config/`.

**Tests are derived from real artifacts, never from a document's description
of them.** Writing tests against what a spec *said* a response looked like is
the failure mode that produced four Criticals during the service's original
build.

## Verifying a change to this page

1. `uv run --extra dev pytest -q` — all green, zero warnings.
2. `curl -s 'localhost:9800/api/?t=tvsearch' | grep -c '<item>'` — unchanged
   from before the change. The UI must not affect the feed.
3. Add a mapping through the page, then re-run (2) **without restarting** —
   the new series should be reachable, which is what proves the reloading
   table is actually in the resolver's path.
