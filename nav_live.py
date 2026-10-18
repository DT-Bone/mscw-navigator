"""Live navigation overlay for MapleStory Classic World.

    py -3.11 nav_live.py [destination map id or name] [--show-in-capture]

With no destination it just shows which map you are on.

Reads only the screen: the minimap's map name (Windows OCR, on map change) and the
player's yellow minimap dot (pixel colours, ~20x/s). Draws an arrow around the character
in a click-through window on top of the game. Never touches the game process.
Close with Ctrl+C in the console.
"""
import collections
import difflib
import ctypes
import ctypes.wintypes as wt
import math
import os
import pathlib
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.messagebox
import traceback

# Physical pixels everywhere (4K at 150% scaling would otherwise be virtualised).
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)

import mss
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import canvas
import minimap
import mobs
import npcs
import quests
import stats
import questlog
import itemhover
import leveling
import mesowatch
import iteminfo
import crafting
import gathering
import inventory
import statusbar
from maps import _norm as map_norm, find as find_map, load_maps
import travel
import items as item_index
from navgraph import MAP_DIR, SCRIPT_TARGETS, _targets, distances, load_graph, route
from ocr_client import SyncOcr
from picker import Picker, load_prefs, remove_prefs, save_prefs
from questhelper import GuideButtons, QuestGuide, QuestPanel
from routeguide import JOB_INSTRUCTORS, RouteFollow, RoutePanel, instructor
from kpqpanel import KpqPanel
from tagtrack import TagTracker
from wzreader import WzFile

HERE = pathlib.Path(__file__).parent
OCR_PS1 = HERE / 'ocr.ps1'
INSTANCE_MUTEX = 'Local\\MSCW-Navigator'  # held while the navigator runs (launcher.py checks it)
DEBUG_DIR = HERE / 'debug'  # minimap captures where the dot wasn't found are saved here, if it exists

# --- tuning (calibrate in live tests) -------------------------------------------
MM_SIDE_BORDER = 6       # UI pixels of minimap frame on each side of the map picture
MM_SLACK = 4             # px the scrolling picture may sit past its computed limits (rounding)
MARKER_LIFT = 117        # map units the minimap draws portal rings and NPC markers above the real spot
DOT_Y_OFFSET = 84        # map units from the minimap dot centre down to the character's feet
VIEW_H = 768             # the game's logical view height (1366x768), scaled to the window
CAM_Y_LIFT = 95         # the camera centres this many map units above the character's feet
CAM_X_OFFSET = 0         # the camera centres this many map units right of the character
BODY_HEIGHT = 35         # map units from feet to where the arrow ring is centred
COMPASS_R = 30           # map units: radius of the compass ring
COMPASS_LIFT = 75        # map units from body centre up to the compass centre (just above the head)
PIVOT_EASE = 0.08        # how quickly the compass follows the character (0..1 per UI frame)
PIVOT_DEADBAND = 4       # pixels of movement ignored, so the compass doesn't shiver
CAM_TAU = (0.9, 0.5)     # seconds: how fast the camera catches up with the character (x, y)
CAM_DEAD = 5             # map units of gap the camera doesn't bother closing
TAG_TRUST = 90           # map units: name-tag readings this close to the camera model correct it
TAG_PULL = 0.08          # share of the gap to a trusted name-tag reading closed per frame
TAG_PULL_FAR = 0.35      # ...when it's more than TAG_PULL_FROM units off (after moving): catch up fast
TAG_PULL_FROM = 30
GROUND_BELOW = (-15, 45)  # a foothold this far below the minimap dot's reading (map units) is where you stand
GROUND_BIAS = 24          # (the dot reads ~20-28 units above the feet: the platform nearest that far down wins)
TAG_DEADBAND = 8         # map units of name-tag disagreement left alone
TAG_FAR_FRAMES = 6       # readings in a row needed before a far-off name tag is believed
TAG_MAX = 250            # map units: a name-tag reading this far from the camera model is never believed (it found
                         # something else that looks like the tag), and the next search starts around the prediction
MARK_LIFT = 115          # map units from the portal's base up to the tip of the marker arrow over it
MARK_SIZE = 24           # map units: height of the marker arrow
HIDDEN_PORTAL_TYPES = {1, 5, 8, 10, 11}  # portal types the game doesn't draw (or only up close)
HIDDEN_COLOUR = '#ff7ad9'  # hidden street markers (pink: unlike the dot, portal rings and NPC markers)
NAME_TAG_CLEAR = 34       # map units below the feet that the name tag occupies; labels go under it
DOT_CLEARANCE = 0.011    # minimap markers keep this far (x window height) from the player's dot
# The minimap's grey frame is ~5 UI px thick (14-15 px at 4K). Windows mistaken for it (NPC dialogs, shop
# windows) measured 50-397 px; anything thicker than this (x window height) isn't the minimap.
MM_MAX_BAND = 0.012
MM_BAND_UI = 5.15         # the minimap frame's thickness in UI px (14-15 px at 4K); a far thinner reading was taken
                          # mid-redraw, and put the title read in the wrong place
# Panels fade while the character moves: opacity then, how far (map units) the minimap dot must move within
# MOVE_WINDOW seconds to count as moving (its own jitter is a few units), and how long after stopping they
# come back. A panel under the mouse stays solid.
FADE_ALPHA, MOVE_DIST, MOVE_WINDOW, FADE_BACK_AFTER = 0.3, 10, 0.4, 0.3
GATHER_COLOUR = '#3fd8e8'   # things to break for a quest (cyan: unlike the dot and the other markers)
TELEPORT_COLOUR = '#9b87ff'  # teleporter markers (violet: also unlike anything the minimap reader looks for)
ARROWS = '→↘↓↙←↖↑↗'  # → ↘ ↓ ↙ ← ↖ ↑ ↗, by angle in 45° steps
ARRIVE_RADIUS = 60       # within this distance of the portal, show "press Up" instead
HIDDEN_ARRIVE_RADIUS = 110  # dot vanished this close to a portal: it's hidden under the portal marker
LOST_HOLD = 2.0          # seconds to keep the last position when the dot briefly disappears
ARRIVED_SHOW = 4.0       # seconds to show "Arrived" before going back to "You are in"
FM_ENTRANCE = 80002000
TITLE_REREAD = 20.0        # seconds between routine re-reads of the minimap title
TITLE_RETRY = 3.0          # ... and between reads while the map is unknown (its title couldn't be read)
LEVEL_EVERY = 30.0        # seconds between status-bar level reads
JOB_WATCH_EVERY = 3.0     # ...and between job reads while waiting for a job advancement in an instructor's room
# Without a minimap, the status bar tells whether the player is in the game world at all: its gauges read
# "HP[...]", "MP[...]", "EXP" and it has a CASH SHOP button. Two of these = in the world (OCR keeps some of
# them every time); none in the Cash Shop, on loading screens or at character select.
STATUS_BAR_WORDS = ('HP', 'MP', 'EXP', 'CASH')
WORLD_CHECK_EVERY = 1.0   # seconds between those checks (only while no minimap is found)
LEVEL_CONFIRM_AFTER = 2.0  # seconds before re-reading to confirm a changed level
STATS_EVERY = 5.0          # seconds between EXP/meso reads while the tracker is shown
STATS_WARMUP_EVERY = 1.5   # ... while it still needs a first reading or the level's EXP total
INVENTORY_SEARCH_EVERY = 15.0  # seconds between whole-screen searches for a closed inventory window
QUESTLOG_EVERY = 3.0      # seconds between reads of the game's Quest Helper box
# Kerning PQ, stage 1 ("1st Accompaniment <1st Stage>"): Cloto asks each member (but the leader) a question; its
# answer is how many Coupons to collect from the Ligators (and trade for a Pass).
MINIMAP_CLEAR = 2.5       # seconds the panels under the minimap step aside when it's lost (see _clear_minimap)
MINIMAP_RETRY = (6, 15, 30)  # while it stays lost, they step aside again after these many seconds (then every 30)
KPQ_STAGE1 = 80000000
KPQ_EVERY = 1.0           # seconds between reads of the dialog area on that map
KPQ_LOG = pathlib.Path(__file__).with_name('kpq_questions.txt')  # every question of Cloto's read, to check the list by
KPQ_DIALOGS = pathlib.Path(__file__).with_name('kpq_dialogs')      # ...and a picture of each
KPQ_COUPON = 4001001
# Words (lower case) that must all be in the question -> (coupons, confirmed). The first matching rule wins, so
# the stat questions (which also mention the advancement) come before the advancement level. Confirmed by the
# game's files: the first job advancement is at level 10 (Rain's Maple Quiz 5: "Level 10" for Magician) and the
# second at 30 (the instructors' quests). The rest are the original game's values, not confirmed for Classic.
# Cloto's questions on Kerning PQ's first stage, as Classic World asks them (her script is server-side, not in the game
# files): (what she asks about, coupons, confirmed, keyword sets that pick the question out of her dialog). Confirmed:
# the game itself says so (Rain's Maple Quiz: Magicians advance at 10; every job does; 2nd job quests start at 30),
# and Cloto has been read asking those ("the number of levels needed to make the first Job Advancement as a
# Magician", "the level required to make the second job advancement").
# The rest are from Classic World guides written from the test servers' footage (Aug 2026); the 21 was seen in that
# footage without its question being written down, and the entry level of this PQ (21) is the likely one.
KPQ_QUESTIONS = (
    ("Level for a Magician's 1st job advancement", 10, True, (('magician', 'advancement'), ('magician', 'job', 'level'))),
    ("Level for a Warrior's, Bowman's or Thief's 1st job advancement", 10, True,
     (('advancement', 'level'), ('first', 'job'), ('1st', 'job'))),
    ('EXP to go from level 1 to 2', 15, False,
     (('exp', 'level 1'), ('experience', 'level 1'), ('exp', 'level 2'), ('experience', 'level 2'))),
    ('INT a Magician needs', 20, False, (('int', 'magician'),)),
    ('Level needed to enter this party quest (?)', 21, False, (('party', 'quest', 'level'),)),
    # "the total number of AP gained from leveling up from level 1 to level 6": 5 AP a level (Rain's Maple Quiz 6);
    # worked out from the levels she names (kpq_answer), in case they change
    ('AP gained from level 1 to level 6 (5 a level)', 25, True, (('ap', 'level'),)),
    ('DEX a Bowman or Thief needs', 25, False, (('dex', 'bowman'), ('dex', 'archer'), ('dex', 'thief'))),
    ('Level for the 2nd job advancement', 30, True,
     (('second', 'advancement'), ('2nd', 'advancement'), ('second', 'job'), ('2nd', 'job'))),
    ('STR a Warrior needs', 35, False, (('str', 'warrior'),)),
)
KPQ_AP = 5  # AP gained each level
# the order they're tried in: specific ones first (the stat questions name a job and its advancement too)
KPQ_MATCH_ORDER = (3, 6, 8, 7, 5, 2, 4, 0, 1)
LOG_HISTORY = 6            # Quest Helper reads kept for smoothing
LOG_GONE = 4               # reads in a row without a quest before it counts as gone
LOG_SETTLE = 3.0           # seconds after a map change before the box is trusted again
TRACK_HZ = 20
FRAME_MS = 16             # overlay redraw interval (~60 fps)
SMOOTH = 0.1             # arrow direction easing per UI frame (0..1)
KEY = '#ff00fe'          # overlay transparency key colour

user32 = ctypes.windll.user32
user32.FindWindowW.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.GetAncestor.restype = wt.HWND


def game_client_rect():
    hwnd = user32.FindWindowW('MapleStoryClass', None)
    if not hwnd:
        return None, None
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return hwnd, (pt.x, pt.y, r.right, r.bottom)


_matchers = {}  # map id -> minimap.PictureMatcher (or None if the map has no minimap image)


def _picture_matcher(map_id, geom, s):
    if map_id not in _matchers:
        matcher = None
        info = geom.get('mm_canvas')
        if info:
            try:
                wz = WzFile(str(MAP_DIR / info[0]))
                data = wz.read_image(dict(wz.images())[info[1]])['miniMap']['canvas']['_data']
                rgba = canvas.decode(wz, data)
                wz.close()
                if rgba is not None:
                    mm = geom['mm']
                    matcher = minimap.PictureMatcher(rgba, round(mm['w'] * s), round(mm['h'] * s))
            except (KeyError, OSError, ValueError) as ex:
                print(f'[minimap] no minimap image for {map_id}: {ex!r}')
        _matchers[map_id] = matcher
    return _matchers[map_id]


def minimap_viewport(well, gh):
    """The part of a minimap well the map picture shows through: the well minus the frame's side borders."""
    b = int(round(MM_SIDE_BORDER * gh / VIEW_H))
    return minimap.Well(well.x0 + b, well.x1 - b, well.y0, well.y1, well.band)


def map_position(region, local, dot, geom, gh, map_id=None):
    """Character's feet in map units from the minimap dot, or None if the minimap can't be placed."""
    mm = geom['mm']
    s = gh / VIEW_H / 2 ** mm['mag']  # the minimap is 1/2^mag of the world scale
    pic_w, pic_h = mm['w'] * s, mm['h'] * s
    # The picture shows through a viewport inset from the window frame by its side borders; a scrolled
    # picture stops at the viewport's edges, not the frame's (missing this put the player ~95 units off
    # near the ends of scrolling maps).
    inner = minimap_viewport(local, gh)
    scrolls = pic_w > inner.w + 3 or pic_h > inner.h + 3
    origin = None
    if scrolls and map_id is not None:
        # A map too big for the minimap window scrolls: find the view by matching the visible part
        # against the map's minimap image. Axes that fit are centred, so they don't need searching.
        matcher = _picture_matcher(map_id, geom, s)
        if matcher:
            view = region[inner.y0:inner.y1, inner.x0:inner.x1]
            rng = lambda size, pic: ((size - pic) / 2,) * 2 if pic <= size + 3 else (size - pic - MM_SLACK, MM_SLACK)
            found = matcher.locate(view, rng(inner.w, pic_w), rng(inner.h, pic_h))
            if found:
                origin = (inner.x0 + found[0], inner.y0 + found[1])
    if origin is None:  # fallback: scroll limits, then portal/NPC markers
        markers = minimap.find_markers(region, local) if scrolls else {}
        # Portal rings and NPC markers are drawn above the actual portal/NPC position.
        anchors = {'portal': [((p['x'] + mm['cx']) * s, (p['y'] - MARKER_LIFT + mm['cy']) * s) for p in geom['portals']],
                   'npc': [((x + mm['cx']) * s, (y - MARKER_LIFT + mm['cy']) * s) for _, x, y in geom.get('npcs', [])]}
        origin = minimap.place_picture(inner, pic_w, pic_h, dot, DOT_Y_OFFSET * s, markers, anchors)
    if origin is None:
        return None
    return (dot[0] - origin[0]) / s - mm['cx'], (dot[1] - origin[1]) / s - mm['cy'] + DOT_Y_OFFSET


def hidden_street_entrances(graph, maps):
    """{map id: [(x, y, destination name, portal is invisible)]}: portals from ordinary maps into
    "Hidden Street" maps. Most of them are invisible in game (portal type 1, or 10 = only up close)."""
    hidden = {m['id']: m['name'] for m in maps if m['street'].strip().lower() == 'hidden street'}
    out = {}
    for map_id, geom in graph.items():
        if map_id in hidden:
            continue
        for p in geom['portals']:
            if p['to'] in hidden:
                out.setdefault(map_id, []).append((p['x'], p['y'], hidden[p['to']], p.get('pt') in HIDDEN_PORTAL_TYPES))
    return out


def teleporters(graph):
    """{map id: [(x, y, exit x, exit y, touch)]}: portals that move you to another spot on the same map.
    The exit is None when the target portal isn't in the data; touch = works by walking into it."""
    out = {}
    for map_id, geom in graph.items():
        by_name = {p['name']: p for p in geom['portals']}
        for p in geom['portals']:
            if p['to'] == map_id and p.get('pt') != 0:
                dest = by_name.get(p['to_portal'])
                exit_xy = (dest['x'], dest['y']) if dest and (dest['x'], dest['y']) != (p['x'], p['y']) else (None, None)
                out.setdefault(map_id, []).append((p['x'], p['y'], *exit_xy, p.get('pt') == 3))
    return out


