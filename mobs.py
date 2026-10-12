"""Monster index from the game files: name, level, HP, EXP and where each one spawns.

    py -3.11 mobs.py find <name>      # e.g. "snail" -> maps and spawn counts
(The index is rebuilt by `py -3.11 navgraph.py build`.)
"""
import difflib
import json
import pathlib
import sys

from wzreader import WzFile
import gamepath

DATA = gamepath.DATA
MOBS_JSON = pathlib.Path(__file__).with_name('mobs.json')


def _norm(s):
    return ' '.join(s.lower().replace('-', ' ').split())


def build(graph):
    wz = WzFile(str(DATA / 'String' / 'String_000.wz'))
    names = {int(k): v.get('name', '') for k, v in wz.read_image(dict(wz.images())['Mob.img']).items()
             if k.isdigit() and isinstance(v, dict)}
    wz.close()
    info = {}
    for path in sorted((DATA / 'Mob').glob('Mob_*.wz')):
        wz = WzFile(str(path))
        for name, entry in wz.images():
            stem = name.rsplit('/', 1)[-1].removesuffix('.img')
            if stem.isdigit():
                info[int(stem)] = wz.read_image(entry).get('info') or {}
        wz.close()
    mobs = {}
    for map_id, m in graph.items():
        for mob_id, (count, respawn) in m.get('mobs', {}).items():
            i = info.get(mob_id, {})
            if 'link' in i and str(i['link']).isdigit():  # stats live on the linked monster
                i = {**info.get(int(i['link']), {}), **{k: v for k, v in i.items() if k != 'link'}}
            entry = mobs.setdefault(mob_id, {'name': names.get(mob_id, f'Monster {mob_id}'), 'level': i.get('level'),
                                             'hp': i.get('maxHP'), 'exp': i.get('exp'), 'boss': bool(i.get('boss')),
                                             'maps': {}})
            entry['maps'][map_id] = [count, respawn]
    MOBS_JSON.write_text(json.dumps(mobs), encoding='utf-8')
    return mobs


def load():
    data = json.loads(MOBS_JSON.read_text(encoding='utf-8'))
    return {int(k): {**v, 'maps': {int(m): c for m, c in v['maps'].items()}} for k, v in data.items()}


def search(query, mobs):
    """Monster ids matching `query`, best first."""
    q = _norm(query)
    if not q:
        return []
    scored = []
    for mob_id, m in mobs.items():
        name = _norm(m['name'])
        if q in name:
            score = 3 + name.startswith(q)
        elif all(any(w.startswith(t) for w in name.split()) for t in q.split()):
            score = 2
        else:
            score = difflib.SequenceMatcher(None, q, name).ratio()
            if score < 0.6:
                continue
        scored.append((score, m['level'] or 0, mob_id))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [mob_id for *_, mob_id in scored]


if __name__ == '__main__' and sys.argv[1:2] == ['find']:
    from maps import load_maps
    names = {m['id']: f'{m["street"]}: {m["name"]}' for m in load_maps()}
    mobs = load()
    for mob_id in search(' '.join(sys.argv[2:]), mobs)[:5]:
        m = mobs[mob_id]
        print(f'{m["name"]} (Lv {m["level"]}, {m["hp"]} HP, {m["exp"]} EXP)')
        for map_id, (count, respawn) in sorted(m['maps'].items(), key=lambda kv: -kv[1][0]):
            extra = f', respawns every {respawn // 60} min' if respawn >= 60 else ''
            print(f'    {count:3d} spawns  {names.get(map_id, map_id)}{extra}')
