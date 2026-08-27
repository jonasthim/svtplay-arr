import html as html_mod
import os
import re
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from svtplay_arr.api.config_ui import VIEWS, build_config_router
from svtplay_arr.config import SETTING_FIELDS, Settings
from svtplay_arr.mappings import MappingTable, add_mapping
from svtplay_arr.models import SonarrEpisode, SvtEpisode, SvtSearchHit
from svtplay_arr.svt.client import SvtApiError, derive_slug

TITLE = "Gift vid första ögonkastet"


class FakeSvt:
    def __init__(self, hits=None, episodes=None, list_episodes_error=None,
                 episodes_by_slug=None):
        self.hits = hits or []
        # slug -> [SvtEpisode], for the sweep, which reads a *different*
        # episode list per candidate. Takes precedence over `episodes`
        # (the single list the per-mapping Check control reads) when a
        # slug is present in it, so tests written before the sweep
        # corroborated anything keep the shape they had.
        self.episodes_by_slug = episodes_by_slug or {}
        # What list_episodes returns/raises for the mapping Check control.
        # Defaulting episodes to [] rather than None means a test that
        # never touches this (i.e. every test written before the Check
        # control existed) still gets a well-typed, empty result if
        # something did call list_episodes -- which none of them should.
        self.episodes = episodes if episodes is not None else []
        self.list_episodes_error = list_episodes_error
        # Recorded so tests can assert list_episodes was never called --
        # the constraint most likely to be violated by a later
        # well-meaning edit that makes the check run on page load.
        self.list_episodes_calls: list[str] = []

    async def search_series(self, query):
        return self.hits

    async def list_episodes(self, slug):
        self.list_episodes_calls.append(slug)
        if self.list_episodes_error is not None:
            raise self.list_episodes_error
        if self.episodes_by_slug:
            return self.episodes_by_slug.get(slug, [])
        return self.episodes


class FakeSonarr:
    def __init__(self, series=None, episodes=None):
        self.series = series if series is not None else []
        # series_id -> [SonarrEpisode]. The sweep corroborates a candidate
        # against these, so a series with none is a series with no
        # evidence -- which is refused, never written.
        self._episodes = episodes or {}
        self.episode_calls: list[int] = []

    async def all_series(self):
        return self.series

    async def episodes(self, series_id):
        self.episode_calls.append(series_id)
        return self._episodes.get(series_id, [])


def _paths(tmp_path: Path):
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        "sonarr_api_key: SECRET-KEY-VALUE\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    return cfg, maps


def _full_paths(tmp_path: Path):
    """Like `_paths`, but every settings field is already on disk.

    `_paths`'s config.yaml omits the int fields, so a form that submits
    them for the first time would always read as "changed" even when the
    operator picked the same values `_form` already fills in by default.
    These values match `_form`'s defaults exactly, so posting `_form`
    unmodified against this fixture is a true no-op save.
    """
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        "sonarr_api_key: SECRET-KEY-VALUE\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n"
        "air_date_tolerance_days: 1\n"
        "rss_window_days: 14\n"
        "max_concurrent_downloads: 1\n",
        encoding="utf-8",
    )
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    return cfg, maps


def _notice_text(html: str) -> str:
    """Isolate the notice banner, since a field's label always appears a
    second time as its own form label -- asserting on the whole body would
    pass even if the notice named the wrong field."""
    m = re.search(r'<p class="notice">(.*?)</p>', html, re.S)
    assert m, f"no notice banner in:\n{html}"
    return m.group(1)


def _error_text(html: str) -> str:
    """Isolate the error banner, for the same reason as `_notice_text`.

    Every settings field name appears in the form regardless, and every
    mapped title appears in the mappings table regardless, so asserting on
    the whole body would pass even when the error says nothing of the kind.
    """
    m = re.search(r'<p class="error">(.*?)</p>', html, re.S)
    assert m, f"no error banner in:\n{html}"
    return m.group(1)


def _client(tmp_path: Path, svt=None, sonarr=None) -> TestClient:
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, svt or FakeSvt(), sonarr or FakeSonarr())
    )
    return TestClient(app)


def test_index_lists_the_existing_mapping(tmp_path: Path):
    body = _client(tmp_path).get("/config/mappings").text
    assert TITLE in body
    assert "288649" in body


def test_index_renders_the_api_key_as_an_editable_masked_field(tmp_path: Path):
    # Reversal of test_index_never_renders_the_api_key, decided 2026-08-25.
    # The value is deliberately in the page source; masking is a
    # shoulder-surfing measure, not a confidentiality one.
    body = _client(tmp_path).get("/config/settings").text
    assert "SECRET-KEY-VALUE" in body
    assert 'name="sonarr_api_key"' in body
    assert 'type="password"' in body


def test_the_page_wires_its_controls_without_inline_event_handlers(tmp_path: Path):
    # Replaces test_the_api_key_reveal_uses_no_javascript, whose subject --
    # a reveal that ran without JavaScript at all -- no longer exists: the
    # Show/Hide button flips the input's own `type`, which nothing but
    # script can do. What that test was also protecting is still worth
    # protecting and is asserted here instead: every behaviour on this page
    # is attached with addEventListener, never an inline `onclick=` or a
    # `javascript:` URI. Those are the two things that would need a
    # script-src loosening from the reverse proxy in front of this page,
    # and they are easy to reintroduce by hand without anything else
    # failing.
    body = _client(tmp_path).get("/config").text
    lowered = body.lower()
    assert "javascript:" not in lowered
    assert not re.search(r"\son[a-z]+\s*=", lowered)
    # ...and there is actually script here doing the wiring, so this
    # cannot pass by the page having no behaviour left at all.
    assert "addEventListener" in _inline_script_code()


def test_the_api_key_field_is_masked_and_holds_the_value_exactly_once(
    tmp_path: Path,
):
    # Replaces test_the_reveal_markup_order_matches_the_css_selector. That
    # test pinned a three-element adjacent-sibling chain -- checkbox,
    # label, and a second `<code class="revealed">` copy of the key -- so
    # that a reorder could not silently break a pure-CSS reveal. The chain
    # is gone with the CSS reveal; what replaces it is this: the mask is
    # the field itself, and the page carries the secret once rather than
    # twice. The security posture is unchanged either way (the value is in
    # the source), but a second rendered copy is a second place to leak
    # from for no gain.
    body = _client(tmp_path).get("/config/settings").text
    field = _input_tag(body, "sonarr_api_key")
    assert 'type="password"' in field, f"the API key field is not masked: {field}"
    assert 'value="SECRET-KEY-VALUE"' in field
    assert body.count("SECRET-KEY-VALUE") == 1, (
        "the API key value is rendered more than once in the page"
    )
    assert "revealed" not in body, "the old second copy of the key is back"


def test_the_show_hide_button_exists_only_once_javascript_has_run(tmp_path: Path):
    # Same rule the mapping filter input follows: a control that cannot
    # work without JavaScript is not rendered by the server, because a
    # dead button is worse than no button. With JS off the field simply
    # stays masked and stays fully editable -- revealing is the
    # convenience, replacing the key is the function.
    #
    # Both absences are asserted in attribute syntax (`name="value"`),
    # which is what a server-rendered button would carry and what the
    # script -- which sets everything through properties and setAttribute
    # -- cannot contain, so the script's own presence in the page cannot
    # satisfy them.
    body = _client(tmp_path).get("/config/settings").text
    assert 'type="button"' not in body, (
        "a button that only JavaScript can make work is server-rendered"
    )
    assert 'class="secret-toggle"' not in body
    # ...and the hook the script attaches to is rendered, together with
    # the field name it builds the button's accessible name from, so this
    # cannot pass by there being no reveal at all.
    assert 'class="secret-field" data-secret-label="Sonarr API key"' in body
    assert "initSecretToggles" in body
    # That the initialiser is actually *run* is not asserted here -- it is
    # test_every_initialiser_in_the_script_is_actually_run's job, for
    # every init* function rather than only this one.


def test_the_show_hide_button_is_a_labelled_toggle_that_cannot_submit(
    tmp_path: Path,
):
    # Replaces test_the_reveal_checkbox_is_not_submitted_with_the_form.
    # The checkbox it named is gone, but the hazard it guarded is not: the
    # control still sits inside the settings <form>. A <button> there
    # defaults to type="submit", which would save every setting on the
    # page on each click *and* steal implicit submission from the Save
    # button when Enter is pressed in a field.
    #
    # Asserted alongside it, because they are one control and each of
    # these failing is invisible in the others: the button flips the
    # input's own type between masked and readable; its visible label is a
    # word, not a bare glyph (the dangerous-field marker already reached
    # production as a "!" whose first review question was what it meant);
    # and its accessible name follows that word rather than going stale on
    # it.
    #
    # Matched a statement at a time, on the names and literals that carry
    # the behaviour, rather than on the exact expressions. A test that
    # fails on a reformat or a renamed local teaches the next person to
    # edit the test instead of reading it.
    script = _inline_script_code()

    # Never a submit -- and matched in either spelling, so setting the
    # property or the attribute both count. The omission is the realistic
    # regression: a <button> with no type at all *is* a submit button.
    _sets_type = r'button\.(type\s*=\s*|setAttribute\(\s*"type"\s*,\s*)'
    assert re.search(_sets_type + '"button"', script), (
        "the toggle never sets itself to type=button, so it submits the form"
    )
    assert not re.search(_sets_type + '"submit"', script), (
        "the toggle is explicitly made a submit button"
    )

    # It flips the input between masked and readable. Both literals in one
    # assignment to `input.type`, so a version that only ever unmasks
    # fails.
    flip = _one_statement(script, r"input\.type", r'"password"')
    assert re.search(r"input\.type", flip) and '"text"' in flip, (
        f"the button does not toggle the input between masked and readable: {flip}"
    )

    # The visible label is one of two words, chosen at render time.
    label = _one_statement(script, r"button\.(textContent|innerText)")
    assert '"Show"' in label and '"Hide"' in label, (
        f"the button's label is not a word: {label}"
    )
    assert "?" in label, f"the button's label never changes: {label}"

    # The accessible name carries the same two words *and* the field's own
    # label, taken from the attribute the server renders rather than from
    # a second copy of that string in here. Linked by the variable the
    # attribute was read into, so renaming that local is fine and actually
    # severing the link is not.
    source = _one_statement(script, r"data-secret-label")
    held_in = re.search(r"var\s+(\w+)\s*=", source)
    assert held_in, f"the field's label is not read into a variable: {source}"
    name = _one_statement(script, r"button", r"aria-?[Ll]abel")
    assert '"Show ' in name and '"Hide ' in name, (
        f"the accessible name does not follow the visible word: {name}"
    )
    assert held_in.group(1) in name, (
        f"the accessible name does not name the field it acts on: {name}"
    )

    # And no aria-pressed. A toggle button either keeps a fixed name and
    # exposes state through aria-pressed, or renames itself to describe
    # the next action; this one renames itself, so adding aria-pressed on
    # top would have it announced as "Hide Sonarr API key, toggle button,
    # pressed" -- worse than either convention alone.
    assert not re.search(r"aria-?[Pp]ressed", script), (
        "the label already carries the state; aria-pressed duplicates it"
    )


def test_the_show_hide_button_sits_inside_the_field_without_covering_it(
    tmp_path: Path,
):
    # The button is positioned over the input's own right-hand edge, so the
    # input has to give that width up or the button lands on top of the
    # text -- and an API key is exactly the kind of string long enough to
    # reach it. Both halves are asserted here: either one alone renders a
    # page that looks right and has a field you cannot read.
    #
    # The reserved width is keyed off `.has-toggle`, a class only the
    # script adds, so with JavaScript off the field is a plain full-width
    # input rather than one with a strip of dead space at its edge.
    css = _stylesheet()

    field = re.search(r"\.secret-field\s*\{([^}]*)\}", css)
    assert field, "no .secret-field rule"
    assert "position: relative" in field.group(1), (
        "the secret field is not a positioning context for its button"
    )

    toggle = re.search(r"\.secret-toggle\s*\{([^}]*)\}", css)
    assert toggle, "no .secret-toggle rule"
    assert "position: absolute" in toggle.group(1), (
        "the button is not placed inside the field"
    )
    assert re.search(r"\bright:\s*[0-9.]", toggle.group(1)), (
        "the button is not pinned to the field's right-hand edge"
    )

    room = re.search(r"\.secret-field\.has-toggle[^{]*\{([^}]*)\}", css)
    assert room, "nothing reserves room for the button"
    assert re.search(r"padding-right:\s*[0-9.]+rem", room.group(1)), (
        "the input reserves no room for the button overlaid on it"
    )


def test_index_shows_every_editable_setting(tmp_path: Path):
    body = _client(tmp_path).get("/config/settings").text
    for key in ("sonarr_url", "incomplete_dir", "air_date_tolerance_days",
                "rss_window_days", "max_concurrent_downloads"):
        assert key in body


def test_index_states_the_tolerance_consequence(tmp_path: Path):
    assert "ambiguous" in _client(tmp_path).get("/config/settings").text.lower()


_SECTION_FIELDS = {
    "Connection": ["sonarr_url", "sonarr_api_key"],
    "Storage": ["incomplete_dir", "completed_dir"],
    "Matching": ["air_date_tolerance_days", "rss_window_days"],
    "Downloads": ["max_concurrent_downloads"],
}


def _section_slices(body: str) -> dict[str, str]:
    """Split `body` at each section <legend>, in the order they appear.

    Lets a test assert a field's id shows up in *its* section's slice and
    nowhere else, which a plain "key in body" check cannot distinguish
    from the field having drifted into the wrong section.
    """
    starts = [
        (m.group(1), m.start())
        for m in re.finditer(r"<legend>([^<]+)</legend>", body)
    ]
    slices = {}
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(body)
        slices[name] = body[start:end]
    return slices


def test_index_groups_settings_into_labeled_sections_in_order(tmp_path: Path):
    body = _client(tmp_path).get("/config/settings").text
    names = [m.group(1) for m in re.finditer(r"<legend>([^<]+)</legend>", body)]
    # A future edit that drops a section, or reorders the fieldsets, fails
    # here rather than only being noticeable by eye.
    assert names == list(_SECTION_FIELDS)


def test_index_places_every_field_in_its_documented_section_and_no_other(
    tmp_path: Path,
):
    body = _client(tmp_path).get("/config/settings").text
    slices = _section_slices(body)
    assert set(slices) == set(_SECTION_FIELDS)

    for name, keys in _SECTION_FIELDS.items():
        for key in keys:
            assert f'id="{key}"' in slices[name], (
                f"{key} missing from the {name} section"
            )
        for other_name, other_keys in _SECTION_FIELDS.items():
            if other_name == name:
                continue
            for other_key in other_keys:
                assert f'id="{other_key}"' not in slices[name], (
                    f"{other_key} leaked into the {name} section"
                )


def test_dangerous_fields_are_visibly_marked(tmp_path: Path):
    body = _client(tmp_path).get("/config/settings").text
    for key in ("air_date_tolerance_days", "incomplete_dir", "completed_dir"):
        pattern = re.compile(
            r'<div class="field-danger">\s*<label for="' + re.escape(key) + '"'
        )
        assert pattern.search(body), f"{key} has no danger treatment:\n{body}"


def test_ordinary_fields_are_not_marked_dangerous(tmp_path: Path):
    body = _client(tmp_path).get("/config/settings").text
    for key in ("sonarr_url", "sonarr_api_key", "rss_window_days",
                "max_concurrent_downloads"):
        pattern = re.compile(
            r'<div class="field-danger">\s*<label for="' + re.escape(key) + '"'
        )
        assert not pattern.search(body), f"{key} wrongly marked dangerous"


_DANGER_GLYPH = "\u26a0"

_DANGEROUS_KEYS = ("air_date_tolerance_days", "incomplete_dir", "completed_dir")


def _danger_block(body: str, key: str) -> str:
    """The `.field-danger` wrapper and label rendered for `key`."""
    m = re.search(
        r'<div class="field-danger">\s*<label for="' + re.escape(key) + r'".*?</label>',
        body,
        re.S,
    )
    assert m, f"{key} has no danger treatment:\n{body}"
    return html_mod.unescape(m.group(0))


