"""Holiday flourishes drawn around the This Week calendar frame.

The theme follows whatever holiday the countdown shows. Art is pure black
and white line work, since gray drops out on the E1001's e-ink.
"""
import math

from markupsafe import Markup

# Calendar frame (matches .grid in _style.css): outer edge of the 3px border.
GRID_L, GRID_T, GRID_R, GRID_B = 20, 108, 780, 403


# ---- drawing helpers -------------------------------------------------------

def _f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _edge(points, pull):
    """Scalloped edge through spine points: each gap curves in toward the midrib."""
    d = ""
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        d += f" Q{_f((x0 + x1) / 2)},{_f((y0 + y1) / 2 * pull)} {_f(x1)},{_f(y1)}"
    return d


def _holly_leaf(length: float, width: float) -> tuple[str, str]:
    """(outline, shaded upper half) paths for a holly leaf along +x, stem at 0,0."""
    ts = [0.0, 0.16, 0.36, 0.56, 0.76, 1.0]       # spine points, stem → tip
    def half(t):
        return width / 2 * (math.sin(math.pi * min(max(t, 0.05), 0.95)) ** 0.75)
    top = [(t * length, -half(t)) for t in ts]
    bot = [(t * length, half(t)) for t in reversed(ts)]
    top[0] = bot[-1] = (0.0, 0.0)
    pull = 0.3
    outline = "M0,0" + _edge(top, pull) + _edge(bot, pull) + " Z"
    shade = "M0,0" + _edge(top, pull) + " L0,0 Z"
    return outline, shade


def _leaf(x, y, angle, length=34, width=17) -> str:
    outline, shade = _holly_leaf(length, width)
    return (f'<g transform="translate({_f(x)},{_f(y)}) rotate({_f(angle)})">'
            f'<path d="{outline}" fill="#fff"/>'
            f'<path d="{shade}" fill="#000"/>'
            f'<path d="{outline}" fill="none" stroke="#000" stroke-width="1.8" stroke-linejoin="round"/>'
            f'<path d="M0,0 L{_f(length * 0.97)},0" stroke="#000" stroke-width="1.6" stroke-linecap="round"/>'
            f'</g>')


def _berry(x, y, r) -> str:
    return (f'<circle cx="{_f(x)}" cy="{_f(y)}" r="{_f(r)}" fill="#000" stroke="#fff" stroke-width="1.3"/>'
            f'<circle cx="{_f(x - r * 0.38)}" cy="{_f(y - r * 0.38)}" r="{_f(r * 0.28)}" fill="#fff"/>')


def _sprig(x, y, h_in, v_in, scale=1.0, h_tilt=11, out_turn=0, nudge=(0, 0)) -> str:
    """A holly sprig on a frame corner, in an engraved style.

    h_in / v_in: which way the borders run from the corner into the frame
    (h_in +1 = right, -1 = left; v_in +1 = down, -1 = up). A leaf lies along
    each border, tilted outward so it stays off the calendar; a third points
    out from the corner, and a cluster of berries sits on top.
    h_tilt: outward tilt of the leaf along the horizontal border (negative
    tilts it in, over the frame, when something sits just outside).
    out_turn: degrees to rotate the outward leaf (positive = clockwise).
    nudge: (dx, dy) shift for the berries.
    """
    L, W, tilt = 36 * scale, 18 * scale, 11
    along_h = (0 - h_tilt * v_in) if h_in > 0 else (180 + h_tilt * v_in)
    along_v = (90 + tilt * h_in) if v_in > 0 else (-90 - tilt * h_in)
    out = math.degrees(math.atan2(-v_in, -h_in))
    out += out_turn
    r = 4.3 * scale
    ox, oy = -h_in * 2 * scale + nudge[0], -v_in * 2 * scale + nudge[1]
    berries = (_berry(x + ox - h_in * 4 * scale, y + oy + v_in * 1 * scale, r) +
               _berry(x + ox + h_in * 1 * scale, y + oy - v_in * 4 * scale, r) +
               _berry(x + ox + h_in * 3.5 * scale, y + oy + v_in * 3.5 * scale, r))
    return (_leaf(x, y, out, L * 0.7, W * 0.85) +
            _leaf(x, y, along_h, L, W) +
            _leaf(x, y, along_v, L, W) + berries)


