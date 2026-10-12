"""In-game destination picker: a small "Navigate" button plus a searchable map list."""
import difflib
import json
import pathlib
import re
import tkinter as tk
import tkinter.font

from mobs import search as mobsearch
from npcs import search as npcsearch
from quests import label as quest_label
from navgraph import distances

PREFS = pathlib.Path(__file__).with_name('nav_prefs.json')
BG, PANEL, FG, DIM, ACCENT, SEL = '#1d2433', '#262f42', '#ffffff', '#8b95a8', '#f2b134', '#3a4763'
MAX_RECENT = 8
STAR, NO_STAR = '\u2605', '\u2606'  # favourite / not (filled and hollow stars)
AND_SPLIT = re.compile(r'\s*(?:,|&|\+|\band\b)\s*', re.I)  # "stump and snail", "stump, snail", ...


def _norm(s):
    return ' '.join(s.lower().replace(':', ' ').replace('-', ' ').split())


def search(query, maps):
    """Maps matching `query`, best first. Substring and word-prefix matches beat fuzzy ones."""
    q = _norm(query)
    if not q:
        return []
    terms = q.split()
    scored = []
    for m in maps:
        name, full = _norm(m['name']), _norm(f'{m["street"]} {m["name"]}')
        words = full.split()
        if q in name:
            score = 3 + name.startswith(q)
        elif q in full:
            score = 2.5
        elif all(any(w.startswith(t) for w in words) for t in terms):
            score = 2
        else:
            score = difflib.SequenceMatcher(None, q, name).ratio()
            if score < 0.6:
                continue
        scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored[:40]]


