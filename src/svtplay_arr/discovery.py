"""Proposing tvdb -> SVT mappings, and deciding which ones may be written.

This is the only module allowed to decide that a mapping is safe to write
without a human confirming it, and it is deliberately built to say "no".
It mirrors `resolver.py` one level up: `Resolver` refuses to guess *which
episode* a Sonarr request means, and returns None on any doubt, because
Sonarr runs with renameEpisodes=False and a wrong answer becomes a
permanent filename in the media library. A wrong *series* mapping is the
same error one level larger -- it makes every episode of that show wrong --
so the rule is the same rule.

**The gate decides on episodes, not on titles.**

It used to decide on the title: the Sonarr series title and the SVT
programme name had to be identical after normalisation, and exactly one SVT
programme had to qualify. That is a weak proxy, and it failed in both
directions.

* It *missed* real matches. TVDB routinely carries the English title for a
  Swedish show, and `Married at First Sight Sweden` will never equal `Gift
  vid första ögonkastet`, so a correct mapping was refused and left for a
  human forever.
* It nearly *wrote wrong ones*. `Vem vet mest?` and `Vem vet mest? (2021)`
  both matched one SVT programme; two separate fixes were needed to stop
  that becoming two mappings on one slug, and the gate underneath was still
  only as strong as a string comparison.

The project already contained the right machine. `matching.episode_matches`
decides whether an SVT episode corresponds to a Sonarr episode: it must be
available, its publication date must sit within `air_date_tolerance_days` of
Sonarr's air date, and SVT's *ordinal* must equal the episode number (never
SVT's season number, which disagrees with TVDB's). So the sweep pulls the
candidate's episode list and Sonarr's episode list and counts how many
episodes correspond under that same rule.

A genuine match produces a run of agreeing episodes. A different programme
with the same name produces none. **The title stops being evidence and
becomes only a search query.**

That is strictly better in both directions at once: it maps shows whose
titles differ, and it refuses same-named shows the old gate would have
accepted.

Reusing `matching.episode_matches` rather than restating it is not
tidiness. If the sweep corroborated under even a slightly different rule
from the one the resolver later matches under, it would write mappings the
resolver then refuses -- or, worse, write them on evidence the resolver
would have rejected. `tests/test_matching.py` pins that this module and
`resolver.py` hold the same function object, that each actually calls it,
and that neither does the air-date arithmetic itself.

**What the gate requires, and why those numbers**

A mapping is written without confirmation only when *all* of:

1. exactly one candidate corroborates, **and**
2. it corroborates on at least `ACCEPT_MIN_EPISODES` (3) uniquely-matching
   episodes, **and**
3. every other candidate that was checked corroborates on **zero**.

Three, because two is reachable by coincidence: a weekly show broadcast in
the same slot as another weekly show shares an air date at ordinal 1 and
again at ordinal 2 with nothing but the schedule in common. Three
consecutive agreements of (available, date-within-tolerance, same ordinal)
is not a schedule artefact.

**The short-run fallback.** A series SVT has only just started publishing
cannot reach three, and refusing it forever is the old gate's failure mode
wearing a new hat. So when fewer than 3 episodes are *available to compare
at all*, every one of them must corroborate and there must be at least
`SHORT_RUN_MIN_EPISODES` (2). Never one: a single shared air date at ordinal
1 is exactly the coincidence a weekly show produces.

**No evidence is not confidence.** A series Sonarr knows about that has not
aired, or that SVT has not published, yields nothing to compare -- so it is
surfaced for a decision and never written. There is no evidence, so there
is no confidence.

Everything not written is surfaced *with its evidence*: each candidate
carries how many episodes corroborated out of how many could be compared,
because "2 of 8 episodes matched" tells the person deciding far more than
"needs a decision".

**What normalisation is still for.** Ranking and deduplication, and nothing
else. `normalise_title` (casefold, collapse whitespace) is applied to both
sides; `normalise_sonarr_title` additionally strips one trailing
parenthesised year and is applied to the Sonarr side only, because carrying
TVDB's disambiguating year (`Solsidan (2019)`) is a fact about Sonarr's
data, not a statement that a year in a title is noise. Neither folds
diacritics -- Swedish titles are distinguished by å/ä/ö. None of this
decides anything any more: it picks which few candidates are worth spending
an episode-list fetch on, and collapses queries that would return the same
results.

**Candidate generation is deliberately wide.** Sonarr's own title *and* its
`alternateTitles` are searched (TVDB usually carries the original-language
title there, which is often exactly SVT's name), and every programme
returned is a candidate. Widening the search is now safe precisely because
the title no longer decides anything.

**`already_claimed` stays.** Two Sonarr series pointing at one SVT
programme answers a search for either with episodes of the same show, and
that is permanent. It is a separate guard from the gate and still correct.
The old worry that "first in Sonarr's list wins" was an arbitrary tiebreak
is now answered by evidence instead: whichever series the episodes actually
corroborate wins, and if both corroborate, neither is written.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, replace

from svtplay_arr.config import Settings
from svtplay_arr.matching import episode_matches
from svtplay_arr.models import SOURCE_AUTO, SonarrEpisode, SvtEpisode, SvtSearchHit
from svtplay_arr.svt.client import derive_slug

log = logging.getLogger(__name__)

# The SVT search item types that denote a programme (a thing with an
# episode list at a slug), as opposed to a single video or a clip that
# merely carries the programme's name. It is the only SVT knowledge in this
# module, and it is a filter, not a choice.
_SERIES_TYPENAMES = ("TvSeries", "TvShow")

# --- how much one "Find mappings" click may cost SVT -----------------
#
# Corroboration is materially more expensive than the title gate was: an
# episode-list fetch *per candidate* on top of a search *per query*, rather
# than one search per series. Against an unofficial API that matters, so
# every axis is bounded and every bound is reported rather than silently
# applied.
#
#   searches per series   <= _QUERIES_PER_SERIES        (title + alternates)
#   episode lists/series  <= _CORROBORATE_PER_SERIES    (top-ranked only)
#   series per run        <= _CAP
#   SVT requests per run  <= _REQUEST_BUDGET            (the hard ceiling)
#
# The budget is the one that actually binds a large first run, and it is
# deliberately the *reported* kind of limit: a run that stops early says so
# (`Sweep.budget_exhausted`), and because a run writes its confident rows,
# the next run skips them and continues where this one stopped.
_CONCURRENCY = 4
_CAP = 200
_QUERIES_PER_SERIES = 3
_CORROBORATE_PER_SERIES = 3
_REQUEST_BUDGET = 600

# The evidence thresholds. See the module docstring for why these numbers
# and not others; they are named rather than inlined because the tests that
# are the safety argument for this module refer to them by name.
ACCEPT_MIN_EPISODES = 3
SHORT_RUN_MIN_EPISODES = 2

# `Settings`' own default, never a literal: the sweep must corroborate at
# the tolerance the resolver will later match at, and a `1` written here
# would be a second copy of an operator-visible setting.
_DEFAULT_TOLERANCE_DAYS = Settings.air_date_tolerance_days

# Sonarr's library titles carry TVDB's disambiguating year as a trailing
# `(2019)`; SVT's names do not. Anchored at the end, so a year that is part
# of the title itself is untouched.
_TRAILING_YEAR = re.compile(r"\s*\(\d{4}\)\s*$")


def normalise_title(name: str) -> str:
    """The comparison form applied to **both** sides.

    Casefold and collapse whitespace. Nothing else.

    This is now used for *ranking and deduplication only* -- which
    candidates are worth an episode-list fetch, and which queries would
    return the same thing twice. It no longer decides whether anything is
    written; the episodes decide that.

    Diacritics are preserved deliberately: Swedish titles are distinguished
    by å/ä/ö, and folding them together would rank a genuinely different
    show as an exact match. A trailing parenthesised year is preserved too,
    which is `normalise_sonarr_title`'s entire subject.
    """
    return " ".join(str(name or "").strip().casefold().split())


def normalise_sonarr_title(title: str) -> str:
    """`normalise_title`, plus stripping one trailing parenthesised year.

    Applied to the **Sonarr** side only, and that asymmetry is the point.
    Sonarr's library titles carry TVDB's disambiguating year (`Solsidan
    (2019)`) where SVT's programme names do not, so stripping it is a fact
    about *Sonarr's* data -- not a general claim that a year in a title is
    noise. Stripping it from SVT's name as well made `Big Brother (2019)`
    and `Big Brother (2020)` compare equal, collapsing the very distinction
    TVDB adds the year to express.

    That collapse can no longer write a wrong row on its own -- the
    episodes decide -- but it would still rank the wrong programme first
    and spend the run's corroboration budget on it, so the asymmetry stays.
    """
    return normalise_title(_TRAILING_YEAR.sub("", str(title or "").strip()))


def _qualifies(hit: object) -> bool:
    """Is this search hit a programme this module could ever map to?

    The one filter over SVT search hits. It had a copy in the gate and a
    copy in the candidate list once, and the copies disagreed, so a
    malformed hit could become a confident row and then make `add_mappings`
    refuse the *entire* batch over it. Two filters that must agree are one
    function, not two that happen to match today.
    """
    return (
        isinstance(hit, SvtSearchHit)
        and hit.typename in _SERIES_TYPENAMES
        and bool(hit.svt_id)
        and bool(hit.name)
    )


@dataclass(frozen=True)
class Evidence:
    """What this candidate's episodes say, and whether that is enough.

    `matched` counts Sonarr episodes that correspond to **exactly one** SVT
    episode which in turn corresponds to **exactly one** Sonarr episode --
    a strictly one-to-one agreement, so a Sonarr special dated alongside the
    run, or an SVT rerun listed twice, can never inflate the count. Both
    directions of "exactly one" are `Resolver`'s own
    `if len(candidates) != 1: return None`, applied to series-level
    evidence.

    `comparable` counts the SVT episodes that could have matched at all:
    published, available, and at an ordinal Sonarr has an aired episode
    for. It is the denominator the short-run fallback keys off, and it is
    SVT-side on purpose -- what bounds the evidence is what SVT has
    actually published, not how many seasons TVDB knows about.

    `error` is set when the evidence could not be gathered (SVT's episode
    list was unreachable). That is doubt, not zero, and it is why
    `corroborates` is False and why `corroborated_match` refuses the whole
    series rather than writing whatever the other candidates said.
    """

    matched: int = 0
    comparable: int = 0
    error: str | None = None

    @property
    def corroborates(self) -> bool:
        if self.error is not None:
            return False
        if self.comparable <= 0:
            # Nothing aired, or nothing published: no evidence, so no
            # confidence. Never a write.
            return False
        if self.comparable >= ACCEPT_MIN_EPISODES:
            return self.matched >= ACCEPT_MIN_EPISODES
        # The short run: everything that could be compared must agree, and
        # one agreement is never enough -- a single shared air date at
        # ordinal 1 is a coincidence a weekly show produces.
        return (
            self.matched == self.comparable
            and self.matched >= SHORT_RUN_MIN_EPISODES
        )

    def describe(self) -> str:
        """One sentence saying why, for the person deciding.

        "2 of 8 episodes matched" is the whole point of surfacing anything
        at all; "needs a decision" on its own tells them nothing they did
        not already know from the row being there.
        """
        if self.error is not None:
            return f"SVT's episode list could not be read: {self.error}"
        if self.comparable == 0:
            return (
                "no episodes to compare -- SVT has published nothing that "
                "Sonarr has an aired episode for"
            )
        plural = "" if self.comparable == 1 else "s"
        note = f"{self.matched} of {self.comparable} episode{plural} matched"
        if self.corroborates:
            return note
        if self.comparable >= ACCEPT_MIN_EPISODES:
            return f"{note}; at least {ACCEPT_MIN_EPISODES} are needed"
        return (
            f"{note}; with only {self.comparable} to compare, all of them "
            f"must match and at least {SHORT_RUN_MIN_EPISODES} are needed"
        )


@dataclass(frozen=True)
class Candidate:
    """One SVT programme offered as a possible match, ready to be written.

    Carries the slug already, so accepting a suggestion is one click with
    nothing left to transcribe off an SVT Play URL. `slug` is
    `derive_slug`'s output -- a suggestion, never a source of truth, and
    the accept path re-renders it in a form the operator can see.

    `evidence` is None until the sweep spends a request corroborating this
    candidate, and stays None for candidates that ranked below the per-
    series corroboration limit or that the run's request budget did not
    reach. None means "not looked at", which is a different statement from
    `Evidence(matched=0)` ("looked at, and the episodes disagree"), and the
    page says which.
    """

    svt_id: str
    name: str
    slug: str
    evidence: Evidence | None = None

    @classmethod
    def of(cls, hit: SvtSearchHit) -> "Candidate":
        return cls(svt_id=hit.svt_id, name=hit.name, slug=derive_slug(hit.name))

    def note(self) -> str:
        """What to show beside this candidate. Never blank."""
        if self.evidence is None:
            return "not checked against SVT's episode list this run"
        return self.evidence.describe()


def corroborate(
    sonarr_episodes: list[SonarrEpisode],
    svt_episodes: list[SvtEpisode],
    *,
    tolerance_days: int,
) -> Evidence:
    """Count how far this SVT programme's episodes agree with this series'.

    Pure, and the only place series-level evidence is computed. It calls
    `matching.episode_matches` -- the resolver's own rule -- and never
    restates it, so the sweep cannot corroborate under a rule the resolver
    would not match under.

    Sonarr season 0 is excluded. A special dated alongside the run
    satisfies both signals (same ordinal, same air date) and would count as
    evidence for a programme it says nothing about; `Resolver._recent_for`
    excludes it for the same reason one level down.
    """
    aired = [
        se
        for se in sonarr_episodes or ()
        if isinstance(se, SonarrEpisode)
        and se.season > 0
        and se.air_date is not None
    ]
    listed = [
        e
        for e in svt_episodes or ()
        if isinstance(e, SvtEpisode)
        and e.available
        and e.ordinal is not None
        and e.published is not None
    ]

    # The denominator: SVT episodes that could have matched something at
    # all. Bounded by what SVT has published, which is what makes the
    # short-run fallback reachable for a returning series whose back
    # catalogue Sonarr knows about but SVT no longer lists.
    numbers = {se.episode for se in aired}
    comparable = sum(1 for e in listed if e.ordinal in numbers)

    # The numerator: strictly one-to-one agreements. A Sonarr episode with
    # two possible SVT partners is ambiguity, not evidence; an SVT episode
    # claimed by two Sonarr episodes is the same ambiguity from the other
    # side, and counting it twice is how a coincidence would be inflated
    # into a threshold.
    partners: dict[str, int] = {}
    for se in aired:
        found = [
            e
            for e in listed
            if episode_matches(
                e, se.air_date, se.episode, tolerance_days=tolerance_days
            )
        ]
        if len(found) != 1:
            continue
        partners[found[0].svt_id] = partners.get(found[0].svt_id, 0) + 1
    matched = sum(1 for count in partners.values() if count == 1)

    return Evidence(matched=matched, comparable=comparable)


def corroborated_match(candidates: list[Candidate]) -> Candidate | None:
    """The gate. Returns the one candidate safe to write, or None.

    `candidates` are the ones actually *checked* this run, each carrying
    its `Evidence`. `Resolver`'s shape, verbatim in spirit: filter to what
    qualifies, then refuse unless exactly one thing does.

    Three ways to get None, and each is a real situation rather than a
    defensive flourish:

    * **Any candidate's evidence is missing.** An SVT outage while checking
      one candidate means the answer for that candidate is unknown, and an
      unknown rival is not a refuted rival. Writing on the others' evidence
      would be deciding a question nobody answered.
    * **Not exactly one corroborates.** Zero is no evidence. Two is two
      programmes whose episodes both agree with this series, which is
      precisely the case a human has to look at -- and the case the old
      title gate resolved by picking whichever came first.
    * **A non-winner matched anything at all.** The requirement is not
      "the best one wins", it is "every other one is refuted". A rival with
      two agreeing episodes has not been refuted, it has been out-scored,
      and out-scoring is the reasoning that writes permanent filenames for
      the wrong show.

    Never raises.
    """
    checked = list(candidates or ())
    if not checked:
        return None
    if any(c.evidence is None or c.evidence.error is not None for c in checked):
        return None

    corroborating = [c for c in checked if c.evidence.corroborates]
    if len(corroborating) != 1:
        return None
    winner = corroborating[0]
    if any(c.evidence.matched > 0 for c in checked if c is not winner):
        return None
    return winner


@dataclass(frozen=True)
class ConfidentMatch:
    """A row the gate is willing to write without anyone confirming it.

    `series_title` is Sonarr's own spelling, verbatim, and never SVT's --
    it becomes the permanent filename in the library, exactly as on the
    manual path. `svt_name` is carried only so the result page can show
    what it matched against; nothing writes it. `evidence` likewise: it is
    what makes the page able to say *why* a row nobody confirmed exists.
    """

    tvdb_id: int
    series_title: str
    svt_series_id: str
    svt_slug: str
    svt_name: str
    evidence: Evidence = Evidence()


@dataclass(frozen=True)
class Proposal:
    """A series the gate refused to decide, and why.

    `outcome` is one of:

    * `needs_decision` -- SVT returned programmes and their episodes were
      checked, but no single one corroborated on its own. `candidates`
      holds them, each carrying its evidence and each one click away from
      being written.
    * `no_match` -- SVT returned nothing usable for any of this series'
      titles. `candidates` is empty; the operator needs the manual search.
    * `search_failed` -- every SVT search for this series errored, so
      nothing is known about it. `reason` carries the failure.
    * `check_failed` -- candidates were found, but the evidence could not
      be gathered: SVT's episode list was unreachable, Sonarr's episode
      list was, or one of this series' searches failed and the candidate
      set is therefore incomplete. Says nothing about whether a match
      exists.
    * `already_claimed` -- the episodes corroborated one candidate, but
      that SVT programme is already mapped to another series (in the file,
      or earlier in this same batch). `candidates` holds it, so a human can
      still accept it deliberately.
    * `budget_exhausted` -- the run's SVT request budget ran out before
      this series could be checked. Nothing was learned about it; run again
      to continue.
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
    what makes "an SVT or Sonarr outage mid-sweep cannot write a partial
    file" structural rather than a promise -- there is nothing to write
    until the sweep has finished returning.
    """

    confident: tuple[ConfidentMatch, ...] = ()
    proposals: tuple[Proposal, ...] = ()
    already_mapped: int = 0
    searched: int = 0
    not_searched: int = 0
    cap: int = _CAP
    skipped_records: int = 0
    # SVT requests this run actually issued, against the ceiling it was
    # given. Reported whether or not the ceiling bit, because "how much did
    # that click cost SVT" is the question the budget exists to answer.
    requests_used: int = 0
    request_budget: int = _REQUEST_BUDGET
    requests_denied: int = 0

    @property
    def capped(self) -> bool:
        """Did the per-run series cap bite? Reported, never silently applied."""
        return self.not_searched > 0

    @property
    def budget_exhausted(self) -> bool:
        """Did the run stop short of what it set out to do?

        A partial sweep reported as a complete one is the failure mode that
        matters here: the operator concludes their library has no more
        mappings to find, when in fact the run stopped asking.
        """
        return self.requests_denied > 0

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

    @property
    def check_failed(self) -> tuple[Proposal, ...]:
        return self._with("check_failed")

    @property
    def already_claimed(self) -> tuple[Proposal, ...]:
        return self._with("already_claimed")

    @property
    def out_of_budget(self) -> tuple[Proposal, ...]:
        return self._with("budget_exhausted")


def _ranked_candidates(
    hits: list, wanted: frozenset[str]
) -> tuple[Candidate, ...]:
    """Every programme SVT returned, deduplicated and ordered by promise.

    Order matters for one reason only: it decides which few candidates are
    worth spending an episode-list fetch on. A name identical to one of the
    series' titles goes first -- not because that makes it right, but
    because if two programmes share a name it is those two whose episodes
    most need comparing. Everything else keeps SVT's own order, which is
    relevance-ranked.

    Nothing is filtered out by name. The operator is shown what SVT offered
    *because* the gate did not decide, and narrowing by name here would
    leave them an empty list and no way forward -- and would reintroduce
    the exact-title rule as a hidden precondition.
    """
    seen: dict[str, Candidate] = {}
    ranks: dict[str, int] = {}
    for hit in hits or []:
        if not _qualifies(hit):
            continue
        if hit.svt_id in seen:
            continue
        seen[hit.svt_id] = Candidate.of(hit)
        ranks[hit.svt_id] = 0 if normalise_title(hit.name) in wanted else 1
    ordered = sorted(seen.values(), key=lambda c: ranks[c.svt_id])
    return tuple(ordered)


@dataclass(frozen=True)
class _Target:
    tvdb_id: int
    series_id: int
    title: str
    queries: tuple[str, ...]
    wanted: frozenset[str]


def _queries_for(title: str, alternates: list, limit: int) -> tuple[str, ...]:
    """The SVT searches to run for one Sonarr series, deduplicated.

    Sonarr's own title first, then its `alternateTitles`. TVDB usually
    carries the original-language title there, which for a Swedish show is
    very often exactly SVT's name -- and the case the title gate could
    never solve (`Married at First Sight Sweden` for `Gift vid första
    ögonkastet`) is solved by simply asking for both.

    Deduplicated on the Sonarr-side normal form, so `Solsidan` and
    `Solsidan (2019)` cost one search rather than two, and the many
    alternates that repeat a title verbatim cost nothing. Capped, because
    a show can carry a dozen alternates and each one is a request.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in [title, *(alternates or ())]:
        text = str(raw or "").strip()
        if not text:
            continue
        key = normalise_sonarr_title(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= max(limit, 1):
            break
    return tuple(out)


def _alternate_titles(record: dict) -> list[str]:
    """The `title` of each `alternateTitles` entry Sonarr returned.

    Defensive about shape rather than trusting it: this is a third-party
    payload, and a sweep must not fall over because one series carries an
    entry in a form nobody expected.
    """
    raw = record.get("alternateTitles")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            title = entry.get("title")
        elif isinstance(entry, str):
            title = entry
        else:
            continue
        if isinstance(title, str) and title.strip():
            out.append(title)
    return out


def _targets(
    series: list, mapped_tvdb_ids: set, queries_per_series: int
) -> tuple[list[_Target], int, int]:
    """Which Sonarr records this sweep will look at, and what it will ask.

    Skips anything already mapped -- there is nothing to propose for a
    series that has a row, and searching for it would be a request to SVT
    with no possible outcome -- and anything too malformed to act on. Both
    counts come back so the result page can account for the whole library
    rather than only the part that produced output.

    Sonarr's internal `id` is required now, not only its `tvdbId`:
    corroboration needs `sonarr.episodes(series_id)`, and a record without
    one cannot be checked, so it is a skipped record rather than a series
    the sweep would search and then be unable to decide.
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
        series_raw = record.get("id")
        if not isinstance(title, str) or not title.strip() or tvdb_raw is None:
            skipped += 1
            continue
        try:
            tvdb_id = int(tvdb_raw)
            series_id = int(series_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if tvdb_id in mapped_tvdb_ids:
            already += 1
            continue
        queries = _queries_for(
            title, _alternate_titles(record), queries_per_series
        )
        if not queries:
            skipped += 1
            continue
        targets.append(_Target(
            tvdb_id=tvdb_id,
            series_id=series_id,
            title=title,
            queries=queries,
            # Every form a candidate's name could be identical to, for
            # ranking only. The Sonarr-side strip on our own titles, the
            # shared form on nothing else: a year in an SVT programme name
            # is part of the name.
            wanted=frozenset(normalise_sonarr_title(q) for q in queries),
        ))
    return targets, already, skipped


class _Budget:
    """The run's SVT request ceiling, spent one request at a time.

    Deliberately not a semaphore or a rate limit: it does not slow anything
    down, it *stops*. And it records what it refused, so the sweep can say
    so rather than returning a short answer that looks complete.

    Safe as a plain counter because everything that spends from it runs on
    one event loop thread; `take` is not a coroutine and cannot be
    interleaved.
    """

    def __init__(self, limit: int):
        self.limit = max(int(limit), 0)
        self.used = 0
        self.denied = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            self.denied += 1
            return False
        self.used += 1
        return True


@dataclass
class _Evaluated:
    """One series' raw findings, before the batch-wide rules are applied."""

    target: _Target
    candidates: tuple[Candidate, ...] = ()
    checked: tuple[Candidate, ...] = ()
    search_error: str | None = None
    sonarr_error: str | None = None
    ran_out_of_budget: bool = False


async def sweep_for_mappings(
    sonarr,
    svt,
    *,
    existing_mappings,
    concurrency: int = _CONCURRENCY,
    cap: int = _CAP,
    tolerance_days: int | None = None,
    queries_per_series: int = _QUERIES_PER_SERIES,
    corroborate_per_series: int = _CORROBORATE_PER_SERIES,
    request_budget: int = _REQUEST_BUDGET,
) -> Sweep:
    """Walk Sonarr's library and propose SVT matches for the unmapped ones.

    Read-only, and it returns a value rather than writing one. It calls
    `sonarr.all_series()` once, `svt.search_series(query)` for each of a
    series' deduplicated titles, `sonarr.episodes(series_id)` for each
    series with a candidate worth checking, and `svt.list_episodes(slug)`
    for each candidate it checks. It never writes, never touches the job
    store or the download pipeline, and never asks SVT to resolve a stream.

    Asking SVT for episode lists is new, and is the whole point: the gate
    decides on episodes now, not on titles. It is still the *matching*
    read the resolver does, through the same client method, and it still
    changes nothing about what the resolver returns for any existing
    mapping.

    A failure from `all_series` propagates: without the library there is
    nothing to sweep, and the caller must not write anything it
    half-learned. Failures below that do not -- they become that series'
    own proposal, because one unreachable request is no reason to discard
    every other result in the run.

    `tolerance_days` is the resolver's configured `air_date_tolerance_days`.
    Corroborating at a different tolerance from the one the resolver will
    later match at is exactly the drift this module exists to avoid, so the
    caller passes the value the service booted with; None falls back to
    `Settings`' own default rather than to a literal.

    `existing_mappings` is the current table (an iterable of
    `models.Mapping`). Deliberately one argument rather than a set of tvdb
    ids beside a set of SVT ids: both facts are derived from it here, so a
    caller cannot supply one and forget the other. That is the shape of
    defect this codebase keeps hitting, and the two checks it feeds -- skip
    what is mapped, refuse to claim a programme twice -- are both
    load-bearing.
    """
    if tolerance_days is None:
        tolerance_days = _DEFAULT_TOLERANCE_DAYS

    existing = list(existing_mappings or ())
    series = await sonarr.all_series()
    targets, already_mapped, skipped = _targets(
        series, {m.tvdb_id for m in existing}, queries_per_series
    )

    searchable = targets[: max(cap, 0)]
    not_searched = len(targets) - len(searchable)
    if not_searched:
        log.info(
            "mapping sweep capped at %d series; %d unmapped series were "
            "not looked at this run",
            cap, not_searched,
        )

    budget = _Budget(request_budget)
    gate = asyncio.Semaphore(max(concurrency, 1))

    async def evaluate(target: _Target) -> _Evaluated:
        """One series, end to end: search, rank, corroborate.

        Search and corroboration are one coroutine per series rather than
        two batched phases, so the run's budget is spent series by series
        in Sonarr's own order. When it runs out, what is left unfinished is
        a suffix of the library -- which is what makes "run it again to
        continue" true rather than a hope.
        """
        hits: list = []
        failures: list[str] = []
        for query in target.queries:
            if not budget.take():
                return _Evaluated(target, ran_out_of_budget=True)
            async with gate:
                try:
                    hits.extend(await svt.search_series(query) or [])
                except Exception as exc:
                    log.warning(
                        "SVT search for %r failed during the mapping sweep: %s",
                        query, exc,
                    )
                    failures.append(str(exc))

        candidates = _ranked_candidates(hits, target.wanted)
        if failures and not candidates:
            return _Evaluated(target, search_error="; ".join(failures))
        if not candidates:
            return _Evaluated(target)
        if failures:
            # Some candidates, but not all of them: the set is incomplete,
            # so "exactly one corroborates and the rest do not" cannot be
            # established. Surfaced with what was found, never written.
            return _Evaluated(
                target, candidates=candidates,
                search_error="; ".join(failures),
            )

        try:
            sonarr_episodes = await sonarr.episodes(target.series_id)
        except Exception as exc:
            log.warning(
                "Sonarr episode list for series %s failed during the mapping "
                "sweep: %s", target.series_id, exc,
            )
            return _Evaluated(
                target, candidates=candidates, sonarr_error=str(exc)
            )

        checked: list[Candidate] = []
        for candidate in candidates[: max(corroborate_per_series, 0)]:
            if not budget.take():
                return _Evaluated(
                    target, candidates=candidates,
                    checked=tuple(checked), ran_out_of_budget=True,
                )
            async with gate:
                try:
                    svt_episodes = await svt.list_episodes(candidate.slug)
                except Exception as exc:
                    # Not zero: unknown. The gate treats it as doubt about
                    # the whole series, which is what stops an SVT outage
                    # part-way through a candidate list writing a row on
                    # whichever candidate happened to answer.
                    log.warning(
                        "SVT episode list for %r failed during the mapping "
                        "sweep: %s", candidate.slug, exc,
                    )
                    checked.append(replace(
                        candidate, evidence=Evidence(error=str(exc))
                    ))
                    continue
            checked.append(replace(candidate, evidence=corroborate(
                sonarr_episodes, svt_episodes, tolerance_days=tolerance_days
            )))

        return _Evaluated(
            target,
            candidates=candidates,
            checked=tuple(checked),
        )

    results = await asyncio.gather(
        *(evaluate(t) for t in searchable), return_exceptions=True
    )

    confident: list[ConfidentMatch] = []
    proposals: list[Proposal] = []
    # Which SVT programme is spoken for, and by which series. Seeded from
    # the file and extended as this batch fills up, so a collision is
    # caught whether the rival row is already on disk or three results
    # further down this same run.
    #
    # The gate cannot do this: it is evaluated per Sonarr series, over the
    # candidates for that one series, so it structurally cannot see a
    # second series corroborating the same programme. Two tvdb ids on one
    # slug means Sonarr asking for the reboot's S01E01 is answered with an
    # episode of the original -- permanently, because renameEpisodes=False.
    #
    # Title *and* tvdb id: the case this guard exists for is two series
    # whose titles differ only by a year, so naming the holder by title
    # alone is ambiguous exactly when it matters most.
    claimed: dict[str, tuple[str, int]] = {
        m.svt_series_id: (m.series_title, m.tvdb_id)
        for m in existing if m.svt_series_id
    }

    for target, result in zip(searchable, results):
        if isinstance(result, BaseException):
            # `evaluate` handles its own failures per request; reaching here
            # means something unforeseen. One series' surprise must not
            # discard the rest of the run's findings.
            log.warning(
                "mapping sweep failed for %r: %s", target.title, result,
                exc_info=isinstance(result, Exception),
            )
            proposals.append(_proposal(
                target, "check_failed",
                f"This series could not be checked: {result}",
            ))
            continue

        proposal_or_match = _decide(result, claimed)
        if isinstance(proposal_or_match, ConfidentMatch):
            claimed[proposal_or_match.svt_series_id] = (
                target.title, target.tvdb_id
            )
            confident.append(proposal_or_match)
        else:
            proposals.append(proposal_or_match)

    if budget.denied:
        log.info(
            "mapping sweep stopped at its budget of %d SVT requests; %d "
            "further requests were not made",
            budget.limit, budget.denied,
        )

    return Sweep(
        confident=tuple(confident),
        proposals=tuple(proposals),
        already_mapped=already_mapped,
        searched=len(searchable),
        not_searched=not_searched,
        cap=cap,
        skipped_records=skipped,
        requests_used=budget.used,
        request_budget=budget.limit,
        requests_denied=budget.denied,
    )


def _proposal(
    target: _Target, outcome: str, reason: str, candidates=()
) -> Proposal:
    return Proposal(
        tvdb_id=target.tvdb_id,
        series_title=target.title,
        outcome=outcome,
        reason=reason,
        candidates=tuple(candidates),
    )


def _decide(found: _Evaluated, claimed: dict) -> ConfidentMatch | Proposal:
    """Turn one series' findings into a row to write, or a reason not to.

    Split out from the sweep so the whole decision is one readable
    sequence, and so the batch-wide `already_claimed` guard is visibly the
    *last* thing consulted -- after the episodes have already decided which
    candidate, if any, this series corroborates.
    """
    target = found.target

    if found.ran_out_of_budget:
        return _proposal(
            target, "budget_exhausted",
            (
                "The run's SVT request budget ran out before this series "
                "could be checked, so nothing is known about it. Run Find "
                "mappings again to continue."
            ),
            found.candidates,
        )

    if not found.candidates:
        if found.search_error:
            return _proposal(
                target, "search_failed",
                f"SVT search failed: {found.search_error}",
            )
        return _proposal(
            target, "no_match",
            (
                "SVT returned no programme for this title or any of Sonarr's "
                "alternate titles. It may not be on SVT Play -- try the "
                "manual search with a shorter query."
            ),
        )

    if found.search_error:
        return _proposal(
            target, "check_failed",
            (
                f"Part of the SVT search for this series failed "
                f"({found.search_error}), so the list of candidates below "
                "may be incomplete and nothing was written. Run Find "
                "mappings again once SVT is reachable."
            ),
            found.candidates,
        )

    if found.sonarr_error:
        return _proposal(
            target, "check_failed",
            (
                f"Sonarr's episode list for this series could not be read "
                f"({found.sonarr_error}), so there was nothing to compare "
                "SVT's episodes against and nothing was written."
            ),
            found.candidates,
        )

    # Candidates the run actually spent a request on, followed by the ones
    # it ranked below the limit. Both are shown; only the first group is
    # evidence, and each candidate says which it is.
    checked = list(found.checked)
    checked_ids = {c.svt_id for c in checked}
    shown = tuple(checked) + tuple(
        c for c in found.candidates if c.svt_id not in checked_ids
    )

    if any(c.evidence is not None and c.evidence.error for c in checked):
        return _proposal(
            target, "check_failed",
            (
                "SVT's episode list could not be read for at least one "
                "candidate, so the evidence is incomplete and nothing was "
                "written. An unchecked candidate is not a refuted one."
            ),
            shown,
        )

    winner = corroborated_match(checked)
    if winner is None:
        return _proposal(
            target, "needs_decision", _why_not(checked), shown,
        )

    if winner.svt_id in claimed:
        holder_title, holder_tvdb = claimed[winner.svt_id]
        # Surfaced, not written, and not refused outright either: a human
        # may legitimately decide this is the row they want (the resolver
        # tolerates two mappings sharing a slug), and the manual create
        # route lets them say so.
        return _proposal(
            target, "already_claimed",
            (
                f"The episodes corroborate SVT programme {winner.name!r} "
                f"({winner.note()}), but it is already mapped to "
                f"{holder_title!r} (tvdbId {holder_tvdb}). Two series "
                "pointing at one SVT programme would answer a search for "
                "either with episodes of the same show, so this was not "
                "written."
            ),
            (winner,),
        )

    return ConfidentMatch(
        tvdb_id=target.tvdb_id,
        # Sonarr's spelling, never SVT's. This is the permanent filename;
        # the whole page exists for this guarantee.
        series_title=target.title,
        svt_series_id=winner.svt_id,
        svt_slug=winner.slug,
        svt_name=winner.name,
        evidence=winner.evidence,
    )


def _why_not(checked: list[Candidate]) -> str:
    """Say which of the gate's three refusals happened, in words.

    "Needs a decision" on its own is the least useful thing this page could
    say. The person reading it wants to know whether SVT had nothing that
    agreed, or two things that did.
    """
    if not checked:
        return (
            "No candidate's episodes were checked this run, so nothing was "
            "written. Pick one, or leave it unmapped."
        )
    corroborating = [c for c in checked if c.evidence.corroborates]
    if len(corroborating) > 1:
        names = ", ".join(repr(c.name) for c in corroborating)
        return (
            f"{len(corroborating)} SVT programmes corroborate this series' "
            f"episodes ({names}), so there is no basis for picking one. "
            "Pick the right one, or leave it unmapped."
        )
    if corroborating:
        rivals = [
            c for c in checked
            if c is not corroborating[0] and c.evidence.matched > 0
        ]
        names = ", ".join(f"{c.name!r} ({c.note()})" for c in rivals)
        return (
            f"{corroborating[0].name!r} corroborates "
            f"({corroborating[0].note()}), but so does part of "
            f"{names} -- a rival that partly agrees has not been ruled out, "
            "only out-scored. Pick one, or leave it unmapped."
        )
    if all(c.evidence.comparable == 0 for c in checked):
        return (
            "There were no episodes to compare: SVT has published nothing "
            "for these programmes that Sonarr has an aired episode for. "
            "Nothing was written, because there is no evidence either way."
        )
    return (
        "No SVT programme's episodes agree with this series' well enough to "
        "write it without you looking. Pick one, or leave it unmapped."
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
        f"{len(sweep.confident)} corroborated match(es) printed above; "
        f"{len(sweep.needs_decision)} need a decision, "
        f"{len(sweep.no_match)} had no SVT match, "
        f"{len(sweep.check_failed)} could not be checked, "
        f"{len(sweep.already_claimed)} matched a programme already mapped, "
        f"{len(sweep.search_failed)} could not be searched. "
        f"{sweep.already_mapped} series were already mapped and not searched. "
        f"{sweep.requests_used} SVT request(s) were made.",
    ]
    if sweep.capped:
        lines.append(
            f"Stopped at the per-run limit of {sweep.cap} series: "
            f"{sweep.not_searched} unmapped series were NOT looked at. Map "
            "what is above, then run again to continue."
        )
    if sweep.budget_exhausted:
        lines.append(
            f"Stopped at the per-run budget of {sweep.request_budget} SVT "
            f"requests: {len(sweep.out_of_budget)} series were NOT checked "
            "and this sweep is incomplete. Run again to continue."
        )
    if sweep.skipped_records:
        lines.append(
            f"{sweep.skipped_records} Sonarr record(s) had no usable title, "
            "TVDB id or Sonarr id and were skipped."
        )
    for confident in sweep.confident:
        lines.append(
            f"\nCORROBORATED      {confident.series_title} "
            f"(tvdbId {confident.tvdb_id})\n  {confident.svt_name}: "
            f"{confident.evidence.describe()}"
        )
    for proposal in sweep.needs_decision:
        lines.append(
            f"\nNEEDS A DECISION  {proposal.series_title} "
            f"(tvdbId {proposal.tvdb_id})\n  {proposal.reason}"
        )
        for c in proposal.candidates:
            lines.append(f"    {c.svt_id}  {c.slug}  {c.name}  -- {c.note()}")
    for proposal in sweep.no_match:
        lines.append(
            f"\nNO SVT MATCH      {proposal.series_title} "
            f"(tvdbId {proposal.tvdb_id})"
        )
    for proposal in sweep.check_failed:
        lines.append(
            f"\nCOULD NOT CHECK   {proposal.series_title} "
            f"(tvdbId {proposal.tvdb_id})\n  {proposal.reason}"
        )
    for proposal in sweep.already_claimed:
        lines.append(
            f"\nALREADY CLAIMED   {proposal.series_title} "
            f"(tvdbId {proposal.tvdb_id})\n  {proposal.reason}"
        )
    for proposal in sweep.search_failed:
        lines.append(
            f"\nSEARCH FAILED     {proposal.series_title} "
            f"(tvdbId {proposal.tvdb_id})\n  {proposal.reason}"
        )
    for proposal in sweep.out_of_budget:
        lines.append(
            f"\nNOT CHECKED       {proposal.series_title} "
            f"(tvdbId {proposal.tvdb_id})\n  {proposal.reason}"
        )
    return "\n".join(lines)


def main() -> None:
    """CLI: print what a Find mappings sweep would write. Writes nothing.

    Entry point `svtplay-arr-suggest-mappings`. Corroborated rows go to
    stdout as pasteable YAML; everything that needs a human goes to
    stderr. The config page is what actually writes -- this is the
    headless preview of the same decision, made by the same gate, at the
    same configured air-date tolerance.
    """
    import asyncio
    import os
    import sys
    from pathlib import Path

    import httpx
    import yaml as _yaml

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
        existing = MappingTable.load(settings.mappings_file).all()
    except FileNotFoundError:
        existing = []
    except ValueError as exc:
        print(f"{settings.mappings_file} is invalid: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    async def run() -> Sweep:
        async with httpx.AsyncClient(timeout=30.0) as http:
            return await sweep_for_mappings(
                SonarrClient(settings.sonarr_url, settings.sonarr_api_key, http),
                SvtClient(http, settings.svt_ua),
                existing_mappings=existing,
                # The resolver's own tolerance, not this module's idea of
                # one: corroborating at a different window from the one
                # that will later match is the drift to avoid.
                tolerance_days=settings.air_date_tolerance_days,
            )

    sweep = asyncio.run(run())
    _yaml.safe_dump(
        {"series": confident_rows(sweep)},
        sys.stdout,
        allow_unicode=True,
        sort_keys=False,
    )
    print(
        "\n# Rows above were corroborated by the series' own episodes; check "
        "them and paste into mappings.yaml. Nothing was written.\n",
        file=sys.stderr,
    )
    print(format_report(sweep), file=sys.stderr)
