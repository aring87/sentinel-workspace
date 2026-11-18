"""Drift for every client at once: each enabled library target's workspace rules, read with the current
sign-in (Lighthouse reaches every delegated workspace from the managing tenant), compared with the library
as Drift compares one (rule_drift.drift). Read-only.

Each workspace is read by its resource URL, so nothing switches Azure CLI's current subscription and several
workspaces can be read at once."""
import csv
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

from .audit_evidence import ARM, CONFIGURATIONS
from .rule_drift import DIFFERENT, LIVE_ONLY, MISSING, drift

WORKERS = 6
MAX_PAGES = 50
FIELDS = ("target", "client", "missing", "different", "not_assigned", "not_in_library", "in_sync",
          "client_rules", "status")
HEADINGS = dict(target="Client target", client="Client", missing="Missing", different="Different",
                not_assigned="Not assigned here", not_in_library="Not in library", in_sync="In step",
                client_rules="Client rules not compared", status="Status")
OK = "Compared"


def workspace_of(target):
    """The workspace record a target file names, or None when it lacks the details to read it."""
    azure = (target.get("raw") or {}).get("azure") or {}
    fields = ("subscription_id", "resource_group", "workspace_name", "workspace_id")
    if not all(isinstance(azure.get(key), str) and azure[key].strip() for key in fields): return None
    resource = ("/subscriptions/" + azure["subscription_id"] + "/resourceGroups/" + azure["resource_group"] +
                "/providers/Microsoft.OperationalInsights/workspaces/" + azure["workspace_name"])
    return dict(tenant_id=str(azure.get("tenant_id") or ""), subscription_id=azure["subscription_id"],
                resource_group=azure["resource_group"], workspace_name=azure["workspace_name"],
                workspace_id=azure["workspace_id"], resource_id=resource)


def read_rules(session, workspace):
    """The workspace's analytics rules, at the version the pipeline deploys with, every page."""
    _, resource, version = CONFIGURATIONS["rules"]
    first = (ARM + quote(workspace["resource_id"], safe="/") + "/providers/Microsoft.SecurityInsights/" +
             resource + "?api-version=" + version)
    expected = urlsplit(first)
    url, records = first, []
    for _ in range(MAX_PAGES):
        parsed = urlsplit(url)
        if parsed.netloc != expected.netloc or parsed.path.lower() != expected.path.lower():
            raise ValueError("Azure returned a page link outside this workspace's rules.")
        page = session.az("rest", "--method", "get", "--url", url, "--resource", ARM + "/") or {}
        records += page.get("value", [])
        url = page.get("nextLink")
        if not url: return records
    raise ValueError("The workspace returned more rule pages than this view reads.")


def short(error):
    text = str(error).strip().splitlines()
    return (text[0] if text else type(error).__name__)[:200]


def summarize(target, result=None, status=OK):
    row = dict(target=target["target"], client=target.get("name") or (target.get("raw") or {}).get("client", ""),
               missing="", different="", not_assigned="", not_in_library="", in_sync="", client_rules="", status=status)
    if result is None: return row
    if result["status"] != "ok":
        return dict(row, status="Several library targets use this workspace" if result["status"] == "ambiguous"
                    else "The workspace didn't match its target file")
    entries = result["entries"]
    return dict(row, missing=sum(entry["kind"] == MISSING for entry in entries),
                different=sum(entry["kind"] == DIFFERENT for entry in entries),
                not_assigned=sum(entry["kind"] == LIVE_ONLY and entry["rule"] is not None for entry in entries),
                not_in_library=sum(entry["kind"] == LIVE_ONLY and entry["rule"] is None for entry in entries),
                in_sync=result["in_sync"], client_rules=result.get("client_rules", 0))


def fleet(snapshot, session, workers=WORKERS, read=read_rules):
    """One summary row per enabled library target, in library order."""
    targets = [client for client in snapshot["clients"] if client.get("state") == "Enabled"]

    def one(target):
        workspace = workspace_of(target)
        if workspace is None: return summarize(target, status="The target file has no workspace details")
        try:
            records = read(session, workspace)
        except Exception as error:  # One unreachable client never stops the others.
            return summarize(target, status="Couldn't read: " + short(error))
        return summarize(target, drift(snapshot, records, workspace))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, targets))


def attention(row):
    """Sort key: the clients with the most to fix first, then the unreadable ones, then the rest by name."""
    if row["status"] != OK: return (1, 0, row["target"].casefold())
    return (0, -(row["missing"] + row["different"]), -row["not_assigned"], row["target"].casefold())


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writerow(HEADINGS)
        writer.writerows(rows)
