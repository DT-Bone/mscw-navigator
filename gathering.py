"""Quest items you get by breaking things on a map (wooden boxes, coconuts, herbs) instead of from monsters.

The maps place these "reactors" (portals.json 'reactors', from navgraph.py) and Reactor.wz names them, but what
one drops is decided by the server, so the items are matched to them here by hand, going by the quests' own words.
"""
from collections import namedtuple

# reactors: their ids (Reactor.wz); plural / single: what to call their spots (spawns: once broken, a thing is gone
# until it comes back); how: where to get the item, for the quest guide; maps: the only maps to look on (None: any map
# that has them); sure: the quest says so (else a good guess)
Source = namedtuple('Source', 'reactors plural single how maps sure')

_BOXES = Source(('0000001',), 'crate spawns', 'Crate Spawn', 'break the wooden crates around Amherst',
                (1010, 1011, 1012, 1013), True)
SOURCES = {
    # Pio's Collecting Recycled Goods: "break down wooden boxes from around town" (Amherst, and its shops)
    4031003: _BOXES,  # Rusty Screw
    4031004: _BOXES,  # Old Wooden Board
    # Riel's Special Taste of Florina Beach: "start collecting those delicious looking Coconuts over there"
    4000061: Source(('0000005', '0000006', '0000007'), 'coconut spawns', 'Coconut Spawn',
                    'hit the coconuts around Florina Beach', None, True),
    # Wing the Fairy's Flying Medicine: Witchgrass Leaf, from the witch's herbs in the swamps
    4031012: Source(('0000004',), 'herb spawns', 'Herb Spawn', "hit the witch's herbs in the swamps", None, True),
    # Dr. Betty's Plant Samples: "a lot of plants covering the woods in Ellinia": the Ellinia flowers, most likely
    4031072: Source(('0000003',), 'flower spawns', 'Flower Spawn',
                    'probably from the flowers in the woods north of Ellinia, going by the quest text', None, False),
}

_spots = {}


def spots(graph, item_id):
    """{map id: [(x, y), ...]}: where to break things for an item ({} if it isn't one of these, or the game data
    was built before reactors were in it)."""
    src = SOURCES.get(item_id)
    if src is None:
        return {}
    key = (id(graph), item_id)
    if key not in _spots:
        out = {}
        for map_id in src.maps or graph:
            pts = [(x, y) for rid, x, y in (graph.get(map_id) or {}).get('reactors') or [] if rid in src.reactors]
            if pts:
                out[map_id] = pts
        _spots[key] = out
    return _spots[key]


def best_map(where, dist, walk_seconds):
    """The map to go to: close by, and with plenty to break (each one is worth about 5 seconds of walking)."""
    reachable = [m for m in where if m in dist] or list(where)
    return min(reachable, key=lambda m: dist.get(m, 99) * walk_seconds - len(where[m]) * 5) if reachable else None