def load_prefs():
    try:
        return json.loads(PREFS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_prefs(**changes):
    try:
        PREFS.write_text(json.dumps({**load_prefs(), **changes}), encoding='utf-8')
    except OSError:
        pass


def load_recent():
    return load_prefs().get('recent', [])


def load_favorites():
    return load_prefs().get('favorite_maps', [])


def save_recent(map_id):
    save_prefs(recent=([map_id] + [r for r in load_recent() if r != map_id])[:MAX_RECENT])


MODES = (('maps', 'Maps'), ('monsters', 'Monsters'), ('npcs', 'NPCs'), ('quests', 'Quests'), ('items', 'Items'))


def _next_mode(mode):
    keys = [k for k, _ in MODES]
    return keys[(keys.index(mode) + 1) % len(keys)]


class Picker:
    """Owns the Navigate button and the search panel. Calls on_pick(map_id or None)."""

    def __init__(self, root, maps, graph, mobs, npcs, qdata, travel_data, k, on_pick, on_close, on_options,
                 on_quest, get_level, prepare_window, on_stream=None, on_stats=None, on_item=None, on_quit=None):
        self.root, self.graph, self.mobs, self.npcs, self.travel, self.k = root, graph, mobs, npcs, travel_data, k
        self.qdata, self.on_quest, self.get_level = qdata, on_quest, get_level
        self.on_quit = on_quit  # the Quit button: close the whole navigator
        self.get_active_quests = lambda: []  # set by the overlay: entries from the game's Quest Helper box
        self.on_item = on_item  # open an item's info panel
        self._item_data = None  # (items, by name, crafting, quest links), loaded when the Items tab is first used
        self.maps = [m for m in maps if m['id'] in graph and graph[m['id']].get('portals') is not None]
        self.by_id = {m['id']: m for m in self.maps}
        self.on_pick, self.on_close, self.on_options, self.prepare = on_pick, on_close, on_options, prepare_window
        self.opts = {'scrolls': False, 'taxi': False, **load_prefs().get('opts', {})}
        self.mob_sort = load_prefs().get('mob_sort', 'count')
        self.arrow_fixed = bool(load_prefs().get('arrow_fixed', False))  # compass under the status box
        self.show_on_stream = bool(load_prefs().get('show_on_stream', False))  # visible to Discord/OBS capture
        self.on_stream = on_stream
        self.show_stats = bool(load_prefs().get('show_stats', False))  # EXP/meso tracker panel
        self.on_stats = on_stats
        self.show_hidden = bool(load_prefs().get('show_hidden', True))  # mark hidden street entrances
        self.show_teleports = bool(load_prefs().get('show_teleports', True))  # mark same-map teleporters
        self.show_item_prices = bool(load_prefs().get('show_item_prices', True))  # right-click item info panel, inventory tab value
        self.mode = 'maps'
        self.mob_only = None  # (query, [mob ids]): what find_monster searched; that query lists only them
        self.current = None
        self.panel = None
        self.panel_hwnd = None
        self.game_rect = None
        self.rows = []

        # Navigate button
        self.button = tk.Toplevel(root)
        self.button.overrideredirect(True)
        self.button.attributes('-topmost', True)
        self.button.config(bg=ACCENT)
        lbl = tk.Label(self.button, text='⌖  Navigate', font=('Segoe UI', 11, 'bold'),
                       bg=BG, fg=FG, padx=int(14 * k), pady=int(5 * k), cursor='hand2')
        lbl.pack(padx=2, pady=2)
        lbl.bind('<Button-1>', lambda e: self.open())
        lbl.bind('<Enter>', lambda e: lbl.config(bg=SEL))
        lbl.bind('<Leave>', lambda e: lbl.config(bg=BG))
        self.button.withdraw()
        self.button_hwnd = prepare_window(self.button)

    # -- button ----------------------------------------------------------------
    def show_button(self, gx, gy, gw, gh):
        if self.game_rect != (gx, gy, gw, gh):
            self.game_rect = (gx, gy, gw, gh)
            self.button.update_idletasks()
            bw = self.button.winfo_reqwidth()
            self.button.geometry(f'+{int(gx + gw / 2 - bw / 2)}+{int(gy + gh * 0.045)}')
        if self.button.state() == 'withdrawn':
            self.button.deiconify()
            self.button.lift()

    def hide_button(self):
        if self.button.state() != 'withdrawn' and self.panel is None:
            self.button.withdraw()

    def raise_windows(self):
        """Keep our windows above the (also topmost) overlay."""
        for w in (self.button, self.panel):
            if w is not None and w.state() != 'withdrawn':
                w.attributes('-topmost', True)
                w.lift()

    def hwnds(self):
        return {h for h in (self.button_hwnd, self.panel_hwnd) if h}

    # -- panel -------------------------------------------------------------------
    def open(self):
        if self.panel is not None:
            self.entry.focus_force()
            return
        k = self.k
        p = self.panel = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.attributes('-topmost', True)
        p.config(bg=ACCENT)
        body = tk.Frame(p, bg=BG, padx=int(16 * k), pady=int(14 * k))
        body.pack(padx=2, pady=2, fill='both', expand=True)

        here = self.by_id.get(self.current)
        tk.Label(body, text='Navigate to', font=('Segoe UI', 14, 'bold'), bg=BG, fg=FG, anchor='w').pack(fill='x')
        tk.Label(body, text=f'You are in: {here["street"]}: {here["name"]}' if here else 'Current map not detected yet',
                 font=('Segoe UI', 10), bg=BG, fg=DIM, anchor='w').pack(fill='x', pady=(0, int(8 * k)))

        tabs = tk.Frame(body, bg=BG)
        tabs.pack(fill='x', pady=(0, int(6 * k)))
        self.tabs = {}
        for mode, text in MODES:
            t = tk.Label(tabs, text=text, font=('Segoe UI', 11, 'bold'), padx=int(12 * k), pady=int(3 * k), cursor='hand2')
            t.pack(side='left', padx=(0, int(6 * k)))
            t.bind('<Button-1>', lambda e, m=mode: self.set_mode(m))
            self.tabs[mode] = t
        tk.Label(tabs, text='Tab to switch', font=('Segoe UI', 9), bg=BG, fg=DIM).pack(side='left', padx=int(6 * k))
        # Monster sort order (shown only on the Monsters tab)
        self.sort_frame = tk.Frame(tabs, bg=BG)
        self.sort_labels = {}
        for key, text in (('closest', 'Closest'), ('share', '% of map'), ('count', 'Spawns')):
            s = tk.Label(self.sort_frame, text=text, font=('Segoe UI', 9, 'bold'), padx=int(8 * k), pady=int(2 * k),
                         cursor='hand2')
            s.pack(side='right', padx=(int(4 * k), 0))
            s.bind('<Button-1>', lambda e, key=key: self.set_sort(key))
            self.sort_labels[key] = s
        tk.Label(self.sort_frame, text='Sort by:', font=('Segoe UI', 9), bg=BG, fg=DIM).pack(side='right')

        checks = tk.Frame(body, bg=BG)
        checks.pack(fill='x', pady=(0, int(6 * k)))
        tk.Label(checks, text='Routes may use:', font=('Segoe UI', 10), bg=BG, fg=DIM).pack(side='left')
        self.opt_vars = {}
        for key, text in (('scrolls', 'Return scrolls'), ('taxi', 'Taxi (Regular Cab)')):
            var = self.opt_vars[key] = tk.BooleanVar(value=self.opts.get(key, False))
            tk.Checkbutton(checks, text=text, variable=var, command=self.options_changed, font=('Segoe UI', 10),
                           bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                           highlightthickness=0, cursor='hand2').pack(side='left', padx=(int(8 * k), 0))

        display = tk.Frame(body, bg=BG)
        display.pack(fill='x', pady=(0, int(6 * k)))
        tk.Label(display, text='Display:', font=('Segoe UI', 10), bg=BG, fg=DIM).pack(side='left')
        self.fixed_var = tk.BooleanVar(value=self.arrow_fixed)
        self.stream_var = tk.BooleanVar(value=self.show_on_stream)
        self.stats_var = tk.BooleanVar(value=self.show_stats)
        self.hidden_var = tk.BooleanVar(value=self.show_hidden)
        self.teleport_var = tk.BooleanVar(value=self.show_teleports)
        self.price_var = tk.BooleanVar(value=self.show_item_prices)
        markers = tk.Frame(body, bg=BG)
        markers.pack(fill='x', pady=(0, int(6 * k)))
        tk.Label(markers, text='Markers:', font=('Segoe UI', 10), bg=BG, fg=DIM).pack(side='left')
        for row, text, var, cmd in ((display, 'Fixed arrow (top of screen)', self.fixed_var, self.arrow_changed),
                                    (display, 'Show on stream / recordings', self.stream_var, self.stream_changed),
                                    (display, 'EXP/meso tracker', self.stats_var, self.stats_changed),
                                    (markers, 'Hidden streets', self.hidden_var, self.hidden_changed),
                                    (markers, 'Teleporters', self.teleport_var, self.teleports_changed),
                                    (markers, 'Item info (right-click) & inventory value', self.price_var, self.prices_changed)):
            tk.Checkbutton(row, text=text, variable=var, command=cmd, font=('Segoe UI', 10), bg=BG, fg=FG,
                           selectcolor=PANEL, activebackground=BG, activeforeground=FG, highlightthickness=0,
                           cursor='hand2').pack(side='left', padx=(int(8 * k), 0))

        self.query = tk.StringVar()
        self.entry = tk.Entry(body, textvariable=self.query, font=('Segoe UI', 13), bg=PANEL, fg=FG,
                              insertbackground=FG, relief='flat', highlightthickness=2,
                              highlightcolor=ACCENT, highlightbackground=SEL)
        self.entry.pack(fill='x', ipady=int(5 * k))
        self.hint = tk.Label(body, font=('Segoe UI', 9), bg=BG, fg=DIM, anchor='w')
        self.hint.pack(fill='x', pady=(int(4 * k), int(6 * k)))

        self.list = tk.Listbox(body, font=('Segoe UI', 11), bg=PANEL, fg=FG, selectbackground=SEL,
                               selectforeground=FG, activestyle='none', relief='flat', highlightthickness=0,
                               height=14, width=80)
        self.list.pack(fill='both', expand=True)

        btns = tk.Frame(body, bg=BG)
        btns.pack(fill='x', pady=(int(10 * k), 0))
        for text, cmd, primary in (('Go', self.go, True), ('Stop navigating', self.stop, False), ('Close', self.close, False)):
            b = tk.Label(btns, text=text, font=('Segoe UI', 11, 'bold'), bg=ACCENT if primary else PANEL,
                         fg=BG if primary else FG, padx=int(14 * k), pady=int(4 * k), cursor='hand2')
            b.pack(side='left', padx=(0, int(8 * k)))
            b.bind('<Button-1>', lambda e, c=cmd: c())
        if self.on_quit:  # there's no console window to close when it runs from the desktop shortcut
            q = tk.Label(btns, text='Quit', font=('Segoe UI', 11, 'bold'), bg=PANEL, fg=DIM,
                         padx=int(14 * k), pady=int(4 * k), cursor='hand2')
            q.pack(side='right')
            q.bind('<Button-1>', lambda e: (self.close(), self.on_quit()))
            q.bind('<Enter>', lambda e: q.config(fg=FG))
            q.bind('<Leave>', lambda e: q.config(fg=DIM))
        # favourite toggle for the selected map (Maps tab); fixed width so its text change doesn't shift it
        self.fav_button = tk.Label(btns, font=('Segoe UI', 11, 'bold'), bg=PANEL, fg=ACCENT, width=12,
                                   pady=int(4 * k), cursor='hand2')
        self.fav_button.bind('<Button-1>', lambda e: self.toggle_favorite())

        self.update_distances()
        self.query.trace_add('write', lambda *_: self.refresh())
        self.entry.bind('<Return>', lambda e: self.go())
        self.entry.bind('<Escape>', lambda e: self.close())
        self.entry.bind('<Tab>', lambda e: (self.set_mode(_next_mode(self.mode)), 'break')[1])
        self.entry.bind('<Down>', lambda e: self.move(1))
        self.entry.bind('<Up>', lambda e: self.move(-1))
        self.list.bind('<Double-Button-1>', lambda e: self.go())
        self.list.bind('<Button-1>', self._star_click)
        self.list.bind('<<ListboxSelect>>', lambda e: self._update_fav_button())
        for w in (self.entry, self.list):
            w.bind('<Control-d>', lambda e: (self.toggle_favorite(), 'break')[1])
        self.list.bind('<Return>', lambda e: self.go())
        self.list.bind('<Escape>', lambda e: self.close())
        self.set_mode(self.mode)

        p.update_idletasks()
        self.panel_hwnd = self.prepare(p)
        if self.game_rect:
            gx, gy, gw, gh = self.game_rect
            p.geometry(f'+{int(gx + gw / 2 - p.winfo_reqwidth() / 2)}+{int(gy + gh * 0.18)}')
        p.lift()
        p.focus_force()
        self.entry.focus_force()

    def update_distances(self):
        self.dist = (distances(self.graph, self.current, self.opts, self.travel)
                     if self.current in self.graph else {})

    def stats_changed(self):
        self.show_stats = self.stats_var.get()
        save_prefs(show_stats=self.show_stats)
        if self.on_stats:
            self.on_stats(self.show_stats)
        self.entry.focus_set()

    def stream_changed(self):
        self.show_on_stream = self.stream_var.get()
        save_prefs(show_on_stream=self.show_on_stream)
        if self.on_stream:
            self.on_stream(self.show_on_stream)
        self.entry.focus_set()

    def hidden_changed(self):
        self.show_hidden = self.hidden_var.get()
        save_prefs(show_hidden=self.show_hidden)
        self.entry.focus_set()

    def prices_changed(self):
        self.show_item_prices = self.price_var.get()
        save_prefs(show_item_prices=self.show_item_prices)
        self.entry.focus_set()

    def teleports_changed(self):
        self.show_teleports = self.teleport_var.get()
        save_prefs(show_teleports=self.show_teleports)
        self.entry.focus_set()

    def arrow_changed(self):
        self.arrow_fixed = self.fixed_var.get()
        save_prefs(arrow_fixed=self.arrow_fixed)
        self.entry.focus_set()

    def options_changed(self):
        self.opts = {k: v.get() for k, v in self.opt_vars.items()}
        save_prefs(opts=self.opts)
        self.on_options(self.opts)
        self.update_distances()
        self.refresh()
        self.entry.focus_set()

    def set_sort(self, key):
        self.mob_sort = key
        save_prefs(mob_sort=key)
        for k_, s in self.sort_labels.items():
            s.config(bg=ACCENT if k_ == key else PANEL, fg=BG if k_ == key else FG)
        self.refresh()
        self.entry.focus_set()

    def set_mode(self, mode):
        self.mode = mode
        for m, t in self.tabs.items():
            t.config(bg=ACCENT if m == mode else PANEL, fg=BG if m == mode else FG)
        if mode == 'monsters':
            self.sort_frame.pack(side='right')
            for k_, s in self.sort_labels.items():
                s.config(bg=ACCENT if k_ == self.mob_sort else PANEL, fg=BG if k_ == self.mob_sort else FG)
        else:
            self.sort_frame.pack_forget()
        if mode == 'maps':
            self.fav_button.pack(side='right')
        else:
            self.fav_button.pack_forget()
        self.hint.config(text={
            'maps': 'Type a map or street name. Enter to go, Esc to close. Click a star (or Ctrl+D) to favorite a map.',
            'monsters': 'Monster name, or several joined by "and" / "," for maps with all of them. Empty = near you.',
            'npcs': 'Type an NPC name or role (e.g. "storage"). Empty shows NPCs near you.',
            'quests': self._quest_hint(),
            'items': 'Type an item name. Empty shows items your active quests still need. Enter opens its info.'}[mode])
        self.refresh()
        self.entry.focus_set()

    def _hops(self, map_id):
        if map_id == self.current:
            return 'you are here'
        d = self.dist.get(map_id)
        if d is None:
            return 'no route' if any(self.opts.values()) else 'no walking route'
        return f'{d} step{"s" * (d != 1)}'

    def _map(self, map_id):
        return self.by_id.get(map_id) or {'id': map_id, 'name': f'Map {map_id}', 'street': ''}

    def _monster_rows(self, q):
        """[(text, map, reachable)] for monsters matching q, or near the player when q is empty.

        Typed searches keep each monster's maps together and sort within it; the "near you"
        list is sorted as a whole. Sort order is self.mob_sort.
        """
        def record(mob_id, map_id):
            count, respawn = self.mobs[mob_id]['maps'][map_id]
            total = sum(c for c, _ in self.graph[map_id]['mobs'].values()) or 1
            return mob_id, map_id, count, count / total, respawn

        key = {'count': lambda r: (-r[2], self.dist.get(r[1], 999)),
               'share': lambda r: (-r[3], -r[2]),
               'closest': lambda r: (self.dist.get(r[1], 999), -r[2])}[self.mob_sort]
        only = self.mob_only[1] if self.mob_only and q.strip() == self.mob_only[0] else None
        terms = [t for t in AND_SPLIT.split(q.strip()) if t]
        if len(terms) > 1 and not only:
            return self._all_of_rows(terms, key)
        if q.strip():
            records = [r for mob_id in (only or mobsearch(q, self.mobs)[:8])
                       for r in sorted((record(mob_id, i) for i in self.mobs[mob_id]['maps']), key=key)]
        else:
            records = sorted((record(mob_id, map_id) for map_id, d in self.dist.items() if d <= 3
                              for mob_id in self.graph[map_id].get('mobs', {})), key=key)
        rows = []
        for mob_id, map_id, count, frac, respawn in records[:80]:
            mob, m = self.mobs[mob_id], self._map(map_id)
            share = f'{100 * frac:.0f}% of map'
            timer = f', every {respawn // 60} min' if count == 1 and respawn >= 60 else ''
            lvl = f' Lv {mob["level"]}' if mob.get('level') else ''
            text = (f' {mob["name"]}{lvl}  ·  {m["name"]}  ·  {count} spawn{"s" * (count != 1)} '
                    f'({share}{timer})  ·  {self._hops(map_id)}')
            rows.append((text, m, map_id in self.dist))
        return rows

    def find_monster(self, mob_id):
        """Open the Monsters tab listing just this monster's maps (an item's "Drops from" Spots button).
        Typing anything else searches as usual."""
        name = self.mobs[mob_id]['name']
        self.open()
        self.mob_only = (name, [mob_id])
        self.set_mode('monsters')
        self.query.set(name)
        self.entry.icursor('end')
        self.panel.lift()
        self.entry.focus_force()

    def _term_mobs(self, term):
        """Monsters a search word refers to: the exact name if there is one ("snail" = Snail),
        else every name containing it ("mushroom"), else the closest fuzzy matches.
        Plurals like "stumps" are tried in singular form too."""
        t = _norm(term)
        forms = [t] + [t[:-n] for n in (1, 2) if len(t) > n + 2 and t.endswith('es'[2 - n:])]
        for match in (lambda f, name: name == f, lambda f, name: f in name):
            for f in forms:
                ids = [i for i, m in self.mobs.items() if match(f, _norm(m['name']))]
                if ids:
                    return ids
        return mobsearch(term, self.mobs)[:3]

    def _all_of_rows(self, terms, key):
        """Maps that have every monster in `terms` (an AND search)."""
        groups = []
        for term in terms:
            ids = self._term_mobs(term)
            if not ids:
                return [(f' No monster matches "{term}"', None, False)]
            groups.append(ids)
        common = set.intersection(*({m for i in ids for m in self.mobs[i]['maps']} for ids in groups))
        records = []
        for map_id in common:
            spawns = self.graph[map_id]['mobs']
            total = sum(c for c, _ in spawns.values()) or 1
            parts = [(' / '.join(self.mobs[i]['name'] for i in ids if i in spawns),
                      sum(spawns[i][0] for i in ids if i in spawns)) for ids in groups]
            combined = sum(c for _, c in parts)
            records.append((parts, map_id, combined, combined / total))
        rows = []
        for parts, map_id, combined, frac in sorted(records, key=key)[:80]:
            m = self._map(map_id)
            text = (f' {" + ".join(f"{names} {c}" for names, c in parts)}  ·  {m["name"]}  ·  '
                    f'{combined} spawns ({100 * frac:.0f}% of map)  ·  {self._hops(map_id)}')
            rows.append((text, m, map_id in self.dist))
        if not rows:
            names = ' and '.join(' / '.join(self.mobs[i]['name'] for i in ids) for ids in groups)
            rows.append((f' No map has all of: {names}', None, False))
        return rows

    def refresh(self):
        q = self.query.get()
        if self.mode == 'monsters':
            self._fill(self._monster_rows(q))
            return
        if self.mode == 'npcs':
            self._fill(self._npc_rows(q))
            return
        if self.mode == 'quests':
            self._fill(self._quest_rows(q))
            return
        if self.mode == 'items':
            self._fill(self._item_rows(q))
            return
        favs = set(load_favorites())
        if q.strip():
            items = [('', m) for m in search(q, self.maps)]
        else:
            favorites = sorted((self.by_id[f] for f in favs if f in self.by_id),
                               key=lambda m: self.dist.get(m['id'], 999))
            recent = [self.by_id[r] for r in load_recent() if r in self.by_id and r != self.current and r not in favs]
            # Main towns only: shops and side rooms are flagged as towns too, but return elsewhere.
            towns = sorted((m for m in self.maps if self.graph[m['id']].get('town')
                            and self.graph[m['id']].get('return') == m['id'] and m['id'] != self.current
                            and m['id'] not in favs), key=lambda m: self.dist.get(m['id'], 999))
            items = ([('', m) for m in favorites] + [('Recent', m) for m in recent]
                     + [('Town', m) for m in towns if m not in recent])
        self._fill([(f' {STAR if m["id"] in favs else NO_STAR} {f"[{tag}] " if tag else ""}{m["name"]}  ·  '
                     f'{m["street"]}  ·  {self._hops(m["id"])}', m, m['id'] in self.dist) for tag, m in items])

    def _npc_rows(self, q):
        """[(text, map, reachable, (x, y, name))] for NPCs matching q, or near the player when q is empty."""
        if q.strip():
            nearest = lambda npc_id: min(self.dist.get(i, 999) for i in self.npcs[npc_id]['maps'])
            pairs = [(npc_id, map_id) for npc_id in npcsearch(q, self.npcs, nearest)[:25]
                     for map_id in sorted(self.npcs[npc_id]['maps'], key=lambda i: self.dist.get(i, 999))]
        else:
            pairs = sorted(((npc_id, map_id) for map_id, d in self.dist.items() if d <= 1
                            for npc_id in {n for n, _, _ in self.graph[map_id].get('npcs', [])} if npc_id in self.npcs),
                           key=lambda p: (self.dist.get(p[1], 999), -len(self.npcs[p[0]]['starts'])))
        rows = []
        for npc_id, map_id in pairs[:80]:
            n, m = self.npcs[npc_id], self._map(map_id)
            role = f' ({n["func"]})' if n['func'] else ''
            levels = [q['lvmin'] for q in n['starts'] if q.get('lvmin')]
            quests = (f'{len(n["starts"])} quest{"s" * (len(n["starts"]) != 1)}'
                      + (f' from Lv {min(levels)}' if levels else '')) if n['starts'] else 'no quests'
            x, y = n['maps'][map_id]
            rows.append((f' {n["name"]}{role}  ·  {m["name"]}  ·  {quests}  ·  {self._hops(map_id)}',
                         m, map_id in self.dist, (x, y, n['name'])))
        return rows

    def _items(self):
        if self._item_data is None:
            import items
            from iteminfo import quest_links
            it, by_name = items.load()
            try:
                import crafting
                disciplines, _, makes, _ = crafting.load()
                craft = (disciplines, makes)
            except OSError:
                craft = None
            self._item_data = (it, by_name, craft, quest_links(self.qdata))
        return self._item_data

    def _item_rows(self, q):
        """[(text, None, True, {'item': id})] for items matching q, or ones your active quests still need."""
        import items
        from iteminfo import how_to_get
        it, by_name, craft, links = self._items()
        term = items.norm(q)
        if term:
            words = term.split()
            scored = []
            for name, ids in by_name.items():
                if name == term:
                    score = 4
                elif name.startswith(term):
                    score = 3
                elif term in name:
                    score = 2.5
                elif all(any(w.startswith(t) for w in name.split()) for t in words):
                    score = 2
                else:
                    score = difflib.SequenceMatcher(None, term, name).ratio()
                    if score < 0.78:
                        continue
                scored.append((-score, len(name), name))
            names = [n for *_, n in sorted(scored)[:60]]
            heading, hints, exact = None, {}, {}
        else:  # what the quests in the game's Quest Helper box still need
            needs, hints, exact = [], {}, {}
            for e in self.get_active_quests():
                q = self.qdata['quests'].get(e['qid'], {})
                text_sources = {int(k): v for k, v in (q.get('text_sources') or {}).items()}
                for (kind, i), (have, need) in e['progress'].items():
                    if kind == 'item' and i in it and (have is None or have < need):
                        needs.append(items.norm(it[i]['name']))
                        exact[items.norm(it[i]['name'])] = i  # the very item the quest wants
                        if i in text_sources:  # the quest's text says which monsters to hunt
                            mobs = ' / '.join(self.qdata['mob_names'].get(m, str(m)) for m in text_sources[i][:2])
                            hints[items.norm(it[i]['name'])] = f'probably from {mobs} (quest text)'
            names = list(dict.fromkeys(needs))
            heading = ' Items your active quests still need:' if names else ' Type an item name to search all items.'
        rows = [(heading, None, False)] if heading else []
        for name in names:
            ids = by_name[name]
            item_id = exact.get(name, ids[0])
            rec = it[item_id]
            kind = rec['cat'] + (f' \u00b7 {rec["type"]}' if rec.get('type') else '')
            price = ('no vendor' if rec.get('no_sale') else f'vendor {rec["price"]:,}' if rec['price'] else 'vendor 0')
            how = how_to_get(item_id, rec, self.qdata, craft, links)
            if not term and name in hints:
                how.append(hints[name])
            text = f' {rec["name"]}  \u00b7  {kind}  \u00b7  {price}  \u00b7  {"; ".join(how) if how else "source unknown"}'
            if len(ids) > 1:
                text += f'  ({len(ids)} items share this name)'
            rows.append((text, None, True, {'item': item_id}))
        if not rows:
            rows.append((' No items found', None, False))
        return rows

    def _quest_hint(self):
        level = self.get_level()
        lv = f'your level: {level}' if level else 'level not detected yet'
        return f'Quest or NPC name. Empty shows quests you can start nearby ({lv}). Pick one to start the helper.'

    def _quest_rows(self, q):
        """[(text, start map, available, {'quest': id})] for quests matching q, or startable near you."""
        quests, npc_names = self.qdata['quests'], self.qdata['npc_names']
        done = set(load_prefs().get('done_quests', []))
        level = self.get_level()

        def start_map(qd):
            n = self.npcs.get(qd['start_npc'])
            return min(n['maps'], key=lambda m: self.dist.get(m, 999)) if n and n['maps'] else None

        def level_ok(qd):
            return level is None or ((qd['lvmin'] or 0) <= level and (not qd['lvmax'] or level <= qd['lvmax']))

        term = _norm(q)
        if term:
            labels = {qid: _norm(quest_label(self.qdata, qid)) for qid in quests}  # includes what shared-name quests want
            hits = [(0 if labels[qid].startswith(term) else 1, qid) for qid, qd in quests.items()
                    if term in labels[qid] or term in _norm(npc_names.get(qd['start_npc'], ''))]
            ids = [qid for _, qid in sorted(hits, key=lambda h: (h[0], quests[h[1]]['lvmin'] or 0))]
        else:
            ids = [qid for qid, qd in quests.items() if qid not in done and level_ok(qd)
                   and all(p in done for p in qd['prereq']) and start_map(qd) in self.dist]
            ids.sort(key=lambda i: (self.dist.get(start_map(quests[i]), 999), -(quests[i]['lvmin'] or 0)))
        rows = []
        if not term:  # quests in the game's own Quest Helper box come first
            for e in self.get_active_quests():
                qd = quests[e['qid']]
                n = self.npcs.get(qd['end_npc'])
                map_id = min(n['maps'], key=lambda m: self.dist.get(m, 999)) if n and n['maps'] else None
                m = self._map(map_id) if map_id else {'id': None, 'name': '?', 'street': ''}
                reqs = len(qd['mobs']) + len(qd['items'])
                got = sum(1 for h, need in e['progress'].values() if h is not None and h >= need)
                state = 'ready to hand in' if e['complete'] else f'{got}/{reqs} done' if reqs else 'in progress'
                rows.append((f' [Active] {quest_label(self.qdata, e["qid"])}  ·  {state}  ·  hand in to '
                             f'{npc_names.get(qd["end_npc"], "?")} ({m["name"]})', m, True, {'quest': e['qid']}))
            ids = [i for i in ids if i not in {e['qid'] for e in self.get_active_quests()}]
        for qid in ids[:60]:
            qd = quests[qid]
            map_id = start_map(qd)
            m = self._map(map_id) if map_id else {'id': None, 'name': '?', 'street': ''}
            npc = npc_names.get(qd['start_npc'], 'no NPC')
            text = (f' {"[done] " if qid in done else ""}{quest_label(self.qdata, qid)}  ·  Lv {qd["lvmin"] or 1}  ·  '
                    f'{npc} ({m["name"]})  ·  {self._hops(map_id) if map_id else "?"}')
            missing = [quests[p]['name'] for p in qd['prereq'] if p not in done and p in quests]
            if missing:
                text += f'  ·  after: {missing[0]}'
            rows.append((text, m, level_ok(qd) and map_id in self.dist and not missing, {'quest': qid}))
        if not rows:
            rows.append((' No quests found' if term else ' No new quests nearby for your level', None, False))
        return rows

    def _fill(self, rows):
        self.rows = []
        self.list.delete(0, 'end')
        for text, m, reachable, *npc in rows:
            self.list.insert('end', text)
            if not reachable:
                self.list.itemconfig('end', fg=DIM)
            self.rows.append((m, npc[0] if npc else None))
        if self.rows:
            self.list.selection_clear(0, 'end')
            self.list.selection_set(0)
            self.list.see(0)
        self._update_fav_button()

    def move(self, step):
        if not self.rows:
            return 'break'
        cur = self.list.curselection()
        i = max(0, min(len(self.rows) - 1, (cur[0] if cur else -1) + step))
        self.list.selection_clear(0, 'end')
        self.list.selection_set(i)
        self.list.see(i)
        self._update_fav_button()
        return 'break'

    # -- favourite maps --------------------------------------------------------------
    def _selected_map(self):
        cur = self.list.curselection()
        return self.rows[cur[0]][0] if cur and cur[0] < len(self.rows) else None

    def _update_fav_button(self):
        m = self._selected_map()
        fav = m is not None and m['id'] in load_favorites()
        self.fav_button.config(text=f'{STAR} Unfavorite' if fav else f'{NO_STAR} Favorite',
                               fg=ACCENT if m is not None else DIM)

    def toggle_favorite(self, map_id=None):
        """Add or remove a map (default: the selected one) from the favourites; keeps it selected."""
        if self.mode != 'maps':
            return
        if map_id is None:
            m = self._selected_map()
            if m is None:
                return
            map_id = m['id']
        favs = load_favorites()
        save_prefs(favorite_maps=[f for f in favs if f != map_id] if map_id in favs else favs + [map_id])
        self.refresh()
        for i, (m, _) in enumerate(self.rows):
            if m and m['id'] == map_id:
                self.list.selection_clear(0, 'end')
                self.list.selection_set(i)
                self.list.see(i)
                break
        self._update_fav_button()
        self.entry.focus_set()

    def _star_click(self, e):
        """A click on a row's star (its first few characters) toggles that map as a favourite."""
        if self.mode != 'maps' or not self.rows:
            return None
        i = self.list.nearest(e.y)
        bbox = self.list.bbox(i)
        star_w = tk.font.Font(font=self.list.cget('font')).measure(f' {STAR} ')
        if bbox and bbox[1] <= e.y <= bbox[1] + bbox[3] and e.x < bbox[0] + star_w and self.rows[i][0]:
            self.toggle_favorite(self.rows[i][0]['id'])
            return 'break'
        return None

    def go(self):
        cur = self.list.curselection()
        if not cur or cur[0] >= len(self.rows):
            return
        m, npc = self.rows[cur[0]]
        if isinstance(npc, dict) and 'quest' in npc:
            self.on_quest(npc['quest'])
            self.close()
            return
        if isinstance(npc, dict) and 'item' in npc:
            if self.on_item:
                self.on_item(npc['item'])
            self.close()
            return
        if m is None:  # an informational row such as "No map has all of ..."
            return
        save_recent(m['id'])
        self.on_pick(m['id'], npc)
        self.close()

    def stop(self):
        self.on_pick(None)
        self.close()

    def close(self):
        if self.panel is not None:
            self.panel.destroy()
            self.panel = None
            self.panel_hwnd = None
            self.on_close()
