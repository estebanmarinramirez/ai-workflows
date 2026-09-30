# Quota routing and effort modes

The monitor evaluates account capacity and requests checkpointed handovers for
the lead, reviewers/verifiers and orchestrator. The four-window architecture,
role worktrees, task history, permissions and publication gates stay intact.
Model/provider changes are never applied by typing `/model` into a running turn.

## User choice

Open **Workspaces → Change Agents → Effort mode and quota routing**:

- **Keep current model and effort settings** (`fixed`, the default): retain the
  current settings for each slot. A quota-driven provider change uses the target
  provider's configured model/effort, without substituting a cheaper tier.
- **Auto · balance model and effort** (`auto`): use the task's difficulty profile
  and its pressure-effort setting. Selecting Auto explicitly releases existing
  manual assignments to routing control. Subsequent manual agent selections pin
  that slot until Auto is chosen again.

The choice is saved in the workspace manifest. It never changes other workspaces
or interrupts a process. Unlaunched automatic requests are cancelled when the
mode changes. A launched successor must acknowledge or exit and be cancelled
before changing mode.

```bash
agent-workspaces routing WORKSPACE
agent-workspaces routing WORKSPACE --json
agent-workspaces routing WORKSPACE effort-mode --mode auto
agent-workspaces routing WORKSPACE effort-mode --mode fixed
agent-workspaces routing WORKSPACE reconcile
agent-workspaces routing WORKSPACE difficulty --task TASK_DIR --difficulty complex
agent-workspaces dispatch --workspace WORKSPACE --template feature --lead codex \
  --seam general --objective 'Implement the requested feature' --difficulty standard
```

## Decisions

Task difficulty is explicit: `simple`, `standard` (default), or `complex`.
The orchestrator classifies the requested work; the router does not guess from
keywords or spend tokens classifying it. In Auto, simple work uses lighter
configured models, standard work uses the configured worker models, and complex
work retains those models with deeper reasoning. Defaults are editable under
`routing.profiles` in the application config, including effort floors under
pressure. Profiles express preferences, not measured quality or token-cost
predictions. Existing provider catalogs validate supported effort levels.

Fresh, ready provider telemetry supplies the most constrained usage limit and
next observed reset. Default freshness is five minutes. Missing timestamps,
future timestamps beyond clock tolerance, invalid percentages, expired reset
observations, missing limits and unavailable accounts are **unknown**, never
zero usage. A passed reset does not imply a replenished quota without a fresh
observation. Model availability still depends on the provider/account; optionally
restrict candidates through `routing.allowed_models.PROVIDER`.

All slots using one provider share one account pool, including the orchestrator.
Acknowledged provider assignments override historical slot names and task
metadata. Slot load across managed workspaces influences routing, without
multiplying the account's quota. Use `routing.account_pools` only when two
provider identifiers actually share one account limit. Multiple credentials per
provider are not currently supported.

The default policy reserves the last 15% for review/verification. New
implementation dispatches cannot consume that reserve, while review can proceed
until exhaustion. Candidates are scored by remaining capacity above reserve
relative to occupied slots. Cross-account review is preferred when available;
otherwise reduced independence is reported. A 15-percentage-point score
improvement and 15-minute cooldown avoid routine switching churn. Difficulty
changes and reserve exhaustion bypass the cooldown. Cancelled equivalent routing
choices do not automatically reappear. Unknown capacity keeps current settings;
it does not authorize a switch to an unverified account.

## Applying decisions

Automatic routing creates a normal handover request and records its reason.
It cannot interrupt commands, invent a handover note or commit code. The outgoing
agent must produce a clean checkpoint and exit, and the successor must
acknowledge before taking ownership. Reviewers remain deferred until activated.
Preferences, provider availability, model capabilities and target quota are
checked again immediately before launch.

The standard dispatcher and reviewer activation refuse known exhausted/reserved
capacity or a pending handover. These checks do not enforce a hard token budget
inside a provider's already-running turn. Token costs are not interchangeable
across providers, and the UI's pooled percentage is descriptive, not a spending
balance. Direct provider CLI use outside this control plane is unaffected.

`routing.mode` supports `automatic` (default), `recommend` (observe only), and
`off`. Explicitly pinned assignments are never silently overridden; a quota
block on a pinned agent requires a user choice or a refreshed quota observation.

The snapshot includes capacity, effort mode, decisions, and independence warnings.
`routing.json` stores the latest applied evaluation and `routing-audit.jsonl`
records changes, including telemetry timestamps and decision reasons. Stable
monitor ticks do not append duplicate records; displayed quota bands avoid
waking the orchestrator for every small usage change.

## Tests

The regression suite covers profile and effort selection, preserving Fixed mode,
stale/missing/invalid limits, exhausted accounts, review reserves, provider
switches and duplicate slots, cross-workspace load, independence, allowlists,
manual pins, user cancellation, cooldowns, preference changes, handover
idempotency, reviewer ownership, and launch-time capacity validation. The chooser
is exercised with both user-selectable modes. Tests use isolated fixtures and
never launch paid model turns or alter live agent sessions.
