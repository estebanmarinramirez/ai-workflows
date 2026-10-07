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
agent-workspaces telemetry audit
agent-workspaces dream replay DISCOVERY_MANIFEST
agent-workspaces benchmark demo DIRECTORY
agent-workspaces benchmark init MANIFEST EXPERIMENT
agent-workspaces benchmark run EXPERIMENT [--all] [--split development|heldout]
agent-workspaces benchmark report EXPERIMENT [--split development|heldout]
agent-workspaces benchmark shadow-report EXPERIMENT [--split development|heldout]
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
agent-workspaces models PROVIDER [--json | --efforts MODEL]
agent-workspaces routing WORKSPACE_ID [show|reconcile] [--json]
agent-workspaces routing WORKSPACE_ID effort-mode --mode auto|fixed
agent-workspaces routing WORKSPACE_ID difficulty --task TASK_DIR --difficulty simple|standard|complex
agent-workspaces layout-set adaptive-grid|four-windows
agent-workspaces live-config WORKSPACE_ID [--json]
agent-workspaces-agents [WORKSPACE_ID]
agent-workspaces agents WORKSPACE_ID [--json] [show|select|checkpoint|apply|accept|cancel|reconcile]
agent-workspaces end WORKSPACE_ID
agent-workspaces archive
agent-workspaces publish TASK_DIR [--confirmed-by-user]
agent-workspaces migrate
```

The Omarchy installer creates a timestamped rollback bundle before replacing managed files.

The desktop grid has one pane per available configured provider plus one conversational orchestrator, capped at four panes total. Three providers produce the full 2×2 layout; two providers produce three panes; one provider still produces two panes. Opening an older workspace reconciles its saved roster so newly available providers are no longer omitted.

### Four windows on one desktop

Choose **Open four-window workspace** in Workspaces Manager, or set the default
for new workspaces with `agent-workspaces layout-set four-windows`. For a specific
set, launch `AW_LAYOUT=four-windows OMARCHY_AGENT_REPOSITORY=/path/to/repo
omarchy-agent-grid` (on one shell line). The preset is saved in `workspace.json`.

This keeps one orchestrator and three independent workers in four terminal
windows on the same desktop. With fewer than three enabled providers, the preset
fills the remaining slots with repeated providers (for example `codex-2` and
`codex-3`), each with its own worktree, branch and persistent tmux session. Lead,
review and verification remain separate roles; repeated providers offer less
cross-provider independence. Existing role slots are preserved; if a changed
roster would exceed three workers, create a new collaboration set. Reopening
reflows existing windows without interrupting a checkpointed handover.

`agent-workspaces layout-set adaptive-grid` restores the adaptive default for new
sets. On macOS, worker slots remain available through tmux; automatic placement
of four desktop windows is an Omarchy feature.

The monitor itself is deterministic and consumes no model tokens. When material state changes, the optional event reviewer writes a short `briefing.md` using the configured lightweight model. A deduplicated pending event then wakes the persistent conversational Orchestrator as soon as its prompt is empty; busy turns and user-typed drafts are never overwritten. Undelivered events retry every monitor interval.

Visible provider prompts are not treated as completion signals because some CLIs render their composer while tools are still running. Workflow state changes only from explicit agent reports, confirmed process failure, or an operator-initiated recovery.

Worker sessions use focused debug-profile checks during implementation. Full repository validation, including release/LTO builds, runs once on the final reviewed commit unless the task is specifically release-only. Worktrees retain separate build directories; if `sccache` is installed, new worker sessions enable it automatically to reuse safe compiler artifacts across worktrees.

Provider manifests declare their CLI update manager. Use **Agent Workspaces → Provider Updates** to inspect and selectively update provider CLIs. Updates retain the prior provider installation so running sessions and their lazily spawned command hosts remain usable; restarted sessions pick up the new binary.

## Configuration benchmarks

Use `agent-workspaces benchmark` to compare fixed agent configurations in private
repository clones. A frozen task matrix, independent verifier, outcome ledger,
matched comparisons and explicit unknown costs keep experiments reviewable.
The included deterministic demo makes no model calls; real provider adapters
are supplied separately. Results never change live routing automatically.
See [the benchmark guide](docs/benchmarking.md) for the runnable demo and adapter contract.
Optional [Bayesian shadow evaluation](docs/bayesian-shadow.md) records pre-outcome
forecasts, separates capability from operational failure, and scores calibration.
It excludes synthetic evidence and never changes live routing.

## Models and usage

The conversational Orchestrator uses Claude `fable` with Remote Control enabled. The independent Claude worker uses `opus` at high effort, while Codex uses `gpt-6.1-sol` and Grok uses `grok-4.6` at high reasoning effort. The event reviewer uses `gpt-6-luna` at low effort because it summarizes deterministic state changes rather than writing production code. Provider commands and the documented policy live in `config/providers/` and `config/config.json`. Model selection uses an explicit command override first, then an acknowledged workspace assignment, then the orchestrator model for its configured provider, then `model_policy[provider].model`, and finally the provider template. Reasoning effort comes from `model_policy[provider].effort` when set, otherwise the template. Fresh launches, resumes, and recovery use the same resolver. Policy changes apply on the next launch or recovery; they do not change an already running TUI session.

Agent Workspaces extends Omarchy's native **Agents** bar panel rather than installing a separate widget. Its user-local updater preserves Omarchy's Claude and Codex collectors and adds:

- Grok's authoritative shared weekly subscription percentage, reset time, local token totals, and model attribution. API-equivalent cost fields embedded in sessions are deliberately excluded because they are not subscription charges or model-specific limits.
- Workspace delivery health: only the newest workflow per workspace is considered current; historical records feed seven-day completions without inflating blocked or active counts. The panel also shows current delivery-budget pressure.

Run `agent-workspaces-usage-update` to refresh the added records immediately. A user-level timer keeps them current every two minutes; Omarchy continues refreshing its built-in provider records normally. The figures are local operational telemetry, not a provider invoice: account limits come from each provider's authenticated CLI where available, while local session totals measure work recorded on this machine.

Additional panel collectors use an `id=executable` registry and publish through the shared runtime in `lib/usage.sh`. The runtime validates Omarchy's record contract, writes atomically, logs per-collector outcomes under the Omarchy agents state directory, and retains the last good record when a collector fails. Run `agent-workspaces-usage-check` for collector, record, freshness, and recent-event diagnostics. For development or downstream packaging, override the registry with colon-separated `AW_USAGE_COLLECTORS` entries.

Active workflows use quota-aware delivery rather than a wall-clock deadline. The Workspaces record averages the most constrained live usage limit from each participating provider into a Pooled model quota, so the indicator tracks actual provider consumption and does not become permanently full merely because a workflow has been running for 90 minutes.

The agent chooser offers a provider-specific model catalog and reasoning levels,
including Codex Max/Ultra and Claude xhigh/Max where supported. `default` omits
explicit effort flags; use it for models such as Haiku. Custom IDs are supported
for gateways. Codex and Grok local caches supplement the offline catalog without
network calls; listed models still require account access. View provenance with
`agent-workspaces models codex --json`. Existing installed policy is retained by
the installer; new defaults apply to new installations, and saved workspace
assignments override global policy.

See the [dated upstream compatibility and optimization review](docs/provider-review-2026-09-30.md)
for sources, version differences, implemented optimizations, and validation limits.

### Automatic routing and user-selected effort

**Change Agents → Effort mode and quota routing** offers **Keep current model and
effort settings** (the default) and **Auto · balance model and effort**. Fixed
keeps your existing per-provider settings; Auto chooses from configurable task
profiles and lowers effort under quota pressure. This is a workspace-specific
choice. Choosing Auto releases previous manual choices to routing control;
subsequent explicit agent selections pin their slots again.

The monitor requests safe handovers based on fresh account capacity, difficulty,
shared slot load, and a review reserve. It never interrupts an agent or treats
missing quota data as available capacity. Stable slot names survive provider
switches, and repeated providers share one quota pool. Snapshot decisions include
reasons; application waits for clean checkpoints, process exit and acknowledgement.
Use `dispatch ... --difficulty simple|standard|complex` to classify the work.

See [quota routing](docs/quota-routing.md) for controls, reserve policy, automatic
versus advisory mode, audit records, tests, and runtime limits.

### Live configuration evidence

`live-config` compares the shared launch resolver with recognized Codex and Grok
footers from the current tmux screen. Each result exposes desired settings,
observed settings, the evidence source, and `match`, `drift`, or `unknown`.
Snapshots include this comparison so the monitor can surface changes to the
orchestrator. Claude and unrecognized or unavailable screens report `unknown`;
launch arguments and saved transcripts never count as live confirmation.
Screen evidence is best effort, not verification through a provider API.
Detection never sends input, changes settings, or restarts sessions.

Following [Herdr's status authority design](https://herdr.dev/docs/agents/#status-authority),
observation has one explicit source, separate from desired policy and display
labels. Drift should be addressed at a safe task boundary. An unknown observation
must not be treated as agreement or as permission to interrupt a running task.

### Change agents during a workflow

Open **Change agents** from the workspace manager or workflow dispatcher, or run
`agent-workspaces-agents WORKSPACE_ID`. Choose Orchestrator or Lead worker,
provider, model, reasoning effort and timing. The choice is workspace-specific;
other workspaces retain their own assignments. A lead replacement runs in the
same role slot/worktree, preserving task history and commits. The acknowledged
lead slot becomes the default for future workflows; an explicit dispatch lead
still takes precedence.

Selection is always available. Activation follows a checkpointed handover:

1. Select a replacement. Use `--when now` to request a prompt checkpoint,
   `checkpoint` for the next safe unit of work, or `next-task` to wait for existing
   tasks to finish. `now` does not interrupt an in-flight process.
2. Save a handover note and checkpoint. Lead worktrees must be clean and committed.
3. Exit the outgoing CLI. The monitor or **Launch successor** action checks that
   the pane contains only a shell, then launches the replacement with the note.
4. The successor acknowledges before taking ownership. Until then the task stays
   paused, with current and pending assignments visible in the snapshot.

Example:

```bash
agent-workspaces agents WORKSPACE_ID select --role lead --task TASK_DIR \
  --provider codex --model MODEL --effort high --when checkpoint
