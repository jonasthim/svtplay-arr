# Contributing to svtplay-arr

Contributions are welcome. This document covers how to get set up, how to run
the tests, and — most importantly — **the testing standard this project holds
itself to**, which is stricter than most and is stated here rather than left
for you to discover in review.

Read [docs/how-it-works.md](docs/how-it-works.md) before changing anything in
`resolver.py`, `worker.py` or `svt/client.py`. The design records in
[`docs/design/`](docs/design/) explain why the strange parts are strange; most
of them are strange because of a specific trap in real data.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/jonasthim/svtplay-arr
cd svtplay-arr
uv sync --extra dev
```

That builds `.venv` with the runtime and development dependencies. Requires
Python 3.12 or newer.

A plain virtualenv works too if you would rather not use uv:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

You do **not** need `ffmpeg`, a Sonarr instance, or a Swedish IP address to run
the test suite. Every external boundary is behind an interface, and the suite
runs entirely offline against recorded fixtures.

## Running the tests

```sh
uv run --extra dev pytest -q
```

Expected output: **all tests pass, zero warnings.** Warnings are not noise
here; a warning is a failure that has not been promoted yet.

Useful variations:

```sh
uv run --extra dev pytest tests/test_resolver.py -q      # one file
uv run --extra dev pytest -q -k "refuses"                # by name
uv run --extra dev pytest -q --cov=svtplay_arr --cov-report=term-missing
```

Warnings are enforced, not just expected:

```sh
uv run --extra dev pytest -W error -q
```

This is what CI runs, so a warning fails the build. It used to be impossible:
`JobStore` had no `close()`, test fixtures left 33 sqlite connections open, and
the resulting `ResourceWarning: unclosed database` made `-W error` fail on its
own — so the gate was off and nothing else was being caught either. `JobStore`
now has `close()`, works as a context manager, and every fixture that opens one
closes it.

If a dependency ever starts emitting a `DeprecationWarning`, deal with the
warning. If it is genuinely outside our control, add one targeted
`-W ignore::...` in `.github/workflows/ci.yml` — never drop `-W error`.

### The `integration` marker

```sh
uv run --extra dev pytest -m integration -v
```

Three tests hit the **real** SVT API and download real media. They are the only
evidence in the whole project that `SvtClient` works against the live API
rather than against a snapshot of it.

They are **excluded by default** — `pyproject.toml` sets
`addopts = "-m 'not integration'"` — and **excluded in CI**, on purpose and
permanently. SVT geo-restricts its content, GitHub's hosted runners do not have
a Swedish IP, and the tests therefore cannot pass there. That is not a bug to
be fixed by relaxing the exclusion. If CI ever fails with an SVT connection
error, find what re-enabled these tests.

To run them you need a **Swedish IP address** and real bandwidth. Leave at
least 60 seconds between runs, or the GraphQL search test will fail on SVT's
own CDN caching — the reason is documented at length at the top of
`tests/test_integration_svt.py`.

## The testing standard

This project practises **strict test-driven development**, and it means
something specific here. State it up front so review is not the place you find
out.

### 1. Tests are written first

Write the test, watch it fail for the right reason, then write the code that
makes it pass. Not "write the code and add a test before opening the PR". The
order is the point: a test written after the code tends to assert what the code
does rather than what it should do.

### 2. A test that passes when the code it names is broken is a bug in the test

This is the rule that matters most. A green test is a claim, and an untrue
claim is worse than no claim, because it stops anyone looking.

Picture 85 green tests over a coordinator that never waited for replies and
could never have worked against real hardware. Every one of those tests
passed. None of them tested anything. That is the failure this rule exists
to prevent.

### 3. Mutation-check every new test

Before you consider a test done: **break the thing it names, and confirm that
test fails.** Comment out the guard, invert the condition, delete the line,
return the wrong value. If the test still passes, it is not testing what its
name says it tests, and it needs rewriting rather than adding to.

Then put the code back. This takes about thirty seconds per test and it is not
optional.

### 4. Tests are derived from real artifacts, never from descriptions of them

Writing tests against what a spec *said* a response looked like is the failure
mode that produced four Critical findings during this project's original build.

The fixtures under `tests/fixtures/svt/` are real SVT responses, captured on a
known date and committed unchanged. **When the code and a fixture disagree, the
fixture wins** and the code gets fixed — that is the whole reason for capturing
them, so that SVT API drift shows up as a fixture mismatch rather than as a
production mystery.

Do not tidy, reformat or regenerate the fixtures. See that directory's README.

### 5. Asynchrony is tested as asynchrony

A fake that answers synchronously cannot test an asynchronous protocol. The
worker is long-running and Sonarr polls `mode=queue` *during* a download, so a
fake that completes instantly would make every queue test pass while proving
nothing. `FakeDownloader` models progress over simulated time, and there is a
test asserting that intermediate states are actually observable.

If you add a fake for something that takes time in production, it must take
time in the test.

## Things that will get a change sent back

Not style preferences — these are the invariants the project is built on. Each
one has a comment in the code explaining the trap it exists for; read it before
arguing with it.

- **Loosening the resolver.** Ambiguity returns nothing. Two candidates is not
  a tie to be broken by preference. `renameEpisodes` is off, so a wrong match
  is a permanently wrong filename, not a retry.
- **Reading SVT's season number.** It is not a statement about TVDB seasons.
  Episodes are identified by ordinal plus air date only.
- **Making a release GUID unstable.** It is derived from `(svt_id, quality)`
  precisely so Sonarr's blocklist works. A changing GUID produces an infinite
  grab → fail → regrab loop.
- **Returning an HTTP 500 from a Sonarr-facing route.** A 500 can make Sonarr
  disable the indexer or the download client entirely. Degrade instead: an
  empty channel, an empty queue, an error object.
- **Letting a failure empty the mapping table.** An empty feed is what makes
  Sonarr reject the indexer. Failures fall back to the last known-good table.
- **A non-`async def` route.** `JobStore` holds one `sqlite3.Connection`
  behind a blocking lock; a threadpool thread holding it stalls the event loop
  and the download worker with it.
- **Computing the same fact in two places.** Two implementations of one truth
  drifting apart is this project's most common historical defect. `/health` and
  the configuration page's status strip call one function. The settings help
  text, the form, and the comments written into `config.yaml` come from one
  table. Keep it that way.
- **Making anything on the configuration page depend on JavaScript.** It is
  permitted as progressive enhancement only. Everything must still work with
  the script blocked or broken.
- **Adding a build step, a bundler, a CDN, or node to the deploy path.**

## Adding a setting

Add one `SettingField` to `SETTING_FIELDS` in `src/svtplay_arr/config.py`, with
a `section` and help text that states the *consequence*, not just the meaning.
That single entry drives the form on the configuration page, the comment
written into `config.yaml`, and the documentation table — so there is nothing
else to keep in sync.

If it needs a default, add it to the `Settings` dataclass and read it in
`Settings.load` via `cls.<name>`, never as a repeated literal. If a bad value
would stop the service booting, add a floor to `_INT_FLOORS`.

Then update [docs/configuration.md](docs/configuration.md), which is the one
place that is not generated.

## Commits and pull requests

- One logical change per commit, with a message that says *why*, not just what.
  The existing history is a reasonable guide.
- Run `uv run --extra dev pytest -W error -q` before pushing. All green, zero
  warnings — CI runs exactly that.
- CI runs the suite on Python 3.12 and 3.13. Both must pass.
- If you found a trap in real SVT or Sonarr behaviour, write it down — in a
  comment at the site, and in `docs/design/` if it changes a decision. That
  reasoning is the most expensive thing in this repository and the easiest to
  lose.

## Reporting a security issue

Do not open a public issue. See [SECURITY.md](SECURITY.md).
