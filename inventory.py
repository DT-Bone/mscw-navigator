"""Read the inventory window: which tab is open, what's in each visible slot and how many.

Everything is placed from the game's own UI data (UI/Item.img/pos: a 5 x 6 grid of 32 px slots, 4 px
apart across and 2 px down, starting at (8, 50) in the window), scaled like the rest of the UI
(window height / 768). The window is found by the red line under its tabs (UI y 42-44, x 3-205).
Stack sizes are drawn with the game's ItemNo digit images, so they're matched exactly rather than
OCR'd; items are recognised by comparing each slot with the game's icons for the open tab.
"""
import threading
import time

import numpy as np
from PIL import Image

import items as item_index
import itemhover
from wzreader import WzFile

UI_DIR = item_index.DATA / 'UI'
VIEW_H = 768
RED_LINE = (3, 42, 203)       # x, y, length of the red line under the tabs (UI px)
TAB_X, TAB_W, TAB_Y, TAB_H = 5, 33, 24, 17   # tab buttons (UI px): Equip, Use, Set Up, Etc, Cash, Deco
TABS = ('Equip', 'Use', 'Set-up', 'Etc', 'Cash', 'Deco')
WINDOW = (209, 289)  # inventory window size (UI px)
CURSOR = 30          # size of the game's mouse cursor (UI px)
EMPTY = np.array([224, 222, 212], np.float32)  # an empty slot's colour
DIGIT_X, DIGIT_Y = -0.31, 19.5  # where a stack size's first digit image goes in its slot (UI px; measured)
DIGIT_SHIFTS = (-0.125, 0.0, 0.125)  # small shifts tried for the first digit (rounding varies a little)
READ_EVERY = 1.5             # seconds between looks at the inventory
ICON_MAX_ERROR = 40          # best icon match worse than this: the item isn't recognised (good ones ~13-23)
SHORTLIST = 8                # icons compared at full resolution after the quick comparison
DIGIT_MAX_ERROR = 50         # mean colour difference above which a position holds no digit (digits <= ~32, others >= ~72)


def _ui_data():
    """(grid settings, ItemNo digit images) from the game's UI files."""
    ui = WzFile(str(UI_DIR / 'UI_000.wz'))
    cv = WzFile(str(UI_DIR / '_Canvas' / '_Canvas_000.wz'))
    ui_imgs, cv_imgs = dict(ui.images()), dict(cv.images())
    pos = ui.read_image(ui_imgs['Item.img'])['pos']
    basic = ui.read_image(ui_imgs['Basic.img'])
    canvases = cv.read_image(cv_imgs['Basic.img'])
    import canvas
    digits = []
    for d in range(10):
        node = canvases['ItemNo'][str(d)]
        digits.append(canvas.decode(cv, node['_data']))
    ui.close()
    cv.close()
    grid = {'cols': pos['slot_col'], 'rows': pos['slot_row'], 'dx': 32 + pos['slot_space_x'],
            'dy': 32 + pos['slot_space_y'], 'x': pos['slot_pos']['x'], 'y': pos['slot_pos']['y']}
    return grid, digits


