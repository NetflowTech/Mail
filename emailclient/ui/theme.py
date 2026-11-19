"""AMail visual theme system: named palettes + one accent.

All chrome colors live here. Email content stays untouched (it is the hero).

NOTE on naming: GOLD / GOLD_DEEP / GOLD_SOFT / GOLD_HOVER are SEMANTIC accent
slots (indicator / text-safe accent / selection fill / hover fill). Themes
like "Mint Fresh" map them to teal — the names stay so the dozens of
paint-time `theme.GOLD` reads across the app keep working unchanged.

Modules read these constants at RUNTIME (e.g. delegate paint), so
`apply_theme` mutates the module globals in place and a repaint picks the
new palette up.
"""
from __future__ import annotations


from PySide6.QtCore import QSettings

DEFAULT_THEME = "AMail Light"

PALETTES = {
    "AMail Light": {  # clean white, airy neutrals, one gold accent
        "BG": "#FFFFFF",          # content surface
        "PANEL": "#F5F6F8",       # sidebar / panel layer
        "PANEL_DARK": "#E8EAED",  # pressed / borders on panel
        "BORDER": "#E8EAED",
        "TEXT": "#1F2125",        # near-black
        "TEXT_DIM": "#70757D",
        "TEXT_FAINT": "#858B94",  # ≥3:1 on the panel: meta text stays legible
        "GOLD": "#C29A3B",        # indicators, dots, focus rings
        "GOLD_DEEP": "#8A6D14",   # text-safe accent, primary pill buttons (≥4.5:1)
        "GOLD_SOFT": "#F7F1DE",   # selection fill
        "GOLD_HOVER": "#F4F5F7",  # neutral hover fill
        "ROW_SEP": "#F0F1F3",
    },
    "Mint Fresh": {  # white surfaces, mint/teal accent
        "BG": "#FFFFFF",
        "PANEL": "#F4F7F6",
        "PANEL_DARK": "#E6EDEC",
        "BORDER": "#E6EBEA",
        "TEXT": "#23292E",
        "TEXT_DIM": "#66727A",
        "TEXT_FAINT": "#83919A",
        "GOLD": "#1FBF96",
        "GOLD_DEEP": "#0A7F5F",
        "GOLD_SOFT": "#E3F7F1",
        "GOLD_HOVER": "#F1FAF7",
        "ROW_SEP": "#EFF3F2",
    },
    "Parchment": {  # warm cream light — paper, not screen
        "BG": "#FFFDF8",
        "PANEL": "#F6F1E6",
        "PANEL_DARK": "#E9E1D0",
        "BORDER": "#EAE2D2",
        "TEXT": "#241F17",
        "TEXT_DIM": "#6E6558",
        "TEXT_FAINT": "#8A8073",
        "GOLD": "#B07D2B",
        "GOLD_DEEP": "#8F6212",
        "GOLD_SOFT": "#F6EBD3",
        "GOLD_HOVER": "#F3EEE4",
        "ROW_SEP": "#F0EADC",
    },
    "Slate & Clay": {  # cool grey surfaces, terracotta accent
        "BG": "#FFFFFF",
        "PANEL": "#F4F5F7",
        "PANEL_DARK": "#E5E7EB",
        "BORDER": "#E6E8EC",
        "TEXT": "#1B1E23",
        "TEXT_DIM": "#6B717A",
        "TEXT_FAINT": "#848B95",
        "GOLD": "#B5623F",
        "GOLD_DEEP": "#96482A",
        "GOLD_SOFT": "#F8EBE4",
        "GOLD_HOVER": "#F3F4F6",
        "ROW_SEP": "#EFF1F3",
    },
    # ---- dark palettes -------------------------------------------------
    # Dark themes darken the CHROME only. The email itself keeps a light
    # sheet (CONTENT_*): messages are white HTML, and a dark frame around a
    # white body is the one thing this app is explicitly not trying to be.
    "AMail Dark": {  # deep neutral charcoal, the house gold
        # The old version tinted every surface brown (R well above B), which
        # read as muddy. Surfaces are now near-neutral with only a trace of
        # warmth; the gold carries all the colour, so it pops instead of
        # blending into a brown background.
        "IS_DARK": True,
        "BG": "#161616",          # content surface: deep, clean
        "PANEL": "#1E1E1F",       # sidebar / panels: one clear step up
        "PANEL_DARK": "#2A2A2C",  # hover / pressed
        "BORDER": "#2C2C2E",
        "TEXT": "#F2F0EB",        # soft warm white, not stark
        "TEXT_DIM": "#A9A69F",
        "TEXT_FAINT": "#7B7873",
        "GOLD": "#E3B95A",        # indicators, dots, focus rings
        "GOLD_DEEP": "#EBC46C",   # text-safe accent, primary buttons (dark text on it)
        "GOLD_SOFT": "#33301F",   # selection fill: gold-tinted, still dark
        "GOLD_HOVER": "#232324",  # neutral hover fill
        "ROW_SEP": "#232323",
    },
    "Graphite": {  # neutral dark, copper accent
        "IS_DARK": True,
        "BG": "#16181A",
        "PANEL": "#1D2023",
        "PANEL_DARK": "#272B2F",
        "BORDER": "#2A2E33",
        "TEXT": "#E6E9EC",
        "TEXT_DIM": "#9AA3AC",
        "TEXT_FAINT": "#6F7880",
        "GOLD": "#C98A5A",
        "GOLD_DEEP": "#DDA070",
        "GOLD_SOFT": "#33261D",
        "GOLD_HOVER": "#262A2E",
        "ROW_SEP": "#22262A",
    },
}

