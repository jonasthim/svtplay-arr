#!/usr/bin/env bash
#
# svtplay-arr installer and upgrader.
#
# One command for both jobs. It looks at the host, decides whether this is a
# fresh install or an upgrade of an existing one, and does that. The operator
# does not have to know which.
#
#     curl -fsSLO https://raw.githubusercontent.com/jonasthim/svtplay-arr/main/install.sh
#     less install.sh          # you are installing a root-level service; read it
#     sudo bash install.sh
#
# What it needs from the host:
#
#   * root, to create a system user, write under /opt and /etc, and talk to
#     systemd;
#   * ffmpeg, which svtplay-dl shells out to for muxing (installed via apt
#     when apt is present; otherwise you install it and re-run);
#   * curl and git, to fetch uv and the source.
#
# It does NOT need a system Python. uv brings its own interpreter, which is
# the whole reason uv is here.
#
# Layout it creates:
#
#     /opt/svtplay-arr/releases/<commit>/   one directory per installed commit,
#                                          each with its own .venv
#     /opt/svtplay-arr/current -> releases/<commit>
#     /opt/svtplay-arr/python/              uv-managed interpreters (shared)
#     /etc/svtplay-arr/config.yaml          never touched once it exists
#     /etc/svtplay-arr/mappings.yaml        never touched once it exists
#     /etc/systemd/system/svtplay-arr.service
#
# An upgrade builds the new release in a new directory and only then flips
# the `current` symlink, so the running version is never modified in place
# and a rollback is a symlink flip rather than a restore.
#
# Every path above is overridable (see the variables below), every privileged
# command goes through a variable that can point at a stub, and --dry-run
# prints what would happen and changes nothing. tests/test_install_sh.py
# exercises all of it.

set -Eeuo pipefail

SCRIPT_NAME=${0##*/}
SCRIPT_VERSION="1.0.0"

# ---------------------------------------------------------------------------
# Overridable settings
#
# Anything the script reads or writes comes from one of these. Tests point
# them at a temporary tree; operators can too.
# ---------------------------------------------------------------------------

: "${SVTPLAY_ARR_PREFIX:=/opt/svtplay-arr}"
: "${SVTPLAY_ARR_CONFIG_DIR:=/etc/svtplay-arr}"
: "${SVTPLAY_ARR_UNIT_DIR:=/etc/systemd/system}"
: "${SVTPLAY_ARR_BIN_DIR:=/usr/local/bin}"
: "${SVTPLAY_ARR_REPO:=https://github.com/jonasthim/svtplay-arr}"
# Empty means "the newest vN.N.N tag in the repository". A fresh install has
# no previous release to fall back to, so it must not be pointed at whatever
# the development branch happens to be at that instant. --ref main is still
# there for anyone who wants it.
: "${SVTPLAY_ARR_REF:=}"
: "${SVTPLAY_ARR_USER:=svtplay}"
: "${SVTPLAY_ARR_GROUP:=media}"
: "${SVTPLAY_ARR_UNIT_NAME:=svtplay-arr.service}"
: "${SVTPLAY_ARR_HEALTH_URL:=http://127.0.0.1:9800/health}"
: "${SVTPLAY_ARR_HEALTH_TIMEOUT:=90}"
: "${SVTPLAY_ARR_HEALTH_INTERVAL:=2}"
: "${SVTPLAY_ARR_KEEP_RELEASES:=3}"
# The Python uv installs for the service. pyproject's requires-python is
# ">=3.12" with no ceiling; CI runs 3.12 and 3.13.
: "${SVTPLAY_ARR_PYTHON:=3.13}"

# Privileged or otherwise untestable interactions. Each is a command name by
# default and a path to a stub in the test suite.
: "${SVTPLAY_ARR_SYSTEMCTL:=systemctl}"
: "${SVTPLAY_ARR_USERADD:=useradd}"
: "${SVTPLAY_ARR_GROUPADD:=groupadd}"
: "${SVTPLAY_ARR_GETENT:=getent}"
: "${SVTPLAY_ARR_APT_GET:=apt-get}"
: "${SVTPLAY_ARR_CHOWN:=chown}"
: "${SVTPLAY_ARR_CURL:=curl}"
: "${SVTPLAY_ARR_GIT:=git}"
: "${SVTPLAY_ARR_FFMPEG:=ffmpeg}"
: "${SVTPLAY_ARR_UV:=}"

# The effective uid. A variable so a test can claim to be root without being
# root; nothing else in the script looks at id(1).
: "${SVTPLAY_ARR_EUID:=$(id -u)}"

# The listen address is not configurable, deliberately. The download link
# handed to Sonarr is built from the host Sonarr used to reach this service,
# so moving the port here without moving it in Sonarr breaks grabs in a way
# that looks like SVT breaking. deploy/README.md explains it.
readonly LISTEN_HOST="0.0.0.0"
readonly LISTEN_PORT="9800"

# uv is pinned, and the checksum of every artifact of that pin is baked in
# below. See install_uv() for why this is not `curl | sh`.
readonly UV_VERSION="0.12.6"

# The one seam in the download path: it lets a test hold a locally built
# artifact to a checksum it can know, so the success path is executed rather
# than only read. Empty in production, where the pinned table below is the
# only source of truth. It grants nothing that SVTPLAY_ARR_UV -- "use this uv
# instead" -- does not already grant.
: "${SVTPLAY_ARR_UV_SHA256:=}"

uv_expected_sha256() {
    case "$1" in
    x86_64-unknown-linux-gnu)
        printf '%s' '8681d8921e7d520fb368991dcf5f9c1905b80f5bf2a265a0ed085c8d8e342477'
        ;;
    aarch64-unknown-linux-gnu)
        printf '%s' 'd58030acd26159499ac82f32da12d1b3c12a3a1bfc414232d9082070c03e128d'
        ;;
    x86_64-unknown-linux-musl)
        printf '%s' '14e4172aace66a475062cebec7ca04f497d5619e95325dfcc9e4447b9c516846'
        ;;
    aarch64-unknown-linux-musl)
        printf '%s' '3719891de9ab41c878a84331e55826d2a46421976a346a65326513a6795b089a'
        ;;
    *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# Derived paths
# ---------------------------------------------------------------------------

RELEASES_DIR="${SVTPLAY_ARR_PREFIX}/releases"
CURRENT_LINK="${SVTPLAY_ARR_PREFIX}/current"
PYTHON_DIR="${SVTPLAY_ARR_PREFIX}/python"
UV_CACHE_DIR="${SVTPLAY_ARR_PREFIX}/.uv-cache"
UNIT_PATH="${SVTPLAY_ARR_UNIT_DIR}/${SVTPLAY_ARR_UNIT_NAME}"
UNIT_BACKUP="${SVTPLAY_ARR_PREFIX}/.${SVTPLAY_ARR_UNIT_NAME}.previous"
CONFIG_FILE="${SVTPLAY_ARR_CONFIG_DIR}/config.yaml"
MAPPINGS_FILE="${SVTPLAY_ARR_CONFIG_DIR}/mappings.yaml"
# Which release was last started AND seen answering /health. `current` alone
# cannot say that: a run killed between the symlink flip and the restart
# leaves `current` naming a release the running process has never been.
ACTIVE_MARKER="${SVTPLAY_ARR_PREFIX}/.active-release"

# A release directory is only trusted once this file is in it. An interrupted
# build leaves the directory without it, and the next run discards and
# rebuilds rather than booting a half-installed tree.
readonly RELEASE_STAMP=".svtplay-arr-release-ok"

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

