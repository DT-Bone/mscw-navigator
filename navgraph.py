"""Build a map-to-map graph from portal data and find walking routes.

Usage:
    py -3.11 navgraph.py build                 # scan Map wz files -> portals.json
    py -3.11 navgraph.py route <from> <to>     # map ids or names, e.g. 80002000 "Henesys"
"""
import heapq
import json
import pathlib
import sys

import travel
from maps import find, load_maps
from wzreader import WzFile
import gamepath

MAP_DIR = gamepath.DATA / 'Map' / 'Map'
PORTALS_JSON = pathlib.Path(__file__).with_name('portals.json')
NO_MAP = 999999999

# Scripted portals whose destination is decided server-side; filled in by hand.
# market00 (Free Market exit) returns you to the town you entered from.
SCRIPT_TARGETS = {
    'market01': [80002000],
    'market02': [80002000],
    'market00': [10001040, 10004000],
}


def _targets(portal):
    if portal['to'] is not None:
        return [portal['to']]
    return SCRIPT_TARGETS.get(portal['script'], [])


def _spawns(life):
    out = {}
    for v in life.values():
        if isinstance(v, dict) and v.get('type') == 'm' and not v.get('hide') and str(v.get('id', '')).isdigit():
            entry = out.setdefault(int(v['id']), [0, 0])
            entry[0] += 1
            entry[1] = max(entry[1], v.get('mobTime') or 0)
    return out


def _timed_spawns(life):
    out = {}
    for v in life.values():
        if isinstance(v, dict) and v.get('type') == 'm' and not v.get('hide') and str(v.get('id', '')).isdigit()                 and (v.get('mobTime') or 0) > 0:
            out[int(v['id'])] = out.get(int(v['id']), 0) + 1
    return out


def build():
    graph = {}
    for path in sorted(MAP_DIR.rglob('*.wz')):
        if path.name.startswith('_Canvas'):
            continue
        wz = WzFile(str(path))
        for name, entry in wz.images():
            stem = name.rsplit('/', 1)[-1].removesuffix('.img')
            if not stem.isdigit():
                continue
            img = wz.read_image(entry)
            info = img.get('info') or {}
            portals = []
            raw = [p for p in (img.get('portal') or {}).values() if isinstance(p, dict)]
            names_here = {p.get('pn') for p in raw}
            for p in raw:
                tm = p.get('tm', NO_MAP)
                script = p.get('script') or ''
                if tm in (NO_MAP, None) and p.get('tn') and p.get('tn') in names_here and p.get('pt') not in (0, None):
                    tm = int(stem)  # a teleporter to another spot on this map, written without a target map
                if tm in (NO_MAP, None) and not script:
                    continue
                portals.append({'name': p.get('pn', ''), 'x': p.get('x'), 'y': p.get('y'),
                                'to': None if tm == NO_MAP else tm, 'to_portal': p.get('tn', ''),
                                # portal type: 2 = visible, 1 = invisible, 10 = only shown up close, ...
                                'script': script, 'pt': p.get('pt')})
            mm = img.get('miniMap') or {}
            graph[int(stem)] = {
                'town': bool(info.get('town')), 'return': info.get('returnMap'), 'portals': portals,
                # minimap geometry: picture size in map units and the map-origin offset
                'mm': {'w': mm['width'], 'h': mm['height'], 'cx': mm.get('centerX', 0),
                       'cy': mm.get('centerY', 0), 'mag': mm.get('mag', 4)} if mm.get('width') else None,
                # visible NPCs (green minimap markers), used to pin down scrolling minimaps
                'npcs': [(int(v['id']), v['x'], v['y']) for v in (img.get('life') or {}).values()
                         if isinstance(v, dict) and v.get('type') == 'n' and not v.get('hide')
                         and str(v.get('id', '')).isdigit()],
                # monster spawn points: {mob id: [count, longest respawn timer in seconds]}
                'mobs': _spawns(img.get('life') or {}),
                # how many of those spawn points are on a respawn timer (bosses etc.), useless for farming
                'mob_timed': _timed_spawns(img.get('life') or {}),
                # camera limits (left, top, right, bottom)
                'vr': [info['VRLeft'], info['VRTop'], info['VRRight'], info['VRBottom']]
                      if all(k in info for k in ('VRLeft', 'VRTop', 'VRRight', 'VRBottom')) else None,
            }
        wz.close()
    # Where each map's minimap image lives (the _Canvas files), for matching scrolling minimaps.
    for path in sorted(MAP_DIR.rglob('_Canvas*.wz')):
        wz = WzFile(str(path))
        for name, entry in wz.images():
            stem = name.rsplit('/', 1)[-1].removesuffix('.img')
            if stem.isdigit() and int(stem) in graph:
                graph[int(stem)]['mm_canvas'] = [str(path.relative_to(MAP_DIR)), name]
        wz.close()
    PORTALS_JSON.write_text(json.dumps(graph), encoding='utf-8')
    return graph


def load_graph():
    graph = {int(k): v for k, v in json.loads(PORTALS_JSON.read_text(encoding='utf-8')).items()}
    for v in graph.values():  # JSON object keys are strings
        v['mobs'] = {int(k): c for k, c in (v.get('mobs') or {}).items()}
        v['mob_timed'] = {int(k): c for k, c in (v.get('mob_timed') or {}).items()}
    return graph


