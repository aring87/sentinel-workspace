"""The Word report for duplicate runs, and the small .docx writer under it: offline, with no Word needed."""
import io
import re
import unittest
from xml.etree import ElementTree
import zipfile

from sentinel_app import brand, duplicate_report, word_document
from sentinel_app.duplicate_workflow import CLEANUP_FIELDS, FIELDS, SCAN_FIELDS, parse_report
from test_duplicate_workflow import AUDIT_ROWS, CLEANUP_ROWS, SCAN_ROWS, report

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def parts(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def text(data):
    """Every piece of text in the document body, joined."""
    root = ElementTree.fromstring(parts(data)["word/document.xml"])
    return " ".join(node.text or "" for node in root.iter(W + "t"))


def record(mode="audit", conclusion="success", **plan):
    return dict(plan=dict(dict(repository="example/detections", revision="a" * 40, identity="analyst", mode=mode,
                               every=False, targets=["client-0-primary", "example-primary"],
                               clients={"example-primary": "Example Corp"}, rules=[], extra_names=[],
                               inputs=dict(mode=mode, similarity="0.90", include_similar="false")), **plan),
                result=dict(run_id=501, run_number=12, state="Finished" if conclusion == "success" else "Failed",
                            conclusion=conclusion, url="https://github.com/example/detections/actions/runs/501",
                            checked_at="2026-11-16T19:42:00+00:00"))


def reports(kind="duplicates", mode="audit", rows=None, extra=None):
    if kind == "cleanup":
        fields, rows, name = CLEANUP_FIELDS, CLEANUP_ROWS if rows is None else rows, "workspace-cleanup.csv"
    elif kind == "scan":
        fields, rows, name = SCAN_FIELDS, SCAN_ROWS if rows is None else rows, "workspace-duplicates.csv"
    else:
        fields, rows, name = FIELDS, AUDIT_ROWS if rows is None else rows, "rule-duplicates.csv"
    files = {name: report(fields, rows), name.replace(".csv", ".xlsx"): b"x"}
    files.update(extra or {})
    return dict(files=files, name=name, artifact="duplicate-reports-" + mode + "-12", mode=mode, run_number=12,
                **parse_report(files[name]))


class WriterTests(unittest.TestCase):
    def test_a_document_is_a_complete_package_of_well_formed_parts(self):
        doc = word_document.Document("Title", header="Header", footer="Footer")
        doc.heading("Section"); doc.paragraph("Body"); doc.bullets(["one", "two"])
        doc.paragraph([doc.link("A link", "https://example.com/a?b=1&c=2"), doc.image(brand.render_png(32), 20)])
        doc.table([("A", 4000), ("B", 6080)], [["1", word_document.Cell("2", fill="FFE699", note="id")]])
        files = parts(doc.to_bytes())
        self.assertFalse([name for name in files if name.endswith("/")])  # No folder entries.
        for name in ("[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml", "word/numbering.xml",
                     "word/header1.xml", "word/footer1.xml", "word/_rels/document.xml.rels", "docProps/core.xml",
                     "word/media/image1.png"):
            self.assertIn(name, files)
        for name, data in files.items():
            if name.endswith((".xml", ".rels")): ElementTree.fromstring(data)  # Raises if malformed.
        rels = files["word/_rels/document.xml.rels"].decode()
        self.assertIn('Target="https://example.com/a?b=1&amp;c=2" TargetMode="External"', rels)
        self.assertIn('Target="media/image1.png"', rels)
        body = files["word/document.xml"].decode()
        self.assertIn('<w:gridCol w:w="4000"/><w:gridCol w:w="6080"/>', body)  # Widths on the grid…
        self.assertIn('<w:tcW w:w="6080" w:type="dxa"/>', body)  # …and on every cell.
        self.assertIn('w:val="clear"', body); self.assertNotIn('w:val="solid"', body)
        self.assertIn("<w:tblHeader/>", body)  # The header row repeats on each page.
        self.assertIn("NUMPAGES", files["word/footer1.xml"].decode())

    def test_any_text_is_escaped_and_characters_xml_cant_hold_are_dropped(self):
        doc = word_document.Document("A & B <c>")
        doc.paragraph("Rule \x00with\x0b <odd> & \"quoted\" text\nsecond line")
        files = parts(doc.to_bytes())
        root = ElementTree.fromstring(files["word/document.xml"])
        self.assertEqual([t.text for t in root.iter(W + "t")], ['Rule with <odd> & "quoted" text', "second line"])
        self.assertIn(b"A &amp; B &lt;c&gt;", files["docProps/core.xml"])

    def test_links_are_only_web_addresses(self):
        doc = word_document.Document("T")
        self.assertNotIn("hyperlink", doc.link("x", "file:///c:/secret"))
        self.assertEqual(doc.links, [])

    def test_png_size_refuses_other_images(self):
        self.assertEqual(word_document.png_size(brand.render_png(48)), (48, 48))
        with self.assertRaises(ValueError): word_document.png_size(b"GIF89a....")


class ReportTests(unittest.TestCase):
    def test_audit_report_has_the_summary_counts_steps_and_every_finding(self):
        data = duplicate_report.build(reports(), record(), logo=brand.render_png(96))
        body = text(data)
        for words in ("Duplicate rule audit", "Audit library rules (read-only)", "Possible copies", "Would be disabled",
                      "Results by client", "Recommended next steps", "Findings", "How duplicates are found",
                      "Example Corp (example-primary)", "client-0-primary", "Old copy", "Lookalike",
                      "Same name + Similar query (97%)", "Similar query (91%)"):
            self.assertIn(words, body)
        self.assertIn("run Preview disabling", body)  # A copy would be disabled: the next step says how.
        self.assertIn("Review the 1 rule matched only by a similar query", body)
        files = parts(data)
        self.assertIn("word/media/image1.png", files)
        self.assertIn(b"actions/runs/501", files["word/_rels/document.xml.rels"])
        self.assertIn(b"Duplicate rule audit", files["docProps/core.xml"])

    def test_colours_match_the_excel_report(self):
        body = parts(duplicate_report.build(reports(), record()))["word/document.xml"].decode()
        self.assertIn('w:fill="FFE699"', body)  # Would disable: amber.
        self.assertIn('w:fill="DDEBF7"', body)  # Review: blue.
        self.assertIn('w:fill="EDEDED"', body)  # Already disabled: grey.

    def test_scan_report_groups_rules_and_reads_the_reason_in_words(self):
        body = text(duplicate_report.build(reports("scan", "scan"), record("scan")))
        for words in ("Duplicate rule scan", "Rules in duplicate groups", "Groups with more than one enabled",
                      "Duplicate enabled", "Same name, similar query (98%)", "Same template", "Run Preview cleanup"):
            self.assertIn(words, body)

    def test_cleanup_report_lists_what_each_group_keeps_and_disables(self):
        body = text(duplicate_report.build(reports("cleanup", "cleanup-preview"), record("cleanup-preview")))
        for words in ("Duplicate cleanup preview", "Preview cleanup (read-only dry run)", "Kept enabled",
                      "Would be disabled", "Groups skipped", "Clean up duplicates would disable 1 rule",
                      "1 group was skipped", "Keep", "Would disable", "Run Clean up duplicates with the same clients",
                      "more than one enabled library rule", "the most recently modified rule"):
            self.assertIn(words, body)
        after = {"workspace-duplicates-after.csv": report(SCAN_FIELDS, SCAN_ROWS)}
        done = [dict(CLEANUP_ROWS[1], action="disabled")] + CLEANUP_ROWS[:1]
        body = text(duplicate_report.build(reports("cleanup", "cleanup-apply", rows=done, extra=after),
                                           record("cleanup-apply")))
        self.assertIn("Duplicate cleanup report", body)
        self.assertIn("This run disabled 1 rule.", body)
        self.assertIn("found 1 group still with more than one enabled rule", body)

    def test_disable_report_counts_what_changed_and_the_audit_after(self):
        rows = [dict(AUDIT_ROWS[0], action="disabled"), dict(AUDIT_ROWS[1], action="FAILED: 403 Forbidden")]
        after = {"rule-duplicates-after.csv": report(FIELDS, [dict(AUDIT_ROWS[1], action="disable")])}
        body = text(duplicate_report.build(reports(mode="disable-apply", rows=rows, extra=after), record("disable-apply")))
        self.assertIn("Disabled by this run", body)
        self.assertIn("It disabled 1 copy, and 1 failed.", body)
        self.assertIn("found 1 enabled copy still to disable", body)
        self.assertIn("couldn't be disabled", body)

    def test_a_failed_run_says_its_reports_may_be_incomplete(self):
        body = text(duplicate_report.build(reports(), record(conclusion="failure")))
        self.assertIn("may be incomplete", body)

    def test_an_empty_run_says_so_and_needs_no_action(self):
        body = text(duplicate_report.build(reports(rows=[]), record()))
        self.assertIn("None of the selected clients", body)
        self.assertIn("No action is needed", body)
        self.assertNotIn("Findings", body)

    def test_a_very_long_run_lists_the_first_rows_and_says_where_the_rest_are(self):
        many = [dict(AUDIT_ROWS[0], dup_rule_id=f"id-{i}") for i in range(duplicate_report.MAX_ROWS + 5)]
        body = text(duplicate_report.build(reports(rows=many), record()))
        self.assertIn(f"first {duplicate_report.MAX_ROWS} of {len(many)} rows", body)
        self.assertEqual(len(re.findall(r"id-\d+", body)), duplicate_report.MAX_ROWS)

    def test_file_name_follows_the_runs_csv(self):
        self.assertEqual(duplicate_report.file_name(reports()), "rule-duplicates.docx")
        self.assertEqual(duplicate_report.file_name(reports("scan", "scan")), "workspace-duplicates.docx")


if __name__ == "__main__":
    unittest.main()
