"""Regenerate sentinel_app/attack_catalog.py, the MITRE ATT&CK techniques the Rule builder's technique picker
offers (Enterprise and ICS), from MITRE's own ATT&CK data (STIX 2.1).

    py scripts\\update_attack_catalog.py                         (downloads both, about 43 MB, for SENTINEL_VERSION)
    py scripts\\update_attack_catalog.py --version 14.1
    py scripts\\update_attack_catalog.py enterprise-attack-13.1.json ics-attack-13.1.json

The catalog follows the ATT&CK version Microsoft Sentinel is aligned to (see "Understand security coverage
by the MITRE ATT&CK framework" on Microsoft Learn), not MITRE's newest: Sentinel maps rules to its own
version, and newer releases retire IDs it still uses (ATT&CK 19 revoked T1562 Impair Defenses, for one).
When Sentinel moves up, change SENTINEL_VERSION, run this, check the diff and commit it.

The app never downloads anything itself: the catalog ships inside it, so the picker works offline.
"""
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sentinel_app.rule_schema import TACTICS  # noqa: E402

SENTINEL_VERSION = "13.1"
SOURCES = ("https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/enterprise-attack/enterprise-attack-{}.json",
           "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/ics-attack/ics-attack-{}.json")
TARGET = ROOT / "sentinel_app" / "attack_catalog.py"
KILL_CHAINS = {"mitre-attack": "enterprise", "mitre-ics-attack": "ics"}
# ATT&CK tactics Sentinel has no name for, filed under the Sentinel tactic that covers them: ICS calls it
# Evasion, and ATT&CK 19 split Enterprise's Defense Evasion into Stealth and Defense Impairment.
SENTINEL_TACTIC = {"evasion": "DefenseEvasion", "stealth": "DefenseEvasion", "defense-impairment": "DefenseEvasion"}
# A technique's tactics are listed in the matrix's own order, so the file is the same on every run
# (rule_schema.TACTICS is a set, whose order changes from one run to the next).
MATRIX_ORDER = ("Reconnaissance", "ResourceDevelopment", "InitialAccess", "Execution", "Persistence",
                "PrivilegeEscalation", "DefenseEvasion", "CredentialAccess", "Discovery", "LateralMovement",
                "Collection", "CommandAndControl", "Exfiltration", "Impact", "ImpairProcessControl",
                "InhibitResponseFunction")
assert set(MATRIX_ORDER) == TACTICS, "MATRIX_ORDER must list every Sentinel tactic once."


def sentinel_tactic(phase):
    """'command-and-control' -> 'CommandAndControl', as Sentinel names tactics."""
    name = SENTINEL_TACTIC.get(phase) or "".join(word.capitalize() for word in phase.split("-"))
    if name not in TACTICS:
        raise SystemExit(f"ATT&CK tactic {phase!r} has no Sentinel tactic: add it to SENTINEL_TACTIC.")
    return name


def attack_id(item):
    """The technique's ATT&CK ID. ICS files list a deprecated technique's ID under 'mitre-ics-attack' (their
    current ones use 'mitre-attack'), so both count; 'mitre-attack' wins if an item has both."""
    ids = {ref.get("source_name"): ref.get("external_id") for ref in item.get("external_references", ())}
    return ids.get("mitre-attack") or ids.get("mitre-ics-attack")


