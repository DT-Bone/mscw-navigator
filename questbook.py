"""Right-click a quest in the game's quest book (Q) to open its guide: the line of text under the cursor is read and
matched to a quest name. Works anywhere a quest's name is written out (the book's list and its detail pane, the
Quest Helper box); item tooltips are dark boxes and are read first (itemhover), so they never get here.
"""
import difflib
import re

from questlog import _lines, _norm

HALF_WIDTH = 260   # UI px read each side of the cursor (the quest book's list is 247 wide)
HALF_HEIGHT = 14   # UI px above and below it (one line of text, and the cursor drawn over it)
MIN_SCORE = 0.85   # how closely the text has to match a quest's name
JOB_IDS = {'Warrior': 100, 'Magician': 200, 'Bowman': 300, 'Thief': 400, 'Pirate': 500}

_names = {}


def _quest_names(qdata):
    key = id(qdata)
    if key not in _names:
        _names[key] = {qid: _norm(q['name']) for qid, q in qdata['quests'].items()}
    return _names[key]


def area(cx, cy, gw, gh):
    """(x0, y0, x1, y1) to read, game window coords, around a right-click at cx, cy."""
    s = gh / 768
    return (int(max(0, cx - HALF_WIDTH * s)), int(max(0, cy - HALF_HEIGHT * s)),
            int(min(gw, cx + HALF_WIDTH * s)), int(min(gh, cy + HALF_HEIGHT * s)))


def _under(words, cx, cy):
    """The text under (cx, cy): its line, cut where words are far apart (the quest book's list and detail pane sit
    side by side), the piece nearest the cursor."""
    line = next((l for l in _lines(words) if l['y'] - l['h'] * 0.4 <= cy <= l['y'] + l['h'] * 1.4), None)
    if line is None:
        return ''
    pieces = []
    for w in line['words']:
        if pieces and w[1] - (pieces[-1][-1][1] + pieces[-1][-1][3]) <= 1.5 * line['h']:
            pieces[-1].append(w)
        else:
            pieces.append([w])
    near = min(pieces, key=lambda p: max(0, p[0][1] - cx, cx - (p[-1][1] + p[-1][3])))
    return ' '.join(w[0] for w in near)


def quest_under(words, cx, cy, qdata, prefer=(), job_class=None):
    """The quest whose name is written under (cx, cy) (same coords as the OCR words), or None.

    A name cut off with "..." matches the quests it's the start of. Of several that fit (quests sharing a name, like
    "Finding the Instructor", one per job; a series whose number didn't read), the one in `prefer` (the Quest Helper
    box), else the one for job_class, else the first."""
    raw = _under(words, cx, cy).strip()
    cut = bool(re.search(r'(\.\.\.|…)\s*$', raw))
    t = _norm(re.sub(r'(\.\.\.|…)\s*$', '', raw))
    if len(t) < 4:
        return None
    names = _quest_names(qdata)
    same = [qid for qid, n in names.items() if n == t]
    if not same and cut and len(t) >= 8:
        same = [qid for qid, n in names.items() if n.startswith(t)]
    if not same:
        scored = sorted(((difflib.SequenceMatcher(None, t, n).ratio(), qid) for qid, n in names.items()), reverse=True)
        if scored[0][0] < MIN_SCORE:
            return None
        # near ties too: "I Need to Find My Daughter" with its number misread is 1 or 2
        same = [qid for score, qid in scored if score >= scored[0][0] - 0.01]
    for qid in sorted(same):
        if qid in prefer:
            return qid
    job = JOB_IDS.get(job_class)
    for_job = [qid for qid in same if job and any(j // 100 == job // 100 for j in qdata['quests'][qid]['jobs'] or ())]
    return min(for_job or same)  # else the first of a series