agent-workspaces agents WORKSPACE_ID checkpoint CHANGE_ID --note HANDOVER.md
# After the outgoing agent exits (or let the monitor do this):
agent-workspaces agents WORKSPACE_ID apply CHANGE_ID
# Executed by the launched successor after reading the note:
agent-workspaces agents WORKSPACE_ID accept CHANGE_ID
```

Omit `--task` and use `--when next-task` to select the default lead for the next
workflow. Changes are retained in `.coordination/agents.json` with request IDs,
checkpoint evidence and acknowledgements. Recovery and reopening cannot bypass
a paused handover. `cancel CHANGE_ID` keeps the outgoing owner; after launch it requires the
unacknowledged successor to exit first;
a failed launch can be retried with `apply` after its process exits. Review roles
remain separate, but selecting the same provider for multiple roles reduces
cross-provider independence. Permission profiles and task scope do not change.

### Camera hand controls (Omarchy / Linux)

The workspace manager's **Hand controls** action starts or stops local webcam
recognition. CLI: `agent-workspaces-gestures setup`, then `start`, `stop`, `status`,
or `preview`. Preview recognizes gestures without performing desktop actions.
The camera runs only while the controller is started; it is not enabled at login.
Frames are processed locally with MediaPipe and never saved or uploaded.

- **Point up and hold** with your index finger in the camera area corresponding
  to the desired on-screen window (about 0.8 seconds) to select it. Camera
  coordinates are mirrored; use the blue pointing box shown in the preview. Visible windows
  on the focused monitor's current workspace can be selected. Ordinary app
  windows support focus; dictation and sending require a managed agent pane.
- **Open palm and hold** to start Voxtype dictation for that selected pane.
- **Closed fist and hold** to stop recording and paste the transcription as a
  draft. Wait for the draft notification.
- **Thumbs-up and hold** for about 1.3 seconds to press Enter once.

Lower your hand briefly between gestures. Window selection stays locked during
recording/transcription and while a draft awaits sending; pointing at the same
window can restore focus. Enter is enabled only after this controller has pasted
its own dictation, and only in the same live, focused agent pane with a recognized
composer. Known trust/confirmation screens and stopped agents are rejected. Screen checks are
best effort and gesture accuracy depends on lighting and camera visibility.

Requires a camera and a running Voxtype daemon. Recognition dependencies live in
an isolated Python environment. Voxtype's per-recording file mode disables both
forms of auto-submit; temporary transcription files are deleted after pasting or
shutdown. The existing keyboard dictation bindings/configuration are unchanged.
Recognition follows [MediaPipe's gesture API](https://developers.google.com/edge/mediapipe/solutions/vision/gesture_recognizer/python).

For troubleshooting, `status` includes frame count, detected gesture, confidence,
and selectable window count. `preview` displays hand landmarks and recognition
confidence with actions disabled (stop live controls first). To keep that display
while using live controls, run `agent-workspaces-gestures start --preview`.
Recognition tolerates brief dropouts; sustained holds and a separate send gesture
are still required.

The default `laptop` profile is for sitting close to the camera. Its pointing box
covers the middle 60% horizontally and 54% vertically, centered in the camera
view. Detection and
tracking thresholds use MediaPipe's 0.5 defaults; selection/start/stop accept 0.6
gesture confidence. Sending still requires 0.8 confidence and a 1.3-second hold.
Use `start --profile desktop` for the larger pointing area and stricter recognition.
Keep the whole hand in frame; the preview warns when tracked landmarks approach
an edge. A pointing area cannot recover fingers outside the camera's field of view.

The blue central dividers are inactive bands, each 10% of the control box's
corresponding dimension. Pointing outside the box or within a divider cannot
select a window. The hovered quadrant is outlined in green and labeled; the
indicator follows the index fingertip, or the thumb tip during a thumbs-up.
The thumb indicator is visual feedback; thumbs-up still means send.

Camera sessions stop automatically after 30 minutes from startup, with a
countdown in the preview and remaining seconds in `status`. This is a fixed
cutoff: gestures and chat replies do not automatically extend it. Restart to
begin another session, or use `start --seconds N` for an earlier cutoff. The live
systemd service also enforces a 30-minute maximum and allows five seconds for
cleanup before terminating a stuck process.

Run `agent-workspaces-gestures doctor --all` without opening the camera to check
agent readiness across desktops. Fresh provider trust prompts require keyboard
setup before dictation; an ordinary terminal supports window focus only.
The preview identifies the selected role and why dictation is unavailable.
If a paste or send has an ambiguous transport failure, controls enter manual
review rather than retrying; inspect the pane and restart the controller to reset.
See [the hardening review](docs/gesture-review.md) for validation and remaining limits.

## Everyday evidence collection

Normal workspace activity is recorded locally in an append-only telemetry ledger.
Run `agent-workspaces telemetry status` to inspect coverage and collection errors,
or `agent-workspaces telemetry export` for JSONL evidence. Existing and inactive
workspaces are included; no new session is required. See [collection and receipt
details](docs/telemetry.md) for scope, privacy, controls and statistical limits.
This observational data does not automatically train the Bayesian shadow policy.

Measured [provider attempts](docs/provider-adapters.md) produce per-task usage
and independent verifier receipts with `agent-workspaces attempts run`. They use
fresh non-interactive sessions and refuse an already occupied managed role.

## Offline discovery replay

The [Dream-RSI adaptation](docs/dream-replay.md) evaluates exploration policies on
recorded discovery trees without provider calls. It reports unsupported actions,
keeps held-out instances separate, and never changes live routing automatically.
Use `agent-workspaces telemetry audit` to check what evidence exists first.
