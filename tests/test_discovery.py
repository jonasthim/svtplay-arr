"""The confidence gate, and the sweep built on it.

Every test here is about one question: when is svtplay-arr allowed to write
a mapping nobody confirmed? The answer this module encodes is "almost
never", and these tests are the whole safety argument for it -- loosening
`confident_match` to first-hit-wins (the behaviour the old
`suggest_mappings` had) must fail several of them.
"""

import asyncio

import pytest

from svtplay_arr.discovery import (
    Candidate,
    confident_match,
    normalise_title,
    sweep_for_mappings,
)
from svtplay_arr.models import SvtSearchHit


def _hit(name, svt_id="abc123", typename="TvSeries"):
    return SvtSearchHit(svt_id=svt_id, name=name, typename=typename)


# --- normalisation ---------------------------------------------------


def test_normalisation_casefolds_and_collapses_whitespace():
    assert normalise_title("  Gift   VID\tförsta ögonkastet ") == (
        "gift vid första ögonkastet"
    )


def test_normalisation_strips_a_trailing_parenthesised_year():
    # Sonarr's library titles routinely carry TVDB's disambiguating year.
    assert normalise_title("Solsidan (2019)") == "solsidan"


def test_normalisation_keeps_a_year_that_is_not_trailing():
    assert normalise_title("1917 (1917) och sedan") == "1917 (1917) och sedan"


def test_normalisation_does_not_fold_diacritics():
    # The single most important rule in this module. Swedish titles differ
    # by å/ä/ö alone, and folding them would manufacture exact matches
    # between genuinely different shows -- the one error class this whole
    # feature exists to avoid.
    assert normalise_title("Mörka hjärtan") != normalise_title("Morka hjartan")


# --- the gate --------------------------------------------------------


def test_an_exact_unique_match_is_confident():
    got = confident_match("Gift vid första ögonkastet", [
        _hit("Gift vid första ögonkastet", "jpmQD3q"),
        _hit("Something Else", "other"),
    ])
    assert got is not None
    assert got.svt_id == "jpmQD3q"


def test_case_and_spacing_differences_still_match():
    got = confident_match("Vem vet mest?", [_hit("VEM  VET   MEST?", "v1")])
    assert got is not None and got.svt_id == "v1"


def test_a_trailing_year_on_the_sonarr_side_still_matches():
    got = confident_match("Solsidan (2019)", [_hit("Solsidan", "s1")])
    assert got is not None and got.svt_id == "s1"


def test_two_qualifying_candidates_are_never_written():
    # Two SVT programmes whose names normalise identically -- a rerun
    # alongside the original, or two seasons listed separately. There is no
    # principled way to pick, so the answer is "ask a human", exactly as
    # Resolver refuses a 2-candidate episode match.
    assert confident_match("Solsidan", [
        _hit("Solsidan", "s1"),
        _hit("solsidan", "s2"),
    ]) is None


def test_a_near_miss_is_never_written():
    # "Vem vet mest? Junior" is a different programme with a different
    # episode list. One character of difference is still a difference.
    assert confident_match("Vem vet mest?", [
        _hit("Vem vet mest? Junior", "j1"),
    ]) is None


def test_a_diacritic_near_miss_is_never_written():
    assert confident_match("Mörka hjärtan", [_hit("Morka hjartan", "m1")]) is None


def test_no_hits_at_all_is_not_a_match():
    assert confident_match("Nonexistent Show", []) is None


def test_only_series_typenames_qualify():
    # A search hit for a single video or a clip carries the show's name but
    # is not the show. Mapping to it would point the resolver's slug at
    # nothing.
    assert confident_match("Dokument inifrån", [
        _hit("Dokument inifrån", "d1", typename="Episode"),
    ]) is None


def test_tvshow_qualifies_alongside_tvseries():
    got = confident_match("Uppdrag granskning", [
        _hit("Uppdrag granskning", "u1", typename="TvShow"),
    ])
    assert got is not None and got.svt_id == "u1"


