"""Find the player's character on screen by its name tag.

The game camera trails the character (up to ~120 map units while walking), so the minimap
alone can't say where the character is drawn. The name tag sits just under the feet:
OCR finds it once, then a template match follows it every frame.
"""
import difflib
import time

import numpy as np

TAG_GAP = 7          # map units from the feet down to the top of the name text
MATCH_MIN = 0.7      # normalised cross-correlation needed to accept a template match
RETRY_OCR = 1.0      # seconds between OCR attempts while the tag is lost
DOWN = 2             # template matching runs at 1/DOWN resolution
PAD = 4              # pixels of tag background kept around the text in the template
FLAT = 0.15          # a patch with under this fraction of the template's contrast counts as flat


def _gray(rgb):
    return rgb[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)


def _box_sum(a, h, w):
    c = np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return c[h:, w:] - c[:-h, w:] - c[h:, :-w] + c[:-h, :-w]


def match(window, tmpl):
    """Best normalised cross-correlation of tmpl inside window: (score, x, y)."""
    H, W = window.shape
    h, w = tmpl.shape
    if H < h or W < w:
        return -1.0, 0, 0
    window = window.astype(np.float64)  # float32 running sums lose the variance of smooth patches
    t = tmpl.astype(np.float64) - tmpl.mean()
    tn = np.sqrt((t * t).sum()) or 1.0
    shape = (H + h, W + w)
    corr = np.fft.irfft2(np.fft.rfft2(window, shape) * np.fft.rfft2(t[::-1, ::-1], shape), shape)
    corr = corr[h - 1:H, w - 1:W]
    s1 = _box_sum(window, h, w)
    s2 = _box_sum(window * window, h, w)
    var = s2 - s1 * s1 / (h * w)
    # Nearly flat patches (far less contrast than the template) can't be the tag, and dividing by their
    # tiny variance would blow the score up past 1 and win the match.
    flat = var < (FLAT * tn) ** 2
    ncc = np.clip(np.where(flat, 0.0, corr / (np.sqrt(np.maximum(var, 1e-9)) * tn)), -1.0, 1.0)
    y, x = np.unravel_index(np.argmax(ncc), ncc.shape)
    return float(ncc[y, x]), int(x), int(y)


class TagTracker:
    def __init__(self, ocr):
        self.ocr = ocr
        self.candidates = set()   # words from the status bar; the player's name is one of them
        self.name = None
        self.tmpl = None          # grayscale template at 1/DOWN scale
        self.box = None           # (x, y, w, h) of the tag text in the game window, full res
        self.last_ocr = 0.0
        self.last_status = 0.0
        self.sw = 2.8

    def reset(self):
        """New map: the camera jumps, so search afresh around the prediction."""
        self.box = None

    def _read_status_bar(self, grab, gw, gh):
        # Bottom-left status bar shows level, job and character name.
        x0, y0 = 0, int(gh * 0.90)
        img = grab(x0, y0, int(gw * 0.35), gh - y0)
        self.candidates = {w[0] for w in self.ocr.words(img) if len(w[0]) >= 3 and w[0].isalnum()}

    def locate(self, grab, pred, sw, gw, gh):
        """Feet position (x, y) in the game window, or None. grab(x, y, w, h) -> RGB array."""
        now = time.time()
        self.sw = sw
        if self.tmpl is not None:
            centers = ([(self.box[0], self.box[1])] if self.box else []) + [
                (pred[0] - self.tmpl.shape[1] * DOWN / 2, pred[1] + TAG_GAP * sw)]
            for cx, cy in centers:
                found = self._search(grab, cx, cy, 90 * sw, 70 * sw, gw, gh)
                if found:
                    return found
        if now - self.last_ocr < RETRY_OCR:
            return None
        self.last_ocr = now
        if not self.candidates and not self.name and now - self.last_status > 5:
            self.last_status = now
            self._read_status_bar(grab, gw, gh)
        return self._acquire(grab, pred, sw, gw, gh)

    def _search(self, grab, x, y, rx, ry, gw, gh):
        th, tw = self.tmpl.shape[0] * DOWN, self.tmpl.shape[1] * DOWN
        x0, y0 = int(max(0, x - rx)), int(max(0, y - ry))
        x1, y1 = int(min(gw, x + tw + rx)), int(min(gh, y + th + ry))
        if x1 - x0 < tw or y1 - y0 < th:
            return None
        win = _gray(grab(x0, y0, x1 - x0, y1 - y0))[::DOWN, ::DOWN]
        score, mx, my = match(win, self.tmpl)
        if score < MATCH_MIN:
            return None
        bx, by = x0 + mx * DOWN, y0 + my * DOWN
        self.box = (bx, by, tw, th)
        return self._feet()

    def _acquire(self, grab, pred, sw, gw, gh):
        x0, x1 = int(max(0, pred[0] - 170 * sw)), int(min(gw, pred[0] + 170 * sw))
        y0, y1 = int(max(0, pred[1] - 90 * sw)), int(min(gh, pred[1] + 140 * sw))
        if x1 - x0 < 20 or y1 - y0 < 20:
            return None
        img = grab(x0, y0, x1 - x0, y1 - y0)
        names = {self.name} if self.name else self.candidates
        best = None
        for text, x, y, w, h in self.ocr.words(img):
            score = max((difflib.SequenceMatcher(None, text.lower(), n.lower()).ratio() for n in names), default=0)
            if score < 0.8:
                continue
            d = (x0 + x + w / 2 - pred[0]) ** 2 + (y0 + y - pred[1]) ** 2
            if best is None or d < best[0]:
                best = (d, text, x, y, w, h)
        if best is None:
            return None
        _, text, x, y, w, h = best
        if not self.name:
            self.name = max(names, key=lambda n: difflib.SequenceMatcher(None, text.lower(), n.lower()).ratio())
            print(f'[tag] tracking name tag "{self.name}"')
        tx0, ty0 = int(max(0, x - PAD)), int(max(0, y - PAD))
        tx1, ty1 = int(min(img.shape[1], x + w + PAD)), int(min(img.shape[0], y + h + PAD))
        self.tmpl = _gray(img[ty0:ty1, tx0:tx1])[::DOWN, ::DOWN].copy()
        self.box = (x0 + tx0, y0 + ty0, tx1 - tx0, ty1 - ty0)
        return self._feet()

    def _feet(self):
        x, y, w, h = self.box
        return x + w / 2, y + PAD - TAG_GAP * self.sw

