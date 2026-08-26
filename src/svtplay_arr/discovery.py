"""Proposing tvdb -> SVT mappings, and deciding which ones may be written.

This is the only module allowed to decide that a mapping is safe to write
without a human confirming it, and it is deliberately built to say "no".
It mirrors `resolver.py` one level up: `Resolver` refuses to guess *which
episode* a Sonarr request means, and returns None on any doubt, because
Sonarr runs with renameEpisodes=False and a wrong answer becomes a
permanent filename in the library. A wrong *series* mapping is the same
error one level larger -- it makes every episode of that show wrong -- so
the rule is the same rule.

`confident_match` therefore requires two independent signals to agree, and
`sweep_for_mappings` surfaces everything else instead of writing it:

  1. **Exact equality after normalisation.** The Sonarr series title and
     the SVT programme name must be the same string once normalised. Not
     a prefix, not a substring, not a fuzzy distance: "Vem vet mest?" and
     "Vem vet mest? Junior" are different programmes with different
     episode lists, and every fuzzy rule that would unite them also unites
     a pair that must stay apart.
  2. **Uniqueness.** Exactly one SVT programme may qualify. Two
     programmes normalising to the same name -- a rerun beside the
     original, two runs listed separately -- is doubt, and doubt is
     surfaced, never resolved by picking the first. This is `Resolver`'s
     `if len(candidates) != 1: return None`, verbatim in spirit.

Normalisation (`normalise_title`) casefolds, collapses whitespace, and
strips a trailing parenthesised year -- Sonarr's library titles carry
TVDB's disambiguating year (`Solsidan (2019)`) where SVT's do not. It
deliberately does **not** strip diacritics. Swedish titles are
distinguished by å/ä/ö, and folding them together would manufacture exact
matches between genuinely different titles: the precise error this module
exists to prevent. `derive_slug` in `svt/client.py` does fold them, and
that is correct there for the opposite reason -- it is reproducing SVT's
own URL convention, and its output is a suggestion a human can correct,
not a decision to write a file.

A Sonarr series whose TVDB title differs from SVT's Swedish title will not
auto-match. That is the intended outcome, not a gap: it becomes a
suggestion with one-click candidates.
"""

import asyncio
import logging
import re
from dataclasses import dataclass

from svtplay_arr.models import SOURCE_AUTO, SvtSearchHit
from svtplay_arr.svt.client import derive_slug

log = logging.getLogger(__name__)

# The SVT search item types that denote a programme (a thing with an
# episode list at a slug), as opposed to a single video or a clip that
# merely carries the programme's name. Same tuple `suggest_mappings` used;
# it is the only SVT knowledge in this module, and it is a filter, not a
# choice.
_SERIES_TYPENAMES = ("TvSeries", "TvShow")

# One SVT search per unmapped series, against an unofficial API. These
# bound what a single "Find mappings" click costs SVT.
#
# `_CONCURRENCY` is modest on purpose: the point is to finish a 200-show
# library in a couple of minutes rather than half an hour, not to saturate
# anything. `_CAP` bounds the total; hitting it is *reported* (see
# `Sweep.capped`) rather than silently truncating the library, and because
# a run writes its confident rows, the next run skips them and continues.
_CONCURRENCY = 4
_CAP = 200

# Sonarr's library titles carry TVDB's disambiguating year as a trailing
# `(2019)`; SVT's names do not. Anchored at the end, so a year that is part
# of the title itself is untouched.
_TRAILING_YEAR = re.compile(r"\s*\(\d{4}\)\s*$")


def normalise_title(title: str) -> str:
    """The comparison form of a series title. See the module docstring.

    Casefold, collapse whitespace, strip one trailing parenthesised year.
    Diacritics are preserved deliberately -- see the module docstring for
    why folding them would be a bug and not a convenience.
    """
    text = _TRAILING_YEAR.sub("", str(title or "").strip())
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class Candidate:
    """One SVT programme offered as a possible match, ready to be written.

    Carries the slug already, so accepting a suggestion is one click with
    nothing left to transcribe off an SVT Play URL. `slug` is
    `derive_slug`'s output -- a suggestion, never a source of truth, and
    the accept path re-renders it in a form the operator can see.
    """

    svt_id: str
    name: str
    slug: str

    @classmethod
    def of(cls, hit: SvtSearchHit) -> "Candidate":
        return cls(svt_id=hit.svt_id, name=hit.name, slug=derive_slug(hit.name))


