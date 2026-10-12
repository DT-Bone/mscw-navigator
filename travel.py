"""Optional non-walking travel: return scrolls and the Regular Cab.

Scroll destinations come from the item data (spec.moveTo; 999999999 = the map's own return
town). Cab stops are the maps with a "Regular Cab" NPC; where a cab can go is decided by the
server, so we assume classic behaviour: any Regular Cab town, plus Lith Harbor.
"""
import json
import pathlib

from wzreader import WzFile
import gamepath

DATA = gamepath.DATA
TRAVEL_JSON = pathlib.Path(__file__).with_name('travel.json')
NEAREST_TOWN = 999999999
LITH_HARBOR = 10000000
COST = {'portal': 1, 'scroll': 2, 'taxi': 3}  # rough effort per step, for choosing routes


def build(graph):
    s = WzFile(str(DATA / 'String' / 'String_000.wz'))
    strings = dict(s.images())
    item_names = s.read_image(strings['Consume.img'])
    npc_names = s.read_image(strings['Npc.img'])
    s.close()
    scrolls = []
    for path in sorted((DATA / 'Item').rglob('Consume_*.wz')):
        wz = WzFile(str(path))
        for name, entry in wz.images():
            if name.endswith('0203.img'):
                for item_id, v in wz.read_image(entry).items():
                    move_to = ((v or {}).get('spec') or {}).get('moveTo')
                    if move_to is not None:
                        scrolls.append({'item': int(item_id), 'to': int(move_to),
                                        'name': (item_names.get(str(int(item_id))) or {}).get('name', f'Item {item_id}')})
        wz.close()
    cab_ids = {int(k) for k, v in npc_names.items() if isinstance(v, dict) and v.get('name') == 'Regular Cab'}
    cabs = {map_id: [x, y] for map_id, m in graph.items() for npc, x, y in m.get('npcs', []) if npc in cab_ids}
    destinations = sorted(set(cabs) | ({LITH_HARBOR} & set(graph)))
    data = {'scrolls': scrolls, 'cabs': cabs, 'cab_destinations': destinations}
    TRAVEL_JSON.write_text(json.dumps(data), encoding='utf-8')
    return data


def load():
    data = json.loads(TRAVEL_JSON.read_text(encoding='utf-8'))
    data['cabs'] = {int(k): v for k, v in data['cabs'].items()}
    return data


def extra_edges(graph, travel, map_id, opts):
    """Scroll/taxi steps available from map_id under the chosen options."""
    edges = []
    if not travel or not opts:
        return edges
    if opts.get('scrolls'):
        seen = set()
        for s in travel['scrolls']:
            to = graph[map_id].get('return') if s['to'] == NEAREST_TOWN else s['to']
            if to and to != NEAREST_TOWN and to != map_id and to in graph and to not in seen:
                seen.add(to)
                edges.append({'kind': 'scroll', 'to': to, 'item': s['name']})
    if opts.get('taxi') and map_id in travel['cabs']:
        for to in travel['cab_destinations']:
            if to != map_id and to in graph:
                edges.append({'kind': 'taxi', 'to': to, 'npc': travel['cabs'][map_id]})
    return edges
