# Bayesian shadow evaluation

The shadow policy records what a Bayesian controller would recommend **before**
a benchmark trial starts. It never launches an agent, changes the trial's
assigned configuration, or writes to production routing. Fixed/Auto preferences,
manual assignments, quota guards, handovers and permission gates remain intact.

This is a small, testable application of the control-layer position in
[Papamarkou et al., ICML 2026](https://proceedings.mlr.press/v306/papamarkou26a.html).
It is not a claim of universal optimality or asymptotic Bayes consistency. The
observation model and its assumptions must be evaluated on real tasks.

## Enable it in a new experiment

Add these fields to a benchmark manifest before initialization:

```json
{
  "data_kind": "real",
  "shadow": {
    "enabled": true,
    "prior_alpha": 1,
    "prior_beta": 1,
    "min_tasks": 3,
    "success_value": 1.0,
    "cost_weight": 1.0,
    "latency_weight": 0.001,
    "minimum_success_mean": 0.0
  }
}
```

Every task must also have a `family` (for example `python-repair`) and a globally
unique `instance_id` identifying the underlying issue. Do not give the same issue
different instance IDs to inflate evidence. Use `repeats` for repeated attempts.
An ID cannot appear in both development and held-out splits.

Every configured role must include `model_version`. The adapter must report the
same version alongside provider/model/effort in its usage receipt. Pin a specific
release or a recorded provider version identifier where available; an alias alone
is not reliable version evidence. The harness can validate receipt agreement,
not independently prove what model a provider served. New versions require new
experiments. Configuration fingerprints cover topology, roles and their versions,
and adapter/verifier script hashes.

`shadow_eligible: false` on a configuration excludes it from recommendations,
while retaining its scheduled benchmark trials for comparison. Eligibility is a
frozen experimental constraint, not a live quota or availability observation.

The shipped demo is explicitly `synthetic`, with `demo` providers. Its shadow
records exercise the control path but never train posteriors, recommend a winner,
or count in calibration scores. Unlabelled experiments cannot enable shadow
learning. Marking an adapter as real is a researcher-supplied provenance assertion;
the harness cannot detect all synthetic adapters or invented receipts.

## Beliefs and evidence

For each configuration and task family, start with Beta(alpha, beta). For observed
binary outcomes, add successes to alpha and failures to beta. Forecasts include
the posterior mean, standard deviation, parameters, and contributing trial IDs.

Two beliefs are maintained separately:

- **End-to-end success:** whether the configuration produced an accepted outcome,
  including operational reliability and the measurement protocol.
- **Capability conditional on usable verification:** whether its output passed
  the independent verifier. Execution failures do not count as reasoning failures.
  This is configuration-level evidence, not attribution to an individual model.

Only completed **development** trials for the same family are eligible. Only
repetition zero contributes; this is a deliberately simple way to avoid treating
repeated attempts on one issue as independent new tasks. Even repetition zero of
the target issue is excluded from its later forecasts, across all configurations.
Different issues may still be correlated. This model assumes conditional
independence/exchangeability within the declared family and does not model that
remaining dependence. No cross-experiment pooling occurs in this version.

Held-out execution is refused until all development trials finish. The completed
development dataset is then fixed for held-out forecasts; held-out outcomes never
feed back into beliefs. Do not edit ledgers or manifests to bypass this boundary.

## Failure attribution

Outcomes carry `failure_category` and nullable `capability_label`:

| Evidence | Category | Capability label |
| --- | --- | --- |
| Verifier passes and telemetry conforms | none | true |
| Verifier exits 1 and telemetry conforms | quality | false |
| Adapter exits unsuccessfully | execution_unknown | unknown |
| Adapter exceeds timeout | execution_timeout | unknown |
| Verifier timeout or exit other than 0/1 | verification_infrastructure | unknown |
| Wrong/missing configuration receipt | protocol | unknown |
| Clone, artifact or harness error | harness | unknown |
| Interrupted execution | interrupted | unknown |

The verifier protocol reserves exit 1 for a completed acceptance check that
failed, and exit 2 or higher for inability to perform that check. Verifiers must
catch operational errors and honor this distinction. A bare Python exception can
also exit 1, so careless verifiers would mislabel infrastructure as quality.
Adapter failures deliberately remain `execution_unknown`; the harness does not
invent a cause such as poor reasoning or a missing tmux session. Detailed adapter
logs and recovery counters remain available for investigation.

## Decision rule

For each eligible candidate:

```text
expected utility = success_value × posterior mean(end-to-end success)
                 − cost_weight × mean observed dollars per attempt
                 − latency_weight × mean observed end-to-end seconds
```

Costs include unsuccessful attempts. Weights express user utility units; the
shipped defaults are explicit experimental preferences, not learned optimal
weights. Cost and latency are empirical means, not Bayesian distributions in
this first model. A zero weight explicitly removes that quantity from the
objective; it never turns an unknown measurement into an observed zero.

The policy abstains if any eligible candidate lacks `min_tasks` distinct
contributing issues or a required cost/latency measurement. It also abstains if
no eligible candidate meets `minimum_success_mean`. This floor concerns a
posterior **mean**, not a credible-bound or risk guarantee. Among remaining
candidates, maximize expected utility; exact ties break by configuration ID.
There is no automatic exploration, within-task value-of-information calculation,
or stop/review action in this initial policy.

## Pre-outcome audit and calibration

```bash
agent-workspaces benchmark shadow-report EXPERIMENT
agent-workspaces benchmark shadow-report EXPERIMENT --split heldout
```

Each trial's append-only `shadow_prediction` event is committed before the
`started` event and before any adapter execution. It contains:

- Policy version and utility/prior settings.
- Actual assigned configuration and the shadow recommendation (or abstention).
- Every candidate's configuration fingerprint, beliefs, costs, and utility.
- Training trial IDs and the maximum visible event ID at prediction time.
- Target task family/instance and split.

The experiment freezes both `harness.py` and `shadow.py`; execution checks their
hashes. To resume an old experiment, run its frozen harness. Legacy v1.3 outcomes
remain readable and are not backfilled with hindsight predictions.

`shadow-report` joins stored forecasts to subsequent observed outcomes, reporting
Brier score, log loss, per-trial probabilities and failure categories. It scores
the forecast for the **actually executed** configuration, not an unexecuted
recommendation. Synthetic/demo forecasts are counted separately and excluded.
Repeated trials remain visible in calibration output, so the averages are
descriptive and do not imply independent-sample confidence intervals.

A lower Brier score is useful evidence about predictive accuracy, not proof that
acting on recommendations improves outcomes. Establish that separately with a
prespecified, held-out policy comparison and enough distinct real tasks. The
existing randomized configuration matrix supplies observational matched outcomes;
this report does not claim causal improvement or counterfactual savings.

## Boundaries for this release

Real provider adapters and a representative labelled task set are still needed
before performance conclusions are possible. Sparse real data produces explicit
abstention, not a confident default recommendation. Current constraints stay
outside the Bayesian layer; enabling shadow mode does not make a live assignment
eligible, override an approval, or change the user's selected effort mode.
