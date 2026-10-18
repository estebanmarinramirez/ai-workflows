# Dream-RSI capture and offline replay

This is an independent, limited adaptation of
[Dream-RSI](https://github.com/zhengkid/Dream-RSI) and
[the paper, section 3](https://arxiv.org/html/2609.14858v2).
As checked on 2026-10-12, the official repository publishes the paper and assets;
its full implementation and reproduction scripts are still pending release.
No upstream code or third-party skill is installed.

## What runs today

```bash
agent-workspaces telemetry audit
agent-workspaces dream replay examples/dream/replay.json
```

The bundled example is synthetic. It exercises the simulator and always abstains
from a production recommendation. The replay command consumes a JSON manifest,
emits a JSON report, runs no model calls, and never edits live assignments or
routing. It does not implement the paper's LLM policy-code generation or automatic
online redeployment. Its policy search evaluates the finite, explicit candidate
list in the manifest. This makes the first implementation reproducible and
reviewable while actual discovery traces are collected.

The manifest records schema version, data kind, a fixed evaluation protocol,
objective weights, incumbent, candidate policies, and independent discovery
worlds. Each world represents one distinct task instance and has a development
or heldout split, root snapshot identifier and score, and ordered measured nodes.
The example is the complete executable replay schema. For manually supplied
worlds, snapshot identifiers must refer to externally retained immutable artifacts.
The capture/export workflow below retains Git snapshots and verifies their
objects and evidence hashes before producing that schema. Evidence digests must point to retained verifier
results, not model self-reports. The simulator checks structure, not authenticity.

Each node includes its primary parent, attempt ID, exact configuration identity
(provider/model/version/effort/topology), result snapshot, verifier version,
evidence SHA-256, score, measured dollars (nullable), and measured wall seconds.
Capture v2 measures admission through verification, with stage timings; older
captures measured generation plus verifier time only. Scores must be nonnegative, comparable under the fixed protocol,
and higher-is-better. Record failures and their costs too. Do not discard failed
attempts to improve a trace. Independent instances must appear only once, across
both splits. A world may branch at the root; each non-root node has at most one
child, matching this paper's root-plus-linear-refinements replay interface.

Existing interactive session snapshots cannot be converted automatically: they
lack immutable parent/result artifacts, independent scores, and task-scoped
costs. Existing `attempts run` receipts supply explicit measured attempts, but
currently do **not** capture complete discovery lineage or workspace snapshots.
Use the new `dream init` / `dream attempt` / `dream export` workflow to capture
those artifacts and assemble the manifest automatically. No real world is
fabricated from the activity ledger. Ordinary interactive chats are not silently
converted into measured attempts.

## Replay and objective

A policy sees only the root and outcomes already revealed. It can open the next
recorded root branch or continue a revealed leaf, with at most `workers` selections
per round. `breadth` prioritizes opening branches and shallow leaves; `best_leaf`
prioritizes the highest observed score. A nullable `stop_score` stops only on an
already observed score; `rounds` bounds exploration. Policies cannot substitute
another model into an existing node: that counterfactual was never measured.

The adapted objective is:

```text
J = quality_weight × best observed score
  − dollar_weight × total measured dollars
  − time_weight × simulated elapsed seconds
  − attempt_weight × number of revealed attempts
```

Measured capture exports carry a `serial-v1` execution contract. Replay caps
workers at one for these worlds, matching `dream rollout`; a policy asking for
four workers therefore receives no unmeasured speedup. Legacy worlds without an
execution contract use idealized parallel timing for screening only and cannot
earn a recommendation. In that diagnostic model, elapsed time sums the maximum
observed duration in each batch. Total
cost sums all revealed attempts, including unsuccessful ones. This replaces the
paper's explicit parallelism bonus with a latency term, so doing extra work has
no independent reward. The idealized model assumes independent workers without shared quota,
queueing, contention, or context-dependent timing changes. The report names that
assumption. Unknown cost stays null; a nonzero cost weight requires measurements
for every node. Zero explicitly excludes that objective dimension.

The candidate list includes the incumbent, and development ties retain it.
Selection uses mean utility across development worlds only. Only the selected
candidate and incumbent are subsequently evaluated on held-out worlds. A shadow
recommendation requires real data, at least three independent instances in each
split, a development improvement, a positive paired lower bound above a predeclared
minimum gain, registered unused holdouts, measured execution metadata, and no
unsupported continuations in the relevant replays. These minimum counts are guardrails, not
statistical significance or a guarantee of generalization. Report trajectories
and paired per-world utilities for inspection. Never repeatedly tune against the
same holdout; use fresh untouched instances after an iteration.

An unavailable continuation is reported explicitly. It has no fabricated result
or cost. A policy hitting that boundary cannot earn a recommendation from that
replay. Even supported replay evaluates only the historical realized search
space, not the full space of possible discoveries. The canonical input SHA-256
and policy version identify every report. Preserve the manifest and report
before collecting another round. Live promotion remains a separate reviewed
change supported by fresh online evaluation.

## Relationship to our other formulas

The live router still enforces provider availability, quota reserves, load,
handover cooldown and the user's fixed/auto effort selection. It does not consume
replay output. The Bayesian shadow policy estimates configuration-level success
and now optionally subtracts `success_value × uncertainty_weight × posterior
stddev` from expected utility. Its default uncertainty weight is zero, retaining
existing behavior. The weight is a preference, not a fitted optimal constant.
See [Bayesian shadow](bayesian-shadow.md) for evidence and abstention rules.


## Ready-to-run capture pilot

Create a synthetic task without making provider calls (choose an installed CLI's
supported model and effort). Paths below refer to the installed release root or
the repository checkout:

```bash
python3 examples/dream/create-pilot.py /tmp/dream-pilot \
  --provider codex --model YOUR_MODEL --effort YOUR_EFFORT
agent-workspaces dream init /tmp/dream-pilot/spec.json /tmp/dream-capture
agent-workspaces dream status /tmp/dream-capture
agent-workspaces dream attempt /tmp/dream-capture smoke --parent root
agent-workspaces dream attempt /tmp/dream-capture smoke --parent 000001
agent-workspaces dream export /tmp/dream-capture > /tmp/dream-replay.json
agent-workspaces dream replay /tmp/dream-replay.json
```

`dream attempt` and `dream rollout` invoke a model and consumes provider quota. Its successful
CLI exit means the attempt was recorded; inspect `accepted` and `failure_category`
to determine its result. `init` runs the
user-supplied independent verifier on the baseline. `status` suggests parents
using the frozen incumbent and already observed scores. A suggested parent is
not a launch request. The pilot stays synthetic even though its provider calls
are real: it never recommends production promotion or contributes receipts to
the production ledger.

For real tasks, provide a spec with `data_kind: real`, the same frozen policy and
objective fields as the example, `agent` (provider/model/effort and optional
required model_version), `timeout_seconds`, `max_attempts_per_world`, and worlds
with unique IDs/instance IDs, split, repository, optional Git ref, prompt_file,
and verifier. Input paths resolve relative to the spec. Use representative,
distinct real issues; do not relabel the arithmetic pilot as real evidence.

Capture uses binary end-to-end acceptance: score 1 means a completed provider
attempt passed the independent verifier, score 0 means no accepted candidate was
produced. Quality, execution, protocol and verification infrastructure failures
remain distinct in each node/receipt. Zero for an operational failure does not
assert that the model's reasoning was incorrect. The best observed score retains
any previously accepted parent. All unsuccessful attempts retain available cost
and timing. A verifier takes one request JSON path and exits 0 for acceptance,
1 for a completed quality rejection, or 2+ for infrastructure failure.

The baseline is verified before provider calls. Each candidate is captured before
verification; the verifier executes in another clone, so test-generated files
cannot become part of the candidate or its next refinement. The verifier can run
arbitrary local code and must be trusted independently of the candidate. These
same-user processes are not an adversarial security boundary.

### Isolation, evidence and operation

- The source Git repository must be clean. Capture uses the selected commit;
  ignored files are not copied, submodules are rejected, and dependencies must
  already be available to the verifier. New non-ignored files, tracked changes,
  modes, symlinks and deletions are retained in result Git snapshots.
- Each attempt clones its recorded parent and receives only baseline and selected
  ancestor diagnostics/output as explicitly untrusted context (last 4,096 characters
  per artifact); the exact sent prompt is retained. Sibling and future outcomes
  are excluded. Root branches reset to the baseline;
  a non-root parent can have only one continuation. Existing workspace sessions,
  provider assignments and source branches are never resumed or edited.
- A nonblocking world lock prevents two collectors from racing on its lineage.
  Different worlds can run in separate windows concurrently. This version runs
  one requested attempt per command; replay's worker batching is simulated.
- Settings, dataset splits, prompts, verifiers and engine hashes freeze at init.
  Required `model_version` mismatches fail the attempt; unattested served versions
  remain null and are explicitly retained alongside requested settings and CLI
  versions. Configuration fingerprints are not proof of provider attestation.
- The private capture directory holds prompt text, code snapshots, model output,
  verification evidence, lineage and receipts. Treat it as sensitive. Nothing is
  automatically pushed to GitHub. Central telemetry receives structured real
  attempt receipts only, without those raw contents.
- Completed real attempts ingest idempotently. If ingestion is disabled or fails,
  artifacts remain local; `dream ingest DIRECTORY` retries without model calls.
- Export checks frozen inputs, record/evidence hashes, parent identities and Git
  object integrity. An interrupted or crashed attempt remains visibly incomplete
  and blocks further attempts/export for that world. Its unknown result or cost
  is not silently discarded. Preserve it for diagnosis; use a new dataset for a
  fresh experiment rather than editing records to erase the attempt.
- Retain the capture directory with its exported manifest. After a software
  upgrade, incompatible capture engines refuse to append; use
  `python3 DIRECTORY/engine/dream.py ...` to operate with its frozen engine.
- Model timeout and per-world attempt budget are enforced. Capture does not
  schedule background calls, automatically alter live routing, generate policy
  code, or tune the utility weights. Freeze these preferences before evaluating
  held-out outcomes, and use fresh holdouts after revisions.

The foundation now covers **frozen baseline → measured attempt → retained result
and lineage → checked export → policy replay**. Production policy promotion still
requires real validation and an explicit integration decision.

## Budget admission (1.8)

New capture engines enforce the current [shared budget policy](budget-control.md)
before creating an attempt. Use an economical model and low/medium effort when
budget control is enabled. Denial leaves no incomplete attempt. Older frozen
engines predate these controls: initialize a fresh pilot instead of using them
for the first budget-controlled run.

## Configuration comparison (1.9)

The idealized replay interface can start independent root attempts in every
available worker slot in one round. This is our repeated-root extension, not a
claim that the paper's distinct-node formalization was defective. Version 4 caps
measured serial captures to one worker in both replay and policy-driven rollout. Missing recorded branches are unsupported, never fabricated. Batch
latency still assumes independent workers; real account contention is excluded.

`agent-workspaces dream compare comparison.json` compares checked capture exports
for different model/effort configurations on the same task instances. Each export
must retain requested agent settings, prompt/verifier hashes and node provenance.
Use the same baseline, evaluation protocol, objective and replay policy for every
candidate. Tasks must initially fail a binary independent acceptance check.

```json
{
  "schema_version": 1,
  "difficulty": "standard",
  "min_instances": 3,
  "uncertainty_weight": 1,
  "incumbent": "low",
  "candidates": [
    {"id": "low", "export": "low.json", "sha256": "EXPORT_SHA256", "policy": "breadth"},
    {"id": "medium", "export": "medium.json", "sha256": "EXPORT_SHA256", "policy": "breadth"}
  ]
}
```

Freeze the comparison specification before evaluation. Development successes
update a Beta(1,1) prior. Selection maximizes quality weight times posterior mean
minus uncertainty weight times posterior standard deviation, then subtracts
weighted mean cost, batch time and attempts. Weights are explicit preferences;
activity counts do not learn them. Held-out evaluation compares only the selected
candidate and incumbent, after selection. At least three independent instances
in each split, complete supported replay and improvement in both splits are
required. This minimum is not a statistical significance guarantee. Use fresh
held-out tasks after inspecting results or revising the specification.

Synthetic or insufficient evidence produces an abstention. Missing monetary cost
is rejected when its objective weight is nonzero; zero cost weight excludes cost
from optimization and makes no savings claim. Provider dollar figures can be
list-price equivalents, not actual subscription credit charges. Export hashes
pin evidence; they do not authenticate arbitrary hand-written traces.

A supported result emits an offline `budget.profiles` configuration proposal,
never an automatic deployment. After prospective validation, a reviewed profile
can override a provider's simple/standard/complex model and effort in Balanced
Auto. Fixed effort and manual assignments retain their existing semantics.
Pressure reductions, protected reserves and economical experiment limits remain
in force. Historical observations without attempt receipts cannot be converted
into measured results; collect fresh attempts with the capture commands above.


## Evidence and execution contract (1.10)

The live capacity router remains a deterministic heuristic. It records eligible
candidate settings/scores, decision identity and its deterministic selection
probability when choosing among candidates. This probability is **not** randomized
exploration or evidence of support for actions never taken. Ordinary workspace
status reports still do not become independent acceptance receipts.

`agent-workspaces dream rollout DIRECTORY WORLD --steps 1` runs the frozen
incumbent policy through the same `choose` interface as serial replay. It caps
steps at the frozen allowance, checks the shared budget on every attempt, stops
on the policy's observed success/stop condition and rejects an observed-prefix
race. There is no background experiment daemon and no automatic policy promotion.
Admission stays fail-fast: no hidden queue and no bypass of active account claims.

Before the provider runs, capture writes `decision.json`: selected parent, eligible
parents, prefix digest, policy ID, decision ID and selection probability (null for
a manually chosen parent). The decision, usage, verification and failure receipts
are linked by attempt identity. Stage timings cover admission, preparation,
generation, snapshotting and verification including its isolated checkout. Their
sum is the attempt wall time. Receipt finalization, telemetry ingestion, later
integration and human wait time are outside this measurement boundary. Queue wait
is zero for this fail-fast executor, not an estimate for interactive sessions.
Workspace dispatch start/completion timestamps remain separate observations.

Configuration comparison retains the Beta mean/standard deviation calculation as
**heuristic development screening**, not a calibrated confidence bound. Promotion
uses a separate paired bound. Both comparison specifications and replay documents
may provide:

```json
"evidence": {
  "alpha": 0.05,
  "minimum_gain": 0.02,
  "utility_bounds": [-1.0, 1.0]
}
```

Those are also the defaults. Freeze them and the sample size before examining
held-out outcomes. For bounds [L,H], set R=H-L. On each independent held-out task,
compute d_i=clip(U_candidate,L,H)-clip(U_incumbent,L,H). Thus d_i is in [-R,R].
Hoeffding gives the one-sided fixed-sample lower bound:

```text
LCB = max(-R, mean(d) - 2 R sqrt(log(1/alpha) / (2 n)))
```

A recommendation requires LCB > minimum_gain, all previous provenance/support
checks, and no observed utility outside the bounds. Any observed clipping causes
abstention. The bound concerns expected **clipped** utility and relies on
independent task instances sampled from the target distribution, a fixed
candidate and sample size, and preferences fixed before holdout inspection.
It is conservative; three positive comparisons normally cannot establish gain.
It does not establish an unbounded-cost guarantee, protect against distribution
shift, or support optional stopping. Cross-task correlation requires a different
sampling unit or statistical model. More samples must be planned, not appended
until a desired conclusion appears. Automatic routing is never changed here.

The local SQLite ledger at `$XDG_STATE_HOME/agent-workspaces/dream-evaluation/`
(default `~/.local/state/...`) atomically claims held-out task identities before
evaluation. Exact repeats return the recorded result; changing candidates,
preferences or evidence requires fresh holdouts. Interrupted evaluations retain
their claims. Registered CLI policy replay and model comparison share that ledger.
Pure library `optimize(document)` calls are screening-only; `register=True` opts
into the ledger. Deleting the ledger, renaming task instances, using another
machine, or prior human inspection can defeat local protection: this is not
cryptographic proof of independence or global preregistration.

Comparison rejects mixed known served-model revisions within one candidate.
Missing served-version attestation remains explicitly unknown; requested settings
are not proof of provider identity. Historical frozen engines and captures are
preserved; initialize a new dataset for v2 decision receipts and timing.

Research motivation: [Dream-RSI v2](https://arxiv.org/abs/2609.14858v2) motivates
shared online/replay semantics; [RRSI](https://github.com/google-research/rrsi)
motivates noise and cost gates. [Bayesian orchestration](https://proceedings.mlr.press/v306/papamarkou26a.html)
motivates uncertainty-aware decisions, without making this controller
Bayes-consistent merely by using a Beta distribution. Contextual exploration,
anytime-valid inference and prospective live promotion remain future measured
stages; they require independent receipts and cannot be learned from snapshot counts.

## Implementation audit against the revised paper (2026-10-18)

The repository PDF at commit `4149ea9181ab1db80f85717ffda2c9f0f130e85b`
uses a best-score/attempt-count/batch-bonus objective while its appendix describes
a Pareto-area objective. [ArXiv v2, section 3](https://arxiv.org/html/2609.14858v2#S3)
now explicitly defines normalized quality–call hypervolume minus a parallelism
penalty. Its prompts are in Appendix D. The older PDF discrepancy should not be
presented as a finding about v2. Upstream runtime code remains unreleased as of
this audit; this project is an independent adaptation.

Our objective remains the explicit weighted utility documented above; our search
compares a supplied finite policy list, not generated policy code or a Pareto
budget sweep. Measured capture is serial. Repeated root launches are our explicit
interface extension. Neither paper-level nondegradation on fixed history nor our
statistical gate proves improvement on the next live task.

The built-in selector receives only revealed outcomes. This is an interface
restriction, not an adversarial sandbox for arbitrary policy programs. Capture
saves the exact assembled prompt and hashes its evidence: baseline plus ancestor
observations, not unrestricted sibling history. The no-sibling instruction is
not itself proof of filesystem isolation. Dependencies, provider revisions and
external tools can still change outcomes. A recorded outcome must not be reused
as a causal prediction under a different context or model.

Ordinary interactive attempts now preserve dispatcher input snapshots, but not
complete model context. They remain observational and explicitly ineligible for
replay; see [task measurement](task-measurement.md). Acceptance, integration
approval and release approval remain separate. Interactive per-call costs and
full review/policy-development overhead are not yet measured. Unknown costs must
not be replaced by zero when claiming savings. The policy evaluation cache's
implementation fingerprint remains a prepared development task, with a verifier
in `examples/measurement/`; no live policy promotion is enabled.