def _danger_badge(block: str, key: str) -> str:
    """The badge element's markup, matched by nesting rather than by regex.

    The badge wraps a decorative glyph in a span of its own, so a
    `(.*?)</span>` pattern would stop at the wrong closing tag and quietly
    assert on half the badge.
    """
    begin = block.find('<span class="danger-badge"')
    assert begin != -1, f"{key} has no danger badge:\n{block}"
    depth = 0
    for m in re.finditer(r"<(/?)span\b[^>]*>", block[begin:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return block[begin:begin + m.end()]
    raise AssertionError(f"{key}'s danger badge span is never closed:\n{block}")


def test_the_danger_marker_says_what_it_means_in_words(tmp_path: Path):
    # The first question the live page was asked, from a phone: "what does
    # the ! mean". An icon carrying the only signal that a setting can
    # silently break the service is not a label. The marker has to read as
    # a word, and it has to be a word in the markup -- CSS-generated
    # content is not dependably announced and is absent entirely with CSS
    # off.
    body = _client(tmp_path).get("/config/settings").text
    for key in _DANGEROUS_KEYS:
        badge = _danger_badge(_danger_block(body, key), key)
        text = re.sub(r"<[^>]+>", "", badge)
        word = re.sub(r"[^A-Za-z]", "", text)
        assert len(word) >= 4, (
            f"{key}'s danger marker reads {text.strip()!r} -- not a word"
        )


def test_the_danger_glyph_is_never_the_only_carrier_of_the_meaning(
    tmp_path: Path,
):
    # The glyph decorates the word; announced on its own it is either
    # nothing or an unpronounceable symbol, so it is hidden from the
    # accessibility tree and the word beside it -- real text, inside the
    # label, announced with the field -- carries the meaning.
    body = _client(tmp_path).get("/config/settings").text
    for key in _DANGEROUS_KEYS:
        badge = _danger_badge(_danger_block(body, key), key)
        holders = re.findall(r"<([a-z]+)([^>]*)>[^<]*" + _DANGER_GLYPH, badge)
        assert holders, f"{key}'s badge carries no warning glyph:\n{badge}"
        for _tag, attrs in holders:
            assert 'aria-hidden="true"' in attrs, (
                f"{key}'s warning glyph is announced as if it were the "
                f"label:\n{badge}"
            )


def test_the_danger_marker_is_not_left_to_css_generated_content(tmp_path: Path):
    # It used to be a `::before { content: "\26a0 " }` on the label: not in
    # the page source, gone with CSS off, and the whole reason the warning
    # read as a bare glyph with nothing to say what it meant.
    css = _stylesheet()
    danger_rules = re.findall(r"\.field-danger[^{]*::before\s*\{([^}]*)\}", css)
    assert not danger_rules, (
        f"the danger marker is back to being CSS content: {danger_rules}"
    )


def test_dangerous_field_treatment_does_not_duplicate_the_help_text(
    tmp_path: Path,
):
    # The spec text: don't add a second explanation that could drift from
    # the field's own help string. The visible marker must be styling, not
    # new prose -- so the consequence sentence still appears exactly once.
    body = _client(tmp_path).get("/config/settings").text
    assert body.count("ambiguous") == 1


def _input_tag(body: str, key: str) -> str:
    """The one form control named `key`.

    Matched on `name`, not `id`: `name` is the thing the browser actually
    posts back, which is what every caller here is really asserting about.
    (It also used to be the only safe discriminator, because the API key
    field was followed by a reveal checkbox whose id was the key plus a
    suffix; that checkbox is gone, but matching on the submitted name is
    still the honest match.)
    """
    m = re.search(r'<input[^>]*name="' + re.escape(key) + r'"[^>]*>', body, re.S)
    assert m, f"no input named {key} in:\n{body}"
    return m.group(0)


def test_the_numeric_settings_ask_a_phone_for_a_numeric_keypad(tmp_path: Path):
    # Half of "not fit for mobile": tapping Concurrent downloads on a phone
    # brought up a full alphabetic keyboard. Driven off each field's
    # declared `kind`, not a list of key names -- a fourth int setting must
    # get the keypad by being declared an int, not by someone remembering
    # to add it here.
    body = _client(tmp_path).get("/config/settings").text
    ints = [f for f in SETTING_FIELDS if f.kind == "int"]
    assert ints, "no int settings; this test would be vacuous"

    for f in SETTING_FIELDS:
        tag = _input_tag(body, f.key)
        if f.kind == "int":
            assert 'inputmode="numeric"' in tag, (
                f"{f.key} is a number but offers a text keyboard: {tag}"
            )
        else:
            assert "inputmode" not in tag, (
                f"{f.key} is not a number but claims a numeric keypad: {tag}"
            )


def test_the_numeric_settings_are_not_type_number(tmp_path: Path):
    # `inputmode` changes the keyboard and nothing else. `type="number"`
    # would also bring spinners, locale-dependent parsing, and browsers
    # that silently discard non-numeric input -- which would quietly take
    # over from `save_settings`' own int() validation and the deliberately
    # specific errors it raises (see test_invalid_settings_are_refused...).
    body = _client(tmp_path).get("/config/settings").text
    assert 'type="number"' not in body
    for f in SETTING_FIELDS:
        if f.kind == "int":
            assert 'type="text"' in _input_tag(body, f.key)


def test_index_survives_a_malformed_config_file(tmp_path: Path):
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sonarr_url: [unterminated\n", encoding="utf-8")
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)
    resp = client.get("/config/settings")

    assert resp.status_code == 200
    assert str(cfg) in resp.text
    # ...and the views that do not depend on config.yaml are untouched by
    # the failure. One broken file must not take the whole page down, which
    # is the same property this asserted when it was all one page.
    assert TITLE in client.get("/config/mappings").text
    assert client.get("/config").status_code == 200


def test_index_survives_a_malformed_mappings_file(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    maps.write_text("series: [unterminated\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)
    resp = client.get("/config/mappings")

    assert resp.status_code == 200
    assert str(maps) in resp.text
    # ...and the settings form, which doesn't depend on mappings.yaml at
    # all, is untouched by the failure.
    assert "sonarr_url" in client.get("/config/settings").text


# --- A failed mappings load must never be reported as "no mappings" ---
#
# `_index` does its own `MappingTable.load`, which raises for a broken
# file and leaves `mappings` empty -- while the running service is still
# serving its last known-good table. Rendering the empty-table row there
# tells the operator "Nothing will be offered to Sonarr", which is both
# false and an invitation to restart: on a fresh boot there is no
# last-good table, the feed genuinely empties, and Sonarr rejects the
# indexer. The page must not talk anyone into causing the failure the
# status strip is warning them about.


@pytest.mark.parametrize(
    "broken",
    [
        "series:\n",  # the shape a hand-edit most easily produces
        "series: [unterminated\n",  # not valid YAML at all
    ],
)
def test_a_broken_mappings_file_never_claims_nothing_is_offered_to_sonarr(
    tmp_path: Path, broken: str,
):
    cfg, maps = _paths(tmp_path)
    maps.write_text(broken, encoding="utf-8")
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(
                mappings_degraded=True, mappings=1, status="degraded",
            ),
        )
    )
    body = TestClient(app).get("/config/mappings").text

    # The sentence that prompts the destructive action must be gone...
    assert "Nothing will be offered to Sonarr" not in body
    assert "No mappings yet" not in body
    # ...and the degraded strip, which is the true half, must remain.
    assert "DEGRADED" in body
    assert 'class="status-chip error"' in body
    # Something true stands in the table's place, and it does not read as
    # an empty list.
    assert "could not be read" in body


def test_a_legitimately_empty_mappings_file_still_says_there_are_none(
    tmp_path: Path,
):
    # The empty state is a real state and must not be suppressed along
    # with the false one: `series: []` loads fine and genuinely means
    # nothing is offered to Sonarr.
    cfg, maps = _paths(tmp_path)
    maps.write_text("series: []\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    body = TestClient(app).get("/config/mappings").text

    assert "No mappings yet. Nothing will be offered to Sonarr." in body
    assert "could not be read" not in body


# The replacement wording has its own version of the same trap. "The
# service keeps serving the last table it loaded successfully" is true
# after a good load, and false on a fresh boot whose file was already
# broken -- where nothing was ever loaded, nothing is offered to Sonarr,
# and Sonarr will reject the indexer. Reassurance exactly where urgency
# is needed is the original finding inverted, so the branch is asserted
# in all three states `mappings_ever_loaded` can be in.

_KEEPS_SERVING = "keeps serving the last table it loaded successfully"
_NEVER_LOADED = "has never been read successfully"


def _broken_mappings_body(tmp_path: Path, status_provider=None) -> str:
    cfg, maps = _paths(tmp_path)
    maps.write_text("series:\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(), status_provider=status_provider,
        )
    )
    return TestClient(app).get("/config/mappings").text


def test_a_broken_mappings_file_after_a_good_load_says_the_table_survives(
    tmp_path: Path,
):
    # The reassuring sentence is correct here and must be kept: the
    # reloading table held its last good copy, so the feed is intact and
    # the operator has time to fix the file properly.
    body = _broken_mappings_body(
        tmp_path,
        status_provider=lambda: _sample_health(
            mappings_degraded=True, mappings_ever_loaded=True, mappings=1,
            status="degraded",
        ),
    )

    assert _KEEPS_SERVING in body
    assert _NEVER_LOADED not in body
    assert "Nothing will be offered to Sonarr" not in body
    assert "No mappings yet" not in body


def test_a_broken_mappings_file_that_never_loaded_says_the_feed_is_empty(
    tmp_path: Path,
):
    # The case that matters most, and the one the reassuring sentence got
    # wrong: a fresh boot with an already-broken file has no last-good
    # table at all. Nothing is offered to Sonarr, and the page must say so
    # plainly rather than implying the feed is being held up by something.
    body = _broken_mappings_body(
        tmp_path,
        status_provider=lambda: _sample_health(
            mappings_degraded=True, mappings_ever_loaded=False, mappings=None,
            status="degraded",
        ),
    )

    assert _NEVER_LOADED in body
    # Above all: not the sentence that would say the opposite.
    assert _KEEPS_SERVING not in body
    # The urgency is stated, and a restart is not offered as a way out --
    # it cannot help, and would only confirm the state.
    assert "offering nothing to Sonarr" in body
    assert "restart" in body.lower()
    assert "restart cannot" in body.lower()
    # ...and still none of the original false wording.
    assert "Nothing will be offered to Sonarr" not in body
    assert "No mappings yet" not in body


def test_a_broken_mappings_file_asserts_neither_without_a_status_provider(
    tmp_path: Path,
):
    # Most routers are built without a status_provider (as most of this
    # file does). With nothing to ask, the page must hedge: say the table
    # is unavailable and stop. Asserting a last-good table it cannot
    # confirm exists is the very thing being fixed here.
    body = _broken_mappings_body(tmp_path)

    assert "could not be read" in body
    assert _KEEPS_SERVING not in body
    assert _NEVER_LOADED not in body
    assert "Nothing will be offered to Sonarr" not in body
    assert "No mappings yet" not in body


def test_a_broken_mappings_file_asserts_neither_when_the_provider_raises(
    tmp_path: Path,
):
    # Same rule when the provider exists but blows up: there is still no
    # dict to ask, so there is still nothing to assert.
    def _boom():
        raise RuntimeError("status computation is on fire")

    body = _broken_mappings_body(tmp_path, status_provider=_boom)

    assert "could not be read" in body
    assert _KEEPS_SERVING not in body
    assert _NEVER_LOADED not in body
    assert "No mappings yet" not in body


def _form(tmp_path: Path, mtime, **over):
    values = {
        "expected_mtime": str(mtime),
        "sonarr_url": "http://sonarr.test:8989",
        # The real form posts this on every save now that the key is
        # editable, so the default mirrors what a browser sends: the value
        # already on disk, resubmitted unchanged.
        "sonarr_api_key": "SECRET-KEY-VALUE",
        "incomplete_dir": f"{tmp_path}/i",
        "completed_dir": f"{tmp_path}/c",
        "air_date_tolerance_days": "1",
        "rss_window_days": "14",
        "max_concurrent_downloads": "1",
    }
    values.update(over)
    return values


def test_saving_settings_writes_and_reloads_with_the_real_loader(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)
    mtime = cfg.stat().st_mtime

    r = client.post("/config/settings", data=_form(tmp_path, mtime))

    assert r.status_code == 200
    assert Settings.load(cfg).rss_window_days == 14


def test_resubmitting_the_same_api_key_leaves_it_unchanged_on_disk(tmp_path: Path):
    # Replaces test_saving_settings_never_echoes_the_api_key. The page now
    # renders the key by design (2026-08-25), so "never echoes" is no longer
    # a guarantee; what still has to hold is that a plain save round-trips
    # the value byte-for-byte rather than mangling or dropping it.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)
    r = client.post("/config/settings", data=_form(tmp_path, cfg.stat().st_mtime))
    assert "SECRET-KEY-VALUE" in r.text
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["sonarr_api_key"] == (
        "SECRET-KEY-VALUE"
    )


def test_saving_a_new_api_key_writes_it_to_the_file(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, sonarr_api_key="NEW-KEY-VALUE"),
    )

    assert r.status_code == 200
    assert Settings.load(cfg).sonarr_api_key == "NEW-KEY-VALUE"


def test_a_blank_api_key_is_refused_with_a_200_and_the_file_untouched(tmp_path: Path):
    # A blank key starts fine and reports healthy, then fails every Sonarr
    # call. It must never reach the file.
    cfg, maps = _paths(tmp_path)
    before = cfg.read_bytes()
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, sonarr_api_key="   "),
    )

    assert r.status_code == 200  # never a 500
    assert "sonarr_api_key" in _error_text(r.text)
    assert cfg.read_bytes() == before


def _paths_without_api_key(tmp_path: Path):
    """A deployment that keeps the key out of the file entirely.

    `deploy/svtplay-arr.service` used to supply it via `$SONARR_API_KEY`, and
    `Settings.load` still honours that. The config page renders the field
    empty for it, which is what makes the interaction below reachable.
    """
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n"
        "rss_window_days: 14\n",
        encoding="utf-8",
    )
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    return cfg, maps


def test_an_env_only_key_blocks_every_settings_save(tmp_path: Path, monkeypatch):
    # The sharp edge of refusing a blank key. With $SONARR_API_KEY set and no
    # key in the file, the field renders empty, the browser posts "", and the
    # blank check refuses the whole save -- so an operator changing
    # rss_window_days is blocked by an error about API keys. Described in the
    # report but asserted nowhere until now.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-OVERRIDE-KEY")
    cfg, maps = _paths_without_api_key(tmp_path)
    before = cfg.read_bytes()
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(
            tmp_path, cfg.stat().st_mtime,
            sonarr_api_key="", rss_window_days="21",
        ),
    )

    assert r.status_code == 200  # never a 500
    assert "sonarr_api_key" in _error_text(r.text)
    # The unrelated field the operator actually came to change is refused too.
    assert cfg.read_bytes() == before
    assert Settings.load(cfg).rss_window_days == 14


def test_the_warning_says_saves_are_blocked_when_the_file_has_no_key(
    tmp_path: Path, monkeypatch
):
    # "stored but not used" describes a much smaller problem than what
    # actually happens here, which is that nothing saves at all.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-OVERRIDE-KEY")
    cfg, maps = _paths_without_api_key(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))

    body = TestClient(app).get("/config/settings").text

    assert "every settings save will be refused" in body
    assert "ENV-OVERRIDE-KEY" not in body


def test_no_blocked_saves_warning_when_the_file_has_its_own_key(
    tmp_path: Path, monkeypatch
):
    # With a key in the file the field renders filled, the browser posts it
    # back, and saves work normally -- the override only means the saved
    # value is not the one in use. Saying "nothing will save" here would be
    # false, and would train the operator to ignore the warning that is true.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-OVERRIDE-KEY")
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    body = client.get("/config/settings").text
    assert "svtplay-arr.service" in body          # the override warning stands
    assert "every settings save will be refused" not in body

    r = client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, rss_window_days="21"),
    )
    assert Settings.load(cfg).rss_window_days == 21


def test_the_page_warns_when_the_environment_overrides_the_api_key(
    tmp_path: Path, monkeypatch
):
    # Settings.load gives $SONARR_API_KEY precedence over the file, so a key
    # saved here would be written and then silently ignored: the page says
    # "saved", the banner says "restart to apply", and the service keeps
    # using the old value forever. Exactly the silent failure this project
    # keeps getting bitten by.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-OVERRIDE-KEY")
    body = _client(tmp_path).get("/config/settings").text

    assert "SONARR_API_KEY" in body
    assert "svtplay-arr.service" in body
    assert "not used" in body.lower()
    # The warning must never be the thing that leaks the effective key.
    assert "ENV-OVERRIDE-KEY" not in body


@pytest.mark.parametrize("value", [None, ""])
def test_no_environment_warning_when_the_variable_is_unset_or_empty(
    tmp_path: Path, monkeypatch, value
):
    # `deploy/svtplay-arr.service` ships a literal `Environment=SONARR_API_KEY=`
    # placeholder, so the empty case is the *normal* deployment, not an edge
    # one. Settings.load ignores an empty value too, so warning about it
    # would be crying wolf on every page load.
    if value is None:
        monkeypatch.delenv("SONARR_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SONARR_API_KEY", value)
    body = _client(tmp_path).get("/config/settings").text
    assert "svtplay-arr.service" not in body


def test_invalid_settings_are_refused_with_a_200_and_the_file_untouched(
    tmp_path: Path
):
    cfg, maps = _paths(tmp_path)
    before = cfg.read_bytes()
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, rss_window_days="soon"),
    )

    assert r.status_code == 200  # never a 500
    assert "rss_window_days" in r.text
    assert cfg.read_bytes() == before


def test_stale_mtime_is_refused_and_explained(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    before = cfg.read_bytes()
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post("/config/settings", data=_form(tmp_path, "1.0"))

    assert r.status_code == 200
    assert "reload" in r.text.lower()
    assert cfg.read_bytes() == before


def test_successful_save_says_a_restart_is_needed(tmp_path: Path):
    # Assert on wording unique to the success notice. The settings form
    # always carries the words "service restart", so asserting on "restart"
    # alone would pass even if the notice were never rendered.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)
    r = client.post("/config/settings", data=_form(tmp_path, cfg.stat().st_mtime))
    assert "settings saved" in r.text.lower()
    assert "restart" in r.text.lower()


def test_saving_with_no_changes_does_not_claim_a_pending_restart(tmp_path: Path):
    cfg, maps = _full_paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post("/config/settings", data=_form(tmp_path, cfg.stat().st_mtime))

    notice = _notice_text(r.text)
    assert "unchanged" in notice.lower()
    assert "restart" not in notice.lower()


def test_changing_one_setting_names_only_that_setting(tmp_path: Path):
    cfg, maps = _full_paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, rss_window_days="21"),
    )

    notice = _notice_text(r.text)
    assert "RSS window (days)" in notice
    for other in (
        "Sonarr URL", "Incomplete directory", "Completed directory",
        "Air date tolerance (days)", "Concurrent downloads",
    ):
        assert other not in notice
    assert "restart" in notice.lower()


def test_changing_two_settings_names_both(tmp_path: Path):
    cfg, maps = _full_paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(
            tmp_path, cfg.stat().st_mtime,
            rss_window_days="21", max_concurrent_downloads="3",
        ),
    )

    notice = _notice_text(r.text)
    assert "RSS window (days)" in notice
    assert "Concurrent downloads" in notice
    for other in ("Sonarr URL", "Incomplete directory", "Completed directory",
                  "Air date tolerance (days)"):
        assert other not in notice


def test_changed_settings_notice_never_contains_the_api_key(
    tmp_path: Path, monkeypatch
):
    # The diff runs over raw config keys read from disk, and sonarr_api_key
    # is one of SETTING_FIELDS since 2026-08-25 -- so this now pins the
    # narrower guarantee that survived the reversal: the notice renders
    # field *labels*, never values, and the effective key (here an
    # environment override that differs from the file's) never reaches the
    # page at all. Only the file's own value is rendered, in its form field.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-OVERRIDE-KEY")
    cfg, maps = _full_paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, rss_window_days="21"),
    )

    # The key the running service would actually use never reaches the page.
    assert "ENV-OVERRIDE-KEY" not in r.text
    notice = _notice_text(r.text)
    assert "SECRET-KEY-VALUE" not in notice
    assert "ENV-OVERRIDE-KEY" not in notice


def test_saving_settings_over_a_malformed_config_renders_an_error_not_a_500(
    tmp_path: Path
):
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sonarr_url: [unterminated\n", encoding="utf-8")
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post("/config/settings", data=_form(tmp_path, cfg.stat().st_mtime))

    assert r.status_code == 200  # never a 500
    assert str(cfg) in r.text


def test_a_corrupted_expected_mtime_is_refused_rather_than_written(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    before = cfg.read_bytes()
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post("/config/settings", data=_form(tmp_path, "not-a-number"))

    assert r.status_code == 200
    assert "not a valid number" in r.text.lower()
    assert cfg.read_bytes() == before


def test_a_rejected_save_redisplays_the_values_that_were_submitted(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(
            tmp_path,
            cfg.stat().st_mtime,
            sonarr_url="http://new-sonarr.test:9999",
            rss_window_days="soon",
        ),
    )

    assert r.status_code == 200
    assert 'value="http://new-sonarr.test:9999"' in r.text
    assert 'value="soon"' in r.text
    # the rejected save must not have written the submitted value either
    assert Settings.load(cfg).sonarr_url == "http://sonarr.test:8989"


def _deployed_paths(tmp_path: Path):
    """The shape actually deployed: only the four required keys.

    Not a contrived minimum -- this is what /etc/svtplay-arr/config.yaml
    holds on the live box. Every optional setting is absent, so the service
    runs on `Settings`' dataclass defaults and the form has to render those
    rather than the empty strings the file literally contains. Rendering
    them blank made the browser post "" for each int field, which
    `save_settings` refuses -- so *every* settings save on that deployment
    was refused, including one that only touched an unrelated field.
    """
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        "sonarr_api_key: SECRET-KEY-VALUE\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    return cfg, maps


def _rendered_field_values(body: str) -> dict[str, str]:
    """The settings form exactly as a browser would read it back.

    Keyed by the input's `name`, so a test can post precisely what the
    rendered page would submit instead of hand-writing a form dict that
    quietly disagrees with what the template emits.
    """
    return {
        m.group(1): html_mod.unescape(m.group(2))
        for m in re.finditer(
            r'<input type="(?:text|password)"[^>]*name="([^"]+)"[^>]*'
            r'value="([^"]*)"',
            body,
            re.S,
        )
    }


def _dataclass_default(key: str) -> str:
    """`Settings`' own default for `key`, read off the dataclass.

    The point of these tests is that there is exactly one copy of every
    default; asserting against a literal "1"/"7" here would create the
    second copy the fix exists to avoid.
    """
    import dataclasses

    for f in dataclasses.fields(Settings):
        if f.name == key:
            assert f.default is not dataclasses.MISSING, f"{key} has no default"
            return str(f.default)
    raise AssertionError(f"Settings has no field {key}")


_DEFAULTED_INT_KEYS = (
    "air_date_tolerance_days",
    "rss_window_days",
    "max_concurrent_downloads",
)


def test_a_config_without_the_optional_keys_renders_their_defaults(tmp_path: Path):
    # The live defect, seen from the page: three blank number boxes.
    cfg, maps = _deployed_paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    body = TestClient(app).get("/config/settings").text

    values = _rendered_field_values(body)
    for key in _DEFAULTED_INT_KEYS:
        assert values.get(key) == _dataclass_default(key), (
            f"{key} renders {values.get(key)!r}, not the value the service "
            f"is actually running with"
        )


def test_a_save_of_the_rendered_page_succeeds_on_the_deployed_config(tmp_path: Path):
    # The whole defect end to end: render the page as deployed, post back
    # exactly what that page would submit with nothing touched, and expect
    # it to be accepted. Before the fix this is a 200 whose body carries
    # "'' is not a whole number" and whose file is untouched.
    cfg, maps = _deployed_paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    body = client.get("/config/settings").text
    submitted = _rendered_field_values(body)
    submitted["expected_mtime"] = str(cfg.stat().st_mtime)

    r = client.post("/config/settings", data=submitted)

    assert r.status_code == 200
    assert '<p class="error">' not in r.text, _error_text(r.text)
    # ...and the file still loads, now with the previously-absent keys
    # written out explicitly at the values the service was already using.
    loaded = Settings.load(cfg)
    for key in _DEFAULTED_INT_KEYS:
        assert str(getattr(loaded, key)) == _dataclass_default(key)


def test_saving_the_deployed_config_untouched_reports_nothing_changed(
    tmp_path: Path,
):
    # Writing keys that were previously absent is a change to the file but
    # not a change to what the service does, so it must not raise the
    # restart banner or name every defaulted field in the notice.
    cfg, maps = _deployed_paths(tmp_path)
    app = FastAPI()
    booted = Settings.load(cfg)
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), booted=booted)
    )
    client = TestClient(app)

    submitted = _rendered_field_values(client.get("/config/settings").text)
    submitted["expected_mtime"] = str(cfg.stat().st_mtime)

    r = client.post("/config/settings", data=submitted)

    notice = _notice_text(r.text)
    assert "unchanged" in notice.lower()
    for label in ("Air date tolerance (days)", "RSS window (days)",
                  "Concurrent downloads"):
        assert label not in notice
    assert '<p class="pending">' not in r.text


