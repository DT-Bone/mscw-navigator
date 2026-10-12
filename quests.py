"""Quest index from the game files: who starts/ends each quest, what it needs, rewards and text.

    py -3.11 quests.py find <quest or NPC name>
(The index is rebuilt by `py -3.11 navgraph.py build`.)

Drop tables are server-side, so a required item's source monsters are guessed from the item's
name and description ("A cap removed from a Blue Mushroom") and may be missing.
"""
import json
import pathlib
import re
import sys

from wzreader import WzFile
import gamepath

DATA = gamepath.DATA
QUESTS_JSON = pathlib.Path(__file__).with_name('quests.json')

# Text codes: #p<npc>#, #m<map>#, #o<mob>#, #t<item>#/#z<item>#, #i<item>#(icon), #c/#a (live counts),
# colour/format switches (#b #k #r #e #n ...), #h# (player name), #L0# list items, literal \n.
CODE = re.compile(r'#([pmotzica])(\d+):?#|#L\d+#|#l|#h0?#|#[bkrdegnsfx]|\\r|\\n')


def fmt(text, names):
    if not isinstance(text, str):
        return ''

    def rep(m):
        kind, num = m.group(1), m.group(2)
        if kind:
            table = {'p': names['npc'], 'm': names['map'], 'o': names['mob'], 't': names['item'], 'z': names['item']}
            return table[kind].get(int(num), '?') if kind in table else ''
        tok = m.group(0)
        if tok == '\\n':
            return '\n'
        if tok.startswith('#h'):
            return 'you'
        return ''
    out = CODE.sub(rep, text).replace('#', '')
    return re.sub(r'[ \t]+', ' ', re.sub(r'\n\s*\n+', '\n', out)).strip()


OPTION = re.compile(r'#L\s*(\d+)\s*#(.*?)(?=#L\s*\d+\s*#|#l|$)', re.S)  # "#L2 #" occurs too


