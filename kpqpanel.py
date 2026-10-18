"""Kerning PQ's first stage: a panel at the left listing every question Cloto can ask with its answer (coupons to
collect), the one she asked highlighted, or what she asked if it isn't in the list yet.

A window of its own (like the quest and route panels), built once and only rebuilt when what it shows changes, so it
doesn't flicker and it fades with the other panels while you move.
"""
import tkinter as tk

from picker import ACCENT, BG, DIM, FG


class KpqPanel:
    def __init__(self, root, k, prepare_window):
        self.k = k
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-topmost', True)
        self.win.config(bg=ACCENT)
        self.body = tk.Frame(self.win, bg=BG, padx=int(12 * k), pady=int(10 * k))
        self.body.pack(padx=2, pady=2)
        self.hwnd = prepare_window(self.win)
        self.shown, self.pos = None, None

    def render(self, questions, asked=None, coupons=None, unknown=None):
        """questions: [(question, coupons, confirmed)]; asked: the index of the one Cloto asked (its answer worked out
        for her exact question: coupons); unknown: her question, if it isn't one of them."""
        key = (tuple(questions), asked, coupons, unknown)
        if key == self.shown:
            return
        self.shown = key
        k = self.k
        for w in self.body.winfo_children():
            w.destroy()
        tk.Label(self.body, text="KERNING PQ  ·  CLOTO'S QUESTIONS", font=('Segoe UI', 8, 'bold'), bg=BG, fg=ACCENT,
                 anchor='w').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, int(4 * k)))
        for i, (question, n, confirmed) in enumerate(questions):
            here = i == asked
            bg, fg = (ACCENT, BG) if here else (BG, FG)
            tk.Label(self.body, text=str(coupons if here and coupons is not None else n), font=('Segoe UI', 11, 'bold'),
                     bg=bg, fg=fg, anchor='e', width=3).grid(row=i + 1, column=0, sticky='nsew')
            tk.Label(self.body, text=question + ('' if confirmed else ' *'), font=('Segoe UI', 9, 'bold' if here else 'normal'),
                     bg=bg, fg=fg if here else '#c9d1e0', anchor='w', padx=int(6 * k)).grid(row=i + 1, column=1, sticky='nsew')
        row = len(questions) + 1
        if unknown:  # a question that isn't in the list: show what she asked
            tk.Label(self.body, text=f'Cloto asked (not in this list yet):\n“{unknown}”', font=('Segoe UI', 9, 'bold'),
                     bg=ACCENT, fg=BG, anchor='w', justify='left', wraplength=int(330 * k), padx=int(6 * k),
                     pady=int(3 * k)).grid(row=row, column=0, columnspan=2, sticky='we', pady=(int(6 * k), 0))
            row += 1
        tk.Label(self.body, text='number = coupons to collect  ·  * from Classic World guides, not yet seen in game'
                                 '  ·  (?) a guess', font=('Segoe UI', 7), bg=BG, fg=DIM, anchor='w', justify='left',
                 wraplength=int(330 * k)).grid(row=row, column=0, columnspan=2, sticky='w', pady=(int(6 * k), 0))

    def show(self, x, y):
        """At (x, y), screen coordinates."""
        pos = (int(x), int(y))
        if pos != self.pos:
            self.pos = pos
            self.win.geometry(f'+{pos[0]}+{pos[1]}')
        if self.win.state() == 'withdrawn':
            self.win.deiconify()
            self.win.lift()

    def rect(self):
        """(x0, y0, x1, y1) on screen, or None while hidden."""
        if self.win.state() == 'withdrawn':
            return None
        x, y = self.win.winfo_rootx(), self.win.winfo_rooty()
        return x, y, x + self.win.winfo_width(), y + self.win.winfo_height()

    def hide(self):
        if self.win.state() != 'withdrawn':
            self.win.withdraw()

    def raise_window(self):
        if self.win.state() != 'withdrawn':
            self.win.attributes('-topmost', True)
            self.win.lift()

    def destroy(self):
        self.win.destroy()