def test_a_setting_written_in_the_file_still_renders_the_files_value(tmp_path: Path):
    # The other half of the fix: falling back to a default must not start
    # overriding a value the operator actually set.
    cfg, maps = _deployed_paths(tmp_path)
    cfg.write_text(
        cfg.read_text(encoding="utf-8") + "rss_window_days: 21\n",
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    body = TestClient(app).get("/config/settings").text

    values = _rendered_field_values(body)
    assert values["rss_window_days"] == "21"
    assert values["rss_window_days"] != _dataclass_default("rss_window_days")


def test_the_api_key_has_no_default_and_still_renders_blank_when_absent(
    tmp_path: Path,
):
    # sonarr_api_key is required and has no dataclass default, so the
    # defaulting must leave it alone rather than inventing a value -- the
    # env-override warning below it reads `values` to decide whether to say
    # "this field is empty because the file has no key of its own".
    (tmp_path / "i").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sonarr_url: http://sonarr.test:8989\n"
        f"incomplete_dir: {tmp_path}/i\ncompleted_dir: {tmp_path}/c\n",
        encoding="utf-8",
    )
    maps = tmp_path / "mappings.yaml"
    add_mapping(
        maps, tvdb_id=288649, svt_series_id="jpmQD3q",
        svt_slug="gift-vid-forsta-ogonkastet", series_title=TITLE,
        expected_mtime=None,
    )
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    body = TestClient(app).get("/config/settings").text

    assert _rendered_field_values(body)["sonarr_api_key"] == ""


def test_an_unknown_key_survives_a_save_from_the_deployed_config(tmp_path: Path):
    # Round-tripping unknown keys is what lets an operator hand-edit
    # something this form does not offer; writing the newly-defaulted keys
    # must not turn into a rewrite that drops it.
    cfg, maps = _deployed_paths(tmp_path)
    cfg.write_text(
        cfg.read_text(encoding="utf-8") + "some_future_key: keep-me\n",
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    submitted = _rendered_field_values(client.get("/config/settings").text)
    submitted["expected_mtime"] = str(cfg.stat().st_mtime)
    r = client.post("/config/settings", data=submitted)

    assert r.status_code == 200
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["some_future_key"] == (
        "keep-me"
    )


def test_search_shows_svt_hits_and_sonarr_series(tmp_path: Path):
    svt = FakeSvt([SvtSearchHit("jpmQD3q", TITLE, "TvSeries")])
    sonarr = FakeSonarr([{"id": 70, "tvdbId": 288649, "title": TITLE}])
    client = _client(tmp_path, svt=svt, sonarr=sonarr)

    body = client.post("/config/mappings/search", data={"q": "gift"}).text

    assert "jpmQD3q" in body
    assert "288649" in body
    # The pre-filled slug is the value the operator is most likely to accept
    # without checking, and a wrong one produces a mapping that fetches a
    # 404 page and silently grabs nothing. Replacing derive_slug(hit.name)
    # with a constant left all 281 tests green.
    assert derive_slug(TITLE) == "gift-vid-forsta-ogonkastet"
    assert f'value="jpmQD3q|{derive_slug(TITLE)}"' in body


def test_search_survives_svt_being_down(tmp_path: Path):
    class Broken(FakeSvt):
        async def search_series(self, query):
            raise RuntimeError("svt unreachable")

    client = _client(tmp_path, svt=Broken())
    r = client.post("/config/mappings/search", data={"q": "gift"})

    assert r.status_code == 200          # never a 500
    assert "unreachable" in r.text.lower() or "could not" in r.text.lower()


def test_search_offers_manual_entry_when_svt_is_down(tmp_path: Path):
    class Broken(FakeSvt):
        async def search_series(self, query):
            raise RuntimeError("svt unreachable")

    body = _client(tmp_path, svt=Broken()).post(
        "/config/mappings/search", data={"q": "gift"}
    ).text
    assert 'name="svt_series_id"' in body


def test_new_mapping_form_renders(tmp_path: Path):
    assert 'name="q"' in _client(tmp_path).get("/config/mappings/new").text


def test_config_ui_defines_no_svt_slug_convention(tmp_path: Path):
    # This module's whole premise is that it knows nothing about SVT beyond
    # calling the client. Slug derivation is SVT domain knowledge (which
    # diacritics fold to which letter) and belongs in svt/client.py, not
    # here -- so config_ui must not carry its own copy of that table.
    import svtplay_arr.api.config_ui as config_ui

    assert not hasattr(config_ui, "_SLUG_FOLD")
    assert not hasattr(config_ui, "_slugify")


def test_empty_query_does_not_return_the_whole_sonarr_library(tmp_path: Path):
    sonarr = FakeSonarr(
        [{"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(5)]
    )
    client = _client(tmp_path, sonarr=sonarr)

    body = client.post("/config/mappings/search", data={"q": "   "}).text

    assert "Show 0" not in body
    assert "enter a show title" in body.lower()


OTHER = "Vem vet mest?"


def test_creating_a_mapping_copies_the_sonarr_title_verbatim(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))
    client = TestClient(app)

    client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "abc123|vem-vet-mest",
        "sonarr": "999",
    })

    created = MappingTable.load(maps).for_tvdb(999)
    assert created is not None
    assert created.series_title == OTHER      # byte-identical, never typed
    assert created.svt_series_id == "abc123"
    assert created.svt_slug == "vem-vet-mest"


def test_creating_a_duplicate_is_refused_without_a_500(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "x|y",
        "sonarr": "288649",
    })

    assert r.status_code == 200
    error = _error_text(r.text)
    assert "already mapped" in error.lower()
    # The spec's Testing table requires the error to name the existing row,
    # not just say "duplicate" -- otherwise the operator has to go read the
    # file to find out what they collided with. Asserted inside the banner,
    # because the mappings table on the same page holds TITLE regardless.
    assert TITLE in error


def test_creating_with_an_unknown_sonarr_id_is_refused(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr([])))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "x|y",
        "sonarr": "12345",
    })

    assert r.status_code == 200
    assert MappingTable.load(maps).for_tvdb(12345) is None


def test_deleting_a_mapping_removes_it(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    client.post(f"/config/mappings/288649/delete",
                data={"expected_mtime": str(maps.stat().st_mtime)})

    assert MappingTable.load(maps).all() == []


def test_deleting_an_unknown_mapping_is_refused_without_a_500(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post("/config/mappings/424242/delete",
                    data={"expected_mtime": str(maps.stat().st_mtime)})

    assert r.status_code == 200
    assert MappingTable.load(maps).for_tvdb(288649) is not None


def test_creating_strips_whitespace_from_manually_entered_svt_fields(
    tmp_path: Path
):
    # Task 7's manual-entry fallback (SVT search unreachable) lets a human
    # type the id and slug by hand; untrimmed whitespace would corrupt the
    # slug used to build SVT Play URLs.
    cfg, maps = _paths(tmp_path)
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))
    client = TestClient(app)

    client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "svt_series_id": "  abc123  ",
        "svt_slug": "  vem-vet-mest  ",
        "sonarr": "999",
    })

    created = MappingTable.load(maps).for_tvdb(999)
    assert created is not None
    assert created.svt_series_id == "abc123"
    assert created.svt_slug == "vem-vet-mest"


def test_a_malformed_svt_radio_value_is_refused_without_a_500(tmp_path: Path):
    # The "svt" field is a single form value encoding "id|slug". svt_id
    # comes unvalidated from SVT's API, so a value containing more than one
    # "|" must fail cleanly rather than silently producing a wrong id or
    # slug (e.g. via partition-on-first-pipe).
    cfg, maps = _paths(tmp_path)
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "abc|123|vem-vet-mest",
        "sonarr": "999",
    })

    assert r.status_code == 200
    assert MappingTable.load(maps).for_tvdb(999) is None


def test_creating_a_mapping_when_sonarr_is_unreachable_is_refused(tmp_path: Path):
    # The operator picked a series from a list that rendered fine seconds
    # earlier; if Sonarr goes down between then and submission, the route
    # must fail cleanly and say why -- never write an incomplete row.
    cfg, maps = _paths(tmp_path)

    class Broken(FakeSonarr):
        async def all_series(self):
            raise RuntimeError("sonarr unreachable")

    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), Broken()))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "abc123|vem-vet-mest",
        "sonarr": "999",
    })

    assert r.status_code == 200
    assert "unreachable" in r.text.lower() or "sonarr" in r.text.lower()
    assert MappingTable.load(maps).for_tvdb(999) is None


def test_creating_with_a_corrupted_expected_mtime_is_refused(tmp_path: Path):
    # A present-but-unparseable expected_mtime must not be silently treated
    # as "no token at all" (which would disable the staleness check) --
    # same guard as the settings route.
    cfg, maps = _paths(tmp_path)
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "expected_mtime": "not-a-number",
        "svt": "abc123|vem-vet-mest",
        "sonarr": "999",
    })

    assert r.status_code == 200
    assert "not a valid number" in r.text.lower()
    assert MappingTable.load(maps).for_tvdb(999) is None


def test_deleting_with_a_corrupted_expected_mtime_is_refused(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post("/config/mappings/288649/delete",
                    data={"expected_mtime": "not-a-number"})

    assert r.status_code == 200
    assert "not a valid number" in r.text.lower()
    assert MappingTable.load(maps).for_tvdb(288649) is not None


def test_creating_strips_whitespace_from_the_radio_encoded_svt_value(
    tmp_path: Path
):
    # svt_id is unvalidated data from SVT's API; stray whitespace on either
    # half of the "id|slug" encoding should be trimmed the same way the
    # manual-entry fields are, not just the manually-typed fallback.
    cfg, maps = _paths(tmp_path)
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))
    client = TestClient(app)

    client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "  abc123  |  vem-vet-mest  ",
        "sonarr": "999",
    })

    created = MappingTable.load(maps).for_tvdb(999)
    assert created is not None
    assert created.svt_series_id == "abc123"
    assert created.svt_slug == "vem-vet-mest"


def test_search_survives_a_malformed_mappings_file(tmp_path: Path):
    # The fourth occurrence of one pattern on this branch: a guard applied
    # to one route and not its siblings. GET /config renders 200 with an
    # error banner for this file; POST /config/mappings/search returned a
    # 500.
    cfg, maps = _paths(tmp_path)
    maps.write_text("series: [unterminated\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))

    r = TestClient(app).post("/config/mappings/search", data={"q": "gift"})

    assert r.status_code == 200  # never a 500
    assert str(maps) in r.text


def test_search_survives_an_unreadable_mappings_file(tmp_path: Path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    cfg, maps = _paths(tmp_path)
    maps.chmod(0o000)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    try:
        r = TestClient(app).post("/config/mappings/search", data={"q": "gift"})
        assert r.status_code == 200  # never a 500
        assert str(maps) in r.text
    finally:
        maps.chmod(0o644)


def test_every_config_route_is_async(tmp_path: Path):
    # JobStore holds one sqlite3.Connection behind a blocking lock, and
    # FastAPI runs a non-async route in a threadpool -- empirically shown to
    # corrupt reads. Mutating `index` to a plain `def` left all 281 tests
    # green, so the rule held only by convention. Sweeping the router makes
    # it enforce itself for the next route someone adds.
    import inspect

    cfg, maps = _paths(tmp_path)
    router = build_config_router(cfg, maps, FakeSvt(), FakeSonarr())
    endpoints = [r for r in router.routes if hasattr(r, "endpoint")]
    assert endpoints, "no routes to check -- the sweep would pass vacuously"
    for route in endpoints:
        assert inspect.iscoroutinefunction(route.endpoint), (
            f"{route.path} is not async def"
        )


def test_a_path_change_without_the_checkbox_is_refused(tmp_path: Path):
    # Changing these under a running worker orphans in-flight downloads. The
    # spec makes the confirmation a hard requirement; mutating the guard to
    # `if False:` left all 281 tests green.
    cfg, maps = _paths(tmp_path)
    (tmp_path / "c2").mkdir()
    before = cfg.read_bytes()
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, completed_dir=f"{tmp_path}/c2"),
    )

    assert r.status_code == 200
    # Inside the banner: "completed_dir" is a form field name and appears on
    # the page whether or not the guard fired.
    error = _error_text(r.text)
    assert "completed_dir" in error
    assert "confirmation" in error.lower()
    assert cfg.read_bytes() == before


def test_a_path_change_with_the_checkbox_is_applied(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    (tmp_path / "c2").mkdir()
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(
            tmp_path, cfg.stat().st_mtime,
            completed_dir=f"{tmp_path}/c2", confirm_paths="yes",
        ),
    )

    assert r.status_code == 200
    assert Settings.load(cfg).completed_dir == tmp_path / "c2"


def test_a_non_path_change_needs_no_checkbox(tmp_path: Path):
    # The confirmation is required for a path change *specifically* -- if it
    # were demanded for every save, the box would become something the
    # operator ticks by reflex and would stop meaning anything.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, rss_window_days="21"),
    )

    assert r.status_code == 200
    assert Settings.load(cfg).rss_window_days == 21


def test_a_form_supplied_series_title_is_ignored(tmp_path: Path):
    # The entire reason this page exists. Sonarr runs renameEpisodes=False,
    # so series_title is the permanent filename and may only ever come from
    # Sonarr's own record. The happy-path test posts no rival value, so it
    # proves the flow, not the guarantee: mutating the code to prefer
    # form.get("series_title") left all 281 tests green.
    cfg, maps = _paths(tmp_path)
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))
    client = TestClient(app)

    client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "abc123|vem-vet-mest",
        "sonarr": "999",
        "series_title": "WRONG TITLE FROM FORM",
    })

    created = MappingTable.load(maps).for_tvdb(999)
    assert created is not None
    assert created.series_title == OTHER
    assert "WRONG TITLE FROM FORM" not in maps.read_text(encoding="utf-8")


def test_the_typed_query_is_never_used_as_the_series_title(tmp_path: Path):
    """The mutation this exists for survived the whole suite.

    Changing the accept route to
    `series_title=str(form.get("q") or match.get("title") or "")` passed
    every test: `test_a_form_supplied_series_title_is_ignored` posts a
    `series_title` key the code never reads, and every other accept test
    posts a `q` that happens to *equal* Sonarr's title.

    Every accept form on the sweep and search pages posts `q`, and on the
    search page `q` is whatever the operator typed. So a future "reuse the
    query we already have" refactor would land green and file every
    episode of the series as `gift - S01E01`, permanently, because Sonarr
    runs with renameEpisodes=False.
    """
    cfg, maps = _paths(tmp_path)
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))
    client = TestClient(app)

    # What someone actually types to find "Vem vet mest?": not its title.
    client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "q": "vem vet",
        "svt": "abc123|vem-vet-mest",
        "sonarr": "999",
    })

    created = MappingTable.load(maps).for_tvdb(999)
    assert created is not None
    assert created.series_title == OTHER
    assert "vem vet\n" not in maps.read_text(encoding="utf-8")


def test_the_notice_names_sonarrs_title_not_the_query(tmp_path: Path):
    # The same guarantee one step further out: the confirmation an
    # operator reads must name what was actually written, or a wrong title
    # is invisible until it reaches the filesystem.
    cfg, maps = _paths(tmp_path)
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))

    r = TestClient(app).post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "q": "vem vet",
        "svt": "abc123|vem-vet-mest",
        "sonarr": "999",
    })

    assert OTHER in _notice_text(r.text)
    assert "vem vet" not in _notice_text(r.text)


def _pending_text(html: str) -> str:
    m = re.search(r'<p class="pending">(.*?)</p>', html, re.S)
    assert m, f"no pending-restart banner in:\n{html}"
    return m.group(1)


def _booted_client(tmp_path: Path):
    """A router that knows the Settings the service actually booted with."""
    cfg, maps = _full_paths(tmp_path)
    booted = Settings.load(cfg)
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), booted=booted)
    )
    return cfg, TestClient(app)


def test_a_pending_restart_survives_the_post_that_caused_it(tmp_path: Path):
    # The scenario: change air_date_tolerance_days from 1 to 3, see the
    # banner, get distracted, never restart. Next week open /config, read
    # 3, and reason about resolver behaviour on that basis. It is running
    # with 1. The POST-only notice never said so on that later GET.
    cfg, client = _booted_client(tmp_path)

    client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, air_date_tolerance_days="3"),
    )
    body = client.get("/config").text

    pending = _pending_text(body)
    assert "Air date tolerance (days)" in pending
    assert "restart" in pending.lower()
    # ...and it names only the field that is actually pending.
    for other in ("Sonarr URL", "Incomplete directory", "Completed directory",
                  "RSS window (days)", "Concurrent downloads"):
        assert other not in pending


def test_no_pending_change_means_no_banner(tmp_path: Path):
    _, client = _booted_client(tmp_path)
    assert '<p class="pending">' not in client.get("/config").text


def test_a_save_that_changes_nothing_leaves_no_pending_banner(tmp_path: Path):
    cfg, client = _booted_client(tmp_path)
    client.post("/config/settings", data=_form(tmp_path, cfg.stat().st_mtime))
    assert '<p class="pending">' not in client.get("/config").text


