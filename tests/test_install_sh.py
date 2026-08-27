"""Tests for `install.sh`.

The installer runs as root and writes to `/opt`, `/etc` and
`/etc/systemd/system`. None of that is testable directly, so the script was
written to be driven somewhere else: every path it touches comes from an
overridable variable, and every privileged interaction (`systemctl`,
`useradd`, `groupadd`, `getent`, `apt-get`, `chown`, `curl`, `uv`) goes
through a variable that points at a real command by default and at a stub
here.

Two things are deliberately *not* stubbed:

* `git`, because the fetch is real work and a `file://` remote makes it cheap
  to do for real. Each test builds a two-commit repository in its temporary
  tree and installs from it, so `git ls-remote`, `git clone` and
  `git checkout --detach` are exercised as written.
* the filesystem, because the release layout -- versioned directories with a
  `current` symlink -- is the thing most worth testing, and simulating it
  would test the simulation.

What is not covered here, and why, is in
`.superpowers/sdd/2026-08-25-config-ui/installer-report.md`.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
PACKAGED_UNIT = REPO_ROOT / "deploy" / "svtplay-arr.service"

HEALTHY = (
    '{"status": "ok", "same_filesystem": true, "worker_alive": true, '
    '"active_jobs": 0, "mappings": 1, "mappings_ever_loaded": true, '
    '"mappings_degraded": false}'
)
DEGRADED = (
    '{"status": "degraded", "same_filesystem": false, "worker_alive": true, '
    '"active_jobs": 0, "mappings": 1, "mappings_ever_loaded": true, '
    '"mappings_degraded": false}'
)


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------
# The harness
# --------------------------------------------------------------------------


@dataclass
class Harness:
    root: Path
    prefix: Path
    config_dir: Path
    unit_dir: Path
    remote: Path
    env: dict

    # -- running --------------------------------------------------------

    def run(self, *args: str, expect: int = 0, **overrides: str):
        env = dict(self.env)
        env.update(overrides)
        proc = subprocess.run(
            ["bash", str(INSTALL_SH), *args],
            env=env,
            cwd=str(self.root),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == expect, (
            f"expected exit {expect}, got {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
        return proc

    # -- stub bookkeeping -----------------------------------------------

    @property
    def call_log(self) -> Path:
        return self.root / "calls.log"

    def calls(self) -> str:
        return self.call_log.read_text() if self.call_log.exists() else ""

    def reset_calls(self) -> None:
        self.call_log.write_text("")

    def set_health(self, payload: str) -> None:
        (self.root / "health.json").write_text(payload)

    def plan_health(self, *payloads: str) -> None:
        """What `/health` returns after the 1st, 2nd, ... service start.

        An empty payload means the endpoint does not answer at all.
        """
        (self.root / "restart-count").write_text("0")
        for old in self.root.glob("health-*.json"):
            old.unlink()
        for index, payload in enumerate(payloads, start=1):
            (self.root / f"health-{index}.json").write_text(payload)

    def fail_uv(self, failing: bool = True) -> None:
        (self.root / "uv-mode").write_text("fail" if failing else "ok")

    # -- the source repository ------------------------------------------

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.remote),
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def commit_upstream(self, version: str = "0.2.0") -> str:
        (self.remote / "pyproject.toml").write_text(
            f'[project]\nname = "svtplay-arr"\nversion = "{version}"\n'
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"release {version}")
        return self.git("rev-parse", "HEAD").strip()

    # -- inspection ------------------------------------------------------

    @property
    def releases(self) -> list[Path]:
        directory = self.prefix / "releases"
        if not directory.is_dir():
            return []
        return sorted(p for p in directory.iterdir() if p.is_dir())

    @property
    def current(self) -> Path:
        return self.prefix / "current"

    @property
    def unit(self) -> Path:
        return self.unit_dir / "svtplay-arr.service"

    @property
    def config(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def mappings(self) -> Path:
        return self.config_dir / "mappings.yaml"


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    root = tmp_path
    prefix = root / "opt" / "svtplay-arr"
    config_dir = root / "etc" / "svtplay-arr"
    unit_dir = root / "etc" / "systemd" / "system"
    bin_dir = root / "usr" / "local" / "bin"
    stubs = root / "stubs"
    remote = root / "upstream"
    for directory in (unit_dir.parent, bin_dir, stubs, remote):
        directory.mkdir(parents=True, exist_ok=True)

    call_log = root / "calls.log"
    call_log.write_text("")
    (root / "health.json").write_text(HEALTHY)
    (root / "accounts").write_text("")

    # systemctl: records what it was asked to do, and swaps in the next
    # planned /health payload whenever the service is (re)started, which is
    # how "the new version does not come up" is expressed.
    _write_exec(
        stubs / "systemctl",
        """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >>"$CALL_LOG"
