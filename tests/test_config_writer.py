from pathlib import Path

import pytest
import yaml

from svtplay_arr import config as config_module
from svtplay_arr.config import (
    DANGEROUS_FIELDS,
    SECTION_ORDER,
    SETTING_FIELDS,
    ConfigError,
    SettingField,
    Settings,
    grouped_setting_fields,
    save_settings,
)
from svtplay_arr.yamlio import ConcurrentModification, read_with_mtime


def _write(tmp_path: Path, extra: str = "") -> Path:
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    p = tmp_path / "config.yaml"
    p.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        "sonarr_api_key: SECRET-KEY-VALUE\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n" + extra,
        encoding="utf-8",
    )
    return p


def _submit(tmp_path: Path, **over) -> dict:
    values = {
        "sonarr_url": "http://sonarr.test:8989",
        "incomplete_dir": f"{tmp_path}/i",
        "completed_dir": f"{tmp_path}/c",
        "air_date_tolerance_days": "1",
        "rss_window_days": "7",
        "max_concurrent_downloads": "1",
    }
    values.update(over)
    return values


def test_api_key_is_preserved_without_being_submitted(tmp_path: Path):
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(p, _submit(tmp_path), expected_mtime=mtime)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["sonarr_api_key"] == (
        "SECRET-KEY-VALUE"
    )


def test_unknown_keys_are_round_tripped(tmp_path: Path):
    p = _write(tmp_path, extra="future_option: keep-me\n")
    _, mtime = read_with_mtime(p)
    save_settings(p, _submit(tmp_path), expected_mtime=mtime)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["future_option"] == "keep-me"


def test_svt_ua_survives_a_settings_save_and_is_read_back(tmp_path: Path):
    # svt_ua is a real setting -- SvtClient sends it to SVT -- but it is
    # not on the settings form, so its whole round trip is: the writer
    # preserves it as a key it was not submitted (the same mechanism as
    # test_unknown_keys_are_round_tripped above), and Settings.load reads
    # it back. It is asserted specifically rather than left to the generic
    # unknown-key test, because a save that silently dropped it would
    # revert the operator's choice to the default with nothing failing --
    # which is the exact silent failure wiring svt_ua up was meant to end.
    p = _write(tmp_path, extra="svt_ua: some-other-client\n")
    _, mtime = read_with_mtime(p)
    save_settings(p, _submit(tmp_path), expected_mtime=mtime)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["svt_ua"] == (
        "some-other-client"
    )
    assert Settings.load(p).svt_ua == "some-other-client"


def test_saved_file_loads_with_the_real_settings_loader(tmp_path: Path):
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(p, _submit(tmp_path, rss_window_days="21"), expected_mtime=mtime)
    assert Settings.load(p).rss_window_days == 21


def test_nested_download_dirs_are_refused_and_file_untouched(tmp_path: Path):
    # The nested directory must exist. Without it dirs_share_filesystem()
    # fires first (it returns False for a directory that isn't there) and
    # ensure_download_dirs_are_disjoint() is never reached -- the test
    # passed for the wrong reason, and the guard against writing a config
    # whose startup sweep deletes the completed library was unprotected.
    p = _write(tmp_path)
    (tmp_path / "i" / "inside").mkdir(parents=True)
    before = p.read_bytes()
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError) as exc:
        save_settings(
            p,
            _submit(tmp_path, completed_dir=f"{tmp_path}/i/inside"),
            expected_mtime=mtime,
        )
    # Name the guard, so the test cannot go on passing via some other
    # ConfigError raised earlier for an unrelated reason.
    assert "separate directories" in str(exc.value)
    assert p.read_bytes() == before


def test_nonexistent_dir_is_refused(tmp_path: Path):
    p = _write(tmp_path)
    before = p.read_bytes()
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError):
        save_settings(
            p,
            _submit(tmp_path, completed_dir=f"{tmp_path}/does-not-exist"),
            expected_mtime=mtime,
        )
    assert p.read_bytes() == before


