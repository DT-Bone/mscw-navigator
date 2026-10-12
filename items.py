"""Item index from the game files: names, descriptions, what NPC shops pay, and the details shown in
the item info panel (requirements and stats of equipment, effects of potions and scrolls, flags).

    py -3.11 items.py find <item name>
(The index is rebuilt by `py -3.11 navgraph.py build`.)

An item's info/price is what a shop pays per item. Throwing stars and bullets also have a
unitPrice per piece (they're recharged, not stacked). Items flagged notSale can't be sold.
Drop tables are server-side, so the monsters that drop an item are only guessed from its name
and description ("Snail Shell" -> Snail).
"""
import json
import pathlib
import re
import struct
import sys

from wzreader import WzFile
import gamepath

DATA = gamepath.DATA
ITEMS_JSON = pathlib.Path(__file__).with_name('items.json')
# Where item records live: Item/<category> holds groups of items per image, Character/<slot> one per image.
ITEM_DIRS = {'Item/Etc': 'Etc', 'Item/Consume': 'Use', 'Item/Install': 'Set-up'}
EQUIP_DIRS = {'Accessory': 'Accessory', 'Cap': 'Hat', 'Cape': 'Cape', 'Coat': 'Top', 'Glove': 'Gloves',
              'Longcoat': 'Overall', 'Pants': 'Bottom', 'Ring': 'Ring', 'Shield': 'Shield', 'Shoes': 'Shoes',
              'Weapon': 'Weapon'}
# Equipment types by the first digits of the item id.
EQUIP_TYPES = {101: 'Face accessory', 102: 'Eye accessory', 103: 'Earrings', 111: 'Ring', 112: 'Ring', 113: 'Belt',
               114: 'Medal', 130: 'One-handed Sword', 131: 'One-handed Axe', 132: 'One-handed Blunt Weapon',
               133: 'Dagger', 137: 'Wand', 138: 'Staff', 140: 'Two-handed Sword', 141: 'Two-handed Axe',
               142: 'Two-handed Blunt Weapon', 143: 'Spear', 144: 'Polearm', 145: 'Bow', 146: 'Crossbow',
               147: 'Claw'}
STATS = {'STR': 'STR', 'DEX': 'DEX', 'INT': 'INT', 'LUK': 'LUK', 'MHP': 'Max HP', 'MMP': 'Max MP',
         'WAT': 'Weapon Att', 'PAD': 'Weapon Att', 'MAD': 'Magic Att', 'PDD': 'Weapon Def', 'MDD': 'Magic Def',
         'ACC': 'Accuracy', 'EVA': 'Avoidability', 'Speed': 'Speed', 'Jump': 'Jump', 'CRT': 'Critical Rate',
         'CRD': 'Critical Damage'}
JOBS = ((1, 'Warrior'), (2, 'Magician'), (4, 'Bowman'), (8, 'Thief'), (16, 'Pirate'))
FLAGS = {'quest': 'Quest item', 'tradeBlock': 'Untradeable', 'only': 'One of a kind', 'accountSharable':
         'Shared by your account', 'timeLimited': 'Expires', 'noDrop': "Can't be dropped"}


def _strings():
    """{item id: (name, description)} for every kind of item."""
    s = WzFile(str(DATA / 'String' / 'String_000.wz'))
    strings = dict(s.images())
    out = {}

    def walk(tree):
        for k, v in tree.items():
            if isinstance(v, dict):
                if k.isdigit() and 'name' in v:
                    out[int(k)] = (v['name'], v.get('desc', ''))
                else:
                    walk(v)
    for img in ('Etc.img', 'Consume.img', 'Ins.img', 'Eqp.img'):
        walk(s.read_image(strings[img]))
    s.close()
    return out


def _desc(text):
    """Description without the game's colour codes and escaped line breaks."""
    if not isinstance(text, str):
        return ''
    text = re.sub(r'#[a-z]', '', text.replace('\\n', '\n').replace('\\r', ''))
    return re.sub(r'[ \t]+', ' ', text).strip()


def _stats(info):
    """{label: value} for the stats an item adds ("incPDD" -> "Weapon Def")."""
    out = {}
    for k, v in info.items():
        key = k.replace('\n', '').removeprefix('inc')
        if k.replace('\n', '').startswith('inc') and key in STATS and isinstance(v, (int, float)) and v:
            out[STATS[key]] = out.get(STATS[key], 0) + v
    return out