started=no
for arg in "$@"; do
    case $arg in
    restart | start | --now) started=yes ;;
    esac
done
if [ "$started" = yes ]; then
    n=0
    [ -f "$HARNESS_ROOT/restart-count" ] && n=$(cat "$HARNESS_ROOT/restart-count")
    n=$((n + 1))
    printf '%s' "$n" >"$HARNESS_ROOT/restart-count"
    if [ -f "$HARNESS_ROOT/health-$n.json" ]; then
        cp "$HARNESS_ROOT/health-$n.json" "$HARNESS_ROOT/health.json"
    fi
fi
exit 0
""",
    )

    # curl: serves health.json, or fails the way curl fails when nothing is
    # listening.
    _write_exec(
        stubs / "curl",
        """#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >>"$CALL_LOG"
if [ -s "$HARNESS_ROOT/health.json" ]; then
    cat "$HARNESS_ROOT/health.json"
    exit 0
fi
exit 7
""",
    )

    _write_exec(
        stubs / "getent",
        """#!/usr/bin/env bash
printf 'getent %s\\n' "$*" >>"$CALL_LOG"
if grep -qx "$1:$2" "$HARNESS_ROOT/accounts" 2>/dev/null; then
    exit 0
fi
exit 2
""",
    )
    _write_exec(
        stubs / "groupadd",
        """#!/usr/bin/env bash
printf 'groupadd %s\\n' "$*" >>"$CALL_LOG"
printf 'group:%s\\n' "${!#}" >>"$HARNESS_ROOT/accounts"
""",
    )
    _write_exec(
        stubs / "useradd",
        """#!/usr/bin/env bash
printf 'useradd %s\\n' "$*" >>"$CALL_LOG"
printf 'passwd:%s\\n' "${!#}" >>"$HARNESS_ROOT/accounts"
""",
    )
    _write_exec(
        stubs / "chown",
        """#!/usr/bin/env bash
printf 'chown %s\\n' "$*" >>"$CALL_LOG"
""",
    )
    _write_exec(stubs / "ffmpeg", "#!/usr/bin/env bash\nexit 0\n")

    # uv: builds the venv the unit's ExecStart points at, or refuses to
    # resolve, which is the "dependencies unresolvable" failure.
    _write_exec(
        stubs / "uv",
        """#!/usr/bin/env bash
printf 'uv %s\\n' "$*" >>"$CALL_LOG"
mode=ok
[ -f "$HARNESS_ROOT/uv-mode" ] && mode=$(cat "$HARNESS_ROOT/uv-mode")
if [ "$mode" = fail ]; then
    echo "error: no solution found when resolving dependencies" >&2
    exit 1
