"""Crafting recipes from the game files (Etc/CraftRecipe.img): six disciplines, each taught by a master
NPC through an apprentice quest ("Silas Irons in Need of an Apprentice" teaches Blacksmithing).

    py -3.11 crafting.py find <item name>
(The index is rebuilt by `py -3.11 navgraph.py build`.)
"""
import json
import pathlib
import re
import sys

from wzreader import WzFile
import gamepath

DATA = gamepath.DATA
CRAFTING_JSON = pathlib.Path(__file__).with_name('crafting.json')
# "A Blacksmith in My Own Right!" names the profession; the craft is named after it
CRAFT_NAMES = {'Blacksmith': 'Blacksmithing', 'Weaponcrafter': 'Weaponcrafting', 'Tailor': 'Tailoring',
               'Carpenter': 'Carpentry', 'Leatherworker': 'Leatherworking', 'Arcforger': 'Arcforging'}


def _disciplines():
    """{skill id: {'name', 'npc'}} from the quests that teach each crafting skill."""
    out = {}
    for path in sorted((DATA / 'Quest').rglob('QuestData_*.wz')):
        wz = WzFile(str(path))
        for _, entry in wz.images():
            q = wz.read_image(entry)
            for act in (q.get('Act') or {}).values():
                for sk in ((act.get('skill') or {}).values() if isinstance(act, dict) else []):
                    skill = sk.get('id') if isinstance(sk, dict) else None
                    if not (isinstance(skill, int) and 92000000 <= skill < 93000000):
                        continue
                    d = out.setdefault(skill, {'name': None, 'npc': None})
                    title = (q.get('QuestInfo') or {}).get('name', '')
                    m = re.match(r'An? (\w+) in My Own Right', title)
                    if m:
                        d['name'] = CRAFT_NAMES.get(m.group(1), m.group(1))
                    if 'Apprentice' in title or 'Partner' in title:
                        d['npc'] = ((q.get('Check') or {}).get('0') or {}).get('npc')
        wz.close()
    return out


def build():
    wz = WzFile(str(DATA / 'Etc' / 'Etc_000.wz'))
    recipes_wz = wz.read_image(dict(wz.images())['CraftRecipe.img'])
    wz.close()
    disciplines = _disciplines()
    recipes = []
    for levels in recipes_wz.values():
        for level, group in levels.items():
            for r in group.values():
                if not isinstance(r, dict) or 'Results' not in r:
                    continue
                skill = ((r.get('Conditions') or {}).get('SkillCheck') or {})
                ingredients = [[v['item'], v.get('count', 1)] for v in (r.get('Ingredients') or {}).values()
                               if isinstance(v, dict) and 'item' in v]
                res = r['Results']
                recipes.append({'skill': skill.get('SkillID'), 'level': skill.get('reqSkillLevel', int(level)),
                                'meso': r.get('Meso', 0), 'time': r.get('ProcessTimeMS', 0) / 1000,
                                'ingredients': ingredients,
                                'additive': [r['AdditiveItemID'], r.get('AdditiveCount', 1)] if r.get('AdditiveItemID') else None,
                                'result': [res['item'], res.get('count', 1)], 'exp': res.get('exp', 0)})
    data = {'disciplines': {str(k): v for k, v in disciplines.items()}, 'recipes': recipes}
    CRAFTING_JSON.write_text(json.dumps(data), encoding='utf-8')
    return data


def load():
    """(disciplines {skill id: {'name', 'npc'}}, recipes, makes {item: [recipes]}, uses {item: [recipes]})."""
    data = json.loads(CRAFTING_JSON.read_text(encoding='utf-8'))
    disciplines = {int(k): v for k, v in data['disciplines'].items()}
    makes, uses = {}, {}
    for r in data['recipes']:
        makes.setdefault(r['result'][0], []).append(r)
        for item, _ in r['ingredients'] + ([r['additive']] if r['additive'] else []):
            uses.setdefault(item, []).append(r)
    return disciplines, data['recipes'], makes, uses


if __name__ == '__main__' and sys.argv[1:2] == ['find']:
    import items
    it, by_name = items.load()
    disciplines, _, makes, uses = load()
    for item_id in by_name.get(items.norm(' '.join(sys.argv[2:])), []):
        name = lambda i: it.get(i, {}).get('name', i)
        for r in makes.get(item_id, []):
            d = disciplines.get(r['skill'], {})
            print(f'made by {d.get("name")} Lv {r["level"]}: ' + ' + '.join(f'{c} {name(i)}' for i, c in r['ingredients'])
                  + f' + {r["meso"]:,} mesos')
        for r in uses.get(item_id, []):
            print(f'used for {r["result"][1]} {name(r["result"][0])} ({disciplines.get(r["skill"], {}).get("name")})')
