"""Find duplicate analytics rules across client workspaces with the rule library's own GitHub workflow.

The app starts the library's sentinel-rule-duplicates.yml on main, follows the run, and downloads the
reports the run uploads: for each pass a CSV and a colour-coded Excel workbook (scripts/rule_duplicates.py
in the detection repository writes both). The workflow signs in with the pipeline's identity and reads
every selected workspace, so the results match a run started from GitHub. Nothing here reads or changes
Azure: the disable modes change workspaces only through that workflow, and Disable duplicates runs in the
library's production environment, where GitHub asks for its approval first.

The run's reports are untrusted input: only top-level .csv and .xlsx files are taken from the artifact,
each within a size limit, and the CSV must have the columns the script writes."""
import base64
import copy
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile

from .preview_workflow import api
from .repository_catalog import GitHubReader, parse_yaml, MAX_FILE
from .repository_reviews import fingerprint, require, ReviewError
from .rule_drafts import atomic_write

WORKFLOW = "sentinel-rule-duplicates.yml"
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW
MODES = ("scan", "audit", "disable-preview", "disable-apply")
# The workflow's dispatch inputs and their types. Any other input could change what a run does, so a
# workflow with different inputs is refused until this adapter is updated (as for previews).
INPUTS = dict(mode="choice", rules="string", targets="string", extra_names="string", include_similar="boolean",
              similarity="string")
# The script's report columns (rule_duplicates.FIELDS and SCAN_FIELDS).
FIELDS = ["target", "repo_rule", "repo_rule_id", "repo_copy", "dup_rule_id", "dup_name", "dup_enabled",
          "dup_managed_by", "match", "similarity", "action"]
SCAN_FIELDS = ["target", "group", "rule_id", "name", "kind", "enabled", "template", "repo_managed", "group_match",
               "enabled_in_group"]
