"""The Rule builder's MITRE ATT&CK technique picker: every technique at the ATT&CK version Sentinel is aligned
to (attack_catalog), each parent with its sub-techniques under it, to tick instead of typing IDs.

It keeps the two comma-separated fields drafts read and write, vars['techniques'] (parents, such as T1562) and
vars['subTechniques'] (such as T1562.004), in step with its ticks, as the tactic grid does with vars['tactics'].
Ticking a parent opens it and tints its sub-techniques, so the ones that may apply stand out; ticking a
sub-technique ticks its parent too, as Sentinel needs. The list holds the Enterprise and ICS matrices; an ICS
technique is marked (ICS), as both have some of the same names (Masquerading, say). IDs the catalog doesn't
list (another matrix's, retired IDs, typing slips in a file) are kept as written and listed under the picker,
with Remove beside them; one it doesn't list can be added by typing it whole into the search (Add). IDs in the
wrong field, and sub-techniques whose parent isn't listed, are named there too, with Fix these."""
import re
import tkinter as tk
from tkinter import ttk

from . import attack_catalog as catalog
from . import rule_drafts as drafts
from . import ui_kit as kit
from .desktop_theme import COLORS, FONTS, px
from .rule_schema import TACTICS

# Parent ID -> (name, tactics, [sub-technique IDs]); sub-technique ID -> (name, tactics, parent ID).
PARENTS, SUBS = {}, {}
for _technique, _name, _tactics in catalog.TECHNIQUES:
    if "." in _technique:
        SUBS[_technique] = (_name, _tactics, _technique.split(".")[0])
        PARENTS[_technique.split(".")[0]][2].append(_technique)
    else:
        PARENTS[_technique] = (_name, _tactics, [])
KNOWN = PARENTS.keys() | SUBS.keys()
HEIGHT = 10  # Rows the list shows; more scroll.
# A whole technique ID typed into the search, which Add keeps as typed when the list doesn't hold it.
TYPED_ID = re.compile(r"T\d{4}(\.\d{3})?", re.IGNORECASE)


def entries(value):
    """A comma- or line-separated list field, split the way drafts.to_rule reads it."""
    return [x.strip() for x in str(value).replace("\n", ",").split(",") if x.strip()]


def name_of(technique):
    """The technique's name, marked (ICS) for an ICS one: 'Masquerading (ICS)' beside Enterprise's Masquerading."""
    name = (PARENTS.get(technique) or SUBS.get(technique) or ("",))[0]
    return name + " (ICS)" if technique in catalog.ICS else name


def tactic_text(tactics, prefer=()):
    """'Defense Evasion', or 'Persistence +2' when a technique serves several tactics. A tactic in ``prefer``
    (the ticked ones the list is narrowed to) is named first, so the row says why it is listed."""
    if not tactics: return ""
    first = next((tactic for tactic in tactics if tactic in prefer), tactics[0])
    return drafts.tactic_name(first) + (f" +{len(tactics) - 1}" if len(tactics) > 1 else "")


