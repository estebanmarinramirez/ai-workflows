# Measuring ordinary agent work

Version 1.11 records interactive task attempts automatically at initial dispatch,
reviewer activation, and explicit retry. No extra provider request is made. A
pre-delivery record binds the attempt to task, role, requested settings, Git
baseline, dirty flag, and an optional frozen measurement contract. Subsequent
records retain delivery, reported start/completion/blocking, acknowledged provider
changes, and output commit references. Retries get new IDs linked to the preceding
attempt under the same task. Resuming a blocked attempt through status updates
preserves its identity; handovers do not fabricate an independent sample.

Records live in `TASK/measurement/journal.json`. The normal telemetry collector
ingests them idempotently, including tasks in inactive workspaces. Old task
histories do not become new attempts. Writes are atomic and locally locked. An
interrupted prompt delivery retains a prepared attempt and the existing
`dispatching` task state: inspect the terminal before explicitly resolving it.
Never automatically retry an ambiguous delivery.

`agent-workspaces measurement status TASK` reports observed elapsed time from
successful delivery to the latest reported terminal status. That duration includes
idle, human, and quota waits. It is **not compute time**. Interactive token counts,
billing, served model revision, individual calls, and time breakdowns remain
unknown unless separately supplied by a provider adapter. A requested effort tier
is configuration, not measured effort. Lifecycle records never assert acceptance.
The telemetry audit reports lifecycle, usage, verification, and overlapping receipt
coverage separately; a usage receipt can still contain unknown cost.

## Prepare before opening agents

Preparation uses saved workspace metadata and existing worker checkouts. It does
not require tmux, an available subscription, or a running model. The usual
multiagent roles are preserved, including deferred reviewers and provider-switched
slots. It requires an existing configured workspace, not a new empty directory.

```sh
agent-workspaces dispatch --workspace WORKSPACE_ID --template bugfix \
  --lead codex --objective 'A concrete bounded change' --prepare-only
# The command prints a JSON object containing the new TASK path.
agent-workspaces measurement prepare TASK --role codex \
  --criteria /absolute/path/acceptance.txt \
  --verifier /absolute/path/independent-verifier.py --max-attempts 2 --timeout 120
agent-workspaces measurement preflight TASK --role codex
```

`prepare` requires a clean committed lead checkout and freezes the task text,
acceptance criteria, verifier bytes, baseline, attempt limit, and verifier timeout.
Changing them requires a new task. The verifier source must be outside the worker
checkout. Review its code before preparing: it is trusted local Python, not
sandboxed by this application. Its dependencies should be pinned by the author.

Preflight checks the frozen inputs, baseline, cleanliness, prior acceptance,
attempt allowance, budget, and pending handover. A missing frozen contract is
reported as observational-only, not silently upgraded to measured evidence.
Actual CLI availability, idle composer, and acknowledged model configuration are
still checked by the existing dispatcher immediately before delivery.

When ready to dispatch, explicitly release the prepared task:

```sh
agent-workspaces measurement release TASK --role codex
agent-workspaces dispatch-ready TASK
```

Release rechecks ownership conflicts and budget, then queues the task. A running
workspace monitor may deliver the queued task automatically. Before release,
monitors leave `prepared` tasks untouched. Preparation does not authorize a
background provider call. The attempt allowance limits explicit starts for the
measured role; it does not count or bound each internal call in an interactive CLI.
Existing account-level budget gates remain in force.

## Verification and completion

The worker uses normal `agent-workspaces status` commands, including an immutable
`--commit` on completion. For the role with a frozen contract, completion invokes
the verifier once on a detached clone of the submitted commit. The worker's dirty
checkout is not the verification artifact. The commit must descend from the
frozen baseline. The verifier receives one argument, a JSON request filename:

```json
{"worktree": "/temporary/checkout", "baseline_commit": "...", "output_commit": "...",
 "task_id": "...", "role": "codex", "attempt_id": "..."}
```

Exit 0 means accepted; exit 1 means rejected. Other exit codes, launch failures,
and timeouts are infrastructure/failure evidence, not quality rejection. Verifier
output hashes and duration are retained. An interrupted verifier retains its
claim and is never rerun automatically. Missing commits stay unverified; correct
the status and run `measurement verify TASK --role ROLE` explicitly. Repeated
successful verification commands return the saved result. Direct edits to status
files bypass the command hooks; use the CLI rather than editing JSON.

Agent-reported completion and independent acceptance remain separate. Existing
integration approvals are unchanged; inspect the measurement result before
accepting a measured task. A failed check never silently becomes a successful
training label. These receipts are local evidence, not cryptographic attestation
against an agent with access to the same user account.

## First development tasks

`examples/measurement/` contains two concrete, intentionally pending tasks:

- `cache-identity`: pin policy evaluation cache identity to evaluator code, even
  when a developer forgets to change the release version.
- `session-observation`: distinguish a missing tmux server from a permissions or
  transport failure in collection health.

Each has explicit criteria and a standalone external verifier. Both verifiers
fail against the current implementation for the expected reason; these are future
agent tasks, not failing tests in the release suite. Copy a verifier outside the
worker checkout before freezing it. Use separate clean task baselines, an attempt
limit, and existing budget gates. They are public development tasks, **not fresh
held-out evidence** and not sufficient to compare model quality statistically.

Before claiming improvement, collect real usage where available and verified
outcomes on representative tasks, preserve retries under each task, then compare
frozen alternatives on fresh independent tasks. Automatic learning and live
policy promotion remain disabled.

## Dispatcher context evidence (1.12)

Initial dispatch, reviewer activation and retry save the exact dispatcher prompt
before sending it. A private, content-addressed JSON artifact retains copies of
task text, protocol, task instruction files, root repository instructions, and
worker/lead status available at capture time. Missing, external, non-regular,
unreadable or oversized files are recorded as gaps rather than invented content.
Each file is bounded to 1 MiB. These are available input snapshots, not proof of
which files the agent read. Acknowledged handovers add the checkpoint note and
new requested settings to the same attempt; they do not invent a fresh sample.

Artifacts live under `TASK/measurement/contexts/`, with private directory/file
permissions. Their raw prompts, file contents, session references and handover
notes stay local to that task. The telemetry ledger receives only a hash and the
scope `dispatcher_inputs_only`. Collection reports an error if a referenced
artifact is missing or changed. The status report checks integrity and links
these hashes to the attempt's reported output commit and verification result.
`telemetry audit` counts attempts with dispatcher context separately; that count
is not a claim of complete context or independent training examples.

Every interactive attempt remains `replay_eligible: false`, including a fully
verified one. Prior conversation, compaction, provider system prompts, later user
messages, dynamic instructions and tool results remain unobserved. Switching
providers preserves those uncertainties. Replay validation rejects manifests or
worlds explicitly labeled with this partial context scope or ineligibility.
Removing a provenance label does not make the evidence valid; externally supplied
manifests are still the producer's responsibility. No automatic conversion from
interactive records to discovery trees exists.

To inspect one task without reading conversations or launching agents:

```sh
agent-workspaces measurement status TASK
agent-workspaces telemetry audit
```

Old attempts remain unrecorded where context was not captured before execution.
No history is backfilled and no live session is restarted to obtain these records.
