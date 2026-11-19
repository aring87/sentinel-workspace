# Reviewed rule schema

The application imports sentinel_app/rule_schema.py, a reviewed copy of detection-as-code/rules.py
(scripts/rules.py) as of detection-as-code main commit 7ee9b7d, which added the
`clients` metadata field and the client-rule template ID exception. It never imports validator
code from a selected local or remote repository.

SHA-256: `d29cc664ea2b2d56afb55b50309687dde491eb38b80a24e49a5756e0164f9a43`

Hash uses UTF-8 with LF line endings. To change the supported schema, review new
reference uploads, update the bundled module and this hash, then run the draft and
full regression tests. An upload alone does not change runtime validation.

The pilot supports Scheduled and NRT fields in that reference, preserves advanced
properties during editing, and calls its validate and to_arm functions before export.
Its checks are not a full ARM schema validation, MITRE relationship validation, KQL
execution, or a guarantee that the private repository still uses the same revision.
The repository pipeline remains authoritative and handles client assignments,
overrides, preview, approval, and deployment.
