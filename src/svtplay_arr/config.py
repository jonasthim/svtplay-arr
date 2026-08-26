from dataclasses import MISSING, dataclass, fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
import os
import yaml

from svtplay_arr.yamlio import atomic_write_yaml, read_with_mtime

_REQUIRED_KEYS = ("sonarr_url", "sonarr_api_key", "incomplete_dir", "completed_dir")


class ConfigError(RuntimeError):
    """The config file is missing a value Settings needs to start."""


@dataclass
class Settings:
    """Everything the service reads from config.yaml.

    Deliberately carries no listen host/port. The systemd unit passes
    --host/--port straight to uvicorn, so settings of that name would never
    have been used to bind anything; they existed only to build the Newznab
    download link, which is now derived from the incoming request instead
    (see api/newznab.py). Keeping them would re-offer the exact footgun that
    made every grab fail: a `listen_host: "0.0.0.0"` line, copied from the
    deployment docs, silently became the host Sonarr was told to fetch the
    .nzb from.
    """

    sonarr_url: str
    sonarr_api_key: str
    incomplete_dir: Path
    completed_dir: Path
    mappings_file: Path = Path("/etc/svtplay-arr/mappings.yaml")
    db_path: Path = Path("/var/lib/svtplay-arr/jobs.db")
    air_date_tolerance_days: int = 1
    # How far back the RSS feed looks. Sonarr polls this every few
    # minutes and each candidate costs an HLS manifest fetch, so the
    # window is what bounds load on SVT's unofficial API.
    rss_window_days: int = 7
    max_concurrent_downloads: int = 1
    # The client identifier SvtClient sends SVT's GraphQL API as its `ua`
    # query parameter. Read from config.yaml by `load` below, but not
    # editable from the configuration page: it exists so that SVT
    # rejecting this string is something an operator can work around
    # without a code change, not as a setting anyone should be adjusting.
    svt_ua: str = "svtplaywebb-play-render-prod-client"
    # Only set when constructed via Settings.load(): the config page needs
    # to know which file it came from. Directly-constructed Settings (as in
    # most tests) leave this None.
    config_path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "Settings":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key in ("sonarr_api_key",):
            env = os.environ.get(key.upper())
            if env:
                raw[key] = env
        # A missing file or malformed YAML already names the path (via
        # FileNotFoundError / yaml.YAMLError); a missing required key would
        # otherwise surface as a bare KeyError with no hint that this is a
        # config problem or which file to fix.
        missing = [key for key in _REQUIRED_KEYS if key not in raw]
        if missing:
            raise ConfigError(
                f"{path}: missing required config key(s): {', '.join(missing)}"
            )
        return cls(
            sonarr_url=raw["sonarr_url"],
            sonarr_api_key=raw["sonarr_api_key"],
            incomplete_dir=Path(raw["incomplete_dir"]),
            completed_dir=Path(raw["completed_dir"]),
            mappings_file=Path(raw.get("mappings_file", cls.mappings_file)),
            db_path=Path(raw.get("db_path", cls.db_path)),
            # `cls.<name>`, never a literal: these repeated the dataclass
            # defaults as a second copy, so 7 above and 7 here could drift
            # apart with nothing failing. The config page renders these same
            # defaults for keys the file omits (see setting_defaults), which
            # makes a third copy exactly the wrong direction to go.
            air_date_tolerance_days=int(
                raw.get("air_date_tolerance_days", cls.air_date_tolerance_days)
            ),
            rss_window_days=int(raw.get("rss_window_days", cls.rss_window_days)),
            max_concurrent_downloads=int(
                raw.get("max_concurrent_downloads", cls.max_concurrent_downloads)
            ),
            # Not on the settings form (like mappings_file and db_path
            # above), but read here all the same: it was a field with a
            # default that `load` never looked for, so a `svt_ua:` line in
            # the file looked like it worked and changed nothing. It is
            # deliberately not offered on the page either -- a wrong value
            # here breaks every SVT call silently, which is the
            # dangerous-field class, and an escape hatch for SVT rejecting
            # the default identifier does not need to be one click away.
            svt_ua=str(raw.get("svt_ua") or cls.svt_ua),
            config_path=path,
        )

    def ensure_download_dirs_are_disjoint(self) -> None:
        """Refuse to start if either download dir contains the other.

        `Worker.sweep_incomplete()` rmtree's everything inside
        `incomplete_dir` on every startup, so that a partial left by a crash
        can never be mistaken for a fresh download. If `completed_dir` is the
        same directory, or sits inside it, that sweep deletes finished
        episodes -- silently, and once per restart. There is no safe
        degraded mode for that, so it is a startup failure rather than a
        `/health` warning.

        This is not hypothetical: deploy/README.md used to describe a mount
        layout that nested the two.
        """
        incomplete = self.incomplete_dir.expanduser().resolve()
        completed = self.completed_dir.expanduser().resolve()
        if incomplete.is_relative_to(completed) or completed.is_relative_to(incomplete):
            raise ConfigError(
                "incomplete_dir and completed_dir must be separate directories, "
                "neither containing the other "
                f"(got incomplete_dir={incomplete}, completed_dir={completed}); "
                "startup clears incomplete_dir, which would delete completed "
                "episodes"
            )

    def dirs_share_filesystem(self) -> bool:
        """Atomic publish requires one filesystem. Guard against a split mount."""
        try:
            return (
                self.incomplete_dir.stat().st_dev == self.completed_dir.stat().st_dev
            )
        except FileNotFoundError:
            return False


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    kind: str  # "path" | "int" | "str" | "secret"
    help: str
    # Which form section this field belongs to. Defaults to "" rather than
    # being required, so a field added without one -- the realistic mistake
    # -- fails soft: grouped_setting_fields() below puts it in a visible
    # fallback bucket instead of dropping it from the page.
    section: str = ""


