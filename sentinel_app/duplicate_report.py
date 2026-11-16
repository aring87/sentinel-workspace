"""The duplicate rule report as a Word document: what a Find duplicates run found, for the people who act on it.

Built from the run's own CSV (the rows the app lists, as parsed by duplicate_workflow) and the saved
request (what was asked for), so it says exactly what the CSV and the Excel report hold, in the same
colours. It reads nothing from GitHub or Azure.

    data = build(reports, record)  # reports: the dialog's dict (files, name, kind, rows, mode, run_number)
"""
from datetime import datetime, timezone
import re

from . import __version__
from . import duplicate_workflow as dupes
from .word_document import Cell, Document, MUTED, NAVY, ACCENT, paragraph_xml, run

# The Excel report's status colours (fill, text), so the two read alike.
STATUS = {"danger": ("F8CBAD", "9C0006"), "warning": ("FFE699", "7F6000"), "success": ("C6EFCE", "006100"),
          "info": ("DDEBF7", "1F4E79"), "neutral": ("EDEDED", "595959")}
ORDER = ("danger", "warning", "info", "success", "neutral")
TITLES = {"scan": "Duplicate rule scan", "audit": "Duplicate rule audit", "disable-preview": "Duplicate rule disable preview",
          "disable-apply": "Duplicate rule disable report", "cleanup-preview": "Duplicate cleanup preview",
          "cleanup-apply": "Duplicate cleanup report"}
MODE_LINES = {"scan": "Scan workspaces (read-only)", "audit": "Audit library rules (read-only)",
              "cleanup-preview": "Preview cleanup (read-only dry run)",
              "cleanup-apply": "Clean up duplicates (changed workspaces after GitHub's approval)",
              "disable-preview": "Preview disabling (read-only dry run)",
              "disable-apply": "Disable library copies (changed workspaces after GitHub's approval)"}
MATCH_WORDS = {"name": "Same name", "template": "Same template", "extra": "Other name", "similar": "Similar query"}
MAX_ROWS = 2000  # Detail rows in the document; the CSV and Excel report always hold every row.


def status_label(kind, row, mode=None):
    """The short status the report's Status column shows."""
    tone = dupes.tone(kind, row)
    if kind == "scan":
        return {"danger": "Duplicate enabled", "success": "Only one enabled", "neutral": "Disabled"}[tone]
    action = str(row.get("action") or "")
    if kind == "cleanup" and action == "keep": return "Keep"
    if tone == "warning": return "Would disable"
    if tone == "success": return "Disabled"
    if tone == "danger": return "Failed"
    if tone == "info": return "Review"
    return "Already disabled" if action == "already disabled" else "Skipped"


def match_text(row):
    words = [MATCH_WORDS.get(part, part) for part in str(row.get("match") or "").split("+") if part]
    text = " + ".join(words)
    if row.get("similarity"):
        try: text += f" ({float(row['similarity']):.0%})"
        except ValueError: pass
    return text


def group_reason(text):
    """The scan's 'name, similar query 0.94-0.99' as 'Same name, similar query (94–99%)'."""
    parts = []
    for part in [p.strip() for p in str(text or "").split(",") if p.strip()]:
        found = re.fullmatch(r"similar query ([0-9.]+)(?:-([0-9.]+))?", part)
        if found:
            low, high = (float(v) for v in (found.group(1), found.group(2) or found.group(1)))
            span = f"{low:.0%}" if low == high else f"{low * 100:.0f}–{high:.0%}"
            parts.append(f"similar query ({span})")
        else:
            parts.append({"name": "same name", "template": "same template"}.get(part, part))
    text = ", ".join(parts)
    return text[:1].upper() + text[1:]


def row_note(kind, row):
    """Why a copy was left alone, for the Notes column; '' where the status says it all."""
    if dupes.tone(kind, row) != "neutral" or row.get("action") == "already disabled": return ""
    text = dupes.action_text(row["action"])
    text = text[len("Skipped: "):] if text.startswith("Skipped: ") else text
    return text[:1].upper() + text[1:]


