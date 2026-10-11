# Shared budget control

Budget control is an admission policy layered over quota routing. It is not a
billing cap: CLI turns can contain several model requests, exact subscription
credit costs may be unknown, and an already-running turn may cross a reserve
threshold. This controller never invents dollar costs or interrupts an active
provider command to enforce a guessed bill.

## Defaults and scope

The shipped configuration keeps `budget.enabled: false` for existing installations
until explicitly enabled. All other limits are available as `budget` keys:

| Setting | Initial value | Meaning |
| --- | --- | --- |
| external_reserve_fraction | 0.30 | New managed work requires more than 30% reported account capacity remaining, including review work. |
| effort_pressure_fraction | 0.50 | Balanced Auto reduces effort once 50% of quota is used, before the launch reserve is reached. |
| experiment_reserve_fraction | 0.50 | RSI requires more than 50% remaining. |
| experiment_attempts_24h | 2 | Shared rolling 24-hour allowance across all Dream datasets/accounts. Failures count. |
| experiment_concurrency | 1 | At most one admitted Dream attempt at once. |
| managed_concurrency_per_pool | 2 | Shared managed/experiment occupancy limit per account pool. |
| boost_max_minutes | 10 | Maximum duration of one temporary lead boost. |
| boost_minutes_24h | 20 | Shared rolling 24-hour allocation for boosts. |
| experiment_timeout_seconds | 120 | Maximum permitted frozen timeout for an RSI attempt. |

Account-pool aliases use `routing.account_pools`. Fresh authoritative quota is
required for automated wakeups, handovers, measured work and experiments.
Interactive CLI startup uses `budget.unknown_interactive: "warn"` by default:
missing/stale telemetry displays a warning and opens the CLI, so users can
inspect account status and obtain fresh observations. Set this to `"block"`
for strict startup admission. Known low quota and shared concurrency limits
still block interactive startup, including restrictive partial account-pool
evidence. Unknown capacity is never reported as available. Direct interaction
can spend quota whose reserve cannot be verified; this exception is not a
billing guarantee and does not authorize automated work.
When enabled on Omarchy, the existing two-minute usage timer also refreshes native
Codex/Claude account limits, so admission does not depend on opening the desktop
panel. These are quota queries, not model turns. Failed refreshes never authorize
spending from guessed capacity. Reserves are admission thresholds, not guaranteed
balances after a long turn.

The gate covers managed fresh/resume/recovery launches, handover launches,
explicit `attempts run`, automated orchestrator wakeups, and new Dream capture
engines. Optional automatic model event-reviews are skipped while budget control
is enabled. Launches use atomic SQLite claims; provider processes retain the
claim PID through exec. Claims stay occupied for the CLI lifetime, including idle
prompts, until it exits. Dead holders are reaped on the next admission. Busy
managed tmux panes started before installation are also counted when identifiable.
Pane claim IDs avoid counting the same managed process twice. If tmux observation
is unavailable, only registered live claims are known; quota reserves still apply.

Experiments wait behind any observed/registered managed work on their account.
Unmanaged ChatGPT/Research/CLI sessions are not controlled or fully observable;
the external reserve leaves headroom for them. Previously frozen benchmark or
Dream harnesses, raw provider commands and direct API calls are outside this
wrapper. Do not use an older frozen harness to bypass the new budget policy.
A fresh economical Dream pilot should be initialized before the first run.

The ledger is private under `$XDG_STATE_HOME/agent-workspaces/budget/ledger.sqlite`
(default `~/.local/state/...`). Admission failures do not create Dream attempt
artifacts or consume the attempt allowance. After admission, operational failures
consume it conservatively. Budgets are rolling elapsed-time windows, not daily
midnight resets. Nothing automatically purchases credits or upgrades plans.

## Balanced Auto versus Fixed

```bash
agent-workspaces budget status
agent-workspaces routing WORKSPACE effort-mode --mode balanced
agent-workspaces routing WORKSPACE effort-mode --mode fixed
```

The workspace agent chooser exposes **Balanced Auto · conserve quota**, the
existing Auto mode, and **Keep current model and effort settings**. Explicitly
selecting Balanced Auto releases existing manual assignments to the routing
policy, through the same checkpointed handover mechanism. Changing the global
default alone does not erase manual pins or workspace Fixed overrides.

