# Agent configuration experiments

The benchmark harness measures fixed workflow configurations without changing
production routing. Each experiment freezes a manifest, standalone Python
adapter/verifier scripts, and a repository commit. It creates a balanced matrix
of tasks × configurations × repetitions, randomizes execution order with a seed,
and gives matching task/repetition pairs the same requested seed.

`benchmark init` snapshots committed Git objects, not uncommitted source edits.
Every trial gets a separate clone and detached checkout from that snapshot.
Your existing branches, tmux sessions, worktrees and task records are not reused.
The parent process's AW_* workspace variables and Codex session identity variables
are removed from subprocess environments. These are isolation measures, not an
OS sandbox: adapters and verifiers are trusted local programs with the user's
normal permissions. They must launch fresh sessions and keep modifications inside
the trial checkout. Credentials remain available for adapters that call providers.

## Try the deterministic demo

Run from the installed CLI, or use `bin/agent-workspaces` from the repository:

```bash
agent-workspaces benchmark demo /tmp/aw-benchmark-demo
agent-workspaces benchmark init /tmp/aw-benchmark-demo/manifest.json /tmp/aw-benchmark-experiment
agent-workspaces benchmark run /tmp/aw-benchmark-experiment --all
agent-workspaces benchmark report /tmp/aw-benchmark-experiment
agent-workspaces benchmark run /tmp/aw-benchmark-experiment --split heldout --all
agent-workspaces benchmark report /tmp/aw-benchmark-experiment --split heldout
```

Use new destination paths for each experiment; existing experiments are never
overwritten. The demo has two small Python repair tasks, four configurations and
two repetitions: 16 trials total, eight development and eight held-out. It makes
**no model calls**. Its adapter applies a deterministic patch identically across
configurations, so its scores validate plumbing, not the quality of agent teams.

`run` without `--all` executes one planned trial. `--trial t00001` selects a
particular planned trial, subject to the selected split. Finished trials cannot
be rerun or overwritten; initialize a new experiment for further repetitions.
Failed trials appear in the ledger and reports. The CLI returns a result object
for a failed trial; inspect `accepted` and `failure`, not just its exit code.

## Real experiments

Supply your own manifest and two standalone Python scripts. The initial release
provides the execution/measurement harness and adapter contract, **not automated
interactive Codex/Claude/Grok drivers**. Running a real adapter is explicit and
may consume provider quota. No paid trials run during installation or tests.

The demo's `manifest.json` is a complete editable example. Required fields:

- `schema_version: 1`, `repository`, and a `revision` (resolved to a commit SHA).
- Relative or absolute `adapter` and `verifier` Python file paths. Both scripts
  are copied and hashed; imports of external helper files are not bundled.
- Positive `repeats`, positive per-process `timeout_seconds`, and integer `seed`.
- `tasks`: unique IDs, `split` (`development` or `heldout`), and an `objective`.
  Additional task data is allowed, for example issue context and task boundaries.
- `configurations`: unique IDs, topology, and explicit role settings. Every role
  specifies `provider`, `model`, and `effort`; pin versions where possible.

| Topology | Worker roles, in addition to coordinator | Intended adapter behavior |
| --- | --- | --- |
| `solo` | `implementer` | One implementation and required checks |
| `review` | `implementer`, `reviewer` | Implementation followed by independent review |
| `parallel` | `implementer-a`, `implementer-b`, `verifier` | Independent scoped edits, integration, then verification |
| `investigate` | `investigator-a`, `investigator-b`, `adjudicator` | Independent investigations before comparing evidence |

The adapter implements role scheduling and must report the actual settings it
used. The harness cannot prove a provider honored an effort flag or that the
adapter truly performed independent review. Preserve provider version/session
receipts in the trial artifacts for audit. Do not silently substitute a model.
A reported configuration mismatch makes the trial unaccepted even if tests pass.
Unknown costs are allowed; missing/malformed role telemetry is a protocol error.

### Adapter interface

The harness launches:

```text
python adapter.py REQUEST_JSON OUTPUT_USAGE_JSON
```

The working directory is the isolated checkout. Request fields include `task`,
`configuration`, `seed`, `repetition`, `baseline`, `worktree`, `artifacts`, and
`timeout_seconds`. The requested seed permits pairing; it does not guarantee
provider reproducibility. Run all provider work synchronously and finish within
the time limit. Process groups are terminated after exit/timeout, including
ordinary background children. Independently daemonized processes are unsupported.

