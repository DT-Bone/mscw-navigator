"""EXP and meso tracking: read EXP from the status bar and mesos from the inventory window.

The game's small digit font defeats plain OCR, so number areas are cut out, turned into dark text
on white, enlarged and smoothed, with a word in front for context (as in questlog.read_count).
Several cut-offs are tried and a value is only accepted when at least two readings agree.
"""
import collections
import re
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


def _enlarged_reads(crop, masks, prefix, suffix='total', sizes=(260, 360, 440)):
    """OCR text of crop under several masks/sizes (each mask: True where the text is). The cut-out is
    placed between two words so even a short number reads as part of a phrase."""
    out = []
    for mask in masks:
        digits = Image.fromarray(np.pad(np.where(mask, 0, 255).astype(np.uint8), 16, constant_values=255))
        for target_h in sizes:
            sc = max(2, round(target_h / digits.height))
            big = digits.resize((digits.width * sc, digits.height * sc), Image.BICUBIC).filter(
                ImageFilter.GaussianBlur(sc / 3))
            font = ImageFont.truetype('arial.ttf', big.height // 3)
            canvas = Image.new('L', (big.width + 1700, big.height), 255)
            draw = ImageDraw.Draw(canvas)
            draw.text((20, big.height // 3), prefix, fill=0, font=font)
            canvas.paste(big, (860, 0))
            draw.text((860 + big.width + 60, big.height // 3), suffix, fill=0, font=font)
            out.append(' '.join(ocr_lines(canvas)).replace(suffix, ' '))
    return out


ocr_lines = None  # set by StatsReader: lambda PIL image -> list of OCR'd lines


def _lum(rgb):
    return rgb[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)


def _vote(values):
    """Most common value seen at least twice, else None."""
    counts = collections.Counter(v for v in values if v is not None)
    if not counts:
        return None
    value, n = counts.most_common(1)[0]
    return value if n >= 2 else None


LOOKALIKE = str.maketrans({'O': '0', 'o': '0', 'D': '0', 'Q': '0', 'l': '1', 'I': '1', 'i': '1', '|': '1',
                           '!': '1', 'S': '5', 's': '5', 'B': '8', 'Z': '2', 'z': '2'})


def _number(token):
    """Digits of a number OCR'd from the game font, fixing lookalike letters; None if not a number."""
    digits = re.sub(r"[,.\s'`’]", '', token.translate(LOOKALIKE))
    return int(digits) if digits.isdigit() else None


def read_exp(bar, ocr):
    """(exp, percent) from a status-bar screenshot; either may be None if it didn't read reliably.

    The percentage reads more reliably than the EXP number, so they're voted on separately."""
    words = ocr.words(bar)
    label = next((w for w in words if w[0].upper().startswith('EXP')), None)
    if label is not None:
        _, x, y, w, h = label
        # include the label: it sometimes reads glued to the number ("EXP24e" for "EXP 24,830")
        x0, x1 = x, min(bar.shape[1], x + w + h * 11)
    else:
        # The "EXP" label itself often doesn't read, but the bar's layout is fixed: HP, MP and EXP
        # sections are evenly spaced, with the Cash Shop button after EXP.
        hp = next((w for w in words if w[0].upper().startswith('HP')), None)
        mp = next((w for w in words if w[0].upper().startswith('MP')), None)
        shop = next((w for w in words if 'CASH' in w[0].upper()), None)
        if mp is None or (hp is None and shop is None):
            return None, None
        _, x, y, w, h = mp
        x0 = x + (x - hp[1]) if hp else x + (shop[1] - x) / 2.4
        x1 = min(bar.shape[1], shop[1] - h * 0.5 if shop else x0 + h * 13)
    crop = bar[int(max(0, y - h * 0.2)):int(y + h * 1.2), int(x0):int(x1)]
    if crop.size == 0:
        return None, None
    rgb, lum = crop.astype(np.float32), _lum(crop)
    exps, pcts = [], []
    masks = (lum > 170, lum > 150, lum > 130, rgb.max(axis=2) > 170)
    for text in _enlarged_reads(crop, masks, 'Total', sizes=(240, 300, 360)):
        text = text.replace('Total', ' ').replace('EXP', ' ')
        # the percentage's digits suffer the same lookalikes ("8s.68%" for "85.68%")
        pct = re.search(r'([0-9OolIsSB]{1,3})[.,]([0-9OolIsSB]{2})\s*[%+/]', text)
        if pct:
            whole, frac = pct.group(1).translate(LOOKALIKE), pct.group(2).translate(LOOKALIKE)
            pct = pct if whole.isdigit() and frac.isdigit() else None
        if not pct or float(f'{whole}.{frac}') > 100:
            continue  # no usable percentage: the number before it can't be trusted either
        pcts.append(float(f'{whole}.{frac}'))
        text = text[:pct.start()]  # the EXP number comes before the percentage
        # digit groups may read as "8, 313" or "2'597"
        num = re.search(r"([0-9OolI|][0-9OolI|,.'`’ ]*)", text.strip())
        if num:
            exps.append(_number(num.group(1).strip()))
    return _vote(exps), _vote(pcts)


def find_meso_field(win):
    """The meso box (x0, y0, x1, y1) in an image of the inventory window: the lowest wide strip of
    pure white (the title bar is white too, but it's at the top)."""
    white = win.min(axis=2) >= 245
    rows = [y for y in range(win.shape[0]) if white[y].sum() >= win.shape[1] * 0.45]
    bands, start = [], None  # runs of consecutive white rows; thin ones are borders, not the box
    for y in rows + [None]:
        if start is not None and (y is None or y != prev + 1):
            bands.append((start, prev))
            start = None
        if y is not None and start is None:
            start = y
        prev = y
    a = win.astype(np.int16)
    coin = (a[..., 0] > 200) & (a[..., 1] > 160) & (a[..., 2] < 90)  # the gold coin left of the box
    for top, bottom in reversed([b for b in bands if b[1] - b[0] >= 6]):
        cols = np.nonzero(white[top:bottom + 1].mean(axis=0) > 0.6)[0]
        if coin[top:bottom + 1, :max(1, int(cols.min()) + 40)].sum() >= 20:
            return int(cols.min()), top, int(cols.max()), bottom
    return None


def read_mesos(win, ocr):
    """Meso count from an image of the inventory window, or None."""
    box = find_meso_field(win)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    crop = win[y0 + 3:y1 - 2, x0 + 6:x1 - 3]  # inside the box's border
    lum = _lum(crop)
    dark = np.nonzero((lum < 150).any(axis=0))[0]
    if len(dark) == 0:
        return None
    # The number is right-aligned: walk left from its last column until a wide gap (the box's left
    # side has the coin icon and a divider, which would confuse the OCR).
    right = left = int(dark[-1])
    for c in reversed(dark[:-1].tolist()):
        if left - c > crop.shape[0] * 1.2:
            break
        left = c
    crop = crop[:, max(0, left - 8):right + 8]
    lum = _lum(crop)
    values = []
    for text in _enlarged_reads(crop, (lum < 110, lum < 150), 'Mesos'):
        m = re.search(r'Mesos\s+([0-9OolI|DQSB][0-9OolI|DQSB,. ]*)', text)
        if m:
            values.append(_number(m.group(1)))
    return _vote(values)


def find_inventory(words):
    """Box of the "ITEM INVENTORY" title in OCR words: (x, y, w, h), or None."""
    for i, (text, x, y, w, h) in enumerate(words):
        if text.upper() == 'ITEM' and i + 1 < len(words) and words[i + 1][0].upper().startswith('INVENTOR'):
            nx, nw = words[i + 1][1], words[i + 1][3]
            return x, y, nx + nw - x, h
    return None


MAX_LEVEL = 200
MIN_TOTAL_PER_LEVEL_SQ = 5  # a level needs at least 5 x level^2 EXP (level 11 needs ~2,360 here)


class StatsTracker:
    """EXP and meso totals over a session that can be started, paused and reset.

    EXP gained carries across level-ups: the amount a level needed is exp / (percent / 100),
    so the rest of the old level is added when the level goes up. Anything gained while paused
    doesn't count (readings keep updating the baseline)."""

    def __init__(self, known_needed=None, on_learn=None):
        # EXP each level needs is fixed game data: keep it across resets and sessions.
        self.needed = {}
        for k, v in sorted((int(k), v) for k, v in (known_needed or {}).items()):
            if self._plausible_total(int(k), v):  # skip totals saved from misreads by older versions
                self.needed[int(k)] = v
        self.on_learn = on_learn  # called with (level, total) when a level's total is learned
        self.reset()

    def reset(self):
        self.running = False
        self.elapsed = 0.0
        self.started_at = None
        self.exp_gained = 0
        self.meso_gained = 0
        self.last_exp = None      # (exp, percent, level)
        self.last_meso = None
        self.meso_seen_at = None
        self.needed_samples = {}  # level -> recent exp / percent estimates, until 3 agree
        self.pending = None       # a big EXP jump waiting for a second reading to confirm it

    def start(self):
        if not self.running:
            self.running, self.started_at = True, time.time()

    def pause(self):
        if self.running:
            self.elapsed += time.time() - self.started_at
            self.running, self.started_at = False, None

    def seconds(self):
        return self.elapsed + (time.time() - self.started_at if self.running else 0)

    @property
    def paused(self):
        """Started since the last reset, but not running now (before the first Start it's just stopped)."""
        return not self.running and self.elapsed > 0

    def update_exp(self, exp, pct, level):
        """exp and/or pct may be None when they didn't read. A reliable (exp, pct) pair tells how much
        EXP the level needs, after which the percentage alone gives the EXP (to 0.01% of a level)."""
        known = self.needed.get(level)
        if exp is not None and pct and known and abs(exp - known * pct / 100) > known * 0.02:
            exp = None  # doesn't fit the percentage: a misread number
        if exp is not None and pct and not known:
            # Learn what the level needs only once 3 readings agree within 1%, so one misread
            # (e.g. "8,313" read as "8") can't spoil every later figure.
            samples = self.needed_samples.setdefault(level, [])
            samples.append(exp / (pct / 100))
            mid = sorted(samples)[len(samples) // 2]
            close = [v for v in samples if abs(v - mid) <= mid * 0.01]
            if len(close) >= 3:
                total = sum(close) / len(close)
                if self._plausible_total(level, total):
                    self.needed[level] = known = total
                    if self.on_learn:
                        self.on_learn(level, known)
                else:  # agrees with itself but not with the other levels: a repeated misread
                    samples.clear()
            del samples[:-8]
        if exp is None and pct is not None and known:
            exp = round(known * pct / 100)
        if exp is None or pct is None:
            return
        prev = self.last_exp
        if prev and self._big_jump(prev, exp, level):
            # A sudden big change (drop, huge gain, odd level) is only believed if the next reading agrees.
            pending, self.pending = self.pending, (exp, pct, level)
            if not (pending and pending[2] == level and abs(pending[0] - exp) <= max(50, exp * 0.02)):
                return
        self.pending = None
        if prev and self.running:
            p_exp, p_pct, p_level = prev
            if level and p_level and level > p_level and p_pct > 0:
                needed = p_exp / (p_pct / 100)          # what the old level took in total
                self.exp_gained += max(0, round(needed - p_exp)) + exp
            elif (level == p_level or not level or not p_level) and exp >= p_exp:
                self.exp_gained += exp - p_exp
            # a drop (death penalty) or an unclear level change: just re-baseline
        self.last_exp = (exp, pct, level)

    def _plausible_total(self, level, total):
        """Could `total` be the EXP that `level` needs? It must suit the level and fit between the totals
        already known for lower and higher levels (they only grow)."""
        if not level or not 1 <= level <= MAX_LEVEL or total < MIN_TOTAL_PER_LEVEL_SQ * level * level:
            return False
        lower = [v for k, v in self.needed.items() if k < level]
        higher = [v for k, v in self.needed.items() if k > level]
        return (not lower or total > max(lower)) and (not higher or total < min(higher))

    @staticmethod
    def _big_jump(prev, exp, level):
        p_exp, _, p_level = prev
        if level and p_level and level != p_level:
            return level != p_level + 1  # one level up is normal; anything else is suspicious
        return exp < p_exp - max(50, p_exp * 0.02) or exp - p_exp > max(2000, p_exp * 0.5)

    def update_meso(self, mesos):
        if self.last_meso is not None and self.running:
            self.meso_gained += mesos - self.last_meso  # can go down when you spend
        self.last_meso, self.meso_seen_at = mesos, time.time()

    def per_hour(self, amount):
        hours = self.seconds() / 3600
        return amount / hours if hours > 1 / 360 else None  # wait ~10s before showing a rate

    def exp_left(self):
        """EXP still needed for the next level, or None."""
        if not self.last_exp or not self.last_exp[1]:
            return None
        exp, pct, level = self.last_exp
        needed = self.needed.get(level) or exp / (pct / 100)
        return max(0, round(needed - exp))

    def to_next_level(self):
        """Seconds until the next level at the current EXP rate, or None."""
        rate = self.per_hour(self.exp_gained)
        left = self.exp_left()
        if left is None or not rate or rate <= 0:
            return None
        return left / rate * 3600


# The panel is sized for its longest possible content (see StatsPanel.__init__).
LONGEST_LINES = ('Time   99:59:59 (paused)',
                 'EXP    +9,999,999  9,999,999/h',
                 'Mesos  +99,999,999  99,999,999/h',
                 'Level  waiting for EXP',
                 'Left   99,999,999 EXP')
LONGEST_NOTE = 'EXP 99,999,999 (99.99%)  ·  Mesos 999,999,999 (open inventory to update)'


class StatsPanel:
    """Small panel at the right of the game window: time, EXP and mesos gained and per hour."""

    def __init__(self, root, k, tracker, prepare_window):
        import tkinter as tk
        from picker import ACCENT, BG, DIM, FG, PANEL
        self.tk, self.root, self.k, self.t = tk, root, k, tracker
        self.colors = (BG, DIM, FG, PANEL, ACCENT)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.config(bg=ACCENT)
        body = tk.Frame(self.win, bg=BG, padx=int(12 * k), pady=int(8 * k))
        body.pack(padx=2, pady=2)
        tk.Label(body, text='EXP / MESO TRACKER', font=('Segoe UI', 8, 'bold'), bg=BG, fg=ACCENT, anchor='w').pack(fill='x')
        self.text = tk.Label(body, font=('Consolas', 10), bg=BG, fg=FG, anchor='w', justify='left')
        self.text.pack(fill='x', pady=(int(4 * k), int(6 * k)))
        self.note = tk.Label(body, font=('Segoe UI', 8), bg=BG, fg=DIM, anchor='nw', justify='left', height=2)
        self.note.pack(fill='x')
        btns = tk.Frame(body, bg=BG)
        btns.pack(fill='x', pady=(int(6 * k), 0))
        self.toggle = self._button(btns, 'Pause', self._toggle, True)
        self._button(btns, 'Reset', self._reset, False)
        # Fixed size: lay out the longest content once and freeze the panel at that size, so it doesn't
        # grow and shrink as the numbers and messages change.
        self.text.config(text='\n'.join(LONGEST_LINES))
        self.text.update_idletasks()
        self.note.config(text=LONGEST_NOTE, wraplength=self.text.winfo_reqwidth())  # always 2 lines tall
        body.update_idletasks()
        body.config(width=body.winfo_reqwidth(), height=body.winfo_reqheight())
        body.pack_propagate(False)
        self.game_rect = None
        self.refresh()
        self.hwnd = prepare_window(self.win)

    def _button(self, parent, text, cmd, primary):
        BG, DIM, FG, PANEL, ACCENT = self.colors
        # fixed width, so "Start" / "Pause" don't nudge the Reset button
        b = self.tk.Label(parent, text=text, font=('Segoe UI', 9, 'bold'), bg=ACCENT if primary else PANEL,
                          fg=BG if primary else FG, padx=int(10 * self.k), pady=int(2 * self.k), cursor='hand2',
                          width=6)
        b.pack(side='left', padx=(0, int(6 * self.k)))
        b.bind('<Button-1>', lambda e: cmd())
        return b

    def _toggle(self):
        self.t.pause() if self.t.running else self.t.start()
        self.refresh()

    def _reset(self):
        self.t.reset()
        self.refresh()

    def refresh(self):
        t = self.t
        secs = int(t.seconds())
        rate = lambda v: f'{v:,.0f}/h' if v is not None else '--/h'
        lines = [f'Time   {secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}{" (paused)" if t.paused else ""}',
                 f'EXP    +{t.exp_gained:,}  {rate(t.per_hour(t.exp_gained))}',
                 f'Mesos  {"+" if t.meso_gained >= 0 else ""}{t.meso_gained:,}  {rate(t.per_hour(t.meso_gained))}']
        secs_left, exp_left = t.to_next_level(), t.exp_left()
        if secs_left is None:
            when = 'waiting for EXP' if t.running else 'press Start'
        elif secs_left >= 86400:
            when = f'~{int(secs_left // 86400)}d {int(secs_left // 3600 % 24)}h'
        else:
            when = f'~{int(secs_left // 3600)}h {int(secs_left // 60 % 60):02d}m'
        lines += [f'Level  {when}', f'Left   {f"{exp_left:,} EXP" if exp_left is not None else "--"}']
        self.text.config(text='\n'.join(lines))
        notes = []
        if t.last_exp:
            notes.append(f'EXP {t.last_exp[0]:,} ({t.last_exp[1]:.2f}%)')
        notes.append(f'Mesos {t.last_meso:,}' if t.last_meso is not None else 'Mesos: open your inventory to read them')
        if t.last_meso is not None and t.meso_seen_at and time.time() - t.meso_seen_at > 60:
            notes[-1] += ' (open inventory to update)'
        self.note.config(text='  ·  '.join(notes))
        self.toggle.config(text='Pause' if t.running else 'Start')

    def place(self):
        """Right-align against the game window's right edge."""
        if not self.game_rect:
            return
        gx, gy, gw, gh = self.game_rect
        self.win.update_idletasks()
        geometry = f'+{int(gx + gw - self.win.winfo_reqwidth() - gw * 0.006)}+{int(gy + gh * 0.5)}'
        if geometry != getattr(self, '_geometry', None):
            self._geometry = geometry
            self.win.geometry(geometry)

    def show(self, game_rect):
        if game_rect != self.game_rect:
            self.game_rect = game_rect
            self.place()
        if self.win.state() == 'withdrawn':
            self.win.deiconify()
            self.win.lift()

    def hide(self):
        if self.win.state() != 'withdrawn':
            self.win.withdraw()

    def raise_window(self):
        if self.win.state() != 'withdrawn':
            self.win.attributes('-topmost', True)
            self.win.lift()

    def destroy(self):
        self.win.destroy()
