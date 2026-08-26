from pathlib import Path

import pytest

from svtplay_arr.mappings import (
    MappingError, MappingTable, add_mapping, remove_mapping,
)
from svtplay_arr.yamlio import read_with_mtime

TITLE = "Gift vid första ögonkastet"


def _add(p: Path, tvdb_id=288649, title=TITLE):
    _, mtime = read_with_mtime(p)
    add_mapping(
        p,
        tvdb_id=tvdb_id,
        svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet",
        series_title=title,
        expected_mtime=mtime,
    )


def test_add_writes_a_row_the_loader_accepts(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    m = MappingTable.load(p).for_tvdb(288649)
    assert m.svt_series_id == "jpmQD3q"
    assert m.svt_slug == "gift-vid-forsta-ogonkastet"


def test_series_title_survives_byte_identical(tmp_path: Path):
    # This string becomes the permanent filename in /mnt/tv.
    p = tmp_path / "mappings.yaml"
    _add(p)
    assert MappingTable.load(p).for_tvdb(288649).series_title == TITLE


def test_duplicate_tvdb_id_is_refused_naming_the_existing_row(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    with pytest.raises(MappingError) as exc:
        _add(p, title="Something Else")
    assert TITLE in str(exc.value)


def test_add_preserves_existing_rows(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    _add(p, tvdb_id=999, title="Vem vet mest?")
    assert {m.tvdb_id for m in MappingTable.load(p).all()} == {288649, 999}


def test_remove_deletes_only_the_named_row(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    _add(p, tvdb_id=999, title="Vem vet mest?")
    _, mtime = read_with_mtime(p)
    remove_mapping(p, 288649, expected_mtime=mtime)
    assert [m.tvdb_id for m in MappingTable.load(p).all()] == [999]


def test_remove_of_an_unknown_id_is_refused(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    _, mtime = read_with_mtime(p)
    with pytest.raises(MappingError):
        remove_mapping(p, 12345, expected_mtime=mtime)


def test_add_to_a_missing_file_creates_it(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    add_mapping(
        p, tvdb_id=1, svt_series_id="x", svt_slug="y", series_title="Z",
        expected_mtime=None,
    )
    assert p.exists()


# --- Fix report: review findings ---


def test_duplicate_check_normalises_a_string_typed_tvdb_id(tmp_path: Path):
    # A hand-edited or quoting-quirk file can hold tvdb_id as a YAML string.
    # "288649" == 288649 is False in Python, so a naive check would let a
    # second row through and MappingTable.load would then reject the file.
    p = tmp_path / "mappings.yaml"
    p.write_text(
        "series:\n"
        "- tvdb_id: '288649'\n"
        "  svt_series_id: jpmQD3q\n"
        "  svt_slug: gift-vid-forsta-ogonkastet\n"
        f"  series_title: {TITLE}\n",
        encoding="utf-8",
    )
    _, mtime = read_with_mtime(p)
    with pytest.raises(MappingError):
        add_mapping(
            p,
            tvdb_id=288649,
            svt_series_id="jpmQD3q",
            svt_slug="gift-vid-forsta-ogonkastet",
            series_title="Something Else",
            expected_mtime=mtime,
        )


def test_remove_succeeds_when_exactly_one_row_matches(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    _, mtime = read_with_mtime(p)
    remove_mapping(p, 288649, expected_mtime=mtime)
    assert MappingTable.load(p).all() == []


def test_remove_refuses_when_multiple_rows_share_the_id(tmp_path: Path):
    # A hand-edited file can hold a duplicate tvdb_id, which MappingTable.load
    # rejects outright. remove_mapping must not silently delete both rows --
    # that is a destructive guess about which row the operator meant, made
    # for them without their knowledge.
    p = tmp_path / "mappings.yaml"
    p.write_text(
        "series:\n"
        "- tvdb_id: 42\n"
        "  svt_series_id: s1\n"
        "  svt_slug: sl1\n"
        "  series_title: First Copy\n"
        "- tvdb_id: 42\n"
        "  svt_series_id: s2\n"
        "  svt_slug: sl2\n"
        "  series_title: Second Copy\n"
        "- tvdb_id: 99\n"
        "  svt_series_id: s3\n"
        "  svt_slug: sl3\n"
        "  series_title: Other\n",
        encoding="utf-8",
    )
    _, mtime = read_with_mtime(p)
    with pytest.raises(MappingError):
        remove_mapping(p, 42, expected_mtime=mtime)
    # Nothing was removed: both rows for tvdb_id 42 are still on disk.
    raw, _ = read_with_mtime(p)
    assert len(raw["series"]) == 3


def test_add_to_a_file_whose_top_level_is_not_a_dict(tmp_path: Path):
    # yaml.safe_load of a top-level list ("- a\n- b\n") returns a list, not a
    # dict. The loader guards this case explicitly; the writer must too.
    p = tmp_path / "mappings.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    _, mtime = read_with_mtime(p)
    add_mapping(
        p, tvdb_id=1, svt_series_id="x", svt_slug="y", series_title="Z",
        expected_mtime=mtime,
    )
    assert MappingTable.load(p).for_tvdb(1).series_title == "Z"


@pytest.mark.parametrize("field", ["svt_series_id", "svt_slug", "series_title"])
def test_add_rejects_a_blank_field(tmp_path: Path, field: str):
    p = tmp_path / "mappings.yaml"
    kwargs = dict(
        tvdb_id=1, svt_series_id="x", svt_slug="y", series_title="Z",
        expected_mtime=None,
    )
    kwargs[field] = "   "
    with pytest.raises(MappingError) as exc:
        add_mapping(p, **kwargs)
    assert field in str(exc.value)
    assert not p.exists()


# --- Task 4: reloading mapping table ---

import logging

from svtplay_arr.mappings import ReloadingMappingTable


def test_reloading_table_picks_up_a_new_row_without_a_restart(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    table = ReloadingMappingTable(p)
    assert table.for_tvdb(999) is None

    _add(p, tvdb_id=999, title="Vem vet mest?")

    assert table.for_tvdb(999) is not None


def test_reloading_table_keeps_last_good_when_the_file_breaks(
    tmp_path: Path, caplog
):
    # An empty mappings table means an empty feed, and an empty feed makes
    # Sonarr reject the indexer. Failing to last-good is load-bearing.
    p = tmp_path / "mappings.yaml"
    _add(p)
    table = ReloadingMappingTable(p)
    assert table.for_tvdb(288649) is not None

    p.write_text("series: [{tvdb_id: 1, ", encoding="utf-8")  # truncated YAML

    with caplog.at_level(logging.WARNING):
        assert table.for_tvdb(288649) is not None
        assert table.all() != []
    assert "mappings" in caplog.text.lower()


def test_reloading_table_starts_empty_when_no_file_exists(tmp_path: Path):
    assert ReloadingMappingTable(tmp_path / "nope.yaml").all() == []


def test_reloading_table_logs_accurately_when_invalid_from_the_start(
    tmp_path: Path, caplog
):
    # There is no prior good table at construction time, so the log must
    # not claim the feed is "unaffected" -- it is presently empty, which is
    # the failure mode this project treats as the worst one.
    p = tmp_path / "mappings.yaml"
    p.write_text("series: [{tvdb_id: 1, ", encoding="utf-8")  # truncated YAML

    with caplog.at_level(logging.WARNING):
        table = ReloadingMappingTable(p)

    assert table.all() == []
    assert "unaffected" not in caplog.text.lower()
    assert "empty" in caplog.text.lower()
