"""Find the minimap and the player's yellow dot in a screenshot (numpy RGB array).

Layout of the expanded minimap window (from the game UI):
    title bar ("MINI MAP"), header (map emblem + street/map name),
    a light neutral-grey band, the map picture, a white->grey band.
The two neutral bands share the same x-extent, which is how the map picture is found
at any resolution or window position.
"""
import time
from dataclasses import dataclass

import numpy as np
import gamepath

# find_dot: the player icon is matched where yellow pixels are; real dots differ from it by ~4-9 (mean
# colour difference over its solid pixels), yellow scenery by 33+.
DOT_SPRITE = (str(gamepath.DATA / 'Map' / '_Canvas' / '_Canvas_000.wz'), 'MapHelper.img')
DOT_MAX_ERROR = 20     # a closer match than this is the player's dot
DOT_SEARCH = 6         # px searched around each yellow spot
DOT_CANDIDATES = 8     # yellow spots tried (biggest first)
FULL_SEARCH_EVERY = 1.0  # seconds between whole-picture searches while tracking a scrolling minimap
SWITCH_MARGIN = 0.05     # how much better a whole-picture match must score to replace the tracked one


@dataclass
class Well:
    """Area between the two grey bands that holds the minimap picture (screen-array coords)."""
    x0: int
    x1: int
    y0: int
    y1: int
    band: int  # thickness of the bottom band, a proxy for UI scale

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0


def _bands(arr, min_len):
    """Horizontal runs of light neutral-grey pixels, merged across consecutive rows."""
    a = arr.astype(np.int16)
    hi, lo = a.max(axis=2), a.min(axis=2)
    mask = ((hi - lo) <= 6) & (lo >= 0xC0)
    bands, open_ = [], []
    for y in range(mask.shape[0]):
        row = mask[y]
        if row.sum() < min_len:
            runs = []
        else:
            d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
            starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
            keep = (ends - starts) >= min_len
            runs = list(zip(starts[keep].tolist(), ends[keep].tolist()))
        nxt = []
        for x0, x1 in runs:
            for b in open_:
                if abs(b['x0'] - x0) <= 4 and abs(b['x1'] - x1) <= 4 and b['y1'] == y:
                    b['y1'] = y + 1
                    nxt.append(b)
                    break
            else:
                nb = {'x0': x0, 'x1': x1, 'y0': y, 'y1': y + 1}
                bands.append(nb)
                nxt.append(nb)
        open_ = nxt
    return [b for b in bands if b['y1'] - b['y0'] >= 3]


def find_wells(arr):
    """Candidate minimap wells: pairs of grey bands with matching x-extent, top to bottom."""
    h, w = arr.shape[:2]
    # The minimap is at least ~0.23 x screen height wide at any aspect ratio (min window width).
    bands = _bands(arr, min_len=int(h * 0.15))
    wells = []
    for a in bands:
        # The map picture is several band-thicknesses tall; this rejects thin strips such as
        # the gap between the minimap's bottom band and the window's lower edge.
        min_gap = 4 * max(a['y1'] - a['y0'], 8)
        below = [b for b in bands if b['y0'] > a['y1'] and abs(b['x0'] - a['x0']) <= 6
                 and abs(b['x1'] - a['x1']) <= 6 and b['y0'] - a['y1'] >= max(min_gap, h * 0.03)]
        if below:
            b = min(below, key=lambda b: b['y0'])
            wells.append(Well(max(a['x0'], b['x0']), min(a['x1'], b['x1']), a['y1'], b['y0'],
                              b['y1'] - b['y0']))
    return wells