# Follows the OS light/dark setting instead of pinning one palette.
SYSTEM_THEME = "System"
SYSTEM_LIGHT = "AMail Light"
SYSTEM_DARK = "AMail Dark"

# Semantic state colours. Kept out of the palettes so every theme gets them;
# a palette may still override any of these by naming them.
_STATE_LIGHT = {
    "DANGER": "#C0392B",
    "SUCCESS": "#2E7D52",
    "WARNING": "#9A6414",
}
_STATE_DARK = {
    "DANGER": "#E9736A",
    "SUCCESS": "#5FBF8B",
    "WARNING": "#E0B252",
}


def _content_defaults(palette: dict) -> dict:
    """Colours for the email sheet itself (reader header + body).

    Light themes reuse their own chrome colours. Dark themes deliberately keep
    a light sheet: emails are white HTML, so a dark frame around a white body
    reads as broken rather than as dark mode.
    """
    if palette.get("IS_DARK"):
        return {
            "CONTENT_BG": "#FFFFFF",
            "CONTENT_TEXT": "#1F2125",
            "CONTENT_DIM": "#70757D",
            "CONTENT_FAINT": "#9AA0A8",
            "CONTENT_BORDER": "#E8EAED",
            "CONTENT_PANEL": "#F5F6F8",
            # Links and attachment chips live on the light sheet, so they need
            # an accent that reads on white, not the light-on-dark one.
            "CONTENT_ACCENT": "#A4831B",
            "CONTENT_ACCENT_SOFT": "#F7F1DE",
        }
    return {
        "CONTENT_BG": palette["BG"],
        "CONTENT_TEXT": palette["TEXT"],
        "CONTENT_DIM": palette["TEXT_DIM"],
        "CONTENT_FAINT": palette["TEXT_FAINT"],
        "CONTENT_BORDER": palette["BORDER"],
        "CONTENT_PANEL": palette["PANEL"],
        "CONTENT_ACCENT": palette["GOLD_DEEP"],
        "CONTENT_ACCENT_SOFT": palette["GOLD_SOFT"],
    }


def _derived_defaults(palette: dict) -> dict:
    """Tokens every surface needs but no palette should have to spell out.

    These exist so call sites never hardcode "white" or "black": the colour
    that sits ON an accent, the text-selection pair, the inverted toast and
    the disabled-text shade all flip correctly between light and dark chrome.
    """
    dark = bool(palette.get("IS_DARK"))
    return {
        # Text/icons drawn on a GOLD_DEEP fill (primary buttons, selection).
        "ON_ACCENT": palette["BG"],
        # Text selection in editors, lists and combo popups. GOLD_DEEP is the
        # text-safe accent, so its inverse (the surface colour) stays legible
        # on it in both modes — plain white on gold did not.
        "SELECTION_BG": palette["GOLD_DEEP"],
        "SELECTION_TEXT": palette["BG"],
        # Disabled controls: clearly dimmer than TEXT_FAINT, never invisible.
        "TEXT_DISABLED": (_darken(palette["TEXT_FAINT"], 0.78) if dark
                          else _lighten(palette["TEXT_FAINT"], 0.35)),
        # The "undo send" toast is inverted on light chrome (dark pill on a
        # light window). On dark chrome an inverted pill would be a white slab,
        # so it becomes a raised panel instead.
        "TOAST_BG": palette["PANEL_DARK"] if dark else palette["TEXT"],
        "TOAST_TEXT": palette["TEXT"] if dark else palette["BG"],
        "TOAST_ACCENT": palette["GOLD"] if dark else _lighten(palette["GOLD"], 0.25),
        # Tooltips: a panel with the theme's own text, in both modes. Never
        # inverted — if the platform fails to paint the tooltip background
        # (macOS does, for rounded ones) the text still reads on the window.
        "TOOLTIP_BG": palette["PANEL_DARK"] if dark else palette["PANEL"],
        "TOOLTIP_TEXT": palette["TEXT"],
    }


