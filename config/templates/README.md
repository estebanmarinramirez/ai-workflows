# Development templates

Agent Workspaces discovers every `*.json` file in this directory. Add a file to create another reusable workflow; no launcher changes are required.

Required fields:

- `schema_version`: currently `1`
- `id`: stable filename-compatible identifier
- `label` and `description`: menu text
- `stage`: `review`, `execute`, `verify`, or `integrate`
- `assignments.lead`, `assignments.reviewer`, `assignments.verifier`
- `required_evidence`: non-empty list of completion evidence

Templates define outcomes and evidence. Concrete build, test, lint, and formatting commands stay in the repository's own instruction files and project configuration so a template remains portable across languages and build systems.

Automation may select a template with `AW_TEMPLATE=bugfix omarchy-agent-prompt`.

