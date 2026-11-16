import re
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sentinel_app import attack_catalog as catalog
from sentinel_app import desktop_app as ui
from sentinel_app import rule_builder_page as rule_builder
from sentinel_app import rule_schema
from sentinel_app import technique_picker as picker_module
from sentinel_app.technique_picker import PARENTS, SUBS
from test_rule_drafts import valid_form
from roomy_screen import roomy_screen, roomy_window

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_attack_catalog  # noqa: E402


class CatalogTests(unittest.TestCase):
    def test_the_catalog_is_sentinels_attack_version_and_hangs_together(self):
        self.assertEqual(catalog.VERSION, update_attack_catalog.SENTINEL_VERSION)
        ids = [technique for technique, *_ in catalog.TECHNIQUES]
        self.assertEqual(ids, sorted(set(ids)))  # In ID order, once each: sub-techniques follow their parent.
        for technique, name, tactics in catalog.TECHNIQUES:
            pattern = r"T\d{4}\.\d{3}" if "." in technique else r"T\d{4}"
            self.assertRegex(technique, "^" + pattern + "$")
            self.assertTrue(name and name == name.strip(), technique)
            self.assertTrue(set(tactics) <= rule_schema.TACTICS, technique)
            if "." in technique: self.assertIn(technique.split(".")[0], PARENTS)
        for technique, (name, replacement) in catalog.RETIRED.items():
            self.assertNotIn(technique, PARENTS.keys() | SUBS.keys())
            self.assertTrue(replacement is None or replacement in PARENTS.keys() | SUBS.keys(), technique)
        # The IDs the builder's examples and checks use are offered.
        for technique in ("T1562", "T1562.004", "T1003", "T1003.001", "T1110"): self.assertIn(technique, PARENTS.keys() | SUBS.keys())
        # ICS: its own techniques (no sub-techniques), under its own tactics too; T0849 is the quick checks' example.
        self.assertTrue(catalog.ICS and catalog.ICS <= PARENTS.keys())
        self.assertEqual(PARENTS["T0849"][0], "Masquerading"); self.assertIn("T0849", catalog.ICS)
        self.assertEqual(picker_module.name_of("T0849"), "Masquerading (ICS)")
        self.assertEqual(picker_module.name_of("T1036"), "Masquerading")
        ics_tactics = {tactic for technique in catalog.ICS for tactic in PARENTS[technique][1]}
        self.assertTrue({"ImpairProcessControl", "InhibitResponseFunction"} <= ics_tactics)
        # ICS's deprecated techniques keep their IDs under 'mitre-ics-attack': they are retired, not unknown.
        self.assertEqual(catalog.RETIRED["T0818"], ("Engineering Workstation Compromise", None))
        self.assertTrue(any(technique.startswith("T08") for technique in catalog.RETIRED))

    def test_the_generator_reads_attack_stix(self):
        ref = lambda technique: [{"source_name": "mitre-attack", "external_id": technique}]
        bundle = {"objects": [
            {"type": "x-mitre-collection", "id": "x", "x_mitre_version": "99.0"},
            {"type": "attack-pattern", "id": "a", "name": "Brute Force", "external_references": ref("T1110"),
             "kill_chain_phases": [{"kill_chain_name": "mitre-attack", "phase_name": "credential-access"}]},
            {"type": "attack-pattern", "id": "b", "name": "Password Spraying ", "external_references": ref("T1110.003"),
             "kill_chain_phases": [{"kill_chain_name": "mitre-attack", "phase_name": "credential-access"}]},
            {"type": "attack-pattern", "id": "c", "name": "Hide", "external_references": ref("T1700"),
             "kill_chain_phases": [{"kill_chain_name": "mitre-attack", "phase_name": "stealth"},
                                   {"kill_chain_name": "mitre-attack", "phase_name": "persistence"}]},
            {"type": "attack-pattern", "id": "d", "name": "Old", "revoked": True, "external_references": ref("T1001")},
            {"type": "attack-pattern", "id": "e", "name": "Gone", "x_mitre_deprecated": True, "external_references": ref("T1002")},
            # A chain: T1003 was revoked by T1001, itself revoked by T1110.003; T1004 by a deprecated one.
            {"type": "attack-pattern", "id": "f", "name": "Older", "revoked": True, "external_references": ref("T1003")},
            {"type": "attack-pattern", "id": "g", "name": "Lost", "revoked": True, "external_references": ref("T1004")},
            {"type": "relationship", "id": "r", "relationship_type": "revoked-by", "source_ref": "d", "target_ref": "b"},
            {"type": "relationship", "id": "s", "relationship_type": "revoked-by", "source_ref": "f", "target_ref": "d"},
            {"type": "relationship", "id": "t", "relationship_type": "revoked-by", "source_ref": "g", "target_ref": "e"}]}
        ics = {"objects": [
            {"type": "x-mitre-collection", "id": "y", "x_mitre_version": "99.0"},
            {"type": "attack-pattern", "id": "i", "name": "Masquerading", "external_references": ref("T0849"),
             "kill_chain_phases": [{"kill_chain_name": "mitre-ics-attack", "phase_name": "evasion"}]},
            {"type": "attack-pattern", "id": "j", "name": "Modify Parameter", "external_references": ref("T0836"),
             "kill_chain_phases": [{"kill_chain_name": "mitre-ics-attack", "phase_name": "impair-process-control"}]},
            {"type": "attack-pattern", "id": "k", "name": "Role Identification", "x_mitre_deprecated": True,
             "external_references": [{"source_name": "mitre-ics-attack", "external_id": "T0850"}]}]}
        version, techniques, retired, ics_ids = update_attack_catalog.catalog(bundle, ics)
        self.assertEqual(version, "99.0")
        self.assertEqual(techniques, [("T0836", "Modify Parameter", ("ImpairProcessControl",)),
                                      ("T0849", "Masquerading", ("DefenseEvasion",)),
                                      ("T1110", "Brute Force", ("CredentialAccess",)),
                                      ("T1110.003", "Password Spraying", ("CredentialAccess",)),
                                      ("T1700", "Hide", ("Persistence", "DefenseEvasion"))])
        self.assertEqual(ics_ids, {"T0836", "T0849"})
        self.assertEqual(retired, {"T0850": ("Role Identification", None),
                                   "T1001": ("Old", "T1110.003"), "T1002": ("Gone", None),
                                   "T1003": ("Older", "T1110.003"), "T1004": ("Lost", None)})
        self.assertEqual(catalog.RETIRED["T1150"], ("Plist Modification", "T1647"))  # Two hops in ATT&CK 13.1.
        rendered = update_attack_catalog.render(version, techniques, retired, ics_ids)
        scope = {}; exec(compile(rendered, "attack_catalog", "exec"), scope)
        self.assertEqual((scope["VERSION"], list(scope["TECHNIQUES"]), scope["RETIRED"], scope["ICS"]),
                         (version, techniques, retired, ics_ids))
        # Files of different versions, or one technique in two files, are refused.
        newer = {"objects": [{**ics["objects"][0], "x_mitre_version": "100.0"}]}
        with self.assertRaises(SystemExit): update_attack_catalog.catalog(bundle, newer)
        with self.assertRaises(SystemExit): update_attack_catalog.catalog(ics, ics)


class TechniquePickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        p = patch.object(ui, "DATA", Path(self.temp.name)); p.start(); self.addCleanup(p.stop)
        roomy_screen(self)
        self.root = tk.Tk(); self.root.withdraw(); roomy_window(self.root)
        self.app = ui.App(self.root); self.root.update()
        self.page = self.app.rule_builder
        self.picker = self.page.picker
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.app.set_busy(False)
        for timer in self.root.tk.call("after", "info"): self.root.after_cancel(timer)
        self.root.destroy()

    def fields(self):
        return self.page.vars["techniques"].get(), self.page.vars["subTechniques"].get()

    def shown(self, parent=""):
        return list(self.picker.tree.get_children(parent))

    def test_a_draft_ticks_its_techniques_and_opens_their_parents(self):
        self.page.load_form(valid_form())  # DefenseEvasion; T1562; T1562.004.
        tree = self.picker.tree
        self.assertEqual(self.picker.ticked, {"T1562", "T1562.004"})
        self.assertTrue(tree.item("T1562", "open"))
        self.assertEqual(tuple(tree.item("T1562.004", "tags")), ("picked",))
        # The parent's other sub-techniques are tinted: the ones worth a look.
        others = [sub for sub in self.shown("T1562") if sub != "T1562.004"]
        self.assertTrue(others)
        self.assertTrue(all(tuple(tree.item(sub, "tags")) == ("suggest",) for sub in others))
        self.assertTrue(self.picker.summary.get().startswith("Selected: T1562 Impair Defenses, T1562.004 Disable"))
        self.assertFalse(self.picker.unknown.winfo_manager())
        self.assertFalse(self.page.dirty())

    def test_ticking_a_sub_technique_ticks_its_parent_and_unticking_a_parent_clears_its_subs(self):
        self.page.load_form(valid_form())
        self.picker.only.set(False)
        self.picker.toggle("T1003.001")
        self.assertEqual(self.fields(), ("T1562, T1003", "T1562.004, T1003.001"))
        self.assertTrue(self.page.dirty())
        self.picker.toggle("T1562")
        self.assertEqual(self.fields(), ("T1003", "T1003.001"))
        self.picker.toggle("T1110")  # A parent opens and tints its sub-techniques.
        self.assertEqual(self.fields(), ("T1003, T1110", "T1003.001"))
        self.assertTrue(self.picker.tree.item("T1110", "open"))
        self.assertEqual(tuple(self.picker.tree.item("T1110.003", "tags")), ("suggest",))
        self.picker.clear()
        self.assertEqual(self.fields(), ("", ""))
        self.assertTrue(self.picker.summary.get().startswith("None selected"))

    def test_the_checks_pass_with_what_the_picker_writes(self):
        form = valid_form(); form.update(techniques="", subTechniques="")
        self.page.load_form(form)
        self.picker.toggle("T1562.004")
        self.assertIsNotNone(self.page.validate())

    def test_search_finds_ids_and_names_and_opens_parents_for_matching_sub_techniques(self):
        self.page.load_form({**valid_form(), "tactics": "", "techniques": "", "subTechniques": ""})
        picker = self.picker
        self.assertEqual(len(self.shown()), len(PARENTS))
        picker.search.set("lsass"); picker.render()  # LSASS Memory, and LSASS Driver under Boot or Logon Autostart.
        self.assertEqual(self.shown(), ["T1003", "T1547"])
        self.assertEqual((self.shown("T1003"), self.shown("T1547")), (["T1003.001"], ["T1547.008"]))
        self.assertTrue(picker.tree.item("T1003", "open"))
        picker.search.set("t1110"); picker.render()  # A parent that matches brings all its sub-techniques.
        self.assertEqual(self.shown(), ["T1110"])
        self.assertEqual(self.shown("T1110"), PARENTS["T1110"][2])
        picker.search.set("no such technique"); picker.render()
        self.assertEqual((self.shown(), picker.count.cget("text")), ([], "No techniques match"))

    def test_the_ticked_tactics_narrow_the_list_but_ticked_techniques_stay(self):
        self.page.load_form({**valid_form(), "tactics": "CredentialAccess"})
        listed = set(self.shown())
        self.assertIn("T1110", listed); self.assertIn("T1562", listed)  # T1562 is ticked.
        self.assertNotIn("T1059", listed)  # Execution.
        self.assertTrue(all(set(PARENTS[x][1]) & {"CredentialAccess"} or x == "T1562" or
                            any("CredentialAccess" in SUBS[s][1] for s in PARENTS[x][2]) for x in listed))
        self.assertIn("for the ticked tactics", self.picker.count.cget("text"))
        self.picker.only.set(False)
        self.assertEqual(len(self.shown()), len(PARENTS))
        self.assertIn(f"ATT&CK {catalog.VERSION}", self.picker.count.cget("text"))

    def test_ids_the_list_lacks_are_kept_named_and_can_be_replaced_or_removed(self):
        self.page.load_form({**valid_form(), "techniques": "T1562, T1086, T0999, T0849", "subTechniques": "T1562.004"})
        picker = self.picker
        self.assertTrue(picker.unknown.winfo_manager())
        text = picker.unknown_text.get()
        self.assertIn("Retired from ATT&CK: T1086 PowerShell (now T1059.001)", text)
        self.assertIn(f"Not in ATT&CK {catalog.VERSION} (Enterprise or ICS), kept as written: T0999", text)
        self.assertIn("T0849", picker.ticked)  # ICS's Masquerading is in the list.
        self.assertIn("T0849 Masquerading (ICS)", picker.summary.get())
        self.assertTrue(picker.replace_button.winfo_manager())
        picker.replace()
        self.assertEqual(self.fields(), ("T1562, T0999, T0849, T1059", "T1562.004, T1059.001"))
        self.assertFalse(picker.replace_button.winfo_manager())
        picker.remove()
        self.assertEqual(self.fields(), ("T1562, T0849, T1059", "T1562.004, T1059.001"))
        self.assertFalse(picker.unknown.winfo_manager())

    def test_a_flagged_technique_outlines_the_picker_with_its_advice_under_the_remove(self):
        self.page.load_form({**valid_form(), "techniques": "T1562, Bogus"})
        with patch("sentinel_app.rule_builder_page.kit.alert"): self.assertIsNone(self.page.validate())
        self.assertIn("techniques", self.page.flagged)
        self.assertEqual(int(self.picker.frame.cget("highlightthickness")), 2)
        label = self.page.flag_labels["techniques"]
        slaves = label.master.pack_slaves()
        self.assertEqual(slaves.index(label), slaves.index(self.picker.unknown) + 1)
        self.picker.remove()
        self.assertNotIn("techniques", self.page.flagged)
        self.assertEqual(int(self.picker.frame.cget("highlightthickness")), 1)

    def test_a_task_running_blocks_ticking(self):
        self.page.load_form(valid_form())
        self.app.set_busy(True, immediate=True)
        self.picker.toggle("T1003")
        self.assertEqual(self.fields(), ("T1562", "T1562.004"))

    def test_tactic_text_shortens_several_tactics_and_names_a_ticked_one_first(self):
        tactics = ("Persistence", "PrivilegeEscalation", "DefenseEvasion")
        self.assertEqual(picker_module.tactic_text(tactics), "Persistence +2")
        self.assertEqual(picker_module.tactic_text(tactics, {"DefenseEvasion"}), "Defense Evasion +2")
        self.assertEqual(picker_module.tactic_text(("CommandAndControl",)), "Command and Control")
        self.assertTrue(all(re.fullmatch(r"T\d{4}", x) for x in PARENTS))

    def test_a_flag_keeps_its_red_outline_through_focus_and_while_either_field_is_flagged(self):
        frame, danger = self.picker.frame, ui.COLORS["danger"]
        self.page.load_form({**valid_form(), "techniques": "T1110", "subTechniques": "T1562.004, Foo"})
        with patch("sentinel_app.rule_builder_page.kit.alert"): self.assertIsNone(self.page.validate())
        self.assertEqual(set(self.page.flagged) & {"techniques", "subTechniques"}, {"techniques", "subTechniques"})
        for sequence in ("<FocusIn>", "<FocusOut>"):
            self.picker.tree.event_generate(sequence)
            self.assertEqual((frame.cget("highlightbackground"), int(frame.cget("highlightthickness"))), (danger, 2))
        self.picker.toggle("T1562")  # Resolves Techniques only: Sub-techniques still holds Foo.
        self.assertNotIn("techniques", self.page.flagged); self.assertIn("subTechniques", self.page.flagged)
        self.assertEqual((frame.cget("highlightbackground"), int(frame.cget("highlightthickness"))), (danger, 2))
        self.picker.remove()
        self.assertEqual(int(frame.cget("highlightthickness")), 1)
        self.picker.tree.event_generate("<FocusIn>")
        self.assertEqual(frame.cget("highlightbackground"), ui.COLORS["accent"])

    def test_a_click_beside_a_rows_box_ticks_it_unless_it_opens_a_parent(self):
        self.page.load_form(valid_form())
        tree, event = self.picker.tree, SimpleNamespace(x=25, y=10)
        for item, children, ticks in (("T1562.001", (), True), ("T1110", ("T1110.001",), False)):
            with patch.object(tree, "identify_region", return_value="tree"), \
                    patch.object(tree, "identify_element", return_value="Treeitem.indicator"), \
                    patch.object(tree, "identify_row", return_value=item), \
                    patch.object(tree, "get_children", return_value=children):
                self.picker.clicked(event)
            self.assertEqual(item in self.picker.ticked, ticks, item)

    def test_unticking_a_parent_takes_its_unlisted_sub_techniques_too(self):
        self.page.load_form({**valid_form(), "techniques": "T1562, T1003", "subTechniques": "T1562.004, T1562.999, T1003.001"})
        self.picker.toggle("T1562")
        self.assertEqual(self.fields(), ("T1003", "T1003.001"))

    def test_ids_in_the_wrong_field_and_missing_parents_are_named_and_fixed(self):
        self.page.load_form({**valid_form(), "techniques": "T1562.004", "subTechniques": "T1562, T1003.001"})
        picker = self.picker
        text = picker.unknown_text.get()
        self.assertIn("In the wrong field: T1562.004 (a sub-technique, listed as a technique), "
                      "T1562 (a technique, listed as a sub-technique)", text)
        self.assertIn("Parent not ticked: T1003 OS Credential Dumping", text)
        self.assertTrue(picker.fix_button.winfo_manager()); self.assertFalse(picker.remove_button.winfo_manager())
        advice = dict(rule_builder.collect_issues(self.page.form()))
        self.assertIn("Fix these", advice["techniques"])
        picker.fix()  # Each field keeps its own values first; the moved ones follow.
        self.assertEqual(self.fields(), ("T1562, T1003", "T1003.001, T1562.004"))
        self.assertFalse(picker.unknown.winfo_manager())
        self.assertNotIn("techniques", dict(rule_builder.collect_issues(self.page.form())))

    def test_ics_tactics_list_ics_techniques_and_the_search_finds_them(self):
        self.page.load_form({**valid_form(), "tactics": "InhibitResponseFunction", "techniques": "", "subTechniques": ""})
        listed = self.shown()
        self.assertTrue(listed)
        self.assertTrue(all(x in catalog.ICS and "InhibitResponseFunction" in PARENTS[x][1] for x in listed), listed)
        self.picker.only.set(False)
        self.picker.search.set("masquerading"); self.picker.render()
        self.assertEqual(self.shown(), ["T0849", "T1036"])  # ICS's and Enterprise's, told apart by (ICS).
        self.assertEqual(self.picker.tree.item("T0849", "values")[0], "Masquerading (ICS)")
        self.picker.search.set("ics"); self.picker.render()
        self.assertEqual(set(self.shown()), set(catalog.ICS))

    def test_a_whole_id_the_list_lacks_can_be_added_from_the_search(self):
        self.page.load_form({**valid_form(), "techniques": "", "subTechniques": ""})
        picker = self.picker
        picker.search.set("t0999"); picker.render()
        self.assertEqual(self.shown(), [])
        self.assertEqual(picker.count_full,
                         f"T0999 isn't in the ATT&CK {catalog.VERSION} Enterprise or ICS list: Add keeps it as typed")
        self.assertEqual((picker.add_button.cget("text"), bool(picker.add_button.winfo_manager())), ("Add T0999", True))
        picker.add_typed()
        self.assertEqual((self.fields(), picker.search.get()), (("T0999", ""), ""))
        picker.render(); self.assertFalse(picker.add_button.winfo_manager())
        self.assertIn("kept as written: T0999", picker.unknown_text.get())
        picker.search.set("T0849"); picker.render()  # An ICS one the list holds: tick it, nothing to add.
        self.assertFalse(picker.add_button.winfo_manager()); self.assertEqual(self.shown(), ["T0849"])
        picker.search.set("T1110"); picker.render()  # One the list holds: nothing to add.
        self.assertFalse(picker.add_button.winfo_manager())
        picker.search.set("T1086"); picker.render()  # A retired one says what replaced it.
        self.assertEqual(picker.count_full, "T1086 PowerShell is retired from ATT&CK: search T1059.001 instead")


if __name__ == "__main__":
    unittest.main()
