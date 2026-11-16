"""The duplicate workflow adapter and its dialog: offline, against an in-memory GitHub."""
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
import zipfile

from sentinel_app import desktop_app as ui
from sentinel_app import duplicate_workflow as dupes
from sentinel_app.duplicate_dialog import DuplicateDialog, row_values
from sentinel_app.duplicate_workflow import DuplicateService, FIELDS, SCAN_FIELDS, WORKFLOW_PATH
from sentinel_app.repository_catalog import GitHubReader
from sentinel_app.repository_reviews import ReviewError
from test_repository_reviews import FakeGitHub, REPO, EXISTING


def workflow(**changes):
    inputs = dict(mode=dict(type="choice", options=["scan", "audit", "disable-preview", "disable-apply"]),
                  rules=dict(type="string"), targets=dict(type="string"), extra_names=dict(type="string"),
                  include_similar=dict(type="boolean"), similarity=dict(type="string"))
    inputs.update(changes)
    return {"on": {"workflow_dispatch": {"inputs": {k: v for k, v in inputs.items() if v is not None}}}}


def report(fields, rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    return out.getvalue().encode("utf-8")


AUDIT_ROWS = [
    dict(target="example-primary", repo_rule=EXISTING, repo_rule_id="a", repo_copy="enabled", dup_rule_id="b",
         dup_name="Old copy", dup_enabled="True", dup_managed_by="", match="name+similar", similarity="0.97",
         action="disable"),
    dict(target="example-primary", repo_rule=EXISTING, repo_rule_id="a", repo_copy="enabled", dup_rule_id="c",
         dup_name="Lookalike", dup_enabled="True", dup_managed_by="", match="similar", similarity="0.91",
         action="review: similar query only"),
    dict(target="client-0-primary", repo_rule=EXISTING, repo_rule_id="a", repo_copy="missing", dup_rule_id="d",
         dup_name="Old copy", dup_enabled="False", dup_managed_by="", match="name", similarity="",
         action="already disabled")]
SCAN_ROWS = [
    dict(target="example-primary", group="1", rule_id="a", name="Brute force", kind="Scheduled", enabled="True",
         template="", repo_managed=EXISTING, group_match="name, similar query 0.98", enabled_in_group="2"),
    dict(target="example-primary", group="1", rule_id="b", name="Brute force", kind="Scheduled", enabled="True",
         template="", repo_managed="", group_match="name, similar query 0.98", enabled_in_group="2"),
    dict(target="example-primary", group="2", rule_id="c", name="Spray", kind="Scheduled", enabled="False",
         template="", repo_managed="", group_match="template", enabled_in_group="1")]


def artifact(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content in files.items(): archive.writestr(name, content)
    return data.getvalue()


class DuplicateGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.files[WORKFLOW_PATH] = json.dumps(workflow()).encode()
        template = json.loads(self.files["clients/example/workspace.yml"])
        for i in range(2):  # client-0 is switched off; client-1 is enabled.
            item = copy.deepcopy(template); item.update(client="client-" + str(i), target="client-" + str(i) + "-primary",
                                                        enabled=i == 1)
            self.files["clients/client-" + str(i) + "/workspace.yml"] = json.dumps(item).encode()
        self.workflow = dict(id=88, path=WORKFLOW_PATH, state="active")
        self.dispatched, self.lose_response = [], False
        self.run = dict(status="in_progress", conclusion=None)
        self.artifacts = [dict(id=5, name="duplicate-reports-audit-12", expired=False, size_in_bytes=100)]
        self.zip = artifact({"rule-duplicates.csv": report(FIELDS, AUDIT_ROWS), "rule-duplicates.xlsx": b"PK-not-real",
                             "../escape.csv": b"x", "nested/inner.csv": b"x", "notes.txt": b"x"})
        self.downloads = []

    def __call__(self, method, endpoint, body=None):
        if "/actions/" not in endpoint: return super().__call__(method, endpoint, body)
        self.calls.append((method, endpoint, copy.deepcopy(body)))
        if method == "GET" and "/actions/workflows/" in endpoint: return copy.deepcopy(self.workflow)
        if method == "GET" and endpoint.endswith("/artifacts?per_page=100"): return dict(artifacts=copy.deepcopy(self.artifacts))
        if method == "GET" and "/actions/runs/" in endpoint:
            run_id = int(endpoint.rsplit("/", 1)[1])
            return dict(id=run_id, workflow_id=88, event="workflow_dispatch", head_branch="main", head_sha=self.head,
                        repository={"id": 42}, run_number=12, **self.run)
        if method == "POST" and endpoint.endswith("/dispatches"):
            self.dispatched.append(copy.deepcopy(body))
            if self.lose_response: raise ReviewError("Lost dispatch response")
            return {"workflow_run_id": 500 + len(self.dispatched)}
        raise AssertionError((method, endpoint))

    def download(self, endpoint):
        self.downloads.append(endpoint)
        return self.zip


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.remote = DuplicateGitHub(); self.service = DuplicateService(self.remote, self.remote.download)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.folder = Path(self.temp.name)

    def prepare(self, mode="audit", targets=("example-primary", "client-1-primary"), **kw):
        return self.service.prepare(REPO, self.remote.head, mode, list(targets), **kw)

    def test_every_enabled_client_is_sent_as_all_and_a_subset_by_name(self):
        record = self.prepare("scan", rules=[EXISTING], extra_names="Old name", include_similar=True)
        self.assertEqual(record["plan"]["inputs"], dict(mode="scan", rules="", targets="all", extra_names="",
                                                        include_similar="false", similarity="0.90"))
        record = self.prepare(targets=["example-primary"], rules=[EXISTING], similarity=0.95)
        self.assertEqual(record["plan"]["inputs"]["targets"], "example-primary")
        self.assertEqual(record["plan"]["inputs"]["rules"], EXISTING)
        self.assertEqual(record["plan"]["inputs"]["similarity"], "0.95")
        self.assertFalse(self.remote.dispatched)  # Checking starts nothing.

    def test_disable_modes_need_rules_and_send_names_and_similar_choice(self):
        with self.assertRaisesRegex(ReviewError, "Choose the library rules"): self.prepare("disable-preview")
        record = self.prepare("disable-apply", rules=[EXISTING], extra_names="Old name; Older name", include_similar=True)
        self.assertEqual(record["plan"]["inputs"]["extra_names"], "Old name,Older name")
        self.assertEqual(record["plan"]["inputs"]["include_similar"], "true")
        with self.assertRaisesRegex(ReviewError, "comma"): self.prepare("audit", extra_names="A, B")

    def test_switched_off_and_unknown_choices_are_refused(self):
        with self.assertRaisesRegex(ReviewError, "enabled"): self.prepare(targets=["client-0-primary"])
        with self.assertRaisesRegex(ReviewError, "at least one"): self.prepare(targets=[])
        with self.assertRaisesRegex(ReviewError, "isn't in the library"): self.prepare(rules=["rules/sentinel/nope.yml"])
        with self.assertRaisesRegex(ReviewError, "similarity"): self.prepare(similarity=0.2)
        with self.assertRaisesRegex(ReviewError, "repository changed"):
            self.service.prepare(REPO, "f" * 40, "scan", ["example-primary"])

    def test_a_changed_or_missing_or_inactive_workflow_stops_the_check(self):
        self.remote.files[WORKFLOW_PATH] = json.dumps(workflow(dry_run=dict(type="boolean"))).encode()
        with self.assertRaisesRegex(ReviewError, "inputs changed"): self.prepare()
        self.remote.files[WORKFLOW_PATH] = json.dumps(workflow(similarity=dict(type="number"))).encode()
        with self.assertRaisesRegex(ReviewError, "changed type"): self.prepare()
        self.remote.files[WORKFLOW_PATH] = json.dumps(workflow(mode=dict(type="choice", options=["scan"]))).encode()
        with self.assertRaisesRegex(ReviewError, "needs the modes"): self.prepare()
        self.remote.files[WORKFLOW_PATH] = json.dumps(workflow()).encode()
        self.remote.workflow["state"] = "disabled_manually"
        with self.assertRaisesRegex(ReviewError, "not active"): self.prepare()
        del self.remote.files[WORKFLOW_PATH]
        with self.assertRaisesRegex(ReviewError, "no .github/workflows"): self.prepare()

    def test_dispatch_sends_once_and_saves_the_attempt_first(self):
        record = self.prepare()
        self.service.dispatch(record, self.folder)
        self.assertEqual(self.remote.dispatched, [{"ref": "main", "inputs": record["plan"]["inputs"]}])
        self.assertEqual(record["result"]["run_id"], 501)
        saved = dupes.load_receipt(self.folder / (record["request_id"] + dupes.RECEIPT_SUFFIX))
        self.assertEqual(saved["result"]["run_id"], 501)
        with self.assertRaisesRegex(ReviewError, "already sent"): self.service.dispatch(record, self.folder)
        lost = self.prepare(); self.remote.lose_response = True
        with self.assertRaises(ReviewError): self.service.dispatch(lost, self.folder)
        saved = dupes.load_receipt(self.folder / (lost["request_id"] + dupes.RECEIPT_SUFFIX))
        self.assertEqual((saved["attempted"], saved["result"]["state"]), (True, "Submission unconfirmed"))

    def test_main_moving_after_the_check_stops_dispatch(self):
        record = self.prepare(); self.remote.head = "e" * 40
        with self.assertRaisesRegex(ReviewError, "Main changed"): self.service.dispatch(record, self.folder)
        self.assertFalse(self.remote.dispatched)

    def test_refresh_reads_status_and_refuses_another_run(self):
        record = self.service.dispatch(self.prepare(), self.folder)
        self.service.refresh(record, self.folder)
        self.assertEqual((record["result"]["state"], record["result"]["run_number"]), ("Running", 12))
        self.remote.run = dict(status="waiting", conclusion=None)
        self.assertEqual(self.service.refresh(record, self.folder)["result"]["state"], "Waiting for approval in GitHub")
        self.remote.run = dict(status="completed", conclusion="failure")
        self.assertEqual(self.service.refresh(record, self.folder)["result"]["state"], "Failed")
        record["plan"]["workflow_id"] = 99  # A receipt edited by hand no longer matches its fingerprint.
        with self.assertRaisesRegex(ReviewError, "changed"): self.service.refresh(record, self.folder)

    def test_reports_take_only_top_level_csv_and_excel_files(self):
        record = self.service.dispatch(self.prepare(), self.folder)
        with self.assertRaisesRegex(ReviewError, "hasn't finished"): self.service.reports(record)
        self.remote.run = dict(status="completed", conclusion="success"); self.service.refresh(record, self.folder)
        reports = self.service.reports(record)
        self.assertEqual(sorted(reports["files"]), ["rule-duplicates.csv", "rule-duplicates.xlsx"])
        self.assertEqual(self.remote.downloads, ["repos/" + REPO + "/actions/artifacts/5/zip"])
        self.remote.artifacts[0]["expired"] = True
        with self.assertRaisesRegex(ReviewError, "expired"): self.service.reports(record)
        self.remote.artifacts = [dict(id=6, name="something-else")]
        with self.assertRaisesRegex(ReviewError, "no reports"): self.service.reports(record)

    def test_artifact_must_be_a_zip_with_a_csv(self):
        with self.assertRaisesRegex(ReviewError, "ZIP"): dupes.read_artifact(b"<html>")
        with self.assertRaisesRegex(ReviewError, "no CSV"): dupes.read_artifact(artifact({"report.xlsx": b"x"}))
        with self.assertRaisesRegex(ReviewError, "couldn't be read"): dupes.read_artifact(b"PK\x03\x04broken")


class ReportTests(unittest.TestCase):
    def test_both_report_shapes_parse_and_unknown_columns_are_refused(self):
        self.assertEqual(dupes.parse_report(report(FIELDS, AUDIT_ROWS))["kind"], "duplicates")
        parsed = dupes.parse_report(b"\xef\xbb\xbf" + report(SCAN_FIELDS, SCAN_ROWS))  # With a BOM too.
        self.assertEqual((parsed["kind"], len(parsed["rows"])), ("scan", 3))
        with self.assertRaisesRegex(ReviewError, "columns"): dupes.parse_report(b"a,b\n1,2\n")
        with self.assertRaisesRegex(ReviewError, "malformed"):
            dupes.parse_report(report(FIELDS, AUDIT_ROWS[:1]) + b"x,y,z,1,2,3,4,5,6,7,8,9,10\n")

    def test_the_main_report_is_the_runs_own_pass(self):
        name, parsed = dupes.main_report({"rule-duplicates-after.csv": report(FIELDS, []),
                                          "rule-duplicates.csv": report(FIELDS, AUDIT_ROWS)})
        self.assertEqual((name, len(parsed["rows"])), ("rule-duplicates.csv", 3))

    def test_tones_follow_the_reports_colour_key(self):
        self.assertEqual([dupes.tone("duplicates", row) for row in AUDIT_ROWS], ["warning", "info", "neutral"])
        self.assertEqual(dupes.tone("duplicates", dict(action="disabled")), "success")
        self.assertEqual(dupes.tone("duplicates", dict(action="FAILED: 403")), "danger")
        self.assertEqual([dupes.tone("scan", row) for row in SCAN_ROWS], ["danger", "danger", "neutral"])
        self.assertEqual(dupes.tone("scan", dict(SCAN_ROWS[0], enabled_in_group="1")), "success")

    def test_actions_and_summaries_read_in_the_librarys_words(self):
        self.assertEqual(dupes.action_text("skip: repo rule missing here"), "Skipped: library rule missing here")
        self.assertEqual(dupes.action_text("skip: enabled repo rule, change it in git"),
                         "Skipped: a library rule, change it in the library")
        self.assertEqual(dupes.action_text("FAILED: denied"), "Failed: denied")
        self.assertEqual(dupes.summary("duplicates", AUDIT_ROWS, "audit"),
                         "3 possible copies across 2 clients · 1 would be disabled · 1 to review · "
                         "1 skipped or already disabled.")
        self.assertEqual(dupes.summary("scan", SCAN_ROWS),
                         "3 rules in 2 duplicate groups across 1 client · 1 group with more than one enabled rule.")
        self.assertIn("No copies", dupes.summary("duplicates", []))
        self.assertEqual(row_values("duplicates", AUDIT_ROWS[0]),
                         ("example-primary", "Old copy", "Yes", "name + similar (97%)", "Would disable", "example.yml"))
        self.assertEqual(row_values("scan", SCAN_ROWS[0])[4], "Group 1 · 2 enabled")

    def test_saving_names_files_after_the_run_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            files = {"rule-duplicates.csv": b"a", "rule-duplicates.xlsx": b"b"}
            first = dupes.save_reports(files, folder, 12)
            second = dupes.save_reports(files, folder, 12)
            self.assertEqual([p.name for p in first], ["rule-duplicates-run12.csv", "rule-duplicates-run12.xlsx"])
            self.assertEqual([p.name for p in second], ["rule-duplicates-run12-2.csv", "rule-duplicates-run12-2.xlsx"])
            self.assertEqual(Path(first[1]).read_bytes(), b"b")
            with self.assertRaises(ReviewError): dupes.save_reports({"../x.csv": b"a"}, folder)

    def test_latest_receipt_skips_unstarted_and_edited_requests(self):
        remote = DuplicateGitHub(); service = DuplicateService(remote, remote.download)
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(dupes.latest_receipt(folder))
            older = service.dispatch(service.prepare(REPO, remote.head, "scan", ["example-primary"]), folder)
            unstarted = service.prepare(REPO, remote.head, "audit", ["example-primary"])
            service.save(unstarted, folder)
            newer = service.dispatch(service.prepare(REPO, remote.head, "audit", ["example-primary"]), folder)
            self.assertEqual(dupes.latest_receipt(folder, REPO)["request_id"], newer["request_id"])
            path = Path(folder) / (newer["request_id"] + dupes.RECEIPT_SUFFIX)
            edited = json.loads(path.read_text()); edited["plan"]["inputs"]["mode"] = "disable-apply"
            path.write_text(json.dumps(edited))
            self.assertEqual(dupes.latest_receipt(folder, REPO)["request_id"], older["request_id"])
            self.assertIsNone(dupes.latest_receipt(folder, "someone/else"))


class DialogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        p = patch.object(ui, "DATA", Path(self.temp.name)); p.start(); self.addCleanup(p.stop)
        self.root = tk.Tk(); self.root.withdraw(); self.app = ui.App(self.root); self.root.update()
        self.addCleanup(self.cleanup)
        self.remote = DuplicateGitHub()
        self.snapshot = GitHubReader(lambda endpoint: self.remote("GET", endpoint)).load(REPO)
        self.open()
        patcher = patch.object(self.app, "work", side_effect=lambda title, task, done, **kw: done(task()))
        patcher.start(); self.addCleanup(patcher.stop)

    def open(self, rules=()):
        self.dialog = DuplicateDialog(self.app, self.snapshot, list(rules))
        self.dialog.service = DuplicateService(self.remote, self.remote.download)

    def cleanup(self):
        self.app.set_busy(False)
        for timer in self.root.tk.call("after", "info"): self.root.after_cancel(timer)
        self.root.destroy()

    def test_opens_on_scan_with_every_enabled_client_ticked(self):
        self.assertEqual(self.dialog.mode.get(), "scan")
        self.assertEqual(self.dialog.selected, {"example-primary", "client-1-primary"})
        self.assertEqual(self.dialog.count.get(), "2 of 2 enabled selected")
        self.assertEqual(str(self.dialog.start_button.cget("state")), "disabled")
        self.assertEqual(str(self.dialog.save_button.cget("state")), "disabled")

    def test_check_start_and_finish_show_the_report_and_save_it(self):
        self.dialog.window.destroy(); self.open([self.snapshot["rules"][0]])
        self.assertEqual(self.dialog.mode.get(), "audit")  # Rules were selected in the library.
        self.dialog.prepare()
        self.assertIn("Run: Audit library rules (read-only)", self.dialog.review.get("1.0", "end"))
        self.assertEqual(str(self.dialog.start_button.cget("state")), "normal")
        self.dialog.start()
        self.assertEqual(len(self.remote.dispatched), 1)
        self.assertTrue(self.dialog.locked and self.dialog.poll_timer)
        from sentinel_app.desktop_theme import COLORS
        self.dialog.mode_bar.render()  # Drawn while the run is going (a theme refresh, say): greyed out.
        self.assertEqual(self.dialog.mode_bar.items["scan"].cget("fg"), COLORS["disabled"])
        self.dialog.poll()  # Still running: it checks again later.
        self.assertIn("Running", self.dialog.notice.get())
        self.remote.run = dict(status="completed", conclusion="success")
        self.dialog.poll()
        self.assertFalse(self.dialog.locked)
        rows = self.dialog.table.get_children()
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.dialog.table.item(rows[0], "values")[4], "Would disable")
        self.assertIn("1 would be disabled", self.dialog.result_text.get())
        self.assertEqual(str(self.dialog.save_button.cget("state")), "normal")
        with tempfile.TemporaryDirectory() as folder:
            with patch("sentinel_app.duplicate_dialog.filedialog.askdirectory", return_value=folder):
                self.dialog.save_reports()
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()),
                             ["rule-duplicates-run12.csv", "rule-duplicates-run12.docx", "rule-duplicates-run12.xlsx"])
            self.assertTrue(zipfile.is_zipfile(Path(folder) / "rule-duplicates-run12.docx"))
        self.assertIn("Saved rule-duplicates-run12.csv", self.dialog.notice.get())
        # Once the run has finished, the mode switch looks usable again, not greyed out as while it ran.
        self.assertNotEqual(self.dialog.mode_bar.items["scan"].cget("fg"), COLORS["disabled"])
        self.assertEqual(self.dialog.banner.action_button.cget("text"), "Open Word report")
        self.assertIn("a Word report, rule-duplicates.docx", self.dialog.result_text.get())
        # If the Word report can't be made, the run's own reports are still saved, and the dialog says so.
        with tempfile.TemporaryDirectory() as folder:
            with patch("sentinel_app.duplicate_dialog.filedialog.askdirectory", return_value=folder), \
                    patch("sentinel_app.duplicate_dialog.duplicate_report.build", side_effect=ValueError("broken")):
                self.dialog.save_reports()
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()),
                             ["rule-duplicates-run12.csv", "rule-duplicates-run12.xlsx"])
        self.assertIn("the Word report couldn't be made: broken", self.dialog.notice.get())

    def test_rules_can_be_chosen_in_the_dialog(self):
        rules = self.snapshot["rules"]
        self.dialog.mode.set("disable-preview")
        self.dialog.all_rules()  # Every library rule, without going back to the Rule library.
        self.assertEqual([r["path"] for r in self.dialog.rules], [r["path"] for r in rules])
        self.assertEqual(self.dialog.rules_text.get(), f"All {len(rules)} library rules.")
        self.dialog.prepare()
        self.assertEqual(self.dialog.record["plan"]["rules"], sorted(r["path"] for r in rules))
        with patch("sentinel_app.duplicate_dialog.kit.modal"):
            self.dialog.choose_rules()
        picker = self.dialog.chooser
        picker.clear(); picker.toggle(next(iter(picker.visible)))
        self.assertEqual(picker.count.get(), f"1 of {len(rules)} rules ticked")
        picker.use()
        self.assertEqual(len(self.dialog.rules), 1)
        self.assertIsNone(self.dialog.record)  # A new choice needs a new check.
        self.dialog.mode.set("scan")
        self.assertFalse(self.dialog.rule_buttons.winfo_manager())  # A scan uses no library rules.

    def test_disable_without_rules_says_how_and_disable_apply_asks_first(self):
        self.dialog.mode.set("disable-preview")
        self.assertIn("None chosen yet", self.dialog.rules_text.get())
        self.dialog.prepare()
        self.assertIn("Choose the library rules", self.dialog.notice.get())
        self.dialog.window.destroy(); self.open([self.snapshot["rules"][0]])
        self.dialog.mode.set("disable-apply"); self.dialog.prepare()
        with patch("sentinel_app.duplicate_dialog.kit.confirm", return_value=False) as confirm:
            self.dialog.start()
        confirm.assert_called_once(); self.assertFalse(self.remote.dispatched)
        with patch("sentinel_app.duplicate_dialog.kit.confirm", return_value=True): self.dialog.start()
        self.assertEqual(self.remote.dispatched[0]["inputs"]["mode"], "disable-apply")

    def test_changing_a_choice_needs_a_new_check(self):
        self.dialog.prepare(); self.assertIsNotNone(self.dialog.record)
        self.dialog.similarity.set("95%")
        self.assertIsNone(self.dialog.record)
        self.assertEqual(str(self.dialog.start_button.cget("state")), "disabled")
        self.dialog.prepare()
        self.dialog.toggle(next(k for k, v in self.dialog.visible.items() if v == "example-primary"))
        self.assertIsNone(self.dialog.record)

    def test_show_last_run_reopens_the_saved_run(self):
        self.dialog.prepare(); self.dialog.start()
        self.dialog.close(); self.open()
        self.remote.run = dict(status="completed", conclusion="success")
        self.dialog.last_run()
        self.assertEqual(len(self.dialog.table.get_children()), 3)
        self.assertEqual(self.dialog.mode.get(), "scan")

    def test_row_button_shows_only_while_it_fits_and_opens_the_dialog(self):
        page = self.app.catalog_page
        page.snapshot = self.snapshot; page.sync_actions()
        self.assertEqual(str(page.duplicates_button.cget("state")), "normal")
        with patch.object(page.commands, "winfo_width", return_value=2000): page.fit_commands()
        self.assertTrue(page.duplicates_button.winfo_manager())
        with patch.object(page.commands, "winfo_width", return_value=200): page.fit_commands()
        self.assertFalse(page.duplicates_button.winfo_manager())  # Still in the Library ▾ menu.
        with patch("sentinel_app.catalog_page.DuplicateDialog") as dialog: page.duplicates_button.invoke()
        dialog.assert_called_once()

    def test_library_menu_opens_the_dialog_with_the_selected_rules(self):
        page = self.app.catalog_page
        page.snapshot = self.snapshot
        with patch.object(page.menu, "tk_popup"): page.library_menu()
        labels = [page.menu.entrycget(i, "label") for i in range(page.menu.index("end") + 1)
                  if page.menu.type(i) == "command"]
        self.assertIn("Find duplicate rules…", labels)
        with patch("sentinel_app.catalog_page.DuplicateDialog") as dialog, \
                patch.object(page, "selected_rules", return_value=[self.snapshot["rules"][0]]):
            page.find_duplicates()
        dialog.assert_called_once_with(self.app, self.snapshot, [self.snapshot["rules"][0]])
        page.snapshot = dict(self.snapshot, repository=None)
        with patch("sentinel_app.catalog_page.DuplicateDialog") as dialog: page.find_duplicates()
        dialog.assert_not_called(); self.assertIn("private GitHub repository", page.note.get())


if __name__ == "__main__":
    unittest.main()
