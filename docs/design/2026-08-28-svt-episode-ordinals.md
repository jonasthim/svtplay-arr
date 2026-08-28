# Why the SVT ordinal is not `item.number`

**Date:** 2026-08-28
**Status:** Decided. `item.number` is rejected, not deferred.

> **What this is.** A design record for a change that was investigated and
> *not* made. The Contento GraphQL details page carries an `item.number` for
> every episode, including for shows this project can derive no ordinal for
> at all — which means it looks like the obvious fix for a real, invisible
> defect. It is not. This document records what was measured against the live
> API, why the field cannot be used, and what would have to change before it
> could be.
>
> It is **not** a user guide. For installing and running the service, see
> `deploy/README.md`. Where this and the code disagree, the code is right.

## The defect this would have fixed

`SvtEpisode.ordinal` is signal 2 of `matching.py::episode_matches`: SVT's
position within its own run, compared against Sonarr's episode number. Air
date is signal 1. Both are required, and an episode with `ordinal=None` is
refused outright.

The ordinal is derived by `svt/client.py::_ordinal`, from `/avsnitt-N` in the
play URL or a leading `N.` in the teaser heading. Shows whose episode titles
carry neither — `uppdrag-granskning` and `agenda` among them — produce
`ordinal=None` for **every** episode, so the resolver refuses the entire
show.

That failure is invisible to the operator. A resolver that refuses everything
looks exactly like a week with nothing new.

`item.number` is populated for all of those episodes. Adopting it would
appear to fix the whole class in one line.

## Why it cannot be used

### 1. `number` is not a position within the run the page shows

It is the episode's index inside an SVT-internal *season* grouping that
`detailsPageByPath` does not expose, and whose boundaries cut across the
selections it does. Measured live on seven shows, 234 episodes:

