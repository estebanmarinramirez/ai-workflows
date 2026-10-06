# Gesture controller hardening review

Date: 2026-09-13. Camera left off after testing.

## Observed demo failure

The live journal recorded selection of an ordinary `org.omarchy.agent` terminal,
followed by rejection of dictation because that window had no managed tmux
recipient. The new demo grid existed on desktop 10, while the controller was
following a different active desktop. Merely opening the grid did not prove that
the user had selected one of its recipients.

The fresh Codex, Claude and orchestrator panes were waiting at first-run trust
prompts. The original composer heuristic misclassified Codex's `› 1. Yes,
continue` menu as input-ready. Grok's demo composer was ready. The read-only
doctor now distinguishes these cases without opening camera/microphone or
sending terminal input. Trust prompts remain for the user to complete normally.

## Findings and changes

| Finding | Change | Evidence |
|---|---|---|
| Trust menus shared the composer glyph | Inspect the full visible screen for first-run prompts and reject numbered choices/confirmation screens | Regression fixtures based on the observed Codex/Claude screens; live doctor |
| Live frame routing was absent from state tests | `Controller.observe` now owns pose-to-action routing for both runtime and tests | Full recognized-pose sequence, neutral rearming and single-send test |
| A held pose could count as neutral after a state transition | Unsupported held poses remain non-neutral | Palm-to-fist and pre-held thumbs-up regression |
| Cached candidate could outlive its pointing position | Resolve the current index position before routing each pointing observation | Shared inactive-gap checks and current-frame routing |
| Pane identity survived provider restart | Pin the uniquely identified provider PID as well as pane PID and provider metadata | Same-pane provider restart and ambiguous process-tree tests; live doctor |
| Cleanup/timeout could lead to repeated paste or Enter | Buffer cleanup is best-effort; ambiguous input delivery enters manual review and disables retries | Paste/send error-injection tests and real isolated tmux transport |
| Repeated daemon errors could skip the recording timeout | Check the deadline before polling external status | Daemon-failure timeout cleanup test |
| Notifications and device failure could interfere with shutdown | Bound notification calls; release handles on failed open; independent systemd runtime cap | Five lifecycle tests and a short systemd watchdog experiment |
| Readiness was hidden until an attempted recording | Show selected role/readiness; add `doctor --all` | Installed read-only preflight reports first-run prompts accurately |

## Architecture

`gesture_camera.py` owns device/model lifecycle, recognition observations, preview,
status, and the camera deadline. It delegates decisions to pure mapping/threshold
functions and `Controller.observe`; it no longer independently implements the
action state machine.

`Controller` owns selection, capture/transcription/draft/review state, hold
rearming, transcript lifetime, and at-most-one input attempt after an ambiguous
error. `Desktop` owns Hyprland, tmux and Voxtype transport. It validates focus,
window/pane identity, provider process identity and composer readiness before
input. `gesture_doctor.py` reuses that same validation with focus checking
disabled, so it can inspect other desktops without focusing them.

## Validation

- 25 mapping, readiness, routing, transport-failure and state-machine tests.
- Five camera lifecycle tests with injected device/model/notification failures;
  these tests never open physical devices.
- Full repository `make test` passed; the five lifecycle tests were subsequently
  added to that target and run separately.
- `make gesture-transport-check` passed against a disposable private tmux server:
  the receiver got one bracketed paste and exactly one carriage return. Only
  recipient validation is bypassed for this local byte receiver.
- A disposable systemd service running `sleep` was terminated by a one-second
  runtime cap, verifying the independent watchdog mechanism without a camera.
- Read-only checks against the running managed panes accepted the established
  agent composers and rejected the three fresh demo trust prompts.

## Remaining limits

There is no completed physical gesture → spoken transcription → provider reply
trial. Unit tests and transport tests do not prove microphone quality, speech
recognition accuracy, or real-user gesture reliability.

Terminal screen inspection remains a conservative heuristic, not an authoritative
provider composer API. A dialog can appear between validation and delivery;
provider-specific input/readiness APIs would be needed to remove that race.
False rejection of a screen containing trust-related text is possible.

Voxtype is a shared daemon without per-recording ownership tokens in this
integration. Concurrent keyboard/manual dictation can race gesture capture;
the controller checks idle before start but cannot provide exclusive ownership.
Ambiguous paste/send failures require manual review and controller restart.

Hyprland/tmux inspection remains synchronous and can reduce camera frame rate
under load. A future snapshot worker should separate preview cadence from IPC,
while retaining fresh recipient validation at action time. Recognition confidence,
hold timing and pointing geometry still require physical usability testing.

The camera follows the focused monitor's current desktop. It does not navigate
between workspace grids. Standalone app windows support focus only; dictation
requires a managed agent pane. The 30-minute cutoff is fixed, not chat-aware.
