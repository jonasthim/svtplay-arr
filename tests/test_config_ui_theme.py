"""Regression tests for base.html's dark-mode palette.

These deliberately do not assert that any particular CSS string exists --
a hex value or a selector name is free to change. What they pin down is
the two things that would actually break the page:

1. Every custom property the light (`:root`) block defines is also
   defined in the `prefers-color-scheme: dark` override. A token present
   in one palette and missing from the other is the realistic regression:
   it silently falls back to whatever the browser's initial value is
   (usually black-on-transparent), which is exactly the "looks broken in
   dark mode" failure the spec calls out.
2. The foreground/background/border pairs actually meet WCAG contrast in
   both palettes -- "check the contrast is genuinely readable" rather
   than assuming a token swap suffices.
"""

import re
from pathlib import Path

_BASE_HTML = (
    Path(__file__).resolve().parent.parent
    / "src" / "svtplay_arr" / "templates" / "base.html"
)


def _css() -> str:
    return _BASE_HTML.read_text(encoding="utf-8")


def _light_root_block(css: str) -> str:
    """The first top-level `:root { ... }` block, i.e. the light palette.

    Not the one nested inside the dark media query -- `re.search` finds
    the first match, and the light block is written before the media
    query in the file.
    """
    m = re.search(r":root\s*\{([^}]*)\}", css)
    assert m, "no :root block found in base.html"
    return m.group(1)


def _dark_root_block(css: str) -> str:
    media = re.search(
        r"@media\s*\(\s*prefers-color-scheme:\s*dark\s*\)\s*\{(.*)", css, re.S
    )
    assert media, "no @media (prefers-color-scheme: dark) block found"
    m = re.search(r":root\s*\{([^}]*)\}", media.group(1))
    assert m, "the dark-mode media query has no :root override inside it"
    return m.group(1)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);", block))


def test_dark_mode_defines_every_token_the_light_mode_defines():
    css = _css()
    light = _tokens(_light_root_block(css))
    dark = _tokens(_dark_root_block(css))

    assert light, "the light :root block defines no custom properties"
    missing = set(light) - set(dark)
    assert not missing, f"dark mode is missing these tokens: {sorted(missing)}"


def test_light_and_dark_palettes_actually_differ():
    # A dark block that just repeats the light values would pass the
    # "every token defined" check above while doing nothing for a viewer
    # in dark mode -- catch that degenerate case explicitly.
    css = _css()
    light = _tokens(_light_root_block(css))
    dark = _tokens(_dark_root_block(css))
    common = set(light) & set(dark)
    assert any(light[k] != dark[k] for k in common)


def test_body_declares_an_explicit_background_and_foreground():
    # A body with no declared background looks broken in dark mode: it
    # shows through to whatever the browser chrome behind it is, while
    # text (which usually does get styled) may already assume a light
    # page. Both must be explicit, and driven by the theme tokens.
    m = re.search(r"(?<!\.)\bbody\s*\{([^}]*)\}", _css())
    assert m, "no body rule found"
    decl = m.group(1)
    assert re.search(r"background\s*:\s*var\(--bg\)", decl)
    assert re.search(r"\bcolor\s*:\s*var\(--fg\)", decl)