def resolved_palette(name: str) -> dict:
    """PALETTES[name] plus every derived token, so switching themes can never
    leave a stale colour behind from the palette before it."""
    palette = dict(PALETTES.get(name, PALETTES[DEFAULT_THEME]))
    palette.setdefault("IS_DARK", False)
    merged = dict(_STATE_DARK if palette["IS_DARK"] else _STATE_LIGHT)
    merged.update(_content_defaults(palette))
    merged.update(_derived_defaults(palette))
    merged.update(palette)          # an explicit palette value always wins
    return merged


# Mutable module globals — set by apply_theme() below (defaults keep linters
# and "import theme before apply" callers safe).
BG = PANEL = PANEL_DARK = BORDER = ""
TEXT = TEXT_DIM = TEXT_FAINT = TEXT_DISABLED = ""
GOLD = GOLD_DEEP = GOLD_SOFT = GOLD_HOVER = ROW_SEP = ""
DANGER = SUCCESS = WARNING = ""
CONTENT_BG = CONTENT_TEXT = CONTENT_DIM = CONTENT_FAINT = ""
CONTENT_BORDER = CONTENT_PANEL = ""
CONTENT_ACCENT = CONTENT_ACCENT_SOFT = ""
ON_ACCENT = SELECTION_BG = SELECTION_TEXT = ""
TOAST_BG = TOAST_TEXT = TOAST_ACCENT = ""
TOOLTIP_BG = TOOLTIP_TEXT = ""
IS_DARK = False

# Every colour token name, for `tokens()` / `color()` below.
_TOKEN_NAMES = (
    "BG", "PANEL", "PANEL_DARK", "BORDER", "ROW_SEP",
    "TEXT", "TEXT_DIM", "TEXT_FAINT", "TEXT_DISABLED",
    "GOLD", "GOLD_DEEP", "GOLD_SOFT", "GOLD_HOVER",
    "DANGER", "SUCCESS", "WARNING",
    "CONTENT_BG", "CONTENT_TEXT", "CONTENT_DIM", "CONTENT_FAINT",
    "CONTENT_BORDER", "CONTENT_PANEL", "CONTENT_ACCENT", "CONTENT_ACCENT_SOFT",
    "ON_ACCENT", "SELECTION_BG", "SELECTION_TEXT",
    "TOAST_BG", "TOAST_TEXT", "TOAST_ACCENT", "TOOLTIP_BG", "TOOLTIP_TEXT",
)


def tokens() -> dict:
    """Current colour tokens as a dict (for str.format-style templates)."""
    g = globals()
    return {k: g[k] for k in _TOKEN_NAMES}


def color(spec: str | None, default: str = "TEXT") -> str:
    """Resolve a colour spec to a hex string, RIGHT NOW.

    *spec* may be a token name ("TEXT_FAINT", "GOLD_DEEP"), a literal colour
    ("#C0392B"), or None/"" for *default*. Call sites that keep a token name
    instead of a baked hex value can be re-resolved after a theme switch.
    """
    spec = spec or default
    g = globals()
    if spec in _TOKEN_NAMES:
        return g[spec]
    return spec


def account_accent(color: str | None) -> str:
    """Effective accent for an account dot/label/chip.

    Empty/None means "use the app's theme accent" — so by default every account
    looks consistent (like Outlook), and only an explicitly chosen per-account
    color overrides it.
    """
    return color if color else GOLD


