"""Colour roles and ready-made setups for the Customize screen.

Colours are grouped by the box they belong to (a background and the text drawn on it), the way design systems name
colour roles. Every setup is checked against the WCAG AA contrast minimums (tests/test_themes.py): 4.5:1 for text
on its box, 3:1 for an input box's border.
"""

GROUPS = [
    ("Page", [("bg", "Background"), ("fg", "Text"), ("mut", "Faint text (hints, counts)"), ("line", "Borders & dividers")]),
    ("Header", [("head", "Background"), ("headfg", "Text & links")]),
    ("Cards, panels & popups", [("card", "Background")]),
    ("Info boxes", [("note", "Background"), ("notefg", "Text")]),
    ("Input boxes (text fields, dropdowns)", [("inbg", "Background"), ("infg", "Text"), ("inline", "Border")]),
    ("Main buttons", [("btn", "Background"), ("btnfg", "Text")]),
    ("Other buttons", [("ghostfg", "Text"), ("ghostline", "Border"), ("danger", "Delete buttons")]),
    ("Links & dates", [("acc", "Links & accent"), ("dposted", "Upload date"), ("dsaved", "Saved date")]),
]
ROLES = [k for _, fields in GROUPS for k, _ in fields]

# colour -> (the box it sits on, minimum ratio). Shown as a warning on the Customize screen.
PAIRS = {"fg": ("bg", 4.5), "mut": ("bg", 4.5), "headfg": ("head", 4.5), "notefg": ("note", 4.5),
         "infg": ("inbg", 4.5), "btnfg": ("btn", 4.5), "ghostfg": ("card", 4.5), "inline": ("card", 3),
         "acc": ("card", 4.5), "dposted": ("note", 4.5), "dsaved": ("note", 4.5)}
# also required of every setup (text that appears on more than one kind of box)
EXTRA_PAIRS = [("fg", "card", 4.5), ("mut", "card", 4.5), ("acc", "bg", 4.5), ("#ffffff", "danger", 4.5),
               ("ghostfg", "bg", 4.5)]

