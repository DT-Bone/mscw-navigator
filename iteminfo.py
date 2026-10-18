"""Item info panel: everything known about an item, opened by right-clicking it (see itemhover).

From the game files: shop price (and what a full stack is worth), stack size, requirements and whether
you can use it, stats (and how they compare with what you're wearing), scrolls that fit it, what
potions and scrolls do, flags, quests (including what your active quests still need), and monsters
that probably drop it with the best place to hunt them and a button to navigate there.
"""
import re
import tkinter as tk
import webbrowser
from urllib.parse import quote_plus

from items import sell_text
from quests import label as quest_label

ATTACK_SPEEDS = {2: 'Faster', 3: 'Faster', 4: 'Fast', 5: 'Fast', 6: 'Normal', 7: 'Slow', 8: 'Slow', 9: 'Slower'}
EFFECTS = {'pad': 'Weapon Att', 'mad': 'Magic Att', 'pdd': 'Weapon Def', 'mdd': 'Magic Def', 'acc': 'Accuracy',
           'eva': 'Avoidability', 'speed': 'Speed', 'jump': 'Jump', 'crt': 'Critical Rate', 'crd': 'Critical Damage'}
MAX_QUESTS = 4          # quests listed per kind of link
MAX_SCROLL_KINDS = 8    # scroll lines listed for a piece of equipment
MAX_RECIPES = 4         # recipes that make an item (a table each)
MAX_USES = 8            # rows in the "Used to make" table
MEOWDB_SEARCH = 'https://www.google.com/search?q='  # MeowDB has no item search URL of its own
TIERS = ('Lesser', 'Intermediate', 'Greater', 'Chaos')
GOOD, BAD = '#7ee08a', '#ff8a80'  # green / red text


def _slot(item_id):
    """Equipment slot group: all weapons share one slot (the game compares any weapon with yours)."""
    group = item_id // 10000
    return 'weapon' if 130 <= group < 170 else group


