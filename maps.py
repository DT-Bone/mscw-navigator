"""Look up maps by name in String.wz (Map.img).

Usage:
    py -3.11 maps.py export              # write maps.json (id, region, street, name)
    py -3.11 maps.py find <text>         # fuzzy-search map/street names, e.g. OCR'd minimap text
"""
import difflib
import json
import pathlib
import sys

from wzreader import WzFile
import gamepath

STRING_WZ = str(gamepath.DATA / 'String' / 'String_000.wz')
MAPS_JSON = pathlib.Path(__file__).with_name('maps.json')


def load_maps():
    wz = WzFile(STRING_WZ)
    tree = wz.read_image(dict(wz.images())['Map.img'])
    wz.close()
    maps = []
    for region, entries in tree.items():
        for map_id, info in entries.items():
            if isinstance(info, dict):
                maps.append({'id': int(map_id), 'region': region,
                             'street': info.get('streetName', ''), 'name': info.get('mapName', '')})
    return sorted(maps, key=lambda m: m['id'])


def _norm(s):
    return ' '.join(s.lower().replace(':', ' ').replace('-', ' ').split())


def find(text, maps, limit=5):
    """Rank maps by similarity to `text` (which may contain street and/or map name)."""
    q = _norm(text)
    scored = []
    for m in maps:
        name, street = _norm(m['name']), _norm(m['street'])
        full = f'{street} {name}'
        # The minimap shows "street" then "name"; a bare name can be a substring of another
        # map's full title (e.g. "Victoria Road"), so exact containment only counts for the full title.
        score = max(difflib.SequenceMatcher(None, q, name).ratio(),
                    difflib.SequenceMatcher(None, q, full).ratio(),
                    1.0 if full == q else 0.0)
        scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return scored[:limit]


if __name__ == '__main__':
    maps = load_maps()
    if sys.argv[1] == 'export':
        MAPS_JSON.write_text(json.dumps(maps, indent=1, ensure_ascii=False), encoding='utf-8')
        print(f'wrote {len(maps)} maps to {MAPS_JSON}')
    elif sys.argv[1] == 'find':
        for score, m in find(' '.join(sys.argv[2:]), maps):
            print(f'{score:.2f}  {m["id"]:>9}  {m["street"]} : {m["name"]}')