class Inventory:
    def __init__(self):
        self.grid, digits = _ui_data()
        # digit images: fill (the bright part) and where the digit is drawn at all
        self.digits = [(d[..., :3].astype(np.float32), d[..., 3] > 128) for d in digits]
        self.icon_ids, icons = item_index.load_icons()
        self.items, _ = item_index.load()
        self.icons = icons
        self.icon_rgb = icons[..., :3].astype(np.float32)
        self.icon_mask = icons[..., 3] > 160
        cats = np.array([self.items[int(i)]['cat'] for i in self.icon_ids])
        self.by_tab = {tab: np.nonzero(cats == tab)[0] for tab in ('Equip', 'Use', 'Set-up', 'Etc')}
        self._digit_cache = {}

    # -- the window ---------------------------------------------------------------------------
    @staticmethod
    def find(img, gh):
        """Top-left corner of the inventory window in img (screen pixels), found by the red line under
        its tabs, or None. The caller should confirm it once by the "ITEM INVENTORY" title."""
        s = gh / VIEW_H
        a = img.astype(np.int16)
        red = (a[..., 0] > 150) & (a[..., 1] < 90) & (a[..., 2] < 90)
        length = RED_LINE[2] * s
        for y in np.nonzero(red.sum(axis=1) > length * 0.85)[0]:
            xs = np.nonzero(red[y])[0]
            # the longest run of red in this row
            breaks = np.nonzero(np.diff(xs) > 2)[0]
            starts = np.r_[xs[0], xs[breaks + 1]]
            ends = np.r_[xs[breaks], xs[-1]]
            k = int(np.argmax(ends - starts))
            if ends[k] - starts[k] >= length * 0.85:
                return int(round(starts[k] - RED_LINE[0] * s)), int(round(y - RED_LINE[1] * s))
        return None

    def tab(self, img, origin, gh):
        """The open tab: its button is pink, the others grey."""
        s, (ox, oy) = gh / VIEW_H, origin
        best, score = None, 0.0
        for i, name in enumerate(TABS):
            x0, y0 = int(ox + (TAB_X + i * TAB_W + 4) * s), int(oy + (TAB_Y + 2) * s)
            box = img[y0:int(y0 + (TAB_H - 4) * s), x0:int(x0 + (TAB_W - 8) * s)].astype(np.int16)
            if box.size == 0:
                continue
            pink = ((box[..., 0] > 180) & (box[..., 0] - box[..., 2] > 40) & (box[..., 0] - box[..., 1] > 50)).mean()
            if pink > score:
                best, score = name, pink
        return best if score > 0.15 else None

    def slot_box(self, origin, gh, col, row):
        """(x0, y0, x1, y1) of a slot in screen pixels."""
        s, g = gh / VIEW_H, self.grid
        x0, y0 = origin[0] + (g['x'] + col * g['dx']) * s, origin[1] + (g['y'] + row * g['dy']) * s
        return x0, y0, x0 + 32 * s, y0 + 32 * s

    def slot_at(self, origin, gh, x, y):
        """(col, row) of the slot under screen point (x, y), or None."""
        for row in range(self.grid['rows']):
            for col in range(self.grid['cols']):
                x0, y0, x1, y1 = self.slot_box(origin, gh, col, row)
                if x0 <= x < x1 and y0 <= y < y1:
                    return col, row
        return None

    # -- stack size -------------------------------------------------------------------------
    def _digit_image(self, d, s, fx, fy):
        """Digit d as the game draws it at scale s (smoothly scaled), offset by the sub-pixel
        fraction (fx, fy): (RGB, opacity 0..1)."""
        key = (d, round(s, 4), round(fx * 4) / 4, round(fy * 4) / 4)
        if key not in self._digit_cache:
            rgb, mask = self.digits[d]
            h, w = mask.shape
            W, H = int(np.ceil(w * s + 1)), int(np.ceil(h * s + 1))
            rgba = np.dstack([rgb, mask * 255.0]).clip(0, 255).astype(np.uint8)
            img = Image.fromarray(rgba).transform((W, H), Image.AFFINE, (1 / s, 0, -key[2] / s, 0, 1 / s, -key[3] / s),
                                                  resample=Image.BILINEAR)
            a = np.asarray(img).astype(np.float32)
            self._digit_cache[key] = (a[..., :3], a[..., 3] / 255)
        return self._digit_cache[key]

    def count(self, img, origin, gh, col, row):
        """The stack size drawn in a slot, or None when there's no number (a single item)."""
        s = gh / VIEW_H
        sx, sy = self.slot_box(origin, gh, col, row)[:2]
        y = sy + DIGIT_Y * s
        digits, x = [], None
        while len(digits) < 5:
            # the first digit also settles the exact position; the rest follow it
            starts = [sx + (DIGIT_X + d) * s for d in DIGIT_SHIFTS] if x is None else [x]
            best = None
            for x0 in starts:
                for d in range(10):
                    e = self._digit_error(img, d, s, x0, y)
                    if e is None:
                        return None
                    if best is None or e < best[0]:
                        best = (e, d, x0)
            if best[0] > DIGIT_MAX_ERROR:
                break
            digits.append(best[1])
            x = best[2] + self.digits[best[1]][1].shape[1] * s  # each digit image follows the last
        return int(''.join(map(str, digits))) if digits else None

    def _digit_error(self, img, d, s, x, y):
        """How different the screen is from digit d drawn at (x, y): mean colour difference over the
        digit's solid pixels (None if off the image)."""
        rgb, alpha = self._digit_image(d, s, x % 1, y % 1)
        ix, iy = int(x), int(y)
        patch = img[iy:iy + rgb.shape[0], ix:ix + rgb.shape[1]].astype(np.float32)
        if patch.shape[:2] != rgb.shape[:2]:
            return None
        return float(np.abs(patch - rgb)[alpha > 0.6].mean())

    # -- one slot ---------------------------------------------------------------------------
    def _slot_ui(self, img, origin, gh, col, row, pad=3):
        """The slot (plus pad UI px around it) shrunk back to UI pixels."""
        s = gh / VIEW_H
        x0, y0, x1, y1 = self.slot_box(origin, gh, col, row)
        x0, y0, x1, y1 = (int(round(v)) for v in (x0 - pad * s, y0 - pad * s, x1 + pad * s, y1 + pad * s))
        crop = img[max(0, y0):y1, max(0, x0):x1]
        size = (32 + 2 * pad, 32 + 2 * pad)
        return np.asarray(Image.fromarray(crop).resize(size, Image.BOX)).astype(np.float32)

    def identify(self, img, origin, gh, col, row, tab, number=None):
        """[(error, item id)] best matches for the item in a slot, best first, or [] for an empty slot.
        The slot is shrunk back to UI pixels and compared with the icons of the open tab's items,
        leaving out the corner the stack size is drawn over."""
        candidates = self.by_tab.get(tab)
        if candidates is None or len(candidates) == 0:
            return []
        pad = 2
        ui = self._slot_ui(img, origin, gh, col, row, pad)
        inner = ui[pad:pad + 32, pad:pad + 32]
        if np.abs(inner - EMPTY).max(axis=2).mean() < 12:
            return []  # nothing but the empty slot's colour
        keep = np.ones((32, 32), bool)
        if number:  # the digits cover the bottom-left corner
            width = sum(self.digits[int(ch)][1].shape[1] for ch in str(number))
            keep[19:32, :min(32, width + 1)] = False
        rgb, mask = self.icon_rgb[candidates], self.icon_mask[candidates] & keep
        best = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                view = ui[pad + dy:pad + dy + 32, pad + dx:pad + dx + 32]
                diff = np.abs(rgb - view).mean(axis=3)
                err = (diff * mask).sum(axis=(1, 2)) / np.maximum(mask.sum(axis=(1, 2)), 1)
                best = err if best is None else np.minimum(best, err)
        # Look-alikes (return scrolls differ only in colour) are told apart at full screen resolution:
        # the shortlist's icons drawn as the game draws them, over the slot's colour.
        shortlist = [candidates[i] for i in np.argsort(best)[:SHORTLIST]]
        scored = sorted((self._screen_error(img, origin, gh, col, row, k, number), int(self.icon_ids[k]))
                        for k in shortlist)
        return scored[:3]

    def _screen_error(self, img, origin, gh, col, row, k, number):
        s = gh / VIEW_H
        x0, y0 = self.slot_box(origin, gh, col, row)[:2]
        icon = self.icons[k].astype(np.float32)
        slot = np.concatenate([np.broadcast_to(EMPTY, (32, 32, 3)) * (1 - icon[..., 3:] / 255)
                               + icon[..., :3] * icon[..., 3:] / 255, np.full((32, 32, 1), 255.0)], axis=2)
        W = int(np.ceil(32 * s))
        drawn = np.asarray(Image.fromarray(slot.clip(0, 255).astype(np.uint8)).transform(
            (W, W), Image.AFFINE, (1 / s, 0, -(x0 % 1) / s, 0, 1 / s, -(y0 % 1) / s), resample=Image.BILINEAR))
        patch = img[int(y0):int(y0) + W, int(x0):int(x0) + W].astype(np.float32)
        if patch.shape[:2] != (W, W):
            return 1e9
        keep = np.zeros((W, W), bool)
        b = int(2.5 * s)  # leave out the slot's edge (a selected slot gets a blue frame)
        keep[b:W - b, b:W - b] = True
        if number:  # and the stack size's digits
            width = sum(self.digits[int(ch)][1].shape[1] for ch in str(number))
            keep[int(19 * s):, :int((width + 1) * s)] = False
        return float(np.abs(patch - drawn[..., :3])[keep].mean())