def test_the_pending_banner_clears_once_the_file_matches_again(tmp_path: Path):
    cfg, client = _booted_client(tmp_path)

    client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, rss_window_days="21"),
    )
    assert '<p class="pending">' in client.get("/config").text

    client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, rss_window_days="14"),
    )
    assert '<p class="pending">' not in client.get("/config").text


def test_the_pending_banner_names_the_api_key_but_never_shows_it(
    tmp_path: Path, monkeypatch
):
    # Replaces test_the_pending_banner_never_names_the_api_key. Now that the
    # key is editable, a key on disk that differs from the one the service
    # booted with is a genuine pending restart -- and the worst kind, since
    # the running service goes on authenticating with the old value. The
    # banner must say so. What must not change is that it names the *label*:
    # neither the booted key nor any other value may reach the banner.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-OVERRIDE-KEY")
    cfg, maps = _full_paths(tmp_path)
    booted = Settings.load(cfg)
    monkeypatch.delenv("SONARR_API_KEY")
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), booted=booted)
    )
    body = TestClient(app).get("/config/settings").text

    pending = _pending_text(body)
    assert "Sonarr API key" in pending
    assert "SECRET-KEY-VALUE" not in pending
    assert "ENV-OVERRIDE-KEY" not in pending
    # The effective key the service booted with never reaches the page at
    # all -- only the file's own value, in its form field.
    assert "ENV-OVERRIDE-KEY" not in body


def test_no_pending_api_key_banner_while_the_environment_overrides_it(
    tmp_path: Path, monkeypatch
):
    # With $SONARR_API_KEY set the file and the booted value differ
    # permanently: no restart can reconcile them. A banner saying "restart
    # svtplay-arr to apply: Sonarr API key" would be untrue and
    # unclearable, sending the operator to do something that cannot work.
    # The warning beside the field states the real situation instead.
    monkeypatch.setenv("SONARR_API_KEY", "ENV-OVERRIDE-KEY")
    cfg, maps = _full_paths(tmp_path)
    booted = Settings.load(cfg)
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), booted=booted)
    )
    body = TestClient(app).get("/config/settings").text

    assert '<p class="pending">' not in body
    assert "svtplay-arr.service" in body  # the env warning is there instead


def test_saving_a_new_api_key_raises_the_pending_restart_banner(tmp_path: Path):
    # The point of making the key editable is that it takes effect; the
    # point of the banner is that until the restart it has not.
    cfg, client = _booted_client(tmp_path)

    client.post(
        "/config/settings",
        data=_form(tmp_path, cfg.stat().st_mtime, sonarr_api_key="NEW-KEY-VALUE"),
    )
    pending = _pending_text(client.get("/config/settings").text)

    assert "Sonarr API key" in pending
    assert "NEW-KEY-VALUE" not in pending


def test_the_page_still_renders_without_booted_settings(tmp_path: Path):
    # build_config_router is called without `booted` throughout the tests
    # and could be in a deployment where Settings was constructed directly.
    # A missing baseline degrades to no banner, never to a broken page.
    body = _client(tmp_path).get("/config/settings").text
    assert "sonarr_url" in body
    assert '<p class="pending">' not in body


def test_an_unloadable_config_file_degrades_to_no_pending_banner(tmp_path: Path):
    # A config-page defect must never be the reason /config stops working.
    cfg, maps = _full_paths(tmp_path)
    booted = Settings.load(cfg)
    cfg.write_text("sonarr_url: [unterminated\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), booted=booted)
    )
    r = TestClient(app).get("/config")

    assert r.status_code == 200
    assert '<p class="pending">' not in r.text


def test_a_padded_but_unchanged_path_does_not_demand_the_checkbox(tmp_path: Path):
    # save_settings stores the stripped value, so the changed-paths check
    # has to compare against what will actually be written. Comparing the
    # raw submission made re-submitting an unchanged directory with a stray
    # space produce the page's most alarming message -- "orphans in-flight
    # downloads" -- for a no-op, training the operator to tick the box past
    # a warning that is usually wrong.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(
            tmp_path, cfg.stat().st_mtime,
            completed_dir=f"  {tmp_path}/c  ",
        ),
    )

    assert r.status_code == 200
    assert '<p class="error">' not in r.text
    assert Settings.load(cfg).completed_dir == tmp_path / "c"


def test_a_padded_genuinely_changed_path_still_demands_the_checkbox(tmp_path: Path):
    # Stripping must not become a way to slip a real path change past the
    # confirmation.
    cfg, maps = _paths(tmp_path)
    (tmp_path / "c2").mkdir()
    before = cfg.read_bytes()
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    client = TestClient(app)

    r = client.post(
        "/config/settings",
        data=_form(
            tmp_path, cfg.stat().st_mtime,
            completed_dir=f"  {tmp_path}/c2  ",
        ),
    )

    assert "completed_dir" in _error_text(r.text)
    assert cfg.read_bytes() == before


# --- A failed mapping create re-renders the search page, not the index ---
#
# Before this, `create_mapping` always fell back to `_index` on any
# failure, throwing away the SVT/Sonarr picker and the operator's
# selections -- forcing a redo of the search for exactly the moment they
# were already dealing with a problem. Each test below drives one of the
# realistic ways the create can fail and checks: the *search results*
# page comes back (not the index), the operator's picks are still
# selected, and mappings.yaml is untouched.


def _is_search_results_page(html: str) -> bool:
    """True for mapping_search.html, false for index.html.

    index.html has no "Add a series:" heading and mapping_search.html has
    no "<h2>Mappings</h2>" table heading -- either alone distinguishes
    them, checking both guards against a future template rename silently
    breaking this signal in only one template.
    """
    return "Add a series:" in html and "<h2>Mappings</h2>" not in html


