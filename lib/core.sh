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
aw_valid_state() { [[ $1 =~ ^(dispatched|in_progress|blocked|completed)$ ]]; }
aw_valid_profile() { [[ $1 =~ ^(safe|trusted|yolo)$ ]]; }

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
}

aw_task_refresh() {
  local task_dir=$1 state_file="$1/state.json" state status_file role role_state any_blocked=false all_completed=true
  [[ -f $state_file ]] || aw_die "missing task state: $state_file"
  exec {task_lock}>"$task_dir/.state.lock"
  flock "$task_lock"
  state=$(jq '.' "$state_file") || aw_die "invalid task state: $state_file"
  while IFS=$'\t' read -r role status_file; do
    [[ -f $status_file ]] || { role_state=dispatched; all_completed=false; }
    if [[ -f $status_file ]]; then
      role_state=$(jq -r '.state // "dispatched"' "$status_file" 2>/dev/null)
      aw_valid_state "$role_state" || role_state=dispatched
      [[ $role_state == blocked ]] && any_blocked=true
      [[ $role_state == completed ]] || all_completed=false
    fi
    state=$(jq --arg role "$role" --arg value "$role_state" '.roles[$role].state=$value' <<<"$state")
  done < <(jq -r '.roles | to_entries[] | [.key, (.value.status_json // .value.status_file)] | @tsv' <<<"$state")

  local current next stage now
  current=$(jq -r '.status' <<<"$state")
  next=$current
  if [[ $current == active && $any_blocked == true ]]; then
    next=blocked
  elif [[ $current == active && $all_completed == true ]]; then
    stage=$(jq -r '.stage' <<<"$state")
    case "$stage" in
      review) next=awaiting_triage ;;
      execute|verify) next=awaiting_integration ;;
      integrate) next=awaiting_pr ;;
    esac
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
  printf '%s\n' "$next"
}

aw_provider_command() {
  local provider=$1 mode=$2 profile=$3 session_id=${4:-}
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
  jq -r '.[] | @sh' <<<"$command_json" | paste -sd' ' -
}

aw_workspace_risks() {
  local workspace_id=$1 session repository role worktree branch dirty ahead task_count active_agents=0
  repository=$(tmux list-sessions -F $'#{@aw_workspace_id}\t#{@aw_repository}' 2>/dev/null | awk -F '\t' -v id="$workspace_id" '$1==id {print $2; exit}')
  task_count=0
  while IFS= read -r state; do
    [[ $(jq -r '.workspace_id' "$state") == "$workspace_id" ]] || continue
    [[ $(jq -r '.status' "$state") =~ ^(completed|ready_for_pr|draft_pr)$ ]] || ((task_count+=1))
  done < <(find "$AW_DATA_HOME" -path '*/.coordination/*/state.json' -type f -print 2>/dev/null)
  printf 'Workspace: %s\nActive tasks: %s\n' "$workspace_id" "$task_count"
  while IFS=$'\t' read -r session role; do
    [[ -n $session ]] || continue
    if [[ $role != integration ]] && omarchy-agent-pane-active "$role" "$session" 2>/dev/null; then ((active_agents+=1)); fi
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

aw_collision_json() {
  local task_dir=$1 state_file="$1/state.json" repository base role worktree
  [[ -f $state_file ]] || aw_die "missing task: $task_dir"
  repository=$(jq -r '.repository' "$state_file")
  base=$(git -C "$repository" rev-parse HEAD)
  local entries='[]'
  while IFS=$'\t' read -r role worktree; do
    local files
    files=$(git -C "$worktree" diff --name-only "$base...HEAD" 2>/dev/null | jq -Rsc 'split("\n")|map(select(length>0))')
    entries=$(jq --arg role "$role" --arg worktree "$worktree" --argjson files "$files" '. + [{role:$role,worktree:$worktree,files:$files}]' <<<"$entries")
  done < <(jq -r '.roles | to_entries[] | [.key,.value.worktree] | @tsv' "$state_file")
  local merges='[]' count i j left_role right_role left_tree right_tree left_head right_head clean
  count=$(jq 'length' <<<"$entries")
  for ((i=0; i<count; i++)); do
    for ((j=i+1; j<count; j++)); do
      left_role=$(jq -r ".[$i].role" <<<"$entries"); right_role=$(jq -r ".[$j].role" <<<"$entries")
      left_tree=$(jq -r ".[$i].worktree" <<<"$entries"); right_tree=$(jq -r ".[$j].worktree" <<<"$entries")
      left_head=$(git -C "$left_tree" rev-parse HEAD); right_head=$(git -C "$right_tree" rev-parse HEAD)
      if git -C "$repository" merge-tree --write-tree --quiet "$left_head" "$right_head" >/dev/null 2>&1; then clean=true; else clean=false; fi
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
      safe:(($overlaps|length)==0 and ([$merges[]|select(.clean==false)]|length)==0)}'
}
