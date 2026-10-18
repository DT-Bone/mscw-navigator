"""Following a leveling route (leveling.py): each stage's quests one after another, then hunting at the stage's map
until its last level, then the next stage. Only while following a route does the navigator move on to the next
quest by itself; where you are on the route is saved (prefs 'following') and picked up again at the next start.
"""
import tkinter as tk

import leveling
from picker import ACCENT, BG, DIM, FG, PANEL

# the first job advancement's instructors (NPC ids): (job class, NPC, town, what the job's like)
JOB_INSTRUCTORS = (('Warrior', 511, 'Perion', 'close combat with swords, axes or blunt weapons; lots of HP'),
                   ('Magician', 313, 'Ellinia', 'spells from a distance; low HP, lots of MP'),
                   ('Bowman', 221, 'Henesys', 'bows and crossbows from a distance'),
                   ('Thief', 411, 'Kerning City', 'fast attacks with claws (throwing stars) or daggers'))


def instructor(job):
    """(NPC id, town) of a job's instructor."""
    return next((npc_id, town) for j, npc_id, town, _ in JOB_INSTRUCTORS if j == job)


class RouteFollow:
    """Where you are on a route: its stage, and the quests picked up or skipped along the way. route is None until
    it's chosen: both routes start the same (levels 1-10), and you pick one at level 10 (choose)."""

    def __init__(self, route, stage, qdata, picked=(), skipped=(), job_done=False, job_choice=None, finished=None):
        self.qdata = qdata
        self.picked, self.skipped = set(picked), set(skipped)  # quest ids
        self.job = None            # (job name, job class) from the status bar, set by the overlay
        self.job_done = job_done   # the first job advancement was made (or marked done)
        self.job_choice = job_choice if job_choice in {j[0] for j in JOB_INSTRUCTORS} else None  # the job picked
        self.choose(route)
        self.stage = max(0, min(int(stage), len(self.stages) - 1))
        # quests the route had you complete (it doesn't know about quests done outside it: those are Skipped). Starting
        # partway along, the earlier stages' are behind you.
        self.finished = set(finished) if finished is not None else {
            qid for st in self.stages[:self.stage] for qid, kind in leveling.tasks(st, self.route, qdata) if kind == 'complete'}

    def choose(self, route):
        """Set (or leave unset: None) the route; the shared start is the same on both, so the stage stays."""
        self.route = route
        self.stages = leveling.stages(route) if route else leveling.stages('A')[:leveling.shared_count()]
        # quests this route does somewhere (an earlier one has to be done before a quest that needs it)
        self.route_quests = {qid for st in self.stages for qid, _ in leveling.tasks(st, route, self.qdata)}

    @property
    def current(self):
        return self.stages[self.stage]

    @property
    def name(self):
        """'Route A' on that route's own stages; 'Leveling route' on the ones both routes share."""
        own = self.route and self.current['route'] == self.route
        return leveling.data()['routes'][self.route]['name'] if own else 'Leveling route'

    def state(self):
        return {'route': self.route, 'stage': self.stage, 'picked': sorted(self.picked), 'skipped': sorted(self.skipped),
                'job_done': self.job_done, 'job_choice': self.job_choice, 'finished': sorted(self.finished)}

    def pending(self, done, active):
        """[(quest id, kind)] of the stage still to do. A quest to pick up is done once it's accepted (in the
        game's Quest Helper box); one to complete once it's handed in. Skipped ones count as done."""
        out = []
        for qid, kind in leveling.tasks(self.current, self.route, self.qdata):
            if qid in done or qid in self.skipped:
                continue
            if kind == 'pick up' and (qid in active or qid in self.picked):
                continue
            out.append((qid, kind))
        return out

    def blocked(self, qid, level, done):
        """Why a quest can't be taken yet ('Lv 18', an earlier quest of the route) or None."""
        q = self.qdata['quests'].get(qid) or {}
        if q.get('lvmin') and level and q['lvmin'] > level:
            return f'Lv {q["lvmin"]}'
        for p in q.get('prereq') or []:
            if p in self.route_quests and p not in done and p not in self.skipped:
                return f'after {self.qdata["quests"][p]["name"]}'
        return None

    def next_task(self, level, done, active):
        return next(((qid, kind) for qid, kind in self.pending(done, active) if not self.blocked(qid, level, done)),
                    None)

    def stage_done(self, level, done, active):
        """Nothing left to do now, and you've reached the stage's last level."""
        return ((level or 0) >= self.current['levels'][1] and self.next_task(level, done, active) is None
                and not self.needs_job(level, done, active))

    def needs_job(self, level, done, active):
        """Level 10 and the shared start's quests done, but no job yet: time for the first job advancement (before
        picking a route). Asked even before the status bar's job has been read; a job read there ends it."""
        if self.job_done:
            return False
        if self.job and self.job[1] != 'Beginner':
            self.job_done = True
            return False
        return (self.stage == leveling.shared_count() - 1 and (level or 0) >= 10
                and self.next_task(level, done, active) is None)

    def needs_choice(self, level, done, active):
        """At level 10, with the shared start done and the first job made: time to pick Route A or B."""
        return self.route is None and self.stage == len(self.stages) - 1 and self.stage_done(level, done, active)

    def advance(self):
        if self.stage + 1 < len(self.stages):
            self.stage += 1
            return True
        return False


