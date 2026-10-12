"""NPC index from the game files: name, role, where each NPC stands, and the quests it starts/ends.

    py -3.11 npcs.py find <name or role>      # e.g. "fredrick", "storage"
(The index is rebuilt by `py -3.11 navgraph.py build`.)
"""
import difflib
import json
import pathlib
import sys

from wzreader import WzFile
import gamepath

DATA = gamepath.DATA
NPCS_JSON = pathlib.Path(__file__).with_name('npcs.json')


def _norm(s):
    return ' '.join(s.lower().replace('-', ' ').split())


def build(graph):
    s = WzFile(str(DATA / 'String' / 'String_000.wz'))
    strings = s.read_image(dict(s.images())['Npc.img'])
    s.close()
    npcs = {}
    for map_id, m in graph.items():
        for npc_id, x, y in m.get('npcs', []):
            info = strings.get(str(npc_id)) or {}
            entry = npcs.setdefault(npc_id, {'name': info.get('name') or f'NPC {npc_id}', 'func': info.get('func', ''),
                                             'maps': {}, 'starts': [], 'ends': []})
            entry['maps'][map_id] = [x, y]
    # Quests: Check/0 holds start requirements (npc, lvmin), Check/1 the completion NPC.
    for path in sorted((DATA / 'Quest').rglob('QuestData_*.wz')):
        wz = WzFile(str(path))
        for name, entry in wz.images():
            stem = name.rsplit('/', 1)[-1].removesuffix('.img')
            if not stem.isdigit():
                continue
            q = wz.read_image(entry)
            check = q.get('Check') or {}
            start, end = check.get('0') or {}, check.get('1') or {}
            quest = {'id': int(stem), 'name': (q.get('QuestInfo') or {}).get('name', f'Quest {stem}'),
                     'lvmin': start.get('lvmin')}
            if start.get('npc') in npcs:
                npcs[start['npc']]['starts'].append(quest)
            if end.get('npc') in npcs:
                npcs[end['npc']]['ends'].append(quest)
        wz.close()
    NPCS_JSON.write_text(json.dumps(npcs), encoding='utf-8')
    return npcs


def load():
    data = json.loads(NPCS_JSON.read_text(encoding='utf-8'))
    return {int(k): {**v, 'maps': {int(m): xy for m, xy in v['maps'].items()}} for k, v in data.items()}


def search(query, npcs, tiebreak=lambda npc_id: 0):
    """NPC ids whose name or role matches `query`, best first (equal matches ordered by tiebreak)."""
    q = _norm(query)
    if not q:
        return []
    scored = []
    for npc_id, n in npcs.items():
        name, func = _norm(n['name']), _norm(n['func'])
        if q in name:
            score = 3 + name.startswith(q)
        elif q in func:
            score = 2.5
        elif all(any(w.startswith(t) for w in (name + ' ' + func).split()) for t in q.split()):
            score = 2
        else:
            score = difflib.SequenceMatcher(None, q, name).ratio()
            if score < 0.7:
                continue
        scored.append((score, npc_id))
    scored.sort(key=lambda s: (-s[0], tiebreak(s[1])))
    return [npc_id for _, npc_id in scored]


if __name__ == '__main__' and sys.argv[1:2] == ['find']:
    from maps import load_maps
    names = {m['id']: f'{m["street"]}: {m["name"]}' for m in load_maps()}
    npcs = load()
    for npc_id in search(' '.join(sys.argv[2:]), npcs)[:6]:
        n = npcs[npc_id]
        role = f' ({n["func"]})' if n['func'] else ''
        print(f'{n["name"]}{role}: starts {len(n["starts"])} quests, ends {len(n["ends"])}')
        for map_id, (x, y) in n['maps'].items():
            print(f'    {names.get(map_id, map_id)} at ({x},{y})')
        for q in n['starts'][:4]:
            print(f'    quest: {q["name"]} (Lv {q["lvmin"]})')