Write this shape to the usage output, with one entry for **every** configured
role (including coordination, retries and failed attempts in the totals):

```json
{
  "roles": {
    "coordinator": {
      "provider": "provider-id",
      "model": "pinned-model-id",
      "effort": "high",
      "input_tokens": null,
      "output_tokens": null,
      "cost_usd": null
    },
    "implementer": {
      "provider": "provider-id",
      "model": "pinned-model-id",
      "effort": "high",
      "input_tokens": null,
      "output_tokens": null,
      "cost_usd": null
    }
  },
  "coordination_seconds": null,
  "recovery_seconds": null,
  "human_seconds": null,
  "handover_failures": null,
  "retries": null
}
```

Use `null` for unavailable measurements, never estimated zero. Dollar cost must
have a defensible measurement basis; a subscription quota percentage is not a
dollar amount. Operational durations may overlap and are not added to measured
wall time. Additional per-role receipt fields are retained. The harness measures
elapsed time independently, including clone/setup, adapter execution, verification,
and artifact capture. Setup time is intentionally included in end-to-end time;
`adapter_seconds` separates the adapter runtime. Hung processes have bounded time
and a failed outcome. Write usage even on failed attempts when possible.

### Independent verifier interface

```text
python verifier.py REQUEST_JSON
```

Exit zero only when the acceptance criteria pass. Run tests and assertions of
behavior, not checks for an agent's self-reported success. The verifier script is
frozen outside the worker checkout, and the harness restores the original request
before invoking it. This is a trusted-program evaluation boundary, not protection
against an adversarial adapter with filesystem access.

The verifier runs only after a successful adapter exit. `verified` records its
result, while `accepted` also requires the adapter telemetry protocol to be valid.
For diagnosis tasks, use pre-labelled findings or externally reviewed evidence;
a passing test alone is not a complete quality measure for every task.

## Ledger and artifacts

Each experiment contains:

- `manifest.json`: configuration, immutable baseline SHA, script hashes, creation
  time, Python/Git/platform information.
- `repository.git`: private repository snapshot used for all trials.
- `harness.py`: a copy of the experiment's harness, with its hash recorded in the
  manifest. A newer installed harness refuses to add trials to an older experiment;
  use its frozen copy or initialize a new experiment. Reports remain readable.
- `ledger.sqlite`: trial plans, exclusive claims, timestamps, outcomes, and
  append-only `started`/`finished` events. SQL triggers prevent ordinary event
  updates/deletions; this is not a cryptographic tamper-proof store.
- `trials/ID/`: request, usage, outcome, separate adapter/verifier logs, tracked
  file diff, Git status, and the full resulting checkout (including untracked
  artifacts). Logs can contain provider output; keep experiments local by default.

Concurrent runners claim different trials transactionally. Ctrl-C records an
interrupted trial and stops its process group. A forcibly killed harness or host
crash can leave a trial `running`; reports expose this rather than counting it as
success or retrying blindly. Check for surviving processes and start a new
experiment if necessary; automatic recovery of interrupted experiments is not
included in this release.

## Interpreting reports

Development is the default split. Held-out runs and reports require explicit
`--split heldout`. This prevents accidental mixing, not human inspection of test
cases; decide the split and acceptance criteria before comparing configurations.

Reports include completion counts, accepted outcomes, failures, mean wall time,
unknown cost counts, operational totals, and total cost per accepted result.
The cost numerator includes failed attempts. A missing cost in any completed
trial leaves aggregate cost unknown. No successes means cost per success is
undefined, not zero. Configuration summaries are marked `complete` only after
all their planned trials finish; partial results can be selection-biased.

Pairwise comparisons use matching task/repetition pairs only. Wilson intervals
are descriptive binomial references: repeated attempts on the same task are
correlated, so these are **not** confidence guarantees for repository-wide
performance. Use more distinct tasks and task-cluster uncertainty estimates in a
later statistical analysis. The harness deliberately declares no winner and
does not promote results into the live router. Keep quality, latency, resource
consumption and human intervention separate until an explicit objective and
quality floor are chosen.

## Next research stage

1. Build a representative task set with independent acceptance criteria and known
   defects, including integration conflicts and handover failures.
2. Implement provider adapters with bounded turns and auditable usage receipts.
3. Freeze an experimental matrix; run development tasks, select candidates, then
   evaluate once on held-out tasks.
4. Only then estimate task-conditioned model utility or train a router. Account
   for changing provider versions, correlated failures and subscription limits.
