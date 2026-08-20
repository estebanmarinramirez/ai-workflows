# Agent Workspaces

Agent Workspaces is an Omarchy-native control plane for persistent coding-agent worktrees. It coordinates independent providers, tmux sessions, evidence-backed tasks, integration gates, and approval-gated draft pull requests.

## Safety model

- `safe`: interactive approvals and workspace sandboxing.
- `trusted`: edits are accepted inside isolated worktrees; Codex remains workspace-sandboxed with automatic review.
- `yolo` (default): provider permission bypasses for workers and the Orchestrator. Architectural boundaries still prohibit cross-worktree edits, automatic integration, pushes, and publication. Changing a running session's profile takes effect on its next restart.

The integration checkout is human-controlled. No merge, push, or pull request happens as a consequence of an agent report.

## Commands

```text
agent-workspaces doctor
agent-workspaces dashboard
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
agent-workspaces end WORKSPACE_ID
agent-workspaces archive
agent-workspaces publish TASK_DIR [--confirmed-by-user]
agent-workspaces migrate
```

Run `make install` to deploy the CLI and user configuration. The installer creates a timestamped rollback bundle before replacing managed files.

The monitor itself is deterministic and consumes no model tokens. When material state changes, the optional event reviewer writes a short `briefing.md` using the configured lightweight model. The persistent conversational Orchestrator uses its separately configured flagship model.

Provider manifests declare their CLI update manager. Use **Agent Workspaces → Provider Updates** to inspect and selectively update provider CLIs. Updates never interrupt running sessions; restarted sessions pick up the new binary.