def _relative_luminance(hex_color: str) -> float:
    hex_color = hex_color.strip().lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

    def lin(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = lin(r), lin(g), lin(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(hex_a: str, hex_b: str) -> float:
    la, lb = _relative_luminance(hex_a), _relative_luminance(hex_b)
    la, lb = max(la, lb), min(la, lb)
    return (la + 0.05) / (lb + 0.05)


# WCAG 2.x thresholds: 4.5:1 for normal text (AA), 3:1 for the boundary of
# a UI component such as a border against its own background.
_TEXT_MIN = 4.5
_UI_MIN = 3.0

# (foreground token, background token) pairs that carry text a viewer
# must actually read.
_TEXT_PAIRS = [
    ("--fg", "--bg"),
    ("--muted-fg", "--bg"),
    ("--input-fg", "--input-bg"),
    ("--error-fg", "--error-bg"),
    ("--notice-fg", "--notice-bg"),
    ("--warn-fg", "--warn-bg"),
    # `.status-chip` puts --fg on --code-bg, and was not covered: the
    # current values pass comfortably, which is exactly why a later
    # palette edit could make every status chip unreadable with the whole
    # theme suite still green. The status strip is the page's at-a-glance
    # health signal, so it is not a place to find that out in production.
    ("--fg", "--code-bg"),
    # The API key field's Show/Hide button: accent ink directly on the
    # input's own background, with no fill or border of its own. That is
    # not a stylistic preference -- a filled chip would need a boundary
    # against --input-bg, and --btn-border on --input-bg is only 1.87:1 in
    # the light palette, well under the 3:1 a UI boundary needs. Ink it
    # is, so the pair it actually depends on is pinned here.
    ("--accent", "--input-bg"),
    # `.danger-badge` -- the word that labels the dangerous-field marker --
    # puts --warn-fg on --bg rather than on --warn-bg, so that it reads as
    # a chip against the panel behind it instead of dissolving into it.
    # That pairing is not covered by any of the above.
    ("--warn-fg", "--bg"),
    # Added with the 2026-08-26 Sonarr restyle. Content no longer sits on
    # --bg: it sits on a panel one step lighter, so every pair below is
    # ink that is actually read but that the list above never covered.
    # --muted-fg on --panel-bg is the one that matters most and the one a
    # palette edit is most likely to break quietly: it is the field help
    # text, which the restyle deliberately made small and quiet, and
    # "quiet" is one step away from "unreadable".
    ("--fg", "--panel-bg"),
    ("--muted-fg", "--panel-bg"),
    # The panel's own header strip is a third background, and its text is
    # muted and small.
    ("--muted-fg", "--panel-head-bg"),
    # The app bar carries its own colours in both themes rather than
    # inheriting the page's, so it needs its own pair.
    ("--topbar-fg", "--topbar-bg"),
    # Links, on both surfaces they appear on.
    ("--accent", "--panel-bg"),
    ("--accent", "--bg"),
    # Button labels, on each of the three button fills.
    ("--on-accent", "--accent"),
    ("--on-danger", "--danger"),
    ("--btn-fg", "--btn-bg"),
    # A hovered mapping row still has to be readable, not just different.
    ("--fg", "--row-hover-bg"),
]

# (border token, background token) pairs -- the border is a UI boundary,
# not body text, so it only needs to clear the lower non-text threshold.
_BORDER_PAIRS = [
    ("--input-border", "--input-bg"),
    ("--error-border", "--error-bg"),
    ("--notice-border", "--notice-bg"),
    ("--warn-border", "--warn-bg"),
]


def _assert_pairs_meet(tokens: dict[str, str], pairs, minimum: float, theme: str):
    for fg_name, bg_name in pairs:
        assert fg_name in tokens, f"{theme}: {fg_name} is not defined"
        assert bg_name in tokens, f"{theme}: {bg_name} is not defined"
        ratio = _contrast(tokens[fg_name], tokens[bg_name])
        assert ratio >= minimum, (
            f"{theme}: {fg_name} on {bg_name} is only {ratio:.2f}:1 "
            f"(need >= {minimum}:1)"
        )


def test_light_palette_meets_contrast_minimums():
    tokens = _tokens(_light_root_block(_css()))
    _assert_pairs_meet(tokens, _TEXT_PAIRS, _TEXT_MIN, "light")
    _assert_pairs_meet(tokens, _BORDER_PAIRS, _UI_MIN, "light")


def test_dark_palette_meets_contrast_minimums():
    tokens = _tokens(_dark_root_block(_css()))
    _assert_pairs_meet(tokens, _TEXT_PAIRS, _TEXT_MIN, "dark")
    _assert_pairs_meet(tokens, _BORDER_PAIRS, _UI_MIN, "dark")


def test_the_muted_ink_is_actually_quieter_than_the_body_ink():
    # The restyle's hierarchy rests on help text being visibly subordinate
    # to its label. It is easy to satisfy the contrast minimums above by
    # simply making --muted-fg equal to --fg, at which point every field's
    # multi-sentence explanation is back to competing with its label --
    # the exact defect the restyle existed to fix -- with the whole theme
    # suite still green. So: quieter than --fg, and still comfortably
    # clear of the backgrounds it is read on (asserted above).
    for block_fn, theme in ((_light_root_block, "light"), (_dark_root_block, "dark")):
        tokens = _tokens(block_fn(_css()))
        fg = _contrast(tokens["--fg"], tokens["--panel-bg"])
        muted = _contrast(tokens["--muted-fg"], tokens["--panel-bg"])
        assert muted < fg, (
            f"{theme}: --muted-fg is not quieter than --fg "
            f"({muted:.2f}:1 vs {fg:.2f}:1)"
        )


def test_message_styles_have_distinct_colours_from_each_other():
    # error/notice/warn must be tellable apart by colour alone in each
    # theme -- on top of (not instead of) the icon and wording, since a
    # viewer scanning by colour should still get useful signal.
    for block_fn, theme in ((_light_root_block, "light"), (_dark_root_block, "dark")):
        tokens = _tokens(block_fn(_css()))
        backgrounds = {
            tokens["--error-bg"],
            tokens["--notice-bg"],
            tokens["--warn-bg"],
        }
        assert len(backgrounds) == 3, f"{theme}: message backgrounds collide"


def test_message_styles_do_not_rely_on_colour_alone():
    # Each of the four message classes must carry a distinguishing glyph
    # via ::before, independent of the colour tokens -- this is what
    # keeps error/notice/warn/pending apart for a viewer who cannot
    # distinguish the hues.
    css = _css()
    for cls in (".error", ".notice", ".warn", ".pending"):
        pattern = re.compile(re.escape(cls) + r"::before\s*\{\s*content:\s*\"[^\"]+\"")
        assert pattern.search(css), f"{cls} has no distinguishing glyph"


def test_pending_is_distinguishable_from_plain_warn():
    # .warn and .pending intentionally share the same caution colour
    # tokens (both are "this needs attention"); they must still be told
    # apart some other way -- font weight, border weight, or icon.
    css = _css()
    # Anchored to the start of a line so this matches only the standalone
    # `.pending { ... }` rule, not the `.warn, .pending { ... }`
    # shared-colour rule that also contains the substring ".pending {".
    pending_rule = re.search(r"(?:^|\n)\s*\.pending\s*\{([^}]*)\}", css)
    assert pending_rule, "no standalone .pending rule"
    assert "font-weight: 600" in pending_rule.group(1) or "700" in pending_rule.group(1)


def test_the_sonarr_surfaces_declare_no_colour_of_their_own():
    # The Sonarr chip on the status strip and the Test connection result
    # both reuse `.status-chip` and the `.error`/`.notice`/`.warn` message
    # styles, whose token pairs are pinned above in both palettes. Their own
    # rules may set geometry and nothing else -- a `color:` or a
    # `background:` here would be a fifth state colour that no contrast test
    # covers, on the two surfaces an operator reads to decide whether the
    # service is working.
    css = _css()
    for selector in (r"\.sonarr-test-result", r"\.sonarr-test \.control > \.help"):
        rule = re.search(selector + r"\s*\{([^}]*)\}", css)
        assert rule, f"no {selector} rule found"
        decl = rule.group(1)
        assert "color:" not in decl, decl
        assert "background" not in decl, decl