def test_a_non_qualifying_hit_does_not_create_ambiguity():
    # The uniqueness rule counts programmes, not search rows: an Episode
    # entry sharing the name is filtered out before the count, so a single
    # real series beside it is still unambiguous.
    got = confident_match("Solsidan", [
        _hit("Solsidan", "s1"),
        _hit("Solsidan", "e1", typename="Episode"),
    ])
    assert got is not None and got.svt_id == "s1"


def test_the_same_programme_returned_twice_is_not_ambiguity():
    # Identity, not fuzz: two rows with the same svtId denote one
    # programme, so collapsing them cannot change *which* programme gets
    # written -- it only stops SVT repeating itself from reading as doubt.
    got = confident_match("Solsidan", [_hit("Solsidan", "s1"), _hit("Solsidan", "s1")])
    assert got is not None and got.svt_id == "s1"


def test_a_blank_sonarr_title_never_matches():
    # Otherwise a Sonarr record with an empty title would match any SVT hit
    # whose name is also blank/whitespace.
    assert confident_match("   ", [_hit("   ", "b1")]) is None


def test_a_blank_svt_name_never_matches():
    assert confident_match("Solsidan", [_hit("", "b1")]) is None


# --- the sweep -------------------------------------------------------


class FakeSonarr:
    def __init__(self, series=None, error=None):
        self.series = series if series is not None else []
        self.error = error
        self.calls = 0

    async def all_series(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.series


class FakeSvt:
    """Records every search, and how many ran at once."""

    def __init__(self, results=None, error_for=(), delay=0.0):
        self.results = results or {}
        self.error_for = set(error_for)
        self.delay = delay
        self.queries: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def search_series(self, query):
        self.queries.append(query)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if query in self.error_for:
                raise RuntimeError("SVT is down")
            return self.results.get(query, [])
        finally:
            self.in_flight -= 1


async def test_an_exact_unique_match_is_proposed_for_writing():
    sonarr = FakeSonarr([{"id": 1, "tvdbId": 288649, "title": "Solsidan"}])
    svt = FakeSvt({"Solsidan": [_hit("Solsidan", "s1")]})

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    assert [m.tvdb_id for m in sweep.confident] == [288649]
    assert sweep.confident[0].svt_series_id == "s1"
    # Derived by svt.client.derive_slug, never left blank.
    assert sweep.confident[0].svt_slug == "solsidan"


async def test_the_series_title_comes_from_sonarr_not_from_svt():
    # SVT's spelling differs only in case, so the gate still matches -- but
    # the title written is the permanent filename, and Sonarr's record is
    # the only place it may come from.
    sonarr = FakeSonarr([{"id": 1, "tvdbId": 7, "title": "Gift vid första ögonkastet"}])
    svt = FakeSvt({
        "Gift vid första ögonkastet": [_hit("GIFT VID FÖRSTA ÖGONKASTET", "g1")]
    })

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    assert sweep.confident[0].series_title == "Gift vid första ögonkastet"


async def test_an_already_mapped_series_is_skipped_without_an_svt_call():
    sonarr = FakeSonarr([
        {"id": 1, "tvdbId": 288649, "title": "Solsidan"},
        {"id": 2, "tvdbId": 999, "title": "Vem vet mest?"},
    ])
    svt = FakeSvt({"Vem vet mest?": [_hit("Vem vet mest?", "v1")]})

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids={288649})

    assert svt.queries == ["Vem vet mest?"]
    assert sweep.already_mapped == 1


async def test_several_candidates_are_surfaced_never_written():
    sonarr = FakeSonarr([{"id": 1, "tvdbId": 7, "title": "Solsidan"}])
    svt = FakeSvt({"Solsidan": [_hit("Solsidan", "s1"), _hit("solsidan", "s2")]})

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    assert sweep.confident == ()
    (p,) = sweep.needs_decision
    assert p.tvdb_id == 7
    assert {c.svt_id for c in p.candidates} == {"s1", "s2"}
    # Every candidate carries a slug, so accepting one is a single click
    # with nothing left to transcribe off an SVT URL.
    assert all(c.slug for c in p.candidates)


