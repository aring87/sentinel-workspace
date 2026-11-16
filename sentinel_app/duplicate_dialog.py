"""Find duplicate rules across clients: start the rule library's duplicate workflow on GitHub, follow the
run, show its report here and save its CSV and colour-coded Excel reports.

The workflow does the work with the pipeline's identity, so it reads every selected client's workspace
without the analyst signing in to each one. Scan, Audit and Preview disabling only read. Disable
duplicates changes workspaces through GitHub, in the library's production environment, which asks for
approval before the run goes ahead."""
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

from . import ui_kit as kit
from .assignment_dialog import ClientPicker, reveal
from .desktop_theme import COLORS, px
from . import brand, duplicate_report, org_logo
from . import duplicate_workflow as dupes
from .duplicate_workflow import DuplicateService, WORKFLOW

# Two groups of runs: from the scan of each workspace (no library rules needed), and from chosen library rules.
SCAN_GROUP = (("scan", "Scan workspaces"), ("cleanup-preview", "Preview cleanup"), ("cleanup-apply", "Clean up duplicates"))
LIBRARY_GROUP = (("audit", "Audit library rules"), ("disable-preview", "Preview disabling"),
                 ("disable-apply", "Disable library copies"))
MODES = SCAN_GROUP + LIBRARY_GROUP
MODE_LABELS = dict(MODES)
MODE_HINTS = {
    "scan": "Read-only. Groups the rules in each workspace that look like copies of each other (same name, same "
            "template or a similar query), whether or not the library manages them.",
    "cleanup-preview": "Read-only. In each workspace, for every group of two or more enabled look-alike rules, lists "
                       "the one it would keep (the library rule if the group has one, otherwise the newest) and the "
                       "others it would disable.",
    "cleanup-apply": "Keeps one rule enabled in each duplicate group and disables (never deletes) the rest, through "
                     "GitHub. GitHub asks for approval of the production environment before the run changes anything.",
    "audit": "Read-only. Lists the workspace rules that copy the library rules you choose, and what Disable "
             "library copies would do with each one.",
    "disable-preview": "Read-only. A dry run of Disable library copies for the rules you choose: it lists each copy "
                       "it would disable, and why it would leave the others.",
    "disable-apply": "Disables (never deletes) the copies of the library rules you choose, keeping the library "
                     "rule, through GitHub. GitHub asks for approval of the production environment first.",
}
SIMILARITIES = ("80%", "85%", "90%", "95%", "100%")
POLL_MS = 15000
# Results columns, renamed for a scan (see headings()).
COLUMNS = (("target", "Client target", 130, False), ("rule", "Workspace rule", 220, True), ("enabled", "Enabled", 70, False),
           ("match", "Match", 150, False), ("action", "Action", 230, True), ("library", "Library rule", 150, True))
SCAN_HEADINGS = dict(rule="Rule", match="Why grouped", action="Group", library="Library rule file")
CLEANUP_HEADINGS = dict(rule="Rule", match="Why grouped", action="Action", library="Library rule file")


def downloads():
    folder = Path.home() / "Downloads"
    return folder if folder.is_dir() else Path.home()


def short_path(path):
    return str(path or "").rsplit("/", 1)[-1] or "—"


def row_values(kind, row):
    """The results table's cells for one report row."""
    enabled = "Yes" if dupes.truthy(row.get("dup_enabled" if kind == "duplicates" else "enabled")) else "No"
    if kind == "cleanup":
        return (row["target"], row["name"], enabled, row["group_match"],
                f"Group {row['group']} · {dupes.action_text(row['action'])}", short_path(row["repo_managed"]))
    if kind == "scan":
        return (row["target"], row["name"], enabled, row["group_match"],
                f"Group {row['group']} · {row['enabled_in_group']} enabled", short_path(row["repo_managed"]))
    match = row["match"].replace("+", " + ")
    if row.get("similarity"):
        try: match += f" ({float(row['similarity']):.0%})"
        except ValueError: pass
    return (row["target"], row["dup_name"], enabled, match, dupes.action_text(row["action"]), short_path(row["repo_rule"]))