def _tree(h=46) -> tuple[str, float, float]:
    """A small engraved fir: three scalloped tiers, garland, ornaments, star.

    Returns (svg body, width, height); the body has a white halo so frame lines
    behind it stop cleanly at its edge.
    """
    w = h * 0.92
    cx = w / 2

    def tier(top, bot, hw, tips):
        # apex → right bough tip, then a scalloped hem back to the left tip
        d = f"M{_f(cx)},{_f(top)} L{_f(cx + hw)},{_f(bot)}"
        hem = ""
        for i in range(tips):
            x0 = cx + hw - 2 * hw * i / tips
            x1 = cx + hw - 2 * hw * (i + 1) / tips
            hem += f" Q{_f((x0 + x1) / 2)},{_f(bot - (bot - top) * 0.2)} {_f(x1)},{_f(bot)}"
        return d + hem + " Z", f"M{_f(cx + hw)},{_f(bot)}" + hem

    out = ""
    halo = ""
    spec = [(h * 0.47, h * 0.86, w * 0.5, 4),   # bottom tier first
            (h * 0.30, h * 0.66, w * 0.38, 3),
            (h * 0.13, h * 0.45, w * 0.26, 2)]
    out += (f'<rect x="{_f(cx - w * 0.07)}" y="{_f(h * 0.82)}" width="{_f(w * 0.14)}" '
            f'height="{_f(h * 0.12)}" fill="#000"/>'
            f'<rect x="{_f(cx - w * 0.2)}" y="{_f(h * 0.93)}" width="{_f(w * 0.4)}" '
            f'height="{_f(h * 0.07)}" fill="#000"/>')
    for top, bot, hw, tips in spec:
        shape, hem = tier(top, bot, hw, tips)
        halo += f'<path d="{shape}" fill="#fff" stroke="#fff" stroke-width="5" stroke-linejoin="round"/>'
        out += (f'<path d="{shape}" fill="#000"/>'
                f'<path d="{hem}" fill="none" stroke="#fff" stroke-width="1.2"/>')
    for y0, y1, x0, x1, sag in [(0.37, 0.33, -0.14, 0.16, 0.08), (0.58, 0.52, -0.25, 0.27, 0.09),
                                (0.79, 0.72, -0.37, 0.4, 0.1)]:
        out += (f'<path d="M{_f(cx + x0 * w)},{_f(y0 * h)} Q{_f(cx)},{_f((max(y0, y1) + sag) * h)} '
                f'{_f(cx + x1 * w)},{_f(y1 * h)}" fill="none" stroke="#fff" stroke-width="1.2" '
                f'stroke-dasharray="1.5 1.6" stroke-linecap="round"/>')
    out += "".join(f'<circle cx="{_f(cx + dx * w)}" cy="{_f(dy * h)}" r="{_f(h * 0.032)}" fill="#fff"/>'
                   for dx, dy in [(0.05, 0.27), (-0.08, 0.5), (0.17, 0.45), (-0.24, 0.68),
                                  (0.06, 0.68), (0.3, 0.64), (-0.12, 0.84), (0.2, 0.83)])
    star_r, star_cy = h * 0.1, h * 0.1
    pts = []
    for i in range(10):
        a = math.radians(-90 + i * 36)
        rr = star_r if i % 2 == 0 else star_r * 0.42
        pts.append(f"{_f(cx + rr * math.cos(a))},{_f(star_cy + rr * math.sin(a))}")
    out += f'<polygon points="{" ".join(pts)}" fill="#000" stroke="#fff" stroke-width="0.8" stroke-linejoin="round"/>'
    halo += (f'<rect x="{_f(cx - w * 0.2 - 2.5)}" y="{_f(h * 0.82 - 2.5)}" width="{_f(w * 0.4 + 5)}" '
             f'height="{_f(h * 0.18 + 5)}" fill="#fff"/>'
             f'<circle cx="{_f(cx)}" cy="{_f(star_cy)}" r="{_f(star_r + 2.5)}" fill="#fff"/>')
    return halo + out, w, h


def _christmas_frame() -> str:
    # Holly on the bottom-left corner. The REMINDERS box sits just below, so
    # the leaf along the bottom border tilts up over the frame instead of out.
    return _sprig(GRID_L - 1, GRID_B + 1, +1, -1, h_tilt=-9, out_turn=18, nudge=(0, -3))


def _christmas_badge() -> str:
    tree, w, h = _tree(46)
    return (f'<svg class="flourish-badge" width="{_f(w)}" height="{_f(h)}" '
            f'viewBox="0 0 {_f(w)} {_f(h)}" xmlns="http://www.w3.org/2000/svg">{tree}</svg>')


THEMES = {
    # theme: (art on the calendar frame, art beside the countdown)
    "christmas": (_christmas_frame, _christmas_badge),
}


def theme_for(name: str) -> str | None:
    n = name.lower()
    if "christmas" in n:
        return "christmas"
    return None


def art(theme: str | None) -> dict:
    """{'frame': overlay for the calendar frame, 'badge': art beside the countdown}"""
    if not theme or theme not in THEMES:
        return {"frame": Markup(""), "badge": Markup("")}
    frame, badge = THEMES[theme]
    return {
        "frame": Markup('<svg class="flourish" width="800" height="480" viewBox="0 0 800 480" '
                        f'xmlns="http://www.w3.org/2000/svg">{frame()}</svg>'),
        "badge": Markup(badge()),
    }