async def test_a_near_miss_is_surfaced_never_written():
    sonarr = FakeSonarr([{"id": 1, "tvdbId": 7, "title": "Vem vet mest?"}])
    svt = FakeSvt({"Vem vet mest?": [_hit("Vem vet mest? Junior", "j1")]})

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    assert sweep.confident == ()
    (p,) = sweep.needs_decision
    assert [c.svt_id for c in p.candidates] == ["j1"]


async def test_no_svt_hits_is_reported_not_written():
    sonarr = FakeSonarr([{"id": 1, "tvdbId": 7, "title": "Nonexistent Show"}])
    svt = FakeSvt({})

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    assert sweep.confident == ()
    assert sweep.needs_decision == ()
    (p,) = sweep.no_match
    assert p.tvdb_id == 7 and p.candidates == ()


async def test_one_failed_svt_search_does_not_abort_the_sweep():
    sonarr = FakeSonarr([
        {"id": 1, "tvdbId": 7, "title": "Broken"},
        {"id": 2, "tvdbId": 8, "title": "Solsidan"},
    ])
    svt = FakeSvt({"Solsidan": [_hit("Solsidan", "s1")]}, error_for={"Broken"})

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    assert [m.tvdb_id for m in sweep.confident] == [8]
    (p,) = sweep.search_failed
    assert p.tvdb_id == 7
    assert "SVT is down" in p.reason


async def test_a_sonarr_outage_is_raised_before_anything_is_proposed():
    sonarr = FakeSonarr(error=RuntimeError("sonarr is down"))
    svt = FakeSvt({})

    with pytest.raises(RuntimeError):
        await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    assert svt.queries == []


async def test_malformed_sonarr_records_are_skipped_without_an_svt_call():
    sonarr = FakeSonarr(["not a dict", {"id": 1, "title": "No TVDB id"},
                         {"id": 2, "tvdbId": 5, "title": ""}])
    svt = FakeSvt({})

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    assert svt.queries == []
    assert sweep.confident == () and sweep.proposals == ()


async def test_the_search_cap_is_reported_rather_than_silently_truncating():
    sonarr = FakeSonarr([
        {"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(10)
    ])
    svt = FakeSvt({})

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set(), cap=4)

    assert len(svt.queries) == 4
    assert sweep.searched == 4
    assert sweep.not_searched == 6
    assert sweep.cap == 4
    assert sweep.capped is True


async def test_an_unreached_cap_is_not_reported_as_capped():
    sonarr = FakeSonarr([{"id": 1, "tvdbId": 7, "title": "Solsidan"}])
    sweep = await sweep_for_mappings(
        sonarr, FakeSvt({}), mapped_tvdb_ids=set(), cap=4
    )
    assert sweep.capped is False and sweep.not_searched == 0


async def test_concurrency_is_bounded():
    # One search per unmapped series against an unofficial API. A library of
    # 300 shows must not become 300 simultaneous requests to SVT.
    sonarr = FakeSonarr([
        {"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(20)
    ])
    svt = FakeSvt({}, delay=0.005)

    await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set(), concurrency=3)

    assert svt.peak_in_flight <= 3
    assert len(svt.queries) == 20


async def test_the_sweep_never_asks_svt_for_an_episode_list():
    # It writes mappings and nothing else: it must not touch the matching
    # path or anything the download pipeline reads.
    class EpisodeListIsForbidden(FakeSvt):
        async def list_episodes(self, slug):
            raise AssertionError("the sweep must not call list_episodes")

    sonarr = FakeSonarr([{"id": 1, "tvdbId": 7, "title": "Solsidan"}])
    svt = EpisodeListIsForbidden({"Solsidan": [_hit("Solsidan", "s1")]})

    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    assert len(sweep.confident) == 1


def test_candidate_is_hashable_and_carries_what_a_write_needs():
    c = Candidate(svt_id="s1", name="Solsidan", slug="solsidan")
    assert (c.svt_id, c.name, c.slug) == ("s1", "Solsidan", "solsidan")


# --- The CLI's report ------------------------------------------------
#
# `svtplay-arr-suggest-mappings` used to be a second implementation of this
# idea, taking the first SVT hit with no confidence check and emitting a
# blank svt_slug. It now runs the same gate as the config page and still
# writes nothing at all.


