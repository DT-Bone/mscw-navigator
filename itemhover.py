"""Which item was right-clicked: the game's item tooltip beside the cursor is read and its title is
looked up in the item index (items.py), for the item info panel.

Item tooltips are dark boxes with the item's name as the first line. Only lines on a dark background
count, so item names elsewhere (the Quest Helper box, chat) aren't taken for one.
"""
import ctypes
import ctypes.wintypes as wt
import difflib
import re
import threading
import time

import mss
import numpy as np

import items as item_index
import questbook
from questlog import _lines

POLL = 0.05            # seconds between right button checks
VK_RBUTTON = 0x02
RETRY_AFTER = 0.35     # nothing read at a right-click: read again this much later (the tooltip may not be up yet)
MIN_SCORE = 0.86       # how closely a tooltip line must match an item name
DARK = 110             # tooltip background must be darker than this (0-255)
# Area searched for the tooltip, in window heights around the cursor (tooltips open beside it,
# flipping to the other side near the window's edges).
LEFT, RIGHT, UP, DOWN = 0.36, 0.42, 0.22, 0.32
# Strip beside a hovered equipment title where the worn item's tooltip title is (window heights).
WORN_SKIP, WORN_RIGHT, WORN_UP_DOWN = 0.2, 0.75, 0.08


_user32 = ctypes.WinDLL('user32')  # own copy, so its argument types don't affect other modules
_user32.IsWindowVisible.argtypes = [wt.HWND]
_user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]


def _elevated(process):
    advapi32 = ctypes.windll.advapi32
    advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    token, value, size = wt.HANDLE(), wt.DWORD(), wt.DWORD()
    if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        return False
    ok = advapi32.GetTokenInformation(token, 20, ctypes.byref(value), 4, ctypes.byref(size))  # TokenElevation
    ctypes.windll.kernel32.CloseHandle(token)
    return bool(ok and value.value)


def self_elevated():
    """Is this program running as administrator?"""
    ctypes.windll.kernel32.GetCurrentProcess.restype = wt.HANDLE
    return _elevated(ctypes.windll.kernel32.GetCurrentProcess())


def game_elevated():
    """Is MapleStory running as administrator? Windows then hides mouse buttons from programs that aren't."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.FindWindowW.restype = wt.HWND
    hwnd = user32.FindWindowW('MapleStoryClass', None)
    if not hwnd:
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel32.OpenProcess.restype = wt.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        return _elevated(handle)
    finally:
        kernel32.CloseHandle(handle)


def own_rects(tracker):
    """Screen rectangles of the navigator's own visible windows, in game window coordinates."""
    gx, gy = tracker.state.get('rect', (0, 0))[:2]
    out = []
    for hwnd in list(tracker.own_hwnds):
        r = wt.RECT()
        if _user32.IsWindowVisible(hwnd) and _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            out.append((r.left - gx, r.top - gy, r.right - gx, r.bottom - gy))
    return out


def cursor_pos():
    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _clean(text):
    """Tooltip title without upgrade marks like "(+3)" or a leading "+3"."""
    return re.sub(r'\(\s*\+?\d+\s*\)|^\+\d+\s*', '', text).strip()


def _segments(line):
    """A text line's words split where they come from different boxes: two tooltips side by side
    read as one line ("•Green Bamboo Hat •Black Headband"), so split at wide gaps and at each "•"
    (tooltip titles start with one)."""
    out = []
    for w in sorted(line['words'], key=lambda w: w[1]):
        gap = w[1] - (out[-1][-1][1] + out[-1][-1][3]) if out else 0
        if not out or gap > 1.2 * line['h'] or w[0].startswith('•'):
            out.append([])
        out[-1].append(w)
    return out


