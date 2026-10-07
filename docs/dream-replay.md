# Dream-RSI capture and offline replay

This is an independent, limited adaptation of
[Dream-RSI](https://github.com/zhengkid/Dream-RSI) and
[the paper, section 3](https://arxiv.org/html/2609.14858v1).
As checked on 2026-10-07, the official repository publishes the paper and assets;
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
evidence SHA-256, score, measured dollars (nullable), and generation-plus-verifier
wall seconds. Scores must be nonnegative, comparable under the fixed protocol,
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

Simulated elapsed time sums the maximum observed duration in each batch. Total
cost sums all revealed attempts, including unsuccessful ones. This replaces the
paper's explicit parallelism bonus with a latency term, so doing extra work has
no independent reward. It assumes independent workers without shared quota,
queueing, contention, or context-dependent timing changes. The report names that
assumption. Unknown cost stays null; a nonzero cost weight requires measurements
for every node. Zero explicitly excludes that objective dimension.

The candidate list includes the incumbent, and development ties retain it.
Selection uses mean utility across development worlds only. Only the selected
candidate and incumbent are subsequently evaluated on held-out worlds. A shadow
recommendation requires real data, at least three independent instances in each
split, a development improvement, a held-out improvement, and no unsupported
continuations in the relevant replays. These minimum counts are guardrails, not
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

Only `dream attempt` invokes a model and consumes provider quota. `init` runs the
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
- Each attempt clones its recorded parent. Root branches reset to the baseline;
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
