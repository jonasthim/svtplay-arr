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

import hashlib
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
# `status` is degraded and every process-level field is fine: what a new
# version reports when a *dependency* is down. The version being replaced
# never asked Sonarr anything, so it reported `ok` through exactly the same
# outage -- which is why the rollback gate must not compare the two.
DEGRADED_BY_A_DEPENDENCY = (
    '{"status": "degraded", "same_filesystem": true, "worker_alive": true, '
    '"active_jobs": 0, "mappings": 1, "mappings_ever_loaded": true, '
    '"mappings_degraded": false, '
    '"svt": {"state": "unknown", "degraded": false, "alive": true}, '
    '"sonarr": {"state": "sonarr", "degraded": true, "alive": true}}'
)
# The process itself broke, and the service was already degraded beforehand.
WORKER_DEAD = (
    '{"status": "degraded", "same_filesystem": false, "worker_alive": false, '
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

    def kill_during_build(self) -> None:
        (self.root / "uv-mode").write_text("kill")

    def kill_during_restart(self) -> None:
        (self.root / "kill-on-restart").write_text("")

    def tamper_with_config_on_restart(self) -> None:
        (self.root / "tamper-config").write_text("")

    def fail_downloads(self) -> None:
        (self.root / "curl-fail").write_text("")

    @property
    def active_marker(self) -> Path:
        return self.prefix / ".active-release"

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
        """An untagged commit on main."""
        (self.remote / "pyproject.toml").write_text(
            f'[project]\nname = "svtplay-arr"\nversion = "{version}"\n'
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"release {version}")
        return self.git("rev-parse", "HEAD").strip()

    def release_upstream(self, version: str = "0.2.0") -> str:
        """A commit plus the vN.N.N tag the installer targets by default."""
        sha = self.commit_upstream(version)
        self.git("tag", f"v{version}")
        return sha

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

verb=""
for arg in "$@"; do
    case $arg in
    -*) ;;
    *)
        verb=$arg
        break
        ;;
    esac
done

# A read, not a mutation: whether the unit is running right now.
if [ "$verb" = is-active ]; then
    if [ "$(cat "$HARNESS_ROOT/service-state" 2>/dev/null)" = active ]; then
        exit 0
    fi
    exit 3
fi

started=no
for arg in "$@"; do
    case $arg in
    restart | start | --now) started=yes ;;
    esac
done
if [ "$started" = yes ]; then
    # Stands in for anything that rewrites config behind the installer's
    # back, so the post-upgrade hash check has something to catch.
    if [ -f "$HARNESS_ROOT/tamper-config" ]; then
        printf '# tampered with\\n' >>"$HARNESS_CONFIG"
    fi
    # A run killed between the symlink flip and the restart.
    if [ -f "$HARNESS_ROOT/kill-on-restart" ]; then
        rm -f "$HARNESS_ROOT/kill-on-restart"
        kill -9 "$PPID"
        exit 0
    fi
    printf 'active' >"$HARNESS_ROOT/service-state"
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

url=""
out=""
prev=""
for arg in "$@"; do
    case $arg in
    http://* | https://* | file://*) url=$arg ;;
    esac
    if [ "$prev" = "-o" ]; then
        out=$arg
    fi
    prev=$arg
done

# A release artifact download rather than a /health poll.
case $url in
*.tar.gz)
    if [ -f "$HARNESS_ROOT/curl-fail" ]; then
        exit 22
    fi
    cp "$HARNESS_ROOT/uv-artifact.tar.gz" "$out" || exit 23
    exit 0
    ;;
esac

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
# The build is where a long run is most likely to be interrupted.
if [ "$mode" = kill ]; then
    rm -f "$HARNESS_ROOT/uv-mode"
    kill -9 "$PPID"
    exit 0
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

