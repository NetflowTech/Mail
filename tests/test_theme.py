"""Tests for emailclient.ui.theme — palette logic and theme resolution."""
from __future__ import annotations

import pytest


class TestResolvedPalette:
    def test_all_palettes_have_required_keys(self):
        from emailclient.ui.theme import PALETTES, resolved_palette
        required = {
            "BG", "PANEL", "PANEL_DARK", "BORDER",
            "TEXT", "TEXT_DIM", "TEXT_FAINT",
            "GOLD", "GOLD_DEEP", "GOLD_SOFT", "GOLD_HOVER",
            "ROW_SEP",
        }
        for name in PALETTES:
            p = resolved_palette(name)
            missing = required - set(p.keys())
            assert not missing, f"{name!r} palette missing keys: {missing}"

    def test_resolved_palette_adds_content_keys(self):
        from emailclient.ui.theme import resolved_palette
        p = resolved_palette("AMail Light")
        assert "CONTENT_BG" in p
        assert "CONTENT_TEXT" in p
        assert "CONTENT_ACCENT" in p

    def test_dark_palette_uses_light_content_bg(self):
        from emailclient.ui.theme import resolved_palette
        p = resolved_palette("AMail Dark")
        # Dark chrome, but email content sheet stays white
        assert p["CONTENT_BG"] == "#FFFFFF"

    def test_light_palette_content_bg_matches_chrome(self):
        from emailclient.ui.theme import resolved_palette
        p = resolved_palette("AMail Light")
        assert p["CONTENT_BG"] == p["BG"]

    def test_unknown_name_falls_back_to_default(self):
        from emailclient.ui.theme import resolved_palette, DEFAULT_THEME, PALETTES
        p = resolved_palette("NonExistentTheme")
        default = resolved_palette(DEFAULT_THEME)
        assert p["BG"] == default["BG"]

    def test_state_colors_present(self):
        from emailclient.ui.theme import resolved_palette
        for name in ("AMail Light", "AMail Dark"):
            p = resolved_palette(name)
            assert "DANGER" in p
            assert "SUCCESS" in p
            assert "WARNING" in p

    def test_dark_palette_is_dark_flag(self):
        from emailclient.ui.theme import resolved_palette
        assert resolved_palette("AMail Dark").get("IS_DARK") is True
        assert resolved_palette("Graphite").get("IS_DARK") is True

    def test_light_palette_is_not_dark(self):
        from emailclient.ui.theme import resolved_palette
        p = resolved_palette("AMail Light")
        assert p.get("IS_DARK", False) is False


class TestEffectiveTheme:
    def test_real_palette_name_returned_unchanged(self):
        from emailclient.ui.theme import effective_theme
        assert effective_theme("AMail Light") == "AMail Light"
        assert effective_theme("AMail Dark") == "AMail Dark"

    def test_unknown_name_returns_default(self):
        from emailclient.ui.theme import effective_theme, DEFAULT_THEME
        assert effective_theme("BogusTheme") == DEFAULT_THEME

    def test_system_theme_returns_a_palette_name(self):
        from emailclient.ui import theme
        result = theme.effective_theme(theme.SYSTEM_THEME)
        assert result in theme.PALETTES


class TestApplyTheme:
    def test_apply_sets_module_globals(self):
        from emailclient.ui import theme
        theme.apply_theme("AMail Light")
        assert theme.BG != ""
        assert theme.GOLD != ""
        assert theme.TEXT != ""

    def test_apply_dark_sets_is_dark(self):
        from emailclient.ui import theme
        theme.apply_theme("AMail Dark")
        assert theme.IS_DARK is True

    def test_apply_light_clears_is_dark(self):
        from emailclient.ui import theme
        theme.apply_theme("AMail Dark")
        theme.apply_theme("AMail Light")
        assert theme.IS_DARK is False

    def test_apply_each_named_palette(self):
        from emailclient.ui import theme
        for name in theme.PALETTES:
            theme.apply_theme(name)
            assert theme.BG != ""


class TestAccountAccent:
    def test_uses_provided_color(self):
        from emailclient.ui import theme
        theme.apply_theme("AMail Light")
        assert theme.account_accent("#DB4437") == "#DB4437"

    def test_empty_string_falls_back_to_gold(self):
        from emailclient.ui import theme
        theme.apply_theme("AMail Light")
        result = theme.account_accent("")
        assert result == theme.GOLD

    def test_none_falls_back_to_gold(self):
        from emailclient.ui import theme
        theme.apply_theme("AMail Light")
        assert theme.account_accent(None) == theme.GOLD


class TestColorHelpers:
    def test_darken_produces_darker_color(self):
        from emailclient.ui.theme import _darken
        original = "#ff8800"
        darker = _darken(original, 0.5)
        assert darker.startswith("#")
        # R channel should be halved
        assert int(darker[1:3], 16) < int(original[1:3], 16)

    def test_lighten_produces_lighter_color(self):
        from emailclient.ui.theme import _lighten
        original = "#404040"
        lighter = _lighten(original, 0.5)
        assert int(lighter[1:3], 16) > int(original[1:3], 16)

    def test_darken_fully(self):
        from emailclient.ui.theme import _darken
        assert _darken("#ffffff", 0.0) == "#000000"


class TestDensity:
    def test_default_density(self):
        from emailclient.ui.theme import DENSITIES, DEFAULT_DENSITY
        assert DEFAULT_DENSITY in DENSITIES

    def test_density_metrics_returns_dict(self):
        from emailclient.ui.theme import density_metrics, DENSITIES
        for name in DENSITIES:
            m = density_metrics(name)
            assert "ROW_H" in m
            assert "AVATAR" in m
            assert "PAD" in m

    def test_compact_smaller_than_relaxed(self):
        from emailclient.ui.theme import density_metrics
        assert density_metrics("Compact")["ROW_H"] < density_metrics("Relaxed")["ROW_H"]

    def test_unknown_density_returns_default(self):
        from emailclient.ui.theme import density_metrics, DENSITIES, DEFAULT_DENSITY
        m = density_metrics("BogusLevel")
        default = density_metrics(DEFAULT_DENSITY)
        assert m["ROW_H"] == default["ROW_H"]


class TestBuildQss:
    def test_build_qss_returns_string(self):
        from emailclient.ui import theme
        theme.apply_theme("AMail Light")
        qss = theme.build_qss()
        assert isinstance(qss, str)
        assert len(qss) > 100

    def test_qss_contains_color_values(self):
        from emailclient.ui import theme
        theme.apply_theme("AMail Light")
        qss = theme.build_qss()
        assert "#" in qss

    def test_qss_no_hardcoded_orostar_strings(self):
        # "OroStarMail" must not appear as a hardcoded string inside the QSS
        # template itself.  Asset file paths embed the *directory* name
        # (OroStarMail-customer-source) which is the user's machine layout,
        # not a code-level reference — so we strip those url() calls before
        # checking.
        import re
        from emailclient.ui import theme
        for name in theme.PALETTES:
            theme.apply_theme(name)
            qss = theme.build_qss()
            # Remove url(...) asset references before checking
            qss_no_urls = re.sub(r'url\([^)]*\)', '', qss)
            assert "orostarmail" not in qss_no_urls.lower(), (
                f"theme {name!r} QSS has a hardcoded 'OroStarMail' string"
            )
