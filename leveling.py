"""Leveling routes for levels 1-45, from TokiLee's Maple Classic Guide (https://mapleclassic.guide/leveling).

leveling.json holds the guide's two routes, summarised, with its maps matched to the game's map ids ("Horny
Mushroom Tree" is Tree Dungeon, Forest Up North II and "Slime Tree" is Dungeon, Southern Forest I: their
monster spawns match the guide's exactly). Each stage is for one route ("A", "B") or both.
"""
import json
import pathlib

FILE = pathlib.Path(__file__).with_name('leveling.json')
_data = None


def data():
    global _data
    if _data is None:
        _data = json.loads(FILE.read_text(encoding='utf-8'))
    return _data


def stages(route):
    """The stages to follow on a route, in order (its own and the shared ones)."""
    return [s for s in data()['stages'] if s['route'] in (route, 'both')]


def current(route_stages, level):
    """Index of the stage for a character level: the first that the level is inside of and not yet at the
    end of; else the last one it's in; else None."""
    if not level:
        return None
    inside = [i for i, s in enumerate(route_stages) if s['levels'][0] <= level <= s['levels'][1]]
    return next((i for i in inside if level < route_stages[i]['levels'][1]), inside[-1] if inside else None)


def levels_text(stage):
    lo, hi = stage['levels']
    return f'Lv {lo}' if lo == hi else f'Lv {lo}–{hi}'


def details(stage, route):
    """The stage's steps, items to collect and quests (on this route), as lines of text."""
    lines = [f'{levels_text(stage)}  ·  {stage["title"]}']
    lines += [f'• {step}' for step in stage.get('steps', [])]
    if stage.get('collect'):
        lines.append('Collect: ' + ', '.join(f'{name} ×{n}' for name, n in stage['collect']))
    for key, label in (('pick up', 'Pick up'), ('complete', 'Complete')):
        if isinstance(stage.get(key), dict) and route is None:  # different on each route
            for r, names in stage[key].items():
                lines.append(f'{label} (Route {r}): ' + ', '.join(names))
        elif quest_list(stage, route, key):
            lines.append(f'{label}: ' + ', '.join(quest_list(stage, route, key)))
    return lines


def sections():
    """The stages as the Leveling tab lists them: [(heading or None, route or None, [stages])] - the shared start,
    each route from level 10, then the stages both routes meet again at."""
    shared = data()['stages'][:shared_count()]
    out = [(None, None, shared)]
    for key, route in data()['routes'].items():
        first = route['summary'].split('.')[0]
        out.append((f'From level 10, {route["name"]}: {first}' if not out[1:] else f'... or {route["name"]}: {first}',
                    key, [st for st in data()['stages'] if st['route'] == key]))
    tail = [st for st in data()['stages'][shared_count():] if st['route'] == 'both']
    out.append(('Both routes again', None, tail))
    return out


_quest_names = None


def resolve_quest(name, qdata):
    """Quest ids for a quest or questline name from the guide: the quest with that name, else every quest named
    "<name> <n>" ("Rain's Maple Quiz" -> Rain's Maple Quiz 1-7). [] if the game has none by that name."""
    global _quest_names
    if _quest_names is None:
        _quest_names = {}
        for qid in sorted(qdata['quests']):
            _quest_names.setdefault(qdata['quests'][qid]['name'].lower(), qid)
    n = name.lower()
    if n in _quest_names:
        return [_quest_names[n]]
    return sorted(qid for nm, qid in _quest_names.items() if nm.startswith(n + ' '))


def quest_list(stage, route, key):
    """A stage's quests to 'pick up' or 'complete' on a route (some stages list them per route)."""
    v = stage.get(key) or []
    return v.get(route, []) if isinstance(v, dict) else v


def tasks(stage, route, qdata):
    """[(quest id, 'pick up' | 'complete')] for a stage, in the order the guide does them (town by town): a quest
    to pick up goes where its steps first name it, one to complete where they last do (accepted in one town,
    handed in at another later on); a quest the steps don't name stays right after the one listed before it.
    A quest picked up and completed at the same point is just a 'complete'; any quest goes after the stage's
    quests it needs (Sam's Suggestion before Mai's Training)."""
    text = ' '.join(stage.get('steps', [])).lower()
    entries = {}  # (quest id, kind) -> sort key
    for key in ('complete', 'pick up'):
        last = -1  # where the previous quest of the list is named
        for k, name in enumerate(quest_list(stage, route, key)):
            found = text.rfind(name.lower()) if key == 'complete' else text.find(name.lower())
            last = found if found >= 0 else last
            for j, qid in enumerate(resolve_quest(name, qdata)):
                entries[(qid, key)] = (last, k, j)
    for qid, key in list(entries):
        if key == 'pick up' and (qid, 'complete') in entries and entries[(qid, 'pick up')] >= entries[(qid, 'complete')]:
            del entries[(qid, 'pick up')]
    ordered, placed = [], set()

    def place(entry, path=()):
        if entry in placed or entry in path:
            return
        qid, kind = entry
        before = [(p, k) for p in qdata['quests'].get(qid, {}).get('prereq') or [] for k in ('pick up', 'complete')]
        if kind == 'complete':
            before.append((qid, 'pick up'))
        for b in before:
            if b in entries:
                place(b, path + (entry,))
        placed.add(entry)
        ordered.append(entry)
    for entry in sorted(entries, key=entries.get):
        place(entry)
    return ordered


def shared_count():
    """How many stages both routes start with (Maple Island and Thicket Around the Beach): the route is chosen
    after them, at level 10."""
    n = 0
    for st in data()['stages']:
        if st['route'] != 'both':
            break
        n += 1
    return n