ARTIFACT_PREFIX = "duplicate-reports-"
MAX_ARTIFACT = 50 * 1024 * 1024
MAX_REPORT = 25 * 1024 * 1024
MAX_TARGETS = 500
REPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(csv|xlsx)")
RECEIPT_SUFFIX = ".duplicates.json"
TARGET = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def api_bytes(endpoint):
    """A GitHub API response as raw bytes (an artifact's ZIP); GitHub CLI follows the download redirect."""
    executable = shutil.which("gh")
    require(executable, "Install GitHub CLI and sign in with gh auth login.")
    args = [executable, "api", "--hostname", "github.com", "-H", "X-GitHub-Api-Version: 2026-03-10", endpoint]
    try:
        result = subprocess.run(args, capture_output=True, timeout=180,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        raise ReviewError("GitHub took too long to send the reports. Select Refresh status to try again.") from error
    require(result.returncode == 0, "GitHub couldn't send the run's reports. Check sign-in and access to the "
            "repository's Actions, then select Refresh status.")
    require(len(result.stdout) <= MAX_ARTIFACT, "The run's reports are larger than this app reads. Open the run in GitHub.")
    return result.stdout


def check_inputs(workflow):
    """Refuse a workflow whose dispatch inputs differ from the ones this adapter sends."""
    event = (workflow.get("on") or {}).get("workflow_dispatch") if isinstance(workflow, dict) else None
    require(isinstance(event, dict), "The duplicate workflow must support workflow_dispatch.")
    inputs = event.get("inputs")
    require(isinstance(inputs, dict) and all(isinstance(v, dict) for v in inputs.values()), "Invalid duplicate workflow inputs.")
    require(set(inputs) == set(INPUTS), "The duplicate workflow's inputs changed. Update this app's duplicate adapter "
            "before running it.")
    for key, kind in INPUTS.items():
        require(inputs[key].get("type", "string") == kind, "The duplicate workflow's " + key + " input changed type.")
    options = inputs["mode"].get("options")
    require(isinstance(options, list) and all(mode in options for mode in MODES),
            "The duplicate workflow needs the modes " + ", ".join(MODES) + ".")


def split_names(text):
    """Display names typed one per line (or separated by semicolons), cleaned; the workflow takes them
    comma-separated, so a name can't hold a comma."""
    names = [part.strip() for part in re.split(r"[\n;]", text or "") if part.strip()]
    require(all("," not in name for name in names), "A rule name to treat as a copy can't contain a comma: "
            "the workflow takes the names comma-separated.")
    require(all(len(name) <= 256 for name in names), "A rule name to treat as a copy is longer than Sentinel allows.")
    return list(dict.fromkeys(names))


def state_text(run):
    """What a run is doing, in words."""
    status, conclusion = run.get("status"), run.get("conclusion")
    if status == "completed":
        return {"success": "Finished", "failure": "Failed", "cancelled": "Cancelled", "timed_out": "Timed out",
                "action_required": "Needs action in GitHub"}.get(conclusion, str(conclusion or "Finished").replace("_", " ").capitalize())
    return {"queued": "Queued", "pending": "Queued", "requested": "Queued", "waiting": "Waiting for approval in GitHub",
            "in_progress": "Running"}.get(status, str(status or "Unknown").replace("_", " ").capitalize())


class DuplicateService:
    def __init__(self, request=None, download=None):
        self.request = request or api
        self.download = download or api_bytes

    # ------------------------------------------------------------ prepare
    def read_workflow(self, snapshot, repository):
        entry = snapshot["entries"].get(WORKFLOW_PATH, {})
        require(entry.get("type") == "blob", "The rule library has no " + WORKFLOW_PATH + " on main. Merge the duplicate "
                "workflow into the library first.")
        require(entry.get("mode") in ("100644", "100755") and entry.get("size", MAX_FILE + 1) <= MAX_FILE,
                "The duplicate workflow is not an ordinary supported file.")
        blob = self.request("GET", "repos/" + repository + "/git/blobs/" + entry["sha"])
        require(blob.get("encoding") == "base64", "Unsupported workflow encoding.")
        return parse_yaml(base64.b64decode(blob["content"]), yaml12=True)

    def prepare(self, repository, revision, mode, targets, rules=(), extra_names=(), include_similar=False,
                similarity=0.9):
        """Check a run against the library on GitHub now, and return the request to start: nothing starts here."""
        require(mode in MODES, "Unsupported duplicate mode.")
        require(isinstance(similarity, (int, float)) and not isinstance(similarity, bool) and 0.5 <= similarity <= 1,
                "Choose a query similarity between 50% and 100%.")
        snapshot = GitHubReader(lambda endpoint: self.request("GET", endpoint)).load(repository)
        require(snapshot["private"] is True and snapshot["can_push"] is True,
                "Use a private detection repository where your GitHub account has write access: starting a workflow needs it.")
        require(snapshot["base_branch"] == "main", "The duplicate workflow runs from main, and this repository's default "
                "branch isn't main.")
        require(snapshot["revision"] == revision, "The repository changed. Select Refresh library, then Check again.")
        enabled = {c["target"]: c for c in snapshot["clients"] if c["raw"].get("enabled") is True}
        require(enabled, "The library has no enabled client targets to check.")
        targets = sorted(set(targets or []))
        require(targets, "Select at least one client.")
        require(len(targets) <= MAX_TARGETS, f"Select up to {MAX_TARGETS} clients.")
        require(all(t in enabled for t in targets), "Every selected client must be enabled in the library. "
                "The workflow skips switched-off clients.")
        paths = {r["path"] for r in snapshot["rules"]}
        rules = sorted(set(rules or [])) if mode != "scan" else []
        require(all(p in paths for p in rules), "A selected rule isn't in the library on main. Select Refresh library.")
        require(all("," not in p for p in rules), "A rule path holds a comma, which the workflow can't take.")
        if mode.startswith("disable"):
            require(rules, "Choose the library rules whose copies to disable: select them in the Rule library, then "
                    "open Find duplicates again.")
        typed = extra_names if isinstance(extra_names, str) else "\n".join(extra_names or ())
        names = split_names(typed) if mode != "scan" else []
        workflow = self.read_workflow(snapshot, repository)
        check_inputs(workflow)
        info = self.request("GET", "repos/" + repository + "/actions/workflows/" + WORKFLOW)
        require(info.get("path") == WORKFLOW_PATH and info.get("state") == "active", "The duplicate workflow is not "
                "active in GitHub. Enable it under Actions.")
        every = targets == sorted(enabled)
        inputs = dict(mode=mode, rules=",".join(rules), targets="all" if every else ",".join(targets),
                      extra_names=",".join(names),
                      include_similar="true" if include_similar and mode.startswith("disable") else "false",
                      similarity=f"{similarity:.2f}")
        names_by_target = {t: enabled[t]["name"] for t in targets}
        plan = dict(repository=repository, repository_id=snapshot["repository_id"], revision=revision,
                    identity=snapshot["identity"], branch="main", workflow_id=info["id"], mode=mode, inputs=inputs,
                    targets=targets, every=every, clients=names_by_target, rules=rules, extra_names=names)
        return dict(version=1, request_id=uuid.uuid4().hex, plan=plan, fingerprint=fingerprint(plan), attempted=False,
                    created_at=datetime.now(timezone.utc).isoformat(), result=None)

    # ------------------------------------------------------------ run
    def save(self, record, folder):
        require(re.fullmatch(r"[0-9a-f]{32}", record["request_id"]), "Invalid workflow request ID.")
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        atomic_write(folder / (record["request_id"] + RECEIPT_SUFFIX), json.dumps(record, indent=2) + "\n")

    def verify(self, plan):
        prefix = "repos/" + plan["repository"]
        metadata = self.request("GET", prefix)
        require(metadata.get("private") is True and metadata.get("id") == plan["repository_id"] and
                metadata.get("permissions", {}).get("push") is True and metadata.get("default_branch") == "main",
                "Repository identity, access or default branch changed. Check again.")
        require(self.request("GET", "user").get("login") == plan["identity"], "GitHub account changed. Check again.")
        require(self.request("GET", prefix + "/commits/main").get("sha") == plan["revision"],
                "Main changed after the check. Select Refresh library, then Check again.")
        info = self.request("GET", prefix + "/actions/workflows/" + WORKFLOW)
        require(info.get("path") == WORKFLOW_PATH and info.get("id") == plan["workflow_id"] and info.get("state") == "active",
                "The duplicate workflow changed or was disabled. Check again.")

    def dispatch(self, record, folder):
        """Start the run once. The request is saved before GitHub is asked, so an unclear answer is never retried."""
        plan = record["plan"]
        require(record["fingerprint"] == fingerprint(plan), "The request changed. Check again.")
        require(not record["attempted"], "This request was already sent. Check its run before starting another.")
        require(plan["branch"] == "main" and plan["mode"] in MODES and plan["inputs"]["mode"] == plan["mode"],
                "Only duplicate runs from main are supported.")
        self.verify(plan)
        record["attempted"] = True
        record["result"] = dict(state="Submission unconfirmed")
        self.save(record, folder)
        response = self.request("POST", "repos/" + plan["repository"] + "/actions/workflows/" + WORKFLOW + "/dispatches",
                                {"ref": "main", "inputs": copy.deepcopy(plan["inputs"])})
        if response is None:
            record["result"] = dict(state="Started; GitHub didn't return the run. Open in GitHub to follow it.")
        else:
            run_id = response.get("workflow_run_id")
            require(type(run_id) is int and run_id > 0, "GitHub didn't return a valid run ID. Check Actions before "
                    "starting another run.")
            record["result"] = dict(run_id=run_id, state="Queued",
                                    url="https://github.com/" + plan["repository"] + "/actions/runs/" + str(run_id))
        self.save(record, folder)
        return record

    def read_run(self, plan, run_id):
        run = self.request("GET", "repos/" + plan["repository"] + "/actions/runs/" + str(run_id))
        require(run.get("id") == run_id and run.get("workflow_id") == plan["workflow_id"] and
                run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main" and
                (run.get("repository") or {}).get("id") == plan["repository_id"],
                "GitHub returned a run that doesn't match this request.")
        return run

    def refresh(self, record, folder):
        """Read the run's status again."""
        plan, result = record["plan"], record.get("result") or {}
        require(record["fingerprint"] == fingerprint(plan), "The saved request changed.")
        require(result.get("run_id"), "GitHub didn't return this run's ID. Open in GitHub to follow it.")
        run = self.read_run(plan, result["run_id"])
        result.update(state=state_text(run), run_status=run.get("status"), conclusion=run.get("conclusion"),
                      run_number=run.get("run_number"), actual_revision=run.get("head_sha"),
                      checked_at=datetime.now(timezone.utc).isoformat())
        if run.get("head_sha") and run["head_sha"] != plan["revision"]:
            result["state"] += " (main changed before the run started)"
        record["result"] = result
        self.save(record, folder)
        return record

    def reports(self, record):
        """The finished run's reports: dict(artifact, files={name: bytes}). Reads only; saves nothing."""
        plan, result = record["plan"], record.get("result") or {}
        require(result.get("run_status") == "completed", "The run hasn't finished yet.")
        listing = self.request("GET", "repos/" + plan["repository"] + "/actions/runs/" + str(result["run_id"])
                               + "/artifacts?per_page=100") or {}
        found = [a for a in listing.get("artifacts") or [] if isinstance(a, dict)
                 and str(a.get("name", "")).startswith(ARTIFACT_PREFIX)]
        require(found, "The run uploaded no reports. Open in GitHub to see why it stopped.")
        artifact = max(found, key=lambda a: a.get("id") or 0)
        require(not artifact.get("expired"), "The run's reports have expired in GitHub (they are kept 14 days). "
                "Start a new run.")
        require(type(artifact.get("id")) is int and (artifact.get("size_in_bytes") or 0) <= MAX_ARTIFACT,
                "The run's reports are larger than this app reads. Open the run in GitHub.")
        data = self.download("repos/" + plan["repository"] + "/actions/artifacts/" + str(artifact["id"]) + "/zip")
        return dict(artifact=artifact["name"], files=read_artifact(data))


def read_artifact(data):
    """{name: bytes} for the top-level CSV and Excel reports in an artifact ZIP; everything else is ignored."""
    require(isinstance(data, (bytes, bytearray)) and data[:2] == b"PK", "GitHub didn't send the reports as a ZIP.")
    files = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir() or not REPORT_NAME.fullmatch(info.filename): continue
                with archive.open(info) as member:
                    content = member.read(MAX_REPORT + 1)
                require(len(content) <= MAX_REPORT, "A report in the run is larger than this app reads.")
                files[info.filename] = content
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, OSError) as error:
        raise ReviewError("The run's reports couldn't be read: " + str(error)) from error
    require(any(name.endswith(".csv") for name in files), "The run's artifact has no CSV report.")
    return files