class _CheckedInputs(HTMLParser):
    """Collects (name, value) for every <input> that carries `checked`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.checked: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        attributes = dict(attrs)
        if "checked" in attributes:
            self.checked.append(
                (attributes.get("name", ""), attributes.get("value", ""))
            )


def _checked_values(html: str, name: str) -> set[str]:
    """The values of the checked inputs in the `name` radio group.

    Parsed, not substring-matched. These assertions cover one of this
    branch's more important guarantees -- after a failed create the
    operator's radio selections survive, so they never have to re-read a
    slug off an SVT URL and hand-type it -- and they used to be written
    as a substring carrying mapping_search.html's own line break and its
    eight spaces of indentation. That made the template's whitespace
    load-bearing: reformatting the file broke a test that had nothing to
    say about formatting, which teaches the next person to "adjust the
    test to match", and that is how a real assertion gets quietly
    hollowed out. Reading the parsed attributes instead is indifferent to
    attribute order, quoting and line breaks, and fails only when a
    selection genuinely fails to survive.

    Returned as a set so a call site can assert the whole group at once:
    exactly one radio checked, and it is the operator's pick.
    """
    parser = _CheckedInputs()
    parser.feed(html)
    parser.close()
    return {value for input_name, value in parser.checked if input_name == name}


def test_a_duplicate_create_failure_shows_the_search_results_with_selections(
    tmp_path: Path,
):
    # The most common real failure: the operator picked a Sonarr series
    # that is already mapped. The spec requires the error to name the
    # existing row *and* for the picker to survive so they can pick a
    # different series without redoing the search.
    cfg, maps = _paths(tmp_path)  # tvdbId 288649 already mapped to TITLE
    before = maps.read_bytes()
    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    svt = FakeSvt([SvtSearchHit("jpmQD3q", TITLE, "TvSeries")])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, svt, sonarr))
    client = TestClient(app)
    slug = derive_slug(TITLE)

    r = client.post("/config/mappings", data={
        "q": "gift",
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": f"jpmQD3q|{slug}",
        "sonarr": "288649",
    })

    assert r.status_code == 200
    assert _is_search_results_page(r.text)
    error = _error_text(r.text)
    assert "already mapped" in error.lower()
    assert TITLE in error
    # The chosen SVT hit and Sonarr series are still selected -- and
    # nothing else in either group is.
    assert _checked_values(r.text, "svt") == {f"jpmQD3q|{slug}"}
    assert _checked_values(r.text, "sonarr") == {"288649"}
    assert maps.read_bytes() == before  # refused write leaves the file alone


def test_sonarr_unreachable_at_confirm_shows_the_search_results(tmp_path: Path):
    # The operator picked a series from a list that rendered fine seconds
    # earlier; Sonarr going down before they hit submit must not also cost
    # them the picker.
    cfg, maps = _paths(tmp_path)
    before = maps.read_bytes()

    class Broken(FakeSonarr):
        async def all_series(self):
            raise RuntimeError("sonarr unreachable")

    svt = FakeSvt([SvtSearchHit("abc123", OTHER, "TvSeries")])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, svt, Broken()))
    client = TestClient(app)
    slug = derive_slug(OTHER)

    r = client.post("/config/mappings", data={
        "q": "vem",
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": f"abc123|{slug}",
        "sonarr": "999",
    })

    assert r.status_code == 200
    assert _is_search_results_page(r.text)
    error = _error_text(r.text)
    assert "sonarr" in error.lower()
    assert "not saved" in error.lower()
    assert maps.read_bytes() == before
    assert MappingTable.load(maps).for_tvdb(999) is None


def test_a_malformed_svt_pair_shows_the_search_results_with_the_sonarr_pick(
    tmp_path: Path,
):
    cfg, maps = _paths(tmp_path)
    before = maps.read_bytes()
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    svt = FakeSvt([SvtSearchHit("abc123", OTHER, "TvSeries")])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, svt, sonarr))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "q": "vem",
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "abc|123|vem-vet-mest",  # more than one "|"
        "sonarr": "999",
    })

    assert r.status_code == 200
    assert _is_search_results_page(r.text)
    error = _error_text(r.text)
    assert "malformed" in error.lower()
    assert _checked_values(r.text, "sonarr") == {"999"}
    assert maps.read_bytes() == before
    assert MappingTable.load(maps).for_tvdb(999) is None


def test_a_stale_expected_mtime_shows_the_search_results(tmp_path: Path):
    # Concurrent modification: mappings.yaml changed since the search page
    # was rendered (a hand edit, or another tab), so the mtime the confirm
    # form carries no longer matches. This is a distinct failure from the
    # "corrupted" case below -- a well-formed but out-of-date number.
    cfg, maps = _paths(tmp_path)
    stale_mtime = maps.stat().st_mtime
    before = maps.read_bytes()
    # Bump the file's mtime without changing its content, simulating a
    # concurrent write that lands between the search render and the confirm.
    os.utime(maps, (maps.stat().st_atime, stale_mtime + 5))

    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    svt = FakeSvt([SvtSearchHit("abc123", OTHER, "TvSeries")])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, svt, sonarr))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "q": "vem",
        "expected_mtime": str(stale_mtime),
        "svt": "abc123|vem-vet-mest",
        "sonarr": "999",
    })

    assert r.status_code == 200
    assert _is_search_results_page(r.text)
    error = _error_text(r.text)
    assert "changed since" in error.lower()
    assert _checked_values(r.text, "sonarr") == {"999"}
    assert maps.read_bytes() == before
    assert MappingTable.load(maps).for_tvdb(999) is None


def test_a_corrupted_expected_mtime_at_confirm_shows_the_search_results(
    tmp_path: Path,
):
    cfg, maps = _paths(tmp_path)
    before = maps.read_bytes()
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    svt = FakeSvt([SvtSearchHit("abc123", OTHER, "TvSeries")])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, svt, sonarr))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "q": "vem",
        "expected_mtime": "not-a-number",
        "svt": "abc123|vem-vet-mest",
        "sonarr": "999",
    })

    assert r.status_code == 200
    assert _is_search_results_page(r.text)
    error = _error_text(r.text)
    assert "not a valid number" in error.lower()
    assert _checked_values(r.text, "sonarr") == {"999"}
    assert maps.read_bytes() == before


def test_the_original_error_survives_when_the_search_rerun_also_fails(
    tmp_path: Path,
):
    # The case most likely to be got wrong: SVT is down, which is why the
    # operator fell back to manual id/slug entry -- and it is *also* why
    # rebuilding the search page (to redisplay it after the create failed
    # for an unrelated reason) fails the same way. The banner must keep
    # showing the reason the *create* failed ("already mapped"), never a
    # secondary "SVT search failed" from the rebuild attempt that
    # replaced it.
    cfg, maps = _paths(tmp_path)  # tvdbId 288649 already mapped to TITLE
    before = maps.read_bytes()

    class BrokenSvt(FakeSvt):
        async def search_series(self, query):
            raise RuntimeError("svt unreachable")

    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, BrokenSvt(), sonarr))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "q": "gift",
        "expected_mtime": str(maps.stat().st_mtime),
        "svt_series_id": "abc123",
        "svt_slug": "vem-vet-mest",
        "sonarr": "288649",
    })

    assert r.status_code == 200
    assert _is_search_results_page(r.text)
    error = _error_text(r.text)
    # The real cause is shown...
    assert "already mapped" in error.lower()
    assert TITLE in error
    # ...never buried under, or replaced by, the rerun's own failure.
    assert "svt search failed" not in error.lower()
    assert "svt unreachable" not in error.lower()
    # The manually-typed SVT fields are preserved (SVT is still down, so
    # the manual-entry fallback is what renders again), and so is the
    # Sonarr pick.
    assert 'value="abc123"' in r.text
    assert 'value="vem-vet-mest"' in r.text
    assert _checked_values(r.text, "sonarr") == {"288649"}
    assert maps.read_bytes() == before


def test_a_radio_selected_svt_hit_survives_a_failed_create(tmp_path: Path):
    # B's own headline scenario, and the one it got wrong: the operator
    # picked an SVT *radio*, so their choice arrived as the single
    # "svt_id|slug" field -- which `_search_failure_response` never
    # decomposed. With SVT flapping (the search that produced the radio
    # worked, the re-run after the failed create does not) the manual
    # fallback renders, and both boxes came back empty while the server
    # was holding the correct id and slug all along. The Sonarr pick
    # survived; the SVT pick did not, forcing exactly the slug
    # transcription off a URL that this page exists to eliminate.
    cfg, maps = _paths(tmp_path)  # tvdbId 288649 already mapped to TITLE
    before = maps.read_bytes()

    class BrokenSvt(FakeSvt):
        async def search_series(self, query):
            raise RuntimeError("svt unreachable")

    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, BrokenSvt(), sonarr))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        "q": "gift",
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "jpmQD3q|gift-vid-forsta-ogonkastet",
        "sonarr": "288649",
    })

    assert r.status_code == 200
    assert _is_search_results_page(r.text)
    assert "already mapped" in _error_text(r.text).lower()
    # No radios to re-check, so the manual-entry fallback is what renders...
    assert 'id="svt_series_id"' in r.text
    # ...and it is pre-filled from the radio value the form carried.
    assert 'value="jpmQD3q"' in r.text
    assert 'value="gift-vid-forsta-ogonkastet"' in r.text
    assert _checked_values(r.text, "sonarr") == {"288649"}
    assert maps.read_bytes() == before


def test_a_radio_selected_svt_hit_is_still_reselected_when_the_rerun_works(
    tmp_path: Path,
):
    # The other half: when the re-run does return the hit, decomposing the
    # radio value must not stop it being re-checked as a radio.
    cfg, maps = _paths(tmp_path)  # tvdbId 288649 already mapped to TITLE
    slug = derive_slug(TITLE)
    svt = FakeSvt([SvtSearchHit("jpmQD3q", TITLE, "TvSeries")])
    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, svt, sonarr))

    r = TestClient(app).post("/config/mappings", data={
        "q": "gift",
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": f"jpmQD3q|{slug}",
        "sonarr": "288649",
    })

    assert _checked_values(r.text, "svt") == {f"jpmQD3q|{slug}"}
    # ...and the manual boxes are not rendered at all in this branch.
    assert 'id="svt_series_id"' not in r.text


def test_a_create_failure_with_no_query_falls_back_to_the_index(tmp_path: Path):
    # If there is nothing to search with (e.g. a pre-existing bookmarked
    # form, or a client that never carried the hidden "q" field), rebuilding
    # the picker is not possible. Falling back to the index with the real
    # error beats a broken or empty search page.
    cfg, maps = _paths(tmp_path)
    before = maps.read_bytes()
    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), sonarr))
    client = TestClient(app)

    r = client.post("/config/mappings", data={
        # no "q" field at all
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "x|y",
        "sonarr": "288649",
    })

    assert r.status_code == 200
    assert not _is_search_results_page(r.text)
    assert "<h2>Mappings</h2>" in r.text  # the index page
    error = _error_text(r.text)
    assert "already mapped" in error.lower()
    assert maps.read_bytes() == before


def test_the_search_query_round_trips_through_the_confirm_form(tmp_path: Path):
    # The confirm form must carry the original query, or a failed create
    # has nothing to rebuild the search from.
    svt = FakeSvt([SvtSearchHit("jpmQD3q", TITLE, "TvSeries")])
    sonarr = FakeSonarr([{"id": 70, "tvdbId": 288649, "title": TITLE}])
    client = _client(tmp_path, svt=svt, sonarr=sonarr)

    body = client.post("/config/mappings/search", data={"q": "gift"}).text

    assert 'name="q" value="gift"' in body


def _episodes(n: int) -> list[SvtEpisode]:
    return [
        SvtEpisode(
            svt_id=f"ep{i}", title=f"Episode {i}", url=f"/video/ep{i}",
            ordinal=i, published=None, available=True, duration_s=None,
        )
        for i in range(n)
    ]


def test_check_reports_episodes_found(tmp_path: Path):
    svt = FakeSvt(episodes=_episodes(5))
    r = _client(tmp_path, svt=svt).post("/config/mappings/288649/check")

    assert r.status_code == 200
    assert svt.list_episodes_calls == ["gift-vid-forsta-ogonkastet"]
    assert "SVT lists 5 episode" in r.text
    # The result must not be readable as "the mapping is correct" -- a
    # valid slug for the wrong show looks identical to a right one.
    assert "does not confirm this mapping points at the right show" in r.text


def test_check_reports_nothing_found(tmp_path: Path):
    svt = FakeSvt(episodes=[])
    r = _client(tmp_path, svt=svt).post("/config/mappings/288649/check")

    assert r.status_code == 200
    assert "no episodes" in r.text.lower()


def test_check_reports_a_404_as_nothing_found(tmp_path: Path):
    svt = FakeSvt(list_episodes_error=SvtApiError("show page request failed", status_code=404))
    r = _client(tmp_path, svt=svt).post("/config/mappings/288649/check")

    assert r.status_code == 200
    assert "404" in r.text
    assert "probably wrong" in r.text


def test_check_reports_an_svt_error_with_the_error(tmp_path: Path):
    svt = FakeSvt(list_episodes_error=SvtApiError("show page request for 'x' failed: boom"))
    r = _client(tmp_path, svt=svt).post("/config/mappings/288649/check")

    assert r.status_code == 200
    assert "could not be checked" in r.text
    assert "boom" in r.text


def test_the_empty_result_message_names_a_parse_failure_too(tmp_path: Path):
    # `parse_show_page` is a regex scan over SVT's escaped payload, so a
    # markup change on SVT's side returns [] from a perfectly valid 200
    # for a perfectly correct slug. In that outage the resolver goes quiet
    # as well, the operator checks every row, and being told the slug is
    # probably wrong for all of them points them away from the parser --
    # the one thing that actually needs fixing. Name it as a cause.
    svt = FakeSvt(episodes=[])
    payload = _client(tmp_path, svt=svt).post(
        "/config/mappings/288649/check", headers={"Accept": "application/json"}
    ).json()
    message = payload["message"].lower()

    assert "no episodes" in message
    # All three causes, not the two it used to hedge between.
    assert "slug" in message
    assert "ended" in message
    assert "svtplay-arr" in message
    assert "pars" in message  # parse / parsing / parser
    # Wording only: the outcome and the colour it drives are unchanged.
    assert payload["outcome"] == "not_found"
    assert payload["css_class"] == "warn"
    assert payload["episode_count"] == 0


def test_the_check_css_classes_mean_what_the_page_says_they_mean():
    # `test_check_json_and_html_paths_agree` proves both response paths
    # read the *same* mapping; it never proves the mapping is right.
    # Swapping found->error and error->warn left all 399 tests green, so a
    # successful check could render in red behind a ✕ while reading "SVT
    # lists 5 episodes". Section A deliberately made colour and glyph
    # carry meaning, so the meaning has to be asserted somewhere.
    from svtplay_arr.api.config_ui import _CHECK_CSS_CLASS

    assert _CHECK_CSS_CLASS == {
        "found": "notice",  # .notice -- ✓, success colours
        "not_found": "warn",  # .warn -- ⚠, caution colours
        "error": "error",  # .error -- ✕, failure colours
        "unknown_mapping": "error",
    }


@pytest.mark.parametrize(
    "make_svt, outcome, css_class",
    [
        (lambda: FakeSvt(episodes=_episodes(5)), "found", "notice"),
        (lambda: FakeSvt(episodes=[]), "not_found", "warn"),
        (
            lambda: FakeSvt(
                list_episodes_error=SvtApiError("gone", status_code=404)
            ),
            "not_found",
            "warn",
        ),
        (lambda: FakeSvt(list_episodes_error=SvtApiError("boom")), "error", "error"),
    ],
    ids=["found", "empty-200", "404", "svt-error"],
)
def test_each_check_outcome_renders_the_colour_it_means(
    tmp_path: Path, make_svt, outcome, css_class,
):
    # The end-to-end half of the assertion above: whatever the mapping
    # says has to reach both the JSON the JS patches a row with and the
    # class attribute the no-JS re-render emits.
    client = _client(tmp_path, svt=make_svt())

    payload = client.post(
        "/config/mappings/288649/check", headers={"Accept": "application/json"}
    ).json()
    assert payload["outcome"] == outcome
    assert payload["css_class"] == css_class

    html = client.post("/config/mappings/288649/check").text
    assert f'class="check-result {css_class}"' in html


def test_an_unknown_mapping_check_renders_as_an_error(tmp_path: Path):
    # No row to attach to, so this takes index.html's fallback paragraph
    # rather than the in-row element -- the class still has to be the
    # failure one.
    client = _client(tmp_path)

    payload = client.post(
        "/config/mappings/99999/check", headers={"Accept": "application/json"}
    ).json()
    assert payload["outcome"] == "unknown_mapping"
    assert payload["css_class"] == "error"

    html = client.post("/config/mappings/99999/check").text
    assert '<p class="error">Check for tvdb_id 99999' in html


def test_check_never_runs_on_page_load(tmp_path: Path):
    # The constraint most likely to be violated by a later well-meaning
    # edit: rendering GET /config must never itself call SVT.
    svt = FakeSvt(episodes=_episodes(3))
    _client(tmp_path, svt=svt).get("/config")
    assert svt.list_episodes_calls == []


def test_check_never_runs_on_any_other_post_either(tmp_path: Path):
    # Saving settings, searching, creating or deleting a mapping must not
    # incidentally trigger a check of anything. Every write route re-renders
    # a page through `_index`, which takes a `check` argument -- so "some
    # other route started passing one" is a live way for SVT to end up
    # being called on a page nobody asked to check. Cover all four, not
    # just the settings POST this test used to stop at.
    svt = FakeSvt(episodes=_episodes(3))
    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    client = _client(tmp_path, svt=svt, sonarr=sonarr)
    maps = tmp_path / "mappings.yaml"  # already written by _client's _paths

    client.post(
        "/config/settings",
        data={"sonarr_url": "http://sonarr.test:8989", "sonarr_api_key": "x",
              "incomplete_dir": str(tmp_path / "i"), "completed_dir": str(tmp_path / "c"),
              "air_date_tolerance_days": "1", "rss_window_days": "14",
              "max_concurrent_downloads": "1"},
    )
    assert svt.list_episodes_calls == [], "settings save called SVT"

    client.post("/config/mappings/search", data={"q": "gift"})
    assert svt.list_episodes_calls == [], "search called SVT's episode list"

    # A create that fails (288649 is already mapped) re-renders the search
    # page; one that succeeds re-renders the index. Drive both.
    client.post("/config/mappings", data={
        "q": "gift", "expected_mtime": str(maps.stat().st_mtime),
        "svt": "jpmQD3q|gift-vid-forsta-ogonkastet", "sonarr": "288649",
    })
    assert svt.list_episodes_calls == [], "a failed create called SVT"

    client.post("/config/mappings/288649/delete", data={
        "expected_mtime": str(maps.stat().st_mtime),
    })
    assert svt.list_episodes_calls == [], "delete called SVT"

    client.post("/config/mappings", data={
        "q": "gift", "expected_mtime": str(maps.stat().st_mtime),
        "svt": "jpmQD3q|gift-vid-forsta-ogonkastet", "sonarr": "288649",
    })
    assert svt.list_episodes_calls == [], "a successful create called SVT"


def test_check_writes_nothing(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    before_cfg = cfg.read_bytes()
    before_maps = maps.read_bytes()
    svt = FakeSvt(episodes=_episodes(2))
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, svt, FakeSonarr()))
    TestClient(app).post("/config/mappings/288649/check")

    assert cfg.read_bytes() == before_cfg
    assert maps.read_bytes() == before_maps


def test_the_no_js_check_response_is_a_page_not_a_json_blob(tmp_path: Path):
    # `check_mapping` picks its response shape off the Accept header, and
    # every other test on this route asserts substrings of the *message* --
    # which appears verbatim in the JSON too. So they all stayed green with
    # the branch forced to JSON, and a no-JS operator's form POST would
    # dump a raw JSON blob into their browser. Pin the shape, not just the
    # words: the content type, and a marker only the full page emits.
    svt = FakeSvt(episodes=_episodes(5))
    r = _client(tmp_path, svt=svt).post("/config/mappings/288649/check")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.text.lstrip().lower().startswith("<!doctype html>")
    # Page furniture that no JSON payload could ever contain. The no-JS
    # branch re-renders the view the Check button lives on, so this is the
    # Mappings view and its nav, not the settings form it used to sit
    # underneath.
    assert "<h2>Mappings</h2>" in r.text
    assert '<nav class="nav"' in r.text


def test_the_js_check_response_is_json_not_a_page(tmp_path: Path):
    # The other half of the same branch: the fetch enhancement asks for
    # JSON so it can patch one row, and must not be handed a whole page to
    # parse -- `r.json()` on an HTML body lands in the .catch and paints
    # "could not reach svtplay-arr itself" over a check that worked.
    svt = FakeSvt(episodes=_episodes(5))
    r = _client(tmp_path, svt=svt).post(
        "/config/mappings/288649/check", headers={"Accept": "application/json"}
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "<h2>Mappings</h2>" not in r.text
    assert set(r.json()) == {
        "tvdb_id", "outcome", "css_class", "episode_count", "message",
    }


def test_check_json_and_html_paths_agree(tmp_path: Path):
    # The design constraint this whole feature turns on: exactly one
    # function computes the result, and both response shapes (the no-JS
    # full-page re-render and the JS fetch's JSON) are thin renderings of
    # it. If they ever disagreed, the operator staring at the page and the
    # JS silently patching a row could tell two different stories about the
    # same mapping.
    svt = FakeSvt(episodes=_episodes(7))
    client = _client(tmp_path, svt=svt)

    html = client.post("/config/mappings/288649/check").text
    payload = client.post(
        "/config/mappings/288649/check", headers={"Accept": "application/json"}
    ).json()

    assert payload["outcome"] == "found"
    assert payload["episode_count"] == 7
    # Jinja HTML-escapes the message for safe display (its apostrophes
    # become &#39; etc.) -- unescape before comparing, since that's a
    # rendering detail, not a difference in the underlying computation.
    assert payload["message"] in html_mod.unescape(html)


def test_check_json_and_html_paths_agree_is_not_vacuous(tmp_path: Path, monkeypatch):
    # Proves the previous test would actually catch the two paths
    # disagreeing, rather than passing no matter what: break the one shared
    # computation so it returns a different message depending on which
    # branch reads it, then confirm the agreement assertion fails.
    import svtplay_arr.api.config_ui as config_ui_module

    real_check_slug = config_ui_module._check_slug
    calls = {"n": 0}

    async def _flaky_check_slug(svt, slug):
        calls["n"] += 1
        result = await real_check_slug(svt, slug)
        if calls["n"] == 2:
            result = {**result, "message": result["message"] + " (mutated)"}
        return result

    monkeypatch.setattr(config_ui_module, "_check_slug", _flaky_check_slug)

    svt = FakeSvt(episodes=_episodes(7))
    client = _client(tmp_path, svt=svt)
    html = client.post("/config/mappings/288649/check").text
    payload = client.post(
        "/config/mappings/288649/check", headers={"Accept": "application/json"}
    ).json()

    assert payload["message"] not in html_mod.unescape(html)


def test_check_on_an_unknown_tvdb_id_is_handled_without_a_500(tmp_path: Path):
    svt = FakeSvt(episodes=_episodes(1))
    r = _client(tmp_path, svt=svt).post("/config/mappings/999999/check")

    assert r.status_code == 200
    assert "no mapping" in r.text.lower()
    assert svt.list_episodes_calls == []  # nothing to check -- never called SVT


def test_check_on_a_non_integer_tvdb_id_is_handled_without_a_500(tmp_path: Path):
    svt = FakeSvt(episodes=_episodes(1))
    r = _client(tmp_path, svt=svt).post("/config/mappings/not-a-number/check")

    assert r.status_code != 500
    assert svt.list_episodes_calls == []


def test_check_json_response_never_includes_the_api_key(tmp_path: Path):
    svt = FakeSvt(episodes=_episodes(1))
    r = _client(tmp_path, svt=svt).post(
        "/config/mappings/288649/check", headers={"Accept": "application/json"}
    )
    assert "SECRET-KEY-VALUE" not in r.text


def test_the_check_control_is_a_plain_form_reachable_without_javascript(tmp_path: Path):
    # Part 2's rule: everything works with JavaScript disabled, as a full
    # page round trip. The Check control must be an ordinary <form method=
    # post>, not a button with only a JS handler.
    body = _client(tmp_path).get("/config/mappings").text
    form = re.search(
        r'<form method="post" action="/config/mappings/288649/check"[^>]*>'
        r"\s*<button type=\"submit\">Check</button>",
        body,
    )
    assert form, f"no plain-HTML check form for the mapping in:\n{body}"


def test_the_mapping_filter_input_is_not_in_the_server_rendered_page(tmp_path: Path):
    # "Render the filter input only when JavaScript is available." The
    # server never emits it; initMappingFilter (base.html) creates and
    # inserts it at runtime. A filter box that does nothing with JS off is
    # worse than none, so it must be entirely absent from a plain fetch.
    body = _client(tmp_path).get("/config/mappings").text
    assert 'id="mapping-filter"' not in body
    # ...and the mechanism that would add it is actually present, so this
    # cannot pass simply because the feature doesn't exist at all.
    assert "initMappingFilter" in body
    assert 'input.id = "mapping-filter"' in body


_BASE_HTML = (
    Path(__file__).resolve().parent.parent
    / "src" / "svtplay_arr" / "templates" / "base.html"
)


def _inline_script() -> str:
    """base.html's one inline <script>, as source text.

    Read off the template rather than a rendered page: these are
    assertions about the script this project ships, and there is no JS
    runtime in the test environment (nor a node anywhere in the deploy
    path) to assert on its behaviour instead.
    """
    m = re.search(r"<script>(.*?)</script>", _BASE_HTML.read_text(encoding="utf-8"), re.S)
    assert m, "no inline <script> in base.html"
    return m.group(1)


def _inline_script_code() -> str:
    """`_inline_script` with its `//` comments stripped.

    The comments in there name the sinks they exist to warn about, so a
    naive substring search over the whole script would fail on its own
    documentation -- and, worse, could be made to pass by deleting the
    warning. The script contains no `//` inside a string literal (no URLs;
    every request goes to `form.action`), so this is a safe strip.
    """
    return re.sub(r"//.*", "", _inline_script())


def _one_statement(script: str, *patterns: str) -> str:
    """The single `;`-terminated statement in `script` matching them all.

    Whitespace is collapsed and the patterns are regexes, so that an
    assertion about what a statement *does* is not also an assertion about
    how it was wrapped or which equivalent spelling it used -- a reformat,
    a renamed local, or `setAttribute("aria-label", x)` rewritten as
    `.ariaLabel = x` must not fail a test about behaviour. A test that
    breaks on a semantically identical rewrite teaches the next person to
    edit the test rather than read it.

    Several patterns because one name is rarely unique in here: aria-label
    matches the filter input's too, while ("button", "aria-?[Ll]abel") is
    the one statement that labels the button.

    Requiring exactly one match is the substance, not a convenience: it is
    what makes the returned string unambiguously *the* place that does the
    thing. A search over the whole script would be satisfied by a second,
    contradicting copy somewhere else; this fails on it.
    """
    hits = [
        re.sub(r"\s+", " ", statement).strip()
        for statement in script.split(";")
        if all(re.search(pattern, statement) for pattern in patterns)
    ]
    assert len(hits) == 1, (
        f"expected exactly one statement matching all of {patterns!r}, "
        f"found {len(hits)}: {hits}"
    )
    return hits[0]


def test_every_initialiser_in_the_script_is_actually_run(tmp_path: Path):
    # initSecretToggles was defined and never called for a while, and
    # nothing failed: the tests naming it only asked whether its name
    # appeared in the page, which its own definition satisfies. Every
    # init* function has that hole, and this branch's most persistent
    # defect is a fix applied to the site where it was noticed and not to
    # its siblings -- so this enumerates the functions the script defines
    # rather than pinning the one that was broken. A fourth initialiser
    # added later is covered by this test already existing, not by
    # somebody remembering to add a line to it.
    script = _inline_script_code()

    defined = set(re.findall(r"function\s+(init\w*)\s*\(\s*\)", script))
    defined.discard("init")
    # A floor, so a regex that quietly stops matching cannot turn this
    # into a test that enumerates nothing and passes.
    assert {"initCheckForms", "initMappingFilter", "initSecretToggles"} <= defined, (
        f"the script's initialisers are no longer being found: {sorted(defined)}"
    )

    body = re.search(r"function\s+init\s*\(\s*\)\s*\{([^}]*)\}", script)
    assert body, "no init() in the inline script"
    called = set(re.findall(r"\b(init\w*)\s*\(\s*\)", body.group(1)))

    assert not defined - called, (
        "defined but never run from init(), so this behaviour is absent from "
        f"the live page: {sorted(defined - called)}"
    )


def test_the_inline_script_never_writes_markup_into_the_page(tmp_path: Path):
    # Mutating `textContent` to `innerHTML` left all 399 tests green. No
    # sink exists today only because this assignment is textContent: the
    # string it writes is `data.message`, which embeds `svt_slug` --
    # accepted unvalidated from the manual-entry form and stored verbatim
    # in mappings.yaml. The sink goes live the moment someone makes that
    # edit, on a page published to the public internet.
    script = _inline_script_code()

    assert "innerHTML" not in script
    assert "outerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "document.write" not in script
    # ...and the one place a server-supplied string reaches the DOM does
    # so as text, so this cannot pass by the assignment having vanished.
    assert "result.textContent = data.message;" in script


def test_the_check_fetch_refuses_a_response_it_cannot_trust(tmp_path: Path):
    # The handler had no `r.ok` check and no guard on `data.message`. A
    # proxy error body, or an SSO JSON 401 from the gateway in front of
    # this page, parses fine and paints the row with the literal string
    # "undefined" -- a control whose only job is trustworthy signal
    # reporting a confident nothing. Both are now thrown into the existing
    # .catch, which says the check could not be made.
    script = _inline_script_code()

    assert re.search(r"if\s*\(\s*!\s*r\.ok\s*\)", script), "no r.ok check"
    assert re.search(
        r"typeof\s+data\.message\s*!==\s*[\"']string[\"']", script
    ), "data.message is painted without being checked"
    assert re.search(
        r"typeof\s+data\.css_class\s*!==\s*[\"']string[\"']", script
    ), "data.css_class is used as a class name without being checked"
    # Both guards hand over to the .catch rather than inventing their own
    # message, so there is still one wording for "the check did not
    # happen".
    assert script.count("throw new Error") >= 2
    assert "could not reach svtplay-arr itself" in script


def test_each_mapping_row_carries_filter_text_for_the_js_filter(tmp_path: Path):
    # The client-side filter (initMappingFilter) matches against this
    # attribute rather than re-deriving column text itself.
    body = _client(tmp_path).get("/config/mappings").text
    assert 'data-filter-text="gift vid första ögonkastet 288649 jpmqd3q gift-vid-forsta-ogonkastet"' in body


def _css_block_at(css: str, start: int) -> str:
    """The `{ ... }` block beginning at or after `start`, brace-matched.

    A regex cannot do this: the narrow-viewport media query contains
    nested rules, so a naive brace pattern stops at the first inner
    closing brace and every assertion built on it is silently partial.
    """
    open_at = css.index("{", start)
    depth = 0
    for i in range(open_at, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[open_at + 1:i]
    raise AssertionError("unbalanced braces in base.html's CSS")


def _stylesheet() -> str:
    """base.html's CSS with its comments removed.

    The comments in there quote selectors and declarations -- that is what
    makes them useful -- so leaving them in means every structural
    assertion below can be satisfied, or broken, by prose. Worse, a comment
    quoting a brace derails the brace matching itself.
    """
    return re.sub(r"/\*.*?\*/", " ", _BASE_HTML.read_text(encoding="utf-8"), flags=re.S)


def _narrow_media_query() -> str:
    """The body of base.html's narrow-viewport `@media` block."""
    css = _stylesheet()
    m = re.search(r"@media\s*\(\s*max-width\s*:[^)]*\)", css)
    assert m, "base.html has no max-width media query; the page never stacks"
    return _css_block_at(css, m.end())


def _selectors(block: str) -> list[str]:
    """Every selector in a flat CSS block, one per comma-separated part."""
    out = []
    for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        for sel in rule.group(1).split(","):
            sel = re.sub(r"/\*.*?\*/", " ", sel, flags=re.S).strip()
            if sel:
                out.append(sel)
    return out


def _emits(body: str, token: str) -> bool:
    """Does the rendered page contain something this selector token matches?"""
    if token.startswith("."):
        return any(
            token[1:] in m.group(1).split()
            for m in re.finditer(r'class="([^"]*)"', body)
        )
    if token.startswith("["):
        name = re.match(r"\[([A-Za-z0-9_-]+)", token).group(1)
        return bool(re.search(r"\s" + re.escape(name) + r"[=\s>]", body))
    return bool(re.search(r"<" + re.escape(token) + r"[\s>]", body))


def _selector_tokens(selector: str) -> list[str]:
    """Split a selector into the type/class/attribute pieces it matches on.

    Pseudo-elements and pseudo-classes are dropped -- they qualify a match
    rather than being something the template emits.
    """
    tokens = []
    for compound in re.split(r"[\s>+~]+", selector):
        compound = re.sub(r"::?[a-z-]+(\([^)]*\))?", "", compound)
        for part in re.findall(r"\[[^\]]+\]|[.#][A-Za-z0-9_-]+|^[a-z]+", compound):
            tokens.append(part)
    return tokens