class DuplicateDialog(ClientPicker):
    def __init__(self, app, snapshot, rules=(), preselect=None):
        """``rules`` are the library rules selected in the Rule library (the ones Audit and the disable
        modes work on); ``preselect`` the client targets to tick, or None for every enabled client."""
        self.app, self.snapshot = app, snapshot
        self.rules = [rule for rule in rules if rule.get("path")]
        self.service = DuplicateService()
        self.folder = app.rule_builder.data_dir / "duplicate-requests"
        self.record, self.locked, self.reports, self.poll_timer = None, False, None, None
        self.eligible = {c["target"] for c in snapshot["clients"] if c["raw"].get("enabled") is True}
        self.selected = set(self.eligible if preselect is None else set(preselect) & self.eligible)
        self.visible = {}
        self.window = window = tk.Toplevel(app.root)
        window.title("Find duplicate rules"); window.configure(bg=COLORS["card"])
        body = ttk.Frame(window, style="Card.TFrame", padding=(kit.space("xl"), px(20), kit.space("xl"), px(18)))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Find duplicate rules", style="CardTitle.TLabel").pack(anchor="w")
        kit.wrap_to_parent(ttk.Label(body, text="Runs the rule library's duplicate workflow on GitHub. It reads each "
                                     "client's workspace with the pipeline's identity; the reports come back here.",
                                     style="CardSubtitle.TLabel", justify="left", width=1), minimum=px(200)).pack(
            fill="x", pady=(px(2), px(14)))
        # Bottom-anchored parts first, so a short screen squeezes the results rather than the buttons.
        self.actions, buttons = kit.action_row(
            body, primary=("Save reports…", self.save_reports),
            secondary=(("Start run", self.start), ("Check", self.prepare)), close=("Close", self.close),
            aux=(("Refresh status", self.refresh_status), ("Open in GitHub", self.open_run), ("Show last run", self.last_run)))
        self.actions.pack(side="bottom", fill="x", pady=(px(14), 0))
        (self.save_button, self.start_button, self.prepare_button, self.close_button,
         self.status_button, self.open_button, self.last_button) = buttons
        kit.tooltip(self.save_button, "Save the run's CSV and colour-coded Excel reports, and a Word report of the "
                                      "findings, to a folder")
        kit.tooltip(self.last_button, "Open the last run started from this PC, and its reports")
        self.notice = tk.StringVar()
        self.banner = kit.Banner(body, self.notice); self.banner.pack(side="bottom", fill="x", pady=(px(10), 0))

        self.mode = tk.StringVar(value="audit" if self.rules else "scan")
        # Two switches on one choice: runs from the scan of each workspace, and runs from chosen library rules.
        modes = ttk.Frame(body, style="Card.TFrame"); modes.pack(anchor="w", fill="x")
        self.mode_bars = []
        for column, (title, group) in enumerate((("From the scan", SCAN_GROUP), ("From library rules", LIBRARY_GROUP))):
            ttk.Label(modes, text=title, style="Hint.TLabel").grid(row=0, column=column, sticky="w",
                                                                   padx=(0 if column == 0 else px(16), 0))
            bar = kit.Segmented(modes, self.mode, group, enabled=lambda: not self.locked and not self.app.busy)
            bar.grid(row=1, column=column, sticky="w", padx=(0 if column == 0 else px(16), 0), pady=(px(2), 0))
            self.mode_bars.append(bar)
        self.mode_bar = self.mode_bars[0]
        self.hint = tk.StringVar()
        self.hint_label = kit.wrap_to_parent(ttk.Label(body, textvariable=self.hint, style="Hint.TLabel", justify="left",
                                                       width=1), minimum=px(200))
        self.hint_label.pack(fill="x", pady=(px(6), px(12)))
        # The choices fold away while results are shown, so the results have the room; New check brings them back.
        self.form = form = ttk.Frame(body, style="Card.TFrame"); form.pack(fill="x")

        bar = ttk.Frame(form, style="Card.TFrame"); bar.pack(fill="x", pady=(0, px(8)))
        ttk.Label(bar, text="Clients", style="Section.TLabel").pack(side="left")
        self.count = tk.StringVar(); ttk.Label(bar, textvariable=self.count, style="Hint.TLabel").pack(side="left", padx=(px(10), 0))
        self.clear_button = ttk.Button(bar, text="Clear", command=self.clear, style="Small.Secondary.TButton")
        self.all_button = ttk.Button(bar, text="Select all enabled", command=self.select_all, style="Small.Secondary.TButton")
        self.clear_button.pack(side="right"); self.all_button.pack(side="right", padx=(0, px(8)))
        self.search = tk.StringVar()
        self.search_box = ttk.Entry(bar, textvariable=self.search, width=28)
        self.search_box.pack(side="right", padx=(0, px(8)))
        kit.placeholder(self.search_box, self.search, "Search clients", search=True)
        frame, self.clients, self.empty = kit.table(form, (("client", "Client", 170, True), ("workspace", "Workspace", 170, True),
                                                           ("target", "Target", 220, True), ("reason", "Availability", 130, False)),
                                                    height=max(3, min(5, len(snapshot["clients"]))), check=True)
        frame.pack(fill="x")
        self.pickable()

        options = ttk.Frame(form, style="Card.TFrame"); options.pack(fill="x", pady=(px(12), 0))
        options.columnconfigure(1, weight=1)
        self.rules_text = tk.StringVar()
        ttk.Label(options, text="Rules", style="Field.TLabel").grid(row=0, column=0, sticky="nw", padx=(0, px(12)))
        kit.wrap_to_parent(ttk.Label(options, textvariable=self.rules_text, style="Body.TLabel", justify="left", width=1),
                           minimum=px(200)).grid(row=0, column=1, columnspan=2, sticky="ew")
        # The rules can be chosen here too, so the disable modes don't need a trip back to the Rule library.
        self.rule_buttons = ttk.Frame(options, style="Card.TFrame")
        self.rule_buttons.grid(row=0, column=3, sticky="ne", padx=(px(12), 0))
        self.all_rules_button = ttk.Button(self.rule_buttons, text="All library rules", command=self.all_rules,
                                           style="Small.Secondary.TButton")
        self.choose_button = ttk.Button(self.rule_buttons, text="Choose rules…", command=self.choose_rules,
                                        style="Small.Secondary.TButton")
        self.all_rules_button.pack(side="left"); self.choose_button.pack(side="left", padx=(px(6), 0))
        kit.tooltip(self.all_rules_button, "Check every rule in the library")
        kit.tooltip(self.choose_button, "Tick the library rules to check")
        self.similarity = tk.StringVar(value="90%")
        ttk.Label(options, text="Query similarity", style="Field.TLabel").grid(row=1, column=0, sticky="w",
                                                                              padx=(0, px(12)), pady=(px(8), 0))
        self.similarity_box = ttk.Combobox(options, textvariable=self.similarity, values=SIMILARITIES, state="readonly", width=6)
        self.similarity_box.grid(row=1, column=1, sticky="w", pady=(px(8), 0))
        kit.tooltip(self.similarity_box, "How alike two queries must be to count as copies (comments and spacing ignored)")
        self.include_similar = tk.BooleanVar(value=False)
        self.similar_check = ttk.Checkbutton(options, text="Also disable rules matched only by a similar query",
                                             variable=self.include_similar)
        self.similar_check.grid(row=1, column=2, columnspan=2, sticky="w", padx=(px(16), 0), pady=(px(8), 0))
        self.names_label = ttk.Label(options, text="Other names", style="Field.TLabel")
        self.names_label.grid(row=2, column=0, sticky="w", padx=(0, px(12)), pady=(px(8), 0))
        self.names = tk.StringVar()
        self.names_box = ttk.Entry(options, textvariable=self.names)
        self.names_box.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(px(8), 0))
        kit.placeholder(self.names_box, self.names, "Older names of these rules to treat as copies, separated by semicolons")

        # The check's review until there are results; then the results take its place.
        self.review_frame, self.review = kit.text_area(body, 6, readonly=True, placeholder=(
            "Select Check to see exactly what the run will do before anything starts in GitHub."))
        self.review_frame.pack(fill="both", expand=True, pady=(px(12), 0))
        self.results = ttk.Frame(body, style="Card.TFrame")
        head = ttk.Frame(self.results, style="Card.TFrame"); head.pack(fill="x", pady=(0, px(6)))
        ttk.Label(head, text="Results", style="Section.TLabel").pack(side="left")
        self.new_button = ttk.Button(head, text="New check", command=self.new_check, style="Small.Secondary.TButton")
        self.new_button.pack(side="right")
        kit.tooltip(self.new_button, "Show the choices again to check another run. These results stay saveable.")
        self.result_text = tk.StringVar()
        kit.wrap_to_parent(ttk.Label(self.results, textvariable=self.result_text, style="Body.TLabel", justify="left",
                                     width=1), minimum=px(200)).pack(fill="x", pady=(0, px(8)))
        table_frame, self.table, self.table_empty = kit.table(self.results, COLUMNS, height=10, sort=True, marker=True)
        table_frame.pack(fill="both", expand=True)
        self.results_kind = None
        # Packed bottom-first above; Tab should still read top to bottom.
        self.banner.lift(); self.actions.lift()

        self.filter = kit.debounce(window, 150, self.render_clients)
        for key in ("<Down>", "<Return>"): self.search_box.bind(key, lambda event: self.filter.flush(), add="+")
        kit.link_search(self.search_box, self.clients)
        self.search.trace_add("write", self.filter)
        self.mode.trace_add("write", lambda *args: self.mode_changed())
        for variable in (self.similarity, self.include_similar, self.names):
            variable.trace_add("write", lambda *args: self.invalidate())
        kit.place_dialog(window, app.root, 1040, 820, 860, 700)
        kit.modal(window, app.root, self.close, primary=self.prepare_button, focus=self.search_box)
        self.render_clients(); reveal(self.clients, self.visible, self.selected)
        self.mode_changed(initial=True)

    # ------------------------------------------------------------- picker
    def say(self, text, tone="info", action=None):
        self.notice.set(text); self.banner.tone(tone); self.banner.set_action(*(action or (None, None)))

    def render_clients(self):
        self.filter.cancel()
        self.clients.delete(*self.clients.get_children()); self.visible = {}
        term = self.search.get().strip().casefold()
        for i, client in enumerate(self.snapshot["clients"]):
            if term and term not in " ".join((client["name"], client["workspace"], client["target"])).casefold(): continue
            target = client["target"]; self.visible[str(i)] = target
            self.clients.insert("", "end", iid=str(i), image=self.box(target),
                                values=(client["name"], client["workspace"], target,
                                        "Ready" if target in self.eligible else "Switched off"),
                                tags=() if target in self.eligible else ("muted",))
        if self.visible: self.empty.hide()
        elif self.snapshot["clients"]: self.empty.show("No clients match your search.", ("Clear search", lambda: self.search.set("")))
        else: self.empty.show("The rule library has no client targets yet.")
        self.paint()

    def selectable(self, target):
        return target in self.eligible

    def count_text(self):
        return f"{len(self.selected)} of {len(self.eligible)} enabled selected"

    def select_all(self):
        if self.can_pick(): self.selected = set(self.eligible); self.changed(self.visible)

    def clear(self):
        if self.can_pick(): self.selected.clear(); self.changed(self.visible)

    def invalidate(self):
        """The choices changed: the last check no longer stands (results already shown stay, to save)."""
        if self.locked: return
        if self.record is not None:
            self.record = None
            kit.set_text(self.review, "")
            self.say("Your choices changed. Select Check again before starting a run.")
        self.controls()

    # ------------------------------------------------------------- choices
    def mode_changed(self, initial=False):
        mode = self.mode.get()
        self.hint.set(MODE_HINTS[mode])
        if mode in dupes.SCAN_MODES:
            self.rules_text.set("Not used: this works from the scan of each workspace, with no library rules.")
        elif self.rules:
            total = len(self.snapshot["rules"])
            if len(self.rules) == total:
                self.rules_text.set(f"All {total} library rules.")
            else:
                names = ", ".join(rule["name"] for rule in self.rules[:6]) + \
                    (f" and {len(self.rules) - 6} more" if len(self.rules) > 6 else "")
                self.rules_text.set(f"{len(self.rules)} of {total} library rules: {names}.")
        elif mode == "audit":
            self.rules_text.set("Every library rule assigned to each client. To check only some, choose them.")
        else:
            self.rules_text.set("None chosen yet. Choose the library rules whose copies to disable, or use them all.")
        if mode in dupes.SCAN_MODES: self.rule_buttons.grid_remove()
        else: self.rule_buttons.grid()
        disabling = mode.startswith("disable")
        for widget in (self.similar_check,):
            if disabling: widget.grid()
            else: widget.grid_remove()
        for widget in (self.names_label, self.names_box):
            if mode in dupes.SCAN_MODES: widget.grid_remove()
            else: widget.grid()
        if not initial and self.results.winfo_manager() and not self.locked: self.show_review()
        if initial:
            self.say("Choose what to run and which clients, then select Check. Nothing starts in GitHub until you "
                     "select Start run.")
            self.controls()
        else:
            self.invalidate()

    def set_rules(self, rules):
        """Check these library rules (rule rows from the snapshot), in library order."""
        if self.locked or self.app.busy: return
        order = {rule["path"]: index for index, rule in enumerate(self.snapshot["rules"])}
        self.rules = sorted((rule for rule in rules if rule.get("path") in order), key=lambda rule: order[rule["path"]])
        self.mode_changed()

    def all_rules(self):
        self.set_rules(list(self.snapshot["rules"]))

    def choose_rules(self):
        if self.locked or self.app.busy: return
        self.chooser = RuleChooser(self, self.snapshot["rules"], {rule["path"] for rule in self.rules}, self.set_rules)

    def choices(self):
        mode = self.mode.get()
        return dict(mode=mode, targets=sorted(self.selected), rules=[rule["path"] for rule in self.rules],
                    extra_names=self.names.get(), include_similar=bool(self.include_similar.get()),
                    similarity=int(self.similarity.get().rstrip("%")) / 100)

    def controls(self):
        if not self.window.winfo_exists(): return
        busy = self.app.busy
        editable = not self.locked and not busy
        for widget in (self.search_box, self.all_button, self.clear_button, self.names_box, self.similar_check,
                       self.all_rules_button, self.choose_button):
            widget.configure(state="normal" if editable else "disabled")
        self.similarity_box.configure(state="readonly" if editable else "disabled")
        for bar in self.mode_bars: bar.render()  # They only redraw on a mode change; locking and unlocking don't.
        self.prepare_button.configure(state="normal" if editable else "disabled",
                                      text="Check again" if self.record and not self.record["attempted"] else "Check")
        startable = bool(self.record and not self.record["attempted"] and not self.locked and not busy)
        run = (self.record or {}).get("result") or {}
        self.start_button.configure(state="normal" if startable else "disabled")
        self.status_button.configure(state="normal" if run.get("run_id") and not busy else "disabled")
        self.save_button.configure(state="normal" if self.reports and not busy else "disabled")
        self.last_button.configure(state="normal" if not self.locked and not busy else "disabled")
        self.new_button.configure(state="normal" if not self.locked and not busy else "disabled")
        self.open_button.configure(state="normal"); self.close_button.configure(state="normal")
        lead = (self.save_button if self.reports and not self.locked else self.start_button if startable else
                None if self.locked else self.prepare_button)
        kit.promote(self.window, lead, *(b for b in (self.save_button, self.start_button, self.prepare_button) if b is not lead))

    def work(self, title, task, done, on_error=None):
        kit.run_in_dialog(self.app, self.window, title, task, done,
                          controls=(self.search_box, self.all_button, self.clear_button, self.prepare_button,
                                    self.all_rules_button, self.choose_button,
                                    self.start_button, self.status_button, self.save_button, self.last_button,
                                    self.names_box, self.similar_check),
                          say=self.say, page="rules", on_error=on_error, after=self.controls, log_as="Duplicate rules:")

    # ------------------------------------------------------------- check and start
    def prepare(self):
        if self.app.busy or self.locked: return
        self.record = None; kit.set_text(self.review, "")
        choice = self.choices()
        def done(record):
            self.record = record
            kit.set_text(self.review, self.review_text(record["plan"]))
            if self.reports: self.show_review()
            self.say("Checked. Read what the run will do above, then select Start run.", "success")
        self.work("Checking the rule library and the duplicate workflow on GitHub…",
                  lambda: self.service.prepare(self.snapshot["repository"], self.snapshot["revision"], choice["mode"],
                                               choice["targets"], choice["rules"], choice["extra_names"],
                                               choice["include_similar"], choice["similarity"]), done)

    def review_text(self, plan):
        inputs = plan["inputs"]
        lines = ["Library: " + plan["repository"], "GitHub account: " + plan["identity"],
                 "Checked against main revision: " + plan["revision"],
                 "Run: " + MODE_LABELS[plan["mode"]] + (" (read-only)" if plan["mode"] not in dupes.APPLY_MODES else
                                                        " (changes workspaces once GitHub's approval is given)"),
                 "Clients: " + (f"every enabled client ({len(plan['targets'])})" if plan["every"] else
                                ", ".join(plan["targets"]))]
        if plan["mode"] not in dupes.SCAN_MODES:
            lines.append("Rules: " + (", ".join(plan["rules"]) if plan["rules"] else "every rule assigned to each client"))
        lines.append("Query similarity: " + f"{float(inputs['similarity']):.0%}")
        if plan["extra_names"]: lines.append("Also treated as copies: " + "; ".join(plan["extra_names"]))
        if plan["mode"].startswith("disable"):
            lines.append("Disables similar-query-only matches: " + ("yes" if inputs["include_similar"] == "true" else "no"))
        lines += ["", "The workflow runs on main with the pipeline's identity. GitHub keeps the reports for 14 days; "
                      "Save reports keeps a copy."]
        return "\n".join(lines)

    def start(self):
        if self.app.busy or self.locked or not self.record or self.record["attempted"]: return
        plan = self.record["plan"]
        if plan["mode"] == "cleanup-apply" and not kit.confirm(
                self.window, "Clean up duplicates",
                f"In {len(plan['targets'])} client workspace{'s' if len(plan['targets']) != 1 else ''}, this run keeps one "
                "rule enabled in each group of look-alike rules (the library rule if the group has one, otherwise the "
                "newest) and disables the rest. It disables, never deletes. Preview cleanup lists exactly which.\n\n"
                "GitHub asks for approval of the production environment before the run goes ahead.",
                ok="Start run", destructive=True):
            return
        if plan["mode"] == "disable-apply" and not kit.confirm(
                self.window, "Disable library copies",
                f"This run disables the copies of {len(plan['rules'])} library rule{'s' if len(plan['rules']) != 1 else ''} "
                f"in {len(plan['targets'])} client workspace{'s' if len(plan['targets']) != 1 else ''}. It disables, never "
                "deletes. Preview disabling lists exactly which copies it would change.\n\nGitHub asks for approval of "
                "the production environment before the run goes ahead.", ok="Start run", destructive=True):
            return
        def done(record):
            self.record, self.locked = record, True
            self.clear_results()
            result = record["result"]
            if result.get("run_id"):
                self.say("Run started in GitHub. This updates every 15 seconds; you can keep working in GitHub meanwhile.",
                         "success")
                self.schedule_poll()
            else:
                self.locked = False
                self.say(result["state"], "warning")
        self.work("Starting the duplicate run in GitHub…", lambda: self.service.dispatch(self.record, self.folder), done)

    # ------------------------------------------------------------- follow the run
    def schedule_poll(self, delay=POLL_MS):
        # On the app's root window, so the timer outlives a closed dialog only until it fires (poll checks).
        self.cancel_poll()
        if self.window.winfo_exists(): self.poll_timer = self.app.root.after(delay, self.poll)

    def cancel_poll(self):
        if self.poll_timer is not None:
            try: self.app.root.after_cancel(self.poll_timer)
            except tk.TclError: pass
            self.poll_timer = None

    def poll(self):
        self.poll_timer = None
        if not self.window.winfo_exists() or not self.record: return
        if self.app.busy: self.schedule_poll(3000); return
        self.refresh_status(automatic=True)

    def refresh_status(self, automatic=False):
        if self.app.busy or not self.record or not (self.record.get("result") or {}).get("run_id"): return
        self.cancel_poll()
        def done(record):
            self.record = record
            result = record["result"]
            if result.get("run_status") == "completed":
                self.fetch_reports()
                return
            tone = "warning" if result.get("run_status") == "waiting" else "info"
            extra = " A reviewer approves it in GitHub (Open in GitHub)." if result.get("run_status") == "waiting" else ""
            self.say(f"Run #{result.get('run_number') or result['run_id']}: {result['state']}.{extra} This updates every "
                     "15 seconds.", tone)
            self.schedule_poll()
        def failed(error):
            self.say(error + " Select Refresh status to try again.", "danger")
        self.work("Reading the run's status from GitHub…", lambda: self.service.refresh(self.record, self.folder), done,
                  on_error=failed)

    def fetch_reports(self):
        record = self.record
        result = record["result"]
        def done(reports):
            self.locked = False
            name, parsed = dupes.main_report(reports["files"])
            self.reports = dict(reports, name=name, run_number=result.get("run_number"), mode=record["plan"]["mode"],
                                **parsed)
            self.show_results()
            ok = result.get("conclusion") == "success"
            self.say(f"Run #{result.get('run_number') or result['run_id']} {'finished' if ok else result['state'].lower()}. "
                     + ("" if ok else "Its reports may be incomplete: Open in GitHub to see why. ")
                     + "Save reports keeps the CSV and the colour-coded Excel report.", "success" if ok else "warning")
        def failed(error):
            self.locked = False
            self.say(f"Run #{result.get('run_number') or result['run_id']}: {result['state']}. " + error,
                     "warning" if result.get("conclusion") != "success" else "danger")
        self.work("Downloading the run's reports from GitHub…", lambda: self.service.reports(record), done, on_error=failed)

    def last_run(self):
        if self.app.busy or self.locked: return
        record = dupes.latest_receipt(self.folder, self.snapshot.get("repository"))
        if record is None:
            self.say("No duplicate run has been started from this PC for this library yet.", "info"); return
        self.mode.set(record["plan"]["mode"])  # Before locking: the hint and options follow the mode.
        self.record, self.locked = record, True
        kit.set_text(self.review, self.review_text(record["plan"]))
        self.clear_results()
        self.refresh_status()

    def clear_results(self):
        self.reports = None
        self.show_review()

    # ------------------------------------------------------------- results
    def new_check(self):
        if self.app.busy or self.locked: return
        self.show_review()
        self.say("Change the choices if you need to, then select Check. Save reports still saves the last run's reports.")
        self.search_box.focus_set()

    def show_review(self):
        if self.results.winfo_manager(): self.results.pack_forget()
        if not self.form.winfo_manager(): self.form.pack(fill="x", after=self.hint_label)
        if not self.review_frame.winfo_manager():
            self.review_frame.pack(fill="both", expand=True, pady=(px(12), 0))

    def show_results(self):
        reports = self.reports
        kind, rows = reports["kind"], reports["rows"]
        self.results_kind = kind
        headings = {key: title for key, title, *_ in COLUMNS}
        if kind == "scan": headings.update(SCAN_HEADINGS)
        elif kind == "cleanup": headings.update(CLEANUP_HEADINGS)
        kit.set_headings(self.table, headings)
        self.table.delete(*self.table.get_children())
        for index, row in enumerate(rows):
            self.table.insert("", "end", iid=str(index), values=row_values(kind, row),
                              image=kit.dot(dupes.tone(kind, row), self.table))
        others = sorted(name for name in reports["files"] if name != reports["name"])
        text = MODE_LABELS[reports["mode"]] + ": " + dupes.summary(kind, rows, reports["mode"])
        text += (" Save reports saves " + ", ".join([reports["name"]] + others) + " and a Word report, "
                 + duplicate_report.file_name(reports) + ".")
        self.result_text.set(text)
        if rows: self.table_empty.hide()
        else: self.table_empty.show("Nothing to list: no duplicates found.")
        if self.review_frame.winfo_manager(): self.review_frame.pack_forget()
        if self.form.winfo_manager(): self.form.pack_forget()
        if not self.results.winfo_manager():
            self.results.pack(fill="both", expand=True, pady=(px(12), 0))
        self.controls()

    def save_reports(self):
        if self.app.busy or not self.reports: return
        folder = filedialog.askdirectory(parent=self.window, title="Save the duplicate reports",
                                         initialdir=str(downloads()), mustexist=True)
        if not folder: return
        files, problem = dict(self.reports["files"]), None
        try:  # The Word report is made here, from the rows shown; the run's own files are saved even if it fails.
            files[duplicate_report.file_name(self.reports)] = duplicate_report.build(
                self.reports, self.record, logo=brand.render_png(96), org_logo=self.organization_logo())
        except Exception as error:
            problem = str(error) or type(error).__name__
            self.app.log("Duplicate rules: couldn't make the Word report: " + problem)
        try:
            saved = dupes.save_reports(files, folder, self.reports.get("run_number"))
        except (OSError, ValueError) as error:
            self.say("Couldn't save the reports: " + str(error), "danger"); return
        names = ", ".join(path.name for path in saved)
        self.app.log("Duplicate rules: saved " + names + " to " + folder)
        opener = getattr(os, "startfile", None)
        word = next((path for path in saved if path.suffix == ".docx"), None)
        if problem:
            self.say(f"Saved {names} to {folder}, but the Word report couldn't be made: {problem}", "warning",
                     ("Open folder", lambda: opener(folder)) if opener else None)
        else:
            self.say(f"Saved {names} to {folder}.", "success",
                     ("Open Word report", lambda: opener(str(word))) if opener and word else None)

    def organization_logo(self):
        """The organization's logo for the Word report (the largest of desktop-data/branding's set), or None."""
        sizes = org_logo.sizes(self.app.rule_builder.data_dir / org_logo.FOLDER)
        if not sizes: return None
        try:
            data = sizes[max(sizes)].read_bytes()
        except OSError:
            return None
        return data if len(data) <= org_logo.MAX_BYTES and data[:8] == bytes.fromhex("89504e470d0a1a0a") else None

    # ------------------------------------------------------------- other
    def open_run(self):
        run = (self.record or {}).get("result") or {}
        url = run.get("url") or ("https://github.com/" + self.snapshot["repository"] + "/actions/workflows/" + WORKFLOW)
        webbrowser.open(url)

    def close(self):
        if self.app.busy:
            self.say("Wait for the current step to finish, then select Close.", "warning"); return
        self.cancel_poll()
        self.window.destroy()