# ---------------------------------------------------------------- reports
def parse_report(data):
    """A CSV report as dict(kind='scan' or 'duplicates', rows=[dict]). Refuses columns it doesn't know."""
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ReviewError("A report isn't UTF-8 text.") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = reader.fieldnames or []
    kind = "scan" if header == SCAN_FIELDS else "duplicates" if header == FIELDS else None
    require(kind, "A report has columns this app doesn't know. Update the app or open the report in Excel.")
    rows = []
    for row in reader:
        require(None not in row and all(value is not None for value in row.values()), "A report row is malformed.")
        rows.append(row)
    return dict(kind=kind, rows=rows)


def main_report(files):
    """(name, parsed) for the report to show: the run's own pass, not the audit made after a change."""
    csvs = sorted(name for name in files if name.endswith(".csv"))
    name = next((n for n in csvs if "-after" not in n), csvs[0])
    return name, parse_report(files[name])


def truthy(value):
    return str(value).strip().lower() == "true"


def action_text(action):
    """The script's action, in the library's words."""
    action = str(action or "").strip()
    fixed = {"disable": "Would disable", "disabled": "Disabled by this run", "already disabled": "Already disabled",
             "review: similar query only": "Review: similar query only",
             "skip: also a selected rule": "Skipped: also a selected rule",
             "skip: enabled repo rule, change it in git": "Skipped: a library rule, change it in the library"}
    if action in fixed: return fixed[action]
    if action.startswith("FAILED"): return "Failed" + action[6:]
    if action.startswith("skip: repo rule "):
        return "Skipped: library rule " + action[len("skip: repo rule "):]
    return action[:1].upper() + action[1:]


