# tests/test_config.py
import re
from pathlib import Path

import httpx
import pytest

import yaml

from svtplay_arr.config import (
    SETTING_FIELDS,
    ConfigError,
    Settings,
    effective_setting_values,
    setting_defaults,
)
from svtplay_arr.svt.client import SvtClient


def test_settings_load_from_yaml(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.example.internal:8989\n"
        "sonarr_api_key: abc123\n"
        "incomplete_dir: /downloads/incomplete\n"
        "completed_dir: /downloads/completed\n",
        encoding="utf-8",
    )
    s = Settings.load(cfg)
    assert s.sonarr_url == "http://sonarr.example.internal:8989"
    assert s.air_date_tolerance_days == 1
    assert s.max_concurrent_downloads == 1


def test_missing_required_key_raises_config_error_naming_file_and_key(
    tmp_path: Path,
):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.example.internal:8989\n"
        "incomplete_dir: /downloads/incomplete\n"
        "completed_dir: /downloads/completed\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        Settings.load(cfg)
    assert str(cfg) in str(exc_info.value)
    assert "sonarr_api_key" in str(exc_info.value)


def test_same_dataset_check_rejects_split_paths(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        f"incomplete_dir: {tmp_path}/a\ncompleted_dir: /proc\n",
        encoding="utf-8",
    )
    s = Settings.load(cfg)
    assert s.dirs_share_filesystem() is False


# --- incomplete/ and completed/ must not contain one another --------------
#
# Worker.sweep_incomplete() rmtree's everything inside incomplete_dir on
# every startup. If completed_dir is the same directory, or lives inside it,
# that sweep deletes finished episodes -- silently, and on a schedule. The
# deployment docs previously described a mount layout that nested the two,
# so this is a configuration the operator could plausibly arrive at.


def _dirs(tmp_path: Path, incomplete: str, completed: str) -> Settings:
    return Settings(
        sonarr_url="http://sonarr.test",
        sonarr_api_key="k",
        incomplete_dir=tmp_path / incomplete,
        completed_dir=tmp_path / completed,
    )


def test_sibling_download_dirs_are_accepted(tmp_path: Path):
    _dirs(tmp_path, "incomplete", "completed").ensure_download_dirs_are_disjoint()


def test_identical_download_dirs_are_rejected(tmp_path: Path):
    with pytest.raises(ConfigError) as exc:
        _dirs(tmp_path, "downloads", "downloads").ensure_download_dirs_are_disjoint()
    assert "incomplete_dir" in str(exc.value)


def test_completed_inside_incomplete_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError):
        _dirs(
            tmp_path, "downloads", "downloads/completed"
        ).ensure_download_dirs_are_disjoint()


def test_incomplete_inside_completed_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError):
        _dirs(
            tmp_path, "downloads/incomplete", "downloads"
        ).ensure_download_dirs_are_disjoint()


