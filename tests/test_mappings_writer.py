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


# --- The plural write, and provenance ---
#
# A sweep proposes N rows at once. N calls to add_mapping would be N
# separate atomic writes: N chances for a concurrent modification to slip
# in mid-sweep, N .bak churns, and a half-written library if one of them
# is refused. add_mappings is the single-write form, and add_mapping is
# now one row through it, so the duplicate/blank/validation semantics
# cannot drift between the two.

from svtplay_arr.mappings import SOURCE_AUTO, SOURCE_MANUAL, add_mappings


def _rows(**overrides):
    row = dict(
        tvdb_id=1, svt_series_id="s1", svt_slug="sl1", series_title="One",
    )
    row.update(overrides)
    return row


def _write_count(monkeypatch) -> list:
    """Count the atomic writes a call actually performs."""
    import svtplay_arr.mappings as mappings_mod

    calls = []
    real = mappings_mod.atomic_write_yaml

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(mappings_mod, "atomic_write_yaml", counting)
    return calls


def test_add_mappings_writes_every_row_in_one_write(tmp_path: Path, monkeypatch):
    p = tmp_path / "mappings.yaml"
    calls = _write_count(monkeypatch)

    add_mappings(p, [
        _rows(tvdb_id=1, series_title="One"),
        _rows(tvdb_id=2, svt_series_id="s2", svt_slug="sl2", series_title="Two"),
        _rows(tvdb_id=3, svt_series_id="s3", svt_slug="sl3", series_title="Three"),
    ], expected_mtime=None)

    assert len(calls) == 1
    assert {m.tvdb_id for m in MappingTable.load(p).all()} == {1, 2, 3}