def _scroll_target(scroll_id):
    """Equipment group a scroll is for: 20400xx -> hats (100xxxx), 20445xx -> bows (145xxxx), ..."""
    return 100 + (scroll_id // 100) % 100


def best_map(mob, dist):
    """(map id, spawn count, steps) of the best place to hunt a monster: many spawns, not too far."""
    spots = [(m, c[0]) for m, c in mob['maps'].items() if c[0] > 0]
    reachable = [(m, n) for m, n in spots if dist.get(m) is not None]
    if reachable:
        m, n = max(reachable, key=lambda s: s[1] / (1 + dist[s[0]] / 4))
        return m, n, dist[m]
    if spots:
        m, n = max(spots, key=lambda s: s[1])
        return m, n, None
    return None


def details(item_id, rec, ctx):
    """[(heading, text, action)] describing an item; heading '' continues the previous section and
    action is None or ('go', map id). ctx: items, qdata, mobs, map_names, dist (map id -> steps from
    here), active (Quest Helper entries), level, job ((name, class) or None), equipped (item ids or None)."""
    rows = []
    add = lambda heading, text, action=None: rows.append((heading, text, action))
    add('Vendor', sell_text(rec))
    qty = ctx.get('qty')
    if qty and qty > 1 and rec['price'] and not rec.get('no_sale') and not rec.get('unit'):
        add('', f'this stack of {qty:,} sells to vendor for {rec["price"] * qty:,} mesos')
    if rec.get('max', 1) > 1:
        add('Stack', f'stacks up to {rec["max"]:,}')
        if rec['price'] and not rec.get('no_sale') and not rec.get('unit'):
            add('', f'a full stack sells to vendor for {rec["price"] * rec["max"]:,} mesos')
    _market(item_id, ctx, add)
    _requirements(rec, ctx, add)
    _stats(item_id, rec, ctx, add)
    if rec['cat'] == 'Equip':
        extra = [f'{rec.get("slots", 0)} upgrade slot{"s" * (rec.get("slots", 0) != 1)}']
        if rec.get('speed'):
            extra.append(f'attack speed {ATTACK_SPEEDS.get(rec["speed"], rec["speed"])}')
        add('Equip', ',  '.join(extra))
        _scrolls(item_id, ctx, add)
    if rec.get('scroll'):
        s = rec['scroll']
        add('Scroll', f'{s["success"]}% success' + (f',  {s["destroy"]}% chance to destroy the item on failure'
                                                    if s.get('destroy') else ''))
    _effects(rec, ctx, add)
    if rec.get('flags'):
        add('Notes', ',  '.join(rec['flags']))
    _sources(item_id, rec, ctx, add)
    _crafting(item_id, ctx, add)
    _quests(item_id, ctx, add)
    # Real drop tables and NPC shop stock aren't in the game files (the server has them): look them up.
    add('Drops / shops', 'not in the game files; look them up on MeowDB',
        ('open', MEOWDB_SEARCH + quote_plus(f'"{rec["name"]}" site:meowdb.com msclassic')))
    return rows


def quest_links(qdata):
    """{item: quest ids} for quests that reward an item and quests that hand it over when accepted."""
    rewards, given = {}, {}
    for qid, q in qdata['quests'].items():
        for i, _ in q['reward']['items']:
            rewards.setdefault(i, []).append(qid)
        for i in q.get('accept_items', []):
            given.setdefault(i, []).append(qid)
    return rewards, given


def how_to_get(item_id, rec, qdata, craft, links):
    """Short notes on where an item comes from, for lists: ["drops from Snail (guess)", "crafted: ..."]."""
    out = []
    mobs = list(dict.fromkeys(rec.get('sources') or qdata['item_sources'].get(item_id) or []))
    if mobs:
        out.append('drops from ' + ' / '.join(qdata['mob_names'].get(m, str(m)) for m in mobs[:2]) + ' (guess)')
    if craft:
        disciplines, makes = craft[0], craft[1]
        recipes = makes.get(item_id) or []
        if recipes:
            r = min(recipes, key=lambda r: r['level'])
            out.append(f'crafted: {disciplines.get(r["skill"], {}).get("name", "crafting")} Lv {r["level"]}')
    rewards, given = links
    if item_id in rewards:
        n = len(rewards[item_id])
        out.append(f'quest reward ({n} quest{"s" * (n != 1)})')
    if item_id in given:
        out.append('given by a quest')
    return out


def _requirements(rec, ctx, add):
    req = rec.get('req') or {}
    if not req:
        return
    add('Requires', '  ·  '.join(f'Lv {v}' if k == 'Level' else v if k == 'Job' else f'{k} {v}'
                                       for k, v in req.items()))
    problems, level, job = [], ctx.get('level'), ctx.get('job')
    if req.get('Level') and level and level < req['Level']:
        problems.append(f'needs Lv {req["Level"]} (you are {level})')
    if req.get('Job') and job and job[1] not in req['Job'].split(' / '):
        problems.append(f'not for {job[1]}s')
    if problems:
        add('', '✗ ' + ',  '.join(problems))
    elif level or job:
        stats = [k for k in ('STR', 'DEX', 'INT', 'LUK') if req.get(k)]
        add('', '✓ your level and job are fine' + (f' (check your {" / ".join(stats)})' if stats else ''))


def _stats(item_id, rec, ctx, add):
    stats = rec.get('stats') or {}
    worn = None
    if rec['cat'] == 'Equip':
        worn = next((i for ids in (ctx.get('equipped') or []) for i in ids
                     if i in ctx['items'] and ctx['items'][i]['cat'] == 'Equip' and _slot(i) == _slot(item_id)), None)
    if worn is not None and worn != item_id:
        # Side by side with what you're wearing in that slot: one line per stat either item has.
        other = ctx['items'][worn].get('stats') or {}
        rows = []
        for k in dict.fromkeys([*stats, *other]):
            this, theirs = stats.get(k, 0), other.get(k, 0)
            change = this - theirs
            rows.append([(k, None), (f'{this:,}' if this else '\u2013', None),
                         (f'{theirs:,}' if theirs else '\u2013', None),
                         (f'{change:+,}' if change else 'same', 'good' if change > 0 else 'bad' if change < 0 else 'dim')])
        add('Stats', '', ('table', {'caption': f'compared with your {ctx["items"][worn]["name"]} (base stats, '
                                               'before scrolls)', 'header': ['', 'This', 'Worn', 'Change'],
                                    'rows': rows}))
        return
    for i, (name, value) in enumerate(stats.items()):
        # a scroll's stats are what it adds to the item it's used on
        add(('Adds' if rec.get('scroll') else 'Stats') if i == 0 else '', f'{name} {value:+,}')


def _scrolls(item_id, ctx, add):
    """Scrolls that fit this equipment, as a table: one row per kind, one column per tier."""
    target = item_id // 10000
    kinds = {}  # kind -> {tier: scroll record}
    for sid, sc in ctx['items'].items():
        if sc.get('scroll') and 2040000 <= sid < 2050000 and _scroll_target(sid) == target:
            base, _, tier = sc['name'].rpartition(': ')
            base = re.sub(r'\s*Scroll$', '', base or sc['name'])
            kinds.setdefault(base, {})[tier.strip()] = sc
    if not kinds:
        return
    # "Hat Accuracy", "Hat HP" -> "Accuracy", "HP": the item type is the same for every row
    words = [k.split() for k in kinds]
    common = 0
    while all(len(w) > common + 1 for w in words) and len({w[common] for w in words}) == 1:
        common += 1
    tiers = [t for t in TIERS if any(t in v for v in kinds.values())]
    header, sub = [''], ['']
    note = None
    for t in tiers:
        rates = {(v[t]['scroll']['success'], v[t]['scroll'].get('destroy', 0)) for v in kinds.values() if t in v}
        header.append(t)
        success, destroy = next(iter(rates)) if len(rates) == 1 else (None, None)
        sub.append(f'{success}%' + ('*' if destroy else '') if success is not None else 'varies')
        if destroy:
            note = f'* {t}: if it fails, {destroy}% chance the item is destroyed'
    rows = []
    for kind, by_tier in list(kinds.items())[:MAX_SCROLL_KINDS]:
        row = [(' '.join(kind.split()[common:]), None)]
        for t in tiers:
            sc = by_tier.get(t)
            row.append(((', '.join(f'{v:+}' for v in (sc.get('stats') or {}).values()) or 'random') if sc else '\u2013',
                        None))
        rows.append(row)
    caption = f'{len(kinds)} kinds; only the first {MAX_SCROLL_KINDS} shown' if len(kinds) > MAX_SCROLL_KINDS else None
    add('Scrolls', '', ('table', {'caption': caption, 'header': header, 'sub': sub, 'rows': rows, 'note': note}))


def _effects(rec, ctx, add):
    effect = rec.get('effect') or {}
    lines = []
    if effect.get('hp'):
        lines.append(f'Restores {effect["hp"]:,} HP')
    if effect.get('mp'):
        lines.append(f'Restores {effect["mp"]:,} MP')
    if effect.get('hpR'):
        lines.append(f'Restores {effect["hpR"]}% of HP')
    if effect.get('mpR'):
        lines.append(f'Restores {effect["mpR"]}% of MP')
    buffs = [f'{label} +{effect[k]}' for k, label in EFFECTS.items() if effect.get(k)]
    if buffs:
        secs = effect.get('time', 0) / 1000
        lines.append(', '.join(buffs) + (f' for {secs:g} s' if secs else ''))
    if effect.get('moveTo'):
        dest = 'the nearest town' if effect['moveTo'] == 999999999 else ctx['map_names'].get(effect['moveTo'], 'another map')
        lines.append(f'Takes you to {dest}')
    if effect.get('exp'):
        lines.append(f'Gives {effect["exp"]:,} EXP')
    for i, line in enumerate(lines):
        add('Use' if i == 0 else '', line)


def _sources(item_id, rec, ctx, add):
    """Monsters that probably drop it (guessed from its name), with where to hunt them. Each monster's
    Spots button lists all its maps in the Search panel, to pick one."""
    mobs = list(dict.fromkeys(rec.get('sources') or ctx['qdata']['item_sources'].get(item_id) or []))
    import gathering
    where = gathering.spots(ctx['graph'], item_id) if not mobs and ctx.get('graph') else {}
    if where:  # from breaking things on maps (wooden boxes, coconuts, ...)
        src = gathering.SOURCES[item_id]
        map_id = gathering.best_map(where, ctx['dist'], 15)
        steps = ctx['dist'].get(map_id)
        here = 'you are here' if map_id == ctx.get('current') else \
            f'{steps} step{"s" * (steps != 1)} away' if steps is not None else 'no route from here'
        add('Comes from', src.how[0].upper() + src.how[1:], ('go', map_id))
        add('', f'best spot: {str(ctx["map_names"].get(map_id, map_id)).strip()} ({len(where[map_id])} '
                f'{src.plural}, {here})')
    first = True
    for mob_id in mobs[:3]:
        mob = ctx['mobs'].get(mob_id)
        if not mob:
            continue
        text = f'{mob["name"]} (Lv {mob["level"]}, {mob["hp"]:,} HP)'
        add('Drops from' if first else '', text + ('  — guessed from the name' if first else ''),
            ('spots', mob_id) if mob.get('maps') else None)
        first = False
        spot = best_map(mob, ctx['dist'])
        if spot:
            map_id, count, steps = spot
            where = 'you are here' if map_id == ctx.get('current') else \
                f'{steps} step{"s" * (steps != 1)} away' if steps is not None else 'no route from here'
            add('', f'best spot: {ctx["map_names"].get(map_id, map_id)} ({count} spawns, {where})')


MAX_SHOPS = 4  # player shops listed per item


def _market(item_id, ctx, add):
    """Free Market price and the player shops seen listing it, from MesoWatch (see mesowatch.py), as two tables.
    ctx['market']: {'summary', 'item', 'state', 'link', 'places'} (places: map name -> map id, for Go)."""
    m = ctx.get('market')
    if not m:
        return
    from mesowatch import ago
    s, doc = m['summary'], m['item']
    if s is None:
        add('Free Market', {'loading': 'looking it up on meso.watch\u2026',
                            'offline': "couldn't reach meso.watch"}.get(m['state'], 'no Free Market data on meso.watch'))
        return
    per = ' /set' if s.get('unitBasis') == 'set' else ''
    mesos = lambda v: f'{int(round(v)):,}'  # their figures can be fractional (e.g. quartiles)
    if s.get('price'):
        spread = (f'{mesos(s["p25"])}\u2013{mesos(s["p75"])}'
                  if s.get('p25') and s.get('p75') and round(s['p25']) != round(s['p75']) else '\u2014')
        basis = (f'{s["priceSales"]:,} sales, 24 h' if s.get('priceBasis') == '24h' and s.get('priceSales') else
                 'last 10 sales' if s.get('priceBasis') == 'last10' else '\u2014')
        trend = s.get('trend')
        arrow = '\u25b2' if (trend or 0) > 0 else '\u25bc'
        trend = '\u2014' if trend is None else 'steady' if abs(trend) < 0.05 else f'{arrow} {abs(trend):.0%}'
        add('Free Market', '', ('table', {
            'header': ['Typical', 'Most sales', 'Based on', 'Trend', 'Last sold'],
            'rows': [[(mesos(s['price']) + per, 'strong'), (spread, None), (basis, 'dim'), (trend, None),
                      (ago(s['lastSold']) if s.get('lastSold') else '\u2014', 'dim')]],
            'note': 'Few sales so far, so these are rough.' if s.get('lowData') else None}))
    else:
        add('Free Market', 'no sales seen yet' + (f'; shops ask about {mesos(s["askMedian"])} mesos{per}'
                                                   if s.get('askMedian') else ''))
    # where to buy it: shops seen listing it; still up first, then ones whose location is known (so Go can take
    # you there), then the most recently seen
    listings = sorted((doc or {}).get('listings') or [],
                      key=lambda l: (bool(l.get('up')), bool(l.get('location')), str(l.get('lastSeen') or '')), reverse=True)
    if doc is None:
        if m['state'] == 'loading':
            add('Shops', 'looking up shops\u2026')
    elif not listings:
        add('Shops', 'none seen listing it lately')
    else:
        shown = listings[:MAX_SHOPS]
        short = lambda where: where.replace('Free Market <', 'FM <') if where else '\u2014'
        add('Shops', '', ('table', {
            'header': ['Seller', 'Where', 'Price', 'Left', 'Seen'], 'align': ['w', 'w', 'e', 'e', 'e'],
            'rows': [[(l.get('seller', '?'), None), (short(l.get('location')), None),
                      (mesos(l.get('price') or 0) + per, 'strong'), (f'{l["stock"]:,}' if l.get('stock') else '\u2014', 'dim'),
                      (ago(l['lastSeen']) if l.get('lastSeen') else '\u2014', 'dim')] for l in shown],
            'row_go': [(m['places'][l['location']],) if l.get('location') in m['places'] else None for l in shown]}))
    add('', 'From meso.watch: read from public streams, so it can be hours old', ('open', m['link'], 'Open'))


def _crafting(item_id, ctx, add):
    """Recipes that make the item (with the master who teaches the craft) and recipes that use it."""
    craft = ctx.get('craft')
    if not craft:
        return
    disciplines, makes, uses = craft
    name = lambda i: ctx['items'].get(i, {}).get('name', f'item {i}')
    for k, r in enumerate(makes.get(item_id, [])[:MAX_RECIPES]):
        d = disciplines.get(r['skill'], {})
        caption = f'{d.get("name", "Crafting")} Lv {r["level"]}'
        go = None
        npc = ctx.get('npcs', {}).get(d.get('npc'))
        if npc and npc.get('maps'):
            map_id, (x, y) = next(iter(npc['maps'].items()))
            caption += f'  \u00b7  taught by {npc["name"]} ({ctx["map_names"].get(map_id, map_id)})'
            go = (map_id, (x, y, npc['name']))
        # what goes in, the fee, and what comes out. The catalyst is optional: crafting with it is a gamble on
        # better results (its own description: "When used while crafting ..., there is a chance ...").
        rows = [[(name(i), None), (f'{c:,}', None)] for i, c in r['ingredients']]
        if r['additive']:
            i, c = r['additive']
            rows.append([(f'{name(i)} (optional)', 'dim'), (f'{c:,}', 'dim')])
        if r['meso']:
            rows.append([('Fee', None), (f'{r["meso"]:,} mesos', None)])
        rows.append([(f'Makes: {name(r["result"][0])}', 'good'), (f'{r["result"][1]:,}', 'good')])
        add('Made by' if k == 0 else '', '', ('table', {
            'caption': caption, 'title': True, 'rows': rows, 'header': ['', 'Qty'], 'go': go}))
    used = sorted(uses.get(item_id, []), key=lambda r: (r['level'], name(r['result'][0])))
    if not used:
        return
    catalyst = lambda r: all(i != item_id for i, _ in r['ingredients'])  # used as the recipe's (optional) catalyst
    only_catalyst = all(catalyst(r) for r in used)
    rows = []
    for r in used[:MAX_USES]:
        d = disciplines.get(r['skill'], {})
        if catalyst(r):
            need = f'{(r["additive"] or [0, 1])[1]:,}' + ('' if only_catalyst else ' (optional catalyst)')
        else:
            need = f'{next(c for i, c in r["ingredients"] if i == item_id):,}'
        result, count = r['result']
        rows.append([(name(result) + (f' \u00d7{count}' if count > 1 else ''), None), (need, None),
                     (f'{d.get("name", "Craft")} {r["level"]}', 'dim')])
    note = 'Uses: how many one craft takes' + (", as an optional catalyst" if only_catalyst else '')
    if len(used) > MAX_USES:
        note += f'  \u00b7  {len(used) - MAX_USES} more recipes not shown'
    add('Used to make', '', ('table', {'header': ['Makes', 'Uses', 'Craft Lv'], 'rows': rows, 'note': note}))


def _quests(item_id, ctx, add):
    qdata = ctx['qdata']
    active = []
    for e in ctx.get('active') or []:
        have, need = e['progress'].get(('item', item_id), (None, None))
        if need:
            more = need - have if have is not None else None
            active.append((e['qid'], f'{quest_label(qdata, e["qid"])}: '
                           + (f'{have} / {need}' + (f'  ({more} more)' if more and more > 0 else '  (done)')
                              if have is not None else f'needs {need}')))
    for i, (_, text) in enumerate(active):
        add('Your quests' if i == 0 else '', text)
    skip = {qid for qid, _ in active}
    need, given, reward = [], [], []
    for qid, q in qdata['quests'].items():
        if qid in skip:
            continue
        if any(i == item_id for i, _ in q['items']):
            need.append(qid)
        if item_id in q.get('accept_items', []):
            given.append(qid)
        if any(i == item_id for i, _ in q['reward']['items']):
            reward.append(qid)
    level = lambda qid: qdata['quests'][qid]['lvmin'] or 0
    for heading, ids in (('Quests need it', need), ('Given by quest', given), ('Quest reward', reward)):
        ids = sorted(ids, key=level)
        for i, qid in enumerate(ids[:MAX_QUESTS]):
            more = f'  (+{len(ids) - MAX_QUESTS} more)' if i == MAX_QUESTS - 1 and len(ids) > MAX_QUESTS else ''
            add(heading if i == 0 else '', quest_label(qdata, qid) + more)


class ItemPanel:
    """A small window with an item's details; closed with its x button (or a second right-click)."""

    def __init__(self, root, k, item_id, rec, rows, prepare_window, on_close, on_navigate, icon=None, same_name=1,
                 on_spots=None):
        """on_navigate(map_id, npc=None): a Go button; on_spots(mob_id): a monster's Spots button."""
        from picker import ACCENT, BG, DIM, FG, PANEL
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.config(bg=ACCENT)
        body = tk.Frame(self.win, bg=BG, padx=int(14 * k), pady=int(10 * k))
        body.pack(padx=2, pady=2)
        wrap = int(380 * k)
        head = tk.Frame(body, bg=BG)
        head.pack(fill='x')
        tk.Label(head, text='ITEM INFO', font=('Segoe UI', 8, 'bold'), bg=BG, fg=ACCENT).pack(side='left')
        close = tk.Label(head, text='✕', font=('Segoe UI', 10, 'bold'), bg=BG, fg=DIM, cursor='hand2')
        close.pack(side='right')
        close.bind('<Button-1>', lambda e: on_close())
        title = tk.Frame(body, bg=BG)
        title.pack(fill='x', pady=(int(4 * k), 0))
        self.photo = None
        if icon is not None:
            from PIL import Image, ImageTk
            scale = max(2, round(2 * k))
            img = Image.fromarray(icon).resize((icon.shape[1] * scale, icon.shape[0] * scale), Image.NEAREST)
            self.photo = ImageTk.PhotoImage(img, master=self.win)
            tk.Label(title, image=self.photo, bg=BG).pack(side='left', padx=(0, int(10 * k)))
        names = tk.Frame(title, bg=BG)
        names.pack(side='left', fill='x')
        tk.Label(names, text=rec['name'], font=('Segoe UI', 13, 'bold'), bg=BG, fg=FG, anchor='w', justify='left',
                 wraplength=wrap - int(80 * k)).pack(fill='x')
        kind = rec['cat'] + (f'  ·  {rec["type"]}' if rec.get('type') else '') + f'  ·  item {item_id}'
        if same_name > 1:
            kind += f'  ·  {same_name} items share this name'
        tk.Label(names, text=kind, font=('Segoe UI', 9), bg=BG, fg=DIM, anchor='w').pack(fill='x')
        if rec.get('desc'):
            tk.Label(body, text=rec['desc'], font=('Segoe UI', 9), bg=BG, fg=DIM, anchor='w', justify='left',
                     wraplength=wrap).pack(fill='x', pady=(int(6 * k), 0))
        grid = tk.Frame(body, bg=BG)
        grid.pack(fill='x', pady=(int(8 * k), 0))
        for r, (heading, text, action) in enumerate(rows):
            tk.Label(grid, text=heading, font=('Segoe UI', 9, 'bold'), bg=BG, fg=ACCENT, anchor='nw').grid(
                row=r, column=0, sticky='nw', padx=(0, int(10 * k)))
            if action and action[0] == 'table':
                spec = action[1]
                self._table(grid, r, spec, k, wrap, (BG, DIM, FG), span=1 if spec.get('go') else 2,
                            on_navigate=on_navigate)
                if spec.get('go'):
                    go = tk.Label(grid, text='Go', font=('Segoe UI', 9, 'bold'), bg=ACCENT, fg=BG, padx=int(8 * k),
                                  cursor='hand2')
                    go.grid(row=r, column=2, sticky='ne', padx=(int(8 * k), 0), pady=(int(2 * k), 0))
                    go.bind('<Button-1>', lambda e, g=spec['go']: on_navigate(*g))
                continue
            colour = GOOD if text.startswith('✓') else BAD if text.startswith('✗') else FG
            tk.Label(grid, text=text, font=('Segoe UI', 9), bg=BG, fg=colour, anchor='w', justify='left',
                     wraplength=wrap - int(130 * k)).grid(row=r, column=1, sticky='w')
            if action and (action[0] in ('go', 'open') or action[0] == 'spots' and on_spots):
                text = action[2] if action[0] == 'open' and len(action) > 2 else \
                    {'go': 'Go', 'open': 'Search', 'spots': 'Spots'}[action[0]]
                go = tk.Label(grid, text=text,
                              font=('Segoe UI', 9, 'bold'), bg=ACCENT, fg=BG, padx=int(8 * k), cursor='hand2')
                go.grid(row=r, column=2, sticky='e', padx=(int(8 * k), 0))
                if action[0] == 'go':  # navigate to a map (and an NPC on it)
                    go.bind('<Button-1>', lambda e, a=action: on_navigate(a[1], *a[2:]))
                elif action[0] == 'spots':  # list where a monster spawns, to pick a map
                    go.bind('<Button-1>', lambda e, mob_id=action[1]: on_spots(mob_id))
                else:  # open a web page in the browser
                    go.bind('<Button-1>', lambda e, url=action[1]: webbrowser.open(url))
        self.win.update_idletasks()
        self.hwnd = prepare_window(self.win)

    @staticmethod
    def _table(grid, row, spec, k, wrap, colours, span=2, on_navigate=None):
        """A small table: optional caption, column titles (and a second title row), rows of cells
        (text, colour: None / 'strong' / 'good' / 'bad' / 'dim'; all but None and dim are bold), optional
        footnote. The first column is left-aligned and the rest right-aligned, unless spec['align'] gives
        'w'/'e' per column. spec['row_go']: per row, arguments for on_navigate (a Go button) or None."""
        BG, DIM, FG = colours
        tint = {'good': GOOD, 'bad': BAD, 'dim': DIM, 'strong': FG}
        box = tk.Frame(grid, bg=BG)
        box.grid(row=row, column=1, columnspan=span, sticky='w', pady=(0, int(4 * k)))
        n, r = len(spec['header']), 0
        align = spec.get('align') or ['w'] + ['e'] * (n - 1)
        side = lambda c: (align[c], align[c]) if c < len(align) else ('e', 'e')
        row_go = spec.get('row_go') or []
        pad = (0, int(14 * k))
        if spec.get('caption'):  # a title (e.g. a recipe's craft and teacher) is white, a plain caption grey
            tk.Label(box, text=spec['caption'], font=('Segoe UI', 9), bg=BG, fg=FG if spec.get('title') else DIM,
                     anchor='w', justify='left',
                     wraplength=wrap - int(130 * k)).grid(row=r, column=0, columnspan=n, sticky='w')
            r += 1
        for titles, size in ((spec['header'], 8), (spec.get('sub'), 8)):
            if not titles:
                continue
            for c, title in enumerate(titles):
                tk.Label(box, text=title, font=('Segoe UI', size, 'bold'), bg=BG, fg=DIM, anchor=side(c)[0]).grid(
                    row=r, column=c, sticky=side(c)[1], padx=pad)
            r += 1
        for i, cells in enumerate(spec['rows']):
            for c, (text, colour) in enumerate(cells):
                tk.Label(box, text=text, font=('Segoe UI', 9, 'bold' if colour in ('good', 'bad', 'strong') else 'normal'),
                         bg=BG, fg=tint.get(colour, FG), anchor=side(c)[0]).grid(row=r, column=c, sticky=side(c)[1],
                                                                                   padx=pad)
            if i < len(row_go) and row_go[i] and on_navigate:  # a Go button at the end of this row
                from picker import ACCENT
                go = tk.Label(box, text='Go', font=('Segoe UI', 8, 'bold'), bg=ACCENT, fg=BG, padx=int(6 * k),
                              cursor='hand2')
                go.grid(row=r, column=n, sticky='w', pady=int(1 * k))
                go.bind('<Button-1>', lambda e, g=row_go[i]: on_navigate(*g))
            r += 1
        if spec.get('note'):
            tk.Label(box, text=spec['note'], font=('Segoe UI', 8), bg=BG, fg=DIM, anchor='w', justify='left',
                     wraplength=wrap - int(130 * k)).grid(row=r, column=0, columnspan=n, sticky='w')
            r += 1
        if spec.get('verdict'):
            text, colour = spec['verdict']
            tk.Label(box, text=text, font=('Segoe UI', 9, 'bold'), bg=BG, fg=tint.get(colour, FG), anchor='w',
                     justify='left', wraplength=wrap - int(130 * k)).grid(row=r, column=0, columnspan=n, sticky='w')

    def place(self, x, y, game_rect):
        """Top-left at (x, y) in the game window, kept inside it."""
        gx, gy, gw, gh = game_rect
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        x, y = min(max(0, x), gw - w), min(max(0, y), gh - h)
        self.win.geometry(f'+{int(gx + x)}+{int(gy + y)}')

    def size(self):
        return self.win.winfo_reqwidth(), self.win.winfo_reqheight()

    def show(self):
        if self.win.state() == 'withdrawn':
            self.win.deiconify()

    def hide(self):
        if self.win.state() != 'withdrawn':
            self.win.withdraw()

    def raise_window(self):
        if self.win.state() != 'withdrawn':
            self.win.attributes('-topmost', True)
            self.win.lift()

    def destroy(self):
        self.win.destroy()