def test_rss_window_days_defaults_to_seven(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    assert Settings.load(cfg).rss_window_days == 7


def test_rss_window_days_is_configurable(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n"
        "rss_window_days: 21\n",
        encoding="utf-8",
    )
    assert Settings.load(cfg).rss_window_days == 21


def _minimal_config(tmp_path: Path) -> Path:
    """config.yaml with only the four required keys -- the deployed shape."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    return cfg


def test_the_rendered_defaults_are_the_ones_the_loader_actually_uses(
    tmp_path: Path,
):
    # The single-source rule, asserted as a relationship rather than as a
    # list of numbers: whatever `setting_defaults` reports for a key must
    # be what `Settings.load` produces from a file that omits it. A second
    # hardcoded copy of any default -- in the loader, in the config page,
    # or here -- fails this the moment the two disagree.
    cfg = _minimal_config(tmp_path)
    loaded = Settings.load(cfg)
    defaults = setting_defaults()
    assert defaults, "no editable setting has a default; the helper is vacuous"
    for key, default in defaults.items():
        assert str(getattr(loaded, key)) == default


def test_effective_values_fill_in_the_defaults_for_absent_keys(tmp_path: Path):
    raw = yaml.safe_load(_minimal_config(tmp_path).read_text(encoding="utf-8"))
    values = effective_setting_values(raw)

    assert set(values) == {f.key for f in SETTING_FIELDS}
    for key, default in setting_defaults().items():
        assert key not in raw, f"{key} is in the minimal file; test is vacuous"
        assert values[key] == default


def test_effective_values_prefer_what_the_file_says(tmp_path: Path):
    raw = yaml.safe_load(_minimal_config(tmp_path).read_text(encoding="utf-8"))
    raw["rss_window_days"] = 21
    assert effective_setting_values(raw)["rss_window_days"] == "21"


def test_a_required_setting_has_no_default_to_fall_back_on():
    # sonarr_api_key, sonarr_url and the two directories are required with
    # no fallback: inventing one for them would write a bogus value to disk
    # (and, for the API key, silently paper over an env-only deployment).
    defaults = setting_defaults()
    for key in ("sonarr_url", "sonarr_api_key", "incomplete_dir", "completed_dir"):
        assert key not in defaults
    assert effective_setting_values({})["sonarr_api_key"] == ""


# --- svt_ua ---------------------------------------------------------------
#
# `Settings` carried this field with a default from the start, and
# `SvtClient` has always taken it from `Settings` (app.py and mappings.py
# both construct `SvtClient(http, settings.svt_ua)`) -- but `Settings.load`
# never read it back out of the file, so a `svt_ua:` line in config.yaml
# looked like it worked and changed nothing. Wired up rather than deleted:
# it is the client identifier SVT's GraphQL API is sent, the one knob that
# lets an operator respond to SVT rejecting it without editing code, and
# every part of the path except this one `raw.get` already existed. The
# tests below pin the whole path, because a setting that is read but never
# reaches anything is the same silent failure one layer along.


def test_svt_ua_is_read_from_the_file(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        "incomplete_dir: /a\ncompleted_dir: /b\n"
        "svt_ua: some-other-client\n",
        encoding="utf-8",
    )
    assert Settings.load(cfg).svt_ua == "some-other-client"


def test_svt_ua_falls_back_to_the_dataclass_default(tmp_path: Path):
    # Same rule as the int settings above it: the fallback is
    # `Settings.svt_ua` itself, never a second literal copy of that string
    # inside `load`.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        "incomplete_dir: /a\ncompleted_dir: /b\n",
        encoding="utf-8",
    )
    assert Settings.load(cfg).svt_ua == Settings.svt_ua


async def test_svt_ua_from_the_file_reaches_svt(tmp_path: Path):
    # The assertion that makes this setting real rather than merely
    # parsed: a value written in config.yaml ends up as the `ua` query
    # parameter on the request SVT actually receives, rather than stopping
    # at a `Settings` attribute nothing reads. This covers config.yaml ->
    # load -> SvtClient -> the wire; the joint between `Settings` and the
    # client the *service* builds is covered separately, by
    # test_the_configured_svt_ua_reaches_the_services_svt_client in
    # tests/test_app.py -- dropping the argument at that call site leaves
    # this test perfectly green.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        "incomplete_dir: /a\ncompleted_dir: /b\n"
        "svt_ua: some-other-client\n",
        encoding="utf-8",
    )
    settings = Settings.load(cfg)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.url.params.get("ua")
        alias = re.match(
            r"query\(\$q: String!\)\{(\w+):", request.url.params["query"]
        ).group(1)
        return httpx.Response(200, json={"data": {alias: []}})

    client = SvtClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), settings.svt_ua
    )
    await client.search_series("anything")

    assert seen["ua"] == "some-other-client"


# --- svt_canary_interval_minutes -------------------------------------------
#
# Same shape as svt_ua above, and for the same reason: an escape hatch read
# from the file but deliberately kept off the settings form. It is how an
# operator on a metered or rate-limited connection reduces load on SVT's
# unofficial API without a code change. A setting that is read but never
# reaches anything is the same silent failure one layer along, so the whole
# path is pinned -- including the floor, since config.yaml is hand-editable
# and a 0 would otherwise become a loop firing at SVT as fast as it answers.


def test_the_canary_interval_is_read_from_the_file(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        "incomplete_dir: /a\ncompleted_dir: /b\n"
        "svt_canary_interval_minutes: 180\n",
        encoding="utf-8",
    )
    assert Settings.load(cfg).svt_canary_interval_minutes == 180


def test_the_canary_interval_falls_back_to_the_dataclass_default(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        "incomplete_dir: /a\ncompleted_dir: /b\n",
        encoding="utf-8",
    )
    loaded = Settings.load(cfg)
    assert loaded.svt_canary_interval_minutes == Settings.svt_canary_interval_minutes
    assert loaded.svt_canary_interval_minutes == 60  # roughly hourly


def test_the_canary_interval_is_not_on_the_settings_form(tmp_path: Path):
    # Like svt_ua and the two paths above it: read from the file, not one
    # click away. Turning it down makes the check noisier for no benefit --
    # the failure it detects lasts until a human fixes it.
    assert "svt_canary_interval_minutes" not in {f.key for f in SETTING_FIELDS}
    assert "svt_canary_interval_minutes" not in setting_defaults()


def test_a_zero_canary_interval_cannot_become_a_busy_loop(tmp_path: Path):
    # config.yaml is hand-editable, so the floor lives where the canary is
    # constructed rather than in the loader (which must keep round-tripping
    # whatever is in the file).
    from svtplay_arr.app import create_app

    (tmp_path / "i").mkdir()
    (tmp_path / "c").mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://x\nsonarr_api_key: k\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n"
        f"db_path: {tmp_path}/jobs.db\nmappings_file: {tmp_path}/mappings.yaml\n"
        "svt_canary_interval_minutes: 0\n",
        encoding="utf-8",
    )
    settings = Settings.load(cfg)
    assert settings.svt_canary_interval_minutes == 0
    app = create_app(settings)
    assert app.state.svt_canary._interval >= 60.0