class RoutePanel:
    """Shown while following a route and no quest is being guided: hunt at the stage's map until its last level."""

    def __init__(self, root, k, prepare_window, on_next_stage, on_stop):
        self.k, self.on_next_stage, self.on_stop = k, on_next_stage, on_stop
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.config(bg=ACCENT)
        self.body = tk.Frame(self.win, bg=BG, padx=int(14 * k), pady=int(12 * k))
        self.body.pack(padx=2, pady=2)
        self.game_rect, self.top, self.shown = None, None, None
        self.hwnd = prepare_window(self.win)

    def render_choice(self, on_choose, job=None):
        """Level 10, after the first job advancement (job: its name, if read): pick the route to follow from here."""
        if self.shown == 'choice':
            return
        self.shown = 'choice'
        k, wrap = self.k, int(380 * self.k)
        for w in self.body.winfo_children():
            w.destroy()
        tk.Label(self.body, text='LEVELING ROUTE', font=('Segoe UI', 8, 'bold'), bg=BG, fg=ACCENT, anchor='w').pack(fill='x')
        tk.Label(self.body, text=f"You're a {job} now! Pick your route" if job else 'Level 10: time to pick your route',
                 font=('Segoe UI', 12, 'bold'), bg=BG, fg=FG, anchor='w').pack(fill='x')
        tk.Label(self.body, text="They're about as fast as each other; quests along the way are optional on both.",
                 font=('Segoe UI', 9), bg=BG, fg=DIM, anchor='w', justify='left', wraplength=wrap).pack(fill='x')
        for key, route in leveling.data()['routes'].items():
            row = tk.Frame(self.body, bg=BG)
            row.pack(fill='x', pady=(int(8 * k), 0))
            b = tk.Label(row, text=route['name'], font=('Segoe UI', 10, 'bold'), bg=ACCENT, fg=BG, padx=int(10 * k),
                         pady=int(3 * k), cursor='hand2')
            b.pack(side='left', anchor='n')
            b.bind('<Button-1>', lambda e, r=key: on_choose(r))
            tk.Label(row, text=route['summary'], font=('Segoe UI', 9), bg=BG, fg=FG, anchor='w', justify='left',
                     wraplength=wrap - int(110 * k)).pack(side='left', padx=(int(8 * k), 0))
        btns = tk.Frame(self.body, bg=BG)
        btns.pack(fill='x', pady=(int(10 * k), 0))
        b = tk.Label(btns, text='Stop following', font=('Segoe UI', 10, 'bold'), bg=PANEL, fg=FG, padx=int(10 * k),
                     pady=int(3 * k), cursor='hand2')
        b.pack(side='left')
        b.bind('<Button-1>', lambda e: self.on_stop())
        self.place()

    def render_job(self, choice, npcs, map_names, on_pick, on_go, on_done):
        """Level 10, the shared start's quests done: which job do you want (choice None), then where its instructor
        is (the arrow takes you there). on_pick(job or None), on_go(job), on_done()."""
        if self.shown == ('job', choice):
            return
        self.shown = ('job', choice)
        k, wrap = self.k, int(380 * self.k)
        for w in self.body.winfo_children():
            w.destroy()
        tk.Label(self.body, text='LEVELING ROUTE', font=('Segoe UI', 8, 'bold'), bg=BG, fg=ACCENT, anchor='w').pack(fill='x')

        def buttons(items):
            btns = tk.Frame(self.body, bg=BG)
            btns.pack(fill='x', pady=(int(10 * k), 0))
            for text, cmd, primary in items:
                b = tk.Label(btns, text=text, font=('Segoe UI', 10, 'bold'), bg=ACCENT if primary else PANEL,
                             fg=BG if primary else FG, padx=int(10 * k), pady=int(3 * k), cursor='hand2')
                b.pack(side='left', padx=(0, int(6 * k)))
                b.bind('<Button-1>', lambda e, c=cmd: c())

        if choice is None:
            tk.Label(self.body, text='Level 10: which job do you want?', font=('Segoe UI', 12, 'bold'), bg=BG, fg=FG,
                     anchor='w').pack(fill='x')
            tk.Label(self.body, text="Time for your first job advancement. Pick one and you'll be taken to its "
                                     "instructor; you pick your leveling route after that.", font=('Segoe UI', 9),
                     bg=BG, fg=DIM, anchor='w', justify='left', wraplength=wrap).pack(fill='x')
            for job, npc_id, town, about in JOB_INSTRUCTORS:
                npc = npcs.get(npc_id) or {}
                row = tk.Frame(self.body, bg=BG)
                row.pack(fill='x', pady=(int(8 * k), 0))
                b = tk.Label(row, text=job, font=('Segoe UI', 10, 'bold'), bg=ACCENT, fg=BG, width=9, pady=int(3 * k),
                             cursor='hand2')
                b.pack(side='left', anchor='n')
                b.bind('<Button-1>', lambda e, j=job: on_pick(j))
                tk.Label(row, text=f'{about[0].upper()}{about[1:]}.\n{npc.get("name", "?")}, {town}', font=('Segoe UI', 9),
                         bg=BG, fg=FG, anchor='w', justify='left',
                         wraplength=wrap - int(110 * k)).pack(side='left', padx=(int(8 * k), 0))
            buttons((('I already have a job', on_done, False), ('Stop following', self.on_stop, False)))
        else:
            npc_id, town = instructor(choice)
            npc = npcs.get(npc_id) or {}
            map_id = next(iter(npc.get('maps') or {}), None)
            where = f'{str(map_names.get(map_id, town)).strip()} ({town})' if map_id else town
            tk.Label(self.body, text=f'Become a {choice}', font=('Segoe UI', 12, 'bold'), bg=BG, fg=FG,
                     anchor='w').pack(fill='x')
            tk.Label(self.body, text=f'Talk to {npc.get("name", "the instructor")} in {where} to make your first job '
                                     f"advancement. From Lith Harbor, take Phil's taxi to {town}; the arrow takes "
                                     f'you there.', font=('Segoe UI', 10), bg=BG, fg=FG,
                     anchor='w', justify='left', wraplength=wrap).pack(fill='x', pady=(int(4 * k), 0))
            tk.Label(self.body, text="If you're missing anything (a level or some stats), they'll tell you. Once "
                                     "you've advanced, press Done, or it moves on by itself when your new job shows "
                                     "on the status bar.", font=('Segoe UI', 9), bg=BG, fg=DIM, anchor='w',
                     justify='left', wraplength=wrap).pack(fill='x', pady=(int(4 * k), 0))
            buttons((('Done \u2713', on_done, True), ('Go', lambda: on_go(choice), False),
                     ('Other job', lambda: on_pick(None), False), ('Stop following', self.on_stop, False)))
        self.place()

    def render(self, follow, level, done, active, map_name):
        content = (follow.stage, level, tuple(follow.pending(done, active)), map_name)
        if content == self.shown:
            return
        self.shown = content
        k, st = self.k, follow.current
        wrap = int(380 * k)
        for w in self.body.winfo_children():
            w.destroy()
        label = lambda text, size=9, colour=DIM, bold=False, pad=0: tk.Label(
            self.body, text=text, font=('Segoe UI', size, 'bold' if bold else 'normal'), bg=BG, fg=colour, anchor='w',
            justify='left', wraplength=wrap).pack(fill='x', pady=(pad, 0))
        label('LEVELING ROUTE', 8, ACCENT, True)
        label(f'{follow.name}  ·  {leveling.levels_text(st)}  ·  {st["title"]}', 12, FG, True)
        hi = st['levels'][1]
        label(f'Hunt at {map_name} until Lv {hi}' + (f'  (you\'re {level})' if level else ''), 10, ACCENT, True, int(6 * k))
        waiting = [(qid, follow.blocked(qid, level, done)) for qid, _ in follow.pending(done, active)]
        if waiting:
            label('Later in this stage: ' + ', '.join(f'{follow.qdata["quests"][q]["name"]} ({why})' for q, why in waiting),
                  9, FG, pad=int(4 * k))
        for step in st.get('steps', []):
            label(f'• {step}', pad=int(2 * k))
        if st.get('collect'):
            label('Collect: ' + ', '.join(f'{n} ×{c}' for n, c in st['collect']), 9, FG, pad=int(4 * k))
        btns = tk.Frame(self.body, bg=BG)
        btns.pack(fill='x', pady=(int(10 * k), 0))
        for text, cmd, primary in (('Next stage ▶', self.on_next_stage, True), ('Stop following', self.on_stop, False)):
            b = tk.Label(btns, text=text, font=('Segoe UI', 10, 'bold'), bg=ACCENT if primary else PANEL,
                         fg=BG if primary else FG, padx=int(10 * k), pady=int(3 * k), cursor='hand2')
            b.pack(side='left', padx=(0, int(6 * k)))
            b.bind('<Button-1>', lambda e, c=cmd: c())
        self.place()

    def show(self, game_rect, top=None):
        if (game_rect, top) != (self.game_rect, self.top):
            self.game_rect, self.top = game_rect, top
            self.place()
        if self.win.state() == 'withdrawn':
            self.win.deiconify()
            self.win.lift()

    def place(self):
        """Under the minimap, like the quest panel, and kept inside the game window."""
        if not self.game_rect:
            return
        gx, gy, gw, gh = self.game_rect
        y = self.top + gh * 0.015 if self.top else gh * 0.25
        self.win.update_idletasks()
        y = max(0, min(y, gh - self.win.winfo_reqheight() - gh * 0.01))
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