# Drives both the form and the comments written into config.yaml, so the
# explanation a user reads and the one left in the file cannot drift.
#
# sonarr_api_key was deliberately excluded here until 2026-08-25, on the
# reasoning that a secret which never reaches the browser cannot leak from
# it. That exclusion was removed on purpose: it made the API key the one
# setting that required SSH, which is the exact asymmetry this page exists
# to remove. What protects the value now is network isolation -- or the SSO
# reverse proxy in front of the site, if it is published at all
# (deploy/README.md) -- and the 0640 config file, not the page's
# own silence. The value is rendered into the HTML whatever the field's
# Show/Hide button is currently set to, so anyone who can authenticate to
# the site can read it, and it lives in browser cache, history and
# screenshots. That is the accepted trade; the field's own help text says
# so to the operator.
SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField(
        "sonarr_url", "Sonarr URL", "str",
        "Where Sonarr lives. Restart required to apply.",
        section="Connection",
    ),
    SettingField(
        "sonarr_api_key", "Sonarr API key", "secret",
        "From Sonarr's Settings > General. The value is rendered into this "
        "page, so anyone who can sign in to this site can read it -- it is "
        "in the page source while the field is still showing dots, and the "
        "Show button only changes what is on your screen. It is also in "
        "your browser's cache, your history, and any screenshot of this "
        "page. Restart required to apply.",
        section="Connection",
    ),
    SettingField(
        "incomplete_dir", "Incomplete directory", "path",
        "Downloads in progress. Must be on the same filesystem as the "
        "completed directory, or publishing stops being atomic and a "
        "half-copied file can be imported. Restart required to apply.",
        section="Storage",
    ),
    SettingField(
        "completed_dir", "Completed directory", "path",
        "Finished files, where Sonarr imports from. Must be on the same "
        "filesystem as the incomplete directory and must not contain it. "
        "Restart required to apply.",
        section="Storage",
    ),
    SettingField(
        "air_date_tolerance_days", "Air date tolerance (days)", "int",
        "How far an SVT publication date may sit from Sonarr's air date. "
        "Widening this makes episodes that share an air date ambiguous with "
        "their neighbours, and ambiguity makes the resolver return nothing "
        "at all. Restart required to apply.",
        section="Matching",
    ),
    SettingField(
        "rss_window_days", "RSS window (days)", "int",
        "How far back the RSS feed looks. Each candidate costs SVT requests "
        "on every poll. Restart required to apply.",
        section="Matching",
    ),
    SettingField(
        "max_concurrent_downloads", "Concurrent downloads", "int",
        "Parallel downloads. Restart required to apply.",
        section="Downloads",
    ),
)

