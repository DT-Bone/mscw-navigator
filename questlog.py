"""Read the game's own Quest Helper box: which quests are active and their progress lines.

The box has a title bar "Quest Helper (n/5)", then for each quest its name (which may wrap onto
a second line) followed by lines like "12 / 30 Blue Mushroom" or "0 / 1 Omok Piece: Pig". The box is
semi-transparent, so OCR can garble parts of it; quest names and requirement names are therefore
matched fuzzily against the quest data, and lines whose numbers can't be read keep "unknown" counts.
"""
import difflib
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PROGRESS = re.compile(r'^\(?(\d+)\s*/\s*(\d+)\)?\s*(.*)$')


def _norm(s):
    return ' '.join(re.sub(r'[^a-z0-9& ]', ' ', s.lower()).split())


def _lines(words):
    """Group OCR word boxes into text lines, top to bottom: [{'text', 'words', 'y', 'h'}]."""
    lines = []
    for word in sorted(words, key=lambda w: (w[2], w[1])):
        text, x, y, w, h = word
        for line in lines:
            if abs(line['y'] + line['h'] / 2 - (y + h / 2)) < max(line['h'], h) * 0.5:
                line['words'].append(word)
                line['h'] = max(line['h'], h)
                break
        else:
            lines.append({'y': y, 'h': h, 'words': [word]})
    for line in lines:
        line['words'].sort(key=lambda w: w[1])
        line['text'] = ' '.join(w[0] for w in line['words'])
    return sorted(lines, key=lambda l: l['y'])


