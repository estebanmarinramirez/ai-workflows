# Offline Dream-RSI adaptation

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
The example is the complete executable schema. Snapshot identifiers must refer
to externally retained immutable artifacts; this tool does not restore or
validate their contents. Evidence digests must point to retained verifier
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
Building a real world still requires a trace producer to retain those artifacts
and assemble the manifest. No real world was fabricated from the activity ledger.

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
