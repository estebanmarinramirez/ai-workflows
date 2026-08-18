# Agent Workspaces

Agent Workspaces is an Omarchy-native control plane for persistent coding-agent worktrees. It coordinates independent providers, tmux sessions, evidence-backed tasks, integration gates, and approval-gated draft pull requests.

## Safety model

- `safe`: interactive approvals and workspace sandboxing.
- `trusted` (default): edits are accepted inside isolated worktrees; Codex remains workspace-sandboxed with automatic review.
- `yolo`: provider permission bypasses. This must be selected explicitly.

The integration checkout is human-controlled. No merge, push, or pull request happens as a consequence of an agent report.

## Commands

```text
agent-workspaces doctor
agent-workspaces dashboard
agent-workspaces health [workspace-id]
agent-workspaces snapshot WORKSPACE_ID [--write]
agent-workspaces monitor WORKSPACE_ID
agent-workspaces dispatch --workspace ID --template TEMPLATE --lead ROLE --objective TEXT
agent-workspaces status --task DIR --role ROLE --state STATE [options]
agent-workspaces refresh TASK_DIR
agent-workspaces collision TASK_DIR
agent-workspaces recover WORKSPACE_ID [ROLE]
agent-workspaces end WORKSPACE_ID
agent-workspaces archive
agent-workspaces publish TASK_DIR
agent-workspaces migrate
```

Run `make install` to deploy the CLI and user configuration. The installer creates a timestamped rollback bundle before replacing managed files.
