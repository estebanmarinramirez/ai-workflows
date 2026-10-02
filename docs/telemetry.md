# Everyday workspace evidence

Version 1.5 adds a durable, local observational ledger for normal workspace use.
Collection is enabled by default (`telemetry.enabled` in the workspace config).
It does not change agent assignments, effort settings, or Bayesian training.

## Storage and collection

The ledger is `$XDG_STATE_HOME/agent-workspaces/telemetry/ledger.sqlite`, normally
`~/.local/state/agent-workspaces/telemetry/ledger.sqlite`. The directory is private
(mode 0700), and the database is mode 0600. SQLite WAL and transactions serialize
concurrent collectors; event rows reject SQL updates and deletes. This protects
against accidental modification, not against the machine owner modifying files.

On Linux the existing usage timer collects every two minutes. Workspace audit
writes and assignment saves also trigger a lightweight collection. These hooks
avoid runtime subprocess probes; the background scan adds those observations.
On other platforms run `agent-workspaces telemetry collect` periodically; this
release does not install a macOS background telemetry scheduler.

Every saved workspace under the workspace data root is scanned, including closed
and inactive workspaces. No new agent session is required. Already-running older
tooling gets coverage from the new background collector, while its old process
cannot acquire newly added hooks until restarted. The collector never resumes or
restarts an agent, switches a branch, or changes a worktree.

| Evidence | Recorded information | Interpretation |
|---|---|---|
| Configuration | Provider model/effort policy, orchestrator choice, layout/profile, routing mode | Desired settings; not proof of the running model |
| Workspace/task | Stable project/workspace/task identity, stage/status, timestamps, role states, gates, transitions, routing intent | Initial saved state or observed change; historical duration is not measured compute time |
| Assignment/handover | Active slot choices and request IDs, target choices, phases and available timestamps | Preserves ownership changes without equating slot/branch name with provider identity |
| Role evidence | Exit codes, timestamps, command hashes, commits, changed-file and blocker counts | Agent-reported evidence, not independent acceptance |
| Routing | Existing decision history, modes, choices, revisions, account-pool IDs | Imported decisions; not a new pre-outcome Bayesian forecast |
| Worktree | Slot, branch, HEAD and dirty flag | Periodic read-only Git observation; dirty does not mean defective |
| Runtime | tmux session, workspace, role and provider labels | A tmux session is not proof of an active model turn |
| Usage | Available account quota limits, reset/source times, token/model counters and source availability flags | Account-scoped snapshots; never task cost or billable-dollar estimates |
| Explicit receipts | Attempt-scoped usage, verification or failure with source identity | Submitted evidence with stated provenance, not automatically authenticated |
| Collector | Version, observation timestamps, import/change provenance, counts and errors | Distinguishes source time from when evidence became available |

A first observation is labelled `initial_observation`, even if the task is old.
Existing audit events are labelled `imported_source_event`. They retain source
and observation timestamps and do not become prospective predictions. Audit
imports deduplicate identical occurrences, preserving repeated identical events
within the source. Snapshot changes compare against the last snapshot, so an
A → B → A sequence is retained. A missing source does not erase past evidence.
Malformed files are reported and retried on the next collection.

Periodic sampling can miss intermediate states. Existing audit history fills in
only what the older tooling actually recorded. Historical settings, per-turn
compute, resolved model versions, task costs and verification cannot be recovered
when no source recorded them. Model aliases are not immutable version identities.
Session inventory failure is reported as unavailable, not as zero sessions.

## Inspect, export and disable

```sh
agent-workspaces telemetry status
agent-workspaces telemetry collect
agent-workspaces telemetry export > evidence.jsonl
agent-workspaces telemetry export --after 1200 > newer-evidence.jsonl
```

Export is ordered JSONL with event IDs, source identity, workspace/task identity,
source and observation times, provenance and structured payloads. Cursor exports
support reproducible analyses without mutating the ledger. Collection status
shows the last scan and any source errors. `collect` returns nonzero for partial
collection; task/assignment hooks report problems without rejecting saved work.

Set `"telemetry": {"enabled": false}` in
`~/.config/agent-workspaces/config.json` to stop new automatic collection and
receipt ingestion. Existing evidence remains available for status/export.
Nothing is uploaded. The ledger is outside the Git repository. There is no
automatic deletion/retention limit yet; monitor disk use and archive exported
records according to your own retention needs. Do not copy a live SQLite file
alone as a backup: use SQLite's backup API (WAL may hold committed records).

Collection excludes prompt/response text, terminal contents, command text,
arbitrary event details, task objectives, summaries, blocker text, changed-file
names and credentials. It does retain local source paths, branch names, commit
IDs, model names and structured identifiers: review an export before sharing it.
Command hashes support correlation, not anonymization of predictable commands.

## Attempt receipts

Adapters or verifiers can write a JSON file and submit it with:

```sh
agent-workspaces telemetry record receipt.json
```

Example usage receipt (replace the identifiers with the actual source values):

```json
{
  "receipt_id": "provider-event-123",
  "kind": "usage",
  "workspace": "project-directory/workspace-directory",
  "task": "task-directory",
  "role": "codex",
  "attempt_id": "attempt-001",
  "source": "provider-adapter-v1",
  "at": "2026-10-02T12:00:00Z",
  "provider": "codex",
  "model": "reported-model",
  "model_version": null,
  "effort": "high",
  "input_tokens": 1000,
  "output_tokens": 200,
  "cached_input_tokens": null,
  "cost_usd": null,
  "wall_seconds": 12.5
}
```

Unknown quantities are omitted or null, never guessed as zero. Token values must
be nonnegative integers; durations/costs must be finite and nonnegative. Usage
receipts represent the named attempt, not an account-wide quota change. Keep the
same attempt ID across related usage, failure and verification receipts. Receipt
IDs must be stable across ingestion retries. Equal retries are idempotent;
conflicting reuse is rejected. Corrections use a new ID and `supersedes` pointing
to an existing receipt from the same source/workspace/task/attempt/kind. Consumers
must resolve corrections before aggregating; this release does not sum receipts.

Verification receipts use `kind: "verification"`, boolean `accepted`,
`verifier_id`, `verifier_version`, and `evidence_sha256` (64 lowercase hex digits),
in addition to the common identity/source/time fields. The hash identifies an
external artifact; ingestion validates its format but does not fetch or execute
it. Failure receipts use `kind: "failure"` and `failure_category`, one of
`quality`, `quota`, `timeout`, `infrastructure`, `cancelled`, `protocol`, `unknown`.
Optional `exit_code` is an integer. No failure category is inferred from a dirty
worktree, blocked status, disappeared terminal, or missing record.

Receipts are marked `submitted_receipt_not_independently_authenticated`. Caller
assertions about model versions and verifier independence require separate
validation. There are no automatic real-provider per-task receipt adapters yet.

## Analysis boundaries

This dataset is observational, with correlated repetitions, unknown task
selection bias and incomplete version/usage evidence. It is **not connected to
Bayesian learning**. Completion flags and passing command reports do not create
success labels, and imported history does not create hindsight forecasts.

The separate [Bayesian benchmark](bayesian-shadow.md) remains the prospective
prediction/calibration path. Before using everyday evidence for policy learning,
add validated provider/verifier adapters, explicit task families and instance
identities, attribution to real attempts, immutable model versions, correction
resolution, and a frozen development/held-out evaluation protocol. Track coverage
and missingness before interpreting configuration differences as causal benefit.