fi
project=""
while [ $# -gt 0 ]; do
    if [ "$1" = --project ]; then
        project=$2
    fi
    shift
done
if [ -z "$project" ]; then
    echo "stub uv: no --project given" >&2
    exit 2
fi
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\\nexit 0\\n' >"$project/.venv/bin/uvicorn"
chmod 0755 "$project/.venv/bin/uvicorn"
""",
    )

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SVTPLAY_ARR_", "UV_", "GIT_"))
    }
    env.update(
        {
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "HARNESS_ROOT": str(root),
            "CALL_LOG": str(call_log),
            "SVTPLAY_ARR_PREFIX": str(prefix),
            "SVTPLAY_ARR_CONFIG_DIR": str(config_dir),
            "SVTPLAY_ARR_UNIT_DIR": str(unit_dir),
            "SVTPLAY_ARR_BIN_DIR": str(bin_dir),
            "SVTPLAY_ARR_REPO": f"file://{remote}",
            "SVTPLAY_ARR_REF": "main",
            "SVTPLAY_ARR_EUID": "0",
            "SVTPLAY_ARR_HEALTH_URL": "http://127.0.0.1:9800/health",
            "SVTPLAY_ARR_HEALTH_TIMEOUT": "1",
            "SVTPLAY_ARR_HEALTH_INTERVAL": "1",
            "SVTPLAY_ARR_SYSTEMCTL": str(stubs / "systemctl"),
            "SVTPLAY_ARR_USERADD": str(stubs / "useradd"),
            "SVTPLAY_ARR_GROUPADD": str(stubs / "groupadd"),
            "SVTPLAY_ARR_GETENT": str(stubs / "getent"),
            "SVTPLAY_ARR_APT_GET": str(root / "no-such-apt-get"),
            "SVTPLAY_ARR_CHOWN": str(stubs / "chown"),
            "SVTPLAY_ARR_CURL": str(stubs / "curl"),
            "SVTPLAY_ARR_FFMPEG": str(stubs / "ffmpeg"),
            "SVTPLAY_ARR_UV": str(stubs / "uv"),
            "SVTPLAY_ARR_GIT": "git",
        }
    )

    harness = Harness(
        root=root,
        prefix=prefix,
        config_dir=config_dir,
        unit_dir=unit_dir,
        remote=remote,
        env=env,
    )

    # A real, tiny upstream. The example files are the project's own, so what
    # the installer seeds is what the repository ships.
    (remote / "deploy").mkdir()
    for name in ("config.example.yaml", "mappings.example.yaml"):
        shutil.copy(REPO_ROOT / "deploy" / name, remote / "deploy" / name)
    (remote / "pyproject.toml").write_text(
        '[project]\nname = "svtplay-arr"\nversion = "0.1.0"\n'
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(remote),
        env=env,
        check=True,
        capture_output=True,
    )
    harness.git("add", "-A")
    harness.git("commit", "-q", "-m", "initial")
    return harness


# --------------------------------------------------------------------------
# Fresh install
# --------------------------------------------------------------------------


def test_fresh_install_produces_a_running_release(harness: Harness):
    proc = harness.run()

    assert len(harness.releases) == 1
    release = harness.releases[0]
    assert (release / ".svtplay-arr-release-ok").is_file()
    assert (release / ".venv" / "bin" / "uvicorn").is_file()

    assert harness.current.is_symlink()
    assert harness.current.resolve() == release.resolve()

    assert harness.unit.is_file()
    unit = harness.unit.read_text()
    assert f"ExecStart={harness.current}/.venv/bin/uvicorn" in unit

    assert "daemon-reload" in harness.calls()
    assert "enable --now svtplay-arr.service" in harness.calls()
    assert "status: ok" in proc.stdout
    assert "Next steps" in proc.stdout
    assert "Rename Episodes OFF" in proc.stdout
    assert "/config" in proc.stdout


def test_fresh_install_seeds_config_with_the_right_modes(harness: Harness):
    harness.run()

    assert harness.config.read_text() == (
        REPO_ROOT / "deploy" / "config.example.yaml"
    ).read_text()
    assert harness.mappings.read_text() == (
        REPO_ROOT / "deploy" / "mappings.example.yaml"
    ).read_text()
    assert _mode(harness.config) == 0o640
    assert _mode(harness.mappings) == 0o640
    assert _mode(harness.config_dir) == 0o750
    assert f"chown -R svtplay:media {harness.config_dir}" in harness.calls()


def test_fresh_install_creates_the_media_group_and_service_user(harness: Harness):
    harness.run()
    calls = harness.calls()
    assert "groupadd --system media" in calls
    assert "useradd --system --no-create-home" in calls
    assert "--gid media svtplay" in calls


def test_install_reports_a_degraded_filesystem_loudly(harness: Harness):
    harness.set_health(DEGRADED)
    proc = harness.run()
    assert "same_filesystem is FALSE" in proc.stderr
    assert "copy-then-delete" in proc.stderr
    assert "corrupt library entry" in proc.stderr


def test_install_fails_loudly_when_the_service_never_answers(harness: Harness):
    harness.plan_health("")  # the first start leaves nothing listening
    proc = harness.run(expect=1)
    assert "did not answer" in proc.stderr
    assert "journalctl -u svtplay-arr.service" in proc.stderr


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_a_second_run_is_a_no_op(harness: Harness):
    harness.run()
    before = {
        "releases": [p.name for p in harness.releases],
        "unit": harness.unit.read_text(),
        "config": harness.config.read_text(),
        "mappings": harness.mappings.read_text(),
    }
    harness.reset_calls()

    proc = harness.run()

    assert "Already up to date" in proc.stdout
    assert [p.name for p in harness.releases] == before["releases"]
    assert harness.unit.read_text() == before["unit"]
    assert harness.config.read_text() == before["config"]
    assert harness.mappings.read_text() == before["mappings"]

    calls = harness.calls()
    assert "systemctl" not in calls, calls
    assert "groupadd" not in calls, calls
    assert "useradd" not in calls, calls
    assert "uv " not in calls, calls


def test_the_release_is_built_where_it_will_live(harness: Harness):
    """A virtualenv is not relocatable, so nothing may be built and moved.

    uv writes the absolute path of `.venv/bin/python` into every console
    script it generates -- `ExecStart` among them -- and records an absolute
    path to `src/` for the editable install of the project itself. Building in
    a staging directory and renaming it afterwards produces a service that
    cannot start, which is what an earlier version of this script did.
    """
    harness.run()
    release = harness.releases[0]

    build = [line for line in harness.calls().splitlines() if line.startswith("uv ")]
    assert build, harness.calls()
    assert f"--project {release}" in build[0]
    assert ".staging" not in harness.calls()
    assert not list((harness.prefix / "releases").glob("*.staging"))


def test_the_interpreter_uv_installs_is_pinned(harness: Harness):
    """requires-python has no ceiling; the installer must not pick at random."""
    harness.run()
    build = next(line for line in harness.calls().splitlines() if line.startswith("uv "))
    assert re.search(r"--python 3\.\d+\b", build), build


def test_an_interrupted_release_is_discarded_and_rebuilt(harness: Harness):
    """A run killed mid-build leaves a directory with no stamp in it."""
    harness.run()
    release = harness.releases[0]
    (release / ".svtplay-arr-release-ok").unlink()
    (release / "half-written").write_text("junk")

    proc = harness.run()

    assert "discarding an incomplete" in proc.stdout
    assert (release / ".svtplay-arr-release-ok").is_file()
    assert not (release / "half-written").exists()
    assert harness.current.resolve() == release.resolve()


# --------------------------------------------------------------------------
# Upgrade
# --------------------------------------------------------------------------


def _install_then_edit_config(harness: Harness) -> str:
    harness.run()
    edited = "sonarr_url: \"http://sonarr.example.internal:8989\"\nsonarr_api_key: \"a-real-key\"\n"
    harness.config.write_text(edited)
    harness.mappings.write_text("series: []\n")
    return edited


def test_upgrade_switches_releases_and_reports_both_versions(harness: Harness):
    harness.run()
    first = harness.releases[0]
    harness.commit_upstream("0.2.0")
    harness.reset_calls()

    proc = harness.run()

    assert len(harness.releases) == 2
    second = next(p for p in harness.releases if p != first)
    assert harness.current.resolve() == second.resolve()
    assert first.is_dir(), "the previous release must stay on disk as the rollback target"

    assert "existing installation found: upgrading" in proc.stdout
    assert "before: 0.1.0" in proc.stdout
    assert "after:  0.2.0" in proc.stdout
    assert "restart svtplay-arr.service" in harness.calls()


def test_upgrade_leaves_config_untouched(harness: Harness):
    edited = _install_then_edit_config(harness)
    config_stat = harness.config.stat()
    harness.commit_upstream("0.2.0")

    proc = harness.run()

    assert harness.config.read_text() == edited
    assert harness.mappings.read_text() == "series: []\n"
    assert harness.config.stat().st_mtime == config_stat.st_mtime
    assert "byte-for-byte unchanged" in proc.stdout


def test_upgrade_whose_build_fails_leaves_the_old_version_running(harness: Harness):
    edited = _install_then_edit_config(harness)
    first = harness.releases[0]
    harness.commit_upstream("0.2.0")
    harness.fail_uv()
    harness.reset_calls()

    proc = harness.run(expect=1)

    assert harness.current.resolve() == first.resolve()
    assert harness.releases == [first]
    assert harness.config.read_text() == edited
    assert "uv could not build" in proc.stderr
    assert "is still running" in proc.stderr
    assert "restart" not in harness.calls(), harness.calls()


def test_upgrade_whose_health_check_fails_rolls_back(harness: Harness):
    edited = _install_then_edit_config(harness)
    first = harness.releases[0]
    harness.commit_upstream("0.2.0")
    # The upgraded service does not come up; the restored one does.
    harness.plan_health("", HEALTHY)

    proc = harness.run(expect=1)

    assert harness.current.resolve() == first.resolve()
    assert "Rolling back" in proc.stdout
    assert "rolled back" in proc.stderr
    assert "running: 0.1.0" in proc.stdout
    assert harness.config.read_text() == edited


def test_upgrade_that_comes_back_degraded_rolls_back(harness: Harness):
    harness.run()
    first = harness.releases[0]
    harness.commit_upstream("0.2.0")
    harness.plan_health(DEGRADED, HEALTHY)

    proc = harness.run(expect=1)

    assert harness.current.resolve() == first.resolve()
    assert "came back as 'degraded'" in proc.stderr


def test_upgrade_of_an_already_degraded_service_is_not_rolled_back(harness: Harness):
    """Rolling back would not fix a broken mount, and would lose the upgrade."""
    harness.set_health(DEGRADED)
    harness.run()
    first = harness.releases[0]
    harness.commit_upstream("0.2.0")

    proc = harness.run()

    second = next(p for p in harness.releases if p != first)
    assert harness.current.resolve() == second.resolve()
    assert "Rolling back" not in proc.stdout


def test_upgrade_migrates_a_pre_release_layout_checkout(harness: Harness):
    """The documented manual install is a plain checkout at the prefix."""
    legacy_venv = harness.prefix / ".venv" / "bin"
    legacy_venv.mkdir(parents=True)
    (legacy_venv / "uvicorn").write_text("#!/bin/sh\nexit 0\n")
    (harness.prefix / "pyproject.toml").write_text(
        '[project]\nname = "svtplay-arr"\nversion = "0.0.9"\n'
    )
    harness.unit_dir.mkdir(parents=True, exist_ok=True)
    harness.unit.write_text(
        "[Service]\n"
        f"ExecStart={harness.prefix}/.venv/bin/uvicorn --factory "
        "svtplay_arr.app:create_app_from_env --host 0.0.0.0 --port 9800\n"
    )

    proc = harness.run()

    assert "predates the releases/ layout" in proc.stdout
    assert harness.current.is_symlink()
    assert harness.current.resolve() == harness.releases[0].resolve()
    # The old checkout is left alone: it is what a rollback would restore.
    assert (legacy_venv / "uvicorn").is_file()
    backup = harness.prefix / ".svtplay-arr.service.previous"
    assert backup.is_file()
    assert f"ExecStart={harness.current}/.venv/bin/uvicorn" in harness.unit.read_text()


def test_prune_keeps_the_requested_number_of_releases(harness: Harness):
    harness.run("--keep", "1")
    first = harness.releases[0]
    harness.commit_upstream("0.2.0")

    harness.run("--keep", "1")

    assert len(harness.releases) == 1
    assert not first.exists()
    assert harness.current.resolve() == harness.releases[0].resolve()


# --------------------------------------------------------------------------
# --dry-run
# --------------------------------------------------------------------------


MUTATING_STUBS = {"systemctl", "groupadd", "useradd", "chown", "uv"}


def _mutating_calls(harness: Harness) -> list[str]:
    """Stub invocations that would have changed the host.

    getent and curl are reads; a dry run is allowed to make them.
    """
    return [
        line
        for line in harness.calls().splitlines()
        if line.split(" ", 1)[0] in MUTATING_STUBS
    ]


def test_dry_run_changes_nothing(harness: Harness):
    proc = harness.run("--dry-run")

    assert not harness.prefix.exists()
    assert not harness.config_dir.exists()
    assert not harness.unit.exists()
    assert _mutating_calls(harness) == []
    assert "would: git clone" in proc.stdout
    assert "daemon-reload" in proc.stdout
    assert "--system media" in proc.stdout
    assert "would: install -d -m 0755" in proc.stdout
    assert "nothing was changed." in proc.stdout


def test_dry_run_over_an_installation_changes_nothing(harness: Harness):
    edited = _install_then_edit_config(harness)
    harness.commit_upstream("0.2.0")
    before = harness.unit.read_text()
    releases_before = [p.name for p in harness.releases]
    harness.reset_calls()

    proc = harness.run("--dry-run")

    assert [p.name for p in harness.releases] == releases_before
    assert harness.unit.read_text() == before
    assert harness.config.read_text() == edited
    assert _mutating_calls(harness) == []
    assert "upgrading" in proc.stdout


def test_dry_run_does_not_need_root(harness: Harness):
    proc = harness.run("--dry-run", SVTPLAY_ARR_EUID="1000")
    assert "--dry-run changes nothing" in proc.stdout


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_refuses_to_run_unprivileged(harness: Harness):
    proc = harness.run(expect=1, SVTPLAY_ARR_EUID="1000")
    assert "must run as root" in proc.stderr
    assert "sudo bash install.sh" in proc.stderr
    assert "--dry-run" in proc.stderr
    assert not harness.prefix.exists()


def test_missing_ffmpeg_without_apt_says_exactly_what_is_missing(harness: Harness):
    proc = harness.run(
        expect=1, SVTPLAY_ARR_FFMPEG=str(harness.root / "no-such-ffmpeg")
    )
    assert "missing: ffmpeg" in proc.stderr
    assert "no apt" in proc.stderr
    assert "Nothing has changed." in proc.stderr
    assert not harness.prefix.exists()


def test_unresolvable_ref_changes_nothing(harness: Harness):
    proc = harness.run(expect=1, SVTPLAY_ARR_REF="no-such-branch")
    assert "could not resolve no-such-branch" in proc.stderr
    assert harness.releases == []


# --------------------------------------------------------------------------
# Structural guarantees
# --------------------------------------------------------------------------


def _logical_lines(text: str):
    """(line number, enclosing function, joined logical line)."""
    function = "<toplevel>"
    buffer: list[str] = []
    start = 1
    for lineno, raw in enumerate(text.splitlines(), start=1):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{", raw)
        if match and not buffer:
            function = match.group(1)
        if not buffer:
            start = lineno
        buffer.append(raw.rstrip("\\").strip() if raw.rstrip().endswith("\\") else raw)
        if raw.rstrip().endswith("\\"):
            continue
        yield start, function, " ".join(part.strip() for part in buffer)
        buffer = []


CONFIG_REFERENCES = ("CONFIG_FILE", "MAPPINGS_FILE", "SVTPLAY_ARR_CONFIG_DIR")

# A mutating command at the start of a command, after `run`, or a redirection
# into a variable. Substrings inside prose ("installs a unit into") do not
# match, which is the point of anchoring.
WRITE_RE = re.compile(
    r"""(?x)
    (?: ^ | [;&|] | \brun\b\s+ ) \s*
    (?: "?\$\{?SVTPLAY_ARR_CHOWN\}?"? | install | cp | mv | rm | ln | touch
      | tee | chmod | chown | dd | sed \s+ -i ) \b
    | > \s* "? \$
    """
)

# seed_config_file is the only thing that may create a config file;
# seed_config is the only thing that may create or chown the directory.
CONFIG_WRITERS = {"seed_config_file", "seed_config"}


def test_only_the_seeding_functions_write_to_the_config_directory():
    """The upgrade path cannot clobber config.yaml, by construction.

    An installer that overwrites configuration is the worst thing this script
    could do and the most common way installers do it, so it is not left to
    whoever edits this file next to remember. `seed_config_file` refuses to
    write over a file that exists, and this test refuses to let a config
    write appear anywhere else.
    """
    offenders = []
    for lineno, function, line in _logical_lines(INSTALL_SH.read_text()):
        if line.lstrip().startswith("#"):
            continue
        if not any(ref in line for ref in CONFIG_REFERENCES):
            continue
        if not WRITE_RE.search(line.strip()):
            continue
        if function not in CONFIG_WRITERS:
            offenders.append(f"install.sh:{lineno} in {function}(): {line.strip()}")
    assert not offenders, (
        "these lines write to the configuration directory from outside "
        f"{sorted(CONFIG_WRITERS)}:\n" + "\n".join(offenders)
    )


def test_seed_config_file_refuses_to_overwrite():
    """The guard above is only as good as the primitive it protects."""
    body = INSTALL_SH.read_text()
    seed = body.split("seed_config_file() {", 1)[1].split("\n}\n", 1)[0]
    assert "[[ -e $dest ]]" in seed
    assert "keeping existing" in seed


def _parse_unit(text: str) -> dict[str, list[str]]:
    joined = re.sub(r"\\\n\s*", " ", text)
    parsed: dict[str, list[str]] = {}
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        parsed.setdefault(key.strip(), []).append(value.strip())
    return parsed


def _render_default_unit() -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SVTPLAY_ARR_")}
    proc = subprocess.run(
        ["bash", "-c", f'source "{INSTALL_SH}"; render_unit'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_generated_unit_matches_the_packaged_unit():
    """The two units must differ in exactly one thing: where ExecStart points.

    install.sh carries its own copy of the unit so that the script is a
    single self-contained download. That copy is free to drift from
    deploy/svtplay-arr.service, which is the one the manual instructions
    still use -- so it is pinned here instead.
    """
    generated = _parse_unit(_render_default_unit())
    packaged = _parse_unit(PACKAGED_UNIT.read_text())

    assert set(generated) == set(packaged)
    for key in packaged:
        if key == "ExecStart":
            continue
        assert generated[key] == packaged[key], key

    (expected,) = packaged["ExecStart"]
    assert generated["ExecStart"] == [
        expected.replace("/opt/svtplay-arr/.venv", "/opt/svtplay-arr/current/.venv")
    ]


def test_generated_unit_keeps_the_load_bearing_properties():
    unit = _render_default_unit()
    assert "User=svtplay" in unit
    assert "Group=media" in unit
    assert "UMask=0002" in unit
    assert "Environment=SVTPLAY_ARR_CONFIG=/etc/svtplay-arr/config.yaml" in unit
    assert "Environment=SONARR_API_KEY=" in unit
    assert "StateDirectory=svtplay-arr" in unit
    assert "--host 0.0.0.0 --port 9800" in unit


def test_uv_download_is_pinned_and_checksummed():
    body = INSTALL_SH.read_text()
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    # The thing a careful operator refuses to run as root, and the reason
    # install_uv is as long as it is.
    assert "astral.sh" not in code
    assert re.search(r'readonly UV_VERSION="\d+\.\d+\.\d+"', body)
    # One checksum per published Linux target, each a full SHA-256.
    checksums = re.findall(r"printf '%s' '([0-9a-f]{64})'", body)
    assert len(checksums) == 4
    assert "sha256sum" in body
    assert "checksum mismatch -- refusing to install" in body


def test_script_is_strict_and_has_a_help_text():
    body = INSTALL_SH.read_text()
    assert body.startswith("#!/usr/bin/env bash\n")
    assert "\nset -Eeuo pipefail\n" in body
    proc = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"], capture_output=True, text=True, check=True
    )
    assert "--dry-run" in proc.stdout
    assert "install or upgrade svtplay-arr" in proc.stdout
