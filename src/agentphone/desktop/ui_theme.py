"""Design tokens for the AgentPhone desktop UI.

One dark, calm surface language taken from the approved mockup: near-black
canvas, three grey layers, hairline borders, one electric-blue accent, and
semantic status colours that appear only where they mean something.
"""

# ── surfaces ────────────────────────────────────────────────────────────
BG = "#0a0d13"            # window canvas
PANEL = "#11151f"         # header / footer / section panel
CARD = "#141926"          # raised card
CARD_HOVER = "#1a2130"
SIDE = "#0d1119"          # message well
BORDER = "#202636"
BORDER_STRONG = "#2c3648"
HAIR = "#1a202d"

# ── ink ─────────────────────────────────────────────────────────────────
TEXT = "#e6eaf2"
MUTED = "#8b93a5"
DIM = "#5b6373"

# ── accent & status ─────────────────────────────────────────────────────
ACCENT = "#4f9dff"
ACCENT_SOFT = "#16243a"
BLUE = "#2f6df6"
GREEN = "#35d07f"
GREEN_SOFT = "#10251b"
AMBER = "#f0b429"
AMBER_SOFT = "#2a2110"
RED = "#f87171"
RED_SOFT = "#2a1416"
BLUE_SOFT = "#142033"

# ── type ────────────────────────────────────────────────────────────────
FONT = "Microsoft YaHei UI"
FONT_TITLE = "Segoe UI"
MONO = "Cascadia Mono"


def font(size, weight="normal", family=FONT):
    return (family, size, weight)


# ── geometry ────────────────────────────────────────────────────────────
RADIUS = 12
PAD = 20
GAP = 12

# Legacy aliases kept so older imports keep resolving.
CHROME = PANEL
WHITE = CARD
INK = TEXT