def _clip(x0, y0, x1, y1, box):
    """The part of the segment (x0, y0)-(x1, y1) inside box (left, top, right, bottom), or None."""
    t0, t1, dx, dy = 0.0, 1.0, x1 - x0, y1 - y0
    for p, q in ((-dx, x0 - box[0]), (dx, box[2] - x0), (-dy, y0 - box[1]), (dy, box[3] - y0)):
        if p == 0:
            if q < 0:
                return None
        elif p < 0:
            t0 = max(t0, q / p)
        else:
            t1 = min(t1, q / p)
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy) if t0 <= t1 else None


def _clear_of_dot(st, gh, seg):
    """True if a minimap drawing (point or segment x0, y0, x1, y1) stays clear of the player's dot.
    Drawing over the dot would hide it from our own minimap reading when the overlay is visible to
    screen capture (Show on stream), freezing the position."""
    if not st.get('dot'):
        return True
    px, py = st['dot']
    x0, y0, x1, y1 = seg
    dx, dy = x1 - x0, y1 - y0
    k = 0.0 if not (dx or dy) else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x0 + k * dx - px, y0 + k * dy - py) > gh * DOT_CLEARANCE


def _label_top(st, x, y, ry, s):
    """Where a marker's label starts: just under its ring, or lower when the character stands there, so
    the label doesn't cover the name tag (which the tag tracker needs to see)."""
    feet = st.get('feet')
    if feet and abs(feet[0] - x) < 90 * s and abs(feet[1] - y) < 50 * s:
        return feet[1] + NAME_TAG_CLEAR * s
    return y + ry + 6 * s


def _well_key(well):
    """Rough identity of a minimap candidate (position and size, to the nearest 10 px)."""
    return None if well is None else (well.x0 // 10, well.y0 // 10, well.w // 10, well.h // 10)


def feet_on_screen(geom, mx, my, gw, gh):
    """Rough estimate of where the character's feet are in the game window (map position mx, my).

    The game draws a 1366x768 view scaled to the window, so the world scale is gh / 768.
    (16 x minimap scale agrees on small maps, but big maps get a shrunk minimap.)
    The camera follows the player and is clamped to the map's view range; it also trails
    behind a moving character, which is why the name tag tracker refines this.
    """
    sw = gh / VIEW_H
    cam_x, cam_y = clamp_camera(geom, mx + CAM_X_OFFSET, my - CAM_Y_LIFT, gw, gh)
    return (mx - cam_x) * sw + gw / 2, (my - cam_y) * sw + gh / 2, sw


def ease_camera(cx, cy, tx, ty, dt):
    """Camera centre after easing toward (tx, ty) for dt seconds (see Tracker._camera)."""
    out = []
    for c, goal, tau in ((cx, tx, CAM_TAU[0]), (cy, ty, CAM_TAU[1])):
        gap = goal - c
        out.append(c + gap * (1 - math.exp(-dt / tau)) if abs(gap) >= CAM_DEAD else c)
    return out


def clamp_camera(geom, cx, cy, gw, gh):
    """Camera centre (map units) kept inside the map's view range, as the game does: a map smaller
    than the view on an axis is centred on that axis."""
    mm = geom['mm']
    sw = gh / VIEW_H
    left, top, right, bottom = geom['vr'] or (-mm['cx'], -mm['cy'], mm['w'] - mm['cx'], mm['h'] - mm['cy'])
    vw, vh = gw / sw, gh / sw
    cx = (left + right) / 2 if right - left <= vw else min(max(cx, left + vw / 2), right - vw / 2)
    cy = (top + bottom) / 2 if bottom - top <= vh else min(max(cy, top + vh / 2), bottom - vh / 2)
    return cx, cy


def grab(sct, left, top, width, height):
    shot = sct.grab({'left': int(left), 'top': int(top), 'width': max(1, int(width)), 'height': max(1, int(height))})
    return np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)


def _white_text(image):
    """Only the minimap title's white letters, black on white, enlarged and smoothed: for a title on a light
    background (Sleepywood's light blue made "Sleepywood" read as "-319")."""
    mask = image[..., :3].astype(np.int16).min(axis=2) > 215
    img = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8))
    return img.resize((img.width * 3, img.height * 3), Image.BICUBIC).filter(ImageFilter.GaussianBlur(1))