SETUPS = [
    ("Midnight blue", "Deep navy, blue header and buttons", {
        "bg": "#0a1224", "fg": "#e8edf7", "mut": "#9aa6bf", "line": "#2b3a5c", "head": "#1e3a8a", "headfg": "#ffffff",
        "card": "#111c33", "note": "#16234a", "notefg": "#e8edf7", "inbg": "#0c1630", "infg": "#e8edf7",
        "inline": "#6176a3", "btn": "#2563eb", "btnfg": "#ffffff", "ghostfg": "#dbe4f5", "ghostline": "#4a5c85",
        "danger": "#c92a2a", "acc": "#7cb4ff", "dposted": "#fdba74", "dsaved": "#5eead4"}),
    ("Graphite", "Neutral dark grey, calm teal", {
        "bg": "#151618", "fg": "#ececee", "mut": "#a3a6ad", "line": "#33363b", "head": "#1f2124", "headfg": "#f2f2f4",
        "card": "#1d1f22", "note": "#24292b", "notefg": "#ececee", "inbg": "#17191b", "infg": "#ececee",
        "inline": "#6b7078", "btn": "#0f766e", "btnfg": "#ffffff", "ghostfg": "#e2e3e6", "ghostline": "#555a61",
        "danger": "#c92a2a", "acc": "#5eead4", "dposted": "#fdba74", "dsaved": "#67e8f9"}),
    ("Forest", "Dark green, soft and natural", {
        "bg": "#0f1d17", "fg": "#e4f1e8", "mut": "#9fb8a8", "line": "#24412f", "head": "#173a2a", "headfg": "#effaf2",
        "card": "#16291f", "note": "#1d3a2b", "notefg": "#e4f1e8", "inbg": "#112219", "infg": "#e4f1e8",
        "inline": "#5f8a70", "btn": "#2f7d4a", "btnfg": "#ffffff", "ghostfg": "#d8eadf", "ghostline": "#4b725a",
        "danger": "#c92a2a", "acc": "#86efac", "dposted": "#fcd34d", "dsaved": "#7dd3fc"}),
    ("High contrast dark", "Black and white, yellow highlights", {
        "bg": "#000000", "fg": "#ffffff", "mut": "#d0d0d0", "line": "#8a8a8a", "head": "#000000", "headfg": "#ffffff",
        "card": "#0d0d0d", "note": "#1a1a1a", "notefg": "#ffffff", "inbg": "#000000", "infg": "#ffffff",
        "inline": "#bdbdbd", "btn": "#ffd400", "btnfg": "#000000", "ghostfg": "#ffffff", "ghostline": "#bdbdbd",
        "danger": "#b00020", "acc": "#ffd400", "dposted": "#ffb347", "dsaved": "#7fffd4"}),
    ("Daylight", "Clean white, blue accents", {
        "bg": "#f4f6fa", "fg": "#18202e", "mut": "#566074", "line": "#d5dbe6", "head": "#1d4ed8", "headfg": "#ffffff",
        "card": "#ffffff", "note": "#e8effc", "notefg": "#18202e", "inbg": "#ffffff", "infg": "#18202e",
        "inline": "#7c879c", "btn": "#1d4ed8", "btnfg": "#ffffff", "ghostfg": "#1f2a3d", "ghostline": "#9aa4b8",
        "danger": "#c92a2a", "acc": "#1d4ed8", "dposted": "#9a3d07", "dsaved": "#0f766e"}),
    ("Paper", "Warm cream, like a notebook", {
        "bg": "#f4ecd8", "fg": "#33291c", "mut": "#665742", "line": "#dccfae", "head": "#e9dfc4", "headfg": "#33291c",
        "card": "#fbf6e8", "note": "#efe4c8", "notefg": "#33291c", "inbg": "#fffdf6", "infg": "#33291c",
        "inline": "#8c7a5a", "btn": "#7a4f00", "btnfg": "#ffffff", "ghostfg": "#3d3122", "ghostline": "#a8956f",
        "danger": "#b42318", "acc": "#7a4f00", "dposted": "#9a3412", "dsaved": "#115e59"}),
    ("Rose", "Light pink, gentle", {
        "bg": "#fff1f4", "fg": "#3b1d28", "mut": "#6f4a57", "line": "#f3cdd8", "head": "#ffe0e8", "headfg": "#3b1d28",
        "card": "#ffffff", "note": "#ffe4ec", "notefg": "#3b1d28", "inbg": "#ffffff", "infg": "#3b1d28",
        "inline": "#a77486", "btn": "#be185d", "btnfg": "#ffffff", "ghostfg": "#4a2433", "ghostline": "#d29aae",
        "danger": "#b42318", "acc": "#be185d", "dposted": "#9a3412", "dsaved": "#0f766e"}),
    ("High contrast light", "White and black, strong blue", {
        "bg": "#ffffff", "fg": "#000000", "mut": "#3d3d3d", "line": "#767676", "head": "#ffffff", "headfg": "#000000",
        "card": "#ffffff", "note": "#f0f0f0", "notefg": "#000000", "inbg": "#ffffff", "infg": "#000000",
        "inline": "#595959", "btn": "#0b3d91", "btnfg": "#ffffff", "ghostfg": "#000000", "ghostline": "#595959",
        "danger": "#a50e0e", "acc": "#0b3d91", "dposted": "#8a3200", "dsaved": "#005f56"}),
]


# how strongly each setup fades the background picture into its page colour (0 = not at all)
FADE = {"Midnight blue": "0.55", "Graphite": "0.6", "Forest": "0.55", "High contrast dark": "0.8", "Daylight": "0.75",
        "Paper": "0.75", "Rose": "0.75", "High contrast light": "0.85"}


def _lum(h: str) -> float:
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    x, y = _lum(a), _lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def problems(colors: dict) -> list[str]:
    """Every pair in a setup that is below its minimum, as readable strings ([] = all fine)."""
    pick = lambda k: k if k.startswith("#") else colors[k]
    checks = [(k, o, need) for k, (o, need) in PAIRS.items()] + EXTRA_PAIRS
    return [f"{k} on {o}: {contrast(pick(k), pick(o)):.2f} < {need}" for k, o, need in checks
            if contrast(pick(k), pick(o)) < need]


def config() -> dict:
    """What the pages need (rendered into them as JSON)."""
    return {"groups": [[t, [[k, label] for k, label in f]] for t, f in GROUPS], "roles": ROLES,
            "pairs": {k: list(v) for k, v in PAIRS.items()},
            "setups": [{"name": n, "desc": d, "colors": c, "dim": FADE[n]} for n, d, c in SETUPS]}