class HoverReader(threading.Thread):
    """Publishes `clicked` for each right-click in the game window: the item whose tooltip is open there
    ({'name', 'ids', 'others', 'x', 'y', 'qty', ...}; 'ids' None when it isn't on an item), plus 't' (when)
    and 'cursor' (where, game window coords). A right-click on a quest's name instead (the quest book's list,
    see questbook.py) publishes `quest_clicked`: {'qid', 't'}."""

    def __init__(self, tracker, grab, make_ocr):
        """grab(sct, left, top, width, height) -> RGB array of the screen; make_ocr() -> an OCR client."""
        super().__init__(daemon=True)
        self.tracker, self.grab, self.make_ocr = tracker, grab, make_ocr
        self.enabled = True
        self.items, self.by_name = item_index.load()
        self.names = list(self.by_name)
        self.clicked = None  # the last right-click (see the class docstring)
        self.inventory = None  # the inventory reader (inventory.InventoryReader), for stack sizes
        self.right_down = False
        self.qdata = None                  # quest data, for right-clicks on quest names (set by the overlay)
        self.quest_context = lambda: ((), None)  # (quests in the Quest Helper box, job class), set by the overlay
        self.quest_clicked = None

    def run(self):
        ocr, sct = self.make_ocr(), getattr(mss, 'MSS', mss.mss)()
        while True:
            time.sleep(POLL)
            st = self.tracker.state
            if not (self.enabled or self.qdata) or not st.get('visible'):
                continue
            gx, gy, gw, gh = st['rect']
            sx, sy = cursor_pos()
            pos = (sx - gx, sy - gy)
            # A right-click on an item asks for its info panel. The button's state is only looked at
            # (no mouse hook), so the game still gets the click; right-clicking inventory items does
            # nothing in game.
            state = ctypes.windll.user32.GetAsyncKeyState(VK_RBUTTON)
            down = bool(state & 0x8001)  # held now, or pressed since the last look
            if down and not self.right_down and 0 <= pos[0] < gw and 0 <= pos[1] < gh:
                grab = lambda x, y, w, h: self.grab(sct, gx + x, gy + y, w, h)
                self._right_click(ocr, grab, pos, gw, gh)
            self.right_down = bool(state & 0x8000)

    def _own_rects(self):
        return own_rects(self.tracker)

    def _quest(self, ocr, grab, pos, gw, gh):
        """The quest whose name was right-clicked (the line of text under the cursor), or None."""
        x0, y0, x1, y1 = questbook.area(*pos, gw, gh)
        img = self._capture(grab, x0, y0, x1, y1)
        prefer, job_class = self.quest_context()
        return questbook.quest_under(ocr.words(img), pos[0] - x0, pos[1] - y0, self.qdata, prefer, job_class)

    def _right_click(self, ocr, grab, pos, gw, gh):
        r = quest = None
        for attempt in range(2):  # a quick click can beat the tooltip: look once more a moment later
            if attempt:
                time.sleep(RETRY_AFTER)
            if self.enabled:
                try:
                    r = self._read(ocr, grab, pos, gw, gh)
                except Exception as ex:
                    print(f'[item] read failed: {ex!r}')
                if r:
                    break
            if not attempt and self.qdata:  # no item tooltip: a quest's name? (the quest book)
                try:
                    quest = self._quest(ocr, grab, pos, gw, gh)
                except Exception as ex:
                    print(f'[quest] read failed: {ex!r}')
                if quest or not self.enabled:
                    break
        if quest:
            self.quest_clicked = {'qid': quest, 't': time.time()}
            print(f'[quest] right-click on {self.qdata["quests"][quest]["name"]}')
        # every right-click is reported (ids None when not on an item): one on nothing closes the panel
        self.clicked = {**(r or {}), 'ids': r['ids'] if r else None, 't': time.time(), 'cursor': pos}
        if r:
            print(f'[item] right-click on {r["name"]}')

    def _capture(self, grab, x0, y0, x1, y1):
        """Screen area (game window coords) with our own windows blanked out: with "Show on stream"
        they're in the capture, and the item info panel shows an item's name in large text on a dark
        background, just like a tooltip title."""
        img = np.array(grab(x0, y0, x1 - x0, y1 - y0))
        for ox0, oy0, ox1, oy1 in self._own_rects():
            img[max(0, oy0 - y0):max(0, oy1 - y0), max(0, ox0 - x0):max(0, ox1 - x0)] = 0
        return img

    def _titles(self, ocr, img, x0, y0, prefix_ok=False):
        """Item names on a dark background in img: [(score, x, y, h, name)] in game window coords.
        prefix_ok: also accept the start of a name (a title cut off at the edge of the capture)."""
        lum = img[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
        found = []
        for line in _lines(ocr.words(img)):
            for words in _segments(line):
                name = item_index.norm(_clean(' '.join(w[0] for w in words)))
                if len(name) < 3:
                    continue
                if name in self.by_name:
                    score, match = 1.0, name
                else:
                    close = difflib.get_close_matches(name, self.names, n=1, cutoff=MIN_SCORE)
                    starts = [n for n in self.names if n.startswith(name)] if prefix_ok and len(name) >= 6 else []
                    if close:
                        match = close[0]
                        score = difflib.SequenceMatcher(None, name, match).ratio()
                    elif len(starts) == 1:
                        score, match = MIN_SCORE, starts[0]
                    else:
                        continue
                lx0 = int(min(w[1] for w in words))
                lx1 = int(max(w[1] + w[3] for w in words))
                ly0, ly1 = int(line['y']), int(line['y'] + line['h'])
                # a tooltip title sits on the tooltip's dark box (the text itself is bright)
                band = lum[max(0, ly0 - 4):ly1 + 4, max(0, lx0 - 8):lx1 + 8]
                if band.size == 0 or np.median(band) > DARK:
                    continue
                found.append((score, x0 + lx0, y0 + ly0, line['h'], match))
        return found

    def _read(self, ocr, grab, at, gw, gh):
        cx, cy = at
        x0, y0 = int(max(0, cx - LEFT * gh)), int(max(0, cy - UP * gh))
        x1, y1 = int(min(gw, cx + RIGHT * gh)), int(min(gh, cy + DOWN * gh))
        found = self._titles(ocr, self._capture(grab, x0, y0, x1, y1), x0, y0, prefix_ok=True)
        if not found:
            return None
        top = max(f[0] for f in found)
        if top <= MIN_SCORE:
            return None  # only cut-off names: those only count for the worn item, never the hovered one
        best = [f for f in found if f[0] >= top - 0.05 and f[0] > MIN_SCORE]
        # Hovering equipment also opens a tooltip for what you're wearing, to the right of the hovered
        # item's (their tops needn't line up), so the leftmost title is the hovered item.
        chosen = min(best, key=lambda f: (f[1], f[2]))
        _, tx, ty, th, match = chosen
        is_equip = lambda name: any(self.items[i]['cat'] == 'Equip' for i in self.by_name[name])
        # Titles to its right: the tooltip of what you're wearing in that slot (a cut-off name counts
        # when only one item starts that way).
        others = [self.by_name[f[4]] for f in sorted(found, key=lambda f: f[1])
                  if f[1] > tx and f[4] != match and is_equip(f[4])] if is_equip(match) else []
        if is_equip(match) and not others:
            # The worn item's tooltip can reach past the area read above: read a strip to the right of
            # the hovered tooltip for its title.
            sx0, sx1 = int(tx + WORN_SKIP * gh), int(min(gw, tx + WORN_RIGHT * gh))
            sy0, sy1 = int(max(0, ty - WORN_UP_DOWN * gh)), int(min(gh, ty + WORN_UP_DOWN * gh))
            if sx1 - sx0 > 20:
                worn = self._titles(ocr, self._capture(grab, sx0, sy0, sx1, sy1), sx0, sy0, prefix_ok=True)
                others = [self.by_name[f[4]] for f in sorted(worn, key=lambda f: f[1]) if f[4] != match and is_equip(f[4])]
        ids = self.by_name[match]
        recs = [self.items[i] for i in ids]
        prices = sorted({r['price'] for r in recs if not r.get('no_sale')})
        rec = recs[0]
        if len(prices) > 1:  # same name, different items: give the range
            text = f'Sell to vendor for {prices[0]:,}-{prices[-1]:,} mesos each'
        else:
            text = item_index.sell_text(next((r for r in recs if not r.get('no_sale')), rec))
        qty = self._stack_size(at)
        if qty and qty > 1 and len(prices) == 1 and prices[0] and not rec.get('unit'):
            text += f'  ·  stack of {qty:,} = {qty * prices[0]:,} mesos'
        print(f'[item] {rec["name"]}: {text}')
        return {'text': text, 'name': rec['name'], 'ids': ids, 'others': others, 'x': tx, 'y': ty, 'at': at,
                'qty': qty}

    def _stack_size(self, at):
        """How many are in the inventory slot under the cursor (from the inventory reader), or None."""
        inv = self.inventory.state if self.inventory else None
        if not inv or not self.inventory.inv:
            return None
        slot = self.inventory.inv.slot_at(inv['origin'], inv['gh'], *at)
        found = inv['slots'].get(slot) if slot else None
        return found[1] if found else None