class TechniquePicker:
    def __init__(self, parent, techniques, subs, tactics, busy=lambda: False, inputs=None):
        self.vars, self.tactics, self.busy = (techniques, subs), tactics, busy
        self.ticked, self.opened, self.storing = set(), set(), False
        inputs = inputs if inputs is not None else []
        self.head = head = kit.field_label(parent, "Techniques", " ")
        self.count, self.count_full = head.hint, ""
        # The hint changes with the list (count_text), so it is shortened here rather than by field_label.
        head.unbind("<Configure>"); head.bind("<Configure>", lambda event: self.fit_count(), add="+")
        bar = ttk.Frame(parent, style="Card.TFrame"); bar.pack(fill="x", pady=(0, px(8)))
        bar.columnconfigure(0, weight=1)
        self.search = tk.StringVar()
        self.search_entry = ttk.Entry(bar, textvariable=self.search)
        self.search_entry.grid(row=0, column=0, sticky="ew")
        kit.placeholder(self.search_entry, self.search, "Search techniques by ID or name", search=True)
        # Shown while the search holds a whole ID the list doesn't (another matrix's, such as Mobile's, say).
        self.add_button = ttk.Button(bar, style="Small.Secondary.TButton", command=self.add_typed)
        kit.tooltip(self.add_button, f"Keep this ID as typed. The list holds ATT&CK {catalog.VERSION} Enterprise and "
                                     "ICS techniques.")
        self.search_entry.bind("<Return>", lambda event: self.add_button.winfo_manager() and self.add_typed(), add="+")
        self.only = tk.BooleanVar(value=True)
        self.only_box = ttk.Checkbutton(bar, text="Only the tactics ticked above", variable=self.only)
        self.only_box.grid(row=0, column=2, padx=(px(14), 0))
        kit.tooltip(self.only_box, "List only techniques under the tactics ticked above. With none ticked, all are listed.")

        self.frame = tk.Frame(parent, bg=COLORS["card"], bd=0, highlightthickness=1,
                              highlightbackground=COLORS["line"], highlightcolor=COLORS["line"])
        self.frame.pack(fill="x")
        # A StripedTreeview for its '…' on cells too long for their column (with the full text on hover); its
        # stripes give way to paint()'s tags, since alternating tints would fight the sub-technique tint.
        self.tree = tree = kit.StripedTreeview(self.frame, columns=("name", "tactics"), show="tree headings",
                                               height=HEIGHT, selectmode="browse")
        tree.heading("#0", text="ID", anchor="w"); tree.column("#0", width=px(130), minwidth=px(130), stretch=False)
        # Only the name stretches: in a narrow window it gives up width (down to its minimum) so the table fits.
        tree.heading("name", text="Technique", anchor="w")
        tree.column("name", width=px(220), minwidth=px(120), stretch=True)
        tree.heading("tactics", text="Tactic", anchor="w")
        tree.column("tactics", width=px(190), minwidth=px(100), stretch=False)  # 'Privilege Escalation +2'.
        scrollbar = ttk.Scrollbar(self.frame, orient="vertical", command=tree.yview)
        # Gridded, as kit.table does, so the frame's width (not the columns') decides the table's.
        tree.grid(row=0, column=0, sticky="nsew"); scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=kit.autohide(scrollbar, scrollbar.grid))
        self.frame.columnconfigure(0, weight=1)
        kit._cell_tips(tree)
        tree.tag_configure("picked", font=FONTS["strong"])
        # The sub-techniques of a ticked parent: the ones worth a look.
        tree.tag_configure("suggest", background=COLORS["accent_soft"])
        self.frame.focus_target = tree
        self.frame.error = False  # Set by the builder's outline() while a technique is flagged.
        # The focus ring gives way to a flag's red outline, as kit's text areas do.
        ring = lambda on: self.frame.configure(highlightbackground=COLORS["danger"] if self.frame.error else
                                               COLORS["accent"] if on else COLORS["line"])
        tree.bind("<FocusIn>", lambda event: (ring(True), tree.focus() or self.focus_first()), add="+")
        tree.bind("<FocusOut>", lambda event: ring(False), add="+")
        tree.bind("<Button-1>", self.clicked)
        tree.bind("<Double-Button-1>", lambda event: "break")  # Tk would open the row too: one click, one tick.
        tree.bind("<space>", lambda event: (self.toggle(tree.focus()), "break")[1])
        tree.bind("<<TreeviewOpen>>", lambda event: self.opened.add(tree.focus()))
        tree.bind("<<TreeviewClose>>", lambda event: self.opened.discard(tree.focus()))

        line = ttk.Frame(parent, style="Card.TFrame"); line.pack(fill="x", pady=(px(8), 0))
        self.summary = tk.StringVar()
        kit.wrap_to_parent(ttk.Label(line, textvariable=self.summary, style="Body.TLabel", justify="left",
                                     width=1)).pack(side="left", fill="x", expand=True, anchor="n")
        self.clear_button = ttk.Button(line, text="Clear", style="Small.Ghost.TButton", command=self.clear)
        self.clear_button.pack(side="right", anchor="n", padx=(px(8), 0))
        self.summary_line = line
        # IDs the catalog doesn't offer, packed under the summary when there are any.
        self.unknown = ttk.Frame(parent, style="Card.TFrame")
        self.unknown_text = tk.StringVar()
        kit.wrap_to_parent(ttk.Label(self.unknown, textvariable=self.unknown_text, style="Hint.TLabel", justify="left",
                                     width=1)).pack(side="left", fill="x", expand=True, anchor="n")
        self.remove_button = ttk.Button(self.unknown, text="Remove", style="Small.Ghost.TButton", command=self.remove)
        kit.tooltip(self.remove_button, "Drop the IDs the list doesn't hold (the retired and unknown ones)")
        self.replace_button = ttk.Button(self.unknown, text="Use the new IDs", style="Small.Ghost.TButton",
                                         command=self.replace)
        kit.tooltip(self.replace_button, "Swap each retired ID for the technique ATT&CK replaced it with")
        self.fix_button = ttk.Button(self.unknown, text="Fix these", style="Small.Ghost.TButton", command=self.fix)
        kit.tooltip(self.fix_button, "File each ID in its own field and tick the parent of each sub-technique")
        self.spacer = ttk.Frame(parent, style="Card.TFrame", height=px(14)); self.spacer.pack(fill="x")
        inputs += [self.search_entry, self.add_button, self.only_box, self.clear_button, self.remove_button,
                   self.replace_button, self.fix_button]

        self.render_soon = kit.debounce(tree, 150, self.render)
        self.search.trace_add("write", self.render_soon)
        self.only.trace_add("write", lambda *args: self.render())
        tactics.trace_add("write", lambda *args: self.only.get() and self.render_soon())
        for var in self.vars: var.trace_add("write", self.read)
        self.read()

    # ------------------------------------------------------------ the list
    def render(self, *args):
        """List the techniques the search and the tactic filter leave, ticked ones always; a parent is open
        while it or one of its sub-techniques is ticked, when opened by hand, or when shown only for
        sub-techniques that match the search."""
        tree, query = self.tree, self.search.get().strip().casefold()
        chosen = {x for x in entries(self.tactics.get()) if x in TACTICS} if self.only.get() else set()
        focus = tree.focus()
        tree.delete(*tree.get_children())
        self.shown = 0
        for parent, (name, tactics, subs) in PARENTS.items():
            picked = parent in self.ticked or any(sub in self.ticked for sub in subs)
            if chosen and not picked and not chosen.intersection(tactics) \
                    and not any(chosen.intersection(SUBS[sub][1]) for sub in subs):
                continue
            hit = not query or query in (parent + " " + name_of(parent)).casefold()  # 'ics' finds the ICS ones.
            matching = [sub for sub in subs if hit or query in (sub + " " + SUBS[sub][0]).casefold()]
            if not hit and not matching: continue
            opened = picked or parent in self.opened or bool(query and not hit)
            tree.insert("", "end", iid=parent, text=" " + parent, values=(name_of(parent), tactic_text(tactics, chosen)),
                        open=opened)
            for sub in matching:
                tree.insert(parent, "end", iid=sub, text=" " + sub,
                            values=(SUBS[sub][0], tactic_text(SUBS[sub][1], chosen)))
            self.shown += 1
        self.mark()
        if focus and tree.exists(focus): tree.focus(focus)
        typed = self.typed()
        if typed:
            self.add_button.configure(text="Add " + typed)
            if not self.add_button.winfo_manager(): self.add_button.grid(row=0, column=1, padx=(px(8), 0))
        else: self.add_button.grid_remove()
        self.count_full = self.count_text(chosen, typed); self.fit_count()

    def typed(self):
        """The whole technique ID in the search, upper-cased, when the list doesn't hold it and no field lists it."""
        query = self.search.get().strip()
        if not TYPED_ID.fullmatch(query): return None
        technique = query.upper()
        listed = entries(self.vars[0].get()) + entries(self.vars[1].get())
        return None if technique in KNOWN or technique in listed else technique

    def fit_count(self):
        """The count beside Techniques, shortened with '…' when the column is narrow, as field_label does."""
        room = self.head.winfo_width() - self.head.label.winfo_reqwidth() - px(12)
        text = self.count_full if self.head.winfo_width() <= 1 else \
            kit.ellipsize(self.count_full, kit.font(self.head, "small"), room) if room > px(30) else ""
        if self.count.cget("text") != text: self.count.configure(text=text)

    def count_text(self, chosen, typed=None):
        total = len(PARENTS)
        if typed in catalog.RETIRED:
            name, replacement = catalog.RETIRED[typed]
            return f"{typed} {name} is retired from ATT&CK" + (f": search {replacement} instead" if replacement else "")
        if typed: return f"{typed} isn't in the ATT&CK {catalog.VERSION} Enterprise or ICS list: Add keeps it as typed"
        if not self.shown: return "No techniques match for the ticked tactics" if chosen else "No techniques match"
        where = " for the ticked tactics" if chosen and self.shown < total else ""
        return (f"{self.shown} of {total} techniques{where}" if self.shown < total else f"{total} techniques") + \
            f" · ATT&CK {catalog.VERSION}, as Sentinel uses"

    def mark(self):
        """Each row's box and tint, from the ticks."""
        tree = self.tree
        for parent in tree.get_children():
            self.paint(parent, None)
            for sub in tree.get_children(parent): self.paint(sub, parent)

    def paint(self, item, parent):
        on = item in self.ticked
        self.tree.item(item, image=kit.checkbox("on" if on else "off", self.tree),
                       tags=("picked",) if on else ("suggest",) if parent in self.ticked else ())

    def focus_first(self):
        first = next(iter(self.tree.get_children()), "")
        if first: self.tree.focus(first); self.tree.selection_set(first)

    def clicked(self, event):
        """A click on a row ticks or unticks it; the arrow beside a parent only opens or closes it. (Tk keeps
        the arrow's place on rows without one, so a click there ticks as the rest of the row does.)"""
        tree = self.tree
        if tree.identify_region(event.x, event.y) not in ("tree", "cell"): return None
        item = tree.identify_row(event.y)
        if "indicator" in tree.identify_element(event.x, event.y) and tree.get_children(item): return None
        self.toggle(item)
        return None

    def toggle(self, item):
        if not item or self.busy(): return
        ticked, parent, drop = set(self.ticked), SUBS[item][2] if item in SUBS else None, set()
        if item in ticked:
            ticked.discard(item)
            if parent is None:  # A sub-technique needs its parent: they go too, listed or only written.
                ticked.difference_update(PARENTS[item][2])
                drop = {x for x in entries(self.vars[1].get()) + entries(self.vars[0].get()) if x.startswith(item + ".")}
        else:
            ticked.add(item)
            if parent is not None: ticked.add(parent)
            else:
                self.opened.add(item)
                if self.tree.exists(item): self.tree.item(item, open=True)
        self.store(ticked, drop)

    # ------------------------------------------------------------ the fields
    def store(self, ticked, drop=()):
        """Write ``ticked`` to the two fields: their other values stay where they are (but those in ``drop``),
        ticks already listed keep their place (moved to the right field if they were in the wrong one) and new
        ones follow in ID order."""
        techniques, subs = (entries(var.get()) for var in self.vars)
        listed = techniques + subs
        fields = []
        for old, other, dotted in ((techniques, subs, False), (subs, techniques, True)):
            belongs = lambda x: x in ticked and ("." in x) == dotted
            kept = [x for x in old if (x not in KNOWN and x not in drop) or belongs(x)] + [x for x in other if belongs(x)]
            kept += sorted(x for x in ticked if ("." in x) == dotted and x not in listed)
            fields.append(", ".join(dict.fromkeys(kept)))
        self.storing = True
        try:
            for var, value in zip(self.vars, fields):
                if var.get() != value: var.set(value)
        finally: self.storing = False
        self.sync(render=False)  # The list stays as it is, scrolled where it was: only the boxes change.

    def read(self, *args):
        """A field changed from elsewhere (a draft opened, Remove): tick what they list and list again."""
        if not self.storing: self.sync(render=True)

    def sync(self, render):
        techniques, subs = entries(self.vars[0].get()), entries(self.vars[1].get())
        listed = list(dict.fromkeys(techniques + subs))
        self.ticked = {x for x in listed if x in KNOWN}
        self.describe(techniques, subs)
        if render: self.render()
        else: self.mark()

    def misplaced(self, techniques, subs):
        """(IDs in the other field's place, listed parents a sub-technique lacks) that Fix these can put right."""
        wrong = [x for x in techniques if x in SUBS] + [x for x in subs if x in PARENTS]
        parents = {x for x in techniques + subs if "." not in x}
        lacking = sorted({x.split(".")[0] for x in subs + techniques if "." in x and TYPED_ID.fullmatch(x)
                          and x.split(".")[0] in PARENTS and x.split(".")[0] not in parents})
        return list(dict.fromkeys(wrong)), lacking

    def describe(self, techniques, subs):
        listed = list(dict.fromkeys(techniques + subs))
        chosen = sorted(x for x in listed if x in KNOWN)
        self.summary.set("Selected: " + ", ".join(f"{x} {name_of(x)}" for x in chosen) if chosen else
                         "None selected. Tick each technique the rule detects; a sub-technique ticks its parent too.")
        self.clear_button.configure(state="normal" if chosen else "disabled")
        retired = [x for x in listed if x in catalog.RETIRED]
        unknown = [x for x in listed if x not in KNOWN and x not in catalog.RETIRED]
        wrong, lacking = self.misplaced(techniques, subs)
        parts = []
        if retired:
            parts.append("Retired from ATT&CK: " + ", ".join(
                f"{x} {catalog.RETIRED[x][0]}" + (f" (now {catalog.RETIRED[x][1]})" if catalog.RETIRED[x][1] else "")
                for x in retired))
        if unknown:
            parts.append(f"Not in ATT&CK {catalog.VERSION} (Enterprise or ICS), kept as written: " + ", ".join(unknown))
        if wrong:
            parts.append("In the wrong field: " + ", ".join(
                f"{x} (a {'sub-technique' if '.' in x else 'technique'}, listed as a "
                f"{'technique' if '.' in x else 'sub-technique'})" for x in wrong))
        if lacking: parts.append("Parent not ticked: " + ", ".join(f"{x} {name_of(x)}" for x in lacking))
        self.unknown_text.set(". ".join(parts) + "." if parts else "")
        # Each button only while it has something to do, right to left: Remove, Use the new IDs, Fix these.
        shows = ((self.remove_button, bool(retired or unknown)),
                 (self.replace_button, any(catalog.RETIRED[x][1] for x in retired)),
                 (self.fix_button, bool(wrong or lacking)))
        for button, show in shows: button.pack_forget()
        for button, show in shows:
            if show: button.pack(side="right", anchor="n", padx=(px(8), 0))
        if parts and not self.unknown.winfo_manager():
            self.unknown.pack(fill="x", after=self.summary_line, pady=(px(6), 0))
        elif not parts: self.unknown.pack_forget()

    def fix(self):
        """File each ID in its own field and tick each listed sub-technique's parent (store() refiles the ticks)."""
        if self.busy(): return
        wrong, lacking = self.misplaced(entries(self.vars[0].get()), entries(self.vars[1].get()))
        self.store(self.ticked | set(lacking))
        if lacking: self.render()  # Their parents open.

    def add_typed(self):
        """Keep the whole ID typed into the search, in its field, with its parent when the list holds that."""
        technique = self.typed()
        if technique is None or self.busy(): return
        var = self.vars[1 if "." in technique else 0]
        var.set(", ".join(entries(var.get()) + [technique]))
        parent = technique.split(".")[0]
        if "." in technique and parent in PARENTS and parent not in entries(self.vars[0].get()):
            self.store(self.ticked | {parent})
        self.search.set("")

    def clear(self):
        if not self.busy(): self.store(set())

    def remove(self):
        """Drop every value the catalog doesn't offer."""
        if self.busy(): return
        for var in self.vars:
            var.set(", ".join(x for x in entries(var.get()) if x in KNOWN))

    def replace(self):
        """Swap each retired ID that ATT&CK replaced for its replacement (and that one's parent)."""
        if self.busy(): return
        replacements = {x: catalog.RETIRED[x][1] for x in entries(self.vars[0].get()) + entries(self.vars[1].get())
                        if x in catalog.RETIRED and catalog.RETIRED[x][1]}
        ticked = set(self.ticked)
        for new in replacements.values():
            ticked.add(new)
            if new in SUBS: ticked.add(SUBS[new][2])
        self.storing = True
        try:
            for var in self.vars: var.set(", ".join(x for x in entries(var.get()) if x not in replacements))
        finally: self.storing = False
        self.store(ticked)
        self.render()  # The new IDs may be under parents the list didn't show.