def catalog(*bundles):
    """(version, [(id, name, tactics)], {retired id: (name, replacement id or None)}, {ICS technique IDs}) from
    ATT&CK bundles of one version (Enterprise, ICS): a technique is ICS when its tactics are ICS ones."""
    versions = {next(item.get("x_mitre_version") for item in bundle["objects"] if item["type"] == "x-mitre-collection")
                for bundle in bundles}
    if len(versions) != 1: raise SystemExit(f"The ATT&CK files are of different versions: {sorted(versions)}.")
    objects = [item for bundle in bundles for item in bundle["objects"]]
    by_ref = {item["id"]: item for item in objects}
    revoked_by = {item["source_ref"]: item["target_ref"] for item in objects
                  if item["type"] == "relationship" and item.get("relationship_type") == "revoked-by"}
    current, retired, ics = {}, {}, set()
    for item in objects:
        if item["type"] != "attack-pattern" or not attack_id(item): continue
        technique = attack_id(item)
        if item.get("revoked") or item.get("x_mitre_deprecated"):
            replacement = by_ref.get(revoked_by.get(item["id"])) if item.get("revoked") else None
            retired[technique] = (item["name"].strip(), attack_id(replacement) if replacement else None); continue
        if technique in current: raise SystemExit(f"{technique} is in more than one ATT&CK file.")
        phases = [phase for phase in item.get("kill_chain_phases", ()) if phase.get("kill_chain_name") in KILL_CHAINS]
        tactics = {sentinel_tactic(phase["phase_name"]) for phase in phases}
        if any(KILL_CHAINS[phase["kill_chain_name"]] == "ics" for phase in phases): ics.add(technique)
        current[technique] = (item["name"].strip(), tuple(sorted(tactics, key=MATRIX_ORDER.index)))
    for technique in current:
        if "." in technique and technique.split(".")[0] not in current:
            raise SystemExit(f"{technique} has no current parent technique.")
    for technique in current: retired.pop(technique, None)
    revoked = dict(retired)
    for technique, (name, replacement) in revoked.items():
        # Follow a chain of revocations (T1150 -> T1547.011 -> T1647) to a technique the catalog offers.
        seen = {technique}
        while replacement in revoked and replacement not in seen:
            seen.add(replacement); replacement = revoked[replacement][1]
        retired[technique] = (name, replacement if replacement in current else None)
    return (versions.pop(), [(technique, *current[technique]) for technique in sorted(current)],
            dict(sorted(retired.items())), ics)


def render(version, techniques, retired, ics):
    lines = ['"""MITRE ATT&CK techniques (Enterprise and ICS) for the Rule builder\'s technique picker, at the ATT&CK',
             "version Microsoft Sentinel is aligned to.",
             "",
             "Generated by scripts/update_attack_catalog.py from MITRE's ATT&CK data. Don't edit it by hand:",
             'run the script again when Sentinel moves to a newer ATT&CK version."""',
             "",
             f"VERSION = {version!r}",
             "# (ID, name, Sentinel tactics) for every current technique, in ID order, so each parent's",
             "# sub-techniques (IDs with a dot) follow it.",
             "TECHNIQUES = ("]
    lines += [f"    ({technique!r}, {name!r}, {tactics!r})," for technique, name, tactics in techniques]
    lines += [")",
              "# The techniques above that are ICS ones (ATT&CK for Industrial Control Systems).",
              "ICS = frozenset({"]
    lines += [f"    {technique!r}," for technique in sorted(ics)]
    lines += ["})",
              "# Techniques ATT&CK has deprecated or revoked by this version: ID -> (name, the current technique",
              "# that replaced it, or None).",
              "RETIRED = {"]
    lines += [f"    {technique!r}: ({name!r}, {replacement!r})," for technique, (name, replacement) in retired.items()]
    lines += ["}", ""]
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    version = SENTINEL_VERSION
    if args[:1] == ["--version"]: version, args = args[1], args[2:]
    if args:
        bundles = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args]
    else:
        bundles = []
        with tempfile.TemporaryDirectory() as folder:  # Removed once read.
            for url in SOURCES:
                url = url.format(version)
                print("Downloading", url)
                source = Path(folder) / url.rsplit("/", 1)[-1]
                with urllib.request.urlopen(url, timeout=300) as response: source.write_bytes(response.read())
                bundles.append(json.loads(source.read_text(encoding="utf-8")))
    version, techniques, retired, ics = catalog(*bundles)
    TARGET.write_text(render(version, techniques, retired, ics), encoding="utf-8", newline="\n")
    parents = sum("." not in technique for technique, *_ in techniques)
    print(f"ATT&CK {version}: {parents} techniques ({len(ics)} ICS), {len(techniques) - parents} sub-techniques, "
          f"{len(retired)} retired -> {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
