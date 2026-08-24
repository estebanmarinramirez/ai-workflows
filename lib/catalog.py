#!/usr/bin/env python3
"""Deterministic local/GitHub repository catalog for Workspaces."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlparse


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "agent-workspaces"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "agent-workspaces"
CONFIG_FILE = CONFIG_HOME / "config.json"
CATALOG_FILE = DATA_HOME / "catalog.json"
MANUAL_FILE = CONFIG_HOME / "catalog-paths.json"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else ""


def canonical_remote(remote: str) -> tuple[str, str]:
    remote = remote.strip()
    if not remote:
        return "", ""
    if remote.startswith("git@") and ":" in remote:
        host, path = remote[4:].split(":", 1)
    elif "://" in remote:
        parsed = urlparse(remote)
        host, path = parsed.hostname or "", parsed.path.lstrip("/")
    else:
        return "", remote
    path = path.removesuffix(".git").strip("/")
    identity = f"forge:{host.lower()}/{path.lower()}"
    return identity, f"https://{host}/{path}"


def git_record(path: Path) -> dict | None:
    top = run("git", "-C", str(path), "rev-parse", "--show-toplevel")
    bare = run("git", "-C", str(path), "rev-parse", "--is-bare-repository") == "true"
    if not top and not bare:
        return None
    root = Path(top or path).resolve()
    common = run("git", "-C", str(root), "rev-parse", "--git-common-dir")
    if common and not Path(common).is_absolute():
        common = str((root / common).resolve())
    remote = run("git", "-C", str(root), "remote", "get-url", "origin")
    identity, web_url = canonical_remote(remote)
    if not identity:
        digest = hashlib.sha256((common or str(root)).encode()).hexdigest()[:16]
        identity = f"local:{digest}"
    porcelain = run("git", "-C", str(root), "status", "--porcelain") if not bare else ""
    worktrees = run("git", "-C", str(root), "worktree", "list", "--porcelain")
    return {
        "id": identity,
        "name": root.name.removesuffix(".git"),
        "local_path": str(root),
        "common_dir": common,
        "remote": remote,
        "web_url": web_url,
        "local": True,
        "bare": bare,
        "branch": run("git", "-C", str(root), "branch", "--show-current"),
        "dirty": bool(porcelain),
        "worktree_count": worktrees.count("worktree "),
        "last_commit_at": run("git", "-C", str(root), "log", "-1", "--format=%cI"),
        "indexed_at": now(),
    }


def candidate_paths(config: dict) -> list[Path]:
    catalog = config.get("repository_catalog", {})
    excluded = set(catalog.get("exclude_names", []))
    max_depth = int(catalog.get("max_depth", 12))
    roots = [Path(os.path.expanduser(p)) for p in config.get("project_roots", [])]
    roots += [Path(os.path.expanduser(p)) for p in load_json(MANUAL_FILE, {"paths": []}).get("paths", [])]
    found: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        root = root.resolve()
        for current, directories, files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            directories[:] = [d for d in directories if d not in excluded]
            if depth >= max_depth:
                directories[:] = []
            if ".git" in directories or ".git" in files:
                found.add(current_path)
                directories[:] = []
            elif "HEAD" in files and "objects" in directories and "refs" in directories:
                found.add(current_path)
                directories[:] = []
    return sorted(found)


def workspace_metadata() -> dict[str, dict]:
    result: dict[str, dict] = {}
    active_ids = set(run("tmux", "list-sessions", "-F", "#{@aw_workspace_id}").splitlines())
    for manifest in DATA_HOME.glob("*/*/workspace.json"):
        data = load_json(manifest, {})
        repository = data.get("repository")
        if not repository:
            continue
        key = str(Path(repository).expanduser().resolve())
        entry = result.setdefault(key, {"workspace_sets": [], "workspace_ids": [], "active_workspace_count": 0, "last_agent_access": ""})
        entry["workspace_sets"].append(data.get("set", manifest.parent.name))
        workspace_id = data.get("workspace_id", "")
        if workspace_id:
            entry["workspace_ids"].append(workspace_id)
            entry["active_workspace_count"] += int(workspace_id in active_ids)
        entry["last_agent_access"] = max(entry["last_agent_access"], data.get("updated_at", ""))
    for entry in result.values():
        try:
            accessed = dt.datetime.fromisoformat(entry["last_agent_access"].replace("Z", "+00:00"))
            entry["recently_used"] = (dt.datetime.now(dt.timezone.utc) - accessed).days < 30
        except ValueError:
            entry["recently_used"] = False
    return result


def github_records(config: dict) -> list[dict]:
    fields = "name,nameWithOwner,url,sshUrl,isPrivate,isFork,isArchived,pushedAt,updatedAt,defaultBranchRef,description"
    records = []
    for owner in config.get("repository_catalog", {}).get("github_owners", []):
        output = run("gh", "repo", "list", owner, "--limit", "1000", "--json", fields)
        if not output:
            raise RuntimeError(f"GitHub inventory failed for {owner}")
        for repo in json.loads(output):
            full = repo["nameWithOwner"]
            records.append({
                "id": f"forge:github.com/{full.lower()}",
                "name": repo["name"],
                "github": full,
                "remote": repo["sshUrl"],
                "web_url": repo["url"],
                "local": False,
                "private": repo["isPrivate"],
                "fork": repo["isFork"],
                "archived": repo["isArchived"],
                "default_branch": (repo.get("defaultBranchRef") or {}).get("name", ""),
                "description": repo.get("description") or "",
                "remote_pushed_at": repo.get("pushedAt") or "",
                "remote_updated_at": repo.get("updatedAt") or "",
                "indexed_at": now(),
            })
    return records


def merge_record(left: dict, right: dict) -> dict:
    merged = dict(left)
    for key, value in right.items():
        if value not in (None, "", [], False) or key not in merged:
            merged[key] = value
    merged["local"] = bool(left.get("local") or right.get("local"))
    return merged


def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def github_cache_stale(config: dict, refreshed_at: str) -> bool:
    if not refreshed_at:
        return True
    try:
        refreshed = dt.datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    ttl = int(config.get("repository_catalog", {}).get("github_ttl_seconds", 900))
    return (dt.datetime.now(dt.timezone.utc) - refreshed).total_seconds() >= ttl


LOCAL_FIELDS = {
    "local_path", "common_dir", "local", "bare", "branch", "dirty",
    "worktree_count", "last_commit_at", "workspace_sets", "workspace_ids",
    "active_workspace_count", "last_agent_access", "recently_used",
}


def remote_only(record: dict) -> dict:
    preserved = {key: value for key, value in record.items() if key not in LOCAL_FIELDS}
    preserved["local"] = False
    return preserved


def refresh(include_github: bool) -> dict:
    config = load_json(CONFIG_FILE, {})
    previous = load_json(CATALOG_FILE, {"repositories": []})
    records: dict[str, dict] = {}
    if not include_github:
        for record in previous.get("repositories", []):
            if record.get("github"):
                records[record["id"]] = remote_only(record)
    if include_github:
        for record in github_records(config):
            records[record["id"]] = merge_record(records.get(record["id"], {}), record)
    metadata = workspace_metadata()
    paths = set(candidate_paths(config))
    paths.update(Path(path) for path in metadata)
    for path in sorted(paths):
        record = git_record(path)
        if not record:
            continue
        record.update(metadata.get(record["local_path"], {}))
        records[record["id"]] = merge_record(records.get(record["id"], {}), record)
    github_refreshed_at = now() if include_github else previous.get("github_refreshed_at", "")
    result = {
        "schema_version": 1,
        "refreshed_at": now(),
        "github_refreshed_at": github_refreshed_at,
        "github_stale": False if include_github else github_cache_stale(config, github_refreshed_at),
        "repositories": sorted(records.values(), key=lambda item: (not item.get("local", False), item.get("name", "").lower())),
    }
    atomic_write(CATALOG_FILE, result)
    return result


def add_path(value: str) -> None:
    path = str(Path(value).expanduser().resolve())
    current = load_json(MANUAL_FILE, {"schema_version": 1, "paths": []})
    current["paths"] = sorted(set(current.get("paths", [])) | {path})
    atomic_write(MANUAL_FILE, current)


def clone_repo(identity: str, destination: str, confirmed: bool) -> None:
    if not confirmed:
        raise RuntimeError("clone requires --confirmed-by-user")
    config = load_json(CONFIG_FILE, {})
    catalog = load_json(CATALOG_FILE, {"repositories": []})
    record = next((item for item in catalog["repositories"] if item["id"] == identity), None)
    if not record or not record.get("remote"):
        raise RuntimeError("catalog entry is not cloneable")
    target = Path(destination).expanduser() if destination else Path(os.path.expanduser(config["repository_catalog"]["clone_root"])) / record["name"]
    if target.exists():
        raise RuntimeError(f"clone target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", record["remote"], str(target)], check=True)
    add_path(str(target))
    refresh(False)
    print(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    refresh_parser = sub.add_parser("refresh")
    refresh_parser.add_argument("--github", action="store_true")
    sub.add_parser("show")
    add_parser = sub.add_parser("add")
    add_parser.add_argument("path")
    clone_parser = sub.add_parser("clone")
    clone_parser.add_argument("identity")
    clone_parser.add_argument("destination", nargs="?", default="")
    clone_parser.add_argument("--confirmed-by-user", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "refresh":
            data = refresh(args.github)
            print(json.dumps({"repositories": len(data["repositories"]), "catalog": str(CATALOG_FILE), "github": args.github}))
        elif args.command == "show":
            print(json.dumps(load_json(CATALOG_FILE, {"schema_version": 1, "repositories": []})))
        elif args.command == "add":
            add_path(args.path)
            refresh(False)
        elif args.command == "clone":
            clone_repo(args.identity, args.destination, args.confirmed_by_user)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"workspaces catalog: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
