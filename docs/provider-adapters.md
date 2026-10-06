# Measured provider attempts (v1.6)

The provider adapter launches fresh, bounded non-interactive Codex or Claude
sessions with explicit model/effort flags. It captures provider usage before
running an independent, frozen Python verifier. Existing interactive sessions
are not automatically wrapped or retroactively attributed to tasks.

## Everyday tasks

Use an idle workspace role with a saved coordination task:

```sh
agent-workspaces attempts run /path/to/workspace/.coordination/task-id \
  --role codex --provider codex --model YOUR_MODEL --effort high \
  --prompt-file /path/to/task-prompt.txt \
  --verifier /path/outside/worker-checkout/verify.py --timeout 180
```

This explicitly launches real provider work and consumes its normal quota.
The selected worktree may be edited. The command refuses a role with an existing
managed tmux session, a pending handover, or another measured attempt holding its
workspace-role lock. It does not change slot ownership, routing choices or task
completion flags. Starting a concurrent interactive agent after the check is not
prevented by the measured-attempt lock; keep the role idle until it finishes.

The verifier is copied and hashed before the model runs. It receives a request
JSON path as its sole argument, runs with cwd set to the worker checkout, and
must exit 0 for accepted, 1 for a completed acceptance-check failure, or 2+ for
infrastructure errors. It should be a standalone trusted script containing
independent behavior checks. The model is not asked to write its own acceptance
test. Each provider/verifier invocation has the specified timeout, so a complete
attempt may take approximately twice that duration, plus startup/recording.

Artifacts live under the task's `attempts/ATTEMPT_ID/`: provider JSON, stderr,
final response, execution receipt, frozen verifier and hash, original request,
verification result/log hashes, normalized receipts and outcome. The attempt
folder is private (0700). **Raw artifacts can contain code and model text**;
the central telemetry ledger receives only structured receipts and hashes.
Nothing is uploaded except the task/code sent through the selected provider's
normal CLI operation. Do not commit raw attempt artifacts or share them blindly.

Verified acceptance, usage and failure receipts share an attempt ID. Ingestion
is transactional and idempotent. If ledger ingestion fails, artifacts survive:

```sh
agent-workspaces attempts ingest /path/to/task/attempts/ATTEMPT_ID
```

This command replays normalized receipts; it does not repeat provider work or
verification. Locally submitted receipts remain caller assertions, not signed
provider attestations. There is no automatic retry that could consume quota or
silently duplicate attempts. A hard kill or power loss may leave a started or
partial artifact; inspect it instead of treating it as completed work.

## Provider evidence

- **Codex:** `exec --json --ephemeral`, explicit sandbox and effort, no resume,
  user config ignored. Requires exactly one successful `turn.completed` usage
  record. Input, output and cached-input tokens are captured separately; cached
  input is a subset, not extra input to add. Exact served model identity and
  dollar cost are unavailable from this receipt format and stay null.
- **Claude:** `--print --output-format json --no-session-persistence`, restricted
  mode, explicit read/edit tool allowlist and no permission bypass. Requires a
  successful result. Records regular input, output, cache-read and cache-creation
  tokens separately. `total_cost_usd` is the CLI-reported cost, not proof of a
  subscription charge. A sole `modelUsage` key is recorded as the observed model
  identifier; multiple models make the single-version identity unknown.
- **Grok:** unsupported by this adapter until the headless receipt contract and
  task attribution are validated. No silent substitution is performed.

`model` and `effort` describe requested flags; this does not prove the provider
honored them. `observed_model` is separate. A requested benchmark `model_version`
is accepted only if the response supplies a matching identifier. A reported
identifier may still be an alias rather than an immutable model revision.
Effort settings and CLI availability remain subject to provider validation.
CLI versions and raw receipt hashes are retained for later audit.

The Claude and Codex tool capabilities differ: Claude's adapter permits direct
file read/edit tools but not shell execution; Codex uses its workspace sandbox.
Treat this as a workflow difference, not an isolated comparison of model quality.
Subprocess groups are terminated on timeout/exit. Benchmark adapter children stay
in the harness's process group so an interrupted harness can clean them up.
These are trusted local programs, not a hostile-code evaluation sandbox.

## Real CLI benchmark adapter

Use the installed `lib/provider_adapter.py` as your manifest's `adapter`. The
benchmark freezes its source exactly like any standalone adapter.
Supported topologies are `solo` (coordinator → implementer) and `review`
(coordinator → implementer → reviewer), with one fresh invocation per role.
Coordinator/reviewer are read-only. Reviewer output is advisory; the independent
verifier supplies acceptance. Parallel/investigation topologies fail explicitly.
No automatic repair round or model substitution is hidden in a trial.

Generate a disposable smoke experiment from the installed examples:

```sh
python /path/to/installation/examples/benchmark/create-provider-smoke.py \
  /tmp/provider-input --provider codex --model YOUR_MODEL --effort low
agent-workspaces benchmark init /tmp/provider-input/manifest.json /tmp/provider-experiment
agent-workspaces benchmark run /tmp/provider-experiment --all
agent-workspaces benchmark run /tmp/provider-experiment --split heldout --all
```

The smoke workload contains two small fixture tasks and uses real provider calls.
It is labelled synthetic and has Bayesian shadow learning disabled. It validates
execution and attribution, **not** representative performance or a winning team.
Benchmark artifacts stay in the experiment ledger; they are not automatically
mixed into everyday workspace telemetry.

For a representative comparison, define distinct real task instances/families,
freeze independent acceptance checks and development/held-out splits, include
repetitions and budgets, and require adequate version provenance. The existing
Bayesian policy still does not learn from everyday receipts or change live
routing. Unknown Codex model version currently prevents claiming a fully
version-attested shadow experiment through this CLI adapter.

Protocol references: [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
and [Claude programmatic execution](https://code.claude.com/docs/en/headless),
checked against installed CLI help. Parser tests use synthetic protocol fixtures;
live smoke results are kept locally, outside the repository.