- **`agenda`** — one `productionPeriod` selection labelled `2026` interleaves
  four such groupings (`numberOfEpisodesInSeason` 20, 16, 4 and 1). Four
  different available episodes therefore carry `number == 1`:
  "Hotet från USA" (the run's real first episode), "Partiledardebatt - del 1",
  "Valdebatt om ekonomin", and "Agenda special: Nordens svar på en orolig
  värld". `2`, `3` and `4` are each claimed twice.
- **`uppdrag-granskning`** — the `2026` and `Sommar 2026` selections both
  number their episodes 1..9; so do `2025` and `Sommar 2025`. 47 of the
  show's 59 available episodes share a `number` with another. Five carry
  `number == 1`, including "The last eel" — the English-language version of
  `KqWLE5w` "Den sista ålen", which is itself `number == 27`.

Collisions are not automatically fatal; air date separates them, and
`husdrommar` has thirteen episodes numbered 1 today, one per season, which
the resolver handles exactly as designed. The difference is that
husdrömmar's thirteen are thirteen genuine first episodes. Uppdrag
granskning's five include a summer re-run strand and an alternate-language
cut, and two of them are **one day apart**.

### 2. Nothing in the response marks a special

This was the actual question the investigation was asked: find a reliable
discriminator, and `number` becomes usable behind it. Every candidate was
tested against shows with genuine specials and against multi-season shows.

| Candidate | Why it fails |
| --- | --- |
| `positionInSeason` | Empty for **every** episode that would gain an ordinal, and non-empty only where `_ordinal` already produces the same value (119 rows, 0 disagreements). It gates in exactly zero new episodes — all risk, no gain. |
| The selection an item sits in | `gift-vid-forsta-ogonkastet`'s "Vad hände sen?" special sits in its own **`season`** selection whose `selectionType`, `listPresentation` and `presentationHint` are identical to the two real runs beside it. Only the editor-typed `name`/`slug` differs, and matching on that is the same class of rule as matching `upcomingOverlay.heading` for "Kommer" — a bug this project already removed. `melodifestivalen`'s extra sits in a `productionPeriod` selection; `agenda`'s special sits *inside* the main selection. |
| `numberOfEpisodesInSeason` | `== 1` catches four specials but admits Agenda's four-part debate broadcast (`nEIS=4`, numbered 1–4, colliding head-on with the run) and all eight of Uppdrag granskning's `Sommar 2025` follow-ups (`nEIS=8`). It is also self-inconsistent: Uppdrag granskning reports `nEIS=70` for two different runs that each restart at 1. |
| `analyticsIdentifiers.viewId` | The closest thing to an answer. Its middle segment is a season slot, and it reads `säsong 0` for gvfo's special — SVT's own specials convention. It then reads `0` for **all 36** of Agenda's episodes, including the numbered 20-episode run, and `0` for `KqWLE5w`, a genuine `number == 27` episode of Uppdrag granskning. It is also off by one from the production period (`2024` reports slot `25`) and not injective with `number`. And it is an untyped analytics display string whose format varies by show (`"säsong 15"` on one, bare `"0"` on another). |
| `Episode.parent` | Returns the series. `Content`'s `possibleTypes` are `KidsTvShow, Single, TvSeries, TvShow` — **there is no `Season` type in this graph**, so the grouping `number` indexes cannot be recovered. |
| `Episode.internal`, `Episode.__typename` | `programId`, `producingDepartment`, `allVersionsHidden`; and `Episode` for everything. Nothing editorial about kind. |

A discriminator that works on one show is not a discriminator. `viewId` works
on two of seven and refuses real content on a third.

### 3. It would change nothing that works today

Where `_ordinal` produces a value, `number` agrees — 134 episodes across
seven shows, **0 disagreements**. So adoption is not a trade of reach against
correctness. On every episode that resolves today it is a pure no-op, and
100% of its effect lands on episodes where the old rule abstained and the new
value is demonstrably ambiguous.

## The trade being made

`productionPeriod`-grouped shows resolve nothing, and will keep resolving
nothing. That is the cheaper failure, and it is the same trade `resolver.py`
makes everywhere else:

> An absent ordinal leaves an episode Wanted, and an operator can chase it.
> A wrong ordinal writes a permanently wrong filename, because Sonarr runs
> `renameEpisodes=False`, and nothing will ever correct it.

Giving a special an ordinal makes it eligible to match a regular episode on
air date alone. That is the failure this project has already been bitten by
once — see the commit "stop the RSS reverse match claiming a Sonarr special".

## What holds the decision up

Three tests, and one of them is doing much more work than its name suggests.

- `test_uppdrag_granskning_still_has_no_ordinals_and_still_will_not_resolve`
  — was written as a marker "meant to be rewritten by the change that does
  it". It is no longer a marker; it asserts that a rejected change stayed
  rejected.
- `test_a_special_still_has_no_ordinal` — gvfo's `KBMY9zX`.
- `test_the_shipped_query_does_not_ask_for_item_number` — **the only test in
  1035 that catches the tempting form of this change.**

That last point is the one worth carrying forward. Adopting `number`
wholesale fails 13 tests, so nobody would ship it. But adding it as a
*fallback* behind `_ordinal` cannot alter any ordinal that exists today, and
so looks obviously safe — and it fails exactly one test, the text assertion
on the query document. No response-driven test can catch it, because **no
captured fixture contains `number`**: the shipped query does not ask for it,
so `item.get("number")` is `None` on every recorded episode while production
behaviour changes on three real shows. Verified by mutation: 1 failed, 1034
passed.

`test_the_query_asks_for_every_field_the_reader_actually_reads` is the
complementary half and does not cover it — that one catches reading a field
the query does not request; this one catches requesting it at all.

## What would make this worth revisiting

A **typed** field, not a cleverer reading of an existing one:

- an `Episode` field naming the kind of item (`episodeType`, `isSpecial`);
- a `Season` type reachable from `Episode.parent` or from the selection, so
  `(season, number)` is a real key rather than a reconstruction;
- `positionInSeason` becoming populated on `productionPeriod`-grouped shows.
  It is `NON_NULL String` and today returns `""` on exactly the shows that
  need it.

Until one of those exists, the reach problem is not solvable from this
endpoint, and the right answer is the one taken everywhere else in this
codebase: refuse, and leave the episode Wanted.

## Method note

`__schema` is blocked, and `__type` — which the earlier GraphQL spike used
freely — is guarded by a bad-faith detector that was not hit then:

```
This request is not asking for introspection in good faith - __Type.fields is present too often!
This request is not asking for introspection in good faith - Query.__type is present too often!
```

One `__type` per request, and `fields` at most once per request. Batching
several type lookups into one document returns `null` for all of them.
