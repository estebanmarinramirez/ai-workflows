# Zed-native co-development (experimental)

This frontend uses **native Zed subagents**, not Claude Code, Codex CLI, tmux,
Omarchy, or an external agent server. Python 3 and Git are required. Run directly
from this checkout; do not run the Omarchy/macOS installers for this frontend.
Existing workspace dispatch, provider routing and Dream capture are unchanged.

The main Zed thread coordinates. `bin/agent-workspaces-zed` records attempts and
verifies candidates, but cannot invoke Zed's native agent tools. There is no
background scheduler, unattended continuation, automatic merge or publication.
This is an independent Dream-inspired workflow, not the upstream runtime.

## Coordinator procedure

1. Ask the user to approve one bounded objective, acceptance criteria and limits.
   Select a clean, committed source baseline; never stash or commit user edits.
2. List the models actually available to Zed. Choose exact model IDs explicitly;
   model aliases and availability are not hardcoded here. A Zed Claude model is
   not a Claude Code CLI session. Route credentials through Zed, never this tool.
3. Prepare an external trusted Python verifier. It takes a JSON request file with
   `worktree`, `baseline_commit`, `output_commit`, `attempt_id` and `artifacts`.
   Exit 0 accepts, 1 rejects quality, other codes/timeouts are infrastructure
   failures. The verifier must check real task behavior, not worker prose. Its
   dependencies and any candidate code it executes are trusted local execution.
4. Initialize private task storage **outside** the source checkout. `init` copies
   the baseline and freezes the brief/verifier. It does not call a model or run
   the verifier. Validate the verifier independently before starting workers.
5. `prepare` creates a separate detached clone from that baseline and returns
   `model` and `message`. Use that exact message in a fresh native `spawn_agent`
   call. Use an appropriate short UI label. Do not append unrecorded instructions
   or silently substitute another model. Ensure the checkout is accessible to
   Zed's tools before dispatch; this recorder cannot grant editor permissions.
6. Save the returned final report verbatim to a local file, then `finish` with the
   actual returned session ID. The recorder snapshots the candidate internally,
   runs the frozen verifier in another clone and retains both evidence hashes
   and the immutable output commit. These internal commits do not change the
   user's source branch. Workers must not commit or change their checkout HEAD.
7. Inspect `status` and the verifier output. Human integration approval remains
   separate. Review a selected candidate read-only, then run combined tests if
   integrating it. Additional reviewer/orchestrator usage is not measured here.

Each attempt is an independent baseline candidate, not a resumed branch of a
Dream discovery tree. This first version does not implement refinement lineage,
read-only reviewer receipts, adaptive model selection or automatic replay export.

Example command shapes (replace paths and MODEL_ID with approved actual values):

```sh
python3 bin/agent-workspaces-zed init /path/to/clean/repo /private/task \
  --brief /private/brief.txt --verifier /private/verifier.py \
  --max-attempts 3 --max-active 1 --timeout 120
python3 bin/agent-workspaces-zed prepare /private/task --model MODEL_ID
# The main Zed thread now calls spawn_agent with the returned model and message.
python3 bin/agent-workspaces-zed finish /private/task 000001 \
  --session-id RETURNED_SESSION_ID --report /private/worker-report.txt
python3 bin/agent-workspaces-zed status /private/task
```

Start with one implementer and an independent review. If trying DeepSeek, Grok,
Claude and Codex, assign explicit bounded responsibilities rather than giving all
four the whole repository. Alternative implementations require separate prepared
candidates. Different models are not proof of independent judgments.

## Failure handling and authority

Preparation reserves an attempt before dispatch. Interrupted preparation or
verification remains active and consumes its slot. An uncertain tool delivery is
not permission to launch again. Inspect the Zed session first. After stopping or
resolving it explicitly, record abandonment:

```sh
python3 bin/agent-workspaces-zed abandon /private/task 000001 \
  --reason 'Operator stopped the ambiguous session; evidence retained'
```

Abandonment does not stop a Zed agent, recover spent quota, or erase the attempt.
The lifetime attempt limit still counts it. The active limit governs recorder
admission, not agents launched outside this workflow. The timeout only bounds
verification; the available Zed subagent tool has no per-call timeout parameter.
Never infer a paid-call deadline from this option.

The checkout is not a security sandbox. All workers run under the same user;
file permissions/hashes detect accidents, not an adversary able to rewrite all
records. The source must be clean, submodules are refused, and ignored files and
secrets are not copied. Clones can be expensive for large repositories. Preserve
artifacts until review; cleanup is explicit, never automatic.

## Context and Dream replay

Every result remains `replay_eligible: false`. The exact outgoing brief and returned
report are retained, but provider/system instructions, tools, compaction and later
conversation are not. Native model IDs are requests, not attestations of served
model revisions. Usage, token totals and cost remain null, not zero.

This frontend deliberately cannot turn native sessions into measured Dream
worlds. Existing Dream capture and replay contracts are not relaxed. Frozen
verifiers distinguish acceptance from completion, but do not establish that a
changed model or schedule would reproduce the outcome.

## API costs and scaling

API access is metered programmable capacity, not equivalent to a monthly coding
subscription. Subscriptions impose usage limits, throttling and product-specific
terms; heavy use can exhaust them. API calls can be cheaper for occasional use
and much more expensive for long autonomous runs. No fixed 10x ratio applies.
Check the actual provider/model pricing and account terms, including any routing
provider such as OpenRouter. Code and prompts go to the configured providers.

For budgeting, include uncached input, cache reads/writes where priced separately,
output/reasoning usage where billed, tool costs and all worker/reviewer/coordinator
calls. Multi-agent context duplication is real. Cache discounts depend on provider
support and qualifying shared prefixes; do not assume cross-model cache sharing.

Use narrow tasks, small briefs, bounded attempts and selective escalation. Spend
more on expensive independent reviews only where risk justifies it. Start at one
active candidate; concurrency reduces possible latency, not token cost. Set
provider-side spending controls where available. This recorder cannot enforce a
dollar ceiling inside native Zed calls and makes no API savings claims. Never
optimize spend from unknown usage or confuse API-equivalent subscription prices
with invoices.

## Validation

```sh
python3 tests/zed
```

Tests use temporary Git repositories and trusted toy verifiers. No model calls,
provider credentials, live sessions or workspace installation are involved.
