from pathlib import Path
import pytest
from svtplay_arr.mappings import MappingTable

YAML = """
series:
  - tvdb_id: 288649
    svt_series_id: jpmQD3q
    svt_slug: gift-vid-forsta-ogonkastet
    series_title: Gift vid första ögonkastet
"""


def test_loads_mapping(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text(YAML, encoding="utf-8")
    table = MappingTable.load(p)
    m = table.for_tvdb(288649)
    assert m.svt_series_id == "jpmQD3q"
    assert m.series_title == "Gift vid första ögonkastet"


def test_unmapped_tvdb_returns_none(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text(YAML, encoding="utf-8")
    assert MappingTable.load(p).for_tvdb(1) is None


def test_missing_file_is_empty_not_fatal(tmp_path: Path):
    assert MappingTable.load(tmp_path / "nope.yaml").for_tvdb(288649) is None


def test_duplicate_tvdb_ids_rejected(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text(YAML + YAML.split("series:")[1], encoding="utf-8")
    with pytest.raises(ValueError):
        MappingTable.load(p)


async def test_suggest_mappings_proposes_rows_without_writing(tmp_path: Path):
    from svtplay_arr.mappings import suggest_mappings
    from svtplay_arr.models import SvtSearchHit

    class FakeSonarr:
        async def all_series(self):
            return [{"id": 70, "tvdbId": 288649,
                     "title": "Gift vid första ögonkastet"}]

    class FakeSvt:
        async def search_series(self, query):
            return [SvtSearchHit("jpmQD3q", "Gift vid första ögonkastet",
                                 "TvSeries")]

    out = await suggest_mappings(FakeSonarr(), FakeSvt())
    assert out == [{
        "tvdb_id": 288649,
        "svt_series_id": "jpmQD3q",
        "svt_slug": "",
        "series_title": "Gift vid första ögonkastet",
        "svt_name": "Gift vid första ögonkastet",
    }]
    # Nothing is written: a wrong series mapping is exactly the error class the
    # resolver refuses to make unaided, so a human confirms it.
    assert list(tmp_path.iterdir()) == []


async def test_suggest_mappings_skips_series_with_no_svt_hit():
    from svtplay_arr.mappings import suggest_mappings

    class FakeSonarr:
        async def all_series(self):
            return [{"id": 1, "tvdbId": 999, "title": "Nonexistent Show"}]

    class FakeSvt:
        async def search_series(self, query):
            return []

    assert await suggest_mappings(FakeSonarr(), FakeSvt()) == []


# --- Hardening beyond the brief's reference code: a hand-edited YAML file is
# at least as likely to be malformed as an API response, so MappingTable.load
# treats it with the same "guard, don't crash" discipline as sonarr.py and
# svt/client.py apply to untrusted JSON.


def test_invalid_yaml_syntax_raises_value_error(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text("series: [this is not: valid: yaml", encoding="utf-8")
    with pytest.raises(ValueError):
        MappingTable.load(p)


def test_non_dict_top_level_document_is_refused(tmp_path: Path):
    # Used to load as an empty table. That is indistinguishable from "the
    # operator has no mappings", which is what let a broken file silently
    # empty the feed -- see the unrecognised-shape family below.
    p = tmp_path / "mappings.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        MappingTable.load(p)


def test_non_list_series_value_is_refused(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text("series: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        MappingTable.load(p)


def test_non_dict_entry_is_skipped_valid_rows_still_load(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text(
        "series:\n  - just a string, not a mapping\n" + YAML.split("series:")[1],
        encoding="utf-8",
    )
    table = MappingTable.load(p)
    assert table.for_tvdb(288649).svt_series_id == "jpmQD3q"


def test_entry_missing_required_key_is_skipped(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text(
        """
series:
  - tvdb_id: 1
    svt_series_id: abc
    svt_slug: some-slug
    # series_title missing
""",
        encoding="utf-8",
    )
    assert MappingTable.load(p).for_tvdb(1) is None


def test_non_integer_tvdb_id_raises_value_error(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text(
        """
series:
  - tvdb_id: not-a-number
    svt_series_id: abc
    svt_slug: some-slug
    series_title: Some Show
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        MappingTable.load(p)


async def test_suggest_mappings_skips_non_dict_series_entry():
    from svtplay_arr.mappings import suggest_mappings

    class FakeSonarr:
        async def all_series(self):
            return ["not a dict"]

    class FakeSvt:
        async def search_series(self, query):
            raise AssertionError("should not be called for a malformed entry")

    assert await suggest_mappings(FakeSonarr(), FakeSvt()) == []


async def test_suggest_mappings_skips_series_missing_tvdb_id():
    from svtplay_arr.mappings import suggest_mappings

    class FakeSonarr:
        async def all_series(self):
            return [{"id": 1, "title": "No TVDB ID"}]

    class FakeSvt:
        async def search_series(self, query):
            raise AssertionError("should not be called for a malformed entry")

    assert await suggest_mappings(FakeSonarr(), FakeSvt()) == []


def test_all_returns_every_mapping(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text(YAML, encoding="utf-8")
    assert [m.tvdb_id for m in MappingTable.load(p).all()] == [288649]


def test_all_is_empty_when_no_file_exists(tmp_path: Path):
    assert MappingTable.load(tmp_path / "nope.yaml").all() == []


# --- The unrecognised-shape family. `MappingTable.load` used to return an
# empty table for a top-level document it did not recognise, which
# `ReloadingMappingTable._refresh` then installed over the last known-good
# table as if it were a successful load: the RSS feed went empty within one
# Sonarr poll, with nothing in the log, because nothing had "failed".
#
# A genuinely empty `series: []` is a state an operator can legitimately
# intend (it is exactly what deleting the last mapping through the config
# page writes), so it stays a successful load. Everything else is a shape
# the loader cannot recognise, and is now a failure.


def test_empty_file_is_a_failure_not_an_empty_table(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        MappingTable.load(p)


def test_top_level_list_is_a_failure_not_an_empty_table(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        MappingTable.load(p)


def test_series_key_with_no_value_is_a_failure(tmp_path: Path):
    # The operator-over-SSH case: delete the rows, leave `series:` behind.
    p = tmp_path / "mappings.yaml"
    p.write_text("series:\n", encoding="utf-8")
    with pytest.raises(ValueError):
        MappingTable.load(p)


def test_missing_series_key_is_a_failure(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text("something_else: 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        MappingTable.load(p)


def test_explicitly_empty_series_list_loads_as_an_empty_table(tmp_path: Path):
    p = tmp_path / "mappings.yaml"
    p.write_text("series: []\n", encoding="utf-8")
    assert MappingTable.load(p).all() == []


# --- ReloadingMappingTable's degrade behaviour, one case per shape.
#
# The measured starting point for every case below is a good file holding
# one mapping that has already loaded. What matters is whether the next
# `_refresh` keeps serving it (and says so in the log) or silently installs
# an empty table -- an empty feed is what makes Sonarr reject the indexer.


def _reloading(tmp_path: Path):
    """A ReloadingMappingTable that has already loaded one good mapping."""
    from svtplay_arr.mappings import ReloadingMappingTable

    p = tmp_path / "mappings.yaml"
    p.write_text(YAML, encoding="utf-8")
    table = ReloadingMappingTable(p)
    assert [m.tvdb_id for m in table.all()] == [288649]
    return p, table


def _rewrite(p: Path, text: str) -> None:
    """Replace the file with a distinguishably newer mtime.

    Reload detection is mtime-based, so a rewrite landing inside the same
    filesystem timestamp tick would be invisible and the test would pass
    for the wrong reason.
    """
    import os

    p.write_text(text, encoding="utf-8")
    os.utime(p, (p.stat().st_atime + 10, p.stat().st_mtime + 10))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("series:\n", id="rows-deleted-series-key-left"),
        pytest.param("", id="empty-file"),
        pytest.param("- just\n- a\n- list\n", id="top-level-list"),
        pytest.param("series: [this is not: valid: yaml", id="unparseable"),
    ],
)
def test_an_unloadable_file_keeps_the_last_good_table_and_warns(
    tmp_path: Path, caplog, text: str
):
    import logging

    p, table = _reloading(tmp_path)
    _rewrite(p, text)

    with caplog.at_level(logging.WARNING, logger="svtplay_arr.mappings"):
        assert [m.tvdb_id for m in table.all()] == [288649]
    assert table.for_tvdb(288649) is not None
    assert caplog.records, "a degrade with no log entry is the silent failure"
    assert str(p) in caplog.text


def test_an_unreadable_file_keeps_the_last_good_table_and_warns(
    tmp_path: Path, caplog
):
    import logging
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    p, table = _reloading(tmp_path)
    _rewrite(p, YAML + "  # touched\n")
    p.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="svtplay_arr.mappings"):
            assert [m.tvdb_id for m in table.all()] == [288649]
        assert caplog.records
    finally:
        p.chmod(0o644)


def test_an_explicitly_emptied_file_is_applied_without_a_warning(
    tmp_path: Path, caplog
):
    # The one legitimate way to reach zero mappings: `series: []`, which is
    # exactly what deleting the last row through the config page writes.
    import logging

    p, table = _reloading(tmp_path)
    _rewrite(p, "series: []\n")

    with caplog.at_level(logging.WARNING, logger="svtplay_arr.mappings"):
        assert table.all() == []
    assert not caplog.records, caplog.text


# --- The two stat-failure branches, which went in with no test at all.
# `coverage` reported both bodies unexecuted by any of the 315 tests.


def test_a_deleted_file_keeps_the_last_good_table_and_warns(tmp_path: Path, caplog):
    import logging

    p, table = _reloading(tmp_path)
    p.unlink()

    with caplog.at_level(logging.WARNING, logger="svtplay_arr.mappings"):
        assert [m.tvdb_id for m in table.all()] == [288649]
    assert table.status()["degraded"] is True
    assert "disappeared" in caplog.text


def test_a_missing_file_that_never_loaded_is_not_degraded(tmp_path: Path):
    # The fresh-install state: no mappings yet, the config page is how they
    # get made. Reporting that as a degrade would make /health red on day
    # one of every deployment.
    from svtplay_arr.mappings import ReloadingMappingTable

    table = ReloadingMappingTable(tmp_path / "nope.yaml")
    assert table.status() == {"ever_loaded": False, "degraded": False, "count": 0}


def _require_non_root():
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")


def test_an_unreachable_directory_keeps_the_last_good_table_and_warns(
    tmp_path: Path, caplog
):
    import logging

    _require_non_root()
    d = tmp_path / "conf"
    d.mkdir()
    p, table = _reloading(d)
    d.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="svtplay_arr.mappings"):
            assert [m.tvdb_id for m in table.all()] == [288649]
        assert table.status()["degraded"] is True
        assert "could not be stat'd" in caplog.text
    finally:
        d.chmod(0o755)


def test_a_cleared_stat_failure_returns_to_healthy(tmp_path: Path):
    # The regression this test exists for: both stat-failure branches
    # returned without touching self._mtime, so once the condition cleared
    # -- with the file byte- and mtime-identical, because nothing had ever
    # been wrong with the file -- _refresh hit the mtime-equal early return
    # and never reset the flag. /health then reported "degraded" forever on
    # a service serving exactly the right mappings, while deploy/README.md
    # told the operator that means the file needs fixing.
    _require_non_root()
    d = tmp_path / "conf"
    d.mkdir()
    p, table = _reloading(d)
    before = p.read_bytes()
    mtime_before = p.stat().st_mtime

    d.chmod(0o000)
    try:
        assert table.status()["degraded"] is True
    finally:
        d.chmod(0o755)

    # Nothing about the file changed while it was unreachable.
    assert p.read_bytes() == before
    assert p.stat().st_mtime == mtime_before

    assert table.status() == {"ever_loaded": True, "degraded": False, "count": 1}
    assert [m.tvdb_id for m in table.all()] == [288649]


def test_a_load_failure_stays_degraded_while_the_file_is_unchanged(tmp_path: Path):
    # The other half of the split: a file that will not load is only fixed
    # by changing it, so that flag must NOT clear on an unchanged file.
    p, table = _reloading(tmp_path)
    _rewrite(p, "series:\n")

    assert table.status()["degraded"] is True
    assert table.status()["degraded"] is True  # still, on a repeat refresh
    assert [m.tvdb_id for m in table.all()] == [288649]

    _rewrite(p, YAML)
    assert table.status()["degraded"] is False