def _record(info, spec, name, desc, category, kind=None):
    rec = {'name': name, 'cat': category, 'price': int(info.get('price') or 0)}
    if kind:
        rec['type'] = kind
    if desc:
        rec['desc'] = _desc(desc)
    if info.get('unitPrice'):
        rec['unit'] = float(info['unitPrice'])
    if info.get('notSale'):
        rec['no_sale'] = True
    if category != 'Equip':
        rec['max'] = int(info.get('slotMax') or 100)
    flags = [label for key, label in FLAGS.items() if info.get(key)]
    if flags:
        rec['flags'] = flags
    req = {k[3:]: info[k] for k in ('reqLevel', 'reqSTR', 'reqDEX', 'reqINT', 'reqLUK') if info.get(k)}
    try:
        job = int(info.get('reqJob') or 0)  # a bit per job class (stored as text on a few items)
    except (TypeError, ValueError):
        job = 0
    if job < 0:
        req['Job'] = 'Beginner'
    elif job:
        jobs = [label for bit, label in JOBS if job & bit]
        if jobs:
            req['Job'] = ' / '.join(jobs)
    if req:
        rec['req'] = req
    stats = _stats(info)
    if stats:
        rec['stats'] = stats
    if category == 'Equip':
        rec['slots'] = int(info.get('tuc') or 0)
        if info.get('attackSpeed'):
            rec['speed'] = int(info['attackSpeed'])
    if info.get('success'):  # scrolls
        rec['scroll'] = {'success': info['success'], 'destroy': info.get('cursed', 0)}
    effect = {k: v for k, v in (spec or {}).items()
              if k in ('hp', 'mp', 'hpR', 'mpR', 'time', 'pad', 'mad', 'pdd', 'mdd', 'acc', 'eva', 'speed', 'jump',
                       'moveTo', 'exp', 'expBuff', 'crt', 'crd') and isinstance(v, (int, float))}
    if effect:
        rec['effect'] = effect
    return rec


