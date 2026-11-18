"""The All clients window: one row per client with its drift counts, from fleet_drift.fleet."""
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from . import ui_kit as kit
from .desktop_theme import COLORS, px
from .fleet_drift import OK, attention, write_csv

COLUMNS = (("target", "Client", 250, True), ("missing", "Missing", 80, False), ("different", "Different", 80, False),
           ("not_assigned", "Not assigned here", 120, False), ("not_in_library", "Not in library", 105, False),
           ("in_sync", "In step", 70, False), ("status", "Status", 220, True))


def summary_text(rows):
    read = [row for row in rows if row["status"] == OK]
    drifted = [row for row in read if row["missing"] or row["different"]]
    failed = len(rows) - len(read)
    return (f"{len(read)} of {len(rows)} clients compared  ·  {len(drifted)} with missing or different rules"
            + (f"  ·  {failed} couldn't be read" if failed else ""))


class FleetDialog:
    def __init__(self, app, rows):
        self.app, self.rows = app, sorted(rows, key=attention)
        self.window = tk.Toplevel(app.root)
        self.window.title("All clients"); self.window.configure(bg=COLORS["card"])
        body = ttk.Frame(self.window, style="Card.TFrame", padding=(kit.space("xl"), px(20), kit.space("xl"), px(18)))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Drift across all clients", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text=summary_text(self.rows), style="CardSubtitle.TLabel").pack(anchor="w", pady=(px(2), px(12)))
        self.actions, buttons = kit.action_row(body, primary=("Open in Drift", self.open_selected),
                                               secondary=(("Save CSV…", self.save),), close=("Close", self.close))
        self.actions.pack(side="bottom", fill="x", pady=(px(14), 0))
        self.open_button, self.save_button, self.close_button = buttons[:3]
        ttk.Label(body, text="Counts compare each client's baseline rules with its workspace, as Drift does. Open a client "
                             "to see the rules behind them.", style="Hint.TLabel").pack(side="bottom", anchor="w", pady=(px(8), 0))
        frame, self.tree, self.empty = kit.table(body, COLUMNS, height=14, sort=True)
        frame.pack(fill="both", expand=True)
        for index, row in enumerate(self.rows):
            values = tuple(row[key] for key, *_ in COLUMNS)
            busy = row["status"] == OK and (row["missing"] or row["different"])
            self.tree.insert("", "end", iid=str(index), values=values,
                             tags=("muted",) if row["status"] != OK else (("attention",) if busy else ()))
        self.tree.tag_configure("attention", foreground=COLORS["danger"])
        if self.rows: self.empty.hide()
        else: self.empty.show("No enabled clients in the library.")
        self.tree.bind("<Double-1>", lambda event: self.open_selected())
        self.tree.bind("<Return>", lambda event: self.open_selected())
        kit.place_dialog(self.window, app.root, 980, 680, 760, 480)
        kit.modal(self.window, app.root, self.close, primary=self.open_button, focus=self.tree)

    def selected_row(self):
        selection = self.tree.selection()
        return self.rows[int(selection[0])] if selection else None

    def open_selected(self):
        row = self.selected_row()
        if row is None: self.app.flash("Select a client first.", "info", 3); return
        self.close()
        self.app.drift_page.open_client(row["target"])

    def save(self):
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        path = filedialog.asksaveasfilename(parent=self.window, title="Save all-clients drift",
                                            initialdir=str(Path.home() / "Downloads"),
                                            initialfile=f"all-clients-drift-{stamp}.csv",
                                            defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
        if not path: return
        write_csv(path, self.rows)
        self.app.flash("Saved " + Path(path).name + ".")

    def close(self):
        try: self.window.grab_release()
        except tk.TclError: pass
        self.window.destroy()