def test_add_mappings_preserves_existing_rows(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    _, mtime = read_with_mtime(p)
    add_mappings(p, [_rows(tvdb_id=1)], expected_mtime=mtime)
    assert {m.tvdb_id for m in MappingTable.load(p).all()} == {288649, 1}


def test_add_mappings_refuses_a_duplicate_of_an_existing_row(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    before = p.read_text(encoding="utf-8")
    _, mtime = read_with_mtime(p)

    with pytest.raises(MappingError) as exc:
        add_mappings(p, [
            _rows(tvdb_id=1),
            _rows(tvdb_id=288649, series_title="Rival"),
        ], expected_mtime=mtime)

    assert TITLE in str(exc.value)
    # All or nothing: the valid row in the same batch is not written either.
    assert p.read_text(encoding="utf-8") == before


def test_add_mappings_refuses_a_duplicate_within_the_batch(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    with pytest.raises(MappingError):
        add_mappings(p, [
            _rows(tvdb_id=5, series_title="First"),
            _rows(tvdb_id=5, series_title="Second"),
        ], expected_mtime=None)
    assert not p.exists()


@pytest.mark.parametrize("field", ["svt_series_id", "svt_slug", "series_title"])
def test_add_mappings_rejects_a_blank_field_and_writes_nothing(
    tmp_path: Path, field: str
):
    p = tmp_path / "mappings.yaml"
    with pytest.raises(MappingError) as exc:
        add_mappings(
            p,
            [_rows(tvdb_id=1), _rows(tvdb_id=2, **{field: "   "})],
            expected_mtime=None,
        )
    assert field in str(exc.value)
    assert not p.exists()


def test_add_mappings_with_no_rows_does_not_touch_the_file(
    tmp_path: Path, monkeypatch
):
    # A sweep that finds nothing confident must not rewrite mappings.yaml:
    # that would bump its mtime (invalidating every open form) and churn a
    # .bak for a no-op.
    p = tmp_path / "mappings.yaml"
    _add(p)
    before = p.stat().st_mtime, p.read_text(encoding="utf-8")
    calls = _write_count(monkeypatch)

    add_mappings(p, [], expected_mtime=before[0])

    assert calls == []
    assert (p.stat().st_mtime, p.read_text(encoding="utf-8")) == before


def test_add_mappings_honours_the_concurrency_check(tmp_path: Path):
    from svtplay_arr.yamlio import ConcurrentModification

    p = tmp_path / "mappings.yaml"
    _add(p)
    before = p.read_text(encoding="utf-8")

    with pytest.raises(ConcurrentModification):
        add_mappings(p, [_rows(tvdb_id=1)], expected_mtime=1.0)

    assert p.read_text(encoding="utf-8") == before


def test_an_auto_written_row_is_marked_in_the_file(tmp_path: Path):
    # A guessed mapping and a hand-confirmed one must never be
    # indistinguishable later: this is what makes an automatic sweep
    # auditable, and revertable as a group.
    p = tmp_path / "mappings.yaml"
    add_mappings(p, [_rows(source=SOURCE_AUTO)], expected_mtime=None)

    assert "source: auto" in p.read_text(encoding="utf-8")
    assert MappingTable.load(p).for_tvdb(1).source == SOURCE_AUTO


def test_a_hand_confirmed_row_carries_no_provenance_key(tmp_path: Path):
    # Absent means manual. Writing `source: manual` on every row would be a
    # new key in every existing operator's file for no added fact.
    p = tmp_path / "mappings.yaml"
    _add(p)
    assert "source:" not in p.read_text(encoding="utf-8")
    assert MappingTable.load(p).for_tvdb(288649).source == SOURCE_MANUAL


def test_an_auto_row_survives_a_later_manual_add(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    add_mappings(p, [_rows(tvdb_id=1, source=SOURCE_AUTO)], expected_mtime=None)
    _add(p, tvdb_id=288649)
    table = MappingTable.load(p)
    assert table.for_tvdb(1).source == SOURCE_AUTO
    assert table.for_tvdb(288649).source == SOURCE_MANUAL


def test_provenance_survives_an_unrelated_edit(tmp_path: Path):
    # Auditing or reverting the sweep as a group only works if the marker
    # outlives every later add and remove through this writer.
    p = tmp_path / "mappings.yaml"
    add_mappings(p, [
        _rows(tvdb_id=1, source=SOURCE_AUTO),
        _rows(tvdb_id=2, svt_series_id="s2", svt_slug="sl2", series_title="Two"),
    ], expected_mtime=None)
    _, mtime = read_with_mtime(p)
    remove_mapping(p, 2, expected_mtime=mtime)
    _add(p, tvdb_id=3, title="Three")

    table = MappingTable.load(p)
    assert table.for_tvdb(1).source == SOURCE_AUTO
    assert table.for_tvdb(3).source == SOURCE_MANUAL


def test_a_non_numeric_tvdb_id_is_refused_rather_than_written(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    with pytest.raises(MappingError):
        add_mappings(p, [_rows(tvdb_id="not a number")], expected_mtime=None)
    assert not p.exists()


# --- One SVT programme, one unconfirmed row ---
#
# Two mappings pointing at one SVT programme answer a search for either
# with episodes of the same show, permanently. `discovery` surfaces that
# before it reaches the writer; this is the net under it, and it is
# scoped to rows nobody confirmed.


def test_an_auto_row_may_not_claim_a_programme_already_mapped(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)  # svt_series_id jpmQD3q, by hand
    before = p.read_text(encoding="utf-8")
    _, mtime = read_with_mtime(p)

    with pytest.raises(MappingError) as exc:
        add_mappings(p, [_rows(
            tvdb_id=999, svt_series_id="jpmQD3q", source=SOURCE_AUTO,
        )], expected_mtime=mtime)

    assert "jpmQD3q" in str(exc.value) and TITLE in str(exc.value)
    assert p.read_text(encoding="utf-8") == before


def test_two_auto_rows_may_not_claim_one_programme_within_a_batch(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    with pytest.raises(MappingError):
        add_mappings(p, [
            _rows(tvdb_id=1, svt_series_id="shared", source=SOURCE_AUTO),
            _rows(tvdb_id=2, svt_series_id="shared", series_title="Two",
                  source=SOURCE_AUTO),
        ], expected_mtime=None)
    assert not p.exists()


def test_a_confirmed_row_may_still_share_a_programme(tmp_path: Path):
    # Unchanged on the manual path. The resolver tolerates two mappings
    # sharing a slug, and a human who does this deliberately has looked at
    # it -- the hazard only exists when nobody did.
    p = tmp_path / "mappings.yaml"
    _add(p)
    _, mtime = read_with_mtime(p)
    add_mapping(
        p, tvdb_id=999, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title="Deliberate Twin",
        expected_mtime=mtime,
    )
    assert {m.tvdb_id for m in MappingTable.load(p).all()} == {288649, 999}


def test_an_auto_row_may_claim_a_programme_nothing_else_points_at(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    _add(p)
    _, mtime = read_with_mtime(p)
    add_mappings(p, [_rows(tvdb_id=999, svt_series_id="fresh", source=SOURCE_AUTO)],
                 expected_mtime=mtime)
    assert MappingTable.load(p).for_tvdb(999).source == SOURCE_AUTO