class InventoryReader(threading.Thread):
    """While the inventory window is open: its tab, and each visible slot's item and stack size.

    Publishes `state` = {'origin', 'tab', 'gh', 'slots': {(col, row): (item id or None, count)},
    'total', 'unknown'} (None while the window isn't open). Slots are only re-read when their pixels
    change, so an open inventory costs little after the first pass."""

    def __init__(self, tracker, grab, make_ocr):
        super().__init__(daemon=True)
        self.tracker, self.grab, self.make_ocr = tracker, grab, make_ocr
        self.enabled = True
        self.state = None
        self.inv = None
        self.confirmed = set()   # window positions whose "ITEM INVENTORY" title has been read
        self.memo = {}           # (tab, slot pixels signature) -> (item id or None, count)

    def _value(self, item_id, count):
        """What a vendor pays for a stack (0 for unrecognised or unsellable items)."""
        rec = self.inv.items.get(item_id) if item_id is not None else None
        return rec['price'] * count if rec and not rec.get('no_sale') else 0

    def run(self):
        import mss
        sct, ocr = getattr(mss, 'MSS', mss.mss)(), None
        while True:
            time.sleep(READ_EVERY)
            st = self.tracker.state
            if not self.enabled or not st.get('visible'):
                self.state = None
                continue
            try:
                if self.inv is None:
                    self.inv = Inventory()
                    ocr = self.make_ocr()
                gx, gy, gw, gh = st['rect']
                img = np.array(self.grab(sct, gx, gy, gw, gh))
                self.state = self._read(img, gw, gh, ocr)
            except Exception as ex:  # keep going; a bad pass just shows nothing
                print(f'[inventory] read failed: {ex!r}')
                self.state = None

    def _read(self, img, gw, gh, ocr):
        inv = self.inv
        origin = inv.find(img, gh)
        if origin is None:
            return None
        prev = self.state if self.state and self.state['origin'] == origin else None
        s = gh / VIEW_H
        gx, gy = self.tracker.state['rect'][:2]
        cx, cy = itemhover.cursor_pos()
        if prev and origin[0] <= cx - gx <= origin[0] + WINDOW[0] * s and origin[1] <= cy - gy <= origin[1] + WINDOW[1] * s:
            return prev  # the mouse is over the window: a tooltip may be covering slots, keep what we had
        covered = itemhover.own_rects(self.tracker)  # our own windows (with "Show on stream" they're captured)
        # the game draws its mouse cursor (a glove, about a slot in size): slots under it can't be read
        cursor = (cx - gx, cy - gy, cx - gx + CURSOR * s, cy - gy + CURSOR * s)
        key = (origin[0] // 4, origin[1] // 4)
        if key not in self.confirmed:
            s = gh / VIEW_H
            x0, y0 = max(0, origin[0]), max(0, origin[1])
            title = img[y0:int(y0 + 20 * s), x0:int(x0 + 150 * s)]
            if 'INVENTORY' not in ' '.join(w[0] for w in ocr.words(title)).upper():
                return None
            self.confirmed.add(key)
        tab = inv.tab(img, origin, gh)
        slots, total, unknown = {}, 0, 0
        for row in range(inv.grid['rows']):
            for col in range(inv.grid['cols']):
                x0, y0, x1, y1 = (int(v) for v in inv.slot_box(origin, gh, col, row))
                if prev and any(x0 < r[2] and r[0] < x1 and y0 < r[3] and r[1] < y1 for r in covered):
                    result = prev['slots'].get((col, row))  # hidden under one of our windows
                    if result:
                        slots[(col, row)] = result
                        total += self._value(*result)
                        unknown += result[0] is None
                    continue
                if x0 < cursor[2] and cursor[0] < x1 and y0 < cursor[3] and cursor[1] < y1:
                    result = prev['slots'].get((col, row)) if prev else None
                    if result:
                        slots[(col, row)] = result
                        total += self._value(*result)
                        unknown += result[0] is None
                    continue
                sig = (tab, img[y0:y1:6, x0:x1:6].tobytes())
                if sig not in self.memo:
                    count = inv.count(img, origin, gh, col, row) if tab != 'Equip' else None
                    found = inv.identify(img, origin, gh, col, row, tab, count) if tab in inv.by_tab else []
                    if not found:
                        result = None  # an empty slot
                    else:
                        err, item_id = found[0]
                        result = (item_id if err <= ICON_MAX_ERROR else None, count or 1)
                    if len(self.memo) > 2000:
                        self.memo.clear()
                    self.memo[sig] = result
                result = self.memo[sig]
                if result is None:
                    continue
                slots[(col, row)] = result
                total += self._value(*result)
                unknown += result[0] is None
        return {'origin': origin, 'tab': tab, 'gh': gh, 'slots': slots, 'total': total, 'unknown': unknown}