def _edges(graph, cur, opts, travel_data, at_start):
    """Steps out of `cur`: portals, plus return scrolls / cabs when enabled in opts."""
    for p in graph.get(cur, {}).get('portals', []):
        targets = _targets(p)
        if p['script'] == 'market00':
            # The Free Market exit returns you to the town you came in from, so it only helps
            # when you're already inside; opts['fm_return'] is that town if the tracker saw it.
            if not at_start:
                continue
            back = (opts or {}).get('fm_return')
            targets = [back] if back else targets
        for nxt in targets:
            if nxt != cur and nxt in graph:
                yield {'kind': 'portal', 'to': nxt, 'portal': p}
    yield from travel.extra_edges(graph, travel_data, cur, opts)


def _search(graph, start, opts, travel_data, goal=None):
    """Cheapest paths from start (cost per step from travel.COST; ties broken by fewer steps)."""
    best, prev = {start: (0, 0)}, {start: None}
    heap = [(0, 0, start)]
    while heap:
        cost, steps, cur = heapq.heappop(heap)
        if (cost, steps) > best[cur]:
            continue
        if cur == goal:
            break
        for e in _edges(graph, cur, opts, travel_data, cur == start):
            cand = (cost + travel.COST[e['kind']], steps + 1)
            if e['to'] not in best or cand < best[e['to']]:
                best[e['to']], prev[e['to']] = cand, (cur, e)
                heapq.heappush(heap, (*cand, e['to']))
    return best, prev


def distances(graph, start, opts=None, travel_data=None):
    """Steps along the best route from `start` to every reachable map."""
    best, _ = _search(graph, start, opts, travel_data)
    return {m: steps for m, (_, steps) in best.items()}


def route(graph, start, goal, opts=None, travel_data=None):
    """Best path as [(map_id, step taken to leave it), ..., (goal, None)], or None.

    A step is {'kind': 'portal', 'portal': ...}, {'kind': 'scroll', 'item': ...} or {'kind': 'taxi', 'npc': [x, y]}.
    """
    _, prev = _search(graph, start, opts, travel_data, goal)
    if goal not in prev:
        return None
    steps, node = [], goal
    while prev[node] is not None:
        cur, e = prev[node]
        steps.append((cur, e))
        node = cur
    return list(reversed(steps)) + [(goal, None)]


def describe(step):
    if step['kind'] == 'scroll':
        return f'use {step["item"]}'
    if step['kind'] == 'taxi':
        return 'take the Regular Cab'
    p = step['portal']
    text = f'take portal "{p["name"]}" at x={p["x"]}'
    if p['script'] == 'market00':
        text += ' (Free Market exit: returns you to the town you entered from)'
    return text


def resolve(arg, maps):
    if arg.isdigit():
        return int(arg)
    score, m = find(arg, maps, limit=1)[0]
    return m['id']


if __name__ == '__main__':
    if sys.argv[1] == 'build':
        g = build()
        edges = sum(1 for m in g.values() for p in m['portals'] if p['to'])
        scripted = sum(1 for m in g.values() for p in m['portals'] if p['script'] and not p['to'])
        print(f'{len(g)} maps, {edges} portal links, {scripted} scripted portals without a fixed target')
        import mobs
        print(f'{len(mobs.build(load_graph()))} monsters indexed')
        t = travel.build(load_graph())
        print(f'{len(t["scrolls"])} return scrolls, Regular Cab in {len(t["cabs"])} maps')
        import npcs
        n = npcs.build(load_graph())
        print(f'{len(n)} NPCs indexed, {sum(len(v["starts"]) for v in n.values())} quest starts')
        import quests
        qd = quests.build(load_graph())
        print(f'{len(qd["quests"])} quests indexed, drop source guessed for '
              f'{sum(1 for v in qd["item_sources"].values() if v)}/{len(qd["item_sources"])} required items')
        import items
        it = items.build(load_graph())
        print(f'{len(it)} items indexed, {sum(1 for v in it.values() if v["price"])} with a shop price')
        print(f'{items.build_icons(it)} item icons cached')
        import crafting
        print(f'{len(crafting.build()["recipes"])} crafting recipes indexed')
    elif sys.argv[1] == 'route':
        # route <from> <to> [--scrolls] [--taxi]
        maps = load_maps()
        names = {m['id']: f'{m["street"]}: {m["name"]}' for m in maps}
        g = load_graph()
        args = [a for a in sys.argv[2:] if not a.startswith('--')]
        opts = {'scrolls': '--scrolls' in sys.argv, 'taxi': '--taxi' in sys.argv}
        a, b = resolve(args[0], maps), resolve(args[1], maps)
        steps = route(g, a, b, opts, travel.load())
        if steps is None:
            print(f'no route from {names.get(a, a)} to {names.get(b, b)}')
        else:
            for map_id, step in steps:
                print(f'{map_id:>9}  {names.get(map_id, "?")}  -> {describe(step) if step else "(arrived)"}')
