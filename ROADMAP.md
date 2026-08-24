# Workspaces Roadmap

Workspaces should present one mental model:

> Choose a repository → choose or create a workspace → work with agents.

## Phase 1 — Repository catalog

- [x] Persist a cross-platform catalog outside the source checkout.
- [x] Index configured filesystem roots recursively with explicit exclusions.
- [x] Recognize normal repositories, linked worktrees, and bare repositories.
- [x] Import existing managed Workspaces and manually registered paths.
- [x] Deduplicate by canonical forge identity, then Git common directory, then path.
- [x] Record branch, dirty state, worktrees, activity, workspace sets, and index time.
- [x] Refresh atomically without deleting unavailable remote records.

## Phase 2 — GitHub reconciliation

- [x] Import all repositories for configured GitHub owners with pagination.
- [x] Preserve public/private, fork, archived, default branch, and activity metadata.
- [x] Represent GitHub-only repositories as cloneable catalog entries.
- [x] Cache remote inventory with a TTL and expose explicit refresh status.
- [x] Clone only after confirmation into a configured project root.
- [x] Reconcile a clone immediately into the same canonical catalog identity.

## Phase 3 — Workspaces Manager

- [x] Make one catalog-driven manager the primary Workspaces interaction.
- [x] Group local, GitHub-only, managed, active, and recently used repositories.
- [x] Offer contextual repository and workspace actions.
- [x] Open or create adaptive one-, two-, or three-provider workspaces.
- [x] Delegate default/single-agent actions to Omarchy 4's native agent commands.
- [x] Keep direct CLI helpers for power users and automation.
- [x] Retain legacy menu routes until migration and UX tests pass.

## Phase 4 — Menu consolidation

- [ ] Replace the current submenu with one primary Workspaces entry.
- [ ] Remove duplicate Open, Manage, Health, Dispatcher, Prompt, Sessions,
      Single Agent, Provider Updates, and Default Agent rows.
- [ ] Keep those capabilities as contextual manager actions.
- [ ] Preserve searchable aliases and direct commands.

## Phase 5 — Operational hardening

- [ ] Add asynchronous refresh via systemd and launchd.
- [ ] Add stale/offline/error states without blocking local operation.
- [ ] Add catalog migrations and corruption recovery.
- [ ] Add end-to-end tests for local-only, remote-only, worktree, bare, private,
      archived, fork, one-provider, two-provider, and three-provider cases.
- [ ] Add structured telemetry for indexing latency and catalog freshness.

## Non-goals

- Never crawl the entire filesystem continuously.
- Never index credentials, provider conversations, caches, Trash, or vendored trees.
- Never clone, fetch, push, publish, or delete from a read-only refresh.
- Never claim same-provider review is cross-model verification.
