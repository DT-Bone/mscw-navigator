"""Quest helper: turn a quest into steps (accept, hunt/loot, hand in) and show them in a panel.

The game doesn't tell us quest progress, so the player advances steps with Next; each step
sets the navigator's destination (an NPC, or the best map to hunt in).
"""
import tkinter as tk

from navgraph import distances
from quests import label as quest_label

SPLIT_MIN = 5  # spawns of each needed monster for one map to cover a whole hunt
WALK_SECONDS = 15     # rough time to cross one map
RESPAWN_SECONDS = 10  # rough time per wave of kills on a map
BG, PANEL, FG, DIM, ACCENT, OK = '#1d2433', '#262f42', '#ffffff', '#8b95a8', '#f2b134', '#7bd88f'


_craft = None
_item_names = None


def _crafting():
    """(disciplines, recipes by the item they make) from crafting.json, or empty if it isn't built."""
    global _craft
    if _craft is None:
        try:
            import crafting
            disciplines, _, makes, _ = crafting.load()
            _craft = (disciplines, makes)
        except OSError:
            _craft = ({}, {})
    return _craft


def ITEM_NAMES():
    """Names of every item (quests.json only names items that quests use)."""
    global _item_names
    if _item_names is None:
        try:
            import items
            _item_names = {i: r['name'] for i, r in items.load()[0].items()}
        except OSError:
            _item_names = {}
    return _item_names