def test_non_integer_value_is_refused_with_a_readable_error(tmp_path: Path):
    p = _write(tmp_path)
    before = p.read_bytes()
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError) as exc:
        save_settings(
            p, _submit(tmp_path, rss_window_days="soon"), expected_mtime=mtime
        )
    assert "rss_window_days" in str(exc.value)
    assert p.read_bytes() == before


def test_stale_mtime_is_refused(tmp_path: Path):
    p = _write(tmp_path)
    with pytest.raises(ConcurrentModification):
        save_settings(p, _submit(tmp_path), expected_mtime=1.0)


def test_the_api_key_is_an_editable_secret_field_next_to_the_url():
    # Replaces test_setting_fields_never_expose_the_api_key, which asserted
    # the opposite. The exclusion was reversed on 2026-08-25 (see the
    # comment above SETTING_FIELDS): the key is configuration, and being the
    # one setting that needed SSH was the asymmetry the page exists to
    # remove.
    keys = [f.key for f in SETTING_FIELDS]
    assert "sonarr_api_key" in keys
    # Read together with sonarr_url, so they sit together on the form.
    assert keys.index("sonarr_api_key") == keys.index("sonarr_url") + 1
    field = next(f for f in SETTING_FIELDS if f.key == "sonarr_api_key")
    # A distinct kind, not "str": the template has to render it differently
    # and any future code has to be able to tell a secret apart.
    assert field.kind == "secret"


def test_the_api_key_help_says_the_value_reaches_the_browser():
    field = next(f for f in SETTING_FIELDS if f.key == "sonarr_api_key")
    help_text = field.help.lower()
    # The operator reading this at 23:00 must learn the actual consequence:
    # the value is in the page, so signing in is enough to read it.
    assert "page" in help_text
    assert "sign in" in help_text or "anyone who can" in help_text


def test_a_submitted_api_key_replaces_the_stored_one(tmp_path: Path):
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(
        p, _submit(tmp_path, sonarr_api_key="NEW-KEY-VALUE"), expected_mtime=mtime
    )
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["sonarr_api_key"] == (
        "NEW-KEY-VALUE"
    )
    assert Settings.load(p).sonarr_api_key == "NEW-KEY-VALUE"


def test_a_padded_api_key_is_stored_stripped(tmp_path: Path):
    # Same boundary as sonarr_url: a key with a stray newline goes into an
    # X-Api-Key header and every Sonarr call fails with a 401 that looks
    # like a revoked key.
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(
        p, _submit(tmp_path, sonarr_api_key="  NEW-KEY-VALUE\n "), expected_mtime=mtime
    )
    assert Settings.load(p).sonarr_api_key == "NEW-KEY-VALUE"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_a_blank_api_key_is_refused_and_the_file_untouched(tmp_path: Path, blank: str):
    # A blank key breaks every Sonarr call -- search, RSS poll, series
    # lookup -- while the service starts fine and /health says ok. Stripping
    # must compose with the blank check rather than turning a whitespace-only
    # submission into an empty value that passes "the key is present".
    p = _write(tmp_path)
    before = p.read_bytes()
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError) as exc:
        save_settings(
            p, _submit(tmp_path, sonarr_api_key=blank), expected_mtime=mtime
        )
    assert "sonarr_api_key" in str(exc.value)
    assert p.read_bytes() == before


def test_a_blank_api_key_is_refused_even_when_the_environment_has_one(
    tmp_path: Path, monkeypatch
):
    # The env fallback exists so a save can succeed on a deployment that
    # keeps the key out of the file entirely. It must not become a way for
    # an explicitly blanked field to be written to disk unnoticed.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-KEY-VALUE")
    p = _write(tmp_path)
    before = p.read_bytes()
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError) as exc:
        save_settings(p, _submit(tmp_path, sonarr_api_key="  "), expected_mtime=mtime)
    assert "sonarr_api_key" in str(exc.value)
    assert p.read_bytes() == before


def test_the_dangerous_fields_carry_their_consequence():
    tolerance = next(f for f in SETTING_FIELDS if f.key == "air_date_tolerance_days")
    assert "ambiguous" in tolerance.help.lower()
    completed = next(f for f in SETTING_FIELDS if f.key == "completed_dir")
    assert "filesystem" in completed.help.lower()