def build(graph=None):
    """Writes items.json. With the map graph, Etc items also get guessed monster sources."""
    strings = _strings()
    items = {}
    for folder, category in ITEM_DIRS.items():
        for path in sorted((DATA / folder).glob('*_000.wz')):
            wz = WzFile(str(path))
            for _, entry in wz.images():
                for key, item in wz.read_image(entry).items():
                    if key.isdigit() and isinstance(item, dict) and int(key) in strings:
                        name, desc = strings[int(key)]
                        items[int(key)] = _record(item.get('info') or {}, item.get('spec'), name, desc, category)
                        items[int(key)]['src'] = folder
            wz.close()
    for slot, kind in EQUIP_DIRS.items():
        for path in sorted((DATA / 'Character' / slot).glob('*_000.wz')):
            wz = WzFile(str(path))
            for img_name, entry in wz.images():
                stem = img_name.rsplit('/', 1)[-1].removesuffix('.img')
                info = wz.read_image(entry).get('info') or {}
                # equipment without a price is cash-shop gear, which shops don't buy
                if stem.isdigit() and int(stem) in strings and 'price' in info and not info.get('cash'):
                    item_id = int(stem)
                    name, desc = strings[item_id]
                    items[item_id] = _record(info, None, name, desc, 'Equip',
                                             EQUIP_TYPES.get(item_id // 10000, kind))
                    items[item_id]['src'] = f'Character/{slot}'
            wz.close()
    if graph:
        _guess_sources(items, graph)
    ITEMS_JSON.write_text(json.dumps(items), encoding='utf-8')
    return items


def _guess_sources(items, graph):
    """Monsters an Etc item probably drops from, by monster names in its name/description."""
    s = WzFile(str(DATA / 'String' / 'String_000.wz'))
    mob_names = {int(k): v.get('name', '') for k, v in s.read_image(dict(s.images())['Mob.img']).items()
                 if k.isdigit() and isinstance(v, dict)}
    s.close()
    spawning = sorted({m for g in graph.values() for m in g.get('mobs', {}) if mob_names.get(int(m))},
                      key=lambda m: -len(mob_names[int(m)]))  # longest first: "Dark Stump" before "Stump"
    for rec in items.values():
        if rec['cat'] != 'Etc':
            continue
        text, found = f'{rec["name"]} {rec.get("desc", "")}'.lower(), []
        for mob in spawning:
            mn = mob_names[int(mob)].lower()
            if re.search(r'\b' + re.escape(mn) + r's?\b', text) and not any(mn in mob_names[f].lower() for f in found):
                found.append(int(mob))
        if found:
            rec['sources'] = found


_icons = {}


def icon(item_id, rec):
    """The item's inventory icon as an RGBA array (from the game files), or None."""
    if item_id in _icons:
        return _icons[item_id]
    import canvas
    out = None
    try:
        key = f'{item_id:08d}'
        folder = DATA / rec['src']
        wz = WzFile(str(next(folder.glob('*_000.wz'))))
        imgs = dict(wz.images())
        node = wz.read_image(imgs[f'{key}.img']) if rec['src'].startswith('Character') else             wz.read_image(imgs[f'{key[:4]}.img'])[key]
        wz.close()
        ref = node['info']['icon']
        link = ref.get('_outlink')
        if link:  # the picture is stored in the category's _Canvas file: follow "<dir>/_Canvas/<img>/<path>"
            parts = link.split('/')
            i = parts.index('_Canvas')
            wz = WzFile(str(next((DATA.joinpath(*parts[:i + 1])).glob('*_000.wz'))))
            ref = wz.read_image(dict(wz.images())[parts[i + 1]])
            for part in parts[i + 2:]:
                ref = ref[part]
        else:
            wz = WzFile(str(next(folder.glob('*_000.wz'))))
        out = canvas.decode(wz, ref['_data'])
        wz.close()
    except (KeyError, StopIteration, OSError, ValueError) as ex:
        print(f'[item] no icon for {item_id}: {ex!r}')
    _icons[item_id] = out
    return out


ICONS_NPZ = pathlib.Path(__file__).with_name('item_icons.npz')
SLOT = 32  # inventory slot size in UI pixels


def build_icons(items):
    """item_icons.npz: every indexed item's icon as it appears in an inventory slot (32x32 RGBA, UI
    pixels). The game draws an icon with its origin at the slot's bottom-left corner."""
    import canvas
    import numpy as np
    ids, pics = [], []
    by_src = {}
    for item_id, rec in items.items():
        by_src.setdefault(rec['src'], []).append(item_id)
    for src, members in by_src.items():
        folder = DATA / src
        wz = WzFile(str(next(folder.glob('*_000.wz'))))
        imgs = dict(wz.images())
        canvases, cv_imgs, cache, cv_cache = None, {}, {}, {}  # data images / their _Canvas images
        for item_id in members:
            key = f'{item_id:08d}'
            try:
                if src.startswith('Character'):
                    node = wz.read_image(imgs[f'{key}.img'])
                else:
                    group = f'{key[:4]}.img'
                    if group not in cache:
                        cache = {group: wz.read_image(imgs[group])}
                    node = cache[group][key]
                ref = node['info']['icon']
                if '_uol' in ref and not src.startswith('Character'):
                    # a link to another item's icon in the same image, e.g. "../../02040000/info/icon"
                    path = [key, 'info']  # a link is relative to the node holding it
                    for part in ref['_uol'].split('/'):
                        path = path[:-1] if part == '..' else path + [part]
                    ref = cache[group]
                    for part in path:
                        ref = ref[part]
                origin = ref.get('origin') or {}
                link = ref.get('_outlink')
                source = wz
                if link:  # stored in the folder's _Canvas file: "<dir>/_Canvas/<img>/<path>"
                    parts = link.split('/')
                    i = parts.index('_Canvas')
                    if canvases is None:
                        canvases = WzFile(str(next(DATA.joinpath(*parts[:i + 1]).glob('*_000.wz'))))
                        cv_imgs = dict(canvases.images())
                    if parts[i + 1] not in cv_cache:
                        cv_cache = {parts[i + 1]: canvases.read_image(cv_imgs[parts[i + 1]])}
                    ref = cv_cache[parts[i + 1]]
                    for part in parts[i + 2:]:
                        ref = ref[part]
                    source = canvases
                pic = canvas.decode(source, ref['_data'])
            except (KeyError, StopIteration, OSError, ValueError, TypeError, struct.error):
                continue  # no usable icon: that item just can't be recognised in the inventory
            if pic is None:
                continue
            slot = np.zeros((SLOT, SLOT, 4), np.uint8)
            x0, y0 = -int(origin.get('x', 0)), SLOT - int(origin.get('y', SLOT))
            h, w = pic.shape[:2]
            sx0, sy0 = max(0, x0), max(0, y0)
            sx1, sy1 = min(SLOT, x0 + w), min(SLOT, y0 + h)
            if sx1 > sx0 and sy1 > sy0:
                slot[sy0:sy1, sx0:sx1] = pic[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0]
                ids.append(item_id)
                pics.append(slot)
        wz.close()
        if canvases:
            canvases.close()
    np.savez_compressed(ICONS_NPZ, ids=np.array(ids), icons=np.stack(pics))
    return len(ids)


def load_icons():
    """(item ids, icons as an (n, 32, 32, 4) array) from item_icons.npz."""
    import numpy as np
    data = np.load(ICONS_NPZ)
    return data['ids'], data['icons']


def norm(name):
    return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', name.lower()).split())


def load():
    """{item id: record}, plus by_name: {normalised name: [item ids]}."""
    items = {int(k): v for k, v in json.loads(ITEMS_JSON.read_text(encoding='utf-8')).items()}
    by_name = {}
    for item_id, rec in items.items():
        by_name.setdefault(norm(rec['name']), []).append(item_id)
    return items, by_name


def sell_text(rec):
    """What a vendor (NPC shop) pays, for display ("Sell to vendor for 80 mesos each")."""
    if rec.get('no_sale'):
        return "Can't be sold to vendors"
    if rec.get('unit'):  # throwing stars / bullets: the price is for a full set
        return f'Sell to vendor for up to {rec["price"]:,} mesos (full set)'
    if not rec['price']:
        return 'Vendors pay nothing for this'
    return f'Sell to vendor for {rec["price"]:,} meso{"s" * (rec["price"] != 1)} each'


if __name__ == '__main__' and sys.argv[1:2] == ['find']:
    items, by_name = load()
    term = norm(' '.join(sys.argv[2:]))
    for item_id, rec in items.items():
        if term in norm(rec['name']):
            print(f'{item_id}  {rec["name"]}: {sell_text(rec)}')
