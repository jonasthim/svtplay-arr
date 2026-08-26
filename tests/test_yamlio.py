from pathlib import Path

import pytest
import yaml

from svtplay_arr.yamlio import (
    ConcurrentModification, atomic_write_yaml, read_with_mtime,
)


def test_read_with_mtime_returns_data_and_mtime(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    data, mtime = read_with_mtime(p)
    assert data == {"a": 1}
    assert mtime == p.stat().st_mtime


def test_read_with_mtime_on_missing_file_is_empty(tmp_path: Path):
    data, mtime = read_with_mtime(tmp_path / "nope.yaml")
    assert data == {}
    assert mtime is None


def test_write_creates_file_with_mode_640(tmp_path: Path):
    p = tmp_path / "c.yaml"
    atomic_write_yaml(p, {"a": 1}, header=["managed"])
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"a": 1}
    assert p.stat().st_mode & 0o777 == 0o640


def test_write_emits_the_header_as_comments(tmp_path: Path):
    p = tmp_path / "c.yaml"
    atomic_write_yaml(p, {"a": 1}, header=["managed by svtplay-arr", "do not"])
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# managed by svtplay-arr\n# do not\n")


def test_write_keeps_previous_contents_as_bak(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("old: true\n", encoding="utf-8")
    atomic_write_yaml(p, {"new": True}, header=[])
    assert yaml.safe_load(p.with_suffix(".yaml.bak").read_text(encoding="utf-8")) == {
        "old": True
    }


def test_write_leaves_no_temp_files_behind(tmp_path: Path):
    p = tmp_path / "c.yaml"
    atomic_write_yaml(p, {"a": 1}, header=[])
    assert [f.name for f in tmp_path.iterdir() if f.name.startswith(".")] == []


def test_stale_mtime_is_refused_and_file_untouched(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(ConcurrentModification):
        atomic_write_yaml(p, {"a": 2}, header=[], expected_mtime=1.0)
    assert p.read_bytes() == before


def test_matching_mtime_is_accepted(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    _, mtime = read_with_mtime(p)
    atomic_write_yaml(p, {"a": 2}, header=[], expected_mtime=mtime)
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"a": 2}


def test_expected_mtime_none_on_a_missing_file_is_allowed(tmp_path: Path):
    p = tmp_path / "new.yaml"
    atomic_write_yaml(p, {"a": 1}, header=[], expected_mtime=None)
    assert p.exists()


def test_unicode_survives_the_round_trip(tmp_path: Path):
    p = tmp_path / "c.yaml"
    title = "Gift vid första ögonkastet"
    atomic_write_yaml(p, {"t": title}, header=[])
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["t"] == title


def test_write_failure_leaves_original_file_intact(tmp_path: Path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    before = p.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr("svtplay_arr.yamlio.os.fsync", boom)

    with pytest.raises(OSError):
        atomic_write_yaml(p, {"a": 2}, header=[])

    assert p.exists()
    assert p.read_bytes() == before


def test_write_failure_leaves_no_temp_files_behind(tmp_path: Path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr("svtplay_arr.yamlio.os.fsync", boom)

    with pytest.raises(OSError):
        atomic_write_yaml(p, {"a": 2}, header=[])

    assert [f.name for f in tmp_path.iterdir() if f.name.startswith(".")] == []
