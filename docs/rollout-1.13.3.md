# Agent Workspaces 1.13.3 deployment candidate

This is artifact preparation only. Independent review/verification and the single final
`make check` after accepted review repairs are still required before installation.
No installation, integration, publication, paid request or live desktop operation was performed.

## Inputs and composition

Installed baseline: `3fd4a2a5b7c6ba299a5ea0ac9cdd75552ff9e18d`, sole parent
`bc3343195855e78d5f3498fcb1c3c93630ab52df`. All 90 snapshot SHA-256 values
were checked against Git blob contents and match. Reviewed source:
`4970d4cd7d838711ae403dc7ed51dd6a420ff0c2`, tree
`376e16ed3fcb667bbe6704e0a2d3f780a7b3c5b8`.

The candidate is a cumulative commit on the installed baseline, composed with
`git merge-tree --write-tree` and a temporary index tree; no branches were merged.
Prior HEAD is preserved as `codex-pre-rollout-20261030` (4af5ca8).
The named reproduction stash e6a0969282f30183d4a3ed15ed59e6a163a542d1 remains untouched.

Only two paths conflicted:

- `compat/bin/omarchy-agent-grid`: use reviewed majority/saved workspace choice and
  fail-closed reflow helper; retain installed stale-window/session recovery, valid
  application classes, orchestrator recovery and monitor startup. Remove obsolete
  inline placement/floating checks now performed by the helper. Monitor startup
  failure still propagates before focus.
- `lib/routing.py`: retain installed explicit credit-backed policy and pressure handling,
  using reviewed normalized credit eligibility and low paid-capacity scoring. Keep
  unknown/no-collector fallback only for explicitly configured ordinary authority.
  Observed credit evidence takes precedence over that fallback; cooldown also checks
  actual eligibility so fresh denial cannot be hidden by a recent routing change.

Necessary interaction changes: budget admission now reads authoritative evidence
before the explicit fallback; capacity rows carry `evidence_present`, conservatively
ORed across aliases. Even empty/malformed/stale sidecars disable the no-collector
fallback. Fresh denial, spend caps, configured minima and unknown shared aliases
remain binding. Experiments/boosts do not use the fallback. No existing authority
is enabled or settings changed. VERSION is 1.13.3; installed VERSION loading,
manager/catalog/pane behavior, rollback and telemetry additions are retained.

## Validation and evidence reuse

Rerun: `python3 tests/credits` (22 pass), `python3 tests/routing` (34 pass),
`python3 tests/budget` (21 pass), `python3 tests/desktop-layout`,
`python3 tests/desktop-reflow`, and `git diff --check`.
Compatibility regressions cover explicit credit policy versus authoritative denial,
spend cap, zero/malformed/stale credits, minimum, malformed sidecar and shared alias;
positive and no-collector ordinary allowance; experiment/boost exclusion; persistent
session launcher reopen and monitor-start failure, transformed/negative monitor geometry.

New mutations: removing the observed-evidence guard fails five credit assertions;
removing monitor-start fails the launcher assertion. Both mutations exited 1 and
production bytes were restored. Logs are retained locally as `.git-rollout-*.log`.
The initial partially composed tree failed the credit suite; the targeted semantic
revert above supplies the meaningful failure proof for the completed compatibility fix.

Reused: authoritative role JSON reports for 20261022-165240-339 and
20261022-194224-023, source regression/mutation evidence through 05c5e66,
combined-source `make check` exit 0 in integration-20261030/result.json. These
are source evidence, not a full-gate result for this new candidate. Do not duplicate
the final full gate in independent worktrees. Run it once after review repairs on
the accepted immutable candidate and record that exact SHA in role evidence.

RPC and compositor evidence remains mocked. Claude/Grok credit collection remains
unsupported; explicit no-collector authority does not invent a verified balance.

## Bounded install and rollback plan for the orchestrator

1. Review the immutable candidate and record the final exact SHA/tree and one full
   `make check` result. Confirm installed-baseline file hashes still match; if runtime
   drifted, stop and reconcile. Keep the integration checkout and its 15 WIP files
   byte-for-byte intact; use an isolated artifact export for packaging.
2. Before installation, retain the entire current 1.13.2 install, managed binaries,
   symlink targets, current-version, systemd units and timer state, menu, complete
   agent-workspaces config including provider catalogs/instructions/templates and
   user provider settings. Record hashes and live tmux session/pane/process identities.
   Preserve drafts and all repository WIP. Retain backups outside version directories.
3. Stage the reviewed 1.13.3 export and inspect all installed payload paths. The stock
   packager overwrites provider catalogs and enables/starts usage units: orchestrator
   must account for these effects, preserve/restore exact existing user provider
   settings, and retain original unit/timer state rather than silently accepting a
   provider-policy change. Do not invoke the stock packager blindly. Its own backup
   and rollback bundle supplement the explicit complete backup.
4. Only the orchestrator performs the already-authorized bounded installation.
   Validate VERSION, normalized credit/routing agreement and helper resolution with
   read-only commands first. No paid inference is necessary. Live window recovery
   smoke tests belong to orchestrator rollout authority; preserve all tmux sessions,
   processes and drafts, and stop on mismatch. Workers do not move windows or restart
   monitors/sessions. No push, PR or other publication is authorized.
5. On failure, restore prior managed binaries/symlinks, full config/provider bytes,
   units and recorded timer state, menu and current-version from the explicit backup
   (or verified packager rollback bundle); retain failed 1.13.3 and diagnostics for
   analysis. Verify 1.13.2 hashes and the same live session/process identities. Never
   reset or clean repository WIP, kill sessions, or drop preservation stashes.

## Exact content delta from installed baseline (excluding this report)

```text
6	0	Makefile
1	0	README.md
1	1	VERSION
48	3	bin/agent-workspaces
18	93	compat/bin/omarchy-agent-grid
177	0	compat/bin/omarchy-agent-grid-reflow
9	2	compat/bin/omarchy-agent-usage-workspaces
6	2	config/instructions/ORCHESTRATOR.md
48	1	docs/budget-control.md
23	3	lib/assignments.py
25	8	lib/budget.py
145	4	lib/capacity.py
52	27	lib/codex_quota.py
52	11	lib/core.sh
24	18	lib/routing.py
1	1	schemas/status.schema.json
1	1	schemas/task.schema.json
45	0	tests/assignments
16	3	tests/budget
80	0	tests/collision
77	0	tests/combined-interactions
100	0	tests/credit-wake
397	0	tests/credits
24	15	tests/desktop-layout
171	0	tests/desktop-reflow
124	0	tests/hyprctl-double
113	0	tests/workflow-deadlock
```

## Exact content delta from reviewed composition (excluding this report)

```text
1	1	VERSION
39	7	compat/bin/omarchy-agent-grid
27	3	compat/bin/omarchy-agent-pane
83	15	compat/bin/workspaces-manager
38	2	compat/bin/workspaces-manager-preview
16	0	docs/budget-control.md
10	0	lib/assignments.py
11	1	lib/budget.py
10	4	lib/capacity.py
179	10	lib/catalog.py
2	1	lib/core.sh
16	3	lib/routing.py
1	1	lib/telemetry.py
1	0	packaging/install
31	2	packaging/rollback
39	0	tests/credits
11	3	tests/desktop-layout
```