def test_the_narrow_layout_targets_elements_the_page_actually_renders(
    tmp_path: Path,
):
    # A media query is easy to write and easy to leave pointing at markup
    # that no longer exists (or never did) -- it fails invisibly, on a
    # phone, in production. Every class, attribute and element the narrow
    # rules select must be something one of the views genuinely emits.
    #
    # Every view, not just the landing one: the stylesheet is shared, so a
    # rule targeting the settings form is as easy to leave dangling as one
    # targeting the mappings table, and pinning it to a single page would
    # quietly stop covering whichever half moved.
    client = _client(tmp_path)
    body = "".join(client.get(path).text for _key, _label, path in VIEWS)
    selectors = _selectors(_narrow_media_query())
    assert selectors, "the narrow-viewport media query is empty"

    for selector in selectors:
        tokens = _selector_tokens(selector)
        assert tokens, f"could not read any target out of {selector!r}"
        for token in tokens:
            assert _emits(body, token), (
                f"the narrow layout styles {token!r} (in {selector!r}), "
                f"which /config never renders"
            )


def test_the_mapping_table_stops_being_a_table_on_a_narrow_viewport(
    tmp_path: Path,
):
    # The defect: `Series | TVDB | SVT` plus two buttons in a fixed table
    # layout on a phone. The series title column collapses to nothing and
    # the several-sentence Check result renders as a vertical ribbon two
    # words wide. Below the breakpoint the rows become stacked blocks.
    block = _narrow_media_query()
    stacking = [
        sel for sel in _selectors(block)
        if "mappings" in sel and re.search(r"\btr\b|\btd\b", sel)
    ]
    assert stacking, "the narrow rules never restyle the mapping rows"

    css = _stylesheet()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        if any("mappings" in s and re.search(r"\btr\b", s)
               for s in m.group(1).split(",")):
            assert "display: block" in m.group(2), (
                f"mapping rows are not stacked: {m.group(0)}"
            )
            break
    else:
        raise AssertionError("no rule stacks the mapping rows")

    # ...and the table survives above the breakpoint, where it is fine.
    assert re.search(r"(?<!\.)\btable\s*\{[^}]*border-collapse", css)


def test_the_stacked_layout_still_lets_the_filter_hide_a_row(tmp_path: Path):
    # `tr { display: block }` is an author rule and outranks the UA
    # sheet's `[hidden] { display: none }`, so switching the layout
    # silently breaks initMappingFilter on exactly the viewport the switch
    # was written for -- typing in the filter box would hide nothing.
    block = _narrow_media_query()
    hiding = [
        (sel, decls)
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", block)
        for sel in m.group(1).split(",")
        for decls in [m.group(2)]
        if "[hidden]" in sel
    ]
    assert hiding, (
        "nothing in the narrow layout re-hides [hidden] elements; the live "
        "filter cannot hide a stacked row"
    )
    for sel, decls in hiding:
        assert "display: none" in decls, f"{sel.strip()} does not hide anything"
        # ...and it has to outrank the display:block rule, which is
        # `table.mappings tr`-shaped: one class plus element names.
        assert "." in sel, (
            f"{sel.strip()} is not specific enough to beat the stacking rule"
        )


def test_the_check_result_gets_the_full_width_of_the_table(tmp_path: Path):
    # The Check message is several sentences by design (it has to say that
    # a resolving slug does not mean a correct mapping). Squeezed into the
    # actions column it was unreadable on a phone and cramped on a desktop.
    body = _client(tmp_path).get("/config/mappings").text
    columns = len(re.findall(r"<th[\s>]", body))
    assert columns >= 3, f"no mapping table header in:\n{body}"

    result_cell = re.search(
        r"<td([^>]*)>\s*<p class=\"check-result", body, re.S
    )
    assert result_cell, (
        f"the check result is not in a cell of its own:\n{body}"
    )
    assert f'colspan="{columns}"' in result_cell.group(1), (
        f"the check result does not span the table: {result_cell.group(0)!r}"
    )


def test_the_check_result_is_filtered_away_with_its_own_mapping(tmp_path: Path):
    # It lives in a row of its own now, and the live filter hides rows by
    # `data-filter-text`. Without the same text on the result's row, a
    # filter that hides a mapping would leave its Check result stranded on
    # the page under someone else's row.
    body = _client(tmp_path).get("/config/mappings").text
    rows = re.findall(r"<tr\b[^>]*>.*?</tr>", body, re.S)
    result_rows = [r for r in rows if 'class="check-result' in r]
    assert result_rows, f"no check-result row in:\n{body}"

    for row in result_rows:
        m = re.search(r'data-filter-text="([^"]*)"', row)
        assert m, f"a check result is not filterable:\n{row}"
        tvdb = re.search(r'data-tvdb-id="(\d+)"', row)
        assert tvdb, f"a check result row names no mapping:\n{row}"
        assert tvdb.group(1) in m.group(1), (
            "the check result's filter text does not match its own mapping"
        )


def test_the_mapping_cells_label_themselves_for_the_stacked_layout(
    tmp_path: Path,
):
    # With the header row gone below the breakpoint, "288649" on a line of
    # its own means nothing; each cell carries its own label instead.
    body = _client(tmp_path).get("/config/mappings").text
    labels = re.findall(r'<td data-label="([^"]+)"', body)
    assert labels, f"no self-labelling cells in:\n{body}"
    headers = [
        re.sub(r"<[^>]+>", "", h).strip()
        for h in re.findall(r"<th[^>]*>.*?</th>", body, re.S)
    ]
    for label in labels:
        assert label in headers, (
            f"cell label {label!r} matches no column heading {headers}"
        )


# ---- Page structure ---------------------------------------------------
# The 2026-08-26 restyle: the page is read beside Sonarr, so it is built
# like Sonarr's settings -- an app bar, content in panels, two-column form
# rows, and the field explanation small and muted beneath its control
# rather than sitting inside the <label> at label size. These pin the
# structural half of that; the colour half is in test_config_ui_theme.py.


def _wide_stylesheet() -> str:
    """base.html's CSS with the narrow-viewport media query cut out.

    Every rule left is one that applies above the breakpoint, so a
    lookup by selector cannot accidentally return the stacked override
    of the same selector.
    """
    css = _stylesheet()
    m = re.search(r"@media\s*\(\s*max-width\s*:[^)]*\)", css)
    assert m, "base.html has no max-width media query; the page never stacks"
    body = _css_block_at(css, m.end())
    start = css.index("{", m.end())
    return css[:m.start()] + css[start + 1 + len(body) + 1:]


def _rules(css: str):
    """(selector, declarations) for every flat rule in `css`."""
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        for sel in m.group(1).split(","):
            sel = sel.strip()
            if sel:
                yield sel, m.group(2)


def _declarations(css: str, selector: str) -> str:
    """The declarations of the one rule written for exactly `selector`."""
    found = [d for sel, d in _rules(css) if sel == selector]
    assert found, f"base.html has no rule for {selector!r}"
    return " ".join(found)


def _font_size_rem(decls: str, what: str) -> float:
    m = re.search(r"font-size:\s*([0-9.]+)rem", decls)
    assert m, f"{what} declares no font-size in rem: {decls!r}"
    return float(m.group(1))


def _help_paragraph(body: str, key: str) -> str:
    m = re.search(r'<p class="help" id="' + re.escape(key) + r'-help">.*?</p>',
                  body, re.S)
    assert m, f"{key} renders no help paragraph in:\n{body}"
    return m.group(0)


def test_no_field_hides_its_explanation_inside_its_label(tmp_path: Path):
    # The single biggest thing wrong with the page that came back from
    # review as unreadable, and the reason the restyle happened at all:
    # every field's explanation lived inside its <label>, at near-body
    # size. These strings are deliberately long -- they were reviewed and
    # are correct -- so at label weight each setting read as a paragraph
    # with an input attached and nothing guided the eye. The wording is
    # untouched; it just may not be part of the label any more.
    body = _client(tmp_path).get("/config/settings").text
    for f in SETTING_FIELDS:
        label = re.search(
            r'<label for="' + re.escape(f.key) + r'".*?</label>', body, re.S
        )
        assert label, f"{f.key} has no label in:\n{body}"
        text = html_mod.unescape(label.group(0))
        assert f.help not in text, (
            f"{f.key}'s explanation is back inside its label"
        )
        assert f.label in text, f"{f.key}'s label lost its name"


def test_every_field_still_renders_its_help_text_verbatim(tmp_path: Path):
    # ...and the counterpart, so the test above can never be satisfied by
    # the explanation having been shortened or dropped instead of moved.
    body = html_mod.unescape(_client(tmp_path).get("/config/settings").text)
    for f in SETTING_FIELDS:
        assert f.help in body, f"{f.key}'s help text is missing"


def test_every_field_renders_its_help_beneath_its_control(tmp_path: Path):
    body = _client(tmp_path).get("/config/settings").text
    for f in SETTING_FIELDS:
        control = _input_tag(body, f.key)
        help_p = _help_paragraph(body, f.key)
        assert body.index(control) < body.index(help_p), (
            f"{f.key}'s help text is rendered above its control"
        )


def test_every_field_control_points_at_its_own_help_text(tmp_path: Path):
    # Moving the explanation out of the <label> would otherwise cost a
    # screen-reader user the explanation entirely: it used to be announced
    # because it was part of the label. aria-describedby is what keeps it
    # announced with the field now, so it is not optional decoration.
    body = _client(tmp_path).get("/config/settings").text
    for f in SETTING_FIELDS:
        _help_paragraph(body, f.key)  # the id it points at exists
        assert f'aria-describedby="{f.key}-help"' in _input_tag(body, f.key), (
            f"{f.key}'s control does not point at its help text"
        )


def test_the_help_text_is_set_quieter_than_the_field_label(tmp_path: Path):
    # "Small and muted, beneath the control, clearly subordinate to the
    # label." A restyle that keeps the markup but sets .help at label size
    # and label weight puts the page straight back where it started, with
    # every other test still green.
    css = _wide_stylesheet()
    help_decls = _declarations(css, ".help")
    label_decls = _declarations(css, ".field > label")

    assert _font_size_rem(help_decls, ".help") < _font_size_rem(
        label_decls, ".field > label"
    ), "the help text is not set smaller than the label"

    help_weight = re.search(r"font-weight:\s*(\d+)", help_decls)
    label_weight = re.search(r"font-weight:\s*(\d+)", label_decls)
    assert help_weight and label_weight
    assert int(help_weight.group(1)) < int(label_weight.group(1)), (
        "the help text is set at the label's weight"
    )
    assert "var(--muted-fg)" in help_decls, (
        "the help text does not use the muted ink"
    )


def _grid_columns(decls: str) -> list[str]:
    """The column list of a `grid-template-columns`, respecting parens."""
    m = re.search(r"grid-template-columns:\s*([^;]+)", decls)
    assert m, f"no grid-template-columns in {decls!r}"
    out, depth, current = [], 0, ""
    for ch in m.group(1).strip():
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch.isspace() and depth == 0:
            if current:
                out.append(current)
            current = ""
        else:
            current += ch
    if current:
        out.append(current)
    return out


def test_a_form_row_is_two_columns_wide_and_one_column_narrow(tmp_path: Path):
    # Label left, control right above the breakpoint; stacked below it,
    # the same way the mappings table already switches. Asserted as a
    # column count rather than a pixel value so the widths stay free.
    wide = _declarations(_wide_stylesheet(), ".field")
    assert "display: grid" in wide, "form rows are not a grid"
    assert len(_grid_columns(wide)) == 2, (
        f"a form row is not two columns: {_grid_columns(wide)}"
    )

    narrow = [
        decls for sel, decls in _rules(_narrow_media_query())
        if sel == ".field" and "grid-template-columns" in decls
    ]
    assert narrow, "the narrow layout never restacks the form rows"
    assert len(_grid_columns(narrow[0])) == 1, (
        f"form rows stay in columns on a phone: {_grid_columns(narrow[0])}"
    )


def test_a_dangerous_field_is_marked_by_an_edge_not_a_filled_block(
    tmp_path: Path,
):
    # These were saturated amber blocks -- label, multi-sentence help and
    # input all inside one -- and three in a row made the ordinary
    # settings around them look like errors. The signal stays (a thin
    # amber edge, plus the "Careful" badge the label already carries);
    # the shouting goes. A rule that styles only the dangerous fields may
    # therefore not paint a background, and may not recolour their text --
    # anything they share with an ordinary field is written as one rule
    # covering both.
    css = _stylesheet()
    edges = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        parts = [s.strip() for s in m.group(1).split(",")]
        danger = [s for s in parts if ".field-danger" in s]
        if not danger:
            continue
        if any(".field-danger" not in s and ".field" in s for s in parts):
            continue  # shared with ordinary fields: the same treatment
        decls = m.group(2)
        assert not re.search(r"(?:^|;)\s*background", decls), (
            f"the dangerous fields are still a filled block: {m.group(0)!r}"
        )
        assert not re.search(r"(?:^|;)\s*color\s*:", decls), (
            f"a dangerous field recolours its own text: {m.group(0)!r}"
        )
        edges.append(decls)
    assert any(
        re.search(r"border-left:[^;]*var\(--warn-border\)", d) for d in edges
    ), "nothing marks a dangerous field at all"

    # ...and the badge is still there, so this cannot pass by the warning
    # having been removed rather than quietened.
    body = _client(tmp_path).get("/config/settings").text
    for key in _DANGEROUS_KEYS:
        _danger_badge(_danger_block(body, key), key)


def test_the_page_carries_an_app_bar_above_its_content(tmp_path: Path):
    # Page furniture visibly separate from page content, the way Sonarr's
    # own header is -- rather than an <h1> sitting on the same bare
    # background as the form.
    body = _client(tmp_path).get("/config").text
    bar = re.search(r"<header[^>]*>(.*?)</header>", body, re.S)
    assert bar, f"no app bar in:\n{body}"
    assert "svtplay-arr" in bar.group(1), "the app bar does not name the app"
    assert body.index("</header>") < body.index("<main"), (
        "the app bar is not above the content"
    )

    css = _wide_stylesheet()
    topbar = " ".join(
        decls for sel, decls in _rules(css) if sel.startswith(".topbar")
    )
    assert "var(--topbar-bg)" in topbar, (
        "the app bar shares the page background instead of its own"
    )


def test_each_settings_section_renders_as_a_panel_with_a_quiet_header(
    tmp_path: Path,
):
    # A card with a header, not a bare fieldset outline -- and still a
    # real <fieldset>/<legend>, so the grouping remains something a screen
    # reader announces rather than a div that merely looks grouped.
    body = _client(tmp_path).get("/config/settings").text
    for name in _SECTION_FIELDS:
        assert re.search(
            r'<fieldset class="settings-section">\s*<legend>'
            + re.escape(name) + r"</legend>",
            body,
        ), f"the {name} section is not a fieldset with a legend"

    css = _wide_stylesheet()
    panel = " ".join(
        decls for sel, decls in _rules(css)
        if sel in (".panel", "fieldset.settings-section")
    )
    assert "var(--panel-bg)" in panel, "settings sections are not panels"

    header = " ".join(
        decls for sel, decls in _rules(css)
        if sel == "fieldset.settings-section > legend"
    )
    assert "var(--panel-head-bg)" in header, (
        "the panel header is not distinguished from the panel"
    )
    assert _font_size_rem(header, "the panel header") < 1.0, (
        "the panel header is not the quiet one the layout depends on"
    )


def test_the_mappings_table_reads_as_a_list_not_a_grid(tmp_path: Path):
    # Quiet header row, a hover state on a row. The narrow-viewport rules
    # already have their own tests; this is the desktop read.
    css = _wide_stylesheet()
    header = _declarations(css, "table.mappings th")
    assert "var(--muted-fg)" in header, "the column headings are not quiet"
    assert _font_size_rem(header, "the column headings") < 1.0

    hover = [
        sel for sel, _ in _rules(css)
        if "mapping-row" in sel and ":hover" in sel
    ]
    assert hover, "a mapping row has no hover state"

def _sample_health(**overrides) -> dict:
    """A plausible /health-shaped dict for status_provider tests.

    Deliberately built independently of app.compute_health -- these tests
    are about how the config page *renders* whatever dict a provider hands
    it, not about health computation itself (that lives in test_app.py,
    which exercises the real compute_health via create_app).
    """
    base = {
        "status": "ok",
        "same_filesystem": True,
        "worker_alive": True,
        "active_jobs": 2,
        "mappings": 3,
        "mappings_ever_loaded": True,
        "mappings_degraded": False,
        "svt": _sample_svt(),
    }
    base.update(overrides)
    return base


def _sample_svt(**overrides) -> dict:
    """The canary half of that dict (see app.compute_health / canary.py)."""
    base = {
        "state": "ok",
        "degraded": False,
        "needs_attention": False,
        "alive": True,
        "checked": 3,
        "failing": 0,
        "episodes_seen": 41,
        "last_checked": "2026-08-27T09:00:00+00:00",
        "last_success": "2026-08-27T09:00:00+00:00",
        "last_checked_age_s": 720.0,
        "last_success_age_s": 720.0,
        "last_error": None,
        "last_error_at": None,
        "failing_series": [],
        "failing_series_truncated": False,
    }
    base.update(overrides)
    return base


def _strip_of(body: str) -> str:
    start = body.index('class="status-strip"')
    return " ".join(body[start: body.index("</div>", start)].split())


def _canary_page(tmp_path: Path, **svt) -> str:
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(svt=_sample_svt(**svt)),
        )
    )
    return TestClient(app).get("/config").text


# --- The SVT canary on the strip -------------------------------------------
#
# The strip's headline question is "is SVT working, and when did we last
# confirm it". These are about rendering only: the states themselves come
# from canary.py and the wiring from test_app.py.


def test_a_working_canary_reads_as_ok_with_when_it_was_confirmed(tmp_path: Path):
    strip = _strip_of(_canary_page(tmp_path))
    assert "SVT: ok" in strip
    assert "3 mappings" in strip
    assert "41 episodes" in strip
    assert "confirmed 12 min ago" in strip
    assert "status-chip error" not in strip


def test_a_never_checked_canary_does_not_read_as_ok(tmp_path: Path):
    # The defect this whole feature removes, rebuilt one level up: a strip
    # that rendered an unchecked canary as "ok" would put the reassuring
    # word in front of the operator for a service that has confirmed
    # nothing at all.
    body = _canary_page(
        tmp_path, state="unknown", checked=0, failing=0, episodes_seen=0,
        last_checked=None, last_success=None,
        last_checked_age_s=None, last_success_age_s=None,
    )
    strip = _strip_of(body)
    assert "SVT: not checked yet since restart" in strip
    assert "SVT: ok" not in strip
    assert "status-chip warn" in strip


def test_every_mapping_failing_reads_as_urgent_and_names_the_cause(
    tmp_path: Path,
):
    body = _canary_page(
        tmp_path, state="svt", degraded=True, needs_attention=True,
        checked=3, failing=3,
        episodes_seen=0, last_error="SVT answered but no episodes could be parsed",
        failing_series=[
            {"tvdb_id": 1, "series_title": "A", "svt_slug": "a", "error": "x"},
        ],
    )
    strip = _strip_of(body)
    assert "SVT: FAILING" in strip
    assert "none of 3 mappings" in strip
    assert "status-chip error" in strip
    # ...and the actionable half, which is what separates "something is
    # wrong" from "here is what it is and what it means".
    assert "None of your 3 mappings" in body
    assert "not at any one show" in body
    assert "nothing will be grabbed" in body