def _darken(hex_color: str, factor: float) -> str:
    """Darken #RRGGBB by factor (0..1) — for pressed/hover accent shades."""
    r = int(int(hex_color[1:3], 16) * factor)
    g = int(int(hex_color[3:5], 16) * factor)
    b = int(int(hex_color[5:7], 16) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lighten(hex_color: str, factor: float) -> str:
    """Move #RRGGBB toward white by factor (0..1)."""
    def ch(i):
        v = int(hex_color[i:i + 2], 16)
        return min(255, int(v + (255 - v) * factor))
    return f"#{ch(1):02x}{ch(3):02x}{ch(5):02x}"


def _shade(hex_color: str, amount: float) -> str:
    """Step an accent away from the surface it sits on.

    On light chrome that means darker; on dark chrome, lighter. Using one
    direction for both made hover states vanish in the dark palettes.
    """
    return (_lighten(hex_color, amount) if IS_DARK
            else _darken(hex_color, 1.0 - amount))


# ---- density -------------------------------------------------------------
# How much air a message row gets. The layout is the same at every level; only
# the metrics change, so nothing is hidden from the user at Compact.
DENSITIES = {
    "Compact":     {"ROW_H": 68, "AVATAR": 32, "PAD": 12},
    "Comfortable": {"ROW_H": 84, "AVATAR": 38, "PAD": 14},
    "Relaxed":     {"ROW_H": 98, "AVATAR": 42, "PAD": 16},
}
DEFAULT_DENSITY = "Comfortable"


def current_density(qs: QSettings | None = None) -> str:
    qs = qs if qs is not None else QSettings("AMail", "ui")
    name = str(qs.value("density", DEFAULT_DENSITY))
    return name if name in DENSITIES else DEFAULT_DENSITY


def set_density(name: str, qs: QSettings | None = None) -> None:
    qs = qs if qs is not None else QSettings("AMail", "ui")
    qs.setValue("density", name if name in DENSITIES else DEFAULT_DENSITY)


def density_metrics(name: str | None = None) -> dict:
    """Row metrics for *name* (or the persisted choice)."""
    return dict(DENSITIES.get(name or current_density(),
                              DENSITIES[DEFAULT_DENSITY]))


def on_content(color_role: str) -> str:
    """Chrome token → the matching colour on the light email sheet.

    Lets one call site serve both, instead of branching on IS_DARK everywhere.
    """
    return {
        "TEXT": CONTENT_TEXT, "TEXT_DIM": CONTENT_DIM,
        "TEXT_FAINT": CONTENT_FAINT, "BORDER": CONTENT_BORDER,
        "BG": CONTENT_BG, "PANEL": CONTENT_PANEL,
    }.get(color_role, color_role)


def system_prefers_dark() -> bool:
    """True when the OS is in dark mode. Falls back to False if we can't tell."""
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt
        hints = QGuiApplication.styleHints()
        if hints is not None and hasattr(hints, "colorScheme"):
            return hints.colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        pass
    return False


def effective_theme(name: str) -> str:
    """Resolve the stored choice to a real palette name.

    "System" is not a palette — it tracks the OS light/dark setting.
    """
    if name == SYSTEM_THEME:
        return SYSTEM_DARK if system_prefers_dark() else SYSTEM_LIGHT
    return name if name in PALETTES else DEFAULT_THEME


def apply_theme(name: str) -> None:
    """Set the module's color globals from PALETTES[name] (fallback default)."""
    globals().update(resolved_palette(effective_theme(name)))


def current_theme(qs: QSettings | None = None) -> str:
    """Persisted theme choice — a palette name, or "System" to follow the OS."""
    qs = qs if qs is not None else QSettings("AMail", "ui")
    name = str(qs.value("theme", DEFAULT_THEME))
    if name == SYSTEM_THEME or name in PALETTES:
        return name
    return DEFAULT_THEME


def set_theme(name: str, qs: QSettings | None = None) -> None:
    """Persist + apply a theme."""
    qs = qs if qs is not None else QSettings("AMail", "ui")
    qs.setValue("theme", name)
    apply_theme(name)


# ---- live re-theming -----------------------------------------------------
# The stylesheet is rebuilt on a theme switch, but plenty of chrome is painted
# from the globals above at CONSTRUCTION time (icons tinted with TEXT, inline
# stylesheets, item foregrounds). Those would keep the old palette — which is
# how dark icons ended up on dark toolbars. Anything like that registers a
# listener here, or hands its stylesheet template to `style()`, and gets
# refreshed by `apply_live()`.
import weakref as _weakref

_listeners: list = []
_QSS_TEMPLATE_PROP = "_amailQssTemplate"


def on_change(callback) -> None:
    """Call *callback()* after every live theme/density change.

    Bound methods are held weakly, so a widget that is gone simply drops off.
    """
    try:
        ref = _weakref.WeakMethod(callback)
    except TypeError:               # plain function / lambda: hold it strongly
        ref = (lambda cb=callback: cb)
    _listeners.append(ref)


def _notify() -> None:
    for ref in list(_listeners):
        cb = ref()
        if cb is None:
            _listeners.remove(ref)
            continue
        try:
            cb()
        except RuntimeError:        # underlying C++ widget already deleted
            _listeners.remove(ref)


_TOKEN_RE = __import__("re").compile(r"\{([A-Z_]+)\}")


def render(template: str) -> str:
    """Fill `{TOKEN}` placeholders in a QSS template from the current palette.

    Deliberately NOT str.format(): that would also honour attribute and index
    lookups such as `{TEXT.__class__}` and so turn any template that ever
    carried untrusted text (a file name, an account label) into a way to poke
    at Python internals. Only bare, known token names are replaced; anything
    else is left exactly as written. `{{` / `}}` still mean literal braces.
    """
    toks = tokens()
    body = template.replace("{{", "\x00").replace("}}", "\x01")
    body = _TOKEN_RE.sub(lambda m: toks.get(m.group(1), m.group(0)), body)
    return body.replace("\x00", "{").replace("\x01", "}")


def style(widget, template: str) -> None:
    """Apply a stylesheet whose colours are `{TOKEN}` placeholders.

    The template is kept on the widget, so `apply_live()` can re-render it
    with the new palette. Literal braces in the QSS are written doubled, the
    same as in an f-string.
    """
    widget.setProperty(_QSS_TEMPLATE_PROP, template)
    widget.setStyleSheet(render(template))


def _restyle_templates() -> None:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            template = w.property(_QSS_TEMPLATE_PROP)
            if template:
                w.setStyleSheet(render(str(template)))
        except RuntimeError:
            continue


def build_palette():
    """A QPalette matching the current globals.

    The stylesheet styles what it names; everything else (tooltips,
    placeholder text, disabled text, message boxes, native-ish dialogs,
    progress bars, header views…) is drawn by the Fusion style from the
    palette. Without this, a dark theme sat on Fusion's light palette and the
    OS palette leaked in: black text on dark panels, grey placeholders that
    vanished, white tooltips with white text.
    """
    from PySide6.QtGui import QColor, QPalette
    pal = QPalette()
    R = QPalette.ColorRole
    G = QPalette.ColorGroup

    def c(hex_color):
        return QColor(hex_color)

    for role, value in (
        (R.Window, BG), (R.WindowText, TEXT),
        (R.Base, BG), (R.AlternateBase, PANEL), (R.Text, TEXT),
        (R.Button, PANEL), (R.ButtonText, TEXT), (R.BrightText, ON_ACCENT),
        (R.Highlight, SELECTION_BG), (R.HighlightedText, SELECTION_TEXT),
        (R.ToolTipBase, TOOLTIP_BG), (R.ToolTipText, TOOLTIP_TEXT),
        (R.PlaceholderText, TEXT_FAINT),
        (R.Link, GOLD_DEEP), (R.LinkVisited, GOLD_DEEP),
        (R.Light, BG if not IS_DARK else PANEL_DARK),
        (R.Midlight, PANEL), (R.Mid, BORDER), (R.Dark, PANEL_DARK),
        (R.Shadow, BORDER),
        (R.Accent, GOLD_DEEP) if hasattr(R, "Accent") else (R.Highlight, SELECTION_BG),
    ):
        pal.setColor(role, c(value))
    for role in (R.WindowText, R.Text, R.ButtonText, R.HighlightedText):
        pal.setColor(G.Disabled, role, c(TEXT_DISABLED))
    pal.setColor(G.Disabled, R.Base, c(PANEL))
    pal.setColor(G.Disabled, R.Button, c(PANEL))
    pal.setColor(G.Disabled, R.Highlight, c(PANEL_DARK))
    return pal


def apply_live(name: str | None = None) -> None:
    """Switch the running app to *name* (or re-apply the persisted choice).

    Updates the globals, the application palette and stylesheet, then every
    registered listener and `style()` template. This is the one entry point
    for a theme change while windows are open.
    """
    from PySide6.QtWidgets import QApplication
    apply_theme(name if name is not None else current_theme())
    app = QApplication.instance()
    if app is not None:
        app.setPalette(build_palette())
        app.setStyleSheet(build_qss())
        _restyle_templates()
    _notify()


def install(app) -> None:
    """Apply the persisted theme to a fresh QApplication and keep "System"
    tracking the OS: macOS/Windows flipping to dark at sunset used to leave
    the app on its light palette until the next launch."""
    apply_live(current_theme())
    try:
        hints = app.styleHints()
        signal = getattr(hints, "colorSchemeChanged", None)
        if signal is not None:
            signal.connect(_on_system_scheme_changed)
    except Exception:
        pass


def _on_system_scheme_changed(*_args) -> None:
    if current_theme() == SYSTEM_THEME:
        apply_live(SYSTEM_THEME)


def build_qss() -> str:
    """App-wide stylesheet from the CURRENT globals (call after apply_theme)."""
    primary_hover = _shade(GOLD_DEEP, 0.13)
    primary_pressed = _shade(GOLD_DEEP, 0.26)
    from ..paths import asset_dir
    assets = asset_dir()
    check_png = (assets / "check.png").as_posix()
    arrow_r = (assets / "arrow_right.png").as_posix()
    arrow_d = (assets / "arrow_down.png").as_posix()
    return f"""
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1.5px solid {TEXT_FAINT};
    border-radius: 4px;
    background: {BG};
}}
QCheckBox::indicator:hover {{ border-color: {GOLD_DEEP}; }}
QCheckBox::indicator:checked {{
    background: {GOLD_DEEP};
    border-color: {GOLD_DEEP};
    image: url({check_png});
}}
QCheckBox::indicator:disabled {{ background: {PANEL}; border-color: {BORDER}; }}

QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1.5px solid {TEXT_FAINT};
    border-radius: 9px;
    background: {BG};
}}
QRadioButton::indicator:hover {{ border-color: {GOLD_DEEP}; }}
QRadioButton::indicator:checked {{
    /* thick accent ring with a light core = clear selected state */
    border: 5px solid {GOLD_DEEP};
    background: {BG};
}}
QRadioButton::indicator:disabled {{ background: {PANEL}; border-color: {BORDER}; }}
* {{
    font-family: -apple-system, "SF Pro Text", "Segoe UI", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QDialog {{ background: {BG}; }}
/* Plain containers draw from the palette (Window role = BG) — but anything
   the style paints on its own needs a rule, or the OS/Fusion default shows
   through. The rules below exist so no widget can fall back to black-on-dark
   or white-on-light in any theme. */
QMessageBox, QInputDialog, QFileDialog, QColorDialog, QFontDialog,
QProgressDialog, QWizard, QErrorMessage {{
    background: {BG};
    color: {TEXT};
}}
/* Qt's tooltip window is a QLabel subclass, so never add a bare
   "QLabel {{ background: ... }}" rule to this sheet: it outranks this one and
   turned tooltips transparent. Square corners on purpose — a rounded tooltip
   needs a window mask, which is unreliable on macOS. */
QToolTip {{
    background-color: {TOOLTIP_BG};
    color: {TOOLTIP_TEXT};
    border: 1px solid {PANEL_DARK};
    padding: 4px 8px;
}}
QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled, QGroupBox:disabled {{
    color: {TEXT_DISABLED};
}}
QPushButton:disabled {{
    background: {PANEL};
    border-color: {BORDER};
    color: {TEXT_DISABLED};
}}
QPushButton#primary:disabled {{
    background: {PANEL_DARK};
    color: {TEXT_DISABLED};
}}
QLineEdit:disabled, QComboBox:disabled, QPlainTextEdit:disabled,
QTextEdit:disabled, QSpinBox:disabled, QDateEdit:disabled {{
    background: {PANEL};
    color: {TEXT_DISABLED};
    border-color: {BORDER};
}}
QLineEdit, QPlainTextEdit, QTextEdit {{ placeholder-text-color: {TEXT_FAINT}; }}

QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 3px 6px;
    color: {TEXT};
}}
QToolButton:hover {{ background: {GOLD_HOVER}; border-color: {BORDER}; }}
QToolButton:pressed, QToolButton:checked {{ background: {GOLD_SOFT}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

QSpinBox, QDoubleSpinBox {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px 8px;
    selection-background-color: {SELECTION_BG};
    selection-color: {SELECTION_TEXT};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {GOLD}; }}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_DIM};
}}

QProgressBar {{
    background: {PANEL_DARK};
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: {TEXT_DIM};
}}
QProgressBar::chunk {{ background: {GOLD_DEEP}; border-radius: 4px; }}

QSlider::groove:horizontal {{
    background: {PANEL_DARK};
    height: 4px;
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {GOLD_DEEP}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {BG};
    border: 2px solid {GOLD_DEEP};
    width: 14px; height: 14px;
    margin: -6px 0;
    border-radius: 8px;
}}

QHeaderView::section {{
    background: {PANEL};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER};
    padding: 5px 8px;
}}
QTableView, QTableWidget, QTreeView, QListView {{
    background: {BG};
    alternate-background-color: {PANEL};
    gridline-color: {BORDER};
    selection-background-color: {GOLD_SOFT};
    selection-color: {TEXT};
    outline: 0;
}}
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
    background: {GOLD_SOFT};
    color: {TEXT};
}}
QTableCornerButton::section {{ background: {PANEL}; border: none; }}

QAbstractScrollArea {{ background: {BG}; }}
QScrollArea {{ background: {BG}; border: none; }}
QStackedWidget {{ background: {BG}; }}

QFrame[frameShape="4"], QFrame[frameShape="5"] {{  /* HLine / VLine */
    color: {BORDER};
    background: {BORDER};
}}
QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 4px 2px; }}
QMenuBar {{ background: {BG}; color: {TEXT}; }}
QMenuBar::item:selected {{ background: {GOLD_SOFT}; }}
QMenu::item:disabled {{ color: {TEXT_DISABLED}; }}
QSizeGrip {{ background: transparent; width: 0; height: 0; }}

QToolBar {{
    background: {BG};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 10px;
    spacing: 8px;
}}

QPushButton {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 5px 14px;
}}
QPushButton:hover {{ background: {GOLD_HOVER}; border-color: {PANEL_DARK}; }}
QPushButton:pressed {{ background: {GOLD_SOFT}; }}
/* Active state for checkable filter toggles (Unread / Flagged) so it's obvious
   the filter is ON and a second click clears it. */
QPushButton:checked {{ background: {GOLD_SOFT}; border-color: {GOLD}; color: {GOLD_DEEP}; font-weight: 600; }}
QPushButton#primary {{
    background: {GOLD_DEEP};
    color: {BG};
    border: none;
    border-radius: 16px;
    padding: 6px 18px;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {primary_hover}; }}
QPushButton#primary:pressed {{ background: {primary_pressed}; }}

QPushButton#flatTool {{
    background: transparent;
    border: none;
    border-radius: 14px;
    padding: 0;
    min-width: 28px;  max-width: 28px;
    min-height: 28px; max-height: 28px;
}}
QPushButton#flatTool:hover {{ background: {PANEL}; }}
QPushButton#flatTool:pressed, QPushButton#flatTool:checked {{
    background: {GOLD_SOFT};
}}

QFrame#morePanel {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#morePanel QPushButton {{
    text-align: left;
    padding: 6px 14px;
}}

QPushButton#ccBccToggle {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 2px 8px;
    color: {TEXT_DIM};
    font-size: 12px;
}}
QPushButton#ccBccToggle:hover {{ background: {GOLD_HOVER}; color: {GOLD_DEEP}; }}

QWidget#groupBar {{
    background: {PANEL};
    border-bottom: 1px solid {BORDER};
}}
QPushButton#chip {{
    border: 1px solid {BORDER};
    border-radius: 14px;
    padding: 4px 14px;
    background: {BG};
}}
QPushButton#chip:hover {{ border-color: {GOLD}; }}
QPushButton#chip:checked {{
    background: {GOLD_SOFT};
    border-color: {GOLD_DEEP};
    font-weight: 600;
}}

QDateEdit {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 5px 9px;
    color: {TEXT};
    selection-background-color: {SELECTION_BG};
    selection-color: {SELECTION_TEXT};
}}
QDateEdit:focus {{ border-color: {GOLD}; }}
QDateEdit::drop-down {{ border: none; width: 22px; }}
QCalendarWidget QWidget {{ background: {BG}; color: {TEXT}; }}
QCalendarWidget QAbstractItemView {{
    background: {BG};
    selection-background-color: {SELECTION_BG};
    selection-color: {SELECTION_TEXT};
    outline: 0;
}}
QCalendarWidget QToolButton {{
    background: transparent;
    color: {TEXT};
    border-radius: 6px;
    padding: 4px 8px;
}}
QCalendarWidget QToolButton:hover {{ background: {GOLD_HOVER}; }}

QPushButton#providerCard {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
    text-align: left;
}}
QPushButton#providerCard:hover {{ border-color: {GOLD_DEEP}; }}
QPushButton#providerCard:checked {{
    background: {GOLD_SOFT};
    border: 2px solid {GOLD_DEEP};
}}

QSplitter::handle {{ background: {BORDER}; width: 1px; }}

QTreeWidget {{
    background: {PANEL};
    border: none;
    padding: 6px 4px;
    outline: 0;
    show-decoration-selected: 0;
}}
QTreeWidget::branch {{ background: transparent; }}
QTreeWidget::branch:selected {{ background: transparent; }}
QTreeWidget::branch:has-children:closed {{ image: url({arrow_r}); }}
QTreeWidget::branch:has-children:open {{ image: url({arrow_d}); }}
QTreeWidget::item {{
    padding: 7px 10px;
    border-radius: 8px;
    margin: 1px 4px;
}}
QTreeWidget::item:hover {{ background: {PANEL_DARK}; }}
QTreeWidget::item:selected {{ background: {GOLD_SOFT}; color: {TEXT}; }}
QTreeWidget::item:disabled {{ background: transparent; }}

QListWidget {{
    background: {BG};
    border: none;
    outline: 0;
}}

QMenu {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 18px;
    border-radius: 6px;
}}
QMenu::item:selected {{ background: {GOLD_SOFT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

QStatusBar {{
    background: {PANEL};
    border-top: 1px solid {BORDER};
    color: {TEXT_DIM};
    font-size: 11px;
}}
/* Footer taskbar: one chip per open compose window. Chrome recedes here —
   neutral shell, state carried by text weight and icon tint, never by a
   coloured border. The accent appears only on the live composer's icon. */
QFrame#composerChip, QFrame#composerChipMin {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    min-height: 24px;
}}
QFrame#composerChipMin {{ background: {PANEL}; }}
QFrame#composerChip:hover, QFrame#composerChipMin:hover {{
    background: {GOLD_HOVER};
    border-color: {PANEL_DARK};
}}
QPushButton#composerChipLabel {{
    background: transparent;
    border: none;
    padding: 2px 0px;
    color: {TEXT};
    font-size: 12px;
    font-weight: 500;
    text-align: left;
}}
QPushButton#composerChipLabelMin {{
    background: transparent;
    border: none;
    padding: 2px 0px;
    color: {TEXT_DIM};
    font-size: 12px;
    font-weight: 400;
    text-align: left;
}}
QPushButton#composerChipClose {{
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 0px;
}}
QPushButton#composerChipClose:hover {{ background: {PANEL_DARK}; }}

/* Attachment preview: the document is the hero, so the frame is a hairline
   and the header stays quiet. */
QLabel#previewName {{
    color: {TEXT};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#previewMeta {{
    color: {TEXT_DIM};
    font-size: 11px;
}}
QFrame#previewFrame {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
/* The frame is the only border in this dialog; children never draw their own. */
QFrame#previewFrame > QAbstractScrollArea {{
    border: none;
    background: transparent;
}}
QFrame#previewRule {{
    background: {BORDER};
    border: none;
}}

QLineEdit, QComboBox {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 9px;
    selection-background-color: {SELECTION_BG};
    selection-color: {SELECTION_TEXT};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {GOLD}; }}
/* Editors (signature / compose body) — clear, Outlook-style text selection. */
QTextEdit, QPlainTextEdit {{
    selection-background-color: {SELECTION_BG};
    selection-color: {SELECTION_TEXT};
}}
QLineEdit#searchBox {{
    background: {PANEL};
    border: 1px solid transparent;
    border-radius: 16px;
    padding: 6px 14px;
}}
QLineEdit#searchBox:focus {{
    background: {BG};
    border-color: {GOLD};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {SELECTION_BG};
    selection-color: {SELECTION_TEXT};
    outline: 0;
}}

QPlainTextEdit, QTextEdit {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 9px;
    selection-background-color: {SELECTION_BG};
    selection-color: {SELECTION_TEXT};
}}
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {GOLD}; }}

QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 7px 14px;
    border: none;
    color: {TEXT_DIM};
}}
QTabBar::tab:selected {{
    color: {GOLD_DEEP};
    font-weight: 600;
    border-bottom: 2px solid {GOLD};
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {PANEL_DARK};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

QWidget#editorBar {{
    background: {PANEL};
    border-bottom: 1px solid {BORDER};
}}
#editorBar QPushButton {{ padding: 2px 7px; min-width: 16px; }}
#editorBar QPushButton:checked {{
    background: {GOLD_SOFT};
    border-color: {GOLD};
}}

QWidget#readerBar {{
    background: {PANEL};
    border-bottom: 1px solid {BORDER};
}}
#readerBar QPushButton {{ padding: 4px 10px; }}

QFrame#fileChip {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#fileChip:hover {{ border-color: {GOLD}; }}
QPushButton#chipClose {{
    border: none;
    background: transparent;
    border-radius: 9px;
    padding: 0;
}}
QPushButton#chipClose:hover {{ background: {PANEL_DARK}; }}

QFrame#notifCard {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#notifCard:hover {{ border-color: {GOLD}; }}
QWidget#notifHeader {{
    background: {PANEL};
    border-bottom: 1px solid {BORDER};
}}
QPushButton#notifClear {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 3px 10px;
    color: {TEXT_DIM};
}}
QPushButton#notifClear:hover {{ background: {GOLD_HOVER}; color: {GOLD_DEEP}; }}

QLabel#appTitle {{
    font-size: 14px;
    font-weight: 700;
    color: {GOLD_DEEP};
    padding: 0 6px;
}}
QLabel#guide {{ color: {TEXT_DIM}; font-size: 12px; }}
/* Quoted conversation shown under a reply — clearly "not your text". */
QTextBrowser#trailView {{
    background: {PANEL_DARK};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px;
    color: {TEXT_DIM};
}}
"""


# Import side effect: globals are right before any window builds.
apply_theme(current_theme())