# A real interpreter plus a real installed-distribution record, so
# project_version's primary path -- reading importlib.metadata through the
# venv it belongs to -- is exercised as written, the same as a real `uv
# sync` leaves behind, rather than skipped in favour of the git fallback
# every test used to fall through to.
python3 -m venv --without-pip --symlinks "$project/.venv" >/dev/null 2>&1
if [ -x "$project/.venv/bin/python3" ]; then
    exact=$(git -C "$project" describe --tags --exact-match 2>/dev/null || true)
    if [ -n "$exact" ]; then
        ver=${exact#v}
    else
        # No exact tag: the shape hatch-vcs's default scheme actually
        # produces for an in-between-releases build -- the next version
        # with a .devN+g<sha> suffix, per pyproject.toml's
        # [tool.hatch.version] comment -- not raw `git describe` output.
        last_tag=$(git -C "$project" describe --tags --abbrev=0 2>/dev/null || true)
        sha=$(git -C "$project" rev-parse --short HEAD 2>/dev/null || true)
        if [ -n "$last_tag" ]; then
            count=$(git -C "$project" rev-list "${last_tag}..HEAD" --count 2>/dev/null || echo 0)
            base=${last_tag#v}
            IFS=. read -r maj min pat <<<"$base"
            ver="${maj}.${min}.$((pat + 1)).dev${count}+g${sha}"
        else
            count=$(git -C "$project" rev-list HEAD --count 2>/dev/null || echo 0)
            ver="0.0.0.dev${count}+g${sha}"
        fi
    fi
    purelib=$("$project/.venv/bin/python3" -c \
        'import sysconfig; print(sysconfig.get_path("purelib"))')
    mkdir -p "$purelib/svtplay_arr-$ver.dist-info"
    printf 'Metadata-Version: 2.1\\nName: svtplay-arr\\nVersion: %s\\n' "$ver" \\
        >"$purelib/svtplay_arr-$ver.dist-info/METADATA"
fi
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
            "HARNESS_CONFIG": str(config_dir / "config.yaml"),
            "CALL_LOG": str(call_log),
            "SVTPLAY_ARR_PREFIX": str(prefix),
            "SVTPLAY_ARR_CONFIG_DIR": str(config_dir),
            "SVTPLAY_ARR_UNIT_DIR": str(unit_dir),
            "SVTPLAY_ARR_BIN_DIR": str(bin_dir),
            "SVTPLAY_ARR_REPO": f"file://{remote}",
            # SVTPLAY_ARR_REF is deliberately unset: the default path is
            # "newest vN.N.N tag", and that is what should be under test.
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
    harness.git("tag", "v0.1.0")
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

    # A fresh install has a real version to report from the moment it is
    # built -- "unknown" is for when there is genuinely nothing installed,
    # not for the thing this run just installed.
    assert "installed: 0.1.0" in proc.stdout
    assert "unknown" not in proc.stdout


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


def test_a_fresh_install_does_not_cry_wolf_about_same_filesystem(harness: Harness):
    """Every first install is degraded, so the alarm must not fire on one.

    `dirs_share_filesystem()` is False when the directories do not exist, and
    the seeded example points at /downloads/incomplete, which never exists on
    a fresh host. Firing the library-corruption warning on 100% of installs is
    how it stops being read by the third one.
    """
    harness.set_health(DEGRADED)
    proc = harness.run()

    assert "same_filesystem is FALSE" not in proc.stderr
    assert "expected at this point" in proc.stdout
    assert "wrote" in proc.stdout and "config.yaml" in proc.stdout
    # It still says what the field would mean if it persists.
    assert "corrupt library entry" in proc.stdout


def test_a_degraded_upgrade_reports_same_filesystem_loudly(harness: Harness):
    """Once config is the operator's own, false means misconfigured."""
    harness.run()
    harness.release_upstream("0.2.0")
    harness.set_health(DEGRADED)
    # Already degraded before, so this is not a rollback case.
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

    # Nothing that changes the host. `systemctl is-active` is allowed and
    # expected -- "up to date" is a claim about what is running, and the only
    # way to check is to ask.
    assert _mutating_calls(harness) == [], harness.calls()
    assert "systemctl is-active" in harness.calls()


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


def test_refuses_to_delete_the_release_the_service_is_running_from(harness: Harness):
    """The stamp is an ordinary dotfile in a tree owned by the service account.

    If it goes missing from the ACTIVE release, treating that release as
    debris deletes the tree the running process was started from. The process
    survives on open file handles, so the run would go on to report the old
    version as still running while `releases/` was empty -- and the service
    would be gone at the next restart.
    """
    harness.run()
    release = harness.releases[0]
    (release / ".svtplay-arr-release-ok").unlink()

    proc = harness.run(expect=1)

    assert release.is_dir()
    assert (release / ".venv" / "bin" / "uvicorn").is_file()
    assert "is the release the service is running from" in proc.stderr
    assert "Removing it would take the running service with it" in proc.stderr
    assert harness.current.resolve() == release.resolve()


def test_an_incomplete_inactive_release_is_discarded_and_rebuilt(harness: Harness):
    harness.run()
    first = harness.releases[0]
    sha = harness.release_upstream("0.2.0")
    stale = harness.prefix / "releases" / sha[:12]
    stale.mkdir(parents=True)
    (stale / "half-written").write_text("junk")

    proc = harness.run()

    assert "discarding an incomplete" in proc.stdout
    assert (stale / ".svtplay-arr-release-ok").is_file()
    assert not (stale / "half-written").exists()
    assert harness.current.resolve() == stale.resolve()
    assert first.is_dir()


def test_a_build_killed_mid_way_is_rebuilt_by_the_next_run(harness: Harness):
    """A real SIGKILL during `uv sync`, not a simulated one.

    This is what the stamp is for, and why it is written after the build
    rather than before: the killed run leaves a directory that looks finished
    by name alone.
    """
    harness.kill_during_build()
    killed = harness.run(expect=-9)
    assert killed.returncode == -9

    assert len(harness.releases) == 1
    partial = harness.releases[0]
    assert not (partial / ".svtplay-arr-release-ok").exists()
    assert not (partial / ".venv").exists()
    assert not harness.current.exists()

    proc = harness.run()

    assert "discarding an incomplete" in proc.stdout
    assert (partial / ".svtplay-arr-release-ok").is_file()
    assert (partial / ".venv" / "bin" / "uvicorn").is_file()
    assert harness.current.resolve() == partial.resolve()


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
    harness.release_upstream("0.2.0")
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
    harness.release_upstream("0.2.0")

    proc = harness.run()

    assert harness.config.read_text() == edited
    assert harness.mappings.read_text() == "series: []\n"
    assert harness.config.stat().st_mtime == config_stat.st_mtime
    assert "byte-for-byte unchanged" in proc.stdout


def test_upgrade_whose_build_fails_leaves_the_old_version_running(harness: Harness):
    edited = _install_then_edit_config(harness)
    first = harness.releases[0]
    harness.release_upstream("0.2.0")
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
    harness.release_upstream("0.2.0")
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
    harness.release_upstream("0.2.0")
    harness.plan_health(DEGRADED, HEALTHY)

    proc = harness.run(expect=1)

    assert harness.current.resolve() == first.resolve()
    assert "came back as 'degraded'" in proc.stderr


def test_upgrade_of_an_already_degraded_service_is_not_rolled_back(harness: Harness):
    """Rolling back would not fix a broken mount, and would lose the upgrade."""
    harness.set_health(DEGRADED)
    harness.run()
    first = harness.releases[0]
    harness.release_upstream("0.2.0")

    proc = harness.run()

    second = next(p for p in harness.releases if p != first)
    assert harness.current.resolve() == second.resolve()
    assert "Rolling back" not in proc.stdout


def test_a_dependency_being_down_does_not_roll_back_a_good_upgrade(
    harness: Harness,
):
    """The coupling this gate must not have.

    `/health`'s top-level `status` folds in the background checks on SVT and
    Sonarr. The version being replaced may never have looked at either --
    the Sonarr check did not exist before 2026-08-28 -- so it reported `ok`
    through an outage the new one correctly calls `degraded`. Upgrade
    svtplay-arr and Sonarr in the same window and a status comparison throws
    away a working upgrade to "fix" a dependency that is merely restarting.

    Nothing prevented that but a few seconds of startup delay in a different
    file, which is not a guard.
    """
    harness.run()
    first = harness.releases[0]
    harness.release_upstream("0.2.0")
    harness.plan_health(DEGRADED_BY_A_DEPENDENCY)

    proc = harness.run()

    second = next(p for p in harness.releases if p != first)
    assert harness.current.resolve() == second.resolve()
    assert "Rolling back" not in proc.stdout
    # Still reported, just not treated as this upgrade's fault.
    assert "degraded" in proc.stdout


def test_a_worker_that_dies_across_an_upgrade_rolls_back_even_when_degraded(
    harness: Harness,
):
    """The other half of dropping the `was ok before` precondition.

    The old gate only fired when the service had been `ok` beforehand, so an
    upgrade that killed the download worker on a host whose mount was already
    split was waved through. Comparing the process-level fields catches it.
    """
    harness.set_health(DEGRADED)
    harness.run()
    first = harness.releases[0]
    harness.release_upstream("0.2.0")
    harness.plan_health(WORKER_DEAD, DEGRADED)

    proc = harness.run(expect=1)

    assert harness.current.resolve() == first.resolve()
    assert "worker_alive was true before the upgrade" in proc.stderr


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
    harness.release_upstream("0.2.0")

    harness.run("--keep", "1")

    assert len(harness.releases) == 1
    assert not first.exists()
    assert harness.current.resolve() == harness.releases[0].resolve()


# --------------------------------------------------------------------------
# --dry-run
# --------------------------------------------------------------------------


MUTATING_STUBS = {"systemctl", "groupadd", "useradd", "chown", "uv"}
# systemctl is not one verb. Asking whether a unit is running changes nothing,
# and the installer has to ask.
SYSTEMCTL_READS = {"is-active", "is-enabled", "show", "status", "cat"}


def _mutating_calls(harness: Harness) -> list[str]:
    """Stub invocations that would have changed the host.

    getent and curl are reads; a dry run is allowed to make them, and so is a
    run that finds there is nothing to do.
    """
    out = []
    for line in harness.calls().splitlines():
        parts = line.split()
        if not parts or parts[0] not in MUTATING_STUBS:
            continue
        if parts[0] == "systemctl":
            verb = next((a for a in parts[1:] if not a.startswith("-")), "")
            if verb in SYSTEMCTL_READS:
                continue
        out.append(line)
    return out


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
    harness.release_upstream("0.2.0")
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
# Interruption between the flip and the restart
# --------------------------------------------------------------------------


def test_an_interrupted_activation_is_finished_by_the_next_run(harness: Harness):
    """`current` naming a release is not the same as running it.

    Killed after the symlink flip and before the restart, the host is on the
    new code by symlink and the old code by process. A re-run that says
    "nothing to do" leaves it that way for good, because every later run
    agrees with it.
    """
    harness.run()
    first = harness.releases[0]
    harness.release_upstream("0.2.0")
    harness.kill_during_restart()

    killed = harness.run(expect=-9)
    assert killed.returncode == -9

    second = next(p for p in harness.releases if p != first)
    assert harness.current.resolve() == second.resolve()
    assert harness.active_marker.read_text().strip() == first.name
    harness.reset_calls()

    proc = harness.run()

    assert "Already up to date" not in proc.stdout
    assert "restart svtplay-arr.service" in harness.calls()
    assert harness.active_marker.read_text().strip() == second.name


def test_a_stopped_service_is_started_by_the_next_run(harness: Harness):
    harness.run()
    (harness.root / "service-state").write_text("inactive")
    harness.reset_calls()

    proc = harness.run()

    assert "Already up to date" not in proc.stdout
    assert _mutating_calls(harness) != []


# --------------------------------------------------------------------------
# Installing uv
# --------------------------------------------------------------------------


def _uv_target() -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SVTPLAY_ARR_")}
    proc = subprocess.run(
        ["bash", "-c", f'source "{INSTALL_SH}"; uv_target'],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _path_without_uv() -> str:
    """A PATH on which `command -v uv` fails, so install_uv actually runs."""
    kept = [
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and not os.access(os.path.join(d, "uv"), os.X_OK)
    ]
    return os.pathsep.join(kept)


def _publish_fake_uv(harness: Harness, target: str) -> str:
    """A release artifact shaped like uv's, holding the harness's uv stub."""
    stage = harness.root / "artifact" / f"uv-{target}"
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copy(harness.root / "stubs" / "uv", stage / "uv")
    (stage / "uv").chmod(0o755)
    (stage / "uvx").write_text("#!/bin/sh\nexit 0\n")
    (stage / "uvx").chmod(0o755)
    tarball = harness.root / "uv-artifact.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(tarball), "-C", str(stage.parent), f"uv-{target}"],
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(tarball.read_bytes()).hexdigest()


@pytest.fixture
def uv_download(harness: Harness):
    """Drive install_uv for real: no uv on PATH, a local artifact for curl."""
    target = _uv_target()
    if not target:
        pytest.skip("uv publishes no build for this architecture")
    path = _path_without_uv()
    if shutil.which("git", path=path) is None:
        pytest.skip("git shares a directory with uv on this host")
    digest = _publish_fake_uv(harness, target)
    return target, path, digest


def test_uv_is_downloaded_verified_and_installed(harness: Harness, uv_download):
    target, path, digest = uv_download
    bin_dir = Path(harness.env["SVTPLAY_ARR_BIN_DIR"])

    proc = harness.run(
        SVTPLAY_ARR_UV="", SVTPLAY_ARR_UV_SHA256=digest, PATH=path
    )

    assert "checksum verified" in proc.stdout
    assert (bin_dir / "uv").is_file()
    assert _mode(bin_dir / "uv") == 0o755
    assert (bin_dir / "uvx").is_file()
    # And the install went on to use it.
    assert (harness.releases[0] / ".venv" / "bin" / "uvicorn").is_file()


def test_uv_failing_its_pinned_checksum_is_refused(harness: Harness, uv_download):
    """No override here: the checksum table in the script is what judges it."""
    target, path, _ = uv_download
    if not re.search(rf"{re.escape(target)}\)\n\s+printf", INSTALL_SH.read_text()):
        pytest.skip(f"no pinned checksum for {target}")
    bin_dir = Path(harness.env["SVTPLAY_ARR_BIN_DIR"])

    proc = harness.run(expect=1, SVTPLAY_ARR_UV="", PATH=path)

    assert "checksum mismatch -- refusing to install" in proc.stderr
    assert not (bin_dir / "uv").exists()
    assert harness.releases == []
    assert not harness.config.exists()


def test_uv_download_failure_stops_before_extracting(harness: Harness, uv_download):
    target, path, _ = uv_download
    harness.fail_downloads()
    bin_dir = Path(harness.env["SVTPLAY_ARR_BIN_DIR"])

    proc = harness.run(expect=1, SVTPLAY_ARR_UV="", PATH=path)

    assert "could not download uv" in proc.stderr
    assert not (bin_dir / "uv").exists()
    assert harness.releases == []

    # The URL it asked for, and how it asked. A pin nobody checks is not a pin.
    request = next(
        line for line in harness.calls().splitlines() if ".tar.gz" in line
    )
    assert (
        f"https://github.com/astral-sh/uv/releases/download/"
        f"{_pinned_uv_version()}/uv-{target}.tar.gz" in request
    )
    assert "--proto =https" in request
    assert "--tlsv1.2" in request


def _pinned_uv_version() -> str:
    match = re.search(r'readonly UV_VERSION="([^"]+)"', INSTALL_SH.read_text())
    assert match
    return match.group(1)


# --------------------------------------------------------------------------
# The guarantees that are only guarantees if they are checked
# --------------------------------------------------------------------------


def test_config_changed_behind_the_installer_is_caught(harness: Harness):
    """The third layer of the config protection, doing something.

    Layers one and two stop this script writing config. This one is the claim
    that the files came through the upgrade unchanged whatever the cause, so
    it needs a change it did not make.
    """
    edited = _install_then_edit_config(harness)
    harness.release_upstream("0.2.0")
    harness.tamper_with_config_on_restart()

    proc = harness.run(expect=1)

    assert "changed during an upgrade" in proc.stderr
    assert "This must never happen" in proc.stderr
    assert harness.config.read_text() != edited  # it really was changed


def test_a_rollback_that_does_not_restore_service_says_so(harness: Harness):
    """The rollback verifies its own work, and reports honestly when it fails."""
    harness.run()
    first = harness.releases[0]
    harness.release_upstream("0.2.0")
    # Neither the upgrade nor the restored version comes back.
    harness.plan_health("", "")

    proc = harness.run(expect=1)

    assert "Rollback complete" not in proc.stdout
    assert "Rollback did not restore service" in proc.stdout
    assert "did not answer /health" in proc.stderr
    assert harness.current.resolve() == first.resolve()


# --------------------------------------------------------------------------
# Refusing dangerous targets
# --------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["/", "/etc", "/opt", "/usr", "/var", "/home"])
def test_refuses_a_shared_system_directory_as_prefix(harness: Harness, target: str):
    """`--prefix /opt` is a plausible reading of "install root".

    It would recursively chown every other application under /opt to the
    service account, as root, and exit 0 without saying anything.
    """
    proc = harness.run(expect=1, SVTPLAY_ARR_PREFIX=target)
    assert f"refusing {target}" in proc.stderr
    assert "recursively chowns" in proc.stderr
    assert "--prefix /opt/svtplay-arr" in proc.stderr


@pytest.mark.parametrize("target", ["/", "/etc", "/usr/local", "/var/lib"])
def test_refuses_a_shared_system_directory_as_config_dir(harness: Harness, target: str):
    """`--config-dir /etc` would take sudoers, the host keys and /etc/ssl."""
    proc = harness.run(expect=1, SVTPLAY_ARR_CONFIG_DIR=target)
    assert f"refusing {target}" in proc.stderr
    assert "--config-dir /etc/svtplay-arr" in proc.stderr


def test_refuses_to_adopt_a_directory_that_is_someone_elses(harness: Harness):
    intruder = harness.root / "opt" / "media-stack"
    (intruder / "plex").mkdir(parents=True)
    (intruder / "plex" / "Preferences.xml").write_text("<x/>")

    proc = harness.run(expect=1, SVTPLAY_ARR_PREFIX=str(intruder))

    assert "does not look like an" in proc.stderr
    assert "svtplay-arr prefix directory" in proc.stderr
    assert "Refusing to take it over" in proc.stderr
    assert "plex" in proc.stderr
    assert (intruder / "plex" / "Preferences.xml").is_file()


def test_adopts_an_empty_directory(harness: Harness):
    empty = harness.root / "opt" / "empty-but-mine"
    empty.mkdir(parents=True)
    harness.run(SVTPLAY_ARR_PREFIX=str(empty))
    assert (empty / "current").is_symlink()


@pytest.mark.parametrize("bad", ["relative/path", "/opt/../etc", "/opt/./x"])
def test_refuses_a_path_that_is_not_plainly_absolute(harness: Harness, bad: str):
    proc = harness.run(expect=1, SVTPLAY_ARR_PREFIX=bad)
    assert "must be an absolute path" in proc.stderr


def test_the_recursive_chown_is_scoped_to_what_the_script_created(harness: Harness):
    harness.run()
    chowns = [c for c in harness.calls().splitlines() if c.startswith("chown")]
    assert chowns
    for call in chowns:
        assert f"-R svtplay:media {harness.prefix}\n" not in call + "\n", call
        assert not call.endswith(f"-R svtplay:media {harness.prefix}"), call
    assert any(f"{harness.prefix}/releases" in c for c in chowns)
    assert any(f"{harness.prefix}/python" in c for c in chowns)


@pytest.mark.parametrize(
    "repo",
    [
        "ext::sh -c whoami",
        "git@github.com:jonasthim/svtplay-arr",
        "ssh://git@github.com/x/y",
        "http://example.invalid/x",
    ],
)
def test_refuses_a_repo_url_git_could_execute(harness: Harness, repo: str):
    proc = harness.run(expect=1, SVTPLAY_ARR_REPO=repo)
    assert "--repo" in proc.stderr
    assert harness.releases == []


@pytest.mark.parametrize(
    "flag,value",
    [
        ("SVTPLAY_ARR_HEALTH_TIMEOUT", "abc"),
        ("SVTPLAY_ARR_HEALTH_INTERVAL", "1.5"),
        ("SVTPLAY_ARR_KEEP_RELEASES", "-1"),
    ],
)
def test_refuses_a_non_numeric_setting_before_doing_anything(
    harness: Harness, flag: str, value: str
):
    """A bad timeout would otherwise trip `set -u` inside wait_for_health.

    That reads as "the service did not answer", and rolls back an upgrade that
    was fine.
    """
    proc = harness.run(expect=1, **{flag: value})
    assert "is not a whole number" in proc.stderr
    assert harness.releases == []
    assert not harness.config.exists()


def test_a_healthy_upgrade_is_not_rolled_back_by_a_bad_timeout(harness: Harness):
    harness.run()
    first = harness.releases[0]
    harness.release_upstream("0.2.0")

    proc = harness.run(expect=1, SVTPLAY_ARR_HEALTH_TIMEOUT="abc")

    assert "is not a whole number" in proc.stderr
    assert "Rolling back" not in proc.stdout
    assert harness.current.resolve() == first.resolve()


# --------------------------------------------------------------------------
# Which ref a fresh install lands on
# --------------------------------------------------------------------------


def test_the_default_target_is_the_newest_release_tag(harness: Harness):
    harness.release_upstream("0.2.0")
    harness.commit_upstream("0.3.0")  # untagged work on main

    proc = harness.run()

    assert "newest release tag: v0.2.0" in proc.stdout
    assert "installed: 0.2.0" in proc.stdout


def test_an_untagged_repository_is_refused_rather_than_installed_from_main(
    harness: Harness,
):
    """A fresh install has no rollback, so it must not land on an in-flight main."""
    harness.git("tag", "-d", "v0.1.0")

    proc = harness.run(expect=1)

    assert "no vN.N.N tag found" in proc.stderr
    assert "--ref main" in proc.stderr
    assert harness.releases == []


def test_ref_main_is_still_available_deliberately(harness: Harness):
    # commit_upstream's pyproject.toml claims "0.3.0", but that commit is
    # untagged -- one commit past v0.1.0 -- and nothing here ever tags it
    # v0.3.0. project_version no longer reads that field (see its own
    # comment in install.sh for why trusting it is the defect this project
    # shipped twice); it reports what the installed distribution actually
    # says, in the honestly-not-a-release .devN+g<sha> shape hatch-vcs
    # itself produces for a build in between tags, rather than echoing an
    # aspired version that was never true of this exact commit.
    harness.commit_upstream("0.3.0")
    sha = harness.git("rev-parse", "--short", "HEAD").strip()
    proc = harness.run("--ref", "main")
    assert f"installed: 0.1.1.dev1+g{sha}" in proc.stdout
    assert "installed: 0.3.0" not in proc.stdout


# --------------------------------------------------------------------------
# Version reporting
# --------------------------------------------------------------------------
#
# v0.3.1 replaced project_version()'s static `version =` read (three
# releases shipped with a number nobody remembered to bump) with
# `git describe`. That still shipped broken: set_ownership() chows every
# release directory -- .git included -- to the service account on every
# run, install included, so by the *next* run this script's own `git`,
# running as root, is refused ("detected dubious ownership in repository")
# and the function's own `2>/dev/null` swallowed the reason, leaving
# "unknown" everywhere an operator actually looks. /health, uv's install
# line and a bare `importlib.metadata.version()` all kept working, because
# none of them ask git anything.
#
# The tests below drive the installer end to end and read what it prints,
# not project_version() in isolation, because reading the package metadata
# directly is exactly the check that missed this the first time.


def test_before_and_after_survive_git_describe_being_refused(
    harness: Harness, tmp_path: Path
):
    """Stands in for a real install's git refusing `describe` against a
    release directory it does not own, the "dubious ownership" failure
    set_ownership()'s chown produces in production. Every other git
    subcommand this script uses (clone, ls-remote, checkout) still works --
    only `describe` fails, the same selective failure ownership causes."""
    git_stub = tmp_path / "git-describe-refused"
    _write_exec(
        git_stub,
        """#!/usr/bin/env bash
for arg in "$@"; do
    if [ "$arg" = describe ]; then
        echo "fatal: detected dubious ownership in repository at '...'" >&2
        exit 128
    fi
done
exec git "$@"
""",
    )
    harness.run(SVTPLAY_ARR_GIT=str(git_stub))
    harness.release_upstream("0.2.0")
    harness.reset_calls()

    proc = harness.run(SVTPLAY_ARR_GIT=str(git_stub))

    assert "before: 0.1.0" in proc.stdout
    assert "after:  0.2.0" in proc.stdout
    assert "unknown" not in proc.stdout


def test_currently_installed_survives_git_describe_being_refused(
    harness: Harness, tmp_path: Path
):
    git_stub = tmp_path / "git-describe-refused"
    _write_exec(
        git_stub,
        """#!/usr/bin/env bash
for arg in "$@"; do
    if [ "$arg" = describe ]; then
        exit 128
    fi
done
exec git "$@"
""",
    )
    harness.run(SVTPLAY_ARR_GIT=str(git_stub))
    harness.reset_calls()

    proc = harness.run(SVTPLAY_ARR_GIT=str(git_stub))

    assert "currently installed: 0.1.0" in proc.stdout
    assert "unknown" not in proc.stdout


def test_currently_installed_falls_back_to_git_when_the_venv_has_no_svtplay_arr(
    harness: Harness,
):
    """A release directory can survive with its venv half-built -- disk
    trouble mid-`uv sync`, or a manual poke -- while the checkout that built
    it is still intact. The dist-info this project's own build would have
    left behind is what's missing here; the fallback must still find a real
    version in the checkout rather than give up."""
    harness.run()
    release = harness.releases[0]
    for dist_info in (release / ".venv").glob("**/svtplay_arr-*.dist-info"):
        shutil.rmtree(dist_info)
    harness.reset_calls()

    proc = harness.run()

    assert "currently installed: 0.1.0" in proc.stdout
    assert "unknown" not in proc.stdout


def _project_version(dir_: Path, git: str = "git") -> str:
    """Calls project_version() directly, the way _uv_target() below calls
    uv_target() -- sourcing the script defines the function without running
    main()."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SVTPLAY_ARR_")}
    env["SVTPLAY_ARR_GIT"] = git
    proc = subprocess.run(
        ["bash", "-c", f'source "{INSTALL_SH}"; project_version "$1"', "_", str(dir_)],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _fabricate_venv(release: Path, version: str) -> None:
    subprocess.run(
        ["python3", "-m", "venv", "--without-pip", str(release / ".venv")],
        check=True,
        capture_output=True,
    )
    python = release / ".venv" / "bin" / "python3"
    purelib = subprocess.run(
        [str(python), "-c", 'import sysconfig; print(sysconfig.get_path("purelib"))'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dist_info = Path(purelib) / f"svtplay_arr-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: svtplay-arr\nVersion: {version}\n"
    )


def test_project_version_reads_the_installed_distribution(tmp_path: Path):
    release = tmp_path / "release"
    _fabricate_venv(release, "9.9.9")
    assert _project_version(release) == "9.9.9"


def test_project_version_does_not_need_git_when_the_venv_answers(tmp_path: Path):
    """The primary source really is primary: git here is not even a usable
    binary, and the version still comes back from the venv."""
    release = tmp_path / "release"
    _fabricate_venv(release, "9.9.9")
    assert _project_version(release, git=str(tmp_path / "no-such-git")) == "9.9.9"


def test_project_version_falls_back_to_git_describe(tmp_path: Path):
    release = tmp_path / "release"
    release.mkdir()
    subprocess.run(["git", "init", "-q", str(release)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(release), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(release), "config", "user.name", "test"], check=True
    )
    (release / "f").write_text("x")
    subprocess.run(
        ["git", "-C", str(release), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(release), "commit", "-q", "-m", "x"], check=True
    )
    subprocess.run(
        ["git", "-C", str(release), "tag", "v1.2.3"], check=True, capture_output=True
    )
    assert _project_version(release) == "1.2.3"


def test_project_version_is_unknown_with_neither_a_venv_nor_a_git_checkout(
    tmp_path: Path,
):
    nothing = tmp_path / "nothing-here"
    nothing.mkdir()
    assert _project_version(nothing) == "unknown"


def test_project_version_is_unknown_for_a_release_directory_that_does_not_exist(
    tmp_path: Path,
):
    assert _project_version(tmp_path / "does-not-exist") == "unknown"


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