def test_one_mapping_failing_reads_as_that_show_not_as_an_outage(tmp_path: Path):
    # `degraded` is False here on purpose -- that is what /health hands the
    # page for this state (see canary.DEGRADED_STATES). The row must still be
    # fully visible: keeping one dead mapping off the machine-readable
    # verdict is about not training the operator to ignore a red light, and
    # it would be self-defeating if it also made the row invisible on the
    # page they actually read.
    body = _canary_page(
        tmp_path, state="series", degraded=False, needs_attention=True,
        checked=3, failing=1,
        failing_series=[
            {"tvdb_id": 7, "series_title": "Ended Show", "svt_slug": "ended-show",
             "error": "404"},
        ],
    )
    strip = _strip_of(body)
    assert "SVT: 1 of 3 mappings" in strip
    # Amber, not red: real and theirs to fix, but nothing else has stopped.
    assert "status-chip warn" in strip
    assert "status-chip error" not in strip
    assert "SVT: FAILING" not in strip
    assert "Ended Show (ended-show)" in body
    assert "re-slugged" in body
    assert '<p class="warn">' in body
    # The urgent shape's claim must not appear here: it would send the
    # operator looking for an outage that is not happening.
    assert "None of your" not in body


def test_a_long_failure_list_is_truncated_with_a_count(tmp_path: Path):
    body = _canary_page(
        tmp_path, state="series", degraded=False, needs_attention=True,
        checked=20, failing=9,
        failing_series=[
            {"tvdb_id": i, "series_title": f"S{i}", "svt_slug": f"s{i}",
             "error": "404"}
            for i in range(5)
        ],
        failing_series_truncated=True,
    )
    assert "and 4 more" in " ".join(body.split())


def test_a_dead_canary_task_reads_as_nothing_is_checking(tmp_path: Path):
    body = _canary_page(
        tmp_path, state="unknown", degraded=True, needs_attention=True,
        alive=False, checked=0, failing=0, last_checked=None,
        last_success=None, last_checked_age_s=None, last_success_age_s=None,
    )
    strip = _strip_of(body)
    assert "SVT: NOT BEING CHECKED" in strip
    assert "status-chip error" in strip
    assert "Nothing is checking SVT any more" in body


def test_a_stalled_canary_says_so(tmp_path: Path):
    body = _canary_page(
        tmp_path, state="stale", degraded=True, needs_attention=True,
        checked=3, failing=0,
        last_checked_age_s=14400.0, last_success_age_s=14400.0,
    )
    strip = _strip_of(body)
    assert "SVT: CHECK STALLED" in strip
    # Four hours, not "240 min". The age formatter is shared with the
    # mappings table now (see templates/_age.html), and it scales -- a
    # per-mapping row that last resolved three days ago would otherwise
    # have read "4320 min". The strip gets the same wording so the two
    # surfaces, one click apart, cannot describe the same moment
    # differently.
    assert "4 hours" in strip


def test_no_mappings_is_neither_a_success_nor_a_failure(tmp_path: Path):
    # A fresh install legitimately has nothing to check. Rendering that as
    # "ok" would claim SVT was confirmed working when nothing asked it.
    body = _canary_page(
        tmp_path, state="no_mappings", checked=0, failing=0, episodes_seen=0,
        last_success=None, last_success_age_s=None,
    )
    strip = _strip_of(body)
    assert "SVT: no mappings to check" in strip
    assert "SVT: ok" not in strip
    assert "status-chip error" not in strip


def test_a_status_dict_without_a_canary_still_renders_the_rest(tmp_path: Path):
    # Every other test in this file builds a router without a canary at all;
    # the strip must degrade to omitting the chip rather than failing the
    # page, which is what any dict predating this field would produce.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    health = _sample_health()
    del health["svt"]
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(), status_provider=lambda: health,
        )
    )
    body = TestClient(app).get("/config").text
    strip = _strip_of(body)
    assert "Worker: alive" in strip
    assert "SVT:" not in strip


def test_no_status_provider_renders_no_status_strip(tmp_path: Path):
    # "No provider at all still renders the page": build_config_router is
    # called without status_provider throughout this file (as most
    # deployments' tests do), and the page must still work -- just without
    # a strip, never a broken one.
    body = _client(tmp_path).get("/config/mappings").text
    assert 'class="status-strip"' not in body
    assert "Status unavailable" not in body
    assert TITLE in body  # the rest of the page is unaffected


def test_status_provider_renders_the_strip(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(),
        )
    )
    body = TestClient(app).get("/config").text
    assert 'class="status-strip"' in body
    assert "Service: ok" in body
    assert "Worker: alive" in body
    assert "Mappings: 3" in body
    assert "Same filesystem: yes" in body
    assert "Active jobs: 2" in body


def test_a_degraded_mapping_table_is_prominent_on_the_page(tmp_path: Path):
    # This is the state the whole feature exists for: a broken
    # mappings.yaml, today discoverable only via `curl localhost:9800/health`.
    # It must not read as a row of green text with one different word --
    # reuse the existing .error style, same as any other failure banner.
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(
                mappings_degraded=True, mappings=1, status="degraded",
            ),
        )
    )
    body = TestClient(app).get("/config").text
    assert "DEGRADED" in body
    assert 'class="status-chip error"' in body


def test_a_split_filesystem_is_flagged_on_the_page(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(
                same_filesystem=False, status="degraded",
            ),
        )
    )
    body = TestClient(app).get("/config").text
    assert "Same filesystem: no" in body
    assert 'class="status-chip warn"' in body


def test_a_dead_worker_is_flagged_on_the_page(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, FakeSvt(), FakeSonarr(),
            status_provider=lambda: _sample_health(
                worker_alive=False, status="degraded",
            ),
        )
    )
    body = TestClient(app).get("/config").text
    assert "Worker: dead" in body
    assert 'class="status-chip error"' in body


def _degraded_search_client(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    svt = FakeSvt([SvtSearchHit("jpmQD3q", TITLE, "TvSeries")])
    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    app = FastAPI()
    app.include_router(
        build_config_router(
            cfg, maps, svt, sonarr,
            status_provider=lambda: _sample_health(
                worker_alive=False, mappings_degraded=True, mappings=1,
                status="degraded",
            ),
        )
    )
    return TestClient(app), maps


def test_the_search_results_page_carries_the_status_strip(tmp_path: Path):
    # The strip used to render only on /config. The search-results page is
    # where an operator ends up while troubleshooting, so a dead worker or
    # a degraded mappings table has to be visible from here too.
    client, _maps = _degraded_search_client(tmp_path)

    body = client.post("/config/mappings/search", data={"q": "gift"}).text

    assert _is_search_results_page(body)
    assert 'class="status-strip"' in body
    assert "Worker: dead" in body
    assert "DEGRADED" in body


def test_the_failed_create_page_carries_the_status_strip(tmp_path: Path):
    # ...and above all here: this is the render that follows a create the
    # operator just watched fail (see the radio-selection tests above).
    client, maps = _degraded_search_client(tmp_path)

    r = client.post("/config/mappings", data={
        "q": "gift",
        "expected_mtime": str(maps.stat().st_mtime),
        "svt": "jpmQD3q|gift-vid-forsta-ogonkastet",
        "sonarr": "288649",  # already mapped -> the create fails
    })

    assert _is_search_results_page(r.text)
    assert "already mapped" in _error_text(r.text).lower()
    assert 'class="status-strip"' in r.text
    assert "Worker: dead" in r.text


def test_the_search_page_strip_never_shows_the_api_key(tmp_path: Path):
    # The strip is /health's dict, which has never carried the key -- pin
    # it on this page too, now that the page renders one.
    client, _maps = _degraded_search_client(tmp_path)
    body = client.post("/config/mappings/search", data={"q": "gift"}).text
    assert "SECRET-KEY-VALUE" not in body


def test_a_provider_that_raises_leaves_the_search_page_rendering(tmp_path: Path):
    # Same forgiveness rule as /config: a broken provider costs the strip,
    # never the page.
    def _boom():
        raise RuntimeError("status computation is on fire")

    cfg, maps = _paths(tmp_path)
    svt = FakeSvt([SvtSearchHit("jpmQD3q", TITLE, "TvSeries")])
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, svt, FakeSonarr(), status_provider=_boom)
    )

    r = TestClient(app).post("/config/mappings/search", data={"q": "gift"})

    assert r.status_code == 200
    assert _is_search_results_page(r.text)
    assert 'class="status-strip"' not in r.text
    assert "Status unavailable" in r.text


def test_a_provider_that_raises_leaves_the_page_rendering(tmp_path: Path):
    # The page must be at least as forgiving as /health: a broken
    # status_provider must never turn into a 500, only a missing (or
    # explicitly unavailable) strip. There is an equivalent scenario for
    # /health itself (store.all_active() raising) that /health survives
    # internally; this is the config-page-specific case where the provider
    # callable itself blows up outright.
    def _boom():
        raise RuntimeError("status computation is on fire")

    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(
        build_config_router(cfg, maps, FakeSvt(), FakeSonarr(), status_provider=_boom)
    )
    r = TestClient(app).get("/config/mappings")

    assert r.status_code == 200
    assert 'class="status-strip"' not in r.text
    assert "Status unavailable" in r.text
    # ...and the rest of the page still works.
    assert TITLE in r.text


# --- Find mappings: the automatic sweep ------------------------------
#
# The confidence gate itself is tested in tests/test_discovery.py. These
# tests are about the page around it: that it writes only what the gate
# approved, in one write, never from the form, and that every failure is
# a rendered page rather than a 500 or a half-written file.


class SweepSvt(FakeSvt):
    """FakeSvt with per-query search results, and a call log."""

    def __init__(self, results=None, error=None, **kwargs):
        super().__init__(**kwargs)
        self.results = results or {}
        self.error = error
        self.queries: list[str] = []

    async def search_series(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.results.get(query, [])


# The gate decides on episodes now, not on titles, so a sweep test that
# expects a row to be written has to supply the evidence for it: a weekly
# run that both sides agree about, episode for episode.
SWEEP_FIRST = date(2026, 8, 3)


def _svt_run(count=8, start=SWEEP_FIRST):
    return [
        SvtEpisode(
            svt_id=f"v{i + 1}", title=f"Avsnitt {i + 1}",
            url=f"/video/v{i + 1}/show/{i + 1}", ordinal=i + 1,
            published=start + timedelta(days=7 * i), available=True,
            duration_s=None,
        )
        for i in range(count)
    ]


def _sonarr_run(series_id, count=8, start=SWEEP_FIRST):
    return [
        SonarrEpisode(
            series_id=series_id, season=1, episode=i + 1,
            air_date=start + timedelta(days=7 * i), title="",
        )
        for i in range(count)
    ]


class BrokenSonarr:
    def __init__(self):
        self.calls = 0

    async def all_series(self):
        self.calls += 1
        raise RuntimeError("sonarr is unreachable")


def _sweep_client(tmp_path: Path, svt, sonarr):
    cfg, maps = _paths(tmp_path)
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, svt, sonarr))
    return TestClient(app), maps


def _discover(client, maps, **extra):
    data = {"expected_mtime": str(maps.stat().st_mtime)}
    data.update(extra)
    return client.post("/config/mappings/discover", data=data)


def test_the_index_offers_find_mappings_as_a_plain_form(tmp_path: Path):
    # No JS: the sweep must work with JavaScript off, so the control is a
    # form POST, not a fetch.
    body = _client(tmp_path).get("/config/mappings").text
    assert 'action="/config/mappings/discover"' in body
    assert "Find mappings" in body


def test_a_corroborated_match_is_written(tmp_path: Path):
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([
        {"id": 7, "tvdbId": 288649, "title": TITLE},      # already mapped
        {"id": 9, "tvdbId": 999, "title": OTHER},
    ], episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert r.status_code == 200
    created = MappingTable.load(maps).for_tvdb(999)
    assert created is not None
    assert created.series_title == OTHER
    assert created.svt_series_id == "vvm123"
    assert created.svt_slug == derive_slug(OTHER)
    assert OTHER in r.text


def test_a_written_row_is_marked_as_auto(tmp_path: Path):
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    _discover(client, maps)

    assert MappingTable.load(maps).for_tvdb(999).source == "auto"


def test_an_already_mapped_series_is_never_searched(tmp_path: Path):
    svt = SweepSvt({})
    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    _discover(client, maps)

    assert svt.queries == []


def test_two_qualifying_candidates_are_offered_not_written(tmp_path: Path):
    svt = SweepSvt({OTHER: [
        SvtSearchHit("a1", OTHER, "TvSeries"),
        SvtSearchHit("a2", OTHER.upper(), "TvSeries"),
    ]})
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert MappingTable.load(maps).for_tvdb(999) is None
    # Each candidate is one click: a form to the existing create route,
    # carrying the same "svt_id|slug" value a radio pick would.
    assert 'value="a1|' in r.text
    assert 'value="a2|' in r.text
    assert 'action="/config/mappings"' in r.text
    assert 'value="999"' in r.text


def test_a_near_miss_is_offered_not_written(tmp_path: Path):
    svt = SweepSvt({OTHER: [SvtSearchHit("j1", OTHER + " Junior", "TvSeries")]})
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert MappingTable.load(maps).for_tvdb(999) is None
    assert "Junior" in r.text


def test_no_svt_match_at_all_is_reported_not_written(tmp_path: Path):
    svt = SweepSvt({})
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert MappingTable.load(maps).for_tvdb(999) is None
    assert OTHER in r.text
    assert "no svt" in r.text.lower() or "nothing on svt" in r.text.lower()


def test_a_sonarr_outage_writes_nothing_and_does_not_500(tmp_path: Path):
    cfg, maps = _paths(tmp_path)
    before = maps.read_text(encoding="utf-8")
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, SweepSvt({}), BrokenSonarr()))
    client = TestClient(app)

    r = client.post("/config/mappings/discover",
                    data={"expected_mtime": str(maps.stat().st_mtime)})

    assert r.status_code == 200
    assert "unreachable" in _error_text(r.text)
    # Byte-identical: no partial write, no rewrite, no .bak churn.
    assert maps.read_text(encoding="utf-8") == before
    assert not (maps.parent / "mappings.yaml.bak").exists()


def test_a_form_supplied_series_title_cannot_reach_the_file(tmp_path: Path):
    # The same guarantee the manual path has, on the automatic one: the
    # title is the permanent filename and comes only from Sonarr's record.
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    _discover(client, maps, series_title="WRONG TITLE FROM FORM",
              svt_series_id="WRONG", svt_slug="wrong-slug")

    created = MappingTable.load(maps).for_tvdb(999)
    assert created.series_title == OTHER
    assert created.svt_slug == derive_slug(OTHER)
    assert "WRONG" not in maps.read_text(encoding="utf-8")


def test_an_svt_name_is_never_used_as_the_series_title(tmp_path: Path):
    # SVT's spelling differs only in case, so the gate matches -- and the
    # file must still carry Sonarr's spelling byte for byte.
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER.upper(), "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    _discover(client, maps)

    assert MappingTable.load(maps).for_tvdb(999).series_title == OTHER
    assert OTHER.upper() not in maps.read_text(encoding="utf-8")


def test_an_invalid_mappings_file_is_never_swept_over(tmp_path: Path):
    # Without knowing what is already mapped the sweep would re-search
    # everything and could write a duplicate row on top of a file it could
    # not read. Refuse before calling anything.
    cfg, maps = _paths(tmp_path)
    maps.write_text("series: [{tvdb_id: 1, ", encoding="utf-8")
    before = maps.read_text(encoding="utf-8")
    svt = SweepSvt({})
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, svt, sonarr))

    r = TestClient(app).post("/config/mappings/discover", data={})

    assert r.status_code == 200
    assert "invalid" in _error_text(r.text)
    assert svt.queries == []
    assert maps.read_text(encoding="utf-8") == before


def test_a_corrupt_expected_mtime_is_refused_before_any_search(tmp_path: Path):
    svt = SweepSvt({OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]})
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = client.post("/config/mappings/discover",
                    data={"expected_mtime": "not-a-number"})

    assert r.status_code == 200
    assert "expected_mtime" in _error_text(r.text)
    assert svt.queries == []
    assert MappingTable.load(maps).for_tvdb(999) is None


def test_a_concurrent_modification_refuses_the_whole_batch(tmp_path: Path):
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)
    before = maps.read_text(encoding="utf-8")

    r = client.post("/config/mappings/discover",
                    data={"expected_mtime": "1.0"})

    assert r.status_code == 200
    assert _error_text(r.text)
    assert maps.read_text(encoding="utf-8") == before
    # "the worst lie this page could tell": the write was refused, so the
    # page must not report the batch as mapped. Mutating `written = ()` to
    # `written = sweep.confident` left every other test green.
    assert "Mapped automatically" not in r.text
    assert "Found, but not saved" in r.text
    assert OTHER in r.text          # still shown, still one click away


def test_the_sweep_reads_episode_lists_because_that_is_what_decides(
    tmp_path: Path
):
    # The inverse of what this test used to assert. The sweep once refused
    # to fetch an episode list at all, and the gate underneath was a string
    # comparison because of it. Reading the episodes *is* the gate now, so
    # the guarantee that matters is a different one: it must read exactly
    # the candidates it is deciding between, and nothing else.
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([
        {"id": 7, "tvdbId": 288649, "title": TITLE},      # already mapped
        {"id": 9, "tvdbId": 999, "title": OTHER},
    ], episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    _discover(client, maps)

    assert svt.list_episodes_calls == [derive_slug(OTHER)]
    # Not the already-mapped series: it costs no request at all, the same
    # as before.
    assert sonarr.episode_calls == [9]


def test_the_sweep_never_touches_the_download_path(tmp_path: Path):
    # Reading episode lists is the matching read. Resolving a stream is the
    # *download* path, and no route on this page may reach it.
    class QualityIsForbidden(SweepSvt):
        async def resolve_quality(self, svt_id):
            raise AssertionError("the sweep must not resolve a stream")

    svt = QualityIsForbidden(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    assert _discover(client, maps).status_code == 200
    assert MappingTable.load(maps).for_tvdb(999) is not None


def test_a_same_named_programme_whose_episodes_disagree_is_not_written(
    tmp_path: Path
):
    # The Critical the old gate would have written: the names are
    # identical, and the episodes are months apart.
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={
            derive_slug(OTHER): _svt_run(start=SWEEP_FIRST + timedelta(days=300))
        },
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert MappingTable.load(maps).for_tvdb(999) is None
    # And the page says *why*, in the numbers that decided it.
    assert "0 of 8 episodes matched" in r.text


def test_a_differently_named_programme_whose_episodes_agree_is_written(
    tmp_path: Path
):
    # The mapping the old gate refused forever: TVDB's English title
    # against SVT's Swedish one. The title is only the query now.
    svt = SweepSvt(
        {"Married at First Sight Sweden": [
            SvtSearchHit("gvfo", TITLE, "TvSeries")
        ]},
        episodes_by_slug={derive_slug(TITLE): _svt_run()},
    )
    sonarr = FakeSonarr(
        [{"id": 9, "tvdbId": 999, "title": "Married at First Sight Sweden"}],
        episodes={9: _sonarr_run(9)},
    )
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    _discover(client, maps)

    created = MappingTable.load(maps).for_tvdb(999)
    assert created is not None
    assert created.svt_series_id == "gvfo"
    # Sonarr's spelling is still the permanent filename, not SVT's.
    assert created.series_title == "Married at First Sight Sweden"


def test_the_written_row_shows_the_evidence_that_wrote_it(tmp_path: Path):
    # A row nobody confirmed has to carry its reason on the page that
    # created it, or auditing it means reading mappings.yaml over SSH --
    # the workflow this page exists to remove.
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    assert "8 of 8 episodes matched" in _discover(client, maps).text


def test_the_sweep_corroborates_at_the_tolerance_the_service_booted_with(
    tmp_path: Path
):
    # The page must not corroborate at a different air-date window from the
    # one the resolver will later match at. A router built with the booted
    # Settings passes that value through; one built without falls back to
    # the same default the service would run at.
    from svtplay_arr.config import Settings

    hits = {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]}
    # Published two days late: outside the default tolerance of 1.
    slugged = {derive_slug(OTHER): _svt_run(start=SWEEP_FIRST + timedelta(days=2))}

    def _run(base, tolerance):
        base.mkdir(parents=True, exist_ok=True)
        cfg, maps = _paths(base)
        booted = Settings(
            sonarr_url="http://sonarr.test:8989", sonarr_api_key="k",
            incomplete_dir=base / "i", completed_dir=base / "c",
            air_date_tolerance_days=tolerance,
        )
        app = FastAPI()
        app.include_router(build_config_router(
            cfg, maps,
            SweepSvt(hits, episodes_by_slug=slugged),
            FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                       episodes={9: _sonarr_run(9)}),
            booted=booted,
        ))
        client = TestClient(app)
        client.post("/config/mappings/discover",
                    data={"expected_mtime": str(maps.stat().st_mtime)})
        return MappingTable.load(maps).for_tvdb(999)

    assert _run(tmp_path / "tight", 1) is None
    assert _run(tmp_path / "loose", 3) is not None


