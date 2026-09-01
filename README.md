# Workspaces

Workspaces is a portable control plane for persistent coding-agent worktrees. It coordinates independent providers, tmux sessions, evidence-backed tasks, integration gates, and approval-gated publication. Omarchy provides the richest desktop adapter; macOS uses the same coordination core through a terminal-native interface.

Provider participation is capability-based, not triad-dependent. A workspace opens with every enabled provider currently installed: one provider gives a useful solo workflow, two add independent review, and three enable the full lead/reviewer/verifier pattern. Disable an unavailable subscription without uninstalling its CLI using `agent-workspaces provider-disable PROVIDER`; restore it with `provider-enable PROVIDER`.

On Omarchy, open **Workspaces → Workspaces Manager** for the primary flow. It presents one catalog of local checkouts, managed workspaces, and repositories from configured GitHub owners. Select a local repository to open a multi-agent workspace, launch Omarchy's default agent, or open a terminal; select a GitHub-only repository to confirm a clone and open it.

## Install

### Omarchy

```bash
make install
```

### macOS

Install Homebrew, clone this repository, then run:

```bash
make install-macos
workspaces
```

The macOS installer uses Homebrew Bash and GNU compatibility tools, installs a `launchd` monitor per opened workspace, and keeps all agents in persistent tmux sessions. `workspaces-open /path/to/repository my-feature` is the non-interactive entry point. Desktop window tiling is intentionally optional; tmux persistence and the coordination contract do not depend on a particular terminal or window manager.

## Safety model

- `safe`: interactive approvals and workspace sandboxing.
- `trusted`: edits are accepted inside isolated worktrees; Codex remains workspace-sandboxed with automatic review.
- `yolo` (default): provider permission bypasses for workers and the Orchestrator. Architectural boundaries still prohibit cross-worktree edits, automatic integration, pushes, and publication. Changing a running session's profile takes effect on its next restart.

The integration checkout is human-controlled. No merge, push, or pull request happens as a consequence of an agent report.

## Commands

```text
agent-workspaces doctor
agent-workspaces dashboard
agent-workspaces catalog refresh [--github]
agent-workspaces catalog show
agent-workspaces catalog add PATH
agent-workspaces catalog clone REPOSITORY_ID [DESTINATION] --confirmed-by-user
agent-workspaces health [workspace-id]
agent-workspaces snapshot WORKSPACE_ID [--write]
agent-workspaces monitor WORKSPACE_ID
agent-workspaces monitor-start WORKSPACE_ID
agent-workspaces monitor-stop WORKSPACE_ID
agent-workspaces attention WORKSPACE_ID
agent-workspaces event-review WORKSPACE_ID
agent-workspaces dispatch --workspace ID --template TEMPLATE --lead ROLE --seam SEAM --objective TEXT
agent-workspaces sync WORKSPACE_ID [--confirmed-by-user]
agent-workspaces activate-reviewers TASK_DIR
agent-workspaces next TASK_DIR
agent-workspaces advance TASK_DIR ACTION --confirmed-by-user [--title TITLE] [--evidence TEXT]
agent-workspaces integrate TASK_DIR --commit SHA [--commit SHA...] --confirmed-by-user
agent-workspaces status --task DIR --role ROLE --state STATE [options]
agent-workspaces refresh TASK_DIR
agent-workspaces collision TASK_DIR
agent-workspaces reconcile-pr TASK_DIR --pr NUMBER
agent-workspaces recover WORKSPACE_ID [ROLE]
agent-workspaces orchestrator-set PROVIDER MODEL [WORKER_MODEL]
agent-workspaces end WORKSPACE_ID
agent-workspaces archive
agent-workspaces publish TASK_DIR [--confirmed-by-user]
agent-workspaces migrate
```

The Omarchy installer creates a timestamped rollback bundle before replacing managed files.

The desktop grid has one pane per available configured provider plus one conversational orchestrator, capped at four panes total. Three providers produce the full 2×2 layout; two providers produce three panes; one provider still produces two panes. Opening an older workspace reconciles its saved roster so newly available providers are no longer omitted.

The monitor itself is deterministic and consumes no model tokens. When material state changes, the optional event reviewer writes a short `briefing.md` using the configured lightweight model. A deduplicated pending event then wakes the persistent conversational Orchestrator as soon as its prompt is empty; busy turns and user-typed drafts are never overwritten. Undelivered events retry every monitor interval.

Visible provider prompts are not treated as completion signals because some CLIs render their composer while tools are still running. Workflow state changes only from explicit agent reports, confirmed process failure, or an operator-initiated recovery.

Worker sessions use focused debug-profile checks during implementation. Full repository validation, including release/LTO builds, runs once on the final reviewed commit unless the task is specifically release-only. Worktrees retain separate build directories; if `sccache` is installed, new worker sessions enable it automatically to reuse safe compiler artifacts across worktrees.

Provider manifests declare their CLI update manager. Use **Agent Workspaces → Provider Updates** to inspect and selectively update provider CLIs. Updates retain the prior provider installation so running sessions and their lazily spawned command hosts remain usable; restarted sessions pick up the new binary.

## Models and usage

The conversational Orchestrator uses Claude `fable` with Remote Control enabled. The independent Claude worker uses `opus` at high effort, while Codex uses `gpt-5.6-sol` and Grok uses `grok-4.6` at high reasoning effort. The event reviewer stays on `gpt-5.6-luna` at low effort because it summarizes deterministic state changes rather than writing production code. Provider commands and the documented policy live in `config/providers/` and `config/config.json`.

Agent Workspaces extends Omarchy's native **Agents** bar panel rather than installing a separate widget. Its user-local updater preserves Omarchy's Claude and Codex collectors and adds:

- Grok's authoritative shared weekly subscription percentage, reset time, local token totals, and model attribution. API-equivalent cost fields embedded in sessions are deliberately excluded because they are not subscription charges or model-specific limits.
- Workspace delivery health: only the newest workflow per workspace is considered current; historical records feed seven-day completions without inflating blocked or active counts. The panel also shows current delivery-budget pressure.

Run `agent-workspaces-usage-update` to refresh the added records immediately. A user-level timer keeps them current every two minutes; Omarchy continues refreshing its built-in provider records normally. The figures are local operational telemetry, not a provider invoice: account limits come from each provider's authenticated CLI where available, while local session totals measure work recorded on this machine.

Additional panel collectors use an `id=executable` registry and publish through the shared runtime in `lib/usage.sh`. The runtime validates Omarchy's record contract, writes atomically, logs per-collector outcomes under the Omarchy agents state directory, and retains the last good record when a collector fails. Run `agent-workspaces-usage-check` for collector, record, freshness, and recent-event diagnostics. For development or downstream packaging, override the registry with colon-separated `AW_USAGE_COLLECTORS` entries.

Active workflows use quota-aware delivery rather than a wall-clock deadline. The Workspaces record averages the most constrained live usage limit from each participating provider into a Pooled model quota, so the indicator tracks actual provider consumption and does not become permanently full merely because a workflow has been running for 90 minutes.