def _room_number(image, read):
    """The number at the end of a "Free Market <11>" title, or None. OCR drops it when reading the whole title
    (and then "Free Market" matches room 1), so it's read on its own: the title's last line is split into
    words by the gaps between its white letters, and the last word ("<11>") is read enlarged, letters only,
    after a printed word (OCR reads a lone number poorly)."""
    white = image[..., :3].astype(np.int16).min(axis=2) > 200
    lines, start = [], None
    for y in range(white.shape[0] + 1):
        on = y < white.shape[0] and white[y].sum() > 2
        if on and start is None:
            start = y
        elif not on and start is not None:
            if y - start > 5:
                lines.append((start, y))
            start = None
    if not lines:
        return None
    y0, y1 = lines[-1]
    cols, h = white[y0:y1].any(axis=0), y1 - y0
    words, first, last, gap = [], None, None, 0
    for x in range(len(cols) + 1):
        if x < len(cols) and cols[x]:
            first, last, gap = (x if first is None else first), x, 0
        elif first is not None:
            gap += 1
            if gap > h * 0.35 or x == len(cols):
                words.append((first, last + 1))
                first = None
    if len(words) < 3:  # "Free", "Market", "<n>"
        return None
    x0, x1 = words[-1]
    word = white[max(0, y0 - 3):y1 + 3, max(0, x0 - 3):x1 + 3]
    img = Image.fromarray(np.where(word, 0, 255).astype(np.uint8))
    img = img.resize((img.width * 5, img.height * 5), Image.BICUBIC).filter(ImageFilter.GaussianBlur(2))
    img = Image.fromarray(np.pad(np.asarray(img), 30, constant_values=255))
    canvas = Image.new('L', (img.width + 600, img.height), 255)
    ImageDraw.Draw(canvas).text((20, img.height // 4), 'Room', fill=0, font=ImageFont.truetype('arial.ttf', img.height // 2))
    canvas.paste(img, (560, 0))
    digits = re.findall(r'\d+', ' '.join(read(canvas.convert('RGB'))[1:] or ['']))
    return int(digits[0]) if digits and 1 <= int(digits[0]) <= 99 else None


class Ocr(threading.Thread):
    """Keeps one PowerShell OCR process alive; only the newest pending image is read.

    on_result(lines, tag, final) returns True once it has used a reading. A reading it can't match to a map
    is followed by readings of other versions of the image; the last one comes with final=True."""

    def __init__(self, on_result):
        super().__init__(daemon=True)
        self.on_result = on_result
        self.jobs = queue.Queue()
        self.proc = subprocess.Popen(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(OCR_PS1), '-Server'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW)

    def submit(self, image, tag):
        self.jobs.put((image, tag))

    def run(self):
        path = os.path.join(tempfile.gettempdir(), f'mscw_nav_header_{os.getpid()}.png')
        while True:
            job = self.jobs.get()
            while not self.jobs.empty():
                job = self.jobs.get()
            image, tag = job
            # The title's second line (the map name) is often missed at its real size, and when the
            # screen is dimmed (a fade or an NPC conversation): read it enlarged, then also with the
            # contrast stretched if the map name is still missing.
            big = Image.fromarray(image).resize((image.shape[1] * 2, image.shape[0] * 2), Image.LANCZOS)
            lines = self._read(big, path)
            if len([l for l in lines if len(l) >= 3]) < 2:
                lines = max(lines, self._read(ImageOps.autocontrast(big, cutoff=1), path), key=len)
            if any('free market' in l.lower() and 'entrance' not in l.lower() and not re.search(r'\d', l) for l in lines):
                room = _room_number(image, lambda img: self._read(img, path))  # OCR dropped "<n>": read it alone
                if room:
                    lines = [l + f' <{room}>' if 'free market' in l.lower() else l for l in lines]
            if self.on_result(lines, tag, False):
                continue
            # Not a map name (a light background can garble it): try other versions of the image, keeping the
            # first reading if none of them works out either.
            for make in (lambda: ImageOps.autocontrast(big, cutoff=1), lambda: _white_text(image)):
                if self.on_result(self._read(make(), path), tag, False):
                    break
            else:
                self.on_result(lines, tag, True)

    def _read(self, img, path):
        img.save(path)
        self.proc.stdin.write(path + '\n')
        self.proc.stdin.flush()
        lines = []
        while (line := self.proc.stdout.readline()) and line.strip() != '<<END>>':
            lines.append(line.strip())
        return lines


class Tracker(threading.Thread):
    def __init__(self, goal, graph, maps):
        super().__init__(daemon=True)
        self.goal, self.graph, self.maps = goal, graph, maps
        self.names = {m['id']: m['name'] for m in maps}
        self.full_names = {m['id']: f'{m["street"]}: {m["name"]}' for m in maps}
        # names shared by several maps told apart by a "<...>" label ("1st Accompaniment <1st Stage>",
        # "Free Market <11>"): for those, the label itself has to be read (see _label_read)
        self.labelled = collections.Counter(m['name'].split('<')[0].strip() for m in maps if '<' in m['name'])
        # maps whose titles are exactly the same (Maple Island's two "Mushroom Town"s): told apart by their minimaps
        titles = collections.defaultdict(list)
        for m in maps:
            if m['id'] in graph:
                titles[(m['street'].strip(), m['name'].strip())].append(m['id'])
        self.same_title = {i: ids for ids in titles.values() if len(ids) > 1 for i in ids}
        self.map_by_id = {m['id']: m for m in maps}
        self.state = {'visible': False}
        self.map_id = None
        self.last_pos = None  # (map_id, x, y, time) of the last time the dot was seen
        self.debug_saved, self.debug_last = 0, 0.0
        self.hwnd = None           # the game window
        self.own_hwnds = set()     # our picker windows; having them focused still counts as "in game"
        self.ocr_text = ''
        self.ocr = Ocr(self._on_ocr)
        self.ocr.start()
        self.tag = TagTracker(SyncOcr())
        self.arrived = None        # (label, time) of the last arrival, shown briefly
        self.goal_npc = None       # (x, y, name) when navigating to an NPC
        self.travel = travel.load()
        self.opts = {'scrolls': False, 'taxi': False, **load_prefs().get('opts', {})}
        self.goal_opts = {}        # route options for the current trip only (see set_goal)
        self.bar_sig = self.bar_pending = None  # the minimised minimap's title, as last read / waiting to settle
        self.bar_route = (None, None)  # (key, route) worked out while the minimap is minimised
        self.fm_return = None      # town the Free Market was entered from
        self.level = load_prefs().get('level')  # character level, re-read from the status bar
        self.last_level_read = 0.0
        self.watch_job = False     # read the job often: waiting for a job advancement (set by the overlay)
        self.kpq_panel = None      # (x0, y0, x1, y1) of the Kerning PQ answers panel
        self.kpq_blank = []        # what the overlay draws on that map (status box, banner, panel): kept out of the
                                   # dialog reads, since with "Show on stream" on it's in the screenshots
        self.character = None      # the name on the status bar ('' if it can't be read; None until it's known)
        self.name_candidate, self.name_misses = None, 0
        self.last_job_read = 0.0
        self.in_world, self.world_checked = True, 0.0  # status bar seen (see _in_game_world)
        self.level_candidate = None  # a new level reading waiting for a second one to agree
        self.job = None            # (job name on the status bar, job class), e.g. ('Archer', 'Bowman')
        self.quest_hint = None     # current quest step, shown under the status line
        self.map_changed_at = 0.0  # when the current map was entered
        self.cam = None            # (map_id, x, y, time): simulated camera centre in map units
        self.cam_target = None     # where the camera is heading (map units)
        self.dot_screen = None     # (x, y, map_id): last place the player's minimap dot was seen
        self.dot_hint, self.dot_hint_key = None, None  # last dot (minimap region coords), to look there first
        self.tag_far = 0           # name-tag readings in a row that disagree with the camera model
        self.ocr_fails, self.ocr_ok = {}, set()  # per minimap candidate: failed reads / ever read a map
        self.bad_wells = {}        # candidate -> time until which it's ignored
        self.force_rescan = False
        self.street_names = {map_norm(m['street']) for m in maps}
        self.reread = False         # read the minimap title again as soon as possible
        self.check_read = False     # the next title read is a routine re-check (a different map needs confirming)
        self.map_candidate = None   # a map a re-check read, waiting for a second read to agree
        self.last_title_read = 0.0
        self.hidden_streets = hidden_street_entrances(graph, maps)
        self.teleporters = teleporters(graph)
        self.show_hidden = True    # these two are set by the overlay from the picker's Markers options
        self.show_teleports = True
        self.mm_xform = None       # (map_id, x0, y0, scale): map units -> minimap pixels in the game window
        self.gather_spots = {}     # map -> [(x, y, name)]: things to break for the guided quest (set by the overlay)

    def set_options(self, opts):
        self.opts = {**self.opts, **opts}
        print(f'[nav] route options: {self.opts}')

    def set_goal(self, goal, npc=None, opts=None):
        """Navigate to map `goal`; with npc=(x, y, name), finish at that NPC on the map. opts: route options for
        this trip only, on top of the Search panel's (e.g. {'taxi': True})."""
        self.goal, self.goal_npc, self.goal_opts = goal, npc, opts or {}
        self.arrived = None
        name = self.full_names.get(goal, goal) if goal is not None else None
        if npc:
            name = f'{npc[2]} in {name}'
        print(f'[nav] destination: {goal} {name}' if goal is not None else '[nav] navigation stopped')

    # -- map identification ------------------------------------------------
    def _on_ocr(self, lines, well, final=True):
        """A reading of the minimap title. Returns True if it was used; False (only when not final) if it
        isn't a map name, so the caller can try another version of the image."""
        text = ' '.join(l for l in lines if len(l) >= 3 and not l.startswith('!')
                        and 'mini' not in l.lower() and 'world' not in l.lower())
        text = re.sub(r'\b[lIi|]st\b', '1st', text)  # OCR reads "1st" as "lst" ("<1st Stage>" matched "<Last Stage>")
        street_only = map_norm(text) in self.street_names
        score, m = (0, None) if street_only or not text else find_map(text, self.maps, limit=1)[0]
        matched = bool(m and score >= 0.6 and m['id'] in self.graph)
        if matched and m['id'] in self.same_title:
            m = self._by_minimap(m, well)
        if matched and not self._label_read(m['name'], text):
            matched = False  # e.g. "Free Market" without its room number: every room matches, so don't guess one
        if not matched and not final:
            return False
        self.ocr_text = text
        check, self.check_read = self.check_read, False
        if street_only:
            # Only the street line ("Victoria Road", shared by most maps) was read, e.g. while the title
            # was fading in: it can't say which map this is (it even equals one map's name). Read again.
            self.reread = True
            return True
        key = _well_key(well)
        if matched:
            self.ocr_fails[key] = 0
            self.ocr_ok.add(key)
            if m['id'] != self.map_id and check and self.map_id is not None and self.map_candidate != m['id']:
                # A routine re-read disagrees with the map we're on: believe it only if the next read agrees.
                self.map_candidate, self.reread, self.check_read = m['id'], True, True
                return True
            if m['id'] != self.map_id:
                print(f'[map] "{text}" -> {m["id"]} {m["street"]}: {m["name"]} ({score:.2f})')
            self.map_id, self.map_candidate = m['id'], None
            return True
        print(f'[map] could not match "{text}"' + (f' (best {m["name"]} {score:.2f})' if m else ''))
        if not check and self.map_id is not None and key in self.ocr_ok:
            # The title changed (this wasn't a routine re-read) and can't be read: we're somewhere new, but
            # not known where. Keeping the old map would draw its portals, hidden streets and teleporters here.
            print(f'[map] the title changed and can\'t be read; map unknown until it can (was {self.map_id})')
            self.map_id, self.map_candidate = None, None
        # A window that never reads as a map name (e.g. the inventory, whose frame looks like the
        # minimap's) is ignored for a while and the minimap is searched for again.
        self.ocr_fails[key] = self.ocr_fails.get(key, 0) + 1
        if well is not None and key not in self.ocr_ok and self.ocr_fails[key] >= 4:
            self.bad_wells[key] = time.time() + 30
            self.force_rescan = True
            print(f'[minimap] {well} does not look like the minimap; ignoring it for 30s')

    # -- main loop -----------------------------------------------------------
    def run(self):
        # An unexpected screen (Cash Shop, loading, a dialog) must never kill tracking: log the error and
        # start the loop over (it re-finds the minimap from scratch).
        while True:
            try:
                self._loop()
            except Exception:
                traceback.print_exc()
                self.state = {'visible': False}
                time.sleep(1.0)

    def _loop(self):
        sct = getattr(mss, 'MSS', mss.mss)()
        well, last_scan, last_sig, pending_sig = None, 0.0, None, None
        bar = None  # the minimap minimised to its title bar, (x0, y0, x1, y1), while there's no open one
        cached_route = (None, None)
        prev_map = None
        while True:
            t0 = time.time()
            hwnd, rect = game_client_rect()
            self.hwnd = hwnd
            fg = user32.GetForegroundWindow()
            if not hwnd or (fg != hwnd and fg not in self.own_hwnds):
                self.state = {'visible': False}
                time.sleep(0.25)
                continue
            gx, gy, gw, gh = rect
            if t0 - self.last_level_read > LEVEL_EVERY:
                self.last_level_read = self.last_job_read = t0
                self._read_level(sct, rect)
            elif self.watch_job and t0 - self.last_job_read > JOB_WATCH_EVERY:
                self.last_job_read = t0
                self._read_job(sct, rect)

            # Re-find the minimap about once a second (it can be moved, collapsed or closed).
            if self.force_rescan:
                well, bar, self.force_rescan = None, None, False
            if well is None and not self._in_game_world(sct, rect):
                # no minimap and no status bar: the Cash Shop, a loading screen or character select
                self.state = {'visible': False}
                time.sleep(0.5)
                continue
            if (well is None and bar is None) or t0 - last_scan > 1.0:
                last_scan = t0
                found = None
                if well is not None:  # cheap check around the last known spot first
                    m = well.band * 4
                    x0, y0 = max(0, well.x0 - m), max(0, well.y0 - m)
                    sub = grab(sct, gx + x0, gy + y0, min(gw - x0, well.w + 2 * m), min(gh - y0, well.h + 2 * m))
                    for w in minimap.find_wells(sub):
                        # Same minimap as before; a resized one (new map) goes through the full search.
                        if abs(w.w - well.w) <= 8 and abs(w.h - well.h) <= 8 and not self._bad(well):
                            found = minimap.Well(w.x0 + x0, w.x1 + x0, w.y0 + y0, w.y1 + y0, w.band)
                            break
                if found is None:
                    full = grab(sct, gx, gy, gw, gh)
                    expected = max(1, round(MM_BAND_UI * gh / VIEW_H))
                    cands = [w if w.band >= expected * 0.6 else minimap.Well(w.x0, w.x1, w.y0, w.y1, expected)
                             for w in minimap.find_wells(full) if not self._bad(w) and w.band <= gh * MM_MAX_BAND]
                    with_dot = [w for w in cands if minimap.find_dot(full, w, gh / VIEW_H)]
                    # Other windows (inventory, shop signs, chat bubbles) have similar frames. Prefer thick
                    # grey bands and size, then confirm by the "MINI MAP" title / "WORLD" button above it.
                    ordered = sorted(with_dot or cands, key=lambda w: (w.band >= gh * 0.005, w.w * w.h), reverse=True)
                    found = next((w for w in ordered if self._is_minimap(full, w)), None)
                    if found != well:
                        print(f'[minimap] full rescan: {len(cands)} candidates, using {found} (was {well})')
                    # no open minimap: is it minimised to its title bar? (the map's name can still be read there)
                    was, bar = bar, None if found else minimap.find_collapsed(full, gh / VIEW_H)
                    if bar and not was:
                        print(f'[minimap] minimised to its title bar at {bar}')
                well = found
                if well is not None:
                    bar = None
            if well is None and bar is not None:
                x0, y0, x1, y1 = bar
                strip = grab(sct, gx + x0, gy + y0, x1 - x0, y1 - y0)
                if not minimap.bar_shown(strip, gh / VIEW_H):
                    bar = None  # gone (opened, or a loading screen): look for the minimap again
                else:
                    self._minimised(strip, rect, bar, t0)
                    self._sleep(t0)
                    continue
            if well is None:
                self.state = {'visible': True, 'rect': rect, 'status': "Can't see the minimap - open and expand it (M)"}
                self._sleep(t0)
                continue

            # Fast path: header (map name) + map picture.
            hx0, hy0, hx1, hy1 = minimap.header_rect(well)
            region = grab(sct, gx + well.x0, gy + hy0, well.w, well.y1 - hy0)
            header = np.ascontiguousarray(region[: hy1 - hy0, hx0 - well.x0:])
            sig = header[::4, ::4].mean(axis=2)
            if self.reread or t0 - self.last_title_read > (TITLE_RETRY if self.map_id is None else TITLE_REREAD):
                # Read the title again even though it looks the same: the last read may have been bad (only
                # its first line, mid-fade), and a misread map would route from portals that aren't there.
                self.check_read = self.check_read or not self.reread
                self.reread, last_sig = False, None
                self.last_title_read = t0
            if last_sig is None or sig.shape != last_sig.shape or np.abs(sig - last_sig).mean() > 4:
                # Header changed: OCR once it has been stable for one frame (map transition fade).
                if pending_sig is not None and sig.shape == pending_sig.shape and np.abs(sig - pending_sig).mean() <= 2:
                    self.ocr.submit(header.copy(), well)
                    self.last_title_read = t0
                    last_sig = sig
                    pending_sig = None
                else:
                    pending_sig = sig
            if self.map_id in self.same_title:
                # walking between maps with the same title (the two Mushroom Towns) leaves the title as it was, so
                # it isn't read again: the minimap's size says which one this is
                best = self._by_minimap(self.map_by_id[self.map_id], well)['id']
                if best != self.map_id:
                    print(f'[map] same title, other minimap -> {best} {self.full_names.get(best)}')
                    self.map_id = best
            local = minimap.Well(0, well.w, well.y0 - hy0, well.y1 - hy0, well.band)
            dot = minimap.find_dot(region, local, gh / VIEW_H, self.dot_hint if self.dot_hint_key == _well_key(well) else None)
            self.dot_hint, self.dot_hint_key = dot, _well_key(well)
            if dot is not None:  # where the dot is in the game window, so the overlay never draws over it
                self.dot_screen = (well.x0 + dot[0], hy0 + dot[1], self.map_id)
            if dot is None and DEBUG_DIR.is_dir() and self.debug_saved < 30 and t0 - self.debug_last > 1.0:
                self.debug_last = t0
                self.debug_saved += 1
                name = DEBUG_DIR / f'nodot_{time.strftime("%H%M%S")}_{self.debug_saved:02d}.png'
                Image.fromarray(region).save(name)
                print(f'[dot] not found; map {self.map_id}, well {well}, last seen {self.last_pos}; saved {name.name}')

            st = {'visible': True, 'rect': rect, 'map': self.map_id,
                  'minimap': (well.x0, max(0, well.y0 - well.band * 15), well.x1, well.y1 + well.band * 2),
                  'dot': self.dot_screen[:2] if self.dot_screen and self.dot_screen[2] == self.map_id else None}
            geom = self.graph.get(self.map_id) if self.map_id else None
            if geom is None or not geom.get('mm'):
                st['status'] = 'Reading map name...' if not self.ocr_text else f'Unknown map: "{self.ocr_text}"'
                self.state = st
                self._sleep(t0)
                continue
            if self.map_id != prev_map:
                # The Free Market exit leads back to the town you came in from, so remember it.
                if self.map_id == FM_ENTRANCE and prev_map in SCRIPT_TARGETS['market00']:
                    self.fm_return = prev_map
                prev_map = self.map_id
                self.map_changed_at = t0
                self.tag.reset()  # the camera jumps on a map change
            if self.goal is not None and self.map_id == self.goal and not self.goal_npc:
                # Arrived: navigation ends here.
                self._arrive(self.full_names.get(self.map_id, self.map_id), t0)
            if self.goal is None:
                if self.arrived and t0 - self.arrived[1] < ARRIVED_SHOW:
                    st['status'] = f'Arrived: {self.arrived[0]}'
                    st['hint'] = 'Navigation finished - click Search for a new destination'
                else:
                    st['status'] = f'You are in: {self.full_names.get(self.map_id, self.map_id)}'
                    st['hint'] = 'Click Search to pick a destination'
                pos = self._player_pos(region, local, dot, geom, gh, t0, well, hy0)
                if pos is not None and not st['status'].startswith('Arrived'):
                    st['status'] += f'   (x {pos[0]:.0f}, y {pos[1]:.0f})'
                if pos is not None and self._wants_markers():
                    # Not navigating, but this map has hidden street entrances or teleporters to mark.
                    fx, fy, sw = self._feet(sct, rect, geom, pos[0], pos[1], t0)
                    st['cam'], st['feet'] = self._camera_state(sw), (fx, fy)
                    self._map_markers(st, pos[0], pos[1], fx, fy, sw, well)
                self.state = st
                self._sleep(t0)
                continue
            goal_name = self.names.get(self.goal, self.goal)
            if self.goal_npc:
                goal_name = f'{self.goal_npc[2]} ({goal_name})'
            if self.goal_npc and self.map_id == self.goal:
                # Final leg: walk to the NPC on this map.
                steps = [(self.map_id, {'kind': 'npc', 'npc': self.goal_npc[:2], 'name': self.goal_npc[2]}),
                         (self.map_id, None)]
            else:
                opts = {**self.opts, **self.goal_opts, 'fm_return': self.fm_return}
                key = (self.map_id, self.goal, tuple(sorted(opts.items())))
                if cached_route[0] != key:
                    cached_route = (key, route(self.graph, self.map_id, self.goal, opts, self.travel))
                steps = cached_route[1]
            if not steps:
                st['status'] = f'No route from here to {goal_name}'
                st['hint'] = 'Try enabling return scrolls or taxis in the Search panel'
                self.state = st
                self._sleep(t0)
                continue
            step, next_map = steps[0][1], steps[1][0]
            hops = len(steps) - 1
            next_name = self.names.get(next_map, next_map)
            st['status'] = f'To {goal_name}: {self._step_text(step, next_name)}'
            if step['kind'] != 'npc':
                st['status'] += f'  ({hops} step{"s" * (hops > 1)})'
            # Player position in map units.
            pos = self._player_pos(region, local, dot, geom, gh, t0, well, hy0)
            if pos is None:
                st['status'] += ('  - no player dot on minimap' if dot is None
                                 else "  - can't place you on this minimap")
                self.state = st
                self._sleep(t0)
                continue
            mx, my, hidden = pos
            # What to head for: the cab NPC, or the nearest portal to the next map (scrolls need nothing).
            target, here_text, radius = None, None, ARRIVE_RADIUS
            if step['kind'] in ('taxi', 'npc', 'boat'):
                name = (step.get('name') or 'the Regular Cab') if step['kind'] == 'taxi' else step['name']
                target, here_text = tuple(step['npc']), f'Talk to {name}'
                radius = (HIDDEN_ARRIVE_RADIUS if hidden else ARRIVE_RADIUS) + 40
            elif step['kind'] == 'portal':
                portals = [p for p in geom['portals'] if next_map in _targets(p)]
                if not portals:
                    st['status'] += '  - no portal found in map data'
                    self.state = st
                    self._sleep(t0)
                    continue
                p = min(portals, key=lambda p: (p['x'] - mx) ** 2 + (p['y'] - my) ** 2)
                target, here_text = (p['x'], p['y']), 'Press Up'
                radius = HIDDEN_ARRIVE_RADIUS if hidden else ARRIVE_RADIUS

            fx, fy, sw = self._feet(sct, rect, geom, mx, my, t0)
            st['cam'], st['feet'] = self._camera_state(sw), (fx, fy)
            st.update({'char': (fx, fy - BODY_HEIGHT * sw), 'scale': sw})
            self._map_markers(st, mx, my, fx, fy, sw, well)
            if target is None:
                st['action'] = f'Use {step["item"]}'
            else:
                st.update({'dir': (target[0] - mx, target[1] - my), 'here_text': here_text,
                           'here': math.hypot(target[0] - mx, target[1] - my) < radius,
                           # where the portal / NPC stands on screen, measured from the character's feet
                           'target_screen': (fx + (target[0] - mx) * sw, fy + (target[1] - my) * sw)})
                if step['kind'] == 'npc' and st['here']:
                    self._arrive(f'{step["name"]} - talk to them', t0)
            self.state = st
            self._sleep(t0)

    @staticmethod
    def _step_text(step, next_name):
        """What to do for a route step, e.g. "next Kerning City" or "take Phil's taxi to Kerning City"."""
        return {'scroll': f'use {step.get("item")}',
                'taxi': f"take {step['name']}'s taxi to {next_name}" if step.get('name')
                        else f'take the Regular Cab to {next_name}',
                'boat': f"take {step.get('name')}'s boat to {next_name}" + (f' ({step["note"]})' if step.get('note') else ''),
                'npc': f'{step.get("name")} is on this map'}.get(step['kind'], f'next {next_name}')

    def _minimised(self, strip, rect, bar, t0):
        """The minimap minimised to its title bar (the game does that on maps without a minimap picture, like shops
        and the job instructors' rooms, and on some big ones): which map this is comes from the bar's title, read
        like the open minimap's. With no dot to place you there's no arrow, but navigation still says what's next,
        and arriving on the map you're headed for ends it."""
        sig = strip[::4, ::4].mean(axis=2)
        if self.reread or t0 - self.last_title_read > (TITLE_RETRY if self.map_id is None else TITLE_REREAD):
            self.check_read = self.check_read or not self.reread
            self.reread, self.bar_sig = False, None
            self.last_title_read = t0
        if self.bar_sig is None or sig.shape != self.bar_sig.shape or np.abs(sig - self.bar_sig).mean() > 4:
            # read it once it has been the same for one frame (a map change fades it)
            p = self.bar_pending
            if p is not None and sig.shape == p.shape and np.abs(sig - p).mean() <= 2:
                self.ocr.submit(np.ascontiguousarray(strip), None)
                self.last_title_read, self.bar_sig, self.bar_pending = t0, sig, None
            else:
                self.bar_pending = sig
        st = {'visible': True, 'rect': rect, 'map': self.map_id, 'minimap': bar}
        geom = self.graph.get(self.map_id) if self.map_id else None
        hint = ("This map has no minimap picture, so there's no arrow here" if geom and not geom.get('mm')
                else 'The minimap is minimised: press + on it to see where you are')
        if geom is not None and self.goal is not None and self.map_id == self.goal:
            self._arrive(f'{self.goal_npc[2]} - talk to them, on this map' if self.goal_npc else
                         self.full_names.get(self.map_id, self.map_id), t0)
        if geom is None:
            st['status'] = 'Reading map name...' if not self.ocr_text else f'Unknown map: "{self.ocr_text}"'
        elif self.goal is None:
            if self.arrived and t0 - self.arrived[1] < ARRIVED_SHOW:
                st['status'] = f'Arrived: {self.arrived[0]}'
                st['hint'] = 'Navigation finished - click Search for a new destination'
            else:
                st['status'] = f'You are in: {self.full_names.get(self.map_id, self.map_id)}'
                st['hint'] = hint
        else:
            goal_name = self.names.get(self.goal, self.goal)
            if self.goal_npc:
                goal_name = f'{self.goal_npc[2]} ({goal_name})'
            opts = {**self.opts, **self.goal_opts, 'fm_return': self.fm_return}
            key = (self.map_id, self.goal, tuple(sorted(opts.items())))
            if self.bar_route[0] != key:
                self.bar_route = (key, route(self.graph, self.map_id, self.goal, opts, self.travel))
            steps = self.bar_route[1]
            if not steps:
                st['status'] = f'No route from here to {goal_name}'
            else:
                hops = len(steps) - 1
                st['status'] = (f'To {goal_name}: {self._step_text(steps[0][1], self.names.get(steps[1][0], steps[1][0]))}'
                                f'  ({hops} step{"s" * (hops > 1)})')
                st['hint'] = hint
        self.state = st

    def _arrive(self, label, t0):
        self.goal, self.goal_npc, self.arrived = None, None, (label, t0)
        print(f'[nav] arrived: {label}; navigation stopped')

    def _player_pos(self, region, local, dot, geom, gh, t0, well, hy0):
        """(x, y, dot hidden) of the character's feet in map units, or None if unknown.

        The last position is held while the dot briefly disappears, and while it's hidden under a
        portal or NPC marker (standing on one). Also records how map units map onto the minimap."""
        pos = map_position(region, local, dot, geom, gh, self.map_id) if dot is not None else None
        if pos is not None:
            mx, my = pos
            s = gh / VIEW_H / 2 ** geom['mm']['mag']
            # minimap pixel (game window coords) = origin + map position * s (from the dot itself: the minimap's
            # own markers are drawn against it)
            self.mm_xform = (self.map_id, well.x0 + dot[0] - mx * s, hy0 + dot[1] - (my - DOT_Y_OFFSET) * s, s)
            my = self._on_ground(geom, mx, my)
            self.last_pos = (self.map_id, mx, my, t0)
            return mx, my, False
        if self.last_pos and self.last_pos[0] == self.map_id and (
                t0 - self.last_pos[3] < LOST_HOLD
                or (dot is None and self._near_marker(geom, *self.last_pos[1:3]))):
            return self.last_pos[1], self.last_pos[2], dot is None
        return None

    @staticmethod
    def _on_ground(geom, mx, my):
        """The height of the platform the character stands on, if there's one just under the minimap dot's reading
        (which is coarse, and reads a little high); else the reading itself (climbing, or in mid-air)."""
        best = None
        for x1, y1, x2, y2 in geom.get('fh') or ():
            if min(x1, x2) <= mx <= max(x1, x2):
                y = y1 + (y2 - y1) * (mx - x1) / (x2 - x1)
                if GROUND_BELOW[0] <= y - my <= GROUND_BELOW[1] and (
                        best is None or abs(y - my - GROUND_BIAS) < abs(best - my - GROUND_BIAS)):
                    best = y
        return my if best is None else best

    def _feet(self, sct, rect, geom, mx, my, t0):
        """(x, y, world scale): where the character's feet are in the game window.

        Everything drawn on the map is placed relative to the camera, which is simulated (see
        _camera) so markers glide with the scenery instead of following every step of the minimap
        dot. A confidently found name tag shows where the camera really is and pulls the model in."""
        gx, gy, gw, gh = rect
        sw = gh / VIEW_H
        cx, cy = self._camera(geom, mx, my, gw, gh, t0)
        fx, fy = (mx - cx) * sw + gw / 2, (my - cy) * sw + gh / 2
        seen = self.tag.locate(lambda x, y, w, h: grab(sct, gx + x, gy + y, w, h), (fx, fy), sw, gw, gh)
        if seen:
            # camera centre implied by the tag (the feet are at seen)
            tx, ty = mx - (seen[0] - gw / 2) / sw, my - (seen[1] - gh / 2) / sw
            gap = math.hypot(tx - cx, ty - cy)
            if gap > TAG_MAX:
                self.tag.box, self.tag_far = None, 0  # not the character's tag: look around the prediction next time
                return fx, fy, sw
            if gap < TAG_TRUST:
                self.tag_far = 0
            else:
                self.tag_far += 1  # far from the model: believe it only if it keeps saying so
            # (small disagreements are just the minimap dot's coarseness; correcting them would shake)
            if gap > TAG_DEADBAND and (self.tag_far == 0 or self.tag_far >= TAG_FAR_FRAMES):
                pull = TAG_PULL_FAR if gap > TAG_PULL_FROM else TAG_PULL
                cx, cy = clamp_camera(geom, cx + (tx - cx) * pull, cy + (ty - cy) * pull, gw, gh)
                self.cam = (self.map_id, cx, cy, t0)
                fx, fy = (mx - cx) * sw + gw / 2, (my - cy) * sw + gh / 2
        else:
            self.tag_far = 0
        return fx, fy, sw

    def _camera_state(self, sw):
        """(x, y, time, target x, target y, world scale): lets the overlay keep easing the camera between
        tracker updates, so markers move smoothly at the overlay's frame rate."""
        return (*self.cam[1:], *self.cam_target, sw)

    def _camera(self, geom, mx, my, gw, gh, t0):
        """Simulated camera centre in map units. The game's camera eases toward a point just above the
        character, closing a fixed share of the gap each tick, so it trails while walking (~115 units at
        walking speed) and catches up after stopping; it starts centred on entering a map."""
        target = clamp_camera(geom, mx + CAM_X_OFFSET, my - CAM_Y_LIFT, gw, gh)
        self.cam_target = target
        if self.cam is None or self.cam[0] != self.map_id:
            self.cam = (self.map_id, *target, t0)
            return target
        _, cx, cy, last = self.cam
        cx, cy = clamp_camera(geom, *ease_camera(cx, cy, *target, min(0.5, max(0.0, t0 - last))), gw, gh)
        self.cam = (self.map_id, cx, cy, t0)
        return cx, cy

    def _wants_markers(self):
        return bool((self.show_hidden and self.hidden_streets.get(self.map_id))
                    or (self.show_teleports and self.teleporters.get(self.map_id)) or self.gather_spots.get(self.map_id))

    def _map_markers(self, st, mx, my, fx, fy, sw, well):
        """Add this map's hidden street entrances, teleporters and things to break for the quest to the state:
        where they are on screen, and on the minimap (where it would draw a portal ring: above the real spot)."""
        on_screen = lambda x, y: (fx + (x - mx) * sw, fy + (y - my) * sw)
        xform = self.mm_xform if self.mm_xform and self.mm_xform[0] == self.map_id else None
        on_mm = lambda x, y: (xform[1] + x * xform[3], xform[2] + (y - MARKER_LIFT) * xform[3])
        view = minimap_viewport(well, sw * VIEW_H)
        inside = lambda p: view.x0 <= p[0] <= view.x1 and view.y0 <= p[1] <= view.y1
        hidden = self.hidden_streets.get(self.map_id, []) if self.show_hidden else []
        if hidden:
            st['hidden'] = [(*on_screen(x, y), label, invisible) for x, y, label, invisible in hidden]
            if xform:
                st['hidden_mm'] = [p for p in (on_mm(x, y) for x, y, _, _ in hidden) if inside(p)]
        gather = self.gather_spots.get(self.map_id, [])
        if gather:
            st['gather'] = [(*on_screen(x, y), name) for x, y, name in gather]
            if xform:
                st['gather_mm'] = [p for p in (on_mm(x, y) for x, y, _ in gather) if inside(p)]
        teleports = self.teleporters.get(self.map_id, []) if self.show_teleports else []
        if teleports:
            st['teleports'] = [(*on_screen(x, y), None if ex is None else math.atan2(ey - y, ex - x), touch)
                               for x, y, ex, ey, touch in teleports]
            if xform:
                marks, lines, seen = [], [], set()
                for x, y, ex, ey, _ in teleports:
                    p = on_mm(x, y)
                    if inside(p):
                        marks.append(p)
                    if ex is not None and frozenset(((x, y), (ex, ey))) not in seen:  # a pair needs one line
                        seen.add(frozenset(((x, y), (ex, ey))))
                        seg = _clip(*p, *on_mm(ex, ey), (view.x0, view.y0, view.x1, view.y1))
                        if seg:
                            lines.append(seg)
                st['teleports_mm'] = (marks, lines)

    def _in_game_world(self, sct, rect):
        """Is the status bar on screen (so the player is in the game world, not in the Cash Shop, on a loading
        screen or at character select)? Read at most every WORLD_CHECK_EVERY seconds."""
        now = time.time()
        if now - self.world_checked >= WORLD_CHECK_EVERY:
            self.world_checked = now
            gx, gy, gw, gh = rect
            strip = grab(sct, gx + int(gw * 0.25), gy + int(gh * 0.955), int(gw * 0.5), gh - int(gh * 0.955))
            words = [w[0].upper() for w in self.tag.ocr.words(strip)]
            in_world = sum(any(w.startswith(p) for w in words) for p in STATUS_BAR_WORDS) >= 2
            if in_world != self.in_world:
                print('[world] back in the game world' if in_world else
                      '[world] no minimap or status bar (Cash Shop, loading screen...): overlay hidden')
            self.in_world = in_world
        return self.in_world

    def _read_job(self, sct, rect, bar=None):
        """The job name on the status bar (e.g. 'Rogue', class Thief); returns the bar's picture."""
        gx, gy, gw, gh = rect
        if bar is None:
            bar = grab(sct, gx, gy + int(gh * 0.90), int(gw * 0.35), gh - int(gh * 0.90))
        words = self.tag.ocr.words(bar)
        job = statusbar.read_job(words)
        if job and job != self.job:
            print(f'[level] job: {job[0]} ({job[1]})')
            self.job = job
        self._read_name(words)
        return bar

    def _read_name(self, words):
        """Which character this is, from the name under the job on the status bar. A name it hasn't seen before
        counts once a second read agrees; a near miss of a known name of the same length (one letter misread) is
        that name. If no name can be read in the first few tries, '' (one profile for every character)."""
        name = statusbar.read_name(words)
        known = list(load_prefs().get('characters') or {})
        if name and name not in known:
            close = [k for k in known if len(k) == len(name) and difflib.SequenceMatcher(None, k, name).ratio() >= 0.8]
            name = close[0] if close else name
        if name and name == self.character:
            self.name_candidate = None
            return
        soon = time.time() - LEVEL_EVERY + LEVEL_CONFIRM_AFTER  # read the status bar again in a moment
        if name and (name in known or name == self.name_candidate):
            print(f'[character] {name}')
            self.character, self.name_candidate = name, None
            self.tag.set_name(name)
        elif name:
            self.name_candidate, self.last_level_read = name, soon
        elif self.character is None:
            self.name_misses += 1
            if self.name_misses >= 5:
                print("[character] can't read the name on the status bar: one profile for every character")
                self.character = ''
            else:
                self.last_level_read = soon

    def _read_level(self, sct, rect):
        bar = self._read_job(sct, rect)
        level = statusbar.read_level(bar, self.tag.ocr)
        if not level or level == self.level:
            self.level_candidate = None
            return
        if level != self.level_candidate:
            # A new level is only believed when the next reading agrees (misreads like 1 for 12 or 111
            # for 11 happen); check again soon rather than in LEVEL_EVERY seconds.
            self.level_candidate = level
            self.last_level_read = time.time() - LEVEL_EVERY + LEVEL_CONFIRM_AFTER
            return
        print(f'[level] {level}')
        self.level, self.level_candidate = level, None
        save_prefs(level=level)

    def _by_minimap(self, m, well):
        """Of the maps with exactly this title, the one whose minimap picture is as tall as the minimap on screen
        (the window fits the picture's height; Maple Island's two Mushroom Towns differ by ~15 px at 4K)."""
        if well is None:  # minimised: no picture to measure, so keep the one we're on if it's one of them
            return self.map_by_id.get(self.map_id, m) if self.map_id in self.same_title[m['id']] else m
        gh = (self.state.get('rect') or (0, 0, 0, VIEW_H))[3]

        def misfit(map_id):
            mm = self.graph[map_id]['mm']
            return abs(mm['h'] * gh / VIEW_H / 2 ** mm['mag'] - well.h)
        # on a tie (maps with the same size too, like the Coconut Harvest event maps) keep the one it was
        best = min(self.same_title[m['id']], key=lambda i: (round(misfit(i)), i != m['id']))
        return self.map_by_id.get(best, m)

    def _label_read(self, name, text):
        """For a map told apart from others only by its "<...>" label, was that label read? (Its words, or its
        number, in the read title.) True for every other map."""
        if '<' not in name or self.labelled.get(name.split('<')[0].strip(), 0) < 2:
            return True
        label = ' '.join(re.sub(r'[^a-z0-9 ]', ' ', name[name.index('<'):].lower()).split())
        read = ' '.join(re.sub(r'[^a-z0-9 ]', ' ', text.lower()).split())
        return bool(re.search(rf'\b{re.escape(label)}\b', read)) or \
            difflib.SequenceMatcher(None, label, read[-len(label):]).ratio() >= 0.8

    def _is_minimap(self, full, well):
        """Read the title area above a candidate once: the minimap has "MINI MAP", a "WORLD" button and
        the map's name (the inventory, for one, has a similar frame but says "ITEM INVENTORY")."""
        key = _well_key(well)
        if key in self.ocr_ok:
            return True
        if self._bad(well):
            return False
        top = max(0, int(well.y0 - well.band * 17))
        text = ' '.join(w[0] for w in self.tag.ocr.words(np.ascontiguousarray(
            full[top:max(top + 1, int(well.y0 - well.band)), well.x0:well.x1]))).lower()
        if 'mini' in text or 'world' in text or (text and find_map(text, self.maps, limit=1)[0][0] >= 0.6):
            self.ocr_ok.add(key)  # its title, its WORLD button or the map name it shows
            return True
        # The map name under it, read the way the map title is (a title on a light background, like
        # Sleepywood's, only reads in a higher-contrast or letters-only version).
        hx0, hy0, hx1, hy1 = minimap.header_rect(well)
        header = np.ascontiguousarray(full[hy0:hy1, hx0:hx1])
        if header.shape[0] > 4 and header.shape[1] > 4:
            big = Image.fromarray(header).resize((header.shape[1] * 2, header.shape[0] * 2), Image.LANCZOS)
            for make in (lambda: big, lambda: ImageOps.autocontrast(big, cutoff=1), lambda: _white_text(header)):
                name = ' '.join(self.tag.ocr.lines(np.asarray(make().convert('RGB'))))
                if name.strip() and find_map(name, self.maps, limit=1)[0][0] >= 0.6:
                    self.ocr_ok.add(key)
                    return True
        # a title that reads as nothing (mid-redraw, after a map change) says nothing either way: look again soon
        self.bad_wells[key] = time.time() + (30 if text.strip() else 2)
        print(f'[minimap] {well} is not the minimap (title reads "{text[:40]}")')
        return False

    def _bad(self, well):
        return self.bad_wells.get(_well_key(well), 0) > time.time()

    @staticmethod
    def _near_marker(geom, x, y):
        spots = [(p['x'], p['y']) for p in geom['portals']] + [(nx, ny) for _, nx, ny in geom.get('npcs', [])]
        return any(math.hypot(sx - x, sy - y) < HIDDEN_ARRIVE_RADIUS for sx, sy in spots)

    @staticmethod
    def _sleep(t0):
        time.sleep(max(0.0, 1 / TRACK_HZ - (time.time() - t0)))


def kpq_question(text):
    """Cloto's question in some dialog text, whole, or ''. She always words it "Here's the question. Defeat the monsters
    here and collect the same number of coupons as the level required to make the second job advancement in
    MapleStory.", so it's what follows that opening, up to the end of the sentence asking for coupons. Nothing else
    counts: chat, item messages ("coupon has been lost") and the stage's instruction ("collect exactly as many Coupons
    as Cloto's answer") all mention coupons too. (Read once a second, a misread opening is soon read right.)"""
    flat = ' '.join(text.split())
    m = re.search(r'here.{0,2}s the question|here is the question', flat, re.I)
    if not m:
        return ''
    rest = flat[m.end():].lstrip(' .:!')
    end = re.search(r'coupon[^.?!]*[.?!]', rest, re.I)
    q = (rest[:end.end()] if end else rest[:300]).strip()
    return q if re.search(r'coupon', q, re.I) else ''


def kpq_answer(text):
    """(coupons, confirmed, dialog text, KPQ_QUESTIONS index) for Cloto's question in some dialog text, or None."""
    t = ' '.join(re.sub(r'[^a-z0-9 ]', ' ', kpq_question(text).lower()).split())
    if not t:
        return None
    for i in KPQ_MATCH_ORDER:
        _, coupons, confirmed, keys = KPQ_QUESTIONS[i]
        if any(all(re.search(rf'\b{re.escape(w)}\b', t) for w in words) for words in keys):
            levels = re.search(r'level (\d+) to level (\d+)', t)
            if KPQ_QUESTIONS[i][0].startswith('AP ') and levels and int(levels.group(2)) > int(levels.group(1)):
                coupons = (int(levels.group(2)) - int(levels.group(1))) * KPQ_AP  # AP between the levels she names
            return coupons, confirmed, text, i
    return None


class KpqReader(threading.Thread):
    """On Kerning PQ's first stage, reads the middle of the game window (where Cloto's dialog opens) about once
    a second. `answer` is (coupons, confirmed, KPQ_QUESTIONS index) from Cloto's question, kept while you stay on
    the map; `unknown` is the question she asked if it isn't one in KPQ_QUESTIONS. Every question read goes into
    KPQ_LOG (with a picture of the dialog in KPQ_DIALOGS), so the list can be checked against what she really asks."""

    def __init__(self, tracker):
        super().__init__(daemon=True)
        self.tracker, self.answer, self.unknown = tracker, None, None
        self.logged = set()

    def _record(self, img, text, found):
        """Write a question down the first time it's read (one line in KPQ_LOG, a half-size picture in KPQ_DIALOGS)."""
        question = kpq_question(text)
        key = ' '.join(re.sub(r'[^a-z0-9 ]', ' ', question.lower()).split())
        if not key or key in self.logged:
            return
        self.logged.add(key)
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        what = f'{found[0]} coupons: {KPQ_QUESTIONS[found[3]][0]}' if found else 'NOT IN THE LIST'
        try:
            with KPQ_LOG.open('a', encoding='utf-8') as f:
                f.write(f'{stamp} | {what} | {question}\n')
            KPQ_DIALOGS.mkdir(exist_ok=True)
            Image.fromarray(img[::2, ::2]).save(KPQ_DIALOGS / f'cloto_{time.strftime("%Y%m%d_%H%M%S")}.png')
        except OSError as ex:
            print(f'[kpq] could not record the question: {ex!r}')
        print(f'[kpq] question recorded ({what}): "{question}"')

    def run(self):
        ocr, sct = SyncOcr(), getattr(mss, 'MSS', mss.mss)()
        while True:
            st = self.tracker.state
            if self.tracker.map_id != KPQ_STAGE1:
                self.answer = self.unknown = None  # a new run starts with a new question
                time.sleep(1.0)
                continue
            if not st.get('visible'):
                time.sleep(0.5)
                continue
            gx, gy, gw, gh = st['rect']
            x0, y0 = int(gw * 0.15), int(gh * 0.12)
            try:  # (down to just above the chat box: its lines talk about coupons too)
                img = np.array(grab(sct, gx + x0, gy + y0, int(gw * 0.7), int(gh * 0.66)))
                # our windows and the answers panel (it lists every question!), if they're in the capture
                for ox0, oy0, ox1, oy1 in itemhover.own_rects(self.tracker) + list(self.tracker.kpq_blank):
                    img[max(0, oy0 - y0):max(0, oy1 - y0), max(0, ox0 - x0):max(0, ox1 - x0)] = 255
                text = ' '.join(ocr.lines(img))
                found = kpq_answer(text)
                if kpq_question(text):
                    self._record(img, text, found)
                    if not found and kpq_question(text):
                        self.unknown, self.answer = kpq_question(text), None
                if found and (found[0], found[1], found[3]) != self.answer:
                    self.unknown = None
                    print(f'[kpq] Cloto: collect {found[0]} coupons ({"confirmed" if found[1] else "unconfirmed"}): '
                          f'"{found[2][:160]}"')
                    self.answer = (found[0], found[1], found[3])
            except Exception as ex:  # keep reading
                print(f'[kpq] read failed: {ex!r}')
            time.sleep(KPQ_EVERY)


def leveling_text(follow):
    """'Lv 10-12 Mushroom Garden' for the stage a route is at."""
    return f'{leveling.levels_text(follow.current)} {follow.current["title"]}'


class QuestLogReader(threading.Thread):
    """Reads the game's Quest Helper box every few seconds (its own thread, so OCR never stalls the arrow).

    OCR of the semi-transparent box flickers (quests missed or misread on single passes), so results are
    smoothed over the last few reads: a quest counts as present if seen in 2 of the last 4 reads, and as
    gone only after 4 reads in a row without it.

    entries: smoothed quests (see questlog.parse), or None while the box can't be seen."""

    def __init__(self, tracker, qdata):
        super().__init__(daemon=True)
        self.tracker, self.qdata = tracker, qdata
        self.entries, self.updated, self.title = None, 0.0, None
        self.history = collections.deque(maxlen=LOG_HISTORY)  # {qid: entry} per read with the box visible

    def _grab(self, sct, gx, gy, x, y, w, h):
        """Part of the game window, with the navigator's own windows painted white. With "Show on stream" on
        they're in the capture, and its quest panel (headed "QUEST HELPER", listing quest names) would
        otherwise be read as the game's box."""
        img = np.array(grab(sct, gx + x, gy + y, w, h))
        for ox0, oy0, ox1, oy1 in itemhover.own_rects(self.tracker):
            img[max(0, oy0 - y):max(0, oy1 - y), max(0, ox0 - x):max(0, ox1 - x)] = 255
        return img

    def gone(self, qid):
        """True once qid has been missing from the last LOG_GONE reads in a row."""
        recent = list(self.history)[-LOG_GONE:]
        return len(recent) == LOG_GONE and all(qid not in r for r in recent)

    def _smoothed(self):
        reads = list(self.history)
        recent = reads[-4:]
        need = 1 if len(recent) < 2 else 2
        order = [qid for r in reversed(reads) for qid in r]
        present = [q for q in dict.fromkeys(order) if sum(q in r for r in recent) >= need]
        entries = []
        for qid in present:
            latest = next(r[qid] for r in reversed(reads) if qid in r)
            progress, settled = {}, True
            for key, (_, need) in latest['progress'].items():
                # Middle value of the last 3 readable counts (lower one of 2), so one misread can't
                # move a step on; "done" also needs at least 2 readings.
                seen = [r[qid]['progress'][key][0] for r in reversed(reads)
                        if qid in r and r[qid]['progress'].get(key, (None,))[0] is not None][:3]
                have = sorted(seen)[(len(seen) - 1) // 2] if seen else None
                progress[key] = (have, need)
                settled = settled and len(seen) >= 2
            known = [hn for hn in progress.values() if hn[0] is not None]
            q = self.qdata['quests'][qid]
            reqs = len(q['mobs']) + len(q['items'])
            entries.append({**latest, 'progress': progress,
                            'complete': bool(reqs) and settled and len(known) == reqs
                            and all(h >= n for h, n in known)})
        return entries

    def run(self):
        ocr, sct = SyncOcr(), getattr(mss, 'MSS', mss.mss)()
        while True:
            st = self.tracker.state
            if not st.get('visible'):
                time.sleep(1.0)
                continue
            gx, gy, gw, gh = st['rect']
            try:
                title, read_entries = questlog.read_entries(lambda x, y, w, h: self._grab(sct, gx, gy, x, y, w, h), gw, gh,
                                                            ocr, self.qdata, self.title)
            except Exception as ex:  # keep reading even if one pass fails
                print(f'[questlog] read failed: {ex!r}')
                time.sleep(QUESTLOG_EVERY * 3)
                continue
            if title and time.time() - self.tracker.map_changed_at < LOG_SETTLE:
                pass  # the box redraws right after a map change; don't trust it yet
            elif title:
                self.history.append({e['qid']: e for e in read_entries})
                entries = self._smoothed()
                if [e['qid'] for e in entries] != [e['qid'] for e in self.entries or []]:
                    print(f'[questlog] active: {[e["name"] for e in entries]}')
                self.title, self.entries = title, entries
            else:
                self.entries = None
            self.updated = time.time()
            # Searching the whole screen for a missing box is slow; check less often then.
            time.sleep(QUESTLOG_EVERY if title else QUESTLOG_EVERY * 4)


class StatsReader(threading.Thread):
    """Reads EXP (status bar) and mesos (inventory window, while it's open) for the EXP/meso tracker.
    Only reads while the tracker panel is shown."""

    def __init__(self, tracker, stats_tracker):
        super().__init__(daemon=True)
        self.tracker, self.stats = tracker, stats_tracker
        self.enabled = False
        self.inv_title, self.last_inv_search = None, 0.0

    def run(self):
        ocr, sct = SyncOcr(), getattr(mss, 'MSS', mss.mss)()
        stats.ocr_lines = lambda img: ocr.lines(np.asarray(img.convert('RGB')))
        while True:
            st = self.tracker.state
            if not self.enabled or not st.get('visible'):
                time.sleep(1.0)
                continue
            gx, gy, gw, gh = st['rect']
            g = lambda x, y, w, h: grab(sct, gx + x, gy + y, w, h)
            try:
                y0 = int(gh * 0.94)
                exp, pct = stats.read_exp(g(int(gw * 0.33), y0, int(gw * 0.47), gh - y0), ocr)
                if exp is not None or pct is not None:
                    self.stats.update_exp(exp, pct, self.tracker.level)
                mesos = self._read_mesos(g, gw, gh, ocr)
                if mesos is not None:
                    self.stats.update_meso(mesos)
            except Exception as ex:  # keep reading even if one pass fails
                print(f'[stats] read failed: {ex!r}')
            # Read quickly until there's a baseline and the current level's EXP total is known.
            warming = self.stats.last_exp is None or not self.stats.needed.get(self.tracker.level)
            time.sleep(STATS_WARMUP_EVERY if warming else STATS_EVERY)

    def _read_mesos(self, g, gw, gh, ocr):
        """Mesos from the inventory window if it's open (found by its "ITEM INVENTORY" title)."""
        title = None
        if self.inv_title:  # look where it was last time
            x, y, w, h = self.inv_title
            rx, ry = int(max(0, x - w)), int(max(0, y - 3 * h))
            t = stats.find_inventory(ocr.words(g(rx, ry, int(min(gw - rx, 3 * w)), int(min(gh - ry, 7 * h)))))
            title = (t[0] + rx, t[1] + ry, t[2], t[3]) if t else None
        if title is None and time.time() - self.last_inv_search > INVENTORY_SEARCH_EVERY:
            self.last_inv_search = time.time()  # reading the whole screen is slow, so not too often
            title = stats.find_inventory(ocr.words(g(0, 0, gw, gh)))
        self.inv_title = title
        if title is None:
            return None
        x, y, w, h = title
        wx, wy = int(max(0, x - 0.15 * w)), int(y)
        return stats.read_mesos(g(wx, wy, int(min(gw - wx, 3 * w)), int(min(gh - wy, 5 * w))), ocr)


class Overlay:
    def __init__(self, tracker, show_in_capture, maps, graph, mob_index):
        self.tracker = tracker
        # Hidden from screen capture by default so our own minimap/name-tag grabs never see our drawings;
        # "Show on stream" (or --show-in-capture) makes everything visible to Discord/OBS.
        self.capture_visible = show_in_capture or bool(load_prefs().get('show_on_stream', False))
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.config(bg=KEY)
        self.root.attributes('-transparentcolor', KEY)
        self.canvas = tk.Canvas(self.root, bg=KEY, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.root.update_idletasks()
        hwnd = user32.GetAncestor(self.root.winfo_id(), 2)
        ex = user32.GetWindowLongW(hwnd, -20)
        # layered | click-through | no taskbar button | never takes focus
        user32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x8000000)
        self.root_hwnd = hwnd
        self._set_affinity(hwnd)
        self.geom = None
        self.vec = None
        self.pos = None
        self.last_topmost = 0
        # Tk swallows KeyboardInterrupt inside callbacks, so handle Ctrl+C ourselves.
        self.stop = False
        signal.signal(signal.SIGINT, lambda *_: setattr(self, 'stop', True))
        k = self.root.winfo_fpixels('1i') / 96  # DPI scale, e.g. 1.5 at 150%
        self.k = k
        self.graph, self.mob_index, self.npc_index, self.qdata = graph, mob_index, npcs.load(), quests.load()
        # the first job advancement's rooms (where the job on the status bar is watched, see tick)
        self.instructor_maps = {m for _, npc, _, _ in JOB_INSTRUCTORS for m in (self.npc_index.get(npc) or {}).get('maps', {})}
        self.map_names = {m['id']: m['name'] for m in maps}
        self.picker = Picker(self.root, maps, graph, mob_index, self.npc_index, self.qdata, tracker.travel, k,
                             on_pick=self._pick_destination,
                             on_options=self._options_changed,
                             on_quest=self.start_quest,
                             get_level=lambda: tracker.level, on_stream=self.set_capture_visible,
                             on_stats=lambda show: self.set_stats_visible(show),
                             on_close=self._refocus_game, prepare_window=self._prepare_window,
                             on_item=self._show_item, on_quit=self.quit, on_follow=self.start_following)
        self.quest = None          # (QuestGuide, QuestPanel) while the helper is active
        self.quest_pending = False  # step target still to be worked out (current map unknown)
        self.questlog = QuestLogReader(tracker, self.qdata)
        self.questlog.start()
        self.kpq = KpqReader(tracker)  # Kerning PQ stage 1: how many coupons Cloto wants
        self.kpq.start()
        self.kpq_have = None           # coupons last counted in the inventory (Etc tab) on that map
        self.guide_buttons = GuideButtons(self.root, k, self._prepare_window, self._guide_from_box)
        self.picker.get_active_quests = lambda: self.questlog.entries or []
        self.quest_seen = False    # the active quest has appeared in the game's Quest Helper box
        self.follow = None         # the leveling route being followed (routeguide.RouteFollow), if any
        self.route_panel = None    # its panel, while hunting between quests
        self.follow_quest = None   # the quest the route is guiding (one you start yourself pauses the route)
        self.last_follow_check = 0.0
        self.character = None      # whose progress is in use (picked up from the tracker, see _character_changed)
        self.minimap_bottom = 0    # bottom edge of the minimap as last seen (the quest panel goes below it)
        self.clear_at, self.clear_tries = None, 0  # next time the panels step aside for a lost minimap; tries so far
        self.clearing = False      # ...and whether they're aside right now
        self.kpq_win = None        # the Kerning PQ answers panel (kpqpanel.KpqPanel), on that PQ's first stage
        self.gather_key, self.gather_cache = None, {}  # things to break for the guided quest (see _gather_spots)
        self.moves = collections.deque(maxlen=60)  # (time, map, x, y) of recent dot positions
        self.last_moved = 0.0      # when the character was last seen moving
        self.fade = 1.0            # the panels' opacity now (eases toward its target)
        self.alphas = {}           # window -> opacity last applied
        self.stats = stats.StatsTracker(load_prefs().get('exp_needed'), on_learn=self._remember_level_total)
        self.stats_reader = StatsReader(tracker, self.stats)
        self.stats_reader.start()
        self.hover = itemhover.HoverReader(tracker, grab, SyncOcr)  # which item was right-clicked
        # ...or which quest's name (the quest book): its guide opens (see _quest_click)
        self.hover.qdata = self.qdata
        self.hover.quest_context = lambda: ([e['qid'] for e in self.questlog.entries or []],
                                            tracker.job[1] if tracker.job else None)
        self.quest_click = None
        self.mesowatch = mesowatch.MesoWatch()  # Free Market prices and shops, looked up when an item panel opens
        # map name -> map id, for Go to where a shop was seen ("Free Market <11>", "Henesys Hunting Ground I")
        self.place_maps = {}
        for m in maps:
            if m['id'] in graph:
                self.place_maps.setdefault(m['name'].strip(), m['id'])
        self.item_panel_click = None  # (click, game rect) the item panel was opened with, to rebuild it
        self.hover.start()
        self.inventory = inventory.InventoryReader(tracker, grab, SyncOcr)  # stack sizes for the item panel
        self.inventory.start()
        self.hover.inventory = self.inventory
        self.item_panel = None     # item info panel, opened by right-clicking an item
        self.item_click = None     # time of the right-click it was opened for
        self.item_panel_id = None  # the item it shows
        try:
            disciplines, _, makes, uses = crafting.load()
            self.craft = (disciplines, makes, uses)
        except OSError:
            self.craft = None  # not built yet: py -3.11 navgraph.py build
        self.stats_panel = None
        self.last_stats_refresh = 0.0
        if self.picker.show_stats:
            self.set_stats_visible(True)
        self.last_log_sync = 0.0
        self.tick()

    # -- quest helper ----------------------------------------------------------------
    def start_quest(self, qid):
        self.stop_quest()
        guide = QuestGuide(qid, self.qdata, self.npc_index, self.mob_index, self.graph, self.map_names)
        panel = QuestPanel(self.root, self.k, guide, self._prepare_window, on_step=self._quest_step,
                           on_stop=self.stop_quest, on_next_quest=self.start_quest,
                           quest_name=lambda q: self.qdata['quests'][q]['name'],
                           on_go=self._navigate_from_item)
        self.quest = (guide, panel)
        self.quest_seen = False
        print(f'[quest] started {qid} {quests.label(self.qdata, qid)}: ' + ' | '.join(s['text'] for s in guide.steps))
        self._sync_questlog(force=True)
        self._quest_step()
        self._refocus_game()

    # -- following a leveling route ---------------------------------------------------------------------
    # -- each character's own progress ------------------------------------------------------------------
    def _progress(self):
        """This character's saved progress: {'following': the leveling route's state, or None}."""
        return (load_prefs().get('characters') or {}).get(self.character or '', {})

    def _save_progress(self, **changes):
        if self.character is None:
            return  # not known yet (it's read from the status bar within seconds of starting)
        chars = load_prefs().get('characters') or {}
        chars[self.character] = {**chars.get(self.character, {}), **changes}
        save_prefs(characters=chars)

    def _character_changed(self, name):
        """The first character seen since starting, or another one logged into: its own leveling route from here on
        (the other's stays saved). A route saved before characters were told apart belongs to the first one seen.
        Lists of quests done, from when those were kept, are dropped."""
        prefs = load_prefs()
        if 'characters' not in prefs and prefs.get('following'):
            print(f'[character] earlier route saved for {name or "every character"}')
            save_prefs(characters={name: {'following': prefs['following']}})
        chars = load_prefs().get('characters') or {}
        if any('done_quests' in c for c in chars.values()):
            save_prefs(characters={k: {a: b for a, b in c.items() if a != 'done_quests'} for k, c in chars.items()})
        remove_prefs('done_quests', 'following')
        if self.character is not None:
            print(f'[character] {self.character or "?"} -> {name or "?"}: their own quests and route')
        self.character = name
        self.follow, self.follow_quest = None, None
        if self.route_panel:
            self.route_panel.destroy()
            self.route_panel = None
        self.picker.followed_route = None
        if self.quest:
            self.stop_quest()
        saved = self._progress().get('following')
        if saved:  # pick the route up where this character left it
            self._resume_following(saved)

    def start_following(self, route, stage):
        """The Leveling tab's "Follow from here", on a stage of `route` (None: a stage both routes share)."""
        if route is None and stage not in leveling.data()['stages'][:leveling.shared_count()]:
            # where both routes meet again the route doesn't matter; keep the one being followed, if any
            route = self.follow.route if self.follow and self.follow.route else 'A'
        index = leveling.stages(route or 'A').index(stage)
        self.follow = RouteFollow(route, index, self.qdata)
        print(f'[route] following {self.follow.name} from {self.follow.current["title"]}')
        self._save_follow()
        self._follow_next()

    def _resume_following(self, saved):
        try:
            self.follow = RouteFollow(saved.get('route'), saved['stage'], self.qdata, saved.get('picked', ()),
                                      saved.get('skipped', ()), saved.get('job_done', False), saved.get('job_choice'),
                                      saved.get('finished'))
        except (KeyError, ValueError, IndexError):
            return
        print(f'[route] still following {self.follow.name}: {self.follow.current["title"]}')
        self._follow_next()

    def stop_following(self):
        print('[route] stopped following')
        self.follow, self.follow_quest = None, None
        self._save_progress(following=None)
        if self.route_panel:
            self.route_panel.destroy()
            self.route_panel = None
        self.stop_quest()

    def _save_follow(self):
        self._save_progress(following=self.follow.state() if self.follow else None)
        self.picker.followed_route = self.follow.route if self.follow else None

    def _follow_state(self):
        """(level, the route's quests finished while following it, quests in the game's Quest Helper box)."""
        return (self.tracker.level, set(self.follow.finished) if self.follow else set(),
                {e['qid'] for e in self.questlog.entries or []})

    def _follow_next(self):
        """Following a route: move on to the next stage when this one is done; guide its next quest, or hunt at
        its map when there's none to do now."""
        f = self.follow
        if not f:
            return
        level, done, active = self._follow_state()
        f.job = self.tracker.job
        if not f.job_done and f.job and f.job[1] != 'Beginner':  # the status bar shows a job: advanced
            f.job_done = True
            print(f'[route] first job advancement made: {f.job[0]} ({f.job[1]})')
            self._save_follow()
        while f.stage_done(level, done, active) and f.advance():
            print(f'[route] next stage: {leveling_text(f)}')
            self._save_follow()
        if f.needs_choice(level, done, active):  # level 10, the shared start and the first job done: pick a route
            self.follow_quest = None
            if self.quest:
                self.stop_quest()
            self.tracker.quest_hint = 'Leveling route: level 10 - pick Route A or B'
            if self.route_panel is None:
                self.route_panel = RoutePanel(self.root, self.k, self._prepare_window, self._next_stage,
                                              self.stop_following)
            job = self.tracker.job
            self.route_panel.render_choice(self._choose_route, job[0] if job and job[1] != 'Beginner' else None)
            return
        task = f.next_task(level, done, active)
        if task:
            qid, kind = task
            if self.route_panel:
                self.route_panel.destroy()
                self.route_panel = None
            if not (self.quest and self.quest[0].qid == qid):
                print(f'[route] {kind}: {quests.label(self.qdata, qid)}')
                self.start_quest(qid)
            self.follow_quest = qid
            panel = self.quest[1]
            panel.banner = (f'{f.name}  \u00b7  {leveling_text(f)}  \u00b7  ' +
                            ("just accept it for now (it's finished later on the route)" if kind == 'pick up' else 'quest'),
                            [('Skip', self._skip_task), ('Stop following', self.stop_following)])
            panel.render()
            return
        if f.needs_job(level, done, active):  # level 10, the shared start's quests done: the first job advancement
            self.follow_quest = None
            if self.quest:
                self.stop_quest()
            if self.route_panel is None:
                self.route_panel = RoutePanel(self.root, self.k, self._prepare_window, self._next_stage,
                                              self.stop_following)
            first = self.route_panel.shown != ('job', f.job_choice)
            self.route_panel.render_job(f.job_choice, self.npc_index, self.map_names, self._choose_job,
                                        self._go_to_instructor, self._job_done)
            if f.job_choice is None:
                self.tracker.quest_hint = 'Leveling route: level 10 - pick your job'
            else:
                self.tracker.quest_hint = f'Leveling route: become a {f.job_choice}'
                if first:  # just picked (or picked up again after a restart): take them to the instructor
                    self._go_to_instructor(f.job_choice)
            return
        # nothing to do right now: hunt at the stage's map until its last level
        self.follow_quest = None
        if self.quest:
            self.stop_quest()
        stage_map = f.current['map']
        self.tracker.quest_hint = f'Route: hunt at {self.map_names.get(stage_map, stage_map)} until Lv {f.current["levels"][1]}'
        if self.tracker.goal != stage_map:
            self.tracker.set_goal(stage_map)
        if self.route_panel is None:
            self.route_panel = RoutePanel(self.root, self.k, self._prepare_window, self._next_stage, self.stop_following)
        self.route_panel.render(f, level, done, active, str(self.map_names.get(stage_map, stage_map)).strip())

    def _choose_route(self, route):
        """The route panel's Route A / Route B at level 10: that route's quests from here on (starting with its
        pick-ups before the job advancement)."""
        if self.follow:
            self.follow.choose(route)
            print(f'[route] chose {self.follow.name}')
            self._save_follow()
            if self.route_panel:
                self.route_panel.shown = None
            self._follow_next()

    def _choose_job(self, job):
        """The job panel's choice (None: "Other job", pick again); the arrow then goes to its instructor."""
        if self.follow:
            self.follow.job_choice = job
            print(f'[route] job: {job or "picking again"}')
            self._save_follow()
            if job is None:
                self.tracker.set_goal(None)
            self._follow_next()

    def _go_to_instructor(self, job):
        """Navigate to a job's instructor, by taxi if that's quicker (Phil in Lith Harbor, where the shared start
        ends), even with the taxi option off."""
        npc = self.npc_index.get(instructor(job)[0])
        if npc and npc['maps']:
            map_id, (x, y) = next(iter(npc['maps'].items()))
            print(f'[route] to the {job} instructor, {npc["name"]} in {self.map_names.get(map_id, map_id)}')
            self.tracker.set_goal(map_id, (x, y, npc['name']), {'taxi': True})
            self._refocus_game()

    def _job_done(self):
        """The job-advancement panel's Done (the status bar is only read every half minute)."""
        if self.follow:
            self.follow.job_done = True
            self._save_follow()
            if self.route_panel:
                self.route_panel.shown = None
            self._follow_next()

    def _clear_minimap(self, mm, gw, gh):
        """Whether the panels under the minimap (quest, leveling route) should step aside right now.

        They go right under the minimap as it is now (minimaps differ in height from map to map). With "Show on
        stream" on, our windows are in the tracker's screenshots too: on a map with a taller minimap, its bottom
        can end up under a panel, and then the minimap can't be found, so the panel never moves. So while the
        minimap is lost the panels step aside for a moment, and it's found and they come back under it. If it's
        still lost after that (closed, say) they come back where they were, and try again a little later, less
        often each time."""
        now = time.time()
        if mm:
            # one that isn't in the top-left corner was a misdetection, and leaves the panels where they were
            if mm[0] < gw * 0.25 and mm[3] < gh * 0.6:
                self.minimap_bottom = mm[3]
            self.clear_at, self.clear_tries = None, 0
            return False
        if not self.capture_visible:
            return False  # our windows aren't in the screenshots, so they can't be in the way
        if self.clear_at is None:
            self.clear_at = now
        if now < self.clear_at:
            return False
        if now < self.clear_at + MINIMAP_CLEAR:
            return True
        self.clear_at = now + MINIMAP_RETRY[min(self.clear_tries, len(MINIMAP_RETRY) - 1)]
        self.clear_tries += 1
        return False

    def _skip_task(self):
        """The route banner's Skip: don't do this quest (already done, or not wanted)."""
        if self.follow and self.quest:
            self.follow.skipped.add(self.quest[0].qid)
            self._save_follow()
            self._follow_next()

    def _next_stage(self):
        """The route panel's "Next stage": move on without waiting for the stage's last level."""
        if self.follow and self.follow.advance():
            self._save_follow()
            self._follow_next()

    def _follow_check(self):
        """Every few seconds while following: a quest picked up (accepted) is done; a level-up may unlock the next
        quest or stage."""
        f = self.follow
        if not f or time.time() - self.last_follow_check < 3:
            return
        self.last_follow_check = time.time()
        level, done, active = self._follow_state()
        if self.quest:
            qid = self.quest[0].qid
            if qid != self.follow_quest:
                return  # a quest you started yourself: the route waits until it's done or stopped
            if (qid, 'pick up') in leveling.tasks(f.current, f.route, self.qdata) and qid in active:
                f.picked.add(qid)  # accepted: that's all this stage needs of it
                self._save_follow()
                self._follow_next()
            elif all(q != qid for q, _ in f.pending(done, active)) and qid not in done:
                self._follow_next()  # the quest being guided isn't the route's any more (e.g. stage moved on)
        else:
            self._follow_next()

    def stop_quest(self):
        if self.quest:
            self.quest[1].destroy()
            self.quest = None
            self.tracker.quest_hint = None
            self.tracker.set_goal(None)
        self._refocus_game()

    def _quest_step(self):
        """Point the navigator at the current step's NPC or hunting map."""
        guide, panel = self.quest
        step = guide.step
        if step is None:
            if self.follow and guide.qid in self.follow.route_quests:  # one of the route's: on the route, it's behind you
                self.follow.finished.add(guide.qid)
                self._save_follow()
            self.tracker.quest_hint = f'Quest complete: {quests.label(self.qdata, guide.qid)}'
            self.tracker.set_goal(None)
            self._refocus_game()
            if self.follow:  # following a route: on to its next quest
                self.root.after(1500, self._follow_next)
            return
        self.tracker.quest_hint = f'Quest: {step["text"]}'
        if self.tracker.map_id is None:
            self.quest_pending = True
            return
        self.quest_pending = False
        map_id, npc, note = guide.target(self.tracker.map_id, self.tracker.opts, self.tracker.travel)
        panel.note = note
        panel.render()  # also re-picks the upcoming quests' hunting maps for where you are now
        if map_id is not None:
            self.tracker.set_goal(map_id, npc)
        else:
            self.tracker.set_goal(None)
        self._refocus_game()

    def _sync_questlog(self, force=False):
        """Advance the helper from the game's Quest Helper box (accepted, counts, all collected, handed in)."""
        log = self.questlog
        if not self.quest or log.entries is None or (log.updated <= self.last_log_sync and not force):
            return
        self.last_log_sync = log.updated
        guide, panel = self.quest
        entry = next((e for e in log.entries if e['qid'] == guide.qid), None)
        # Handed in = it was in the box, has now been gone for several reads, and you're where the
        # hand-in NPC is (a quest can't be handed in anywhere else).
        end = self.npc_index.get(guide.q['end_npc'])
        at_npc_map = bool(end) and self.tracker.map_id in end['maps']
        handed_in = self.quest_seen and log.gone(guide.qid) and at_npc_map
        index = guide.index
        if guide.apply_log(entry, handed_in):
            if guide.index != index:  # a new step: point the navigator at it and rebuild the panel
                print(f'[quest] step {index + 1} -> {guide.index + 1} (from the game Quest Helper box)')
                self._quest_step()
                if guide.step is None or self.quest_pending:
                    panel.render()  # _quest_step returned before rebuilding it (quest done / map unknown)
            else:  # only counts changed: update those lines in place (a rebuild flashes)
                panel.update_counts()
        self.quest_seen = self.quest_seen or entry is not None

    @staticmethod
    def _remember_level_total(level, total):
        save_prefs(exp_needed={**load_prefs().get('exp_needed', {}), str(level): total})
        print(f'[stats] level {level} needs {total:,.0f} EXP (saved)')

    def set_stats_visible(self, visible):
        """Show or hide the EXP/meso tracker panel (reading only happens while it's shown)."""
        self.stats_reader.enabled = visible
        if visible and self.stats_panel is None:
            self.stats_panel = stats.StatsPanel(self.root, self.k, self.stats, self._prepare_window)
        elif not visible and self.stats_panel is not None:
            self.stats_panel.destroy()
            self.stats_panel = None
        self._refocus_game()

    def _extra_hwnds(self):
        return ({self.quest[1].hwnd} if self.quest else set()) | ({self.stats_panel.hwnd} if self.stats_panel else set()) \
            | ({self.item_panel.hwnd} if self.item_panel else set()) | self.guide_buttons.hwnds() \
            | ({self.route_panel.hwnd} if self.route_panel else set()) | ({self.kpq_win.hwnd} if self.kpq_win else set())

    def _quest_click(self):
        """A right-click on a quest's name in the game (its quest book): open that quest's guide."""
        c = self.hover.quest_clicked
        if c and c['t'] != self.quest_click:
            self.quest_click = c['t']
            if not (self.quest and self.quest[0].qid == c['qid']):
                print(f'[quest] guiding {quests.label(self.qdata, c["qid"])} (right-clicked in the game)')
                self.start_quest(c['qid'])

    def _guide_from_box(self, qid):
        """A Guide button beside a quest in the game's Quest Helper box."""
        if not (self.quest and self.quest[0].qid == qid):
            print(f'[quest] guiding {quests.label(self.qdata, qid)} (from the Quest Helper box)')
            self.start_quest(qid)

    def _kpq_banner(self, rect):
        """Kerning PQ stage 1: how many coupons Cloto asked for (and how many you have, from the inventory's Etc
        tab when it's open), at the top of the screen until you leave the map."""
        gw, gh = rect[2], rect[3]
        if self.tracker.map_id != KPQ_STAGE1:
            self.kpq_have = None
            self.tracker.kpq_panel, self.tracker.kpq_blank = None, []
            if self.kpq_win:
                self.kpq_win.hide()
            return
        inv = self.inventory.state
        if inv and inv.get('tab') == 'Etc':
            self.kpq_have = sum(n for i, n in inv['slots'].values() if i == KPQ_COUPON)
        answer = self.kpq.answer
        if answer is None and self.kpq.unknown:
            text = "Kerning PQ: Cloto asked a question I don't have an answer for yet (it's at the left)"
        elif answer is None:
            text = "Kerning PQ: talk to Cloto - I'll read how many coupons you need"
        else:
            coupons, confirmed, _ = answer
            text = f'Kerning PQ: collect {coupons} coupons'
            if self.kpq_have is not None:
                text += f'  \u00b7  you have {self.kpq_have}' + ('  \u2713' if self.kpq_have >= coupons else '')
            if not confirmed:
                text += "  (from Classic World guides; tell me if Cloto disagrees)"
        # just under the status box (where you are, where you're headed), never over it
        banner = self._label(gw / 2, (getattr(self, 'status_bottom', None) or gh * 0.1) + 12, text, size=13, anchor='n')
        self._kpq_panel(rect)
        self.tracker.kpq_blank = [r for r in (getattr(self, 'status_rect', None), banner, self.tracker.kpq_panel) if r]

    def _kpq_panel(self, rect):
        """The answers panel (kpqpanel.KpqPanel) at the left, under the minimap and any of our panels there; it steps
        aside with them while a lost minimap is looked for."""
        gx, gy, gw, gh = rect
        if self.kpq_win is None:
            self.kpq_win = KpqPanel(self.root, self.k, self._prepare_window)
        a = self.kpq.answer
        self.kpq_win.render([(q, n, ok) for q, n, ok, _ in KPQ_QUESTIONS], a[2] if a else None, a[0] if a else None,
                            self.kpq.unknown)
        if self.clearing:
            self.kpq_win.hide()
            self.tracker.kpq_panel = None
            return
        top = self.minimap_bottom or gh * 0.25
        for panel in ((self.quest[1] if self.quest else None), self.route_panel):
            win = getattr(panel, 'win', None)
            if win is not None and win.winfo_ismapped():
                top = max(top, win.winfo_rooty() - gy + win.winfo_height())
        self.kpq_win.show(gx + gw * 0.006, gy + top + gh * 0.015)
        r = self.kpq_win.rect()
        self.tracker.kpq_panel = (r[0] - gx, r[1] - gy, r[2] - gx, r[3] - gy) if r else None

    def _moving(self):
        """Has the character moved more than MOVE_DIST map units within the last MOVE_WINDOW seconds?"""
        pos, now = self.tracker.last_pos, time.time()
        if pos and (not self.moves or self.moves[-1][0] != pos[3]):
            self.moves.append((pos[3], pos[0], pos[1], pos[2]))
        recent = [m for m in self.moves if now - m[0] <= MOVE_WINDOW and pos and m[1] == pos[0]]
        return bool(recent) and any(math.dist(m[2:], pos[1:3]) > MOVE_DIST for m in recent)

    def _fade_panels(self):
        """Fade the panels and buttons while the character moves, so they hide less of the game; back to solid
        once it has stood still for FADE_BACK_AFTER seconds. The Search panel (when open) and any window under
        the mouse stay solid."""
        now = time.time()
        if self._moving():
            self.last_moved = now
        fading = self.picker.fade_moving and self.picker.panel is None and now - self.last_moved < FADE_BACK_AFTER
        target = FADE_ALPHA if fading else 1.0
        self.fade += (target - self.fade) * (0.35 if target < self.fade else 0.15)  # out quickly, back gently
        if abs(self.fade - target) < 0.02:
            self.fade = target
        cx, cy = itemhover.cursor_pos()
        wins = [self.picker.button] + [w for w, _, _ in self.guide_buttons.windows.values()]
        wins += [p.win for p in (self.quest[1] if self.quest else None, self.stats_panel, self.item_panel,
                                 self.route_panel, self.kpq_win) if p]
        for win in wins:
            try:
                under_mouse = (win.winfo_rootx() <= cx < win.winfo_rootx() + win.winfo_width()
                               and win.winfo_rooty() <= cy < win.winfo_rooty() + win.winfo_height())
                alpha = round(1.0 if under_mouse else self.fade, 2)
                if self.alphas.get(win) != alpha:
                    win.attributes('-alpha', alpha)
                    self.alphas[win] = alpha
            except tk.TclError:  # a window that was just closed
                self.alphas.pop(win, None)
        self.alphas = {w: a for w, a in self.alphas.items() if w in wins}  # forget closed windows

    def _box_buttons(self, st):
        """Place the Guide buttons beside the quests in the game's Quest Helper box (as last read)."""
        log = self.questlog
        history = list(log.history)
        if not log.entries or not log.title or not history:
            self.guide_buttons.hide()
            return
        latest = history[-1]
        taken = [e['line'] for e in latest.values() if e.get('line')]
        placed = []
        for e in log.entries:
            src = latest.get(e['qid']) or e  # the newest position; an older one only if that line is still free
            line = src.get('line')
            if not line or (e['qid'] not in latest and any(abs(line[0] - t[0]) < line[1] * 0.8 for t in taken)):
                continue
            placed.append((e['qid'], line))
        self.guide_buttons.update(st['rect'], log.title, placed, self.quest[0].qid if self.quest else None)

    def _pick_destination(self, map_id, npc=None):
        self.tracker.set_goal(map_id, npc)

    def _options_changed(self, opts):
        self.tracker.set_options(opts)
        if self.quest and self.quest[0].step and self.quest[0].step['kind'] in ('hunt', 'gather'):
            self._quest_step()  # the best hunting map depends on how far away maps are

    def _prepare_window(self, win):
        """Our extra windows: apply the capture setting; return the top-level HWND."""
        win.update_idletasks()
        hwnd = user32.GetAncestor(win.winfo_id(), 2)
        self._set_affinity(hwnd)
        return hwnd

    def _set_affinity(self, hwnd):
        # WDA_EXCLUDEFROMCAPTURE (0x11) hides a window from screen capture; WDA_NONE (0) shows it.
        if hwnd and not user32.SetWindowDisplayAffinity(hwnd, 0 if self.capture_visible else 0x11):
            print('[overlay] could not change capture visibility for a window')

    def set_capture_visible(self, visible):
        self.capture_visible = visible
        for hwnd in {self.root_hwnd} | self.picker.hwnds() | self._extra_hwnds():
            self._set_affinity(hwnd)
        print(f'[overlay] {"visible to" if visible else "hidden from"} streams and recordings')

    def quit(self):
        """The Search panel's Quit button: close the navigator (tick() ends it on the next frame)."""
        print('Quit from the Search panel.')
        self.stop = True

    def _refocus_game(self):
        if self.tracker.hwnd:
            user32.SetForegroundWindow(self.tracker.hwnd)

    def tick(self):
        if self.stop:
            print('Stopped.')
            self.root.destroy()
            return
        try:
            self._draw()
            self._market_ready()
        except Exception:  # one bad frame must not stop the overlay updating
            traceback.print_exc()
        self.root.after(FRAME_MS, self.tick)

    def _market_ready(self):
        """A MesoWatch look-up finished: rebuild the item panel if it still shows that item."""
        while not self.mesowatch.ready.empty():
            item_id = self.mesowatch.ready.get_nowait()
            if self.item_panel and self.item_panel_id == item_id and self.item_panel_click:
                self._open_item_panel(*self.item_panel_click)

    def _draw(self):
        st = self._advance(self.tracker.state)
        c = self.canvas
        c.delete('all')
        if st.get('visible'):
            gx, gy, gw, gh = st['rect']
            if self.geom != st['rect']:
                self.geom = st['rect']
                self.root.geometry(f'{gw}x{gh}+{gx}+{gy}')
            if time.time() - self.last_topmost > 2:
                self.root.attributes('-topmost', True)
                self.picker.raise_windows()
                if self.quest:
                    self.quest[1].raise_window()
                if self.stats_panel:
                    self.stats_panel.raise_window()
                if self.item_panel:
                    self.item_panel.raise_window()
                self.guide_buttons.raise_windows()
                if self.route_panel:
                    self.route_panel.raise_window()
                if self.kpq_win:
                    self.kpq_win.raise_window()
                self.last_topmost = time.time()
            self.status_bottom = self._status(st.get('status', ''), self.tracker.quest_hint or st.get('hint'), gw, gh)
            if st.get('teleports'):
                self._teleport_markers(st, gw, gh)
            self._item_info(st)
            self._quest_click()
            if st.get('hidden') or st.get('hidden_mm'):
                self._hidden_markers(st, gw, gh)
            if st.get('gather') or st.get('gather_mm'):
                self._gather_markers(st, gw, gh)
            if 'target_screen' in st:
                self._target_marker(st, gw, gh)
            if 'char' in st:
                self._arrow(st, gw, gh)
            self.picker.current = st.get('map')
            self._box_buttons(st)
            self._fade_panels()
            self._follow_check()
            clear = self.clearing = self._clear_minimap(st.get('minimap'), gw, gh)
            if self.route_panel:  # the route's hunting panel; a quest's panel takes its place
                if self.quest or clear:
                    self.route_panel.hide()
                else:
                    self.route_panel.show(st['rect'], self.minimap_bottom or None)
            self._kpq_banner(st['rect'])
            self.picker.set_navigating(self.tracker.goal is not None)
            self.picker.show_button(gx, gy, gw, gh)
            if self.stats_panel:
                self.stats_panel.show(st['rect'])
                if time.time() - self.last_stats_refresh > 1:
                    self.last_stats_refresh = time.time()
                    self.stats_panel.refresh()
            if self.quest:
                if clear:
                    self.quest[1].hide()
                else:
                    self.quest[1].show(st['rect'], self.minimap_bottom or None)
                if self.quest_pending and self.tracker.map_id is not None:
                    self._quest_step()
                self._sync_questlog()
        else:
            self.vec = self.pos = None
            self.clear_at = None  # (a map change's loading screen, or tabbed out): step aside at once if it's lost after
            self.picker.hide_button()
            self.guide_buttons.hide()
            if self.route_panel:
                self.route_panel.hide()
            if self.quest:
                self.quest[1].hide()
            if self.stats_panel:
                self.stats_panel.hide()
            if self.item_panel:
                self.item_panel.hide()
            if self.kpq_win:
                self.kpq_win.hide()
        self.tracker.own_hwnds = self.picker.hwnds() | self._extra_hwnds()
        self.tracker.show_hidden = self.picker.show_hidden
        if self.tracker.character is not None and self.tracker.character != self.character:
            self._character_changed(self.tracker.character)
        self.tracker.gather_spots = self._gather_spots()
        # waiting for the first job advancement, in an instructor's room: look at the job on the status bar often
        self.tracker.watch_job = bool(self.follow and not self.follow.job_done
                                      and self.tracker.map_id in self.instructor_maps)
        self.tracker.show_teleports = self.picker.show_teleports
        self.hover.enabled = self.inventory.enabled = self.picker.show_item_prices

    @staticmethod
    def _advance(st):
        """The tracker updates ~20 times a second; between updates keep easing its camera model and shift
        what's pinned to the map (markers) by that much, so they glide instead of stepping."""
        cam = st.get('cam')
        if not cam:
            return st
        cx, cy, t, tx, ty, sw = cam
        nx, ny = ease_camera(cx, cy, tx, ty, min(0.3, max(0.0, time.time() - t)))
        dx, dy = (cx - nx) * sw, (cy - ny) * sw
        if not dx and not dy:
            return st
        st = dict(st)
        if 'target_screen' in st:
            st['target_screen'] = (st['target_screen'][0] + dx, st['target_screen'][1] + dy)
        for key in ('hidden', 'teleports', 'gather'):
            if key in st:
                st[key] = [(x + dx, y + dy, *rest) for x, y, *rest in st[key]]
        return st

    def _status(self, text, hint, gw, gh):
        """The status line (and hint) at the top of the screen; returns the bottom edge of its box (None if none)."""
        if not text:
            return None
        font = ('Segoe UI', 13, 'bold')
        t = self.canvas.create_text(gw / 2, gh * 0.12, text=text, fill='white', font=font)
        x0, y0, x1, y1 = self.canvas.bbox(t)
        if hint:
            h = self.canvas.create_text(gw / 2, y1 + 6, text=hint, fill='#c9d1e0', font=('Segoe UI', 10), anchor='n')
            y1 = self.canvas.bbox(h)[3]
            x0, x1 = min(x0, self.canvas.bbox(h)[0]), max(x1, self.canvas.bbox(h)[2])
        bg = self.canvas.create_rectangle(x0 - 14, y0 - 8, x1 + 14, y1 + 8, fill='#1d2433', outline='#f2b134', width=2)
        self.canvas.tag_lower(bg)
        self.status_rect = (int(x0 - 16), int(y0 - 10), int(x1 + 16), int(y1 + 10))
        return y1 + 8

    def _item_info(self, st):
        """Right-click an item to open its info panel (beside the cursor, on the side away from the game's
        tooltip); right-click it again, or anywhere that isn't an item, to close it. Right-clicking another
        item shows that one instead."""
        c = self.hover.clicked if self.picker.show_item_prices else None
        if c and c['t'] != self.item_click:
            self.item_click = c['t']
            if self.item_panel and (not c['ids'] or c['ids'][0] == self.item_panel_id):
                self._close_item_panel()
            elif c['ids']:
                self._open_item_panel(c, st['rect'])
        if self.item_panel:
            self.item_panel.show()

    def _open_item_panel(self, c, rect):
        item_id = c['ids'][0]
        rec = self.hover.items[item_id]
        t = self.tracker
        here = t.map_id if t.map_id in self.graph else None
        ctx = {'items': self.hover.items, 'qdata': self.qdata, 'mobs': self.mob_index, 'map_names': self.map_names,
               'dist': distances(self.graph, here, {**t.opts, 'fm_return': t.fm_return}, t.travel) if here else {},
               'current': here, 'active': self.questlog.entries or [], 'level': t.level, 'job': t.job,
               'equipped': c.get('others'), 'qty': c.get('qty'), 'craft': self.craft, 'npcs': self.npc_index,
               'graph': self.graph}
        summary, listing, fresh = self.mesowatch.cached(item_id)
        if not fresh:
            self.mesowatch.lookup(item_id)  # the panel is rebuilt when it's in (see _market_ready)
        ctx['market'] = {'summary': summary, 'item': listing, 'state': self.mesowatch.state(item_id),
                         'link': mesowatch.item_link(item_id), 'places': self.place_maps}
        rows = iteminfo.details(item_id, rec, ctx)
        if self.item_panel:
            self.item_panel.destroy()
        self.item_panel = iteminfo.ItemPanel(self.root, self.k, item_id, rec, rows, self._prepare_window,
                                             self._close_item_panel, self._navigate_from_item,
                                             icon=item_index.icon(item_id, rec), same_name=len(c['ids']),
                                             on_spots=self._monster_spots)
        self.item_panel_id = item_id
        self.item_panel_click = (c, rect)
        w, h = self.item_panel.size()
        if c.get('center'):  # opened from the item database: in the middle of the game window
            self.item_panel.place(rect[2] / 2 - w / 2, rect[3] * 0.18, rect)
            return
        cx, cy = c['cursor']
        gap = 30 * rect[3] / VIEW_H
        x = cx + gap if c['x'] < cx else cx - gap - w  # the tooltip is on the other side
        self.item_panel.place(x, cy - h / 3, rect)

    def _show_item(self, item_id):
        """The item database (picker's Items tab): show an item's info panel."""
        rect = self.tracker.state.get('rect') or self.geom
        if rect:
            self._open_item_panel({'ids': [item_id], 'center': True}, rect)

    def _navigate_from_item(self, map_id, npc=None):
        """Go buttons (item panel, Quest Helper's upcoming hunts): to where a monster spawns, or to an NPC."""
        print(f'[item] navigating to {npc[2] + " in " if npc else ""}{self.map_names.get(map_id, map_id)}')
        self._pick_destination(map_id, npc)
        self._refocus_game()

    def _monster_spots(self, mob_id):
        """An item panel's Spots button: the Search panel's Monsters tab with that monster's maps. The item
        panel closes so it doesn't sit over the list (right-click the item to see it again)."""
        print(f'[item] listing spots for {self.mob_index[mob_id]["name"]}')
        self._close_item_panel()
        self.picker.find_monster(mob_id)

    def _close_item_panel(self):
        if self.item_panel:
            self.item_panel.destroy()
            self.item_panel, self.item_panel_id = None, None
            self._refocus_game()

    def _label(self, x, y, text, size=12, anchor='center'):
        t = self.canvas.create_text(x, y + (4 if anchor == 'n' else 0), text=text, fill='white',
                                    font=('Segoe UI', size, 'bold'), anchor=anchor)
        x0, y0, x1, y1 = self.canvas.bbox(t)
        bg = self.canvas.create_rectangle(x0 - 10, y0 - 4, x1 + 10, y1 + 4, fill='#1d2433', outline='#f2b134', width=2)
        self.canvas.tag_lower(bg, t)
        return int(x0 - 12), int(y0 - 6), int(x1 + 12), int(y1 + 6)

    def _pivot(self, st, gw, gh):
        """Where the compass sits: just above the character's head (steadied), or fixed under the status box."""
        fixed = (gw / 2, gh * 0.22)
        if self.picker.arrow_fixed:
            self.pos = None
            return fixed
        cx, cy = st['char']
        target = (cx, cy - COMPASS_LIFT * st['scale'])
        if self.pos is None:
            self.pos = target
        d = math.dist(self.pos, target)
        if d > PIVOT_DEADBAND:  # ignore tiny jitter; glide (faster for big moves) instead of jumping
            a = PIVOT_EASE if d < 200 else PIVOT_EASE * 2.5
            self.pos = (self.pos[0] + (target[0] - self.pos[0]) * a, self.pos[1] + (target[1] - self.pos[1]) * a)
        mm = st.get('minimap')
        if self.capture_visible and mm:
            # When we're visible to capture, our own minimap grabs would see a yellow compass drawn over
            # the minimap (it looks like the player dot), so park it at the fixed spot instead.
            r = (COMPASS_R + 20) * st['scale']
            if mm[0] - r < self.pos[0] < mm[2] + r and mm[1] - r < self.pos[1] < mm[3] + r:
                return fixed
        return self.pos

    def _teleport_markers(self, st, gw, gh):
        """Teleporters: a violet ring with an arrow pointing where it takes you; on the minimap a violet
        diamond at each one and a dashed line to its exit."""
        c, s = self.canvas, st.get('scale', gh / VIEW_H)
        mm = st.get('minimap')
        marks, lines = st.get('teleports_mm', ([], []))
        for line in lines:
            if _clear_of_dot(st, gh, line):
                c.create_line(*line, fill=TELEPORT_COLOUR, width=2, dash=(4, 3))
        d = max(4, gh * 0.0032)
        for x, y in marks:
            if not _clear_of_dot(st, gh, (x, y, x, y)):
                continue
            c.create_polygon(x, y - d, x + d, y, x, y + d, x - d, y, fill=TELEPORT_COLOUR, outline='#1d2433', width=2)
        for x, y, angle, touch in st['teleports']:
            rx, ry = 26 * s, 7 * s
            if not (-rx < x < gw + rx and 0 < y < gh * 0.9):
                continue
            if mm and mm[0] - rx < x < mm[2] + rx and mm[1] - ry < y < mm[3] + 50 * s:
                continue
            c.create_oval(x - rx, y - ry, x + rx, y + ry, outline='#1d2433', width=6)
            c.create_oval(x - rx, y - ry, x + rx, y + ry, outline=TELEPORT_COLOUR, width=3)
            arrow = '' if angle is None else ' ' + ARROWS[round(angle / (math.pi / 4)) % 8]
            text = f'Teleport{arrow}  ({"walk in" if touch else "press Up"})'
            t = c.create_text(x, _label_top(st, x, y, ry, s), text=text, fill='white', font=('Segoe UI', 9, 'bold'),
                              anchor='n')
            x0, y0, x1, y1 = c.bbox(t)
            bg = c.create_rectangle(x0 - 6, y0 - 2, x1 + 6, y1 + 2, fill='#1d2433', outline=TELEPORT_COLOUR, width=1)
            c.tag_lower(bg, t)

    def _hidden_markers(self, st, gw, gh):
        """Hidden street entrances: a dashed pink ring where the portal is, with the street's name under
        it, and a pink diamond on the minimap (so they can be found while still off screen)."""
        c, s = self.canvas, st.get('scale', gh / VIEW_H)
        mm = st.get('minimap')
        for x, y, name, invisible in st.get('hidden', []):
            rx, ry = 34 * s, 9 * s
            if not (-rx < x < gw + rx and 0 < y < gh * 0.9):
                continue
            if mm and mm[0] - rx < x < mm[2] + rx and mm[1] - ry < y < mm[3] + 60 * s:
                continue
            c.create_oval(x - rx, y - ry, x + rx, y + ry, outline='#1d2433', width=6)
            c.create_oval(x - rx, y - ry, x + rx, y + ry, outline=HIDDEN_COLOUR, width=3, dash=(6, 4))
            top = _label_top(st, x, y, ry, s)
            t1 = c.create_text(x, top, text=name, fill='white', font=('Segoe UI', 10, 'bold'), anchor='n')
            sub = 'Hidden Street - invisible portal' if invisible else 'Hidden Street'
            t2 = c.create_text(x, c.bbox(t1)[3] + 1, text=sub, fill=HIDDEN_COLOUR, font=('Segoe UI', 8), anchor='n')
            x0, y0 = min(c.bbox(t1)[0], c.bbox(t2)[0]), c.bbox(t1)[1]
            x1, y1 = max(c.bbox(t1)[2], c.bbox(t2)[2]), c.bbox(t2)[3]
            bg = c.create_rectangle(x0 - 8, y0 - 3, x1 + 8, y1 + 3, fill='#1d2433', outline=HIDDEN_COLOUR, width=1)
            c.tag_lower(bg, t1)
        d = max(5, gh * 0.004)
        for x, y in st.get('hidden_mm', []):
            if not _clear_of_dot(st, gh, (x, y, x, y)):
                continue
            c.create_polygon(x, y - d, x + d, y, x, y + d, x - d, y, fill=HIDDEN_COLOUR, outline='#1d2433', width=2)

    def _gather_markers(self, st, gw, gh):
        """Things to break for the quest (wooden boxes, coconuts, herbs): a cyan ring around each one with its
        name above, and a cyan dot on the minimap (they're also there while broken, waiting to come back)."""
        c, s = self.canvas, st.get('scale', gh / VIEW_H)
        mm = st.get('minimap')
        rx, ry = 30 * s, 22 * s
        for x, y, name in st.get('gather', []):
            y -= 4 * s  # the ring's bottom at the ground, clear of the character's name tag
            if not (-rx < x < gw + rx and ry < y < gh * 0.9):
                continue
            if mm and mm[0] - rx < x < mm[2] + rx and mm[1] - ry < y < mm[3] + 40 * s:
                continue
            c.create_oval(x - rx, y - ry, x + rx, y + ry, outline='#1d2433', width=6)
            c.create_oval(x - rx, y - ry, x + rx, y + ry, outline=GATHER_COLOUR, width=3)
            t = c.create_text(x, y - ry - 4 * s, text=name, fill='white', font=('Segoe UI', 9, 'bold'), anchor='s')
            x0, y0, x1, y1 = c.bbox(t)
            bg = c.create_rectangle(x0 - 6, y0 - 2, x1 + 6, y1 + 2, fill='#1d2433', outline=GATHER_COLOUR, width=1)
            c.tag_lower(bg, t)
        d = max(4, gh * 0.003)
        for x, y in st.get('gather_mm', []):
            if not _clear_of_dot(st, gh, (x, y, x, y)):
                continue
            c.create_oval(x - d, y - d, x + d, y + d, fill=GATHER_COLOUR, outline='#1d2433', width=2)

    def _gather_spots(self):
        """{map: [(x, y, name)]}: the things to break for the guided quest, once it's accepted, until its counts
        for them are full."""
        if not self.quest:
            return {}
        g = self.quest[0]
        key = (g.qid, g.index, tuple(sorted(g.progress.items())))
        if self.gather_key != key:
            accepted = g.index > 0 or not (g.steps and g.steps[0].get('accept'))
            out = {}
            for i, step in enumerate(g.steps):
                if step['kind'] != 'gather' or i < g.index or not accepted:
                    continue
                for item, req in zip(step['items'], step['reqs']):
                    have, need = g.progress.get(req, (None, None))
                    if have is not None and need is not None and have >= need:
                        continue
                    for map_id, pts in gathering.spots(self.graph, item).items():
                        out.setdefault(map_id, {}).update({p: gathering.SOURCES[item].single for p in pts})
            self.gather_key = key
            self.gather_cache = {m: [(x, y, name) for (x, y), name in d.items()] for m, d in out.items()}
        return self.gather_cache

    def _target_marker(self, st, gw, gh):
        """A fixed marker over the portal (or NPC) being navigated to: a ring at its base and an
        arrow pointing down at it. Only drawn while that spot is in view."""
        x, y = st['target_screen']  # already smooth: it moves with the simulated camera
        s = st['scale']
        h, w = MARK_SIZE * s, MARK_SIZE * 0.75 * s
        tip = y - MARK_LIFT * s
        rx, ry = 34 * s, 9 * s
        box = (x - max(w, rx), tip - h, x + max(w, rx), y + ry)
        if box[2] < 0 or box[0] > gw or box[3] < 0 or box[1] > gh * 0.9:  # off screen or behind the status bar
            return
        mm = st.get('minimap')
        if mm and box[0] < mm[2] and box[2] > mm[0] and box[1] < mm[3] and box[3] > mm[1]:
            return  # never draw over the minimap (a yellow mark there could pass for the player dot)
        for colour, width in (('#1d2433', 6), ('#ffd23f', 3)):
            self.canvas.create_oval(x - rx, y - ry, x + rx, y + ry, outline=colour, width=width)
        head, shaft = h * 0.55, w * 0.38
        self.canvas.create_polygon(x, tip, x - w, tip - head, x - shaft, tip - head, x - shaft, tip - h,
                                   x + shaft, tip - h, x + shaft, tip - head, x + w, tip - head,
                                   fill='#ffd23f', outline='#1d2433', width=3)

    def _arrow(self, st, gw, gh):
        px, py = self._pivot(st, gw, gh)
        s = st['scale']
        if st.get('action'):  # e.g. "Use Return Scroll to Henesys": no arrow, just say what to do
            self.vec = None
            self._label(px, py, st['action'], 13)
            return
        dx, dy = st['dir']
        n = math.hypot(dx, dy) or 1
        target = (dx / n, dy / n)
        if self.vec is None:
            self.vec = target
        else:
            vx = self.vec[0] + (target[0] - self.vec[0]) * SMOOTH
            vy = self.vec[1] + (target[1] - self.vec[1]) * SMOOTH
            m = math.hypot(vx, vy) or 1
            self.vec = (vx / m, vy / m)
        if st['here']:
            h = 22 * s
            self.canvas.create_polygon(px, py - h, px - h * 0.8, py + h * 0.4, px + h * 0.8, py + h * 0.4,
                                       fill='#ffd23f', outline='#1d2433', width=3)
            self._label(px, py + h * 1.4, st.get('here_text') or 'Press Up')
            return
        # Compass: a ring with the arrow spinning about its centre.
        r = COMPASS_R * s
        self.canvas.create_oval(px - r, py - r, px + r, py + r, outline='#1d2433', width=6)
        self.canvas.create_oval(px - r, py - r, px + r, py + r, outline='#f2b134', width=3)
        ux, uy = self.vec
        nx, ny = -uy, ux
        half, head_l, head_w, shaft_w = 0.8 * r, 0.5 * r, 0.45 * r, 0.17 * r
        tail = (px - ux * half, py - uy * half)
        tip = (px + ux * half, py + uy * half)
        neck = (tip[0] - ux * head_l, tip[1] - uy * head_l)
        pts = [
            (tail[0] + nx * shaft_w, tail[1] + ny * shaft_w), (neck[0] + nx * shaft_w, neck[1] + ny * shaft_w),
            (neck[0] + nx * head_w, neck[1] + ny * head_w), tip,
            (neck[0] - nx * head_w, neck[1] - ny * head_w), (neck[0] - nx * shaft_w, neck[1] - ny * shaft_w),
            (tail[0] - nx * shaft_w, tail[1] - ny * shaft_w),
        ]
        self.canvas.create_polygon(*[v for xy in pts for v in xy], fill='#ffd23f', outline='#1d2433', width=3)

    def run(self):
        self.root.mainloop()


class _Tee:
    """Writes to the console (if there is one) and to the log file (nav_log.txt), so the log can be read
    even when the navigator runs as administrator in its own window, or with no window at all."""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]  # pythonw has no console streams

    def write(self, text):
        for s in self.streams:
            s.write(text)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def _single_instance():
    """True if no other navigator is running. Holds a named mutex for as long as this one runs (the
    launcher looks for it too, so double-clicking the shortcut again doesn't start a second copy)."""
    global _instance_mutex
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.restype = wt.HANDLE
    _instance_mutex = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
    # ERROR_ALREADY_EXISTS; or ERROR_ACCESS_DENIED: it exists but belongs to a copy running as administrator
    if ctypes.get_last_error() in (183, 5):
        if _instance_mutex:
            kernel32.CloseHandle(_instance_mutex)
        _instance_mutex = None
        return False
    return True


def main():
    if not _single_instance():
        root = tk.Tk()
        root.withdraw()
        tk.messagebox.showinfo('MSCW Navigator', 'MSCW Navigator is already running.\n\n'
                               'Its Search button is at the top of the game window.')
        return
    if sys.stdout:
        sys.stdout.reconfigure(line_buffering=True)
    log = open(HERE / 'nav_log.txt', 'w', encoding='utf-8')
    sys.stdout, sys.stderr = _Tee(sys.stdout, log), _Tee(sys.stderr, log)
    if itemhover.game_elevated() and not itemhover.self_elevated():
        print('[item] MapleStory runs as administrator but the navigator does not, so right-clicks on items '
              "can't be seen. Start it with \"Start Navigator.bat\" to use the item info panel.")
    maps, graph = load_maps(), load_graph()
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if args:
        arg = ' '.join(args)
        goal = int(arg) if arg.isdigit() else find_map(arg, maps, limit=1)[0][1]['id']
        name = next((m['name'] for m in maps if m['id'] == goal), goal)
        print(f'Destination: {goal} {name}. Switch to the game. Quit from the Search panel (or Ctrl+C here).')
    else:
        goal = None
        print('Switch to the game and click Search (top of the screen) to pick a destination. '
              'Quit from the Search panel (or Ctrl+C here).')
    tracker = Tracker(goal, graph, maps)
    tracker.start()
    overlay = Overlay(tracker, '--show-in-capture' in sys.argv, maps, graph, mobs.load())
    try:
        overlay.run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