# The order sections are grouped and rendered in -- independent of
# SETTING_FIELDS's own order (which drives config.yaml's comment header and
# must not be reshuffled to change this). Currently the two orders happen
# to coincide because SETTING_FIELDS was already laid out this way; that is
# a coincidence grouped_setting_fields() does not depend on.
SECTION_ORDER: tuple[str, ...] = ("Connection", "Storage", "Matching", "Downloads")

# A field whose `.section` isn't in SECTION_ORDER (unset, or a typo) lands
# here instead of vanishing from the page.
_FALLBACK_SECTION = "Other"

# Fields whose consequences are silent and severe enough to need a visible
# warning treatment on the form, beyond the help text every field already
# has. See SETTING_FIELDS above for what each one actually says.
DANGEROUS_FIELDS: frozenset[str] = frozenset(
    {"air_date_tolerance_days", "incomplete_dir", "completed_dir"}
)


def grouped_setting_fields(
    fields: tuple[SettingField, ...] = SETTING_FIELDS,
) -> list[tuple[str, list[SettingField]]]:
    """Group `fields` by `.section`, in `SECTION_ORDER`.

    Every field in `fields` appears in exactly one returned group -- a
    field whose section is missing or unrecognised is not dropped, it is
    collected into a trailing "Other" group instead, so a future field
    added without a section shows up as a visibly mis-filed setting rather
    than one that silently never renders.
    """
    buckets: dict[str, list[SettingField]] = {name: [] for name in SECTION_ORDER}
    fallback: list[SettingField] = []
    for f in fields:
        if f.section in buckets:
            buckets[f.section].append(f)
        else:
            fallback.append(f)
    groups = [(name, buckets[name]) for name in SECTION_ORDER if buckets[name]]
    if fallback:
        groups.append((_FALLBACK_SECTION, fallback))
    return groups

def setting_defaults() -> dict[str, str]:
    """Each editable setting's default, as the string a form would carry.

    Read straight off `Settings`' dataclass fields, so this is not a second
    copy of any default -- it is the same one `Settings.load` falls back to,
    which is the whole point: a key absent from config.yaml is not "unset",
    it is running at this value, and the page has to say so.

    Only `SETTING_FIELDS` keys appear, and only those that actually have a
    default: `sonarr_url`, `sonarr_api_key` and the two directories are
    required with no fallback, so they are absent here and a caller renders
    them empty -- which for the API key specifically is what the
    "the file has no key of its own" warning on the form keys off.
    """
    declared = {
        f.name: f.default
        for f in dataclass_fields(Settings)
        if f.default is not MISSING and f.default is not None
    }
    return {
        f.key: str(declared[f.key]) for f in SETTING_FIELDS if f.key in declared
    }


def effective_setting_values(raw: dict) -> dict[str, str]:
    """What the service would actually use for each editable setting.

    `raw` is config.yaml as parsed. A key the file does not contain does
    not render blank: it renders the default the service is running on.

    This exists because rendering only what the file happened to contain
    made every settings save on the live deployment impossible. The
    deployed config.yaml carries just the four required keys, so the three
    int fields rendered empty, the browser posted "" for each, and
    `save_settings` refused the lot with "'' is not a whole number" --
    including a save where the operator had only edited an unrelated field.

    Values are stringified for the form; `None` (a key present but empty in
    YAML) is treated as absent, since that is what `int(None)` would be
    anyway.
    """
    defaults = setting_defaults()
    return {
        f.key: str(
            raw[f.key]
            if raw.get(f.key) is not None
            else defaults.get(f.key, "")
        )
        for f in SETTING_FIELDS
    }


_INT_KEYS = {f.key for f in SETTING_FIELDS if f.kind == "int"}

# Floors below which the value parses fine but breaks something downstream:
# Worker(...) does asyncio.Semaphore(max_concurrent_downloads), which raises
# ValueError for anything below 1; a negative rss_window_days or a negative
# air_date_tolerance_days is nonsensical for the same reason a save must
# never write a value that keeps the service from booting.
_INT_FLOORS = {
    "max_concurrent_downloads": 1,
    "rss_window_days": 1,
    "air_date_tolerance_days": 0,
}


