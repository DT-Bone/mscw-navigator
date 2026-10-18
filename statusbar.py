"""Read the character level from the status bar ("LV." followed by digits on orange tiles)."""
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
import gamepath


_LEVEL_DIGITS = None
UI_DIR = gamepath.DATA / 'UI'


def _level_digits():
    """The game's own level digit tiles (UI Basic.img/LevelNo/0-9), decoded to RGBA."""
    global _LEVEL_DIGITS
    if _LEVEL_DIGITS is None:
        import canvas
        from wzreader import WzFile
        _LEVEL_DIGITS = []
        for path in sorted(UI_DIR.rglob('_Canvas*.wz')):
            wz = WzFile(str(path))
            images = dict(wz.images())
            if 'Basic.img' in images:
                digits = wz.read_image(images['Basic.img'])['LevelNo']
                _LEVEL_DIGITS = [canvas.decode(wz, digits[str(d)]['_data']) for d in range(10)]
            wz.close()
            if _LEVEL_DIGITS:
                break
    return _LEVEL_DIGITS


def match_level_tiles(region):
    """Level from the orange digit tiles in region, by comparing each tile with the game's digit
    images (OCR struggles with this font). None if no tiles are found."""
    templates = _level_digits()
    if not templates:
        return None
    a = region.astype(int)
    orange = (a[..., 0] > 200) & (a[..., 1] > 80) & (a[..., 1] < 200) & (a[..., 2] < 90)
    if orange.sum() < 30:
        return None
    th, tw = templates[0].shape[:2]
    rows = np.nonzero(orange.any(axis=1))[0]
    height = rows.max() - rows.min() + 1
    runs, start = [], None  # columns with orange in them; tiles may touch, so split wide runs
    for x, filled in enumerate(list(orange.any(axis=0)) + [False]):
        if filled and start is None:
            start = x
        elif not filled and start is not None:
            n = max(1, round((x - start) / (height * tw / th)))
            runs += [(start + (x - start) * i // n, start + (x - start) * (i + 1) // n) for i in range(n)]
            start = None
    digits = []
    for x0, x1 in runs:
        if x1 - x0 < height * 0.4:
            continue  # a sliver, not a tile
        tile = region[rows.min():rows.max() + 1, x0:x1, :3]
        shape = tile.min(axis=2) > 190  # the white digit; compare shapes at the tile's own size
        diffs = []
        for t in templates:
            big = np.asarray(Image.fromarray(t).resize((shape.shape[1], shape.shape[0]), Image.NEAREST))
            diffs.append(np.mean(shape != ((big[..., :3].min(axis=2) > 190) & (big[..., 3] > 128))))
        digits.append(str(int(np.argmin(diffs))))
    level = int(''.join(digits)) if digits else 0
    return level if 1 <= level <= 250 else None


def read_level(bar, ocr):
    """Level shown in a status-bar screenshot (RGB array), or None. `ocr` is an ocr_client.SyncOcr."""
    words = ocr.words(bar)
    if not words:  # no status bar on screen (e.g. the Cash Shop)
        return None
    lv = next((w for w in words if w[0].upper().startswith('LV')), None)
    if lv is not None:
        _, x, y, w, h = lv
        x0, x1 = int(x + w), int(min(bar.shape[1], x + w + h * 3.2))
        # the tiles are taller than the "LV." text, so look well above and below it
        y0, y1 = int(max(0, y - h * 0.5)), int(min(bar.shape[0], y + h * 2.0))
    else:
        # The "LV." label doesn't always read; the orange digit tiles sit left of the job/name text.
        # job and name are stacked at the same x (e.g. "Magician" above "Stoicism")
        stacked = [a[1] for a in words for b in words if a is not b and abs(a[1] - b[1]) < 10
                   and abs(a[2] - b[2]) > a[4] * 0.6 and a[0].isalpha() and b[0].isalpha()]
        right = min(stacked) if stacked else max((w[1] for w in words if len(w[0]) >= 4 and w[0].isalpha()),
                                                 default=bar.shape[1] * 0.4)
        # only the rows level with the job/name text (chat tags above can be orange too)
        text = [w for w in words if abs(w[1] - right) < 10] or words
        top = int(max(0, min(w[2] for w in text) - text[0][4] * 0.5))
        bottom = int(min(bar.shape[0], max(w[2] + w[4] for w in text) + text[0][4] * 0.5))
        a = bar[top:bottom, :int(right)].astype(int)
        mask = (a[..., 0] > 200) & (a[..., 1] > 80) & (a[..., 1] < 200) & (a[..., 2] < 90)
        if mask.sum() < 30:
            return None
        ys, xs = np.nonzero(mask)
        y0, y1 = top + int(ys.min()) - 2, top + int(ys.max()) + 3
        x0, x1 = int(xs.min()) - 2, int(xs.max()) + 3
    level = match_level_tiles(bar[max(0, y0):y1, max(0, x0):x1])
    if level:
        return level
    tile = bar[max(0, y0):y1, max(0, x0):x1].astype(int)  # fallback: OCR the digits
    r, g, b = tile[..., 0], tile[..., 1], tile[..., 2]
    orange = (r > 200) & (g > 80) & (g < 200) & (b < 90)
    if not orange.any():
        return None
    cols, rows = np.nonzero(orange.any(axis=0))[0], np.nonzero(orange.any(axis=1))[0]
    tile = tile[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    # White digits on orange -> black on white; the OCR needs them large, smoothed and with
    # a word in front, or it ignores a lone pair of blocky digits.
    digits = Image.fromarray(np.pad(np.where(tile.min(axis=2) > 190, 0, 255).astype(np.uint8), 24,
                                    constant_values=255))
    for target_h in (530, 440, 640):
        scale = max(2, round(target_h / digits.height))
        big = digits.resize((digits.width * scale, digits.height * scale), Image.BICUBIC).filter(
            ImageFilter.GaussianBlur(scale / 3))
        canvas = Image.new('L', (big.width + 600, big.height), 255)
        ImageDraw.Draw(canvas).text((20, big.height // 3), 'Level', fill=0,
                                    font=ImageFont.truetype('arial.ttf', big.height // 3))
        canvas.paste(big, (560, 0))
        for line in ocr.lines(np.asarray(canvas.convert('RGB'))):
            m = re.fullmatch(r'\s*(\d{1,3})\s*', line.replace(' ', ''))
            if m and 1 <= int(m.group(1)) <= 250:
                return int(m.group(1))
    return None


# Job names the status bar can show, by job class (the classes item requirements use).
JOB_CLASSES = {
    'Beginner': ('Beginner',),
    'Warrior': ('Warrior', 'Swordman', 'Swordsman', 'Fighter', 'Page', 'Spearman', 'Crusader', 'Knight', 'Hero',
                'Paladin', 'Dragon Knight', 'Dark Knight', 'White Knight'),
    'Magician': ('Magician', 'Wizard', 'Cleric', 'Mage', 'Priest', 'Bishop', 'Arch Mage'),
    'Bowman': ('Bowman', 'Archer', 'Hunter', 'Crossbowman', 'Ranger', 'Sniper', 'Bowmaster', 'Marksman'),
    'Thief': ('Thief', 'Rogue', 'Assassin', 'Bandit', 'Hermit', 'Chief Bandit', 'Night Lord', 'Shadower'),
    'Pirate': ('Pirate', 'Brawler', 'Gunslinger', 'Marauder', 'Outlaw', 'Buccaneer', 'Corsair'),
}


def read_name(words):
    """The character's name: the word right under the job name on the status bar ('Rogue' over 'Jogger'), or None.
    words: OCR words (text, x, y, w, h). Names are one word of letters and digits."""
    job_words = {n.split()[0].lower() for names in JOB_CLASSES.values() for n in names}
    for text, x, y, w, h in words:
        if text.lower() in job_words:
            below = [v for v in words if h * 0.6 < v[2] - y < h * 2.2 and abs(v[1] - x) < h * 1.5]
            if below:
                name = min(below, key=lambda v: v[2])[0]
                if re.fullmatch(r'[A-Za-z0-9]{2,13}', name):
                    return name
    return None


def read_job(words):
    """(job name, job class) from the status bar's OCR words (e.g. ('Archer', 'Bowman')), or None."""
    text = ' '.join(w[0] for w in words).lower()
    for job_class, names in JOB_CLASSES.items():
        for name in sorted(names, key=len, reverse=True):  # "Dark Knight" before "Knight"
            if re.search(r'\b' + re.escape(name.lower()) + r'\b', text):
                return name, job_class
    return None