def _quiz(say, names):
    """Questions with a right answer in a quest's dialogue (Say/<phase>), e.g. Icarus's quiz in "I'm Bored":
    {phase: [[question, choice number, answer text]]}. Say/<phase>/stop/<question>/answer is the 1-based
    choice that's accepted; the other stop entries are the NPC's replies to wrong choices."""
    out = {}
    for phase, conv in (say or {}).items():
        if not isinstance(conv, dict):
            continue
        for key, stop in sorted((conv.get('stop') or {}).items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            raw = conv.get(key)
            if not (key.isdigit() and isinstance(stop, dict) and isinstance(raw, str) and stop.get('answer')):
                continue
            options = {int(n): fmt(text, names) for n, text in OPTION.findall(raw)}
            answer = options.get(int(stop['answer']) - 1)
            if answer is None:
                continue
            question = fmt(raw[:raw.find('#L')], names).replace('\n', ' ')
            # long speeches end with the actual question; keep its last sentence
            sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+(?=[A-Z"])', question) if s.strip()]
            asks = [s for s in sentences if s.endswith('?')]
            question = (asks or sentences or [''])[-1]
            out.setdefault(phase, []).append([question, int(stop['answer']), re.sub(r'^\d+\.\s*', '', answer)])
    return out


def _reqs(node):
    return [[int(v['id']), int(v.get('count', 1))] for v in (node or {}).values()
            if isinstance(v, dict) and str(v.get('id', '')).lstrip('-').isdigit()]


def _item_strings(s, strings):
    items = {}

    def walk(tree):
        for k, v in tree.items():
            if isinstance(v, dict):
                if k.isdigit() and 'name' in v:
                    items[int(k)] = (v.get('name', ''), v.get('desc', ''))
                else:
                    walk(v)
    for img in ('Etc.img', 'Consume.img', 'Ins.img', 'Cash.img', 'Pet.img', 'Eqp.img'):
        walk(s.read_image(strings[img]))
    return items


def build(graph):
    from maps import load_maps
    s = WzFile(str(DATA / 'String' / 'String_000.wz'))
    strings = dict(s.images())
    item_strings = _item_strings(s, strings)
    names = {
        'npc': {int(k): v.get('name', '') for k, v in s.read_image(strings['Npc.img']).items()
                if k.isdigit() and isinstance(v, dict)},
        'mob': {int(k): v.get('name', '') for k, v in s.read_image(strings['Mob.img']).items()
                if k.isdigit() and isinstance(v, dict)},
        'map': {m['id']: m['name'] for m in load_maps()},
        'item': {i: n for i, (n, _) in item_strings.items()},
    }
    s.close()
    # Monsters that actually spawn somewhere, longest names first (so "Dark Stump" beats "Stump").
    spawning = sorted({mob for m in graph.values() for mob in m.get('mobs', {})},
                      key=lambda i: -len(names['mob'].get(i, '')))

    def sources(item_id):
        name, desc = item_strings.get(item_id, ('', ''))
        text, found = f'{name} {desc}'.lower(), []
        for mob in spawning:
            mn = names['mob'].get(mob, '').lower()
            if mn and re.search(r'\b' + re.escape(mn) + r's?\b', text) and not any(mn in names['mob'][f].lower() for f in found):
                found.append(mob)
        return found

    quests = {}
    for path in sorted((DATA / 'Quest').rglob('QuestData_*.wz')):
        wz = WzFile(str(path))
        for img_name, entry in wz.images():
            stem = img_name.rsplit('/', 1)[-1].removesuffix('.img')
            if not stem.isdigit():
                continue
            q = wz.read_image(entry)
            info, check, act = q.get('QuestInfo') or {}, q.get('Check') or {}, q.get('Act') or {}
            start, end, reward = check.get('0') or {}, check.get('1') or {}, act.get('1') or {}
            on_accept = act.get('0') or {}
            reward_items = [[i, c] for i, c in _reqs(reward.get('item')) if c > 0]
            random_reward = any(isinstance(v, dict) and v.get('prop') for v in (reward.get('item') or {}).values())
            quests[int(stem)] = {
                'name': info.get('name') or f'Quest {stem}',
                'lvmin': start.get('lvmin'), 'lvmax': start.get('lvmax'),
                'start_npc': start.get('npc'), 'end_npc': end.get('npc'),
                'prereq': [int(v['id']) for v in (start.get('quest') or {}).values()
                           if isinstance(v, dict) and v.get('state') == 2],
                'jobs': [v for v in (start.get('job') or {}).values() if isinstance(v, int)],
                'mobs': [[i, c] for i, c in _reqs(end.get('mob')) if c > 0],
                'items': [[i, c] for i, c in _reqs(end.get('item')) if c > 0],
                'reward': {'exp': reward.get('exp'), 'money': reward.get('money'),
                           'items': reward_items, 'random': random_reward},
                'text': {'start': fmt(info.get('0'), names), 'progress': fmt(info.get('1'), names),
                         'done': fmt(info.get('2'), names)},
                'next': [],
                # items the NPC hands over when you accept (Act/0), e.g. a letter to deliver
                'accept_items': [i for i, c in _reqs(on_accept.get('item')) if c > 0],
                # quiz answers: phase '0' is asked when accepting, '1' when completing
                'quiz': _quiz(q.get('Say'), names),
            }
        wz.close()
    for qid, q in quests.items():
        for p in q['prereq']:
            if p in quests:
                quests[p]['next'].append(qid)
    # Required items that another quest gives you (on accept or as a reward): say which quest.
    givers = {}
    for qid, q in quests.items():
        for i in q['accept_items']:
            givers.setdefault(i, []).append(qid)
        for i, _ in q['reward']['items']:
            givers.setdefault(i, []).append(qid)
    for qid, q in quests.items():
        q['item_from_quest'] = {}
        for i, _ in q['items']:
            others = [g for g in givers.get(i, []) if g != qid]
            if i not in q['accept_items'] and others:
                # prefer a prerequisite quest, else the lowest-level giver
                pre = [g for g in others if g in q['prereq']]
                q['item_from_quest'][i] = (pre or sorted(others, key=lambda g: quests[g]['lvmin'] or 0))[0]
    # Items whose name doesn't say where they drop ("Omok Table") are often explained in the quest text
    # ("defeating Stump, Slime, and Octopus"): the monsters it names that aren't already the source of
    # another required item are the likely source.
    for q in quests.values():
        text = ' '.join(q['text'].values()).lower()
        named = []
        for mob in spawning:
            mn = names['mob'].get(mob, '').lower()
            if mn and re.search(r'\b' + re.escape(mn) + r's?\b', text) and not any(mn in names['mob'][f].lower() for f in named):
                named.append(mob)
        explained = {m for i, _ in q['items'] for m in sources(i)} | {m for m, _ in q['mobs']}
        q['text_sources'] = {}
        for i, _ in q['items']:
            if not sources(i) and i not in q['accept_items'] and i not in q['item_from_quest']:
                left = [m for m in named if m not in explained]
                if left:
                    q['text_sources'][i] = left
    used_items = {i for q in quests.values() for i, _ in q['items'] + q['reward']['items']}
    data = {'quests': quests,
            'items': {i: names['item'].get(i, f'Item {i}') for i in used_items},
            'item_sources': {i: sources(i) for i in {i for q in quests.values() for i, _ in q['items']}},
            'mob_names': names['mob'], 'npc_names': names['npc']}
    QUESTS_JSON.write_text(json.dumps(data), encoding='utf-8')
    return data


def load():
    data = json.loads(QUESTS_JSON.read_text(encoding='utf-8'))
    ints = lambda d: {int(k): v for k, v in d.items()}
    out = {'quests': ints(data['quests']), 'items': ints(data['items']),
           'item_sources': ints(data['item_sources']), 'mob_names': ints(data['mob_names']),
           'npc_names': ints(data['npc_names'])}
    names = [q['name'] for q in out['quests'].values()]
    out['shared_names'] = {n for n in names if names.count(n) > 1}
    return out


def label(qdata, qid):
    """Quest name, plus what it asks for when other quests share the name
    ("Donating to Henesys (30 Orange Mushroom Cap)")."""
    q = qdata['quests'][qid]
    if q['name'] not in qdata.get('shared_names', ()):
        return q['name']
    wants = [f'{c} {qdata["items"].get(i, i)}' for i, c in q['items']] + \
            [f'{c} {qdata["mob_names"].get(i, i)}' for i, c in q['mobs']]
    return f'{q["name"]} ({", ".join(wants)})' if wants else f'{q["name"]} (Lv {q["lvmin"] or 1})'


if __name__ == '__main__' and sys.argv[1:2] == ['find']:
    data = load()
    term = ' '.join(sys.argv[2:]).lower()
    for qid, q in data['quests'].items():
        if term in q['name'].lower() or term in data['npc_names'].get(q['start_npc'], '').lower():
            print(f'[{qid}] {q["name"]} (Lv {q["lvmin"]}) from {data["npc_names"].get(q["start_npc"])}'
                  f' -> {data["npc_names"].get(q["end_npc"])}')
            for i, c in q['mobs']:
                print(f'    kill {c} {data["mob_names"].get(i, i)}')
            for i, c in q['items']:
                src = ', '.join(data['mob_names'].get(m, str(m)) for m in data['item_sources'].get(i, [])) or 'unknown source'
                print(f'    get {c} {data["items"].get(i, i)} ({src})')
            print(f'    reward: {q["reward"]["exp"]} EXP, {q["reward"]["money"]} mesos; next: {q["next"]}')
            print(f'    text: {q["text"]["progress"][:200]!r}')