async def _sweep(series, results, **kwargs):
    return await sweep_for_mappings(
        FakeSonarr(series), FakeSvt(results), mapped_tvdb_ids=set(), **kwargs
    )


async def test_the_cli_rows_are_what_the_page_would_have_written():
    from svtplay_arr.discovery import confident_rows

    sweep = await _sweep(
        [{"id": 1, "tvdbId": 288649, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1")]},
    )

    assert confident_rows(sweep) == [{
        "tvdb_id": 288649,
        "svt_series_id": "s1",
        # Derived, never blank: the old CLI emitted "" here and left the
        # operator to transcribe it off an SVT Play URL.
        "svt_slug": "solsidan",
        "series_title": "Solsidan",
        # Marked, so a row pasted from the CLI is the same kind of row the
        # page writes rather than silently passing as hand-confirmed.
        "source": "auto",
    }]


async def test_the_cli_prints_no_row_for_anything_ambiguous():
    from svtplay_arr.discovery import confident_rows

    sweep = await _sweep(
        [{"id": 1, "tvdbId": 7, "title": "Solsidan"}],
        {"Solsidan": [_hit("Solsidan", "s1"), _hit("solsidan", "s2")]},
    )

    assert confident_rows(sweep) == []


async def test_the_cli_report_names_every_undecided_series_and_its_candidates():
    from svtplay_arr.discovery import format_report

    sweep = await _sweep(
        [
            {"id": 1, "tvdbId": 7, "title": "Vem vet mest?"},
            {"id": 2, "tvdbId": 8, "title": "Not On SVT"},
        ],
        {"Vem vet mest?": [_hit("Vem vet mest? Junior", "j1")]},
    )

    report = format_report(sweep)

    assert "Vem vet mest?" in report and "j1" in report and "junior" in report
    assert "NO SVT MATCH" in report and "Not On SVT" in report


async def test_the_cli_report_says_when_the_cap_bit():
    from svtplay_arr.discovery import format_report

    sweep = await _sweep(
        [{"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(5)],
        {},
        cap=2,
    )

    report = format_report(sweep)
    assert "NOT searched" in report and "3" in report


async def test_the_cli_report_names_a_failed_search_separately_from_no_match():
    from svtplay_arr.discovery import format_report

    sonarr = FakeSonarr([{"id": 1, "tvdbId": 7, "title": "Broken"}])
    svt = FakeSvt({}, error_for={"Broken"})
    sweep = await sweep_for_mappings(sonarr, svt, mapped_tvdb_ids=set())

    report = format_report(sweep)
    # "SVT had nothing for this" and "SVT could not be asked" are different
    # facts and must not be reported as the same one.
    assert "SEARCH FAILED" in report
    assert "NO SVT MATCH" not in report


def test_the_console_script_points_at_a_callable_that_exists():
    import tomllib

    from svtplay_arr import discovery

    with open("pyproject.toml", "rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]
    assert scripts["svtplay-arr-suggest-mappings"] == "svtplay_arr.discovery:main"
    assert callable(discovery.main)


def test_the_old_first_hit_wins_helper_is_gone():
    # Two implementations of one idea drifting apart is this codebase's
    # most persistent defect. suggest_mappings was rewritten out of
    # existence rather than fixed in parallel; nothing may quietly restore
    # a second, laxer matcher beside the gate.
    import svtplay_arr.mappings as mappings_mod

    assert not hasattr(mappings_mod, "suggest_mappings")
    assert not hasattr(mappings_mod, "main")


def test_the_sweep_cannot_reach_the_matching_or_download_path():
    # A structural version of the rule: this module writes mappings and
    # nothing else. If it ever imports the resolver, the worker or the job
    # store, "the sweep only writes mappings" has stopped being true by
    # construction and become a thing someone has to remember.
    import ast
    from pathlib import Path

    tree = ast.parse(Path("src/svtplay_arr/discovery.py").read_text("utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    forbidden = {
        "svtplay_arr.resolver",
        "svtplay_arr.worker",
        "svtplay_arr.store",
        "svtplay_arr.downloader",
        "svtplay_arr.naming",
    }
    assert not (imported & forbidden)