def save_settings(
    path: Path, submitted: dict[str, str], *, expected_mtime: float | None
) -> None:
    """Validate and write config.yaml.

    Refuses anything that would not start the service, so a save can never
    be the reason svtplay-arr fails to boot. The file is untouched on
    refusal.
    """
    existing, _ = read_with_mtime(path)
    merged = dict(existing)  # round-trips unknown keys and the API key
    for field in SETTING_FIELDS:
        if field.key not in submitted:
            continue
        # Strip at the boundary, the way create_mapping already strips the
        # values a human types into its form. A leading space on
        # sonarr_url makes httpx build "%20%20http://sonarr.test:8989/..."
        # -- a schemeless relative URL. The service starts, /health says
        # ok, and every Sonarr call raises SonarrApiError: every search and
        # every RSS poll silently returns nothing. A path with trailing
        # whitespace is the same class of damage one directory down.
        raw = str(submitted[field.key]).strip()
        if field.key in _INT_KEYS:
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"{field.key}: {raw!r} is not a whole number"
                ) from exc
            floor = _INT_FLOORS.get(field.key)
            if floor is not None and value < floor:
                raise ConfigError(
                    f"{field.key}: {value} is below the minimum of {floor}"
                )
            merged[field.key] = value
        else:
            merged[field.key] = raw

    # sonarr_api_key is on the form now, but it is still optional there: a
    # submission that omits it leaves whatever is already on disk (the loop
    # above skips absent keys), and deployments that set it purely via
    # $SONARR_API_KEY (see deploy/svtplay-arr.service) have no on-disk value
    # at all. Settings.load() covers that case with its own env override, so
    # a save must accept the same deployment rather than refusing with an
    # error about a field the operator left alone. The env value is used
    # only to satisfy validation and build the candidate below; it is never
    # written into `merged`, so the secret never moves from the environment
    # onto disk on its own.
    api_key = merged.get("sonarr_api_key") or os.environ.get("SONARR_API_KEY")

    missing = [
        key
        for key in _REQUIRED_KEYS
        if key not in merged and not (key == "sonarr_api_key" and api_key)
    ]
    if missing:
        raise ConfigError(
            f"{path}: missing required config key(s): {', '.join(missing)}"
        )

    # Stripping above turns a whitespace-only submission into "", which the
    # `missing` check above would wave through: the key *is* present. An
    # empty sonarr_url or incomplete_dir starts a service that cannot reach
    # Sonarr or cannot write anywhere sensible, so it is refused here
    # rather than silently written.
    #
    # This is also what stops a blanked sonarr_api_key field being written:
    # the `api_key` fallback above would happily satisfy `missing` from the
    # environment, leaving an empty key on disk that breaks every Sonarr
    # call the moment the environment variable goes away. The check reads
    # `merged`, not `api_key`, precisely so an explicitly-submitted blank is
    # caught rather than papered over.
    blank = [
        key
        for key in _REQUIRED_KEYS
        if isinstance(merged.get(key), str) and not merged[key].strip()
    ]
    if blank:
        raise ConfigError(
            f"{path}: required config key(s) must not be blank: "
            f"{', '.join(blank)}"
        )

    candidate = Settings(
        sonarr_url=merged["sonarr_url"],
        sonarr_api_key=api_key,
        incomplete_dir=Path(merged["incomplete_dir"]),
        completed_dir=Path(merged["completed_dir"]),
    )
    # The service's own checks, not a parallel copy.
    candidate.ensure_download_dirs_are_disjoint()
    if not candidate.dirs_share_filesystem():
        raise ConfigError(
            "incomplete_dir and completed_dir must both exist and be on the "
            "same filesystem; publishing is an atomic rename, which silently "
            "degrades to copy-then-delete across filesystems"
        )

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = [f"managed by svtplay-arr; last written {stamp}", ""]
    # Each SETTING_FIELDS entry becomes exactly one "# ..." comment line in
    # atomic_write_yaml's header. A help string containing a newline would
    # emit an uncommented continuation line that yaml.safe_load then chokes
    # on, so any embedded newline is flattened rather than trusted.
    header += [
        f"{f.key}: {f.help}".replace("\n", " ") for f in SETTING_FIELDS
    ]
    atomic_write_yaml(path, merged, header=header, expected_mtime=expected_mtime)
