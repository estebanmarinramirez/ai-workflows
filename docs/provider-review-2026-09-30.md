# Provider compatibility review — 2026-09-30

## Versions and evidence

| Provider | Installed / reviewed | Upstream evidence |
| --- | --- | --- |
| Codex | 0.159.2, current stable at review | [0.159.2](https://github.com/openai/codex/releases/tag/rust-v0.159.2), [0.159.0 changes](https://github.com/openai/codex/releases/tag/rust-v0.159.0), [model guidance](https://learn.chatgpt.com/docs/models) |
| Claude Code | 2.1.285, current stable at review | [2.1.285](https://github.com/anthropics/claude-code/releases/tag/v2.1.285), [model configuration](https://code.claude.com/docs/en/model-config) |
| Grok Build | Installed 1.0.41; npm stable 1.0.44 | [official public repository](https://github.com/xai-org/grok-build), [reviewed source revision](https://github.com/xai-org/grok-build/commit/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8), [npm distribution](https://www.npmjs.com/package/@xai-official/grok) |

These are CLI repositories, not the proprietary model implementations. Grok had
no GitHub release entries at review; its public source snapshot and npm package
are separate evidence and must not be treated as identical builds. Installed
`--help`, Codex's local model cache, Grok's `models` output and local capability
cache were also checked. Cache contents are account-local hints, not proof of
subscription access. No credentials or account identifiers are stored here.

## Changes and compatibility decisions

- **Codex:** new installations select `gpt-6.1-sol` for workers and `gpt-6-luna`
  for short event summaries. The picker retains older supported choices and
  adds cached models and their effort levels; Luna rejects Ultra. Existing
  explicit workspace assignments remain authoritative. Version 0.159 changes
  TUI headers and removes prompt suggestions: unknown screens still report
  unknown, and a visible composer never grants permission to interrupt. Keep
  explicit sandbox/approval flags. Do not enable instant interruption for
  automated handovers.
- **Claude:** retain `fable` and `opus` aliases, which follow provider routing.
  Offer current explicit IDs, xhigh/max where supported, and default effort
  for Haiku. The installed CLI confirms these flags. Alias resolution and
  model/effort restrictions can differ on gateways. The latest release fixes
  model-switch context limits, Remote Control queue persistence and hook
  hangs; retain checkpointed fresh-process handover rather than introducing
  an untested live SDK switch. Preserve Remote Control and coordination access
  when replacing an orchestrator.
- **Grok:** expose 4.7 and 4.7-build-fast from current CLI/cache evidence;
  retain 4.6 as the default because it remains the public source default.
  The [source model definitions](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-models/default_models.json)
  support xhigh for 4.6 but only high for 4.5. Upstream changes include daemon
  ownership, queued prompts and per-path tool scheduling. Do not infer daemon
  activity from a TUI footer or send duplicate prompts to compensate for
  latency. Updating the CLI remains a separate Provider Updates operation;
  this application update does not replace running provider binaries.

## Optimizations implemented

- Read model caches locally, without network calls or launching paid turns in
  the chooser. Export only model IDs and reasoning capabilities.
- Actually pass the event reviewer's low-effort policy; previously it was
  documented but omitted from the CLI arguments.
- Take one compositor snapshot per window readiness poll, rather than one
  per role. Correct monitor offsets and rotated monitor dimensions.
- Keep the existing focused-check/full-final-gate build policy and isolated
  worktrees. A four-window preset reuses persistent sessions; repeated providers
  get distinct role slots, worktrees and branches.

## Validation and limits

`make test` exercises provider policy precedence, fresh/resume/recovery,
model catalogs and malformed caches, picker selection, handover acknowledgement,
failed launches, changed/dirty worktrees, disabled providers, cancellation,
configuration observation, all provider counts, and the actual desktop launcher
against a simulated compositor. The desktop test checks four windows on one
desktop, reopening, scaled/rotated monitors and negative monitor origins.
ShellCheck, syntax, portability, and an isolated real-tmux transport test cover
the supporting scripts. CI runs the full suite.

No paid model turn, live provider switch or rearrangement of the user's active
windows is part of these tests. Live subscription entitlements, gateway aliases,
and compositor timing remain runtime-dependent. Automatic upgrades do not
rewrite acknowledged agent assignments or interrupt active work.