def tone(kind, row):
    """The status colour the report gives a row: danger, warning, success, info or neutral."""
    if kind == "scan":
        if not truthy(row.get("enabled")): return "neutral"
        try: others = int(row.get("enabled_in_group") or 0)
        except ValueError: others = 0
        return "danger" if others > 1 else "success"
    action = str(row.get("action") or "")
    if action.startswith("FAILED"): return "danger"
    if action == "disable": return "warning"
    if action == "disabled": return "success"
    if action.startswith("review"): return "info"
    return "neutral"


def summary(kind, rows, mode=None):
    """One line on what the report found."""
    targets = len({row["target"] for row in rows})
    clients = f"{targets} client{'s' if targets != 1 else ''}"
    if kind == "scan":
        groups = {(row["target"], row["group"]) for row in rows}
        live = {(row["target"], row["group"]) for row in rows if tone(kind, row) == "danger"}
        if not rows: return "No rules that look like copies of each other in the selected clients."
        return (f"{len(rows)} rules in {len(groups)} duplicate group{'s' if len(groups) != 1 else ''} across {clients}"
                f" · {len(live)} group{'s' if len(live) != 1 else ''} with more than one enabled rule.")
    if not rows: return "No copies of the library rules in the selected clients."
    counts = {}
    for row in rows: counts[tone(kind, row)] = counts.get(tone(kind, row), 0) + 1
    parts = [f"{len(rows)} possible cop{'ies' if len(rows) != 1 else 'y'} across {clients}"]
    for key, word in (("success", "disabled"), ("warning", "would be disabled" if mode != "disable-apply" else "not disabled"),
                      ("danger", "failed"), ("info", "to review"), ("neutral", "skipped or already disabled")):
        if counts.get(key): parts.append(f"{counts[key]} {word}")
    return " · ".join(parts) + "."