class QuestGuide:
    def __init__(self, qid, qdata, npcs, mobs, graph, map_names):
        self.qid, self.q = qid, qdata['quests'][qid]
        self.qdata, self.npcs, self.mobs, self.graph, self.map_names = qdata, npcs, mobs, graph, map_names
        self.steps = self._build_steps()
        self.index = 0
        self.progress = {}  # (kind, id) -> (have, need) read from the game's Quest Helper box
        self.dist = {}      # map -> maps away, from where the last target() was worked out

    def _npc_name(self, npc_id):
        return self.qdata['npc_names'].get(npc_id) or f'NPC {npc_id}'

    def _build_steps(self):
        q, steps = self.q, []
        if q['start_npc']:
            steps.append({'kind': 'talk', 'npc': q['start_npc'], 'accept': True,
                          'text': f'Talk to {self._npc_name(q["start_npc"])} to accept the quest',
                          'quiz': (q.get('quiz') or {}).get('0', [])})
        kill_mobs = [m for m, _ in q['mobs']]
        # One line per requirement; group everything that can be done by hunting into one step.
        hunt_lines, hunt_mobs, hunt_reqs, other, crafts = [], [], [], [], []
        for mob, count in q['mobs']:
            hunt_lines.append(f'Kill {count} {self.qdata["mob_names"].get(mob, mob)}')
            hunt_mobs.append([mob])
            hunt_reqs.append(('mob', mob))
        given = set(q.get('accept_items', []))
        for item, count in q['items']:
            plan = self._item_plan(q, item, count, kill_mobs)
            if plan[0] == 'deliver':
                continue  # handed to you when you accept: nothing to find, just deliver it
            if plan[0] == 'hunt':
                hunt_lines.append(plan[1])
                hunt_mobs.append(plan[2])
                hunt_reqs.append(('item', item))
            elif plan[0] == 'craft':
                crafts.append({'kind': 'craft', 'npc': plan[2], 'text': plan[1], 'lines': [plan[1]],
                               'reqs': [('item', item)]})
            else:
                other.append((plan[1], ('item', item)))
        for lines, groups, reqs in self._split_hunts(hunt_lines, hunt_mobs, hunt_reqs):
            steps.append({'kind': 'hunt', 'mob_groups': groups, 'lines': lines, 'reqs': reqs,
                          'text': lines[0] if len(lines) == 1 else 'Hunt: ' + '; '.join(lines)})
        steps += crafts
        for line, req in other:
            steps.append({'kind': 'other', 'text': line, 'lines': [line], 'reqs': [req]})
        if q['end_npc']:
            npc = self._npc_name(q['end_npc'])
            delivered = [self.qdata['items'].get(i, f'Item {i}') for i, _ in q['items'] if i in given]
            if delivered and len(delivered) == len(q['items']) and not q['mobs']:
                text = f'Deliver {" and ".join(delivered)} to {npc} (you get it when you accept)'
            elif q['mobs'] or q['items']:
                text = f'Bring everything to {npc} to complete the quest'
                if delivered:
                    text += f' (including {" and ".join(delivered)}, which you get when you accept)'
            else:
                text = f'Talk to {npc} to complete the quest'
            steps.append({'kind': 'talk', 'npc': q['end_npc'], 'text': text, 'quiz': (q.get('quiz') or {}).get('1', [])})
        return steps

    def _item_plan(self, q, item, count, kill_mobs=()):
        """Where a quest's required item comes from: ('deliver',) when the NPC hands it over, ('hunt',
        text, monsters), ('craft', text, master NPC), or ('other', text). Checked in this order: given on
        accept, given by an earlier quest in the chain, a monster named in the item's name, a crafting
        recipe, a monster the quest text names, the quest's own kill targets, any quest that gives some."""
        qd = self.qdata
        name = qd['items'].get(item, f'Item {item}')
        mob_list = lambda mobs: ' / '.join(qd['mob_names'].get(m, str(m)) for m in mobs)
        if item in q.get('accept_items', []):
            return ('deliver',)
        from_quest = {int(k): v for k, v in (q.get('item_from_quest') or {}).items()}
        given_by = from_quest.get(item)
        if given_by is not None and given_by in q['prereq']:  # an earlier quest in this chain gives it
            return ('other', f'Get {count} {name} (you get it from the quest "{qd["quests"][given_by]["name"]}")')
        src = [m for m in qd['item_sources'].get(item) or [] if m in self.mobs]
        if src:
            return ('hunt', f'Collect {count} {name} (from {mob_list(src)})', src)
        disciplines, makes = _crafting()
        if item in makes:
            r = min(makes[item], key=lambda r: r['level'])
            d = disciplines.get(r['skill'], {})
            crafts = -(-count // r['result'][1])  # one craft can make several
            item_name = lambda i: qd['items'].get(i) or ITEM_NAMES().get(i, f'item {i}')
            parts = [f'{c * crafts:,} {item_name(i)}' for i, c in r['ingredients']]
            if r['additive']:
                parts.append(f'{r["additive"][1] * crafts:,} {item_name(r["additive"][0])}')
            text = (f'Craft {count} {name}: {d.get("name", "crafting")} Lv {r["level"]}, needs {" + ".join(parts)}'
                    + (f' + {r["meso"] * crafts:,} mesos' if r['meso'] else '')
                    + (f' in all' if crafts > 1 else '')
                    + (f' (learn it from {self._npc_name(d["npc"])})' if d.get('npc') else ''))
            return ('craft', text, d.get('npc'))
        text_src = [m for m in {int(k): v for k, v in (q.get('text_sources') or {}).items()}.get(item, [])
                    if m in self.mobs]
        if text_src:
            return ('hunt', f'Collect {count} {name} (probably from {mob_list(text_src)}, going by the quest text)',
                    text_src)
        if kill_mobs:  # kill-and-collect quests usually drop from the targets
            return ('hunt', f'Collect {count} {name} (probably from {mob_list(kill_mobs)})', list(kill_mobs))
        if given_by is not None:
            return ('other', f'Get {count} {name} (the quest "{qd["quests"][given_by]["name"]}" gives some)')
        return ('other', f'Get {count} {name} (not a known monster drop; see the quest text)')

    def upcoming(self, depth=2):
        """[(quest name, [(what it needs, hunting map or None)])] for the next quests in this chain that
        need items or kills, so they can be collected on the way (a quest's text often describes the
        whole errand). Hunting lines name the best map for the monster, as hunting steps do."""
        quests, out = self.qdata['quests'], []
        frontier, seen = list(self.q['next']), {self.qid}
        for _ in range(depth):
            nxt = []
            for qid in frontier:
                if qid in seen or qid not in quests:
                    continue
                seen.add(qid)
                q = quests[qid]
                kills = [m for m, _ in q['mobs']]
                lines = [self._hunt_line(f'Kill {c} {self.qdata["mob_names"].get(m, m)}', [m], c)
                         for m, c in q['mobs']]
                for item, count in q['items']:
                    plan = self._item_plan(q, item, count, kills)
                    if plan[0] == 'deliver':
                        lines.append((f'{self.qdata["items"].get(item, item)}: handed to you when you accept it', None))
                    elif plan[0] == 'hunt':
                        lines.append(self._hunt_line(plan[1], plan[2], count * 2))
                    else:
                        lines.append((plan[1], None))
                if lines:
                    out.append((quest_label(self.qdata, qid), lines))
                nxt += q['next']
            frontier = nxt
        return out

    def _hunt_line(self, text, mobs, kills):
        """(text + where to hunt, map) for a monster needed later in the chain; distances are from where
        the current step was last worked out (self.dist)."""
        best = self.best_map([mobs], [kills], self.dist)
        if best is None:
            return text, None
        map_id = best[0]
        parts = [f'{self.farmable(map_id, [m])} {self.qdata["mob_names"].get(m, m)}' for m in mobs
                 if m in self.graph[map_id]['mobs']]
        return f'{text}: hunt at {str(self.map_names.get(map_id, map_id)).strip()} ({" + ".join(parts)})', map_id

    def apply_log(self, entry, handed_in):
        """Update from the game's Quest Helper box. entry is this quest's (smoothed) entry, or None if it
        isn't in the box; handed_in means it has reliably left the box at the hand-in NPC's map.
        Returns True if anything changed."""
        before = (self.index, self.progress)
        final = len(self.steps) - 1
        if entry is not None:
            self.progress = entry['progress']
            if self.index == 0 and self.steps and self.steps[0].get('accept'):
                self.index = 1  # it's in the box, so it has been accepted
            if entry['complete'] and self.steps[final]['kind'] == 'talk' and self.index < final:
                self.index = final  # everything collected: go hand it in
            while self.index < final and self.steps[self.index].get('reqs') and all(
                    (self.progress.get(r) or (None, None))[0] is not None
                    and self.progress[r][0] >= self.progress[r][1] for r in self.steps[self.index]['reqs']):
                self.index += 1  # this hunt's counts are full: on to the next step
        elif handed_in and self.index == final:
            self.index = len(self.steps)  # left the box while handing in: completed
        return (self.index, self.progress) != before

    def progress_text(self, req):
        have, need = self.progress.get(req, (None, None))
        if need is None:
            return ''
        if have is None:
            return f'  ?/{need}'
        return f'  {have}/{need} ✓' if have >= need else f'  {have}/{need}'

    def farmable(self, map_id, mob_ids):
        """Spawn points for these monsters on a map, leaving out ones on a respawn timer."""
        m = self.graph[map_id]
        return sum(m['mobs'].get(i, [0])[0] - m.get('mob_timed', {}).get(i, 0) for i in mob_ids)

    def _split_hunts(self, lines, groups, reqs):
        """One hunting step if some map has a decent number (SPLIT_MIN+) of every monster needed;
        otherwise one step per monster, each with its own best map."""
        if not lines:
            return []
        by_mobs = {}  # requirements grouped by the monsters that satisfy them (kill + drop of one monster)
        for line, group, req in zip(lines, groups, reqs):
            entry = by_mobs.setdefault(tuple(group), ([], [], []))
            entry[0].append(line)
            entry[1].append(group)
            entry[2].append(req)
        if len(by_mobs) == 1:
            return [(lines, groups, reqs)]

        spawns = self.farmable
        maps_for = [{m for mob in key for m in self.mobs.get(mob, {}).get('maps', {})} for key in by_mobs]
        if any(all(spawns(m, key) >= SPLIT_MIN for key in by_mobs) for m in set.intersection(*maps_for)):
            return [(lines, groups, reqs)]
        return list(by_mobs.values())

    @property
    def step(self):
        return self.steps[self.index] if self.index < len(self.steps) else None

    def target(self, current_map, opts, travel_data):
        """(map_id, npc (x, y, name) or None, note) for the current step, or (None, None, note)."""
        step = self.step
        if step is None or current_map not in self.graph:
            return None, None, ''
        dist = self.dist = distances(self.graph, current_map, opts, travel_data)
        if step['kind'] in ('talk', 'craft') and step.get('npc'):
            n = self.npcs.get(step['npc'])
            if not n or not n['maps']:
                return None, None, "this NPC isn't on any map in the game data"
            map_id = min(n['maps'], key=lambda m: dist.get(m, 999))
            x, y = n['maps'][map_id]
            return map_id, (x, y, n['name']), ''
        if step['kind'] == 'hunt':
            best = self.best_map(step['mob_groups'], self.needs(step), dist)
            if best is None:
                return None, None, 'no reachable map has these monsters'
            map_id, spawns, all_in_one = best
            # e.g. "Rocky Road I: 10 Stump + 12 Slime" (only the monsters this step needs)
            mobs_here = self.graph[map_id]['mobs']
            wanted = dict.fromkeys(m for g in step['mob_groups'] for m in g)
            parts = [f'{self.farmable(map_id, [m])} {self.qdata["mob_names"].get(m, m)}' for m in wanted
                     if m in mobs_here]
            note = f'{self.map_names.get(map_id, map_id)}: ' + (' + '.join(parts) if parts else f'{spawns} spawns')
            if not all_in_one:
                note += ' (no single map has every monster; this one has the first)'
            return map_id, None, note
        return None, None, ''

    def best_map(self, groups, needs, dist):
        """Best map for monster groups (one per requirement) needing `needs` kills each.

        Picks the map with the lowest rough time: walking there (WALK_SECONDS a map) plus, per monster,
        kills needed / spawn points x RESPAWN_SECONDS. Small jobs stay close; big grinds go where
        monsters are dense. Maps must have every group, else the first group's maps are used."""
        spawns = self.farmable
        maps_for = [{m for mob in g for m in self.mobs.get(mob, {}).get('maps', {})} for g in groups]
        common = set.intersection(*maps_for) if maps_for else set()
        pool, all_in_one = (common, True) if common else (maps_for[0] if maps_for else set(), False)
        reachable = [m for m in pool if m in dist] or list(pool)
        if not reachable:
            return None
        kills = {}  # kills + drops from one monster overlap, so take the larger need per monster group
        for g, n in zip(groups, needs):
            kills[tuple(g)] = max(kills.get(tuple(g), 0), n)

        def seconds(map_id):
            hunt = sum(n / max(spawns(map_id, g), 0.5) * RESPAWN_SECONDS for g, n in kills.items())
            return dist.get(map_id, 99) * WALK_SECONDS + hunt
        best = min(reachable, key=seconds)
        flat = list(dict.fromkeys(m for g in groups for m in g))
        return best, spawns(best, flat), all_in_one

    def needs(self, step):
        """Kills each requirement of a hunting step takes (items: about 2 kills each, drop rates being unknown)."""
        counts = {('mob', i): c for i, c in self.q['mobs']}
        counts.update({('item', i): c * 2 for i, c in self.q['items']})
        return [counts.get(r, 10) for r in step['reqs']]


class QuestPanel:
    """Small panel at the right of the game window listing the steps of the active quest."""

    def __init__(self, root, k, guide, prepare_window, on_step, on_stop, on_next_quest, quest_name, on_go=None):
        self.root, self.k, self.guide = root, k, guide
        self.on_step, self.on_stop, self.on_next_quest, self.quest_name = on_step, on_stop, on_next_quest, quest_name
        self.on_go = on_go  # on_go(map_id): navigate to a map (the Go buttons of upcoming hunts)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.config(bg=ACCENT)
        self.body = tk.Frame(self.win, bg=BG, padx=int(14 * k), pady=int(12 * k))
        self.body.pack(padx=2, pady=2)
        self.note = ''
        self.game_rect = None
        self.render()
        self.hwnd = prepare_window(self.win)

    # -- layout ------------------------------------------------------------------
    def render(self):
        k, g, q = self.k, self.guide, self.guide.q
        for w in self.body.winfo_children():
            w.destroy()
        wrap = int(380 * k)
        tk.Label(self.body, text='QUEST HELPER', font=('Segoe UI', 8, 'bold'), bg=BG, fg=ACCENT, anchor='w').pack(fill='x')
        tk.Label(self.body, text=quest_label(g.qdata, g.qid), font=('Segoe UI', 13, 'bold'), bg=BG, fg=FG, anchor='w',
                 wraplength=wrap, justify='left').pack(fill='x')
        reward = []
        if q['lvmin']:
            reward.append(f'Lv {q["lvmin"]}+')
        if q['reward'].get('exp'):
            reward.append(f'{q["reward"]["exp"]:,} EXP')
        if q['reward'].get('money'):
            reward.append(f'{q["reward"]["money"]:,} mesos')
        if q['reward']['items']:
            n = len(q['reward']['items'])
            reward.append(f'1 of {n} random items' if q['reward'].get('random') else f'{n} item{"s" * (n > 1)}')
        tk.Label(self.body, text='  \u00b7  '.join(reward), font=('Segoe UI', 9), bg=BG, fg=DIM, anchor='w').pack(
            fill='x', pady=(0, int(8 * k)))

        for i, step in enumerate(g.steps):
            done, current = i < g.index, i == g.index
            mark = '\u2713' if done else '\u25b6' if current else '\u25cb'
            row = tk.Frame(self.body, bg=PANEL if current else BG)
            row.pack(fill='x', pady=1)
            tk.Label(row, text=mark, font=('Segoe UI', 11, 'bold'), width=2, bg=row['bg'],
                     fg=OK if done else ACCENT if current else DIM).pack(side='left', anchor='n')
            lines = step.get('lines') if current and step.get('lines') else [step['text']]
            if current and step.get('reqs'):
                lines = [line + g.progress_text(req) for line, req in zip(lines, step['reqs'])]
            box = tk.Frame(row, bg=row['bg'])
            box.pack(side='left', fill='x')
            for line in lines:
                tk.Label(box, text=line, font=('Segoe UI', 10, 'bold' if current else 'normal'), bg=row['bg'],
                         fg=FG if current else DIM, anchor='w', justify='left', wraplength=wrap - int(30 * k)).pack(fill='x')
            if current and self.note:
                tk.Label(box, text='\u2192 ' + self.note, font=('Segoe UI', 9), bg=row['bg'], fg=ACCENT, anchor='w',
                         justify='left', wraplength=wrap - int(30 * k)).pack(fill='x')
            if current and step.get('quiz'):
                # the NPC asks questions here: give the choice to pick for each, in order
                tk.Label(box, text='Answers, in order:', font=('Segoe UI', 9, 'bold'), bg=row['bg'], fg=ACCENT,
                         anchor='w').pack(fill='x', pady=(int(4 * k), 0))
                for n, (question, choice, answer) in enumerate(step['quiz'], 1):
                    short = question if len(question) <= 70 else question[:67].rstrip() + '\u2026'
                    tk.Label(box, text=f'{n}. {short}', font=('Segoe UI', 9), bg=row['bg'], fg=DIM, anchor='w',
                             justify='left', wraplength=wrap - int(30 * k)).pack(fill='x')
                    tk.Label(box, text=f'    \u2192 {answer}  (choice {choice})', font=('Segoe UI', 9, 'bold'),
                             bg=row['bg'], fg=OK, anchor='w', justify='left',
                             wraplength=wrap - int(30 * k)).pack(fill='x')

        finished = g.index >= len(g.steps)
        ahead = g.upcoming() if not finished else []
        if ahead:
            # the next quests in this chain: their items and kills can be collected on the way
            tk.Label(self.body, text='COMING UP IN THIS QUEST CHAIN (collect these on the way)',
                     font=('Segoe UI', 8, 'bold'), bg=BG, fg=ACCENT, anchor='w').pack(fill='x', pady=(int(8 * k), 0))
            for name, lines in ahead:
                tk.Label(self.body, text=name, font=('Segoe UI', 9, 'bold'), bg=BG, fg=FG, anchor='w', justify='left',
                         wraplength=wrap).pack(fill='x')
                for line, go in lines:
                    row = tk.Frame(self.body, bg=BG)
                    row.pack(fill='x', padx=(int(10 * k), 0), pady=(0, int(2 * k)))
                    if go is not None and self.on_go:  # navigate to that hunting map
                        b = tk.Label(row, text='Go', font=('Segoe UI', 9, 'bold'), bg=ACCENT, fg=BG, padx=int(8 * k),
                                     cursor='hand2')
                        b.pack(side='right', anchor='n', padx=(int(8 * k), 0))
                        b.bind('<Button-1>', lambda e, m=go: self.on_go(m))
                    tk.Label(row, text='• ' + line, font=('Segoe UI', 9), bg=BG, fg=DIM, anchor='w', justify='left',
                             wraplength=wrap - int(60 * k)).pack(side='left', fill='x')
        text = q['text']['done'] if finished else (q['text']['progress'] or q['text']['start'])
        if text:
            tk.Label(self.body, text=text[:420] + ('\u2026' if len(text) > 420 else ''), font=('Segoe UI', 9),
                     bg=BG, fg=DIM, anchor='w', justify='left', wraplength=wrap).pack(fill='x', pady=(int(8 * k), 0))

        btns = tk.Frame(self.body, bg=BG)
        btns.pack(fill='x', pady=(int(10 * k), 0))
        if finished:
            tk.Label(self.body, text='Quest complete!', font=('Segoe UI', 11, 'bold'), bg=BG, fg=OK, anchor='w').pack(
                fill='x', before=btns)
            for nxt in q['next'][:2]:
                self._button(btns, f'Next: {self.quest_name(nxt)}', lambda n=nxt: self.on_next_quest(n), True)
            self._button(btns, 'Close', self.on_stop, not q['next'])
        else:
            if g.index > 0:
                self._button(btns, '\u25c0 Back', lambda: self.move(-1), False)
            last = g.index == len(g.steps) - 1
            self._button(btns, 'Done \u2713' if last else 'Next \u25b6', lambda: self.move(1), True)
            self._button(btns, 'Stop quest', self.on_stop, False)
        self.place()

    def _button(self, parent, text, cmd, primary):
        b = tk.Label(parent, text=text, font=('Segoe UI', 10, 'bold'), bg=ACCENT if primary else PANEL,
                     fg=BG if primary else FG, padx=int(10 * self.k), pady=int(3 * self.k), cursor='hand2')
        b.pack(side='left', padx=(0, int(6 * self.k)))
        b.bind('<Button-1>', lambda e: cmd())

    def move(self, step):
        g = self.guide
        g.index = max(0, min(len(g.steps), g.index + step))
        self.note = ''
        self.on_step()
        self.render()

    def set_note(self, note):
        if note != self.note:
            self.note = note
            self.render()

    # -- placement -------------------------------------------------------------------
    def show(self, game_rect, top=None):
        """top: lowest y (game window coords) the panel may start at, e.g. under the minimap."""
        if (game_rect, top) != (self.game_rect, getattr(self, 'top', None)):
            self.game_rect, self.top = game_rect, top
            self.place()
        if self.win.state() == 'withdrawn':
            self.win.deiconify()
            self.win.lift()

    def place(self):
        if not self.game_rect:
            return
        gx, gy, gw, gh = self.game_rect
        y = max(gh * 0.25, (getattr(self, 'top', None) or 0) + gh * 0.015)
        self.win.update_idletasks()
        self.win.geometry(f'+{int(gx + gw * 0.006)}+{int(gy + y)}')

    def hide(self):
        if self.win.state() != 'withdrawn':
            self.win.withdraw()

    def raise_window(self):
        if self.win.state() != 'withdrawn':
            self.win.attributes('-topmost', True)
            self.win.lift()

    def destroy(self):
        self.win.destroy()