def read_count(img, x0, x1, y0, y1, ocr, need=None):
    """OCR a "have / need" pair cut out of img. The game font's digits defeat plain OCR, so the
    dark text is isolated, enlarged and smoothed, with a word in front for context. Several darkness
    cut-offs are tried (busy backgrounds need a strict one, plain ones a softer one that keeps the
    thin strokes whole); with `need` known, only a reading that matches it is accepted."""
    crop = img[max(0, int(y0)):int(y1), max(0, int(x0)):max(int(x0) + 1, int(x1))]
    if crop.size == 0:
        return None
    rgb = crop[..., :3].astype(np.float32)
    lum = rgb @ np.array([0.299, 0.587, 0.114], np.float32)
    for mask in (rgb.max(axis=2) < 100, lum < 115, lum < 135):
        digits = Image.fromarray(np.pad(np.where(mask, 0, 255).astype(np.uint8), 20, constant_values=255))
        for target_h in (300, 420):
            sc = max(2, round(target_h / digits.height))
            big = digits.resize((digits.width * sc, digits.height * sc), Image.BICUBIC).filter(
                ImageFilter.GaussianBlur(sc / 3))
            canvas = Image.new('L', (big.width + 700, big.height), 255)
            ImageDraw.Draw(canvas).text((20, big.height // 3), 'Count', fill=0,
                                        font=ImageFont.truetype('arial.ttf', big.height // 3))
            canvas.paste(big, (660, 0))
            for line in ocr.lines(np.asarray(canvas.convert('RGB'))):
                m = re.search(r'(\d+)\s*/\s*(\d+)', line)
                if m and (need is None or int(m.group(2)) == need):
                    return int(m.group(1)), int(m.group(2))
    return None


def _find_title(words):
    """(x, y, w, h) of the words "Quest Helper", and the number of quests its "(n/5)" says (or None).
    OCR mangles "(3/5)" into "(3E)" or "(SE)", so only the first character is used."""
    for i, (text, x, y, w, h) in enumerate(words):
        if text.lower() == 'quest' and i + 1 < len(words) and words[i + 1][0].lower().startswith('helper'):
            count = re.search(r'\(\s*([0-9SOoIl|])', ' '.join(t[0] for t in words[i + 1:i + 3]))
            count = int(count.group(1).translate(LOOKALIKE).replace('S', '5')) if count else None
            return (x, y, words[i + 1][1] + words[i + 1][3] - x, h), count
    return None, None


# The box is see-through, so busy scenery behind it (stone walls, foliage) garbles OCR of its black text.
# A cleaned copy keeps only the dark text pixels, on white, smoothed (the game font is a pixel font). Each
# darkness cut-off suits different backgrounds; what they find is added to the plain reading.
CLEAN_CUTS = (120, 100)  # luminance below which a pixel counts as text
CLEAN_SCALE = 2          # enlargement before OCR
SAME_NAME = 0.8          # a cleaned-only quest named this much like one already read is a misreading of it


def _cleaned_words(body, ocr, cut):
    rgb = body[..., :3].astype(np.float32)
    lum = rgb @ np.array([0.299, 0.587, 0.114], np.float32)
    img = Image.fromarray(np.where(lum < cut, 0, 255).astype(np.uint8))
    sc = CLEAN_SCALE
    big = img.resize((img.width * sc, img.height * sc), Image.BICUBIC).filter(ImageFilter.GaussianBlur(sc / 3))
    return [(t, x / sc, y / sc, w / sc, h / sc) for t, x, y, w, h in ocr.words(np.asarray(big.convert('RGB')))]


def _box_lines(words, h):
    """_lines of the box body, stopping at a big gap (past the bottom of the box)."""
    out, last_y = [], None
    for line in _lines(words):
        if last_y is not None and line['y'] - last_y > 6 * h:  # quests are separated by a blank line
            break
        last_y = line['y']
        out.append(line)
    return out


def _read_box(grab, gw, gh, ocr, last_title):
    """(title box, quest count from the title or None, body image, title height, body's top-left corner) or
    Nones."""
    regions = []
    if last_title:  # where it was last time, then the usual top-right corner, then everywhere
        x, y, w, h = last_title
        regions.append((max(0, int(x - w)), max(0, int(y - 2 * h)), int(5 * w), int(5 * h)))
    regions += [(gw // 2, 0, gw - gw // 2, int(gh * 0.45)), (0, 0, gw, gh)]
    for rx, ry, rw, rh in regions:
        rw, rh = min(rw, gw - rx), min(rh, gh - ry)
        t, count = _find_title(ocr.words(grab(rx, ry, rw, rh)))
        if t:
            x, y, w, h = t[0] + rx, t[1] + ry, t[2], t[3]
            # The box is about 3.5x as wide as the words "Quest Helper" (it reaches the window's right
            # edge when docked there); quests are listed below the title bar.
            bx0, bx1 = max(0, int(x - 0.15 * w)), min(gw, int(x + 3.6 * w))
            by0, by1 = int(y + 1.5 * h), min(gh, int(y + 1.5 * h + gh * 0.4))
            return (x, y, w, h), count, grab(bx0, by0, bx1 - bx0, by1 - by0), h, (bx0, by0)
    return None, None, None, None, None


def read(grab, gw, gh, ocr, last_title=None):
    """(title box, lines under it, image of that area) for the Quest Helper box, or (None, [], None).

    grab(x, y, w, h) -> RGB array of the game window. Lines are as returned by _lines.
    """
    title, _, body, h, _ = _read_box(grab, gw, gh, ocr, last_title)
    if title is None:
        return None, [], None
    return title, _box_lines(ocr.words(body), h), body


def read_entries(grab, gw, gh, ocr, qdata, last_title=None):
    """(title box, parse() entries) for the Quest Helper box, or (None, []). Each entry's 'line' is (y, height)
    of its name's first line in the window (for buttons beside it), when known.

    The plain reading is kept as it is. If it misses quests (fewer than the title's "(n/5)") or
    requirement lines, cleaned copies of the box (text only) are read too, adding quests it missed (up
    to n) and counts it couldn't read."""
    title, count, body, h, origin = _read_box(grab, gw, gh, ocr, last_title)
    if title is None:
        return None, []
    by0 = origin[1]
    entries = parse(_box_lines(ocr.words(body), h), qdata, body, ocr)
    for cut in CLEAN_CUTS:
        if _whole(entries, qdata, count):
            break
        lines = _box_lines(_cleaned_words(body, ocr, cut), h)
        extra = parse(lines, qdata)
        # counts are re-read (slow) only for quests the plain reading missed; it already tried the others
        new = {e['qid'] for e in extra} - {e['qid'] for e in entries}
        if any(v[0] is None for e in extra if e['qid'] in new for v in e['progress'].values()):
            extra = parse(lines, qdata, body, ocr, reread=new)
        entries = _merge(entries, extra, qdata, count, h)
    for e in entries:
        if e.get('y') is not None:
            e['line'] = (by0 + e['y'], h)
    return title, entries


def _reqs(qdata, qid):
    q = qdata['quests'][qid]
    return len(q['mobs']) + len(q['items'])


def _whole(entries, qdata, count):
    """Every quest the title mentions was read, each with all its requirement lines and counts."""
    return (count is not None and len(entries) >= count
            and all(len(e['progress']) == _reqs(qdata, e['qid']) and all(v[0] is not None for v in e['progress'].values())
                    for e in entries))


def _merge(entries, extra, qdata, count, h):
    """entries plus what `extra` (another reading of the same box) adds: quests not read before (up to
    the title's count, with at least one of their requirement lines recognised, and not just a misreading
    of one already there) and counts that were unknown. When the two readings name different quests on
    the same line, the one with more requirement lines recognised under it wins."""
    out = [dict(e, progress=dict(e['progress'])) for e in entries]
    by_qid = {e['qid']: e for e in out}
    for e in extra:
        if e['qid'] in by_qid:
            mine = by_qid[e['qid']]['progress']
            for key, (have, need) in e['progress'].items():
                if mine.get(key, (None,))[0] is None:
                    mine[key] = (have, need)
            continue
        same_line = next((o for o in out if o.get('y') is not None and e.get('y') is not None
                          and abs(o['y'] - e['y']) < h * 0.8), None)
        if same_line is not None:
            if len(e['progress']) > len(same_line['progress']):
                out[out.index(same_line)] = by_qid[e['qid']] = dict(e, progress=dict(e['progress']))
                del by_qid[same_line['qid']]
        elif (count is None or len(out) < count) and e['progress'] and not any(
                difflib.SequenceMatcher(None, _norm(e['name']), _norm(o['name'])).ratio() >= SAME_NAME for o in out):
            out.append(dict(e, progress=dict(e['progress'])))
            by_qid[e['qid']] = out[-1]
    for e in out:
        known = [hn for hn in e['progress'].values() if hn[0] is not None]
        e['complete'] = bool(_reqs(qdata, e['qid'])) and len(known) == _reqs(qdata, e['qid']) and all(have >= n for have, n in known)
    return out


def parse(lines, qdata, body=None, ocr=None, reread=None):
    """Quest Helper lines -> [{'qid', 'name', 'progress': {(kind, id): (have, need)}, 'complete'}].

    lines are _lines() dicts (or plain strings, for testing). With body (the image the lines were read
    from) and ocr, counts that don't read cleanly are re-read with read_count (only for the quest ids in
    `reread`, if given)."""
    lines = [l if isinstance(l, dict) else {'text': l, 'words': []} for l in lines]
    # Left edge of the box's text: every progress line's numbers start here, even when OCR drops them.
    margin = min((l['words'][0][1] for l in lines if l['words']), default=0)
    quests = qdata['quests']
    names = {qid: _norm(q['name']) for qid, q in quests.items()}

    def best_quest(text):
        t = _norm(text)
        if len(t) < 4:
            return None, 0, []
        # Compare against the start of each name too, since long names wrap onto a second line. A start-of-name
        # match counts a little less than the whole name, so "Luke the Security Guy" is that quest and not the
        # start of "Luke the Security Guy's Wish to Travel" (on a tie, the higher quest id used to win).
        scored = sorted(((max(difflib.SequenceMatcher(None, t, n).ratio(),
                              0.98 * difflib.SequenceMatcher(None, t, n[:len(t)]).ratio() if len(t) >= 12 else 0), qid)
                         for qid, n in names.items()), reverse=True)
        score, qid = scored[0]
        # near ties: "I Need to Find My Daughter" with its number dropped is 1 or 2 (picked below, by what it needs)
        alts = [q for sc, q in scored if sc >= score - 0.01 and names[q] != names[qid]]
        return qid, score, alts

    entries = []
    for line in lines:
        text = line['text']
        m = PROGRESS.match(text.strip())
        if m and entries:
            entries[-1]['raw'].append((int(m.group(1)), int(m.group(2)), m.group(3), line))
            continue
        qid, score, alts = best_quest(text)
        if score >= 0.75 and not (entries and entries[-1]['qid'] == qid):
            entries.append({'qid': qid, 'name': quests[qid]['name'], 'raw': [], 'y': line.get('y'), 'alts': alts})
        elif entries and not entries[-1]['raw'] and score >= 0.75:
            continue  # second line of a wrapped quest name
        elif entries:
            entries[-1]['raw'].append((None, None, text, line))  # a progress line whose numbers didn't read
    for e in entries:
        # Several quests can share a name ("Donating to Henesys" x17, each wanting a different item):
        # pick the one whose requirements match the progress lines under it.
        same = [qid for qid, n in names.items() if n == names[e['qid']]] + e.pop('alts', [])
        if len(same) > 1 and not e['raw']:
            e['qid'] = min(same)  # nothing under it to go by: the first of a series
            e['name'] = quests[e['qid']]['name']
        elif len(same) > 1:
            def fit(qid):
                req_names = [_norm(qdata['mob_names'].get(i, '')) for i, _ in quests[qid]['mobs']] + \
                            [_norm(qdata['items'].get(i, '')) for i, _ in quests[qid]['items']]
                return sum(max((difflib.SequenceMatcher(None, _norm(line[3]['text'] if line[3] else line[2]), r).ratio()
                                for r in req_names), default=0) for line in e['raw'])
            e["qid"] = max(sorted(same), key=fit)  # (a tie: the first of a series)
            e['name'] = quests[e['qid']]['name']
        q = quests[e['qid']]
        reqs = [('mob', i, c, qdata['mob_names'].get(i, '')) for i, c in q['mobs']] + \
               [('item', i, c, qdata['items'].get(i, '')) for i, c in q['items']]
        e['progress'] = {}
        for have, need, what, line in e.pop('raw'):
            if not reqs:
                break
            kind, rid, count, rname = _best_req(line, what, reqs)
            if kind is None:
                continue
            if need != count or have is None:
                have, need = _count_from_text(line, rname, count, have, need)
            if (need != count or have is None) and body is not None and ocr is not None and line['words']                     and (reread is None or e['qid'] in reread):
                # Re-read the numbers: they sit left of the requirement's name.
                got = read_count(body, min(margin, line['words'][0][1]) - line['h'] * 0.6, _name_start(line, rname) - 2,
                                 line['y'] - line['h'] * 0.15, line['y'] + line['h'] * 1.15, ocr, count)
                if got and got[1] == count:
                    have, need = got
            e['progress'][(kind, rid)] = (have if need == count else None, count)
        known = [hn for hn in e['progress'].values() if hn[0] is not None]
        e['complete'] = bool(reqs) and len(known) == len(reqs) and all(h >= n for h, n in known)
    return entries


def _best_req(line, what, reqs):
    """The quest requirement a progress line refers to (matched on its trailing words)."""
    words = [w[0] for w in line['words']] or what.split()
    best, best_score = (None, None, None, None), 0.5
    for r in reqs:
        n = len(r[3].split()) or 1  # words as displayed ("Maria's Letter" = 2)
        tail = ' '.join(words[-n:]) if words else what
        score = max(difflib.SequenceMatcher(None, _norm(tail), _norm(r[3])).ratio(),
                    difflib.SequenceMatcher(None, _norm(what), _norm(r[3])).ratio())
        if score > best_score:
            best, best_score = r, score
    return best


def _name_start(line, rname):
    """x where the requirement name begins in a progress line (the numbers are to its left)."""
    n = len(rname.split()) or 1
    words = line['words']
    return words[max(0, len(words) - n)][1] if words else 0


LOOKALIKE = str.maketrans({'I': '1', 'l': '1', 'i': '1', '|': '1', '!': '1', 'L': '1', 'O': '0', 'o': '0',
                           'D': '0', 'Q': '0', 'U': '0'})
SLASHES = '/$47M'  # characters the game font's "/" gets misread as


def _count_from_text(line, rname, need_known, have, need):
    """Recover "have / need" from OCR text of the numbers, knowing what "need" must be.

    The game font's digits OCR badly ("1 / 1" -> "1 $1", "7 / 10" -> "7410"), but the line must end with
    the known need, and what's before it (minus a misread slash) is the current count."""
    words = line['words'] or []
    n = len(rname.split()) or 1
    raw = ''.join(w[0] for w in words[:max(0, len(words) - n)]) if words else line['text']
    text = ''.join(ch for ch in raw if ch.isalnum() or ch in SLASHES).translate(LOOKALIKE)
    need_s = str(need_known)
    if '/' in text:
        left, _, right = text.partition('/')
        if right.endswith(need_s) and left.isdigit():
            return int(left), need_known
    if text.endswith(need_s) and len(text) > len(need_s):
        left = text[:-len(need_s)]
        # The slash may come out as "$", "4", "7", "M" ... or as "1" ("7 / 10" -> "7110"). Try the
        # readings and prefer one that doesn't exceed the need: counts above it are rare (only items).
        options = [left]
        if len(left) > 1 and (left[-1] in SLASHES or left[-1] == '1'):
            options.insert(0, left[:-1])
        options = [int(o) for o in options if o.isdigit()]
        within = [o for o in options if o <= need_known]
        if within:
            return within[0], need_known
        if options and options[-1] <= need_known * 10:
            return options[-1], need_known
    return have, need