def short_path(path):
    return str(path or "").rsplit("/", 1)[-1] or "—"


def when(value):
    """An ISO time as '16 Nov 2026, 14:59' in this PC's time zone; '' for none."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None: moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone()
    return local.strftime("%d %b %Y, %H:%M ") + (local.tzname() or "")


def plural(count, word, many=None):
    return f"{count} {word if count == 1 else (many or word + 's')}"


def status_cell(kind, row, mode):
    fill, color = STATUS[dupes.tone(kind, row)]
    return Cell(status_label(kind, row, mode), fill=fill, color=color, bold=True)


def count_cell(value, tone):
    if not value: return Cell("–", color=MUTED, align="center")
    fill, color = STATUS[tone]
    return Cell(str(value), fill=fill, color=color, bold=True, align="center")


def kpi_tiles(doc, tiles):
    """A row of figures: [(value, label, tone)]."""
    width = 10080 // len(tiles)
    cells = []
    for value, label, tone in tiles:
        fill, color = STATUS[tone] if tone else ("EEF2F7", NAVY)
        cells.append(Cell([paragraph_xml(run(str(value), color=color, bold=True), style="KpiValue"),
                           paragraph_xml(run(label, color=color), style="KpiLabel")], fill=fill))
    columns = [("", width)] * (len(tiles) - 1) + [("", 10080 - width * (len(tiles) - 1))]
    doc.table(columns, [cells], header=False, border_color="FFFFFF", border_size=24)


class Report:
    def __init__(self, reports, record, logo=None, org_logo=None, generated=None):
        self.reports, self.record = reports, record
        self.plan, self.result = record["plan"], record.get("result") or {}
        self.kind, self.rows, self.mode = reports["kind"], reports["rows"], reports.get("mode") or self.plan["mode"]
        self.generated = generated or datetime.now(timezone.utc)
        self.logo, self.org_logo = logo, org_logo
        number = reports.get("run_number") or self.result.get("run_number") or self.result.get("run_id") or ""
        self.run_label = f"GitHub run #{number}" if number else "GitHub run"
        self.title = TITLES[self.mode]
        self.doc = Document(self.title, header="Sentinel Workspace  ·  Confidential",
                            footer=f"{self.run_label}  ·  {self.plan['repository']}", created=self.generated)
        self.clients = self.plan.get("clients") or {}

    def client_name(self, target):
        name = self.clients.get(target)
        return f"{name} ({target})" if name and name != target else target

    # ------------------------------------------------------------- sections
    def build(self):
        self.title_block()
        self.run_details()
        self.summary()
        self.by_client()
        self.actions()
        self.findings()
        self.method()
        return self.doc.to_bytes()

    def title_block(self):
        doc = self.doc
        mark = [paragraph_xml(doc.image(self.logo, 44, "Sentinel Workspace"))] if self.logo else [paragraph_xml("")]
        words = [paragraph_xml(run(self.title), style="Title"),
                 paragraph_xml(run("Microsoft Sentinel analytics rules  ·  " + self.plan["repository"]), style="Subtitle")]
        org = [paragraph_xml(doc.image(self.org_logo, 40, "Organization logo"), align="right")] if self.org_logo else \
            [paragraph_xml("")]
        doc.table([("", 900), ("", 7180), ("", 2000)], [[Cell(mark), Cell(words), Cell(org)]], header=False, borders=False)
        doc.paragraph("", border_bottom=ACCENT, space_after=200)

    def run_details(self):
        doc, plan, result = self.doc, self.plan, self.result
        inputs = plan.get("inputs") or {}
        targets = plan.get("targets") or []
        if plan.get("every"):
            clients = f"Every enabled client in the library ({len(targets)})"
        else:
            shown = ", ".join(targets[:10]) + (f" and {len(targets) - 10} more" if len(targets) > 10 else "")
            clients = f"{len(targets)} selected: {shown}"
        rules = plan.get("rules") or []
        if self.mode in dupes.SCAN_MODES:
            checked = "Not used: this works from the scan of each workspace, comparing every rule with the others"
        elif rules:
            checked = f"{len(rules)}: " + ", ".join(short_path(p) for p in rules[:10]) + \
                (f" and {len(rules) - 10} more" if len(rules) > 10 else "")
        else: checked = "Every library rule assigned to each client"
        run_cell = [doc.link(self.run_label, result.get("url")), run("  ·  " + (result.get("state") or "Unknown"))]
        details = [("Run", run_cell), ("What ran", MODE_LINES[self.mode]), ("Clients", clients),
                   ("Library rules", checked)]
        if inputs.get("similarity"):
            try: details.append(("Query similarity", f"{float(inputs['similarity']):.0%} or more alike counts as a copy"))
            except ValueError: pass
        if plan.get("extra_names"): details.append(("Other names", "; ".join(plan["extra_names"])))
        if self.mode.startswith("disable"):
            details.append(("Similar-only matches", "Disabled too" if inputs.get("include_similar") == "true"
                            else "Left for review, not disabled"))
        details += [("Library revision", f"main at {str(plan.get('revision', ''))[:12]}"),
                    ("Started by", plan.get("identity") or "—"),
                    ("Run checked", when(result.get("checked_at")) or "—"),
                    ("Report generated", when(self.generated.isoformat()))]
        rows = [[Cell(key, bold=True, color=NAVY, fill="F3F6FA"),
                 Cell("".join(value) if isinstance(value, list) else value)] for key, value in details]
        doc.table([("", 2300), ("", 7780)], rows, header=False)
        if result.get("conclusion") not in (None, "success"):
            doc.callout(run(f"The run finished as {result.get('state') or result.get('conclusion')}. Its reports may be "
                            "incomplete; the run's log in GitHub says why.", color="9C0006"), color="C0392B", fill="FDECEA")

    def counts(self, rows):
        found = {tone: 0 for tone in ORDER}
        for row in rows: found[dupes.tone(self.kind, row)] += 1
        return found

    def summary(self):
        doc, rows = self.doc, self.rows
        doc.heading("Summary")
        targets = sorted({row["target"] for row in rows})
        if self.kind == "scan":
            groups = {(row["target"], row["group"]) for row in rows}
            live = {(row["target"], row["group"]) for row in rows if dupes.tone("scan", row) == "danger"}
            kpi_tiles(doc, [(len(rows), "Rules in duplicate groups", None), (len(groups), "Duplicate groups", None),
                            (len(live), "Groups with more than one enabled", "danger" if live else "success"),
                            (len(targets), "Clients affected", None)])
            if not rows:
                doc.paragraph("No rules in the selected clients' workspaces look like copies of each other.")
                return
            doc.paragraph(f"The scan found {plural(len(rows), 'rule')} in {plural(len(groups), 'group')} of rules that "
                          f"look like copies of each other, across {plural(len(targets), 'client')}. "
                          + (f"{plural(len(live), 'group has', 'groups have')} more than one enabled rule, so the same "
                             "detection runs more than once there and can raise duplicate incidents."
                             if live else "Every group has at most one enabled rule, so no detection runs twice."))
            return
        if self.kind == "cleanup":
            groups = {(row["target"], row["group"]) for row in rows}
            action = lambda row: str(row.get("action") or "")
            kept = sum(action(row) == "keep" for row in rows)
            would = sum(action(row) == "disable" for row in rows)
            done = sum(action(row) == "disabled" for row in rows)
            skipped = {(row["target"], row["group"]) for row in rows if action(row).startswith("skip")}
            failed = sum(action(row).startswith("FAILED") for row in rows)
            tiles = [(len(groups), "Duplicate groups", None), (kept, "Kept enabled", "info")]
            tiles.append((done, "Disabled by this run", "success") if self.mode == "cleanup-apply"
                         else (would, "Would be disabled", "warning"))
            tiles.append((len(skipped), "Groups skipped", "neutral"))
            if failed: tiles.append((failed, "Failed", "danger"))
            kpi_tiles(doc, tiles)
            if not rows:
                doc.paragraph("No workspace among the selected clients has two or more enabled copies of a rule: "
                              "there is nothing to clean up.")
                return
            text = (f"Across {plural(len(targets), 'client')}, {plural(len(groups), 'group')} of look-alike rules "
                    f"{'has' if len(groups) == 1 else 'have'} two or more rules enabled. Each keeps one rule enabled: "
                    "the library rule if the group has one, otherwise the most recently modified. ")
            if self.mode == "cleanup-apply":
                text += f"This run disabled {plural(done, 'rule')}" + (f", and {failed} failed." if failed else ".")
            else:
                text += f"Clean up duplicates would disable {plural(would, 'rule')}."
            if skipped:
                text += (f" {plural(len(skipped), 'group was', 'groups were')} skipped because "
                         f"{'it has' if len(skipped) == 1 else 'they have'} more than one enabled library rule.")
            doc.paragraph(text)
            after = next((name for name in self.reports.get("files", {}) if name.endswith("-after.csv")), None)
            if after:
                try:
                    found = dupes.parse_report(self.reports["files"][after])["rows"]
                    left = {(row["target"], row["group"]) for row in found if dupes.tone("scan", row) == "danger"}
                except Exception:  # The run's own report still stands; the follow-up scan is extra.
                    left = None
                if left is not None:
                    doc.callout(run("A scan run straight after the change found "
                                    + (f"{plural(len(left), 'group')} still with more than one enabled rule."
                                       if left else "no group with more than one enabled rule.")))
            return
        counts = self.counts(rows)
        tiles = [(len(rows), "Possible copies", None)]
        if self.mode == "disable-apply": tiles.append((counts["success"], "Disabled by this run", "success"))
        else: tiles.append((counts["warning"], "Would be disabled", "warning"))
        tiles += [(counts["info"], "To review", "info"), (counts["neutral"], "Skipped or already disabled", "neutral")]
        if counts["danger"]: tiles.append((counts["danger"], "Failed", "danger"))
        kpi_tiles(doc, tiles)
        if not rows:
            doc.paragraph("None of the selected clients' workspaces holds a copy of the library rules checked.")
            return
        enabled = sum(dupes.truthy(row["dup_enabled"]) for row in rows)
        noun = {"audit": "audit", "disable-preview": "dry run", "disable-apply": "run"}[self.mode]
        text = (f"The {noun} found {plural(len(rows), 'workspace rule')} across {plural(len(targets), 'client')} that "
                f"{'looks' if len(rows) == 1 else 'look'} like copies of library rules; {enabled} of them "
                f"{'is' if enabled == 1 else 'are'} enabled. ")
        if self.mode == "disable-apply":
            text += f"It disabled {plural(counts['success'], 'copy', 'copies')}"
            text += f", and {counts['danger']} failed." if counts["danger"] else "."
        else:
            text += (f"{plural(counts['warning'], 'enabled copy', 'enabled copies')} would be disabled by Disable library copies"
                     + (f", and {counts['info']} matched only by a similar query and "
                        f"{'needs' if counts['info'] == 1 else 'need'} review" if counts["info"] else "") + ".")
        doc.paragraph(text)
        after = next((name for name in self.reports.get("files", {}) if name.endswith("-after.csv")), None)
        if after:
            try:
                remaining = [row for row in dupes.parse_report(self.reports["files"][after])["rows"]
                             if row.get("action") == "disable"]
            except Exception:  # The run's own report still stands; the follow-up audit is extra.
                remaining = None
            if remaining is not None:
                doc.callout(run("An audit run straight after the change found "
                                + (f"{plural(len(remaining), 'enabled copy', 'enabled copies')} still to disable."
                                   if remaining else "no enabled copies left to disable.")))

    def by_client(self):
        doc, rows = self.doc, self.rows
        if not rows: return
        doc.heading("Results by client", 2)
        targets = sorted({row["target"] for row in rows}, key=lambda t: self.client_name(t).casefold())
        if self.kind == "scan":
            columns = [("Client", 4080), ("Rules", 1500), ("Groups", 1500), ("Groups with more than one enabled", 3000)]
            table, totals = [], [0, 0, 0]
            for target in targets:
                mine = [row for row in rows if row["target"] == target]
                groups = {row["group"] for row in mine}
                live = {row["group"] for row in mine if dupes.tone("scan", row) == "danger"}
                totals = [totals[0] + len(mine), totals[1] + len(groups), totals[2] + len(live)]
                table.append([Cell(self.client_name(target)), Cell(str(len(mine)), align="center"),
                              Cell(str(len(groups)), align="center"), count_cell(len(live), "danger")])
            table.append(([Cell("Total", bold=True)] + [Cell(str(v), bold=True, align="center") for v in totals], "E2E8F0"))
            doc.table(columns, table)
            return
        if self.kind == "cleanup":
            last = ("Disabled", "success") if self.mode == "cleanup-apply" else ("Would disable", "warning")
            columns = [("Client", 3680), ("Groups", 1100), ("Kept", 1100), (last[0], 1500), ("Skipped", 1300),
                       ("Failed", 1400)]
            table, totals = [], [0] * 5
            for target in targets:
                mine = [row for row in rows if row["target"] == target]
                action = lambda row: str(row.get("action") or "")
                values = [len({row["group"] for row in mine}), sum(action(r) == "keep" for r in mine),
                          sum(action(r) == ("disabled" if last[1] == "success" else "disable") for r in mine),
                          sum(action(r).startswith("skip") for r in mine), sum(action(r).startswith("FAILED") for r in mine)]
                totals = [a + b for a, b in zip(totals, values)]
                table.append([Cell(self.client_name(target)), Cell(str(values[0]), align="center"),
                              count_cell(values[1], "info"), count_cell(values[2], last[1]),
                              count_cell(values[3], "neutral"), count_cell(values[4], "danger")])
            table.append(([Cell("Total", bold=True)] + [Cell(str(v), bold=True, align="center") for v in totals], "E2E8F0"))
            doc.table(columns, table)
            return
        last = ("Disabled", "success") if self.mode == "disable-apply" else ("Would disable", "warning")
        columns = [("Client", 3280), ("Copies", 1100), (last[0], 1400), ("Review", 1100), ("Skipped", 1100),
                   ("Already disabled", 1100), ("Failed", 1000)]
        table, totals = [], [0] * 6
        for target in targets:
            mine = [row for row in rows if row["target"] == target]
            counts = self.counts(mine)
            already = sum(row.get("action") == "already disabled" for row in mine)
            values = [len(mine), counts[last[1]], counts["info"], counts["neutral"] - already, already, counts["danger"]]
            totals = [a + b for a, b in zip(totals, values)]
            table.append([Cell(self.client_name(target)), Cell(str(values[0]), align="center"),
                          count_cell(values[1], last[1]), count_cell(values[2], "info"), count_cell(values[3], "neutral"),
                          count_cell(values[4], "neutral"), count_cell(values[5], "danger")])
        table.append(([Cell("Total", bold=True)] + [Cell(str(v), bold=True, align="center") for v in totals], "E2E8F0"))
        doc.table(columns, table)

    def actions(self):
        doc, rows = self.doc, self.rows
        doc.heading("Recommended next steps", 2)
        steps = []
        if self.kind == "scan":
            live = {(row["target"], row["group"]) for row in rows if dupes.tone("scan", row) == "danger"}
            if live:
                steps.append(f"Run Preview cleanup with the same clients and query similarity: for each of the "
                             f"{plural(len(live), 'group')} with more than one enabled rule it lists the one rule it "
                             "keeps (the library rule if the group has one, otherwise the newest) and the others it "
                             "would disable.")
                steps.append("Check groups matched on name or template alone: their queries may differ, so a copy may "
                             "have been tuned on purpose.")
                steps.append("Then run Clean up duplicates with the same choices. It disables, never deletes, and "
                             "GitHub asks for approval before it changes anything.")
            if not steps: steps.append("No action is needed: no detection runs more than once in the clients scanned.")
            doc.bullets(steps)
            return
        if self.kind == "cleanup":
            action = lambda row: str(row.get("action") or "")
            would = [row for row in rows if action(row) == "disable"]
            loose = {(row["target"], row["group"]) for row in would if "similar" not in str(row.get("group_match"))}
            skipped = {(row["target"], row["group"]) for row in rows if action(row).startswith("skip")}
            failed = [row for row in rows if action(row).startswith("FAILED")]
            if would and loose:
                steps.append(f"Check the {plural(len(loose), 'group')} matched on name or template alone before "
                             "applying: their queries may differ, so the copy being disabled may have been tuned on purpose.")
            if would and self.mode == "cleanup-preview":
                steps.append(f"Run Clean up duplicates with the same clients and query similarity to disable the "
                             f"{plural(len(would), 'rule')} marked Would disable.")
            if skipped:
                steps.append(f"{plural(len(skipped), 'group has', 'groups have')} more than one enabled library rule. "
                             "Change those in the library (for example, unassign one of them from the client), or the "
                             "next deployment turns the disabled one back on.")
            if failed:
                steps.append(f"{plural(len(failed), 'rule', 'rules')} couldn't be disabled. The run's log in GitHub has "
                             "the error; fix access or the rule, then run Clean up duplicates again.")
            if not steps: steps.append("No action is needed: every duplicate group has one rule enabled.")
            doc.bullets(steps)
            return
        by = lambda test: [row for row in rows if test(row)]
        would = by(lambda row: row.get("action") == "disable")
        review = by(lambda row: str(row.get("action", "")).startswith("review"))
        missing = by(lambda row: str(row.get("action", "")).startswith("skip: repo rule"))
        managed = by(lambda row: row.get("action") == "skip: enabled repo rule, change it in git")
        failed = by(lambda row: str(row.get("action", "")).startswith("FAILED"))
        if would and self.mode == "audit":
            steps.append(f"Disable the {plural(len(would), 'enabled copy', 'enabled copies')} marked Would disable: run "
                         "Preview disabling for their library rules to confirm the list, then Disable library copies. It "
                         "disables, never deletes, and GitHub asks for approval before it changes anything.")
        if would and self.mode == "disable-preview":
            steps.append(f"Run Disable library copies with the same choices to disable the "
                         f"{plural(len(would), 'copy', 'copies')} marked Would disable.")
        if review:
            steps.append(f"Review the {plural(len(review), 'rule')} matched only by a similar query. If one is a copy, list "
                         "its name under Other names in the next run (or tick Also disable rules matched only by a "
                         "similar query); if it detects something different, keep it.")
        if missing:
            steps.append(f"{plural(len(missing), 'copy was', 'copies were')} left alone because the library rule isn't "
                         "running in that workspace. Deploy the library rule first (Preview and deploy), then run the "
                         "audit again; until then the copy is the only coverage.")
        if managed:
            steps.append(f"{plural(len(managed), 'match is', 'matches are')} itself a library rule. Change those in the "
                         "library (for example, unassign one of the two rules from the client) rather than in the "
                         "workspace, or the next deployment puts them back.")
        if failed:
            steps.append(f"{plural(len(failed), 'copy', 'copies')} couldn't be disabled. The run's log in GitHub has the "
                         "error; fix access or the rule, then run Disable library copies again.")
        if not steps: steps.append("No action is needed: no enabled copy of the library rules checked is left to disable.")
        doc.bullets(steps)

    def colour_key(self):
        doc = self.doc
        if self.kind == "scan":
            key = [("danger", "Duplicate enabled", "Enabled, and its group has more than one enabled rule."),
                   ("success", "Only one enabled", "Enabled, and the only enabled rule in its group."),
                   ("neutral", "Disabled", "Disabled in the workspace.")]
        elif self.kind == "cleanup":
            key = [("info", "Keep", "The one rule left enabled in its group."),
                   ("warning", "Would disable", "An enabled rule Clean up duplicates would disable."),
                   ("success", "Disabled", "Disabled by this run."),
                   ("neutral", "Skipped / Already disabled", "A group with more than one enabled library rule, or a "
                                                             "rule already disabled."),
                   ("danger", "Failed", "The change was not applied.")]
        else:
            key = [("warning", "Would disable", "An enabled copy that Disable library copies would disable."),
                   ("success", "Disabled", "Disabled by this run."),
                   ("info", "Review", "Matched only by a similar query: a person should decide."),
                   ("neutral", "Skipped / Already disabled", "Left alone (the reason is in Notes), or already disabled."),
                   ("danger", "Failed", "The change was not applied.")]
        rows = [[Cell(label, fill=STATUS[tone][0], color=STATUS[tone][1], bold=True), Cell(text)] for tone, label, text in key]
        doc.table([("Status colour", 2300), ("Meaning", 7780)], rows)

    def findings(self):
        doc, rows = self.doc, self.rows
        if not rows: return
        doc.heading("Findings", 1, page_break_before=True)
        self.colour_key()
        shown = rows[:MAX_ROWS]
        if len(rows) > MAX_ROWS:
            doc.callout(run(f"This document lists the first {MAX_ROWS} of {len(rows)} rows. The CSV and Excel report saved "
                            "with it hold every row."))
        # A cleanup lists each group's kept rule first, then the ones it disables.
        rank = lambda row: (-1 if self.kind == "cleanup" and row.get("action") == "keep" else
                            ORDER.index(dupes.tone(self.kind, row)),
                            str(row.get("name") or row.get("dup_name") or "").casefold())
        for target in sorted({row["target"] for row in shown}, key=lambda t: self.client_name(t).casefold()):
            mine = [row for row in shown if row["target"] == target]
            doc.heading(self.client_name(target), 2)
            if self.kind in ("scan", "cleanup"):
                groups = sorted({row["group"] for row in mine}, key=lambda g: int(g) if str(g).isdigit() else 0)
                line = f"{plural(len(mine), 'rule')} in {plural(len(groups), 'group')}"
                if self.kind == "cleanup":
                    off = sum(str(row.get("action")) in ("disable", "disabled") for row in mine)
                    line += f"  ·  {off} {'disabled' if self.mode == 'cleanup-apply' else 'would be disabled'}"
                doc.paragraph(run(line, color=MUTED), style="Small")
                table = []
                for number, group in enumerate(groups):
                    members = sorted((row for row in mine if row["group"] == group), key=rank)
                    fill = "F3F6FA" if number % 2 else None
                    for row in members:
                        table.append(([Cell(f"{group}", align="center"), status_cell(self.kind, row, self.mode),
                                       Cell(row["name"], note=row["rule_id"]),
                                       Cell("Yes" if dupes.truthy(row["enabled"]) else "No", align="center"),
                                       Cell(short_path(row["repo_managed"])), Cell(group_reason(row["group_match"]))], fill))
                doc.table([("Group", 700), ("Status", 1500), ("Rule", 3200), ("Enabled", 950), ("Library rule", 1650),
                           ("Why grouped", 2080)], table)
                continue
            counts = self.counts(mine)
            summary = [f"{plural(len(mine), 'possible copy', 'possible copies')}"] + \
                      [f"{counts[t]} {w}" for t, w in (("warning", "would disable"), ("success", "disabled"),
                                                        ("info", "to review"), ("danger", "failed")) if counts[t]]
            doc.paragraph(run("  ·  ".join(summary), color=MUTED), style="Small")
            table = []
            for row in sorted(mine, key=rank):
                note = row_note(self.kind, row)
                table.append([status_cell(self.kind, row, self.mode), Cell(row["dup_name"], note=row["dup_rule_id"]),
                              Cell("Yes" if dupes.truthy(row["dup_enabled"]) else "No", align="center"),
                              Cell(match_text(row)), Cell(short_path(row["repo_rule"])), Cell(note)])
            doc.table([("Status", 1400), ("Workspace rule", 3050), ("Enabled", 950), ("Match", 1700),
                       ("Library rule", 1500), ("Notes", 1480)], table, band=True)

    def method(self):
        doc = self.doc
        doc.heading("How duplicates are found", 1, page_break_before=bool(self.rows))
        if self.kind == "scan":
            doc.paragraph("A scan compares every analytics rule in each workspace with every other rule there, whether "
                          "or not the library manages it, and groups rules that:")
            doc.bullets(["have the same display name (ignoring case and spacing),",
                         "were created from the same Microsoft template, or",
                         "have queries at least as alike as the query similarity set for the run (comments and spacing "
                         "ignored; only rules reading the same first table are compared)."])
            doc.paragraph("A scan only reads: it changes nothing.")
        elif self.kind == "cleanup":
            doc.paragraph("The run scans each workspace as Scan workspaces does: it compares every analytics rule with "
                          "every other rule there, whether or not the library manages it, and groups rules that:")
            doc.bullets(["have the same display name (ignoring case and spacing),",
                         "were created from the same Microsoft template, or",
                         "have queries at least as alike as the query similarity set for the run (comments and spacing "
                         "ignored; only rules reading the same first table are compared)."])
            doc.paragraph("In each group with more than one enabled rule, it keeps one enabled and disables the rest:")
            doc.bullets(["the rule kept is the library rule when the group has one, so the pipeline never turns a "
                         "disabled copy back on; otherwise the most recently modified rule;",
                         "a group with more than one enabled library rule is skipped: those are changed in the library;",
                         "rules are disabled, never deleted, and only through the library's GitHub workflow after "
                         "approval of its production environment."])
            doc.paragraph("Groups are transitive (if A is like B and B is like C, all three are grouped) and a name or "
                          "template match ignores the query, so read the preview before applying.")
        else:
            doc.paragraph("For each library rule checked, the run compares it with every other analytics rule in the "
                          "client's workspace. A workspace rule with a different rule ID counts as a copy when it:")
            doc.bullets(["has the same display name (ignoring case and spacing),",
                         "was created from the same Microsoft template,",
                         "has a name listed under Other names, or",
                         "has a query at least as alike as the query similarity set for the run (comments and spacing "
                         "ignored). A match on the query alone is listed for review and is disabled only when the run "
                         "is asked to."])
            doc.paragraph("Disable library copies only ever disables a copy, never deletes one, and only:")
            doc.bullets(["in a workspace where the library rule itself is present and enabled, so coverage never drops;",
                         "when the copy isn't itself an enabled library rule (those are changed in the library instead);",
                         "through the library's GitHub workflow, after approval of its production environment."])
        doc.heading("About this report", 2)
        files = ", ".join(sorted(self.reports.get("files", {})))
        doc.paragraph(f"Generated by Sentinel Workspace {__version__} from the reports of {self.run_label} "
                      f"({self.reports.get('artifact') or 'its artifact'}): {files}. The workflow read each workspace "
                      "with the pipeline's identity; this report adds nothing the run didn't record.", style="Small")


def build(reports, record, logo=None, org_logo=None, generated=None):
    """The report for a finished run, as .docx bytes."""
    return Report(reports, record, logo=logo, org_logo=org_logo, generated=generated).build()


def file_name(reports):
    """The report's file name, beside the run's CSV: rule-duplicates.docx or workspace-duplicates.docx."""
    return reports["name"].rsplit(".", 1)[0] + ".docx"