def write_new(path, data):
    """Write ``data`` to a new file at ``path`` in one step: a failed write leaves nothing half-written."""
    fd, temporary = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output: output.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def save_reports(files, folder, run_number=None):
    """Write the run's reports into ``folder`` (named after the run, never over an existing file); their paths."""
    folder = Path(folder)
    require(folder.is_dir(), "Choose an existing folder for the reports.")
    saved = []
    for name in sorted(files):
        require(REPORT_NAME.fullmatch(name), "Unexpected report name.")
        stem, ext = name.rsplit(".", 1)
        base = f"{stem}-run{run_number}" if run_number else stem
        path, n = folder / f"{base}.{ext}", 2
        while path.exists():
            path, n = folder / f"{base}-{n}.{ext}", n + 1
        write_new(path, files[name])
        saved.append(path)
    return saved


# ---------------------------------------------------------------- receipts
def load_receipt(path):
    """A saved duplicate request, checked: it starts no run and reads nothing from GitHub."""
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(record, dict) and record.get("version") == 1, "Unsupported duplicate request.")
    plan = record.get("plan")
    require(isinstance(plan, dict) and record.get("fingerprint") == fingerprint(plan), "A saved duplicate request was changed.")
    require(re.fullmatch(r"[0-9a-f]{32}", str(record.get("request_id"))), "Invalid request ID.")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", str(plan.get("repository"))), "Invalid repository.")
    require(plan.get("mode") in MODES and plan.get("branch") == "main", "Invalid duplicate request.")
    require(isinstance(plan.get("targets"), list) and all(isinstance(t, str) and TARGET.fullmatch(t) for t in plan["targets"]),
            "Invalid saved targets.")
    result = record.get("result")
    require(result is None or (isinstance(result, dict) and isinstance(result.get("state"), str)), "Invalid run result.")
    if result and result.get("run_id") is not None:
        require(type(result["run_id"]) is int and result["run_id"] > 0, "Invalid run ID.")
    datetime.fromisoformat(record["created_at"])
    return record


def latest_receipt(folder, repository=None):
    """The newest saved request that started a run (for ``repository`` when given), or None."""
    found = []
    for path in Path(folder).glob("*" + RECEIPT_SUFFIX) if Path(folder).is_dir() else []:
        try: record = load_receipt(path)
        except (OSError, ValueError, KeyError, TypeError, ReviewError): continue
        if not record["attempted"] or not (record.get("result") or {}).get("run_id"): continue
        if repository and record["plan"]["repository"] != repository: continue
        found.append(record)
    return max(found, key=lambda r: r["created_at"]) if found else None