@dataclass(frozen=True)
class ConfidentMatch:
    """A row the gate is willing to write without anyone confirming it.

    `series_title` is Sonarr's own spelling, verbatim, and never SVT's --
    it becomes the permanent filename in the library, exactly as on the
    manual path. `svt_name` is carried only so the result page can show
    what it matched against; nothing writes it.
    """

    tvdb_id: int
    series_title: str
    svt_series_id: str
    svt_slug: str
    svt_name: str


@dataclass(frozen=True)
class Proposal:
    """A series the gate refused to decide, and why.

    `outcome` is one of:

    * `needs_decision` -- SVT returned programmes, but not exactly one
      whose name matches. `candidates` holds them, each one click away
      from being written.
    * `no_match` -- SVT returned nothing usable for this title at all.
      `candidates` is empty; the operator needs the manual search.
    * `search_failed` -- the SVT search itself errored. Says nothing about
      whether a match exists; `reason` carries the failure.
    """

    tvdb_id: int
    series_title: str
    outcome: str
    reason: str
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class Sweep:
    """Everything one "Find mappings" run found. Writes nothing itself.

    Deliberately a plain value: the caller decides what to do with
    `confident` (the config page writes it in one atomic batch; the CLI
    prints it). Keeping the decision and the write in separate places is
    what makes "a Sonarr outage mid-sweep cannot write a partial file"
    structural rather than a promise -- there is nothing to write until the
    sweep has finished returning.
    """

    confident: tuple[ConfidentMatch, ...] = ()
    proposals: tuple[Proposal, ...] = ()
    already_mapped: int = 0
    searched: int = 0
    not_searched: int = 0
    cap: int = _CAP
    skipped_records: int = 0

    @property
    def capped(self) -> bool:
        """Did the cap actually bite? Reported, never silently applied."""
        return self.not_searched > 0

    def _with(self, outcome: str) -> tuple[Proposal, ...]:
        return tuple(p for p in self.proposals if p.outcome == outcome)

    @property
    def needs_decision(self) -> tuple[Proposal, ...]:
        return self._with("needs_decision")

    @property
    def no_match(self) -> tuple[Proposal, ...]:
        return self._with("no_match")

    @property
    def search_failed(self) -> tuple[Proposal, ...]:
        return self._with("search_failed")


def confident_match(
    series_title: str, hits: list[SvtSearchHit]
) -> SvtSearchHit | None:
    """The gate. Returns the one SVT hit safe to write, or None.

    Both rules from the module docstring, in `Resolver`'s own shape:
    filter to what qualifies, then refuse unless exactly one thing does.

    Rows sharing an `svt_id` are collapsed first. That is identity, not
    fuzz -- two rows with the same svtId are one programme, so collapsing
    them cannot change *which* programme is written; it only stops SVT
    repeating itself from reading as ambiguity.

    Never raises, and never returns a hit for a blank title on either
    side: an empty normalised form would otherwise match any other empty
    one.
    """
    wanted = normalise_title(series_title)
    if not wanted:
        return None

    qualifying: dict[str, SvtSearchHit] = {}
    for hit in hits or []:
        if not isinstance(hit, SvtSearchHit):
            continue
        if hit.typename not in _SERIES_TYPENAMES:
            continue
        if normalise_title(hit.name) != wanted:
            continue
        qualifying.setdefault(hit.svt_id, hit)

    if len(qualifying) != 1:
        return None
    return next(iter(qualifying.values()))


def _series_candidates(hits: list[SvtSearchHit]) -> tuple[Candidate, ...]:
    """Every programme SVT returned, deduplicated, for a human to pick from.

    The gate's own filter minus the equality rule: the operator is being
    shown what SVT offered *because* nothing matched exactly, so narrowing
    by name here would leave them an empty list and no way forward.
    """
    seen: dict[str, Candidate] = {}
    for hit in hits or []:
        if not isinstance(hit, SvtSearchHit):
            continue
        if hit.typename not in _SERIES_TYPENAMES:
            continue
        if not hit.svt_id or not hit.name:
            continue
        seen.setdefault(hit.svt_id, Candidate.of(hit))
    return tuple(seen.values())


@dataclass
class _Target:
    tvdb_id: int
    title: str