def find_dot(arr, well, scale=None, hint=None):
    """Centre of the player's yellow minimap dot inside `well`, or None. scale: the UI scale (window
    height / 768); estimated from the well's frame when not given.

    The dot is the game's own icon (Map/_Canvas MapHelper.img/minimap/user: a yellow disc in a dark
    ring with a white glow). Yellow pixels only suggest where to look; the icon itself is then matched
    there, comparing just its solid pixels, so yellow scenery next to it (mushroom-house roofs, the
    gold dome in Henesys Market) neither hides it nor passes for it. hint: where the dot was last time
    (same coordinates as the result); it's checked first, which is all most frames need."""
    scale = scale or well.band * 0.1875  # the frame band is ~15 px when the UI is 2.8125x
    rgb, solid, (cxo, cyo) = _dot_template(round(scale, 3))
    W = rgb.shape[0]
    sub = arr[well.y0:well.y1, well.x0:well.x1]
    a = sub.astype(np.int16)
    mask = (a[..., 0] >= 200) & (a[..., 1] >= 170) & (a[..., 2] <= 90) & (a[..., 0] - a[..., 2] >= 120)
    ys, xs = np.nonzero(mask)
    if len(xs) < 4:
        return None
    cell = max(4, int(W * 0.45))
    keys = (ys // cell) * 100000 + (xs // cell)
    uniq, counts = np.unique(keys, return_counts=True)
    spots = []
    if hint is not None:
        spots.append((hint[0] - well.x0, hint[1] - well.y0))
    for key in uniq[np.argsort(-counts)][:DOT_CANDIDATES]:
        cy, cx = (key // 100000) * cell + cell // 2, (key % 100000) * cell + cell // 2
        near = (abs(ys - cy) <= cell) & (abs(xs - cx) <= cell)
        spots.append((float(xs[near].mean()), float(ys[near].mean())))
    r = DOT_SEARCH
    best, tried = None, []
    for mx, my in spots:
        if best is not None and best[0] <= DOT_MAX_ERROR:
            break  # found it (usually at the hint, where it was last frame)
        if any(abs(mx - tx) <= r and abs(my - ty) <= r for tx, ty in tried):
            continue  # already searched around here
        tried.append((mx, my))
        # every placement of the icon within +-r px of the yellow's middle, compared at once
        x0, y0 = int(round(mx - cxo)) - r, int(round(my - cyo)) - r
        area = sub[max(0, y0):y0 + W + 2 * r, max(0, x0):x0 + W + 2 * r].astype(np.float32)
        if area.shape[0] < W or area.shape[1] < W:
            continue
        views = np.lib.stride_tricks.sliding_window_view(area, (W, W, 3))[:, :, 0]
        err = np.abs(views - rgb).mean(axis=-1)[..., solid].mean(axis=-1)  # per placement
        iy, ix = np.unravel_index(np.argmin(err), err.shape)
        e = float(err[iy, ix])
        if best is None or e < best[0]:
            best = (e, max(0, x0) + ix + cxo, max(0, y0) + iy + cyo)
    if best is None or best[0] > DOT_MAX_ERROR:
        return None
    return best[1] + well.x0, best[2] + well.y0


_dot_templates = {}


def _dot_template(scale):
    """The player icon as the game draws it at this UI scale: (RGB, solid-pixel mask, yellow disc centre)."""
    if scale not in _dot_templates:
        from PIL import Image
        import canvas
        from wzreader import WzFile
        global _dot_sprite
        if _dot_sprite is None:
            wz = WzFile(DOT_SPRITE[0])
            _dot_sprite = canvas.decode(wz, wz.read_image(dict(wz.images())[DOT_SPRITE[1]])['minimap']['user']['_data'])
            wz.close()
        h, w = _dot_sprite.shape[:2]
        size = (max(4, int(round(w * scale))), max(4, int(round(h * scale))))
        t = np.asarray(Image.fromarray(_dot_sprite).resize(size, Image.BILINEAR)).astype(np.float32)
        solid = t[..., 3] > 200
        yellow = solid & (t[..., 0] > 200) & (t[..., 1] > 170) & (t[..., 2] < 90)
        ys, xs = np.nonzero(yellow)
        _dot_templates[scale] = (t[..., :3], solid, (float(xs.mean()), float(ys.mean())))
    return _dot_templates[scale]


_dot_sprite = None


def _blobs(mask, well, min_px=12):
    """Centres of marker-sized blobs in a well-local mask, in screen-array coords."""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return []
    cell = max(6, well.band * 2)
    keys = (ys // cell) * 100000 + (xs // cell)
    uniq, counts = np.unique(keys, return_counts=True)
    found = []
    for k, c in zip(uniq.tolist(), counts.tolist()):
        if c < min_px:
            continue
        cy, cx = (k // 100000) * cell + cell / 2, (k % 100000) * cell + cell / 2
        near = (abs(ys - cy) <= cell) & (abs(xs - cx) <= cell)
        found.append(((xs[near].min() + xs[near].max()) / 2, (ys[near].min() + ys[near].max()) / 2, int(near.sum())))
    merged = []
    for x, y, _ in sorted(found, key=lambda f: -f[2]):
        if all((x - mx) ** 2 + (y - my) ** 2 > (cell * 0.8) ** 2 for mx, my in merged):
            merged.append((x, y))
    return [(x + well.x0, y + well.y0) for x, y in merged]


def find_markers(arr, well):
    """Portal rings (blue) and NPC markers (green) visible in the minimap."""
    sub = arr[well.y0:well.y1, well.x0:well.x1].astype(np.int16)
    r, g, b = sub[..., 0], sub[..., 1], sub[..., 2]
    ring = (b - r > 90) & (g - r > 60) & (b - g > 15) & (r < 80)
    npc = (g > 170) & (r < 60) & (b < 60)
    return {'portal': _blobs(ring, well), 'npc': _blobs(npc, well)}


def place_picture(well, pic_w, pic_h, dot, dot_above_feet, markers, anchors):
    """Screen position of the map picture's top-left corner, or None if it can't be pinned down.

    The minimap draws the map at a fixed scale. If the picture fits the well it is centred;
    otherwise the view scrolls to keep the character's feet centred, stopping at the picture's
    edges. When it's mid-scroll, portal/NPC markers are matched to their known positions
    (`anchors`, picture coords) to find the offset.
    """
    origin = []
    for axis in (0, 1):
        lo, size, pic = (well.x0, well.w, pic_w) if axis == 0 else (well.y0, well.h, pic_h)
        if pic <= size + 3:
            origin.append(lo + (size - pic) / 2)
            continue
        centred = lo + size / 2 - (dot_above_feet if axis == 1 else 0)
        d = dot[axis] if dot else None
        if d is not None and d < centred - 5:
            origin.append(lo)                   # scrolled to the start
        elif d is not None and d > centred + 5:
            origin.append(lo + size - pic)      # scrolled to the end
        else:
            origin.append(None)
    if None not in origin:
        return tuple(origin)
    # Offsets a mid-scroll view can have on each unresolved axis.
    ranges = [(well.x0 + well.w - pic_w, well.x0), (well.y0 + well.h - pic_h, well.y0)]
    votes = []
    for kind in ('portal', 'npc'):
        for sx, sy in markers.get(kind, []):
            for ax, ay in anchors.get(kind, []):
                o = (sx - ax, sy - ay)
                if all(abs(o[i] - origin[i]) <= 6 if origin[i] is not None
                       else ranges[i][0] - 6 <= o[i] <= ranges[i][1] + 6 for i in (0, 1)):
                    votes.append(o)
    if not votes:
        return None
    support = max(([w for w in votes if abs(w[0] - v[0]) <= 4 and abs(w[1] - v[1]) <= 4] for v in votes), key=len)
    if len(support) < 2 and len(votes) > 1:
        return None  # ambiguous: one marker could be any of several portals
    return tuple(origin[i] if origin[i] is not None else float(np.mean([w[i] for w in support])) for i in (0, 1))


def header_rect(well):
    """Region above the well holding the street/map name (for OCR).

    Skips the map emblem on the left and the "MINI MAP" title bar above, both of which
    produce junk text. Sizes are in units of the grey band's thickness so they scale with the UI.
    """
    top = max(0, int(well.y0 - well.band * 9.5))
    return well.x0 + well.band * 8, top, well.x1, max(top + 1, well.y0 - well.band * 2)


def _lum(rgb):
    return rgb[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)


class PictureMatcher:
    """Find where a scrolling minimap's picture sits by matching what's visible against the map's
    own minimap image (from the game files). Works anywhere on the map, with or without portal/NPC
    markers in view; markers and other players' dots on top are just noise to the match."""

    MIN_SCORE = 0.25

    def __init__(self, rgba, pic_w, pic_h):
        from PIL import Image
        big = np.asarray(Image.fromarray(rgba).resize((pic_w, pic_h), Image.BILINEAR))
        gray, mask = _lum(big), big[..., 3] > 200  # only the image's solid parts take part
        self.levels = {k: (gray[::k, ::k], mask[::k, ::k]) for k in (1, 2, 4)}
        self.prev = None  # last (ox, oy), to search near it next time
        self.last_full = 0.0  # when the whole picture was last searched

    def _score(self, view, k, ox, oy):
        """Masked normalised cross-correlation for picture origin (ox, oy) relative to the view's corner."""
        gray, mask = self.levels[k]
        x0, y0 = -int(round(ox / k)), -int(round(oy / k))
        vh, vw = view.shape
        sy0, sx0 = max(0, y0), max(0, x0)
        sy1, sx1 = min(gray.shape[0], y0 + vh), min(gray.shape[1], x0 + vw)
        if sy1 - sy0 < 4 or sx1 - sx0 < 4:
            return -1.0
        m = mask[sy0:sy1, sx0:sx1]
        if m.sum() < 50:
            return -1.0
        a = gray[sy0:sy1, sx0:sx1][m]
        b = view[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0][m]
        a, b = a - a.mean(), b - b.mean()
        return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-6))

    def _search(self, view_full, k, xs, ys):
        view = view_full[::k, ::k]
        return max((self._score(view, k, x, y), x, y) for x in xs for y in ys)

    def locate(self, view_rgb, xr, yr):
        """Picture origin relative to the view's top-left corner, within xr/yr = (lo, hi) ranges
        (lo == hi for an axis that doesn't scroll). Returns (ox, oy) or None."""
        view = _lum(view_rgb)
        clamp = lambda v, r: min(max(v, r[0]), r[1])
        refine = lambda x, y: self._search(view, 1, sorted({clamp(x + d, xr) for d in range(-3, 4)}),
                                           sorted({clamp(y + d, yr) for d in range(-3, 4)}))
        tracked = None
        if self.prev:  # track: small window around the last position at half resolution
            px, py = self.prev
            xs = sorted({clamp(px + d, xr) for d in range(-10, 11, 2)})
            ys = sorted({clamp(py + d, yr) for d in range(-10, 11, 2)})
            tracked = refine(*self._search(view, 2, xs, ys)[1:])
        now = time.monotonic()
        if tracked is None or now - self.last_full > FULL_SEARCH_EVERY:
            # Look over the whole picture too (always at first, then about once a second): the view
            # jumps when a teleporter moves you, and tracking alone would stay latched onto a
            # similar-looking stretch (repeating ground tiles) near the old spot.
            self.last_full = now
            xs = list(range(int(xr[0]), int(xr[1]) + 1, 4)) or [xr[0]]
            ys = list(range(int(yr[0]), int(yr[1]) + 1, 4)) or [yr[0]]
            found = refine(*self._search(view, 4, xs, ys)[1:])
            if tracked is None or found[0] > tracked[0] + SWITCH_MARGIN:
                tracked = found
        score, x, y = tracked
        if score < self.MIN_SCORE:
            self.prev = None
            return None
        self.prev = (x, y)
        return x, y