def test_every_dangerous_field_is_a_real_setting():
    # Guards the other direction of the DANGEROUS_FIELDS/SETTING_FIELDS
    # relationship: a typo'd or renamed key in DANGEROUS_FIELDS would
    # otherwise silently stop highlighting anything, rather than failing.
    keys = {f.key for f in SETTING_FIELDS}
    assert DANGEROUS_FIELDS <= keys
    assert DANGEROUS_FIELDS == {
        "air_date_tolerance_days", "incomplete_dir", "completed_dir",
    }


def test_grouped_setting_fields_covers_every_field_exactly_once():
    # The realistic regression: a field added to SETTING_FIELDS without
    # thinking about grouping must still show up on the page, exactly
    # once -- not vanish, and not appear twice because it matched more
    # than one bucket.
    groups = grouped_setting_fields()
    seen = [f.key for _, fields in groups for f in fields]
    assert sorted(seen) == sorted(f.key for f in SETTING_FIELDS)
    assert len(seen) == len(set(seen))


def test_grouped_setting_fields_uses_the_documented_order_and_membership():
    groups = dict(grouped_setting_fields())
    assert list(groups) == list(SECTION_ORDER)
    assert [f.key for f in groups["Connection"]] == [
        "sonarr_url", "sonarr_api_key",
    ]
    assert [f.key for f in groups["Storage"]] == [
        "incomplete_dir", "completed_dir",
    ]
    assert [f.key for f in groups["Matching"]] == [
        "air_date_tolerance_days", "rss_window_days",
    ]
    assert [f.key for f in groups["Downloads"]] == ["max_concurrent_downloads"]


def test_a_field_with_no_known_section_falls_back_instead_of_vanishing():
    # SettingField's `section` defaults to "" -- exactly what a field added
    # without thinking about grouping would have. It must still render
    # somewhere, in a visibly-named bucket, rather than being silently
    # dropped from the page.
    stray = SettingField("stray", "Stray", "str", "help text")
    assert stray.section == ""

    groups = grouped_setting_fields(SETTING_FIELDS + (stray,))
    by_name = dict(groups)
    assert "Other" in by_name
    assert [f.key for f in by_name["Other"]] == ["stray"]
    # It did not get folded into a real section either.
    for name in SECTION_ORDER:
        assert "stray" not in [f.key for f in by_name.get(name, [])]

    seen = [f.key for _, fields in groups for f in fields]
    assert sorted(seen) == sorted(f.key for f in SETTING_FIELDS) + ["stray"]


def test_grouping_does_not_change_setting_fields_order_or_the_yaml_header(
    tmp_path: Path,
):
    # Grouping is a presentation concern; SETTING_FIELDS itself drives the
    # config.yaml header comments (see save_settings) and must not be
    # reordered to build the section view -- reordering it would silently
    # change the header's key order and, via save_settings's merge, could
    # change round-trip semantics for a hand-edited file.
    keys = [f.key for f in SETTING_FIELDS]
    assert keys == [
        "sonarr_url", "sonarr_api_key", "incomplete_dir", "completed_dir",
        "air_date_tolerance_days", "rss_window_days",
        "max_concurrent_downloads",
    ]

    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(p, _submit(tmp_path), expected_mtime=mtime)
    text = p.read_text(encoding="utf-8")
    comment_lines = [
        line for line in text.splitlines() if line.startswith("# ") and ": " in line
    ]
    commented_keys = [line[2:].split(": ", 1)[0] for line in comment_lines]
    assert commented_keys == keys

    # And a full round-trip through Settings.load still works, with every
    # submitted value intact.
    reloaded = Settings.load(p)
    assert reloaded.sonarr_url == "http://sonarr.test:8989"
    assert reloaded.air_date_tolerance_days == 1
    assert reloaded.rss_window_days == 7
    assert reloaded.max_concurrent_downloads == 1


def _write_without_api_key(tmp_path: Path) -> Path:
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    p = tmp_path / "config.yaml"
    p.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    return p


