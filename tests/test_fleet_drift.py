"""All clients drift: every enabled target read and compared, offline with in-memory fakes only."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel_app import fleet_drift
from sentinel_app.fleet_drift import OK, attention, fleet, read_rules, workspace_of, write_csv
from test_rule_drift import A, B, D, MANAGING, RECORDS, library, live, target_file

FAR = "34343434-3434-4343-8343-343434343434"


def snapshot():
    """acme (the drift fixture's workspace), bluestone (no subscription details) and far (in step)."""
    return library(extra={"clients/far/far-primary.yml": target_file(
        "far-primary", dict(tenant_id=MANAGING, subscription_id="56565656-5656-4565-8565-565656565656",
                            resource_group="rg-far", workspace_name="law-far", workspace_id=FAR),
        [dict(path="rules/sentinel/a.yml")])},
        acme_azure=dict(tenant_id=MANAGING, subscription_id="12121212-0000-4000-8000-000000000000",
                        resource_group="rg-acme", workspace_name="law-acme",
                        workspace_id="11111111-2222-4333-8444-555555555555"))


class Session:
    def __init__(self, pages): self.pages, self.calls = pages, []

    def az(self, *args):
        self.calls.append(args)
        return self.pages.pop(0)


class FleetTests(unittest.TestCase):
    def test_workspace_comes_from_the_target_file(self):
        snap = snapshot()
        far = next(c for c in snap["clients"] if c["target"] == "far-primary")
        self.assertEqual(workspace_of(far)["resource_id"],
                         "/subscriptions/56565656-5656-4565-8565-565656565656/resourceGroups/rg-far"
                         "/providers/Microsoft.OperationalInsights/workspaces/law-far")
        blue = next(c for c in snap["clients"] if c["target"] == "bluestone-primary")
        self.assertIsNone(workspace_of(blue))  # No subscription or resource group in its file.

    def test_reads_every_page_and_refuses_links_elsewhere(self):
        workspace = workspace_of(next(c for c in snapshot()["clients"] if c["target"] == "far-primary"))
        base = fleet_drift.ARM + workspace["resource_id"] + "/providers/Microsoft.SecurityInsights/alertRules"
        session = Session([{"value": [1], "nextLink": base + "?api-version=x&$skipToken=2"}, {"value": [2]}])
        self.assertEqual(read_rules(session, workspace), [1, 2])
        self.assertIn("2023-12-01-preview", session.calls[0][4])  # The pipeline's deploy version.
        session = Session([{"value": [1], "nextLink": "https://management.azure.com/subscriptions/other/alertRules"}])
        with self.assertRaisesRegex(ValueError, "outside this workspace"):
            read_rules(session, workspace)

    def test_one_row_per_enabled_target_and_one_failure_stops_nothing(self):
        def read(session, workspace):
            if workspace["workspace_name"] == "law-far": raise RuntimeError("AuthorizationFailed\nmore detail")
            return RECORDS
        rows = {row["target"]: row for row in fleet(snapshot(), object(), workers=2, read=read)}
        self.assertEqual(set(rows), {"acme-primary", "bluestone-primary", "far-primary"})
        acme = rows["acme-primary"]
        self.assertEqual((acme["status"], acme["missing"], acme["different"], acme["not_assigned"],
                          acme["not_in_library"], acme["in_sync"]), (OK, 1, 1, 1, 1, 1))
        self.assertEqual(rows["far-primary"]["status"], "Couldn't read: AuthorizationFailed")
        self.assertEqual(rows["bluestone-primary"]["status"], "The target file has no workspace details")
        order = [row["target"] for row in sorted(rows.values(), key=attention)]
        self.assertEqual(order[0], "acme-primary")  # The client with the most to fix leads.

    def test_csv_has_a_heading_row_and_every_client(self):
        rows = fleet(snapshot(), object(), workers=1, read=lambda session, workspace: [live(A, "Alpha")])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "drift.csv"
            write_csv(path, rows)
            lines = list(csv.reader(path.open(encoding="utf-8")))
        self.assertEqual(lines[0][:3], ["Client target", "Client", "Missing"])
        self.assertEqual(len(lines), 1 + len(rows))


if __name__ == "__main__":
    unittest.main()
