#!/bin/bash

set -o pipefail

AW_VERSION_FILE="$(dirname "${BASH_SOURCE[0]}")/../VERSION"
if [[ -f $AW_VERSION_FILE ]]; then AW_VERSION=$(<"$AW_VERSION_FILE"); else AW_VERSION=unknown; fi
AW_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}/agent-workspaces
AW_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}/agent-workspaces
AW_CACHE_HOME=${XDG_CACHE_HOME:-$HOME/.cache}/agent-workspaces
AW_CONFIG_FILE=$AW_CONFIG_HOME/config.json
AW_PROVIDER_HOME=$AW_CONFIG_HOME/providers
AW_SESSION_HELPER=$(dirname "${BASH_SOURCE[0]}")/sessions.py

aw_die() { printf 'agent-workspaces: %s\n' "$*" >&2; exit 1; }
aw_now() { date --iso-8601=seconds; }
aw_require() { command -v "$1" >/dev/null 2>&1 || aw_die "missing dependency: $1"; }
aw_platform() { [[ $(uname -s) == Darwin ]] && printf 'macos\n' || printf 'linux\n'; }
aw_notify() {
  local title=$1 message=$2
  if [[ $(aw_platform) == macos ]]; then
    /usr/bin/osascript - "$title" "$message" <<'APPLESCRIPT' >/dev/null 2>&1 || true
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
APPLESCRIPT
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send -a 'Workspaces' "$title" "$message" 2>/dev/null || true
  fi
}
aw_valid_state() { [[ $1 =~ ^(deferred|dispatched|in_progress|blocked|completed|waived)$ ]]; }
aw_closed_task() { [[ $1 =~ ^(cancelled|archived|superseded)$ ]]; }
aw_valid_profile() { [[ $1 =~ ^(safe|trusted|yolo)$ ]]; }

aw_provider_available() {
  local provider=$1 manifest="$AW_PROVIDER_HOME/$1.json" executable enabled
  [[ -f $manifest ]] || return 1
  enabled=$(jq -r --arg provider "$provider" 'if .provider_overrides[$provider].enabled == false then false else true end' "$AW_CONFIG_FILE" 2>/dev/null)
  [[ $enabled == true ]] || return 1
  executable=$(jq -r '.executable // .id' "$manifest")
  command -v "$executable" >/dev/null 2>&1
}