def test_save_succeeds_when_api_key_only_in_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SONARR_API_KEY", "ENV-KEY-VALUE")
    p = _write_without_api_key(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(p, _submit(tmp_path), expected_mtime=mtime)  # must not raise


def test_env_only_api_key_is_not_written_to_the_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SONARR_API_KEY", "ENV-KEY-VALUE")
    p = _write_without_api_key(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(p, _submit(tmp_path), expected_mtime=mtime)
    assert "sonarr_api_key" not in yaml.safe_load(p.read_text(encoding="utf-8"))


def test_save_without_api_key_anywhere_is_still_refused(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SONARR_API_KEY", raising=False)
    p = _write_without_api_key(tmp_path)
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError) as exc:
        save_settings(p, _submit(tmp_path), expected_mtime=mtime)
    assert "sonarr_api_key" in str(exc.value)


def test_max_concurrent_downloads_floor_is_enforced(tmp_path: Path):
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError) as exc:
        save_settings(
            p,
            _submit(tmp_path, max_concurrent_downloads="-1"),
            expected_mtime=mtime,
        )
    assert "max_concurrent_downloads" in str(exc.value)


def test_rss_window_days_floor_is_enforced(tmp_path: Path):
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError) as exc:
        save_settings(
            p, _submit(tmp_path, rss_window_days="0"), expected_mtime=mtime
        )
    assert "rss_window_days" in str(exc.value)


def test_air_date_tolerance_days_floor_is_enforced(tmp_path: Path):
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError) as exc:
        save_settings(
            p,
            _submit(tmp_path, air_date_tolerance_days="-1"),
            expected_mtime=mtime,
        )
    assert "air_date_tolerance_days" in str(exc.value)


def test_air_date_tolerance_days_of_zero_is_allowed(tmp_path: Path):
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(
        p, _submit(tmp_path, air_date_tolerance_days="0"), expected_mtime=mtime
    )  # must not raise; the floor is inclusive


def test_header_survives_a_help_string_with_an_embedded_newline(
    tmp_path: Path, monkeypatch
):
    extra_field = SettingField("note", "Note", "str", "line one\nline two")
    monkeypatch.setattr(
        config_module, "SETTING_FIELDS", SETTING_FIELDS + (extra_field,)
    )
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(p, _submit(tmp_path), expected_mtime=mtime)
    # A newline embedded in a header line would otherwise emit an
    # uncommented continuation line that breaks YAML parsing.
    loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert loaded["sonarr_url"] == "http://sonarr.test:8989"


def test_padded_values_are_stored_stripped(tmp_path: Path):
    # httpx builds "  http://sonarr.test:8989/api/v3/series" into a
    # schemeless relative URL: the service starts, /health says ok, and
    # every Sonarr call raises SonarrApiError -- every search and every RSS
    # poll silently returns nothing. create_mapping already strips its
    # inputs; this is the same boundary.
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(
        p,
        _submit(
            tmp_path,
            sonarr_url="  http://sonarr.test:8989\n ",
            rss_window_days=" 21 ",
        ),
        expected_mtime=mtime,
    )
    loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert loaded["sonarr_url"] == "http://sonarr.test:8989"
    assert loaded["rss_window_days"] == 21


def test_padded_paths_are_stored_stripped(tmp_path: Path):
    p = _write(tmp_path)
    _, mtime = read_with_mtime(p)
    save_settings(
        p,
        _submit(tmp_path, incomplete_dir=f"  {tmp_path}/i  "),
        expected_mtime=mtime,
    )
    assert Settings.load(p).incomplete_dir == tmp_path / "i"


def test_a_whitespace_only_required_value_is_refused(tmp_path: Path):
    # Stripping must compose with the required-key check rather than
    # quietly turning a blank submission into an empty value that passes
    # "the key is present".
    p = _write(tmp_path)
    before = p.read_bytes()
    _, mtime = read_with_mtime(p)
    with pytest.raises(ConfigError) as exc:
        save_settings(p, _submit(tmp_path, sonarr_url="   "), expected_mtime=mtime)
    assert "sonarr_url" in str(exc.value)
    assert p.read_bytes() == before