DRY_RUN=false
MODE=""              # install | upgrade
PHASE="starting up"  # what the failure trap reports
PREVIOUS_TARGET=""   # what `current` pointed at before we touched it
PREVIOUS_DESC=""     # human-readable version we started from
UNIT_BACKED_UP=false
ACTIVATED=false      # the symlink flip happened
CONFIG_HASH_BEFORE=""
MAPPINGS_HASH_BEFORE=""
CONFIG_SEEDED=false  # this run wrote config.yaml from the example
PRE_HEALTH_STATUS=""
PRE_HEALTH_BODY=""   # the whole pre-upgrade /health body, for service_regressed
REGRESSION=""        # what service_regressed found, for the rollback message
TMP_DIR=""

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
log() { printf '    %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
err() { printf 'error: %s\n' "$*" >&2; }

die() {
    err "$*"
    printf 'state: %s\n' "$(state_summary)" >&2
    exit 1
}

state_summary() {
    local active
    if [[ -L $CURRENT_LINK ]]; then
        active="current -> $(readlink "$CURRENT_LINK")"
    elif [[ -d ${SVTPLAY_ARR_PREFIX}/.venv ]]; then
        active="pre-release-layout checkout at ${SVTPLAY_ARR_PREFIX}"
    else
        active="no release is active"
    fi
    printf 'failed while %s; %s; config at %s untouched' \
        "$PHASE" "$active" "$SVTPLAY_ARR_CONFIG_DIR"
}

on_err() {
    local rc=$1 line=$2
    err "unexpected failure (exit ${rc}) at ${SCRIPT_NAME} line ${line}"
    printf 'state: %s\n' "$(state_summary)" >&2
}
trap 'on_err $? $LINENO' ERR

cleanup() {
    if [[ -n $TMP_DIR && -d $TMP_DIR ]]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT

# Every mutation goes through this. --dry-run is therefore a property of the
# script rather than a thing each step has to remember.
run() {
    if $DRY_RUN; then
        printf '    would: %s\n' "$*"
        return 0
    fi
    "$@"
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

usage() {
    cat <<USAGE
${SCRIPT_NAME} ${SCRIPT_VERSION} - install or upgrade svtplay-arr

Usage: sudo bash ${SCRIPT_NAME} [options]

The same command installs on a fresh host and upgrades an existing
installation; which one it is is detected, not chosen.

Options:
  --dry-run             Print every action and change nothing. Does not
                        need root.
  --ref REF             Branch, tag or commit to install. Default: the
                        newest vN.N.N tag in the repository, so a fresh
                        install never lands on an in-flight main.
  --repo URL            Source repository, https:// or file:/// only
                        (default: ${SVTPLAY_ARR_REPO}).
  --prefix DIR          Install root (default: ${SVTPLAY_ARR_PREFIX}). Must be a
                        directory of its own: this script chowns what is
                        under it to ${SVTPLAY_ARR_USER}.
  --config-dir DIR      Configuration directory (default: ${SVTPLAY_ARR_CONFIG_DIR}).
                        Same rule: a directory of its own.
  --unit-dir DIR        systemd unit directory (default: ${SVTPLAY_ARR_UNIT_DIR}).
  --health-timeout N    Seconds to wait for /health (default: ${SVTPLAY_ARR_HEALTH_TIMEOUT}).
  --keep N              Old releases to keep (default: ${SVTPLAY_ARR_KEEP_RELEASES}).
  -h, --help            This text.
  --version             Print the script version.

config.yaml and mappings.yaml are written only when they do not exist. An
upgrade cannot modify them; the script verifies that it did not. Their
directory's mode and ownership ARE reasserted on every run.
USAGE
}

# ---------------------------------------------------------------------------
# Argument validation
#
# --prefix and --config-dir become the target of a recursive chown and a
# chmod that run as root. `--prefix /opt` is a plausible reading of "install
# root" and would hand every other application under /opt to the service
# account; `--config-dir /etc` would take sudoers, the sshd host keys and
# /etc/ssl with it, and both would exit 0 having said nothing. That is not
# abuse, it is an ordinary misreading of a plain options table -- so the
# values are checked before anything runs.
# ---------------------------------------------------------------------------

# Never a valid target, whatever the flag says.
readonly RESERVED_PATHS=(
    / /bin /boot /dev /etc /etc/systemd /home /lib /lib32 /lib64 /libx32
    /media /mnt /opt /proc /root /run /sbin /srv /sys /tmp /usr /usr/bin
    /usr/lib /usr/local /usr/local/bin /usr/sbin /usr/share /var /var/lib
    /var/log /var/run /var/tmp
)

# Absolute, no "." or ".." components, no trailing or doubled slashes.
# Prints the cleaned path, or returns 1 (never exits -- the caller is often
# an assignment, and an exit inside $() would only kill the subshell).
normalize_path() {
    local p=$1
    if [[ $p != /* ]]; then
        return 1
    fi
    while [[ $p == *//* ]]; do
        p=${p//\/\//\/}
    done
    while [[ ${#p} -gt 1 && $p == */ ]]; do
        p=${p%/}
    done
    case $p in
    */../* | */.. | */./* | */.) return 1 ;;
    esac
    printf '%s' "$p"
}

is_reserved_path() {
    local candidate=$1 reserved
    for reserved in "${RESERVED_PATHS[@]}"; do
        if [[ $candidate == "$reserved" ]]; then
            return 0
        fi
    done
    return 1
}

dir_is_empty() {
    local entries
    entries=$(find "$1" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)
    [[ -z $entries ]]
}

# Result of assert_safe_target, so that a failure can `die` in the caller's
# shell rather than in a command substitution's subshell.
NORMALIZED_PATH=""

assert_safe_target() {
    local flag=$1 raw=$2 suggestion=$3 normalized
    normalized=$(normalize_path "$raw") ||
        die "${flag}: ${raw} must be an absolute path with no '.' or '..' components"
    if is_reserved_path "$normalized"; then
        err "${flag}: refusing ${normalized}."
        err ""
        err "This script chmods and recursively chowns the directories it is given"
        err "to ${SVTPLAY_ARR_USER}:${SVTPLAY_ARR_GROUP}, as root. Pointing it at a shared system"
        err "directory would hand that directory, and everything already under it,"
        err "to the service account."
        err ""
        die "give it one of its own: ${flag} ${suggestion}"
    fi
    NORMALIZED_PATH=$normalized
}

# Refuse to adopt a directory that already holds someone else's files.
assert_target_is_ours() {
    local flag=$1 path=$2 kind=$3
    if [[ ! -e $path ]]; then
        return 0
    fi
    if [[ ! -d $path ]]; then
        die "${flag}: ${path} exists and is not a directory"
    fi
    if dir_is_empty "$path"; then
        return 0
    fi
    case $kind in
    prefix)
        # releases/ and current are this layout; .venv is the hand-built one
        # that an upgrade migrates.
        if [[ -d ${path}/releases || -L ${path}/current || -d ${path}/.venv ]]; then
            return 0
        fi
        ;;
    config)
        if [[ -f ${path}/config.yaml || -f ${path}/mappings.yaml ]]; then
            return 0
        fi
        ;;
    esac
    err "${flag}: ${path} exists, is not empty, and does not look like an"
    err "svtplay-arr ${kind} directory. Refusing to take it over -- this script"
    err "chmods and recursively chowns that directory as root."
    err "It contains: $(find "$path" -mindepth 1 -maxdepth 1 -printf '%f ' 2>/dev/null | head -c 160)"
    die "point ${flag} somewhere of its own, or clear ${path} first"
}

# git resolves a remote through helper programs, and some of those transports
# execute a command taken from the URL itself (ext::sh -c ...). The scheme is
# therefore restricted here rather than handed to `git clone` as typed.
validate_repo() {
    case $SVTPLAY_ARR_REPO in
    https://?*) ;;
    file:///?*) ;;
    *)
        err "--repo: only https:// and file:/// URLs are accepted."
        err "git reads some remote URLs as commands to run (ext::sh -c ...), so"
        err "the scheme is checked rather than passed straight through."
        die "refusing ${SVTPLAY_ARR_REPO}"
        ;;
    esac
    if [[ $SVTPLAY_ARR_REPO == *"::"* ]]; then
        die "--repo: ${SVTPLAY_ARR_REPO} contains '::', which git reads as a remote helper"
    fi
}