class RuleChooser(ClientPicker):
    """Tick the library rules a duplicate run checks: search by name or file, tick all shown or clear, then
    Use these rules. Nothing changes in the dialog until then."""

    def __init__(self, owner, rules, chosen, done):
        self.owner, self.app, self.rules, self.done = owner, owner.app, list(rules), done
        self.selected, self.visible, self.locked = set(chosen), {}, False
        self.window = window = tk.Toplevel(owner.window)
        window.title("Choose library rules"); window.configure(bg=COLORS["card"])
        body = ttk.Frame(window, style="Card.TFrame", padding=(kit.space("xl"), px(20), kit.space("xl"), px(18)))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Choose library rules", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text="The run looks for copies of the rules you tick in each client's workspace.",
                  style="CardSubtitle.TLabel").pack(anchor="w", pady=(px(2), px(12)))
        actions, buttons = kit.action_row(body, primary=("Use these rules", self.use), close=("Cancel", window.destroy))
        actions.pack(side="bottom", fill="x", pady=(px(14), 0))
        self.use_button = buttons[0]
        bar = ttk.Frame(body, style="Card.TFrame"); bar.pack(fill="x", pady=(0, px(8)))
        self.search = tk.StringVar()
        self.search_box = ttk.Entry(bar, textvariable=self.search)
        self.search_box.pack(side="left", fill="x", expand=True)
        kit.placeholder(self.search_box, self.search, "Search rules or files", search=True)
        ttk.Button(bar, text="Clear", command=self.clear, style="Small.Secondary.TButton").pack(side="right")
        ttk.Button(bar, text="Tick all shown", command=self.toggle_all, style="Small.Secondary.TButton").pack(
            side="right", padx=(px(8), px(8)))
        self.count = tk.StringVar()
        ttk.Label(body, textvariable=self.count, style="Hint.TLabel").pack(anchor="w", pady=(0, px(6)))
        frame, self.clients, self.empty = kit.table(body, (("rule", "Rule", 260, True), ("state", "State", 90, False),
                                                           ("file", "File", 300, True)), height=12, check=True)
        frame.pack(fill="both", expand=True)
        self.pickable()
        actions.lift()
        self.filter = kit.debounce(window, 150, self.render)
        self.search.trace_add("write", self.filter)
        kit.link_search(self.search_box, self.clients)
        kit.place_dialog(window, owner.window, 820, 640, 640, 480)
        kit.modal(window, owner.window, window.destroy, primary=self.use_button, focus=self.search_box)
        self.render()

    def render(self, *args):
        self.filter.cancel()
        self.clients.delete(*self.clients.get_children()); self.visible = {}
        term = self.search.get().strip().casefold()
        for index, rule in enumerate(self.rules):
            if term and term not in (rule["name"] + " " + rule["path"]).casefold(): continue
            self.visible[str(index)] = rule["path"]
            self.clients.insert("", "end", iid=str(index), image=self.box(rule["path"]),
                                values=(rule["name"], rule.get("state", ""), rule["path"]))
        if self.visible: self.empty.hide()
        else: self.empty.show("No rules match your search.", ("Clear search", lambda: self.search.set("")))
        self.paint()

    def count_text(self):
        return f"{len(self.selected)} of {len(self.rules)} rules ticked"

    def invalidate(self):
        pass  # Nothing to undo: the choice only counts once Use these rules is pressed.

    def toggle_all(self):
        if self.visible: self.selected |= set(self.visible.values()); self.paint(self.visible)

    def clear(self):
        self.selected.clear(); self.paint(self.visible)

    def use(self):
        chosen = [rule for rule in self.rules if rule["path"] in self.selected]
        self.window.destroy()
        self.done(chosen)