def _targets(series: list, mapped_tvdb_ids: set) -> tuple[list[_Target], int, int]:
    """Which Sonarr records this sweep will search SVT for.

    Skips anything already mapped -- there is nothing to propose for a
    series that has a row, and searching for it would be a request to SVT
    with no possible outcome -- and anything too malformed to act on. Both
    counts come back so the result page can account for the whole library
    rather than only the part that produced output.
    """
    targets: list[_Target] = []
    already = 0
    skipped = 0
    for record in series or []:
        if not isinstance(record, dict):
            skipped += 1
            continue
        title = record.get("title")
        tvdb_raw = record.get("tvdbId")
        if not isinstance(title, str) or not title.strip() or tvdb_raw is None:
            skipped += 1
            continue
        try:
            tvdb_id = int(tvdb_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if tvdb_id in mapped_tvdb_ids:
            already += 1
            continue
        targets.append(_Target(tvdb_id=tvdb_id, title=title))
    return targets, already, skipped


async def sweep_for_mappings(
    sonarr,
    svt,
    *,
    mapped_tvdb_ids: set,
    concurrency: int = _CONCURRENCY,
    cap: int = _CAP,
) -> Sweep:
    """Walk Sonarr's library and propose SVT matches for the unmapped ones.

    Read-only. It calls exactly two things -- `sonarr.all_series()` once,
    and `svt.search_series(title)` at most `cap` times -- and returns a
    value. It never writes, never touches the resolver, the job store or
    the download pipeline, and never asks SVT for an episode list.

    A failure from Sonarr propagates: without the library there is nothing
    to sweep, and more importantly the caller must not write anything it
    half-learned. A failure from one SVT search does not -- it becomes that
    series' `search_failed` proposal, because one unreachable search is no
    reason to discard every other result in the run.
    """
    series = await sonarr.all_series()
    targets, already_mapped, skipped = _targets(series, set(mapped_tvdb_ids or ()))

    searchable = targets[: max(cap, 0)]
    not_searched = len(targets) - len(searchable)
    if not_searched:
        log.info(
            "mapping sweep capped at %d searches; %d unmapped series were "
            "not searched this run",
            cap, not_searched,
        )

    confident: list[ConfidentMatch] = []
    proposals: list[Proposal] = []
    gate = asyncio.Semaphore(max(concurrency, 1))

    async def search(target: _Target):
        async with gate:
            return await svt.search_series(target.title)

    results = await asyncio.gather(
        *(search(t) for t in searchable), return_exceptions=True
    )

    for target, result in zip(searchable, results):
        if isinstance(result, BaseException):
            log.warning(
                "SVT search failed for %r during the mapping sweep: %s",
                target.title, result,
            )
            proposals.append(Proposal(
                tvdb_id=target.tvdb_id,
                series_title=target.title,
                outcome="search_failed",
                reason=f"SVT search failed: {result}",
            ))
            continue

        best = confident_match(target.title, result)
        if best is not None:
            confident.append(ConfidentMatch(
                tvdb_id=target.tvdb_id,
                # Sonarr's spelling, never SVT's. This is the permanent
                # filename; the whole page exists for this guarantee.
                series_title=target.title,
                svt_series_id=best.svt_id,
                svt_slug=derive_slug(best.name),
                svt_name=best.name,
            ))
            continue

        candidates = _series_candidates(result)
        if candidates:
            proposals.append(Proposal(
                tvdb_id=target.tvdb_id,
                series_title=target.title,
                outcome="needs_decision",
                reason=(
                    # Two different situations, and "SVT offered 1
                    # programme, but not exactly one matches" reads as a
                    # contradiction rather than as the near miss it is.
                    (
                        "SVT offered one programme, but its name is not "
                        "identical to this title."
                        if len(candidates) == 1
                        else f"SVT offered {len(candidates)} programmes, and "
                        "no single one of them has a name identical to this "
                        "title."
                    )
                    + " Pick one, or leave it unmapped."
                ),
                candidates=candidates,
            ))
        else:
            proposals.append(Proposal(
                tvdb_id=target.tvdb_id,
                series_title=target.title,
                outcome="no_match",
                reason=(
                    "SVT returned no programme for this title. It may not be "
                    "on SVT Play, or SVT may spell it differently -- try the "
                    "manual search with a shorter query."
                ),
            ))

    return Sweep(
        confident=tuple(confident),
        proposals=tuple(proposals),
        already_mapped=already_mapped,
        searched=len(searchable),
        not_searched=not_searched,
        cap=cap,
        skipped_records=skipped,
    )


# --- The CLI: the same sweep, reported rather than written -----------
#
# `svtplay-arr-suggest-mappings` (declared in pyproject.toml) used to call
# a second, separate implementation of this idea -- `suggest_mappings` in
# mappings.py -- which took the first SVT hit of a series typename with no
# confidence check at all, and emitted rows with a blank `svt_slug` for a
# human to fill in. That is precisely the guess this module exists to
# refuse, and two implementations of one idea drifting apart is this
# codebase's most persistent defect, so it was deleted rather than fixed
# in parallel.
#
# The entry point stays, pointed here, because it is the only way to see
# what a sweep would do without a browser -- and, unlike the config page,
# it still writes nothing at all. Same gate, same slug derivation, same
# `source: auto` marker: paste its output and you get byte-for-byte the
# rows the page would have written.


def confident_rows(sweep: Sweep) -> list[dict]:
    """The confident matches as mappings.yaml rows, ready to paste.

    Identical in shape and content to what the config page writes for the
    same sweep -- including the derived slug and the `source: auto` marker,
    so a row pasted from here is not silently a different kind of row from
    one the page wrote.
    """
    return [
        {
            "tvdb_id": m.tvdb_id,
            "svt_series_id": m.svt_series_id,
            "svt_slug": m.svt_slug,
            "series_title": m.series_title,
            "source": SOURCE_AUTO,
        }
        for m in sweep.confident
    ]


def format_report(sweep: Sweep) -> str:
    """The human half of the CLI's output: everything not safe to paste.

    Deliberately separate from `confident_rows`, and written to stderr by
    `main`, so `... > rows.yaml` captures only rows a human could paste
    without re-reading them -- and the part that needs a decision cannot be
    swept into a file by a shell redirect.
    """
    lines = [
        f"{len(sweep.confident)} confident match(es) printed above; "
        f"{len(sweep.needs_decision)} need a decision, "
        f"{len(sweep.no_match)} had no SVT match, "
        f"{len(sweep.search_failed)} could not be searched. "
        f"{sweep.already_mapped} series were already mapped and not searched.",
    ]
    if sweep.capped:
        lines.append(
            f"Stopped at the per-run limit of {sweep.cap} SVT searches: "
            f"{sweep.not_searched} unmapped series were NOT searched. Map "
            "what is above, then run again to continue."
        )
    if sweep.skipped_records:
        lines.append(
            f"{sweep.skipped_records} Sonarr record(s) had no usable title "
            "or TVDB id and were skipped."
        )
    for proposal in sweep.needs_decision:
        lines.append(
            f"\nNEEDS A DECISION  {proposal.series_title} "
            f"(tvdbId {proposal.tvdb_id})\n  {proposal.reason}"
        )
        for c in proposal.candidates:
            lines.append(f"    {c.svt_id}  {c.slug}  {c.name}")
    for proposal in sweep.no_match:
        lines.append(
            f"\nNO SVT MATCH      {proposal.series_title} "
            f"(tvdbId {proposal.tvdb_id})"
        )
    for proposal in sweep.search_failed:
        lines.append(
            f"\nSEARCH FAILED     {proposal.series_title} "
            f"(tvdbId {proposal.tvdb_id})\n  {proposal.reason}"
        )
    return "\n".join(lines)


def main() -> None:
    """CLI: print what a Find mappings sweep would write. Writes nothing.

    Entry point `svtplay-arr-suggest-mappings`. Confident rows go to
    stdout as pasteable YAML; everything that needs a human goes to
    stderr. The config page is what actually writes -- this is the
    headless preview of the same decision, made by the same gate.
    """
    import asyncio
    import os
    import sys
    from pathlib import Path

    import httpx
    import yaml as _yaml

    from svtplay_arr.config import Settings
    from svtplay_arr.mappings import MappingTable
    from svtplay_arr.sonarr import SonarrClient
    from svtplay_arr.svt.client import SvtClient

    settings = Settings.load(
        Path(os.environ.get("SVTPLAY_ARR_CONFIG", "/etc/svtplay-arr/config.yaml"))
    )

    # Already-mapped series are skipped here exactly as they are on the
    # page: there is nothing to propose for a series that has a row, and
    # searching for it would be a request to SVT with no possible outcome.
    # A mappings file that will not parse is not a reason to re-search the
    # whole library, so it stops the run rather than being read as empty.
    try:
        mapped = {m.tvdb_id for m in MappingTable.load(settings.mappings_file).all()}
    except FileNotFoundError:
        mapped = set()
    except ValueError as exc:
        print(f"{settings.mappings_file} is invalid: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    async def run() -> Sweep:
        async with httpx.AsyncClient(timeout=30.0) as http:
            return await sweep_for_mappings(
                SonarrClient(settings.sonarr_url, settings.sonarr_api_key, http),
                SvtClient(http, settings.svt_ua),
                mapped_tvdb_ids=mapped,
            )

    sweep = asyncio.run(run())
    _yaml.safe_dump(
        {"series": confident_rows(sweep)},
        sys.stdout,
        allow_unicode=True,
        sort_keys=False,
    )
    print(
        "\n# Rows above matched exactly and unambiguously; check them and "
        "paste into mappings.yaml. Nothing was written.\n",
        file=sys.stderr,
    )
    print(format_report(sweep), file=sys.stderr)