def test_the_sweep_page_never_renders_the_api_key(tmp_path: Path):
    # Both the corroborated path and the surfaced-with-evidence path: the
    # sweep now renders per-candidate evidence and several new failure
    # reasons, and none of them may carry the key.
    svt = SweepSvt(
        {
            OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")],
            "Show B": [SvtSearchHit("b1", "Show B", "TvSeries")],
        },
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([
        {"id": 9, "tvdbId": 999, "title": OTHER},
        {"id": 10, "tvdbId": 1000, "title": "Show B"},
    ], episodes={9: _sonarr_run(9), 10: _sonarr_run(10)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    body = _discover(client, maps).text
    assert "8 of 8 episodes matched" in body      # the page really rendered
    assert "SECRET-KEY-VALUE" not in body


def test_an_svt_search_failure_is_reported_per_series(tmp_path: Path):
    svt = SweepSvt({}, error=SvtApiError("SVT timed out"))
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert r.status_code == 200
    assert "SVT timed out" in r.text
    assert MappingTable.load(maps).for_tvdb(999) is None


def test_a_sweep_with_nothing_to_do_does_not_rewrite_the_file(tmp_path: Path):
    svt = SweepSvt({})
    sonarr = FakeSonarr([{"id": 7, "tvdbId": 288649, "title": TITLE}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)
    before = maps.stat().st_mtime, maps.read_text(encoding="utf-8")

    r = _discover(client, maps)

    assert r.status_code == 200
    assert (maps.stat().st_mtime, maps.read_text(encoding="utf-8")) == before


def test_hitting_the_search_cap_is_said_out_loud(tmp_path: Path, monkeypatch):
    import svtplay_arr.api.config_ui as config_ui

    svt = SweepSvt({})
    sonarr = FakeSonarr([
        {"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(6)
    ])
    client, maps = _sweep_client(tmp_path, svt, sonarr)
    monkeypatch.setattr(config_ui, "_SWEEP_CAP", 2)

    r = _discover(client, maps)

    assert len(svt.queries) == 2
    # The old assertions here matched anything: "4" is in the inline
    # stylesheet (14rem, .45rem) and "not searched" is in the summary line
    # that renders on every sweep ("0 already mapped, not searched"), so
    # deleting the warning outright left the suite green. Assert on the
    # warning banner itself, and on the words only it says.
    warning = re.search(r'<p class="warn">(.*?)</p>', r.text, re.S)
    assert warning, f"no cap warning banner in:\n{r.text}"
    said = " ".join(warning.group(1).split())
    assert "per-run limit of 2" in said
    assert "4 unmapped series were" in said
    assert "Run Find mappings again" in said


def test_hitting_the_request_budget_is_said_out_loud(tmp_path: Path, monkeypatch):
    # A partial sweep reported as a complete one is the failure mode that
    # matters most here: the operator reads "0 need a decision" and
    # concludes the library holds nothing more to map, when the run simply
    # stopped asking SVT.
    import svtplay_arr.api.config_ui as config_ui

    svt = SweepSvt({})
    sonarr = FakeSonarr([
        {"id": i, "tvdbId": 1000 + i, "title": f"Show {i}"} for i in range(6)
    ])
    client, maps = _sweep_client(tmp_path, svt, sonarr)
    monkeypatch.setattr(config_ui, "_SWEEP_REQUEST_BUDGET", 2)
    monkeypatch.setattr(config_ui, "_SWEEP_CONCURRENCY", 1)

    r = _discover(client, maps)

    assert len(svt.queries) == 2
    warnings = " ".join(
        " ".join(m.split())
        for m in re.findall(r'<p class="warn">(.*?)</p>', r.text, re.S)
    )
    assert "budget of 2 SVT requests" in warnings
    assert "this sweep is incomplete" in warnings
    assert "Run Find mappings again" in warnings
    # ...and the four it never reached are named as unchecked, not as
    # series SVT had nothing for.
    assert "Not checked this run" in r.text


def test_a_series_cut_off_mid_check_still_shows_what_was_learned(
    tmp_path: Path, monkeypatch
):
    # The run corroborated the first candidate and stopped before the
    # second. Nothing is written -- an unchecked rival is not a refuted one
    # -- but discarding the evidence it did gather would make the next run
    # and the operator both start from nothing.
    import svtplay_arr.api.config_ui as config_ui

    svt = SweepSvt(
        {OTHER: [
            SvtSearchHit("vvm123", OTHER, "TvSeries"),
            SvtSearchHit("vvm456", OTHER + " repris", "TvSeries"),
        ]},
        episodes_by_slug={
            derive_slug(OTHER): _svt_run(),
            derive_slug(OTHER + " repris"): _svt_run(),
        },
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)
    monkeypatch.setattr(config_ui, "_SWEEP_REQUEST_BUDGET", 2)
    monkeypatch.setattr(config_ui, "_SWEEP_CONCURRENCY", 1)

    r = _discover(client, maps)

    assert MappingTable.load(maps).for_tvdb(999) is None
    assert "Not checked this run" in r.text
    assert "8 of 8 episodes matched" in r.text     # the one it did read
    assert "not checked" in r.text                 # and the one it did not


def test_the_budget_warning_is_absent_when_the_budget_was_not_reached(
    tmp_path: Path
):
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert "sweep is incomplete" not in r.text
    assert "Not checked this run" not in r.text


def test_a_sonarr_episode_outage_is_a_rendered_page_not_a_500(tmp_path: Path):
    # Corroboration reads Sonarr's episode list, which is a new way for
    # this route to fail. It gets the same treatment as every other
    # failure here: a rendered page, an untouched file, and no 500.
    class ExplodingSonarr(FakeSonarr):
        async def episodes(self, series_id):
            raise RuntimeError("episode request failed")

    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = ExplodingSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)
    before = maps.read_text(encoding="utf-8")

    r = _discover(client, maps)

    assert r.status_code == 200
    assert MappingTable.load(maps).for_tvdb(999) is None
    assert maps.read_text(encoding="utf-8") == before
    assert "Could not be checked" in r.text


def test_the_cap_warning_is_absent_when_the_cap_was_not_reached(tmp_path: Path):
    svt = SweepSvt({})
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert "per-run limit" not in r.text


def test_a_hand_accepted_suggestion_is_not_marked_auto(tmp_path: Path):
    # The one-click accept goes through the ordinary create route, so the
    # row it writes is what it is: confirmed by a human. Marking it `auto`
    # would defeat the point of recording provenance at all.
    svt = SweepSvt({OTHER: [
        SvtSearchHit("a1", OTHER + " Junior", "TvSeries"),
        SvtSearchHit("a2", OTHER + " Senior", "TvSeries"),
    ]})
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    _discover(client, maps)
    client.post("/config/mappings", data={
        "expected_mtime": str(maps.stat().st_mtime),
        "q": OTHER,
        "svt": "a1|vem-vet-mest-junior",
        "sonarr": "999",
    })

    created = MappingTable.load(maps).for_tvdb(999)
    assert created.series_title == OTHER      # still Sonarr's, never SVT's
    assert created.source == "manual"


def test_every_confident_row_is_written_in_a_single_write(
    tmp_path: Path, monkeypatch
):
    # N add_mapping calls would be N chances for a concurrent modification
    # to land mid-sweep and leave the library half-mapped, and N .bak
    # churns for one logical change.
    import svtplay_arr.api.config_ui as config_ui
    import svtplay_arr.mappings as mappings_mod

    titles = ["Show A", "Show B", "Show C"]
    svt = SweepSvt(
        {t: [SvtSearchHit(f"id{i}", t, "TvSeries")]
         for i, t in enumerate(titles)},
        episodes_by_slug={derive_slug(t): _svt_run() for t in titles},
    )
    sonarr = FakeSonarr(
        [{"id": i, "tvdbId": 500 + i, "title": t} for i, t in enumerate(titles)],
        episodes={i: _sonarr_run(i) for i in range(len(titles))},
    )
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    # Patched only now, so the fixture's own setup write is not counted.
    writes = []
    real = mappings_mod.atomic_write_yaml
    monkeypatch.setattr(
        mappings_mod, "atomic_write_yaml",
        lambda *a, **k: (writes.append(a[0]), real(*a, **k))[1],
    )
    assert config_ui.add_mappings is mappings_mod.add_mappings

    _discover(client, maps)

    assert len(writes) == 1
    assert len(MappingTable.load(maps).all()) == 4  # the fixture row plus three


def test_only_what_the_gate_approved_is_written_alongside_it(tmp_path: Path):
    # The route must write the gate's output and nothing else. A sweep that
    # produces both a confident match and an ambiguous one is the case where
    # a loosened route could quietly slip the ambiguous one into the same
    # batch -- the confident row makes the write happen, and the extra row
    # rides along inside it.
    svt = SweepSvt(
        {
            "Confident Show": [
                SvtSearchHit("c1", "Confident Show", "TvSeries")
            ],
            OTHER: [
                SvtSearchHit("a1", OTHER, "TvSeries"),
                SvtSearchHit("a2", OTHER.upper(), "TvSeries"),
            ],
        },
        # Only the confident one has evidence. The ambiguous pair share a
        # derived slug and are left with nothing to corroborate, which is
        # a refusal, not a write.
        episodes_by_slug={derive_slug("Confident Show"): _svt_run()},
    )
    sonarr = FakeSonarr([
        {"id": 8, "tvdbId": 888, "title": "Confident Show"},
        {"id": 9, "tvdbId": 999, "title": OTHER},
    ], episodes={8: _sonarr_run(8), 9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    table = MappingTable.load(maps)
    assert table.for_tvdb(888) is not None    # the gate approved this one
    assert table.for_tvdb(999) is None        # and refused this one
    assert {m.tvdb_id for m in table.all()} == {288649, 888}
    # Second, independent assertion: neither ambiguous candidate's id may
    # appear in the file at all, however the row got there. One test
    # standing between a loosened route and a wrong permanent filename is
    # thin cover for the consequence.
    written = maps.read_text(encoding="utf-8")
    assert "a1" not in written and "a2" not in written
    # ...and the page must not describe the refused one as mapped either.
    mapped_section = r.text.split("<h2>Needs a decision</h2>")[0]
    assert "Confident Show" in mapped_section
    assert OTHER not in mapped_section


# --- Provenance in the mappings table --------------------------------
#
# A `source: auto` row that is only visible by opening mappings.yaml over
# SSH is a marker that does not do its job: auditing what the sweep
# guessed is exactly the workflow this page exists to remove. A guessed
# mapping and a hand-confirmed one must be tellable apart in the view
# most people will actually look at.


def _series_cell(html: str, title: str) -> str:
    """The Series cell for one mapping row, isolated.

    Asserting on the whole body would pass on a marker rendered against
    some other row -- or on one rendered nowhere near the table at all.
    """
    m = re.search(
        r'<td data-label="Series">(.*?)</td>',
        html[html.index(html_mod.escape(title, quote=False)) - 200:],
        re.S,
    )
    assert m, f"no Series cell found for {title!r} in:\n{html}"
    return m.group(1)


def _auto_rows_client(tmp_path: Path, rows: str):
    """A client over a mappings.yaml written verbatim, not through the writer.

    The compatibility case needs a file with no `source` key at all --
    which is what every deployment written before this feature has -- and
    that is a property of the bytes on disk, not of anything the writer
    would produce today.
    """
    cfg, maps = _paths(tmp_path)
    maps.write_text(rows, encoding="utf-8")
    app = FastAPI()
    app.include_router(build_config_router(cfg, maps, FakeSvt(), FakeSonarr()))
    return TestClient(app)


# The rendered element, not the bare class name: `auto-badge` also
# appears in base.html's stylesheet, where its presence proves nothing
# about any row.
_BADGE = '<span class="auto-badge">'

_AUTO_ROW = (
    "series:\n"
    "- tvdb_id: 1\n  svt_series_id: s1\n  svt_slug: sl1\n"
    "  series_title: Guessed Show\n  source: auto\n"
)
_MANUAL_ROW = (
    "series:\n"
    "- tvdb_id: 2\n  svt_series_id: s2\n  svt_slug: sl2\n"
    "  series_title: Confirmed Show\n  source: manual\n"
)
_LEGACY_ROW = (
    "series:\n"
    "- tvdb_id: 3\n  svt_series_id: s3\n  svt_slug: sl3\n"
    "  series_title: Legacy Show\n"
)


def test_an_auto_created_row_is_marked_in_the_mappings_table(tmp_path: Path):
    body = _auto_rows_client(tmp_path, _AUTO_ROW).get("/config/mappings").text
    assert _BADGE in _series_cell(body, "Guessed Show")


def test_a_hand_confirmed_row_carries_no_marker(tmp_path: Path):
    # A row a human picked needs no decoration; marking everything would
    # make the marker mean nothing.
    body = _auto_rows_client(tmp_path, _MANUAL_ROW).get("/config/mappings").text
    assert _BADGE not in _series_cell(body, "Confirmed Show")
    assert _BADGE not in body


def test_a_row_from_a_file_with_no_source_field_is_not_called_a_guess(
    tmp_path: Path,
):
    # The compatibility case: every mappings.yaml written before this
    # feature existed has no `source` key at all, and those rows were all
    # put there by a human. Rendering them as guesses would tell every
    # existing operator their whole library was machine-written.
    body = _auto_rows_client(tmp_path, _LEGACY_ROW).get("/config/mappings").text
    assert "Legacy Show" in body                     # it still renders
    assert _BADGE not in _series_cell(body, "Legacy Show")


def test_the_auto_marker_says_what_it_means_in_words(tmp_path: Path):
    # The lesson from the dangerous-field marker, which reached production
    # as a bare "!" whose first review question was what it meant. A glyph
    # alone carries nothing to someone seeing it for the first time.
    cell = _series_cell(
        _auto_rows_client(tmp_path, _AUTO_ROW).get("/config/mappings").text, "Guessed Show"
    )
    m = re.search(r'<span class="auto-badge"[^>]*>(.*?)</span>', cell, re.S)
    assert m, f"no auto badge in:\n{cell}"
    words = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    assert re.search(r"[A-Za-z]{4,}", words), (
        f"the auto marker carries no word, only {words!r}"
    )


def _auto_note(html: str) -> str | None:
    m = re.search(r'<p class="help auto-note">(.*?)</p>', html, re.S)
    return m.group(1) if m else None


def test_the_marker_is_explained_once_beneath_the_table(tmp_path: Path):
    # Understandable without hovering or guessing: one note under the
    # table saying what the badge means and what to do about it.
    note = _auto_note(_auto_rows_client(tmp_path, _AUTO_ROW).get("/config/mappings").text)
    assert note is not None, "the auto marker is never explained"
    assert "confirm" in note.lower()
    assert "filename" in note.lower()


def test_nothing_is_explained_when_nothing_was_auto_matched(tmp_path: Path):
    # An explanation of a marker that appears nowhere is noise on the page
    # every operator who never runs the sweep will look at.
    body = _auto_rows_client(tmp_path, _MANUAL_ROW).get("/config/mappings").text
    assert _BADGE not in body
    assert _auto_note(body) is None


def test_a_swept_row_shows_its_marker_on_the_very_next_page_load(tmp_path: Path):
    # End to end, through the real writer rather than a hand-written file:
    # what the sweep writes is what the table then marks.
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("vvm123", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    _discover(client, maps)
    body = client.get("/config/mappings").text

    assert _BADGE in _series_cell(body, OTHER)
    # The fixture row was added by hand and must not have been relabelled.
    assert _BADGE not in _series_cell(body, TITLE)


# --- C1/I2 through the real route ------------------------------------


def test_two_sonarr_series_differing_only_by_year_write_one_row(tmp_path: Path):
    # The reported end-to-end failure. Both Sonarr titles normalise to the
    # same Sonarr-side form and both matched the one SVT programme, so both
    # rows were written and marked auto. Sonarr then asked for the reboot's
    # S01E01, the resolver followed the slug to the original show, and the
    # file landed permanently under the reboot's name.
    hits = [SvtSearchHit("vvm", "Vem vet mest?", "TvSeries")]
    # Both series' episodes agree with the one SVT run -- Sonarr carrying
    # the show twice -- so the gate corroborates both and only the
    # batch-wide claim guard stops the second row.
    svt = SweepSvt(
        {"Vem vet mest?": hits, "Vem vet mest? (2021)": hits},
        episodes_by_slug={"vem-vet-mest": _svt_run()},
    )
    sonarr = FakeSonarr([
        {"id": 1, "tvdbId": 100, "title": "Vem vet mest?"},
        {"id": 2, "tvdbId": 200, "title": "Vem vet mest? (2021)"},
    ], episodes={1: _sonarr_run(1), 2: _sonarr_run(2)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    written = [m for m in MappingTable.load(maps).all() if m.svt_series_id == "vvm"]
    assert len(written) == 1
    assert written[0].tvdb_id == 100
    # And the one that lost is surfaced, not silently dropped.
    assert "Already claimed" in r.text


def test_a_year_tagged_svt_name_is_not_written(tmp_path: Path):
    # Sonarr "Big Brother (2019)" against a sole SVT hit "Big Brother
    # (2020)". Different runs, visibly different titles.
    svt = SweepSvt({
        "Big Brother (2019)": [SvtSearchHit("bb2020", "Big Brother (2020)", "TvSeries")]
    })
    sonarr = FakeSonarr([{"id": 1, "tvdbId": 100, "title": "Big Brother (2019)"}])
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert MappingTable.load(maps).for_tvdb(100) is None
    assert "Big Brother (2020)" in r.text     # offered, for a human to judge


def test_a_sweep_will_not_claim_a_programme_mapped_by_hand(tmp_path: Path):
    # The fixture row maps tvdb 288649 to svt jpmQD3q by hand. A sweep that
    # matches the same programme for a different series must not append a
    # second row to it.
    svt = SweepSvt(
        {OTHER: [SvtSearchHit("jpmQD3q", OTHER, "TvSeries")]},
        episodes_by_slug={derive_slug(OTHER): _svt_run()},
    )
    sonarr = FakeSonarr([{"id": 9, "tvdbId": 999, "title": OTHER}],
                        episodes={9: _sonarr_run(9)})
    client, maps = _sweep_client(tmp_path, svt, sonarr)

    r = _discover(client, maps)

    assert MappingTable.load(maps).for_tvdb(999) is None
    assert "Already claimed" in r.text
    assert TITLE in r.text                    # names who holds it