Balanced Auto starts simple work on economical models at low effort. Standard
work uses Sol/medium, Sonnet/medium, or Grok Build Fast/medium. Complex work uses
Sol/high, Opus/high, or Grok/high. Quota pressure reduces medium to low and high
to medium. Existing catalog capability validation still applies. These are
explicit operating preferences, not learned optimal settings or guarantees of
account access. Fixed keeps the requested model/effort but remains subject to
launch, reserve and concurrency admission checks.

A monitor re-evaluates eligible active tasks and requests changes at checkpoints.
An initial unassigned Balanced launch uses the standard profile. Already-running
turns are not changed in place. Direct typing into an interactive CLI cannot be
intercepted by this controller; automated wakeups and task dispatch are gated.

## Bounded escalation

For an eligible lead in Balanced Auto, a difficult task can receive a temporary
explicit boost, for example:

```bash
agent-workspaces budget boost WORKSPACE codex --model gpt-6-astra --effort high --minutes 10
```

Only one boost grant can be active globally. Effort is capped at high. The full
requested duration is charged when granted, even if checkpointing delays its
use. A boost is a routing candidate, not a promise of immediate activation:
manual pins, provider eligibility, load, quota, pending handovers, and checkpoint
requirements still apply. It affects the lead only. Under quota pressure it is
suppressed. There is no automatic escalation based on an unverified claim that
an agent is stuck; a coordinator may request a boost after inspecting the blocker.

After expiry, unlaunched boost requests are cancelled without recording a user
veto, and launch-time validation refuses expired requests. Balanced routing
returns an active boosted lead to its ordinary profile at its next safe
checkpoint, bypassing the cooldown when settings need to change. Expiry never
kills an in-flight model request; the time allowance is a scheduling window,
not a hard limit on already-started computation.

## RSI before the first run

With budget control enabled, frozen Dream settings must select the economical
simple-profile model for their provider (Codex Luna, Claude Sonnet, or Grok Build
Fast), with low/medium effort and a timeout within the cap. The controller refuses
expensive settings rather than silently changing a frozen experiment.

Initialize a new synthetic pilot with an economical model and low effort. Its
frozen engine includes the budget module but reads the current user budget
configuration at each admission. Original prompt/model/evaluator settings and
source evidence stay frozen. `dream status` reads the dataset; `budget status`
shows current quota and allowances. Neither makes provider calls. Run experiments
when foreground sessions have released the shared account.

## First-run isolation and cancellation

Measured Claude calls disable configured MCP servers explicitly in addition to
restricted file tools. Experiments count their invoking managed pane for account
occupancy; only interactive CLI replacement excludes its own launching pane.
Malformed existing budget configuration fails closed rather than reverting to
disabled defaults. A missing configuration still supports an unconfigured install.

Normal termination (SIGTERM) cleans up an isolated measured provider process
group before releasing the budget claim. Interrupted captures can remain
incomplete and must be inspected, not automatically retried. SIGKILL or machine
failure cannot run this cleanup; inspect surviving processes before another run.
A new frozen dataset is required to use these adapter and admission fixes.

The usage timer includes a Codex quota-only fallback when the native collector
returns unknown capacity. It reads app-server responses with an explicit byte
buffer so batched notifications cannot hide the account response. It publishes
only successfully validated limits and makes no inference calls. A working
native reading skips this additional query. Omarchy package files are unchanged.

## Explicit exception for unavailable quota feeds

`budget.unknown_managed_providers` defaults to an empty list. A user can opt a
specific provider into ordinary managed work despite unknown quota, for example
`["grok"]`. This permits existing-provider review activation and automated
wakeups; it does not mark quota as available or enable automatic routing into
an unverified pool. The controller warns that reserve protection cannot be
verified. Known low quota (including partial pool evidence) and process
concurrency still apply. Experiments and temporary boosts never use this
exception. Remove the provider from the list to restore strict admission.

New workflow tasks are persisted as `awaiting_configuration`. The dispatcher
reconciles routing using that task's difficulty and sends work only after the
required handover is acknowledged, budget admission succeeds and each selected
CLI has an empty idle composer. The monitor resumes this queue automatically.
`dispatching` means delivery began: after interruption inspect the panes and
recorded sent slots before manual recovery; the monitor never blindly resends.
This preserves existing in-flight work, user drafts, Fixed mode and manual pins.

Reviewed Dream comparison proposals may set
`budget.profiles.PROVIDER.DIFFICULTY` to a model/effort object for Balanced Auto.
These overrides do not widen the measured-experiment model allowance or disable
quota protection. See [configuration comparison](dream-replay.md).