aw_origin_base() {
  local repository=$1 base remote_head
  base=$(git -C "$repository" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
  if [[ -z $base ]]; then
    # Ask the remote for its default branch before guessing a conventional name.
    remote_head=$(timeout 10 git -C "$repository" ls-remote --symref origin HEAD 2>/dev/null | awk '$1=="ref:" && $3=="HEAD" {sub("^refs/heads/","",$2); print $2; exit}' || true)
    if [[ -n $remote_head ]] && git -C "$repository" show-ref --verify --quiet "refs/remotes/origin/$remote_head"; then
      base="origin/$remote_head"
    fi
  fi
  if [[ -z $base ]]; then
    for candidate in origin/main origin/master; do
      git -C "$repository" show-ref --verify --quiet "refs/remotes/$candidate" && { base=$candidate; break; }
    done
  fi
  [[ -n $base ]] || aw_die 'cannot determine the origin default branch; pass --base REF'
  printf '%s\n' "$base"
}

# Read-only integration preflight. Prints one JSON document describing the base
# the integration branch would start from, the checkout it would leave and
# return to, and the validated commit selection. Nothing is created or changed.
# Usage: aw_integration_plan_json TASK_DIR BASE_REF_OR_EMPTY [COMMIT...]
aw_integration_plan_json() {
  local task_dir=$1 explicit_base=$2; shift 2
  local state="$task_dir/state.json" repository blockers='[]' base_ref='' base_sha='' base_source=''
  local original_branch='' original_sha='' dirty=0 set_slug task_id branch branch_exists=false default_ref=''
  local commits=() commit full status_json reported role plan_id roles_for entries='[]' reported_by
  [[ -f $state ]] || aw_die "missing task: $task_dir"
  repository=$(jq -r '.repository' "$state")
  git -C "$repository" rev-parse --git-dir >/dev/null 2>&1 || aw_die "integration checkout is not a git repository: $repository"
  block() { blockers=$(jq -c --arg b "$1" '. + [$b]' <<<"$blockers"); }
  original_sha=$(git -C "$repository" rev-parse --verify -q HEAD 2>/dev/null || true)
  original_branch=$(git -C "$repository" branch --show-current 2>/dev/null || true)
  dirty=$(git -C "$repository" status --porcelain 2>/dev/null | wc -l)
  ((dirty == 0)) || block "integration checkout is dirty ($dirty path(s)); commit or stash outside this workflow first"
  [[ -n $original_sha ]] || block 'integration checkout has no commits'
  default_ref=$(aw_origin_base "$repository" 2>/dev/null || true)
  if [[ -n $explicit_base ]]; then
    base_ref=$explicit_base; base_source=explicit
  elif [[ -n $default_ref ]]; then
    base_ref=$default_ref; base_source=origin-default
  fi
  if [[ -z $base_ref ]]; then
    block 'cannot determine the origin default branch; pass --base REF'
  elif base_sha=$(git -C "$repository" rev-parse --verify -q "$base_ref^{commit}" 2>/dev/null); then :
  else
    base_sha=''; block "unknown base: $base_ref"
  fi
  for commit in "$@"; do
    if ! full=$(git -C "$repository" rev-parse --verify -q "$commit^{commit}" 2>/dev/null); then
      block "unknown commit: $commit"; continue
    fi
    reported_by='[]'
    while IFS=$'\t' read -r role status_json; do
      [[ -f $status_json ]] || continue
      while IFS= read -r reported; do
        [[ $(git -C "$repository" rev-parse --verify -q "$reported^{commit}" 2>/dev/null || true) == "$full" ]] \
          && { reported_by=$(jq -c --arg r "$role" '. + [$r]' <<<"$reported_by"); break; }
      done < <(jq -r '.commits // [] | .[]' "$status_json")
    done < <(jq -r '.roles | to_entries[] | [.key, (.value.status_json // .value.status_file // "")] | @tsv' "$state")
    [[ $reported_by != '[]' ]] || block "commit was not reported by an assigned agent: $commit"
    commits+=("$full")
    entries=$(jq -c --arg sha "$full" --argjson by "$reported_by" '. + [{sha:$sha,reported_by:$by}]' <<<"$entries")
  done
  ((${#commits[@]} > 0)) || block 'select at least one --commit reported by an assigned agent'
  set_slug=$(jq -r '.set' "$state"); task_id=$(basename "$task_dir")
  branch=$(sed -E 's/[^[:alnum:]_.\/-]+/-/g' <<<"integration/$set_slug/$task_id")
  git -C "$repository" show-ref --verify --quiet "refs/heads/$branch" && { branch_exists=true; block "integration branch already exists: $branch"; }
  plan_id=$(printf '%s\n' "$task_id" "$repository" "$base_sha" "${commits[@]}" | sha256sum | awk '{print substr($1,1,16)}')
  jq -n --arg task "$task_id" --arg repository "$repository" --arg original_branch "$original_branch" --arg original_sha "$original_sha" \
    --argjson dirty "$dirty" --arg base_ref "$base_ref" --arg base_sha "$base_sha" --arg base_source "$base_source" --arg default_ref "$default_ref" \
    --argjson commits "$entries" --arg branch "$branch" --argjson branch_exists "$branch_exists" --arg plan_id "$plan_id" --argjson blockers "$blockers" '
    {task_id:$task, repository:$repository,
     original:{branch:$original_branch, sha:$original_sha, detached:($original_branch == "")},
     clean:($dirty == 0), base:{ref:$base_ref, sha:$base_sha, source:$base_source}, origin_default:$default_ref,
     commits:$commits, integration_branch:$branch, plan_id:$plan_id, blockers:$blockers, ready:($blockers | length == 0)}'
}

aw_atomic_jq() {
  local file=$1; shift
  local directory temporary
  directory=$(dirname "$file")
  mkdir -p "$directory"
  temporary=$(mktemp "$directory/.${file##*/}.XXXXXX") || return 1
  if jq "$@" "$file" >"$temporary"; then
    mv "$temporary" "$file"
  else
    rm -f "$temporary"
    return 1
  fi
}

aw_workspace_root() {
  local workspace_id=$1 root state
  while IFS= read -r state; do
    [[ $(jq -r '.workspace_id // empty' "$state" 2>/dev/null) == "$workspace_id" ]] || continue
    root=${state%/.coordination/*}
    printf '%s\n' "$root"
    return 0
  done < <(find "$AW_DATA_HOME" -path '*/.coordination/*/state.json' -type f -print 2>/dev/null)
  while IFS= read -r root; do
    [[ -f $root/workspace.json ]] || continue
    [[ $(jq -r '.workspace_id' "$root/workspace.json") == "$workspace_id" ]] && { printf '%s\n' "$root"; return 0; }
  done < <(find "$AW_DATA_HOME" -mindepth 2 -maxdepth 2 -type d -print 2>/dev/null)
  return 1
}

aw_audit() {
  local workspace_root=$1 event=$2 details=${3:-'{}'} actor=${4:-system}
  local audit_file="$workspace_root/.coordination/audit.jsonl" lock_file="$workspace_root/.coordination/.audit.lock"
  mkdir -p "$(dirname "$audit_file")"
  exec {audit_lock}>"$lock_file"
  flock "$audit_lock"
  jq -cn --arg at "$(aw_now)" --arg event "$event" --arg actor "$actor" --argjson details "$details" \
    '{schema_version:1,at:$at,event:$event,actor:$actor,details:$details}' >>"$audit_file"
  flock -u "$audit_lock"
  exec {audit_lock}>&-
  # Observe after releasing the source lock; failures must not block task work.
  python3 "$(dirname "$AW_SESSION_HELPER")/telemetry.py" collect --fast --workspace "$workspace_root" >/dev/null || \
    printf 'agent-workspaces: telemetry collection incomplete; inspect telemetry status\n' >&2
}

aw_roles_allow_gate() {
  jq -e '
    all(.roles[]?; .state == "completed" or .state == "waived")
    and all(.roles[]? | select(.state == "waived");
         ((.waiver.reason // "") | gsub("^\\s+|\\s+$"; "")) != ""
         and (.waiver.actor // "") != ""
         and (.waiver.at // "") != "")
  ' "$1" >/dev/null
}

aw_task_refresh() {
  local task_dir=$1 state_file="$1/state.json" state status_file role role_state waiver any_blocked=false all_completed=true any_deferred=false any_waived=false
  [[ -f $state_file ]] || aw_die "missing task state: $state_file"
  exec {task_lock}>"$task_dir/.state.lock"
  flock "$task_lock"
  state=$(jq '.' "$state_file") || aw_die "invalid task state: $state_file"
  if aw_handover_frozen "$task_dir"; then
    flock -u "$task_lock"; exec {task_lock}>&-
    jq -r '.status' <<<"$state"
    return 0
  fi
  local current next stage now
  current=$(jq -r '.status' <<<"$state")
  if aw_closed_task "$current"; then
    flock -u "$task_lock"; exec {task_lock}>&-
    printf '%s\n' "$current"
    return 0
  fi
  while IFS=$'\t' read -r role status_file; do
    [[ -f $status_file ]] || { role_state=dispatched; all_completed=false; waiver=null; }
    if [[ -f $status_file ]]; then
      role_state=$(jq -r '.state // "dispatched"' "$status_file" 2>/dev/null)
      aw_valid_state "$role_state" || role_state=dispatched
      waiver=$(jq -c '.waiver // null' "$status_file" 2>/dev/null || printf 'null')
      if [[ $role_state == deferred ]]; then
        any_deferred=true
      fi
      [[ $role_state == waived ]] && any_waived=true
      [[ $role_state == blocked ]] && any_blocked=true
      [[ $role_state == completed || $role_state == deferred || $role_state == waived ]] || all_completed=false
    fi
    state=$(jq --arg role "$role" --arg value "$role_state" --argjson waiver "${waiver:-null}" '
      .roles[$role].state=$value
      | if $value == "waived" and $waiver != null then .roles[$role].waiver=$waiver else del(.roles[$role].waiver) end
    ' <<<"$state")
  done < <(jq -r '.roles | to_entries[] | [.key, (.value.status_json // .value.status_file)] | @tsv' <<<"$state")

  next=$current
  if [[ $any_waived == true ]]; then
    state=$(jq '.gates.reduced_assurance=true' <<<"$state")
  fi
  if [[ $current == active && $any_deferred == true && $all_completed == true ]]; then
    next=awaiting_review
  elif [[ $current == active && $any_blocked == true ]]; then
    next=blocked
  elif [[ $current == active && $all_completed == true ]]; then
    stage=$(jq -r '.stage' <<<"$state")
    case "$stage" in
      review) next=awaiting_triage ;;
      execute|verify) next=awaiting_integration ;;
      integrate) next=awaiting_pr ;;
    esac
  elif [[ $current == awaiting_review && $any_deferred == false && $any_blocked == false && $all_completed == true ]]; then
    stage=$(jq -r '.stage' <<<"$state")
    case "$stage" in
      review) next=awaiting_triage ;;
      execute|verify) next=awaiting_integration ;;
      integrate) next=awaiting_pr ;;
    esac
  elif [[ $current == blocked && $any_blocked == false ]]; then
    if [[ $all_completed == true ]]; then
      stage=$(jq -r '.stage' <<<"$state")
      case "$stage" in
        review) next=awaiting_triage ;;
        execute|verify) next=awaiting_integration ;;
        integrate) next=awaiting_pr ;;
      esac
    else
      next=active
    fi
  fi
  now=$(aw_now)
  if [[ $next != "$current" ]]; then
    state=$(jq --arg from "$current" --arg to "$next" --arg at "$now" \
      '.status=$to | .updated_at=$at | .transitions += [{from:$from,to:$to,at:$at,actor:"dispatcher:auto"}]' <<<"$state")
    aw_audit "${task_dir%/.coordination/*}" task.transition "$(jq -cn --arg id "$(basename "$task_dir")" --arg from "$current" --arg to "$next" '{task_id:$id,from:$from,to:$to}')"
  else
    state=$(jq --arg at "$now" '.updated_at=$at' <<<"$state")
  fi
  local temporary
  temporary=$(mktemp "$task_dir/.state.json.XXXXXX")
  printf '%s\n' "$state" >"$temporary" && mv "$temporary" "$state_file"
  flock -u "$task_lock"
  exec {task_lock}>&-
  printf '%s\n' "$next"
}

# Role slots stay stable across provider changes, retaining their worktrees.
aw_role_provider() {
  local provider=$1 role=$2 workspace_id=${3:-${AW_WORKSPACE_ID:-}} root selection=''
  if [[ -n $workspace_id ]]; then
    root=$(aw_workspace_root "$workspace_id" 2>/dev/null || true)
    if [[ -f $root/.coordination/agents.json ]]; then
      selection=$(jq -r --arg role "$role" '.active[$role].provider // empty' "$root/.coordination/agents.json")
    fi
  fi
  printf '%s\n' "${selection:-$provider}"
}

aw_handover_frozen() {
  local task root records
  task=$(realpath "$1") || return 1
  root=${task%/.coordination/*}
  records="$root/.coordination/agents.json"
  [[ -f $records ]] || return 1
  jq -e --arg task "$task" 'any(.requests[]; .task == $task and (.phase == "ready" or .phase == "launching" or .phase == "awaiting_ack"))' "$records" >/dev/null
}

aw_require_no_handover() {
  if aw_handover_frozen "$1"; then aw_die 'task ownership is paused for handover; the successor must acknowledge before work resumes'; fi
}

aw_provider_command() {
  local provider=$1 mode=$2 profile=$3 session_id=${4:-} model_override=${5:-} role=${6:-$1} workspace_id=${7:-${AW_WORKSPACE_ID:-}}
  local workspace_root='' selection='{}'
  if [[ -n $workspace_id ]]; then
    workspace_root=$(aw_workspace_root "$workspace_id" 2>/dev/null || true)
    if [[ -f $workspace_root/.coordination/agents.json ]]; then
      selection=$(jq -c --arg role "$role" '.active[$role] // {}' "$workspace_root/.coordination/agents.json")
    fi
  fi
  [[ -z ${AW_HANDOVER_TARGET:-} ]] || selection=$AW_HANDOVER_TARGET
  provider=$(jq -r --arg provider "$provider" '.provider // $provider' <<<"$selection")
  local manifest="$AW_PROVIDER_HOME/$provider.json"
  [[ -f $manifest ]] || aw_die "unknown provider: $provider"
  aw_valid_profile "$profile" || aw_die "invalid permission profile: $profile"
  local command_json
  # Codex --last can select a sibling worktree's conversation. Managed
  # workspaces must verify even cached IDs and never use that fallback.
  local resume_directory=''
  if [[ $provider == codex && $mode == resume && -n $workspace_id ]]; then
    [[ -n $workspace_root ]] || aw_die "cannot verify resume workspace: $workspace_id"
    resume_directory="$workspace_root/$role"
    [[ $role == orchestrator ]] && resume_directory="$workspace_root/.orchestrator"
    session_id=$(python3 "$AW_SESSION_HELPER" "$resume_directory" "$session_id") || return 1
    [[ -n $session_id ]] || mode=fresh
  fi
  if [[ $mode == resume && -n $session_id ]]; then
    command_json=$(jq -c --arg id "$session_id" --arg profile "$profile" '[.resume[] | if . == "{session_id}" then $id else . end] + .profiles[$profile]' "$manifest")
  elif [[ $mode == resume ]]; then
    command_json=$(jq -c --arg profile "$profile" '.continue + .profiles[$profile]' "$manifest")
  else
    command_json=$(jq -c --arg profile "$profile" '.fresh + .profiles[$profile]' "$manifest")
  fi
  if [[ -n $resume_directory ]]; then
    command_json=$(jq -c --arg cwd "$resume_directory" '. + ["--cd", $cwd]' <<<"$command_json")
  fi
  # Explicit override > acknowledged workspace assignment > orchestrator selection > provider policy > template.
  # Resolve here so fresh, resume, and recovery cannot drift apart.
  local policy model effort effort_flag=''
  policy=$(jq -c --arg provider "$provider" --arg role "$role" '
    (.model_policy[$provider] // {}) +
    (if $role == "orchestrator" and $provider == (.orchestrator_provider // "codex")
     then {model:(.orchestrator_model // .model_policy[$provider].model)}
     else {} end)' "$AW_CONFIG_FILE") || return 1
  policy=$(jq -c --argjson selection "$selection" '. + $selection' <<<"$policy") || return 1
  model=${model_override:-$(jq -r '.model // empty' <<<"$policy")}
  effort=$(jq -r '.effort // empty' <<<"$policy")
  local budget_helper="$(dirname "${BASH_SOURCE[0]}")/budget.py" budget_enabled=false
  budget_enabled=$(jq -r '.budget.enabled // false' "$AW_CONFIG_FILE")
  if [[ $budget_enabled == true && -n $workspace_id && -z $model_override ]]; then
    policy=$(python3 "$budget_helper" resolve "$provider" "$model" "$effort" "$workspace_id" "$role") || return 1
    model=$(jq -r '.model' <<<"$policy")
    effort=$(jq -r '.effort' <<<"$policy")
  fi
  case "$provider" in
    codex) effort_flag=config ;;
    claude) effort_flag=--effort ;;
    grok) effort_flag=--reasoning-effort ;;
  esac
  command_json=$(jq -c --arg model "$model" --arg effort "$effort" --arg flag "$effort_flag" '
    def reasoning: test("^model_reasoning_effort\\s*=");
    def clean:
      if length == 0 then []
      elif $model != "" and (.[0] == "--model" or .[0] == "-m") then .[2:] | clean
      elif $model != "" and (.[0] | startswith("--model=")) then .[1:] | clean
      elif $effort != "" and $flag != "" and $flag != "config" and .[0] == $flag then .[2:] | clean
      elif $effort != "" and $flag != "" and $flag != "config" and (.[0] | startswith($flag + "=")) then .[1:] | clean
      elif $effort != "" and $flag == "config" and (.[0] == "-c" or .[0] == "--config") and ((.[1] // "") | reasoning) then .[2:] | clean
      elif $effort != "" and $flag == "config" and (.[0] | test("^(--config=|-c)model_reasoning_effort\\s*=")) then .[1:] | clean
      else [.[0]] + (.[1:] | clean) end;
    clean
    + (if $model != "" then ["--model",$model] else [] end)
    + (if $effort == "" or $flag == "" then []
       elif $effort == "default" then []
       elif $flag == "config" then ["-c", "model_reasoning_effort=" + ($effort | tojson)]
       else [$flag,$effort] end)' <<<"$command_json") || return 1
  if [[ $budget_enabled == true ]]; then
    command_json=$(jq -c --arg helper "$budget_helper" --arg provider "$provider" '["python3",$helper,"launch",$provider,"--"] + .' <<<"$command_json") || return 1
  fi
  jq -r '.[] | @sh' <<<"$command_json" | paste -sd' ' -
}

aw_resume_command() {
  # Group compound launchers; a successful resume must not run a fresh session.
  # An explicit interrupt/termination is an exit request, not a resume failure.
  printf '(%s); aw_resume_exit=$?; if (( aw_resume_exit > 0 && aw_resume_exit < 128 )); then (%s); fi\n' "$1" "$2"
}

aw_orchestrator_prompt_idle() {
  local provider=$1 content
  content=$(cat)
  # Claude renders an empty composer with NBSP; normalize only that space.
  content=${content//$'\u00a0'/ }
  # Providers can keep an empty composer visible during an active turn.
  if tail -8 <<<"$content" | grep -qi 'esc to interrupt'; then return 1; fi
  if [[ $provider == grok ]]; then
    content=$(sed -E 's/^[[:space:]]*│[[:space:]]?/ /; s/[[:space:]]*│[[:space:]]*$//' <<<"$content")
  fi
  # An earlier empty composer must never override the user's current draft.
  case "$provider" in
    codex) content=$(awk '/^[[:space:]]*›/ {last=$0} END {print last}' <<<"$content") ;;
    claude|grok) content=$(awk '/^[[:space:]]*❯/ {last=$0} END {print last}' <<<"$content") ;;
  esac
  case "$provider" in
    codex) grep -Eq '^[[:space:]]*›([[:space:]]+Ask Codex to do anything)?[[:space:]]*$' <<<"$content" ;;
    claude|grok) grep -Eq '^[[:space:]]*❯[[:space:]]*$' <<<"$content" ;;
    *) return 1 ;;
  esac
}

# Wake identity is task status, every gate value, role state, role commits and
# changed files, integration and pull-request records, dispatch and reviewer
# eligibility, next actions, and pending handovers. Timestamps, quota
# telemetry, status evidence, live footers, and audit lines stay observational.
aw_actionable_payload() {
  (
    set -euo pipefail
    workspace_root=$1
    directory=$(mktemp -d)
    trap 'rm -rf "$directory"' EXIT
    tasks_file=$directory/tasks.json
    assignments_file=$directory/assignments.json
    if [[ -f $workspace_root/.coordination/agents.json ]]; then
      jq -c '{
        active: ((.active // {}) | to_entries | sort_by(.key) | map({slot:.key, provider:.value.provider, model:.value.model, effort:.value.effort})),
        pending: [(.requests // {})[] | select(.phase=="requested" or .phase=="ready" or .phase=="launching" or .phase=="awaiting_ack") | {id,slot,role,phase,when,provider:.target.provider,model:.target.model,effort:.target.effort}] | sort_by(.id)
      }' "$workspace_root/.coordination/agents.json" >"$assignments_file"
    else
      printf '%s\n' '{"active":[],"pending":[]}' >"$assignments_file"
    fi
    : >"$directory/tasks.lines"
    while IFS= read -r state; do
      task_dir=$(dirname "$state")
      next_ids=$(aw_next_actions_json "$task_dir" | jq -c '[.[].id] | sort')
      artifacts='[]'
      while IFS=$'\t' read -r slot status_path; do
        [[ -n $slot ]] || continue
        if [[ -n $status_path && -f $status_path ]]; then
          item=$(jq -c --arg slot "$slot" '{slot:$slot, commits: ((.commits // []) | map(select(type=="string")) | unique), files: ((.changed_files // []) | map(select(type=="string")) | unique)}' "$status_path") || return 1
        else
          item=$(jq -nc --arg slot "$slot" '{slot:$slot,commits:[],files:[]}')
        fi
        artifacts=$(jq -c --argjson item "$item" '. + [$item]' <<<"$artifacts") || return 1
      done < <(jq -r '.roles // {} | to_entries | sort_by(.key)[] | [.key, (.value.status_json // .value.status_file // "")] | @tsv' "$state")
      jq -c --argjson next "$next_ids" --argjson artifacts "$artifacts" '{
        id, status, stage,
        roles: ((.roles // {}) | to_entries | sort_by(.key) | map({slot:.key, state:(.value.state // "dispatched")})),
        artifacts: $artifacts,
        gates: ((.gates // {}) | to_entries | sort_by(.key) | from_entries),
        integration: (if (.integration | type) == "object" then (.integration | del(.updated_at, .checked_at, .at)) else {} end),
        pr: (if (.pr | type) == "object" then (.pr | del(.updated_at, .checked_at, .at)) else {} end),
        sent: ((.dispatch.sent // []) | sort),
        deferred: ((.review_activation.deferred // {}) | keys | sort),
        next: $next
      }' "$state" >>"$directory/tasks.lines"
    done < <(find "$workspace_root/.coordination" -mindepth 2 -maxdepth 2 -name state.json -type f -print 2>/dev/null | sort)
    if [[ -s $directory/tasks.lines ]]; then
      jq -s 'sort_by(.id)' "$directory/tasks.lines" >"$tasks_file"
    else
      printf '%s\n' '[]' >"$tasks_file"
    fi
    jq -nS --slurpfile tasks "$tasks_file" --slurpfile assignments "$assignments_file" \
      '{tasks:$tasks[0], assignments:$assignments[0]}'
  )
}

aw_actionable_signature() {
  local payload
  payload=$(aw_actionable_payload "$1") || return 1
  [[ -n $payload ]] || return 1
  printf '%s' "$payload" | sha256sum | awk '{print $1}'
}

# Record at most one undelivered wake. Returns 0 only when a new event is queued.
aw_queue_orchestrator_wake() {
  local workspace_root=$1 signature previous signature_file pending_file temporary
  signature=$(aw_actionable_signature "$workspace_root") || return 1
  [[ -n $signature ]] || return 1
  mkdir -p "$workspace_root/.orchestrator"
  signature_file="$workspace_root/.orchestrator/.state-signature"
  pending_file="$workspace_root/.orchestrator/.pending-wake"
  previous=''
  [[ -f $signature_file ]] && read -r previous <"$signature_file" || true
  temporary=$(mktemp "$workspace_root/.orchestrator/.state-signature.XXXXXX") || return 1
  printf '%s\n' "$signature" >"$temporary"
  mv "$temporary" "$signature_file"
  if [[ -n $previous && $signature != "$previous" ]]; then
    temporary=$(mktemp "$workspace_root/.orchestrator/.pending-wake.XXXXXX") || return 1
    printf '%s\n' "$signature" >"$temporary"
    mv "$temporary" "$pending_file"
    return 0
  fi
  return 1
}

aw_workspace_risks() {
  local workspace_id=$1 session repository role worktree branch dirty ahead task_count overdue_count=0 active_agents=0 now task_status deadline
  repository=$(tmux list-sessions -F $'#{@aw_workspace_id}\t#{@aw_repository}' 2>/dev/null | awk -F '\t' -v id="$workspace_id" '$1==id {print $2; exit}' || true)
  task_count=0
  now=$(aw_now)
  while IFS= read -r state; do
    [[ $(jq -r '.workspace_id' "$state") == "$workspace_id" ]] || continue
    task_status=$(jq -r '.status' "$state")
    if [[ ! $task_status =~ ^(completed|ready_for_pr|draft_pr|cancelled|archived|superseded)$ ]]; then
      ((task_count+=1))
      deadline=$(jq -r '.delivery.deadline_at // empty' "$state")
      [[ -n $deadline && $deadline < $now ]] && ((overdue_count+=1))
    fi
  done < <(find "$AW_DATA_HOME" -path '*/.coordination/*/state.json' -type f -print 2>/dev/null)
  printf 'Workspace: %s\nActive tasks: %s\nOver budget: %s\n' "$workspace_id" "$task_count" "$overdue_count"
  while IFS=$'\t' read -r session role; do
    [[ -n $session ]] || continue
    if [[ $role != integration && $role != orchestrator ]] && agent-workspaces-pane-active "$role" "$session" 2>/dev/null; then ((active_agents+=1)); fi
    worktree=$(tmux display-message -p -t "$session" '#{pane_current_path}' 2>/dev/null)
    [[ $role == integration && -n $repository ]] && worktree=$repository
    if git -C "$worktree" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      branch=$(git -C "$worktree" branch --show-current)
      dirty=$(git -C "$worktree" status --porcelain | wc -l)
      ahead=0
      if [[ -n $repository && $worktree != "$repository" ]]; then
        ahead=$(git -C "$worktree" rev-list --count "$(git -C "$repository" rev-parse HEAD)..HEAD" 2>/dev/null || printf 0)
      fi
      session_profile=$(tmux show-option -qv -t "$session" @aw_profile); session_profile=${session_profile:-unknown}
      printf '%-12s %-10s %-28s dirty=%s ahead=%s\n' "$role" "$session_profile" "$branch" "$dirty" "$ahead"
    fi
  done < <(tmux list-sessions -F $'#{session_name}\t#{@aw_role}\t#{@aw_workspace_id}' 2>/dev/null | awk -F '\t' -v id="$workspace_id" '$3==id {print $1 "\t" $2}')
  printf 'Running agents: %s\n' "$active_agents"
}

aw_next_actions_json() {
  local task_dir=$1 state_file="$1/state.json" status
  [[ -f $state_file ]] || aw_die "missing task: $task_dir"
  status=$(jq -r '.status' "$state_file")
  # A closed task has no next action, even when a handover request is still recorded.
  # Nonterminal tasks still stop until the successor acknowledges.
  case "$status" in
    completed|cancelled|archived|superseded) jq -cn '[]'; return 0 ;;
  esac
  if aw_handover_frozen "$task_dir"; then
    jq -cn '[{id:"complete-handover",label:"Finish the pending agent handover before continuing",mutating:false}]'
    return 0
  fi
  case "$status" in
    prepared) jq -cn '[{id:"prepare-measurement",label:"Freeze acceptance criteria and verifier, then release the prepared task",mutating:false}]' ;;
    active) jq -cn '[{id:"wait",label:"Agents are still working",mutating:false}]' ;;
    awaiting_configuration) jq -cn '[{id:"configuration-pending",label:"Waiting for task configuration acknowledgement; monitor resumes automatically",mutating:false}]' ;;
    dispatching) jq -cn '[{id:"inspect-dispatch",label:"Inspect interrupted prompt delivery before retrying",mutating:false}]' ;;
    awaiting_review) jq -cn '[{id:"activate-reviewers",label:"Release the immutable lead artifact to reviewers",mutating:true},{id:"waive-role",label:"Record a user-approved waiver for one deferred role; this is reduced assurance, not review evidence",mutating:true}]' ;;
    blocked) jq -cn '[{id:"resolve-blocker",label:"Review blockers and dispatch a focused follow-up",mutating:false}]' ;;
    awaiting_triage) jq -cn '[{id:"dispatch-execution",label:"Approve scope and dispatch an execution cycle",mutating:true},{id:"complete-review",label:"Close the review without implementation",mutating:true}]' ;;
    awaiting_integration)
      if jq -e 'any(.roles[]?; .state=="waived") or .gates.reduced_assurance==true' "$state_file" >/dev/null; then
        jq -cn '[{id:"approve-integration",label:"Approve integration with recorded reduced assurance; waived roles are not review evidence",mutating:true},{id:"dispatch-verification",label:"Request another independent verification cycle",mutating:true}]'
      else
        jq -cn '[{id:"approve-integration",label:"Validate collisions and approve an integration handoff",mutating:true},{id:"dispatch-verification",label:"Request another independent verification cycle",mutating:true}]'
      fi
      ;;
    integration_ready)
      if jq -e '(.integration.branch // "") != ""' "$state_file" >/dev/null; then
        jq -cn '[{id:"record-integration",label:"Record validation evidence for the integrated branch",mutating:true}]'
      else
        jq -cn '[{id:"plan-integration",label:"Run integrate --plan: read-only base and commit preflight for the integration checkout",mutating:false},{id:"integrate",label:"Apply the approved plan from its exact base: integrate --base-sha SHA --commit SHA ... --confirmed-by-user",mutating:true}]'
      fi
      ;;
    awaiting_pr) jq -cn '[{id:"prepare-pr",label:"Prepare PR title, body, and readiness checklist",mutating:true}]' ;;
    awaiting_pr_review) jq -cn '[{id:"approve-pr",label:"Approve the validated integration branch for publication",mutating:true}]' ;;
    ready_for_pr) jq -cn '[{id:"publish",label:"Push and open a draft pull request",mutating:true,external:true}]' ;;
    draft_pr) jq -cn '[{id:"review-pr",label:"Review the draft pull request and CI",mutating:false}]' ;;
    *) jq -cn --arg status "$status" '[{id:"inspect",label:("Inspect unsupported workflow state: " + $status),mutating:false}]' ;;
  esac
}

aw_collision_json() {
  local task_dir=$1 state_file="$1/state.json" repository base role worktree status_json
  [[ -f $state_file ]] || aw_die "missing task: $task_dir"
  repository=$(jq -r '.repository' "$state_file")
  base=$(git -C "$repository" rev-parse HEAD)
  local entries='[]'
  while IFS=$'\t' read -r role worktree status_json; do
    local files commits commit commit_files
    files='[]'; commits='[]'
    if [[ -f $status_json ]]; then
      files=$(jq -c '.changed_files // []' "$status_json")
      commits=$(jq -c '.commits // []' "$status_json")
      while IFS= read -r commit; do
        [[ -n $commit ]] || continue
        git -C "$worktree" cat-file -e "$commit^{commit}" 2>/dev/null || continue
        commit_files=$(git -C "$worktree" show --format= --name-only "$commit" | jq -Rsc 'split("\n")|map(select(length>0))')
        files=$(jq -c --argjson extra "$commit_files" '. + $extra | unique' <<<"$files")
      done < <(jq -r '.[]' <<<"$commits")
    fi
    entries=$(jq --arg role "$role" --arg worktree "$worktree" --argjson files "$files" --argjson commits "$commits" \
      '. + [{role:$role,worktree:$worktree,files:$files,commits:$commits}]' <<<"$entries")
  done < <(jq -r '.roles | to_entries[] | [.key,.value.worktree,(.value.status_json // .value.status_file // "")] | @tsv' "$state_file")
  local merges='[]' count i j left_role right_role left_tree right_tree left_head right_head clean
  count=$(jq 'length' <<<"$entries")
  for ((i=0; i<count; i++)); do
    for ((j=i+1; j<count; j++)); do
      left_role=$(jq -r ".[$i].role" <<<"$entries"); right_role=$(jq -r ".[$j].role" <<<"$entries")
      left_tree=$(jq -r ".[$i].worktree" <<<"$entries"); right_tree=$(jq -r ".[$j].worktree" <<<"$entries")
      left_head=$(jq -r ".[$i].commits[-1] // empty" <<<"$entries")
      right_head=$(jq -r ".[$j].commits[-1] // empty" <<<"$entries")
      if [[ -z $left_head || -z $right_head ]]; then
        clean=true
      elif git -C "$repository" merge-tree --write-tree --quiet "$left_head" "$right_head" >/dev/null 2>&1; then
        clean=true
      else
        clean=false
      fi
      merges=$(jq --arg left "$left_role" --arg right "$right_role" --argjson clean "$clean" '. + [{left:$left,right:$right,clean:$clean}]' <<<"$merges")
    done
  done
  jq -n --arg base "$base" --argjson roles "$entries" --argjson merges "$merges" '
    ([
      range(0; $roles|length) as $i | range($i+1; $roles|length) as $j |
      (($roles[$i].files - ($roles[$i].files - $roles[$j].files))) as $shared |
      select($shared|length>0) | {left:$roles[$i].role,right:$roles[$j].role,files:$shared,
        identical:(($roles[$i].commits[-1] // "") != "" and $roles[$i].commits[-1] == $roles[$j].commits[-1]),
        critical:[$shared[]|select(test("(^|/)(Cargo.lock|package-lock.json|pnpm-lock.yaml|yarn.lock|.*migration.*)$";"i"))]}
    ]) as $overlaps |
    {base:$base,roles:$roles,merges:$merges,overlaps:$overlaps,
      lockfile_policy:{manual_merge:false,instruction:"Resolve Cargo manifests first; take a known-good Cargo.lock side; regenerate affected packages with cargo update -p <crate> --precise <version>; run locked validation and review the diff.",files:([$overlaps[].files[] | select(endswith("Cargo.lock"))] | unique)},
      safe:(([$overlaps[] | select(.identical|not)] | length)==0 and ([$merges[]|select(.clean==false)]|length)==0)}'
}
