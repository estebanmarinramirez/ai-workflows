#!/bin/bash

# Shared runtime for producers that feed Omarchy's native Agents panel.

aw_usage_state_root() {
  printf '%s/omarchy/agents\n' "${XDG_STATE_HOME:-$HOME/.local/state}"
}

aw_usage_log() {
  local level=$1 collector=$2 message=$3 root log
  root=$(aw_usage_state_root)
  log="$root/usage-update.log"
  mkdir -p "$root"
  printf '%s [%s] [%s] %s\n' "$(date --iso-8601=seconds)" "$level" "$collector" "$message" >>"$log"
  if [[ $(wc -l <"$log") -gt 1000 ]]; then
    local temporary
    temporary=$(mktemp "$root/.usage-log.XXXXXX") || return 0
    tail -n 1000 "$log" >"$temporary" && mv "$temporary" "$log"
  fi
}

aw_usage_validate_record() {
  local expected_id=$1
  jq -e --arg id "$expected_id" '
    .schemaVersion == 1 and
    .id == $id and
    (.name | type == "string") and
    (.ready | type == "boolean") and
    (.limits | type == "array") and
    (.updatedAt | type == "string")
  ' >/dev/null
}

aw_usage_atomic_write() {
  local target=$1 content=$2 directory temporary
  directory=$(dirname "$target")
  mkdir -p "$directory"
  temporary=$(mktemp "$directory/.usage.XXXXXX") || return 1
  if ! printf '%s\n' "$content" >"$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  mv "$temporary" "$target"
}

aw_usage_refresh_collector() {
  local id=$1 collector=$2 usage_dir=$3 record
  if [[ ! -x $collector ]]; then
    aw_usage_log ERROR "$id" "collector is missing or not executable: $collector"
    return 1
  fi
  if ! record=$($collector); then
    aw_usage_log ERROR "$id" "collector exited unsuccessfully; retaining last good record"
    return 1
  fi
  if ! aw_usage_validate_record "$id" <<<"$record"; then
    aw_usage_log ERROR "$id" "collector returned an invalid panel record; retaining last good record"
    return 1
  fi
  if ! aw_usage_atomic_write "$usage_dir/$id.json" "$record"; then
    aw_usage_log ERROR "$id" "could not publish record"
    return 1
  fi
  aw_usage_log INFO "$id" "record refreshed"
}
