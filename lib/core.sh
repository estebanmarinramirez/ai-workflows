#!/bin/bash

set -o pipefail

AW_VERSION=1.0.0
AW_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}/agent-workspaces
AW_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}/agent-workspaces
AW_CACHE_HOME=${XDG_CACHE_HOME:-$HOME/.cache}/agent-workspaces
AW_CONFIG_FILE=$AW_CONFIG_HOME/config.json
AW_PROVIDER_HOME=$AW_CONFIG_HOME/providers

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
aw_valid_state() { [[ $1 =~ ^(deferred|dispatched|in_progress|blocked|completed)$ ]]; }
aw_valid_profile() { [[ $1 =~ ^(safe|trusted|yolo)$ ]]; }

aw_origin_base() {
  local repository=$1 base
  base=$(git -C "$repository" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
  if [[ -z $base ]]; then
    for candidate in origin/main origin/master; do
      git -C "$repository" show-ref --verify --quiet "refs/remotes/$candidate" && { base=$candidate; break; }
    done
  fi
  [[ -n $base ]] || aw_die 'cannot determine the origin default branch'
  printf '%s\n' "$base"
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
}

aw_task_refresh() {
  local task_dir=$1 state_file="$1/state.json" state status_file role role_state any_blocked=false all_completed=true any_deferred=false
  [[ -f $state_file ]] || aw_die "missing task state: $state_file"
  exec {task_lock}>"$task_dir/.state.lock"
  flock "$task_lock"
  state=$(jq '.' "$state_file") || aw_die "invalid task state: $state_file"
  while IFS=$'\t' read -r role status_file; do
    [[ -f $status_file ]] || { role_state=dispatched; all_completed=false; }
    if [[ -f $status_file ]]; then
      role_state=$(jq -r '.state // "dispatched"' "$status_file" 2>/dev/null)
      aw_valid_state "$role_state" || role_state=dispatched
      if [[ $role_state == deferred ]]; then
        any_deferred=true
      fi
      [[ $role_state == blocked ]] && any_blocked=true
      [[ $role_state == completed || $role_state == deferred ]] || all_completed=false
    fi
    state=$(jq --arg role "$role" --arg value "$role_state" '.roles[$role].state=$value' <<<"$state")
  done < <(jq -r '.roles | to_entries[] | [.key, (.value.status_json // .value.status_file)] | @tsv' <<<"$state")

  local current next stage now
  current=$(jq -r '.status' <<<"$state")
  next=$current
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

aw_provider_command() {
  local provider=$1 mode=$2 profile=$3 session_id=${4:-} model_override=${5:-}
  local manifest="$AW_PROVIDER_HOME/$provider.json"
  [[ -f $manifest ]] || aw_die "unknown provider: $provider"
  aw_valid_profile "$profile" || aw_die "invalid permission profile: $profile"
  local command_json
  if [[ $mode == resume && -n $session_id ]]; then
    command_json=$(jq -c --arg id "$session_id" --arg profile "$profile" '[.resume[] | if . == "{session_id}" then $id else . end] + .profiles[$profile]' "$manifest")
  elif [[ $mode == resume ]]; then
    command_json=$(jq -c --arg profile "$profile" '.continue + .profiles[$profile]' "$manifest")
  else
    command_json=$(jq -c --arg profile "$profile" '.fresh + .profiles[$profile]' "$manifest")
  fi
  if [[ -n $model_override ]]; then
    command_json=$(jq -c --arg model "$model_override" '
      reduce .[] as $argument
        ({command:[],skip:false};
          if .skip then .skip=false
          elif $argument == "--model" or $argument == "-m" then .skip=true
          else .command += [$argument]
          end)
      | .command + ["--model",$model]' <<<"$command_json")
  fi
  jq -r '.[] | @sh' <<<"$command_json" | paste -sd' ' -
}

aw_orchestrator_prompt_idle() {
  local provider=$1 content
  content=$(cat)
  case "$provider" in
    codex) grep -Eq '^[[:space:]]*›[[:space:]]+Ask Codex to do anything[[:space:]]*$' <<<"$content" ;;
    claude|grok) grep -Eq '^[[:space:]]*❯[[:space:]]*$' <<<"$content" ;;
    *) return 1 ;;
  esac
}

aw_workspace_risks() {
  local workspace_id=$1 session repository role worktree branch dirty ahead task_count overdue_count=0 active_agents=0 now task_status deadline
  repository=$(tmux list-sessions -F $'#{@aw_workspace_id}\t#{@aw_repository}' 2>/dev/null | awk -F '\t' -v id="$workspace_id" '$1==id {print $2; exit}' || true)
  task_count=0
  now=$(aw_now)
  while IFS= read -r state; do
    [[ $(jq -r '.workspace_id' "$state") == "$workspace_id" ]] || continue
    task_status=$(jq -r '.status' "$state")
    if [[ ! $task_status =~ ^(completed|ready_for_pr|draft_pr)$ ]]; then
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
  case "$status" in
    active) jq -cn '[{id:"wait",label:"Agents are still working",mutating:false}]' ;;
    awaiting_review) jq -cn '[{id:"activate-reviewers",label:"Release the immutable lead artifact to reviewers",mutating:true}]' ;;
    blocked) jq -cn '[{id:"resolve-blocker",label:"Review blockers and dispatch a focused follow-up",mutating:false}]' ;;
    awaiting_triage) jq -cn '[{id:"dispatch-execution",label:"Approve scope and dispatch an execution cycle",mutating:true},{id:"complete-review",label:"Close the review without implementation",mutating:true}]' ;;
    awaiting_integration) jq -cn '[{id:"approve-integration",label:"Validate collisions and approve an integration handoff",mutating:true},{id:"dispatch-verification",label:"Request another independent verification cycle",mutating:true}]' ;;
    integration_ready)
      if jq -e '(.integration.branch // "") != ""' "$state_file" >/dev/null; then
        jq -cn '[{id:"record-integration",label:"Record validation evidence for the integrated branch",mutating:true}]'
      else
        jq -cn '[{id:"integrate",label:"Integrate an explicitly selected commit plan",mutating:true}]'
      fi
      ;;
    awaiting_pr) jq -cn '[{id:"prepare-pr",label:"Prepare PR title, body, and readiness checklist",mutating:true}]' ;;
    awaiting_pr_review) jq -cn '[{id:"approve-pr",label:"Approve the validated integration branch for publication",mutating:true}]' ;;
    ready_for_pr) jq -cn '[{id:"publish",label:"Push and open a draft pull request",mutating:true,external:true}]' ;;
    draft_pr) jq -cn '[{id:"review-pr",label:"Review the draft pull request and CI",mutating:false}]' ;;
    completed) jq -cn '[]' ;;
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
        critical:[$shared[]|select(test("(^|/)(Cargo.lock|package-lock.json|pnpm-lock.yaml|yarn.lock|.*migration.*)$";"i"))]}
    ]) as $overlaps |
    {base:$base,roles:$roles,merges:$merges,overlaps:$overlaps,
      lockfile_policy:{manual_merge:false,instruction:"Resolve Cargo manifests first; take a known-good Cargo.lock side; regenerate affected packages with cargo update -p <crate> --precise <version>; run locked validation and review the diff.",files:([$overlaps[].files[] | select(endswith("Cargo.lock"))] | unique)},
      safe:(($overlaps|length)==0 and ([$merges[]|select(.clean==false)]|length)==0)}'
}