# A non-numeric timeout would otherwise trip `set -u` deep inside
# wait_for_health, which reads as "the service did not answer" -- and would
# roll back a perfectly healthy upgrade.
validate_number() {
    local flag=$1 value=$2
    if [[ ! $value =~ ^[0-9]+$ ]]; then
        die "${flag}: '${value}' is not a whole number"
    fi
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
        --dry-run) DRY_RUN=true ;;
        --ref)
            [[ $# -ge 2 ]] || die "--ref needs a value"
            SVTPLAY_ARR_REF=$2
            shift
            ;;
        --repo)
            [[ $# -ge 2 ]] || die "--repo needs a value"
            SVTPLAY_ARR_REPO=$2
            shift
            ;;
        --prefix)
            [[ $# -ge 2 ]] || die "--prefix needs a value"
            SVTPLAY_ARR_PREFIX=$2
            shift
            ;;
        --config-dir)
            [[ $# -ge 2 ]] || die "--config-dir needs a value"
            SVTPLAY_ARR_CONFIG_DIR=$2
            shift
            ;;
        --unit-dir)
            [[ $# -ge 2 ]] || die "--unit-dir needs a value"
            SVTPLAY_ARR_UNIT_DIR=$2
            shift
            ;;
        --health-timeout)
            [[ $# -ge 2 ]] || die "--health-timeout needs a value"
            SVTPLAY_ARR_HEALTH_TIMEOUT=$2
            shift
            ;;
        --keep)
            [[ $# -ge 2 ]] || die "--keep needs a value"
            SVTPLAY_ARR_KEEP_RELEASES=$2
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        --version)
            printf '%s\n' "$SCRIPT_VERSION"
            exit 0
            ;;
        *) die "unknown option: $1 (try --help)" ;;
        esac
        shift
    done

    validate_repo
    validate_number --health-timeout "$SVTPLAY_ARR_HEALTH_TIMEOUT"
    validate_number --health-interval "$SVTPLAY_ARR_HEALTH_INTERVAL"
    validate_number --keep "$SVTPLAY_ARR_KEEP_RELEASES"

    assert_safe_target --prefix "$SVTPLAY_ARR_PREFIX" /opt/svtplay-arr
    SVTPLAY_ARR_PREFIX=$NORMALIZED_PATH
    assert_safe_target --config-dir "$SVTPLAY_ARR_CONFIG_DIR" /etc/svtplay-arr
    SVTPLAY_ARR_CONFIG_DIR=$NORMALIZED_PATH
    assert_safe_target --unit-dir "$SVTPLAY_ARR_UNIT_DIR" /etc/systemd/system
    SVTPLAY_ARR_UNIT_DIR=$NORMALIZED_PATH

    assert_target_is_ours --prefix "$SVTPLAY_ARR_PREFIX" prefix
    assert_target_is_ours --config-dir "$SVTPLAY_ARR_CONFIG_DIR" config

    # --prefix/--config-dir/--unit-dir land after the derived paths were
    # computed from the defaults, so recompute.
    RELEASES_DIR="${SVTPLAY_ARR_PREFIX}/releases"
    CURRENT_LINK="${SVTPLAY_ARR_PREFIX}/current"
    PYTHON_DIR="${SVTPLAY_ARR_PREFIX}/python"
    UV_CACHE_DIR="${SVTPLAY_ARR_PREFIX}/.uv-cache"
    UNIT_PATH="${SVTPLAY_ARR_UNIT_DIR}/${SVTPLAY_ARR_UNIT_NAME}"
    UNIT_BACKUP="${SVTPLAY_ARR_PREFIX}/.${SVTPLAY_ARR_UNIT_NAME}.previous"
    CONFIG_FILE="${SVTPLAY_ARR_CONFIG_DIR}/config.yaml"
    MAPPINGS_FILE="${SVTPLAY_ARR_CONFIG_DIR}/mappings.yaml"
    ACTIVE_MARKER="${SVTPLAY_ARR_PREFIX}/.active-release"
}

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

require_root() {
    if [[ $SVTPLAY_ARR_EUID -eq 0 ]]; then
        return 0
    fi
    if $DRY_RUN; then
        log "not running as root; --dry-run changes nothing, so carrying on"
        return 0
    fi
    err "this script must run as root."
    err "It creates the ${SVTPLAY_ARR_USER} system user and the ${SVTPLAY_ARR_GROUP} group,"
    err "writes ${SVTPLAY_ARR_PREFIX} and ${SVTPLAY_ARR_CONFIG_DIR}, installs a unit into"
    err "${SVTPLAY_ARR_UNIT_DIR}, and calls systemctl. None of that is possible unprivileged."
    err ""
    err "    sudo bash ${SCRIPT_NAME}"
    err ""
    err "To see exactly what it would do first, without root and without changes:"
    err ""
    err "    bash ${SCRIPT_NAME} --dry-run"
    exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }

report_platform() {
    local pretty="unknown" kernel arch
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        pretty=$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-${NAME:-unknown}}")
    fi
    kernel=$(uname -sr)
    arch=$(uname -m)
    step "Platform"
    log "distribution: ${pretty}"
    log "kernel:       ${kernel}"
    log "architecture: ${arch}"
    if have "$SVTPLAY_ARR_APT_GET"; then
        log "packages:     apt (ffmpeg will be installed for you if missing)"
    else
        log "packages:     no apt; prerequisites must already be present"
    fi
}

# apt is the documented deployment. On anything else the script refuses to
# guess at package names for a distro nobody has tested this on, and instead
# says exactly what is missing.
ensure_os_prereqs() {
    step "Host prerequisites"

    local missing=()
    have "$SVTPLAY_ARR_FFMPEG" || missing+=(ffmpeg)
    have "$SVTPLAY_ARR_GIT" || missing+=(git)
    have "$SVTPLAY_ARR_CURL" || missing+=(curl)

    if [[ ${#missing[@]} -eq 0 ]]; then
        log "ffmpeg, git and curl are present"
        return 0
    fi

    if ! have "$SVTPLAY_ARR_APT_GET"; then
        err "missing: ${missing[*]}"
        err ""
        err "This host has no apt, and guessing package names for an untested"
        err "distribution is worse than saying so. Install the above with your"
        err "package manager and run this script again. Nothing has changed."
        exit 1
    fi

    log "installing with apt: ${missing[*]} ca-certificates"
    run "$SVTPLAY_ARR_APT_GET" update -qq
    run env DEBIAN_FRONTEND=noninteractive "$SVTPLAY_ARR_APT_GET" install -y \
        --no-install-recommends "${missing[@]}" ca-certificates
}

# The uv target triple for this host, or failure if it is one uv does not
# publish a Linux build for.
uv_target() {
    local machine libc
    machine=$(uname -m)
    case $machine in
    x86_64 | amd64) machine=x86_64 ;;
    aarch64 | arm64) machine=aarch64 ;;
    *) return 1 ;;
    esac
    if [[ -e /lib/ld-musl-${machine}.so.1 ]]; then
        libc=musl
    elif ldd --version 2>&1 | head -n1 | grep -qi musl; then
        libc=musl
    else
        libc=gnu
    fi
    printf '%s-unknown-linux-%s' "$machine" "$libc"
}

# Not `curl https://astral.sh/uv/install.sh | sh`.
#
# That pipes whatever is served at fetch time straight into a root shell:
# nothing is pinned, nothing is verified, and there is no artifact left to
# audit afterwards. An operator who refuses to run that is right to. So:
# pin a version, fetch that exact release artifact, check it against the
# SHA-256 recorded in this script (not one downloaded next to the tarball,
# which would only prove the two came from the same place), and stop if they
# disagree.
install_uv() {
    local target url sha tarball actual
    target=$(uv_target) || die "no uv build for $(uname -m); install uv yourself and re-run"
    if [[ -n $SVTPLAY_ARR_UV_SHA256 ]]; then
        sha=$SVTPLAY_ARR_UV_SHA256
    else
        sha=$(uv_expected_sha256 "$target") ||
            die "no pinned checksum for uv target ${target}; install uv yourself and re-run"
    fi
    url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz"

    log "uv is not installed; fetching uv ${UV_VERSION} for ${target}"
    log "from     ${url}"
    log "expected sha256 ${sha}"

    if $DRY_RUN; then
        printf '    would: download, verify sha256, and install uv and uvx into %s\n' \
            "$SVTPLAY_ARR_BIN_DIR"
        SVTPLAY_ARR_UV="${SVTPLAY_ARR_BIN_DIR}/uv"
        return 0
    fi

    have sha256sum || die "sha256sum is missing; cannot verify the uv download"

    TMP_DIR=$(mktemp -d)
    tarball="${TMP_DIR}/uv.tar.gz"
    "$SVTPLAY_ARR_CURL" -fsSL --proto '=https' --tlsv1.2 -o "$tarball" "$url" ||
        die "could not download uv from ${url}; nothing has changed"

    actual=$(sha256sum "$tarball" | cut -d' ' -f1)
    if [[ $actual != "$sha" ]]; then
        rm -f "$tarball"
        err "uv checksum mismatch -- refusing to install."
        err "  expected ${sha}"
        err "  actual   ${actual}"
        die "the download was discarded and nothing has changed"
    fi
    log "checksum verified"

    tar -xzf "$tarball" -C "$TMP_DIR"
    install -d -m 0755 "$SVTPLAY_ARR_BIN_DIR"
    install -m 0755 "${TMP_DIR}/uv-${target}/uv" "${SVTPLAY_ARR_BIN_DIR}/uv"
    install -m 0755 "${TMP_DIR}/uv-${target}/uvx" "${SVTPLAY_ARR_BIN_DIR}/uvx"
    rm -rf "$TMP_DIR"
    TMP_DIR=""
    SVTPLAY_ARR_UV="${SVTPLAY_ARR_BIN_DIR}/uv"
    log "installed ${SVTPLAY_ARR_BIN_DIR}/uv"
}

ensure_uv() {
    step "uv"
    if [[ -n $SVTPLAY_ARR_UV ]]; then
        log "using ${SVTPLAY_ARR_UV}"
        return 0
    fi
    if have uv; then
        SVTPLAY_ARR_UV=$(command -v uv)
        log "already installed: ${SVTPLAY_ARR_UV}"
        return 0
    fi
    if [[ -x ${SVTPLAY_ARR_BIN_DIR}/uv ]]; then
        SVTPLAY_ARR_UV="${SVTPLAY_ARR_BIN_DIR}/uv"
        log "already installed: ${SVTPLAY_ARR_UV}"
        return 0
    fi
    install_uv
}

ensure_account() {
    step "Service account"

    # On a dedicated container the media group does not exist. That is the
    # normal path here, not an edge case: it is a host and NFS-export
    # concept, and a fresh container inherits nothing.
    if "$SVTPLAY_ARR_GETENT" group "$SVTPLAY_ARR_GROUP" >/dev/null 2>&1; then
        log "group ${SVTPLAY_ARR_GROUP} exists"
    else
        log "creating group ${SVTPLAY_ARR_GROUP}"
        run "$SVTPLAY_ARR_GROUPADD" --system "$SVTPLAY_ARR_GROUP"
    fi

    if "$SVTPLAY_ARR_GETENT" passwd "$SVTPLAY_ARR_USER" >/dev/null 2>&1; then
        log "user ${SVTPLAY_ARR_USER} exists"
    else
        log "creating user ${SVTPLAY_ARR_USER}"
        run "$SVTPLAY_ARR_USERADD" --system --no-create-home \
            --shell /usr/sbin/nologin --gid "$SVTPLAY_ARR_GROUP" "$SVTPLAY_ARR_USER"
    fi
}

ensure_directories() {
    run install -d -m 0755 "$SVTPLAY_ARR_PREFIX"
    run install -d -m 0755 "$RELEASES_DIR"
    run install -d -m 0755 "$PYTHON_DIR"
}

# ---------------------------------------------------------------------------
# Configuration
#
# seed_config_file is the ONLY thing in this script that writes into
# SVTPLAY_ARR_CONFIG_DIR, and it refuses to write over an existing file. The
# upgrade path therefore cannot modify config.yaml or mappings.yaml even by
# accident -- and capture_config_fingerprint/assert_config_untouched prove it
# afterwards rather than asking anyone to take it on trust.
#
# tests/test_install_sh.py::test_only_the_seeding_functions_write_to_the_
# config_directory enforces the "only thing" half of that claim against the
# source of this file, so a future edit cannot quietly add a second writer.
# ---------------------------------------------------------------------------

seed_config_file() {
    local src=$1 dest=$2
    if [[ -e $dest ]]; then
        log "keeping existing ${dest}"
        return 0
    fi
    if [[ ! -r $src ]]; then
        die "example file ${src} is missing from the release"
    fi
    log "writing ${dest} from $(basename "$src")"
    run install -m 0640 "$src" "$dest"
    CONFIG_SEEDED=true
}

seed_config() {
    local release_dir=$1
    step "Configuration"
    # 0750: config.yaml holds the Sonarr API key, so the directory is not
    # readable beyond the service account and its group.
    run install -d -m 0750 "$SVTPLAY_ARR_CONFIG_DIR"
    seed_config_file "${release_dir}/deploy/config.example.yaml" "$CONFIG_FILE"
    seed_config_file "${release_dir}/deploy/mappings.example.yaml" "$MAPPINGS_FILE"
    run "$SVTPLAY_ARR_CHOWN" -R "${SVTPLAY_ARR_USER}:${SVTPLAY_ARR_GROUP}" \
        "$SVTPLAY_ARR_CONFIG_DIR"
}

_hash_or_absent() {
    if [[ -f $1 ]]; then
        sha256sum "$1" | cut -d' ' -f1
    else
        printf 'absent'
    fi
}

capture_config_fingerprint() {
    CONFIG_HASH_BEFORE=$(_hash_or_absent "$CONFIG_FILE")
    MAPPINGS_HASH_BEFORE=$(_hash_or_absent "$MAPPINGS_FILE")
}

_assert_one_untouched() {
    local path=$1 before=$2 now
    # A file that did not exist may have been seeded -- that is a repair of a
    # half-installed system, not a clobber. A file that did exist must come
    # out the other side identical.
    if [[ $before == absent ]]; then
        return 0
    fi
    now=$(_hash_or_absent "$path")
    if [[ $now != "$before" ]]; then
        err "${path} changed during an upgrade. This must never happen."
        err "  before ${before}"
        err "  after  ${now}"
        return 1
    fi
    return 0
}

assert_config_untouched() {
    local ok=0
    _assert_one_untouched "$CONFIG_FILE" "$CONFIG_HASH_BEFORE" || ok=1
    _assert_one_untouched "$MAPPINGS_FILE" "$MAPPINGS_HASH_BEFORE" || ok=1
    if [[ $ok -ne 0 ]]; then
        err "The service is running the new release, but an installer that can"
        err "rewrite configuration is a bug regardless of the outcome. Restore"
        err "your configuration and report this."
        exit 1
    fi
    log "config.yaml and mappings.yaml are byte-for-byte unchanged"
}

# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

# A fresh install has nothing to roll back to, so the default target is the
# newest release tag rather than the tip of the development branch: a bad
# main should not be the first thing a stranger meets. Upgrades follow the
# same rule, which keeps "run the same command again" true.
latest_release_tag() {
    local line
    line=$("$SVTPLAY_ARR_GIT" ls-remote --tags --refs --sort=-v:refname \
        "$SVTPLAY_ARR_REPO" 'v[0-9]*' 2>/dev/null | head -n1 || true)
    if [[ -z $line ]]; then
        return 1
    fi
    printf '%s' "${line##*refs/tags/}"
}

resolve_ref() {
    local line
    line=$("$SVTPLAY_ARR_GIT" ls-remote "$SVTPLAY_ARR_REPO" "$SVTPLAY_ARR_REF" 2>/dev/null |
        head -n1 || true)
    if [[ -n $line ]]; then
        printf '%s' "${line%%[[:space:]]*}"
        return 0
    fi
    if [[ $SVTPLAY_ARR_REF =~ ^[0-9a-fA-F]{7,40}$ ]]; then
        printf '%s' "$SVTPLAY_ARR_REF"
        return 0
    fi
    return 1
}

release_id() { printf '%s' "${1:0:12}"; }

# pyproject.toml has no static version field to read any more -- three
# releases shipped with one nobody remembered to bump, which is the whole
# defect PEP 440-via-hatch-vcs replaced (see pyproject.toml's
# [tool.hatch.version] comment). The first fix here was `git describe`
# against the checkout fetch_release() clones -- correct in isolation, and
# what shipped as v0.3.1. It was still wrong in production: set_ownership()
# runs on *every* install and upgrade, and it chowns every release directory
# -- .git included -- to the service account. By the next run, this
# script's own `git`, running as root, refuses to touch a directory root
# does not own ("detected dubious ownership in repository"), and the
# `2>/dev/null` this function already had swallowed the reason, leaving
# only "unknown". `/health`, uv's own install line and a bare
# `importlib.metadata.version()` all kept working through the same
# upgrades, because none of them ask git anything -- they read the
# venv's installed distribution metadata, which ownership never touches.
#
# So that is what this asks first: the venv's own interpreter, reporting
# its own installed "svtplay-arr" distribution. It is what every other
# working consumer already reads, it describes what is actually installed
# rather than what git believes was meant to be, and it cannot disagree
# with /health because it is the same call /health makes.
#
# `git describe` is kept as a fallback, tried only when the venv can't
# answer -- no interpreter (a release directory left half-built by an
# interrupted run) or no "svtplay-arr" distribution recorded in it. In
# that case the checkout is the only version information left on disk, and
# `-c safe.directory` is passed explicitly so the same ownership mismatch
# that broke the first fix cannot take the fallback out too. The leading
# "v" this project's tags use (v0.1.0, v0.2.0, ...) is stripped so this
# reads like the version hatch-vcs reports (PEP 440 has no "v" prefix).
#
# "unknown" is left for when neither source has anything to say: no venv,
# and $dir is not a git checkout either.
project_version() {
    local dir=$1 python=$1/.venv/bin/python v

    if [[ -x $python ]]; then
        v=$("$python" -c '
import importlib.metadata as m, sys
try:
    print(m.version("svtplay-arr"))
except m.PackageNotFoundError:
    sys.exit(1)
' 2>/dev/null || true)
        if [[ -n $v ]]; then
            printf '%s' "$v"
            return 0
        fi
    fi

    v=$("$SVTPLAY_ARR_GIT" -c "safe.directory=${dir}" -C "$dir" \
        describe --tags --always --dirty 2>/dev/null || true)
    [[ $v == v[0-9]* ]] && v=${v#v}
    printf '%s' "${v:-unknown}"
}

# What is running right now, in words.
describe_current() {
    local target
    if [[ -L $CURRENT_LINK ]]; then
        target=$(readlink -f "$CURRENT_LINK" 2>/dev/null || readlink "$CURRENT_LINK")
        printf '%s (%s)' "$(project_version "$target")" "$(basename "$target")"
        return 0
    fi
    if [[ -d ${SVTPLAY_ARR_PREFIX}/.venv ]]; then
        local sha
        sha=$("$SVTPLAY_ARR_GIT" -C "$SVTPLAY_ARR_PREFIX" rev-parse --short=12 HEAD 2>/dev/null || true)
        printf '%s (%s, pre-release-layout checkout)' \
            "$(project_version "$SVTPLAY_ARR_PREFIX")" "${sha:-unknown}"
        return 0
    fi
    printf 'none'
}

# A release is built where it will live, never staged elsewhere and renamed.
#
# A virtualenv is not relocatable: uv bakes the absolute path of
# .venv/bin/python into every console script it generates, and the editable
# install of the project itself records an absolute path to src/. Building in
# <dir>.staging and renaming afterwards produces an ExecStart that cannot
# start -- which is exactly what an earlier version of this script did.
#
# Completeness is therefore marked by the stamp file rather than by the
# directory's name. It is written last; a directory without it is the debris
# of an interrupted run and is discarded here rather than trusted.
fetch_release() {
    local dest=$1 sha=$2

    if [[ -f "${dest}/${RELEASE_STAMP}" ]]; then
        log "release $(basename "$dest") is already built; reusing it"
        return 0
    fi
    if [[ -e $dest ]]; then
        # prune_releases makes this same comparison before it removes
        # anything; without it here, a missing stamp on the ACTIVE release
        # would delete the tree the running service was started from. The
        # process survives on open file handles, so the script would go on to
        # report the old version as still running while releases/ was empty
        # and `current` dangled -- and the service would be gone at the next
        # restart, or the next reboot.
        if [[ -L $CURRENT_LINK ]] &&
            [[ $(readlink -f "$CURRENT_LINK" 2>/dev/null) == "$(readlink -f "$dest" 2>/dev/null)" ]]; then
            err "${dest} is the release the service is running from, and it has no"
            err "${RELEASE_STAMP} -- so this script cannot tell whether it is complete."
            err "Removing it would take the running service with it."
            err ""
            err "Install a different ref instead (--ref ...), or stop the service and"
            die "remove ${dest} by hand once you are sure of what is in it"
        fi
        log "discarding an incomplete ${dest} left by an earlier run"
        run rm -rf "$dest"
    fi

    log "cloning ${SVTPLAY_ARR_REPO}"
    run "$SVTPLAY_ARR_GIT" clone --quiet "$SVTPLAY_ARR_REPO" "$dest" ||
        die "could not clone ${SVTPLAY_ARR_REPO}; nothing has changed"
    run "$SVTPLAY_ARR_GIT" -C "$dest" checkout --quiet --detach "$sha" ||
        die "could not check out ${sha}; nothing has changed"

    build_release "$dest"

    run touch "${dest}/${RELEASE_STAMP}"
}

build_release() {
    local dir=$1
    log "building the environment with uv (it provides Python ${SVTPLAY_ARR_PYTHON}; the host does not need one)"
    # --python pins the interpreter to a version CI actually tests against.
    # requires-python has no upper bound, so without this uv would happily
    # pick whatever the newest release is on the day of the install and run
    # the service on an interpreter nothing here has ever been run on.
    #
    # --python-preference only-managed keeps a stray system interpreter out
    # of it entirely, which is the point of using uv here.
    if ! run env \
        UV_PYTHON_INSTALL_DIR="$PYTHON_DIR" \
        UV_CACHE_DIR="$UV_CACHE_DIR" \
        UV_PYTHON_PREFERENCE=only-managed \
        UV_NO_PROGRESS=1 \
        "$SVTPLAY_ARR_UV" sync --frozen --python "$SVTPLAY_ARR_PYTHON" --project "$dir"; then
        run rm -rf "$dir"
        err "uv could not build the environment for this release."
        if [[ $MODE == upgrade ]]; then
            die "the upgrade was abandoned before anything was switched over; ${PREVIOUS_DESC} is still running"
        fi
        die "nothing was installed"
    fi
}

# Scoped to the directories this script itself created, never the prefix as a
# whole. Validation already refuses a shared prefix; this refuses to make that
# check the only thing standing between a typo and a recursive root chown of
# somebody else's application directory. The prefix itself stays root-owned
# and 0755, which is all the service needs to traverse it.
set_ownership() {
    step "Ownership"
    local -a targets=("$RELEASES_DIR" "$PYTHON_DIR")
    if [[ -d $UV_CACHE_DIR ]] || $DRY_RUN; then
        targets+=("$UV_CACHE_DIR")
    fi
    log "${SVTPLAY_ARR_USER}:${SVTPLAY_ARR_GROUP} on ${targets[*]}"
    run "$SVTPLAY_ARR_CHOWN" -R "${SVTPLAY_ARR_USER}:${SVTPLAY_ARR_GROUP}" "${targets[@]}"
}

activate_release() {
    local dest=$1
    run ln -sfn "$dest" "${CURRENT_LINK}.new"
    run mv -Tf "${CURRENT_LINK}.new" "$CURRENT_LINK"
    ACTIVATED=true
}

prune_releases() {
    local keep=$SVTPLAY_ARR_KEEP_RELEASES active
    [[ -d $RELEASES_DIR ]] || return 0
    active=$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)
    local -a candidates=()
    local d
    while IFS= read -r d; do
        [[ -n $d ]] || continue
        if [[ -n $active && $(readlink -f "$d") == "$active" ]]; then
            continue
        fi
        candidates+=("$d")
    done < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null |
        sort -rn | cut -d' ' -f2-)

    # keep - 1, because the active release is one of the kept ones.
    local drop_from=$((keep - 1))
    if [[ $drop_from -lt 0 ]]; then
        drop_from=0
    fi
    local i
    for ((i = drop_from; i < ${#candidates[@]}; i++)); do
        log "removing old release $(basename "${candidates[$i]}")"
        run rm -rf "${candidates[$i]}"
    done
}

# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------

# The generated unit and deploy/svtplay-arr.service must not drift: the only
# difference is that this one points at the `current` symlink instead of a
# fixed checkout. tests/test_install_sh.py::test_generated_unit_matches_packaged_unit
# compares every directive of the two.
render_unit() {
    cat <<UNIT
[Unit]
Description=svtplay-arr
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVTPLAY_ARR_USER}
Group=${SVTPLAY_ARR_GROUP}
# NFS exports commonly squash identities, which makes a chown inside a
# container cosmetic. The umask is what actually makes writes land as 664 /
# 775 for the rest of the media stack.
UMask=0002
# systemd creates /var/lib/svtplay-arr owned ${SVTPLAY_ARR_USER}:${SVTPLAY_ARR_GROUP} before the
# process starts; that is where Settings.db_path defaults to.
StateDirectory=svtplay-arr
# Deliberately empty. The key lives in config.yaml so the configuration page
# can edit it; a non-empty value here silently overrides the file.
Environment=SONARR_API_KEY=
Environment=SVTPLAY_ARR_CONFIG=${CONFIG_FILE}
# Written by install.sh. ExecStart goes through the 'current' symlink, so an
# upgrade or a rollback is a symlink flip plus a restart.
ExecStart=${CURRENT_LINK}/.venv/bin/uvicorn \\
  --factory svtplay_arr.app:create_app_from_env \\
  --host ${LISTEN_HOST} --port ${LISTEN_PORT}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
}

install_unit() {
    step "systemd unit"
    local desired
    desired=$(render_unit)

    if [[ -f $UNIT_PATH ]]; then
        if [[ $desired == "$(cat "$UNIT_PATH")" ]]; then
            log "${UNIT_PATH} is already correct"
            return 0
        fi
        log "backing up the current unit to ${UNIT_BACKUP}"
        run cp -p "$UNIT_PATH" "$UNIT_BACKUP"
        UNIT_BACKED_UP=true
    fi

    log "writing ${UNIT_PATH}"
    run install -d -m 0755 "$SVTPLAY_ARR_UNIT_DIR"
    if $DRY_RUN; then
        printf '    would: write the unit (ExecStart=%s/.venv/bin/uvicorn ... --host %s --port %s)\n' \
            "$CURRENT_LINK" "$LISTEN_HOST" "$LISTEN_PORT"
    else
        # Written beside and renamed into place: a unit file truncated
        # half-way through is one systemd will refuse, and this is the file
        # the service is about to be restarted from.
        render_unit >"${UNIT_PATH}.new"
        chmod 0644 "${UNIT_PATH}.new"
        mv -f "${UNIT_PATH}.new" "$UNIT_PATH"
    fi
    run "$SVTPLAY_ARR_SYSTEMCTL" daemon-reload
}

start_service() {
    step "Service"
    if [[ $MODE == install ]]; then
        log "enabling and starting ${SVTPLAY_ARR_UNIT_NAME}"
        run "$SVTPLAY_ARR_SYSTEMCTL" enable --now "$SVTPLAY_ARR_UNIT_NAME"
    else
        # enable is idempotent and cheap, and it repairs an installation whose
        # unit was never enabled.
        run "$SVTPLAY_ARR_SYSTEMCTL" enable "$SVTPLAY_ARR_UNIT_NAME"
        log "restarting ${SVTPLAY_ARR_UNIT_NAME}"
        run "$SVTPLAY_ARR_SYSTEMCTL" restart "$SVTPLAY_ARR_UNIT_NAME"
    fi
}

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

json_field() {
    printf '%s' "$1" | tr ',{}' '\n' |
        sed -n "s/^[[:space:]]*\"$2\"[[:space:]]*:[[:space:]]*//p" |
        head -n1 | tr -d '"' | tr -d '[:space:]'
}

fetch_health() {
    "$SVTPLAY_ARR_CURL" -fsS --max-time 5 "$SVTPLAY_ARR_HEALTH_URL" 2>/dev/null || true
}

# Prints the health JSON on stdout and returns 0 if the endpoint answered
# within the budget; returns 1 if it never did.
wait_for_health() {
    local deadline=$((SECONDS + SVTPLAY_ARR_HEALTH_TIMEOUT)) body=""
    while :; do
        body=$(fetch_health)
        if [[ -n $body && $body == *'"status"'* ]]; then
            printf '%s' "$body"
            return 0
        fi
        if [[ $SECONDS -ge $deadline ]]; then
            return 1
        fi
        sleep "$SVTPLAY_ARR_HEALTH_INTERVAL"
    done
}

# Did this upgrade break something *this service* is responsible for?
#
# Deliberately not a comparison of the top-level `status`, which is what this
# used to be. That field folds in the background checks on SVT and Sonarr --
# facts about the world outside this process, which the version being replaced
# may never have looked at at all. Upgrade svtplay-arr and Sonarr in the same
# window and the old binary reports `ok` because it never asked Sonarr a
# question, the new one correctly reports `degraded`, and a status comparison
# rolls back a perfectly good upgrade to "fix" a dependency that is merely
# restarting. Nothing but a few seconds of startup delay was standing between
# that and happening, and a rollback gate must not rest on a timing margin in
# another file.
#
# So this compares only the flat, process-level facts every version
# understands, and only a regression in one of them counts. A dependency being
# down is still reported by report_health, still turns /health red for a
# monitor, and is still in the body logged above -- it is simply not evidence
# that *this upgrade* failed.
#
# It also no longer requires the service to have been `ok` beforehand: a
# worker that dies across an upgrade of an already-degraded service is still a
# failed upgrade, and the old gate would have waved it through.
#
# Sets REGRESSION to what it found. Returns 1 (no regression) when there is no
# pre-upgrade body to compare against, which is the same "cannot tell, do not
# roll back" answer the old gate gave for an empty PRE_HEALTH_STATUS.
service_regressed() {
    local before=$1 after=$2 field was now
    REGRESSION=""
    [[ -n $before ]] || return 1
    for field in worker_alive same_filesystem; do
        was=$(json_field "$before" "$field")
        now=$(json_field "$after" "$field")
        if [[ $was == "true" && $now != "true" ]]; then
            REGRESSION="${field} was true before the upgrade and is '${now:-missing}' now"
            return 0
        fi
    done
    was=$(json_field "$before" mappings_degraded)
    now=$(json_field "$after" mappings_degraded)
    if [[ $was != "true" && $now == "true" ]]; then
        REGRESSION="mappings_degraded became true across the upgrade"
        return 0
    fi
    return 1
}

report_health() {
    local body=$1 status same_fs
    status=$(json_field "$body" status)
    same_fs=$(json_field "$body" same_filesystem)
    log "/health: ${body}"
    log "status: ${status:-unknown}"

    if [[ $same_fs == "false" ]] && $CONFIG_SEEDED; then
        # This run wrote config.yaml from the example, so incomplete_dir and
        # completed_dir are still /downloads/... and do not exist on this host
        # yet. same_filesystem is false for every fresh install, without
        # exception -- and a warning that fires every single time is a warning
        # nobody reads by the third install. The one below has to still mean
        # something when it fires, so it does not fire here.
        log ""
        log "same_filesystem is false, which is expected at this point: this run"
        log "wrote ${CONFIG_FILE} from the example, and the example's"
        log "incomplete_dir and completed_dir do not exist on this host yet."
        log "Setting them is the next step below."
        log ""
        log "Check /health again once you have set them and restarted. If it is"
        log "still false THEN it is the serious one, and it means Sonarr can import"
        log "a half-copied file as a permanent, corrupt library entry."
    elif [[ $same_fs == "false" ]]; then
        warn ""
        warn "same_filesystem is FALSE. incomplete_dir and completed_dir are not"
        warn "on one filesystem, so publishing a finished download is no longer an"
        warn "atomic rename -- it has degraded to copy-then-delete, and Sonarr can"
        warn "import a half-copied file as a permanent, corrupt library entry."
        warn "Nothing detects that after the fact."
        warn ""
        warn "Fix the mount layout before pointing Sonarr at this service."
        warn "See docs/installation.md, 'The mount layout'."
    fi
    if [[ $(json_field "$body" worker_alive) == "false" ]]; then
        warn "worker_alive is false: the download worker is not running. Check journalctl -u ${SVTPLAY_ARR_UNIT_NAME}."
    fi
    if [[ $(json_field "$body" mappings_degraded) == "true" ]]; then
        warn "mappings_degraded is true: mappings.yaml failed to load and the last good table is being served."
    fi
}

# `current` says which release is meant to be running. This says which one
# actually got started and answered. They differ exactly when a run was
# interrupted between the symlink flip and the restart -- in which case the
# next run has work to do, and must not report itself already up to date.
record_active_release() {
    local id=$1
    if $DRY_RUN; then
        printf '    would: record %s as the running release in %s\n' "$id" "$ACTIVE_MARKER"
        return 0
    fi
    printf '%s\n' "$id" >"${ACTIVE_MARKER}.new"
    mv -f "${ACTIVE_MARKER}.new" "$ACTIVE_MARKER"
}

recorded_active_release() {
    if [[ -f $ACTIVE_MARKER ]]; then
        head -n1 "$ACTIVE_MARKER"
    fi
}

service_is_active() {
    "$SVTPLAY_ARR_SYSTEMCTL" is-active --quiet "$SVTPLAY_ARR_UNIT_NAME" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

rollback() {
    local reason=$1
    step "Rolling back"
    err "$reason"
    log "restoring ${PREVIOUS_DESC}"

    if $ACTIVATED; then
        if [[ -n $PREVIOUS_TARGET ]]; then
            ln -sfn "$PREVIOUS_TARGET" "${CURRENT_LINK}.new"
            mv -Tf "${CURRENT_LINK}.new" "$CURRENT_LINK"
            log "current -> ${PREVIOUS_TARGET}"
        else
            rm -f "$CURRENT_LINK"
            log "removed the ${CURRENT_LINK} symlink (there was none before)"
        fi
    fi

    if $UNIT_BACKED_UP && [[ -f $UNIT_BACKUP ]]; then
        cp -p "$UNIT_BACKUP" "$UNIT_PATH"
        log "restored the previous unit file"
    fi
    "$SVTPLAY_ARR_SYSTEMCTL" daemon-reload || true
    "$SVTPLAY_ARR_SYSTEMCTL" restart "$SVTPLAY_ARR_UNIT_NAME" || true

    local body
    if body=$(wait_for_health); then
        if [[ -n $PREVIOUS_TARGET ]]; then
            record_active_release "$(basename "$PREVIOUS_TARGET")"
        else
            rm -f "$ACTIVE_MARKER"
        fi
        step "Rollback complete"
        err "The upgrade failed and was rolled back."
        log "running: ${PREVIOUS_DESC}"
        report_health "$body"
        err "Nothing about your configuration was changed. Look at"
        err "journalctl -u ${SVTPLAY_ARR_UNIT_NAME} for why the new version did not come up."
        exit 1
    fi

    step "Rollback did not restore service"
    err "The upgrade failed AND the previous version did not answer /health"
    err "within ${SVTPLAY_ARR_HEALTH_TIMEOUT}s after being restored."
    err "current -> $(readlink "$CURRENT_LINK" 2>/dev/null || echo "(absent)")"
    err "unit:    ${UNIT_PATH}"
    err "Your configuration was not touched. Check systemctl status ${SVTPLAY_ARR_UNIT_NAME}"
    err "and journalctl -u ${SVTPLAY_ARR_UNIT_NAME}."
    exit 1
}

# ---------------------------------------------------------------------------
# Closing report
# ---------------------------------------------------------------------------

next_steps() {
    local host
    host=$(hostname 2>/dev/null || printf 'this-host')
    step "Next steps"
    cat <<NEXT
    1. Open the configuration page and fill in Sonarr's URL and API key:

           http://${host}:${LISTEN_PORT}/config

       or edit ${CONFIG_FILE} directly. The four keys that must be set are
       sonarr_url, sonarr_api_key, incomplete_dir and completed_dir.
       Settings need a restart to apply:

           systemctl restart ${SVTPLAY_ARR_UNIT_NAME}

    2. Add at least one mapping (the same page, "Add mapping" or
       "Find mappings"). Sonarr tests an indexer with a parameterless search
       and rejects it outright if the feed comes back empty.

    3. In Sonarr, turn Rename Episodes OFF
       (Settings > Media Management > Episode Naming).

       This project is built and tested against renameEpisodes off: the
       release title svtplay-arr generates is also the output filename, so
       the two cannot diverge. With renaming on, nothing here has been
       tested.

    4. Add the indexer (Newznab) and download client (SABnzbd), and the
       remote path mapping -- docs/installation.md, "Connect Sonarr".

    Logs:    journalctl -u ${SVTPLAY_ARR_UNIT_NAME} -f
    Health:  curl ${SVTPLAY_ARR_HEALTH_URL}
NEXT
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

detect_mode() {
    if [[ -L $CURRENT_LINK || -d ${SVTPLAY_ARR_PREFIX}/.venv ]]; then
        MODE=upgrade
    else
        MODE=install
    fi
}

main() {
    parse_args "$@"

    step "svtplay-arr installer ${SCRIPT_VERSION}"
    if $DRY_RUN; then
        log "--dry-run: nothing will be changed"
    fi

    require_root
    report_platform

    detect_mode
    PREVIOUS_DESC=$(describe_current)
    if [[ -L $CURRENT_LINK ]]; then
        PREVIOUS_TARGET=$(readlink "$CURRENT_LINK")
    fi

    step "Mode"
    if [[ $MODE == install ]]; then
        log "no existing installation under ${SVTPLAY_ARR_PREFIX}: installing"
    else
        log "existing installation found: upgrading"
        log "currently installed: ${PREVIOUS_DESC}"
        if [[ ! -L $CURRENT_LINK ]]; then
            log "this installation predates the releases/ layout; it will be migrated"
            log "to ${RELEASES_DIR}/<commit> with a ${CURRENT_LINK} symlink."
            log "The existing checkout is left in place, untouched, as the rollback target."
        fi
        PRE_HEALTH_BODY=$(fetch_health)
        PRE_HEALTH_STATUS=$(json_field "$PRE_HEALTH_BODY" status)
        if [[ -n $PRE_HEALTH_STATUS ]]; then
            log "current /health status: ${PRE_HEALTH_STATUS}"
        fi
    fi

    # Taken before anything is fetched or built, and checked at the end.
    capture_config_fingerprint

    PHASE="checking host prerequisites"
    ensure_os_prereqs
    PHASE="installing uv"
    ensure_uv
    PHASE="creating the service account"
    ensure_account
    PHASE="creating directories"
    ensure_directories

    PHASE="resolving the release to install"
    step "Source"
    local sha id release_dir
    # Resolved out here, not inside resolve_ref, because a value set in a
    # command substitution never comes back.
    if [[ -z $SVTPLAY_ARR_REF ]]; then
        SVTPLAY_ARR_REF=$(latest_release_tag) || {
            err "no vN.N.N tag found in ${SVTPLAY_ARR_REPO}."
            err "That is what a fresh install targets by default, so that it never"
            err "lands on whatever the development branch happens to be."
            die "pass --ref main to install the development branch deliberately"
        }
        log "newest release tag: ${SVTPLAY_ARR_REF}"
    fi
    PHASE="resolving ${SVTPLAY_ARR_REF}"
    sha=$(resolve_ref) ||
        die "could not resolve ${SVTPLAY_ARR_REF} in ${SVTPLAY_ARR_REPO}; nothing has changed"
    id=$(release_id "$sha")
    release_dir="${RELEASES_DIR}/${id}"
    log "repository: ${SVTPLAY_ARR_REPO}"
    log "ref:        ${SVTPLAY_ARR_REF} -> ${sha}"
    log "release:    ${release_dir}"

    # The near-no-op second run.
    local unit_is_current=false
    if [[ -f $UNIT_PATH ]] && [[ $(render_unit) == "$(cat "$UNIT_PATH")" ]]; then
        unit_is_current=true
    fi
    # "Already up to date" has to mean the service is running this release,
    # not merely that a symlink names it. A run killed between the flip and
    # the restart leaves `current` pointing at a release the running process
    # has never been -- and saying "nothing to do" there leaves the host on
    # the old code indefinitely, with every later run agreeing.
    if [[ -f "${release_dir}/${RELEASE_STAMP}" ]] &&
        [[ -L $CURRENT_LINK ]] &&
        [[ $(readlink -f "$CURRENT_LINK") == "$(readlink -f "$release_dir")" ]] &&
        $unit_is_current &&
        [[ $(recorded_active_release) == "$id" ]] &&
        service_is_active; then
        step "Already up to date"
        log "${PREVIOUS_DESC} is installed and active; there is nothing to do"
        PHASE="checking health"
        local body
        if body=$(wait_for_health); then
            step "Health"
            report_health "$body"
        else
            warn "the service did not answer ${SVTPLAY_ARR_HEALTH_URL}; check systemctl status ${SVTPLAY_ARR_UNIT_NAME}"
        fi
        exit 0
    fi

    PHASE="fetching and building ${id}"
    step "Release ${id}"
    fetch_release "$release_dir" "$sha"

    PHASE="seeding configuration"
    if $DRY_RUN; then
        step "Configuration"
        printf '    would: create %s (0750) and write %s and %s from the release examples if absent (0640, %s:%s)\n' \
            "$SVTPLAY_ARR_CONFIG_DIR" "$CONFIG_FILE" "$MAPPINGS_FILE" \
            "$SVTPLAY_ARR_USER" "$SVTPLAY_ARR_GROUP"
    else
        seed_config "$release_dir"
    fi

    PHASE="setting ownership"
    set_ownership

    PHASE="installing the systemd unit"
    install_unit

    PHASE="activating release ${id}"
    step "Activating ${id}"
    activate_release "$release_dir"
    log "current -> ${release_dir}"

    PHASE="starting the service"
    start_service

    PHASE="checking health"
    step "Health"
    if $DRY_RUN; then
        printf '    would: poll %s for up to %ss and report what it says\n' \
            "$SVTPLAY_ARR_HEALTH_URL" "$SVTPLAY_ARR_HEALTH_TIMEOUT"
        record_active_release "$id"
    else
        local body
        if body=$(wait_for_health); then
            report_health "$body"
            local status
            status=$(json_field "$body" status)
            # An upgrade that turns a healthy service into an unhealthy one is
            # a failed upgrade. An upgrade of a service that was already
            # degraded (a mount is down, say) is not -- rolling back would not
            # fix it and would lose the upgrade.
            if [[ $MODE == upgrade ]] && service_regressed "$PRE_HEALTH_BODY" "$body"; then
                rollback "the service came back as '${status}' after the upgrade: ${REGRESSION}"
            fi
            # Only now is this release known to be the one actually serving.
            record_active_release "$id"
        else
            if [[ $MODE == upgrade ]]; then
                rollback "${SVTPLAY_ARR_HEALTH_URL} did not answer within ${SVTPLAY_ARR_HEALTH_TIMEOUT}s after the upgrade"
            fi
            err "the service did not answer ${SVTPLAY_ARR_HEALTH_URL} within ${SVTPLAY_ARR_HEALTH_TIMEOUT}s."
            err "It is installed and enabled, and your configuration is in place."
            err "Look at: systemctl status ${SVTPLAY_ARR_UNIT_NAME}"
            err "         journalctl -u ${SVTPLAY_ARR_UNIT_NAME} -n 50"
            exit 1
        fi
    fi

    PHASE="verifying configuration was untouched"
    if [[ $MODE == upgrade ]] && ! $DRY_RUN; then
        step "Configuration"
        assert_config_untouched
    fi

    PHASE="pruning old releases"
    step "Housekeeping"
    prune_releases
    log "keeping at most ${SVTPLAY_ARR_KEEP_RELEASES} releases in ${RELEASES_DIR}"

    step "Done"
    if $DRY_RUN; then
        log "nothing was changed."
        log "would go from: ${PREVIOUS_DESC}"
        log "to:            release ${id} (${sha})"
    elif [[ $MODE == upgrade ]]; then
        log "before: ${PREVIOUS_DESC}"
        log "after:  $(describe_current)"
    else
        log "installed: $(describe_current)"
    fi

    if [[ $MODE == install ]] || $DRY_RUN; then
        next_steps
    else
        log ""
        log "Settings changes need: systemctl restart ${SVTPLAY_ARR_UNIT_NAME}"
        log "Mapping changes do not."
    fi
}

# Sourcing the script defines its functions without running anything, which
# is how the test suite gets at render_unit with the real default paths.
if [[ ${BASH_SOURCE[0]} == "${0}" ]]; then
    main "$@"
fi
