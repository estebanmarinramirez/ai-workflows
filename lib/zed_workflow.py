"""Zed-native attempt records; no CLI workers, provider calls or replay promotion."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from assignments import now
from dream_capture import clone, git, locked, read, safe_id, verify, write
from provider_adapter import sha

VERSION = "zed-native-v1"
ACTIVE = {"prepared", "verifying"}
LIMITATIONS = [
    "Only the outgoing brief and returned report are retained, not complete Zed context.",
    "System instructions, tool results, compaction and resumed conversation are unobserved.",
    "Model identity is requested, not provider-attested; usage and billing are unknown.",
    "Checkout boundaries are instructions, not a sandbox; verification uses trusted local code.",
]


def positive(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("limits must be positive integers")
    return value


def initialize(
    repository: Path,
    directory: Path,
    brief: Path,
    verifier: Path,
    max_attempts: int = 3,
    max_active: int = 1,
    timeout: int = 120,
) -> dict:
    repository, directory = repository.resolve(), directory.resolve()
    verifier = verifier.resolve()
    for value in (max_attempts, max_active, timeout):
        positive(value)
    if max_active > max_attempts:
        raise ValueError("max_active cannot exceed max_attempts")
    if directory.is_relative_to(repository):
        raise ValueError("keep task artifacts outside the source checkout")
    if verifier.is_relative_to(repository):
        raise ValueError("trusted verifier must be outside the worker source")
    if git(repository, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("source checkout must be clean")
    tracked = git(repository, "ls-files", "--stage")
    if any(line.startswith("160000 ") for line in tracked.splitlines()):
        raise ValueError("submodules are not supported")
    baseline = git(repository, "rev-parse", "HEAD")
    prompt, verifier_bytes = brief.read_bytes(), verifier.read_bytes()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    (directory / "brief.txt").write_bytes(prompt)
    (directory / "verifier.py").write_bytes(verifier_bytes)
    for name in ("brief.txt", "verifier.py"):
        (directory / name).chmod(0o600)
    clone(repository, directory / "baseline", baseline)
    contract = dict(
        schema_version=1,
        engine=VERSION,
        baseline=baseline,
        max_attempts=max_attempts,
        max_active=max_active,
        verifier_timeout=timeout,
        files={name: sha(directory / name) for name in ("brief.txt", "verifier.py")},
        replay_eligible=False,
        context_scope="zed_dispatch_inputs_only",
        limitations=LIMITATIONS,
        created_at=now(),
    )
    write(directory / "contract.json", contract)
    (directory / "contract.sha256").write_text(sha(directory / "contract.json"))
    write(directory / "journal.json", {"attempts": []})
    return contract


def load(directory: Path) -> dict:
    if sha(directory / "contract.json") != (directory / "contract.sha256").read_text():
        raise ValueError("frozen contract changed")
    contract = read(directory / "contract.json")
    if contract["engine"] != VERSION:
        raise ValueError("incompatible Zed recorder version")
    for name, digest in contract["files"].items():
        if sha(directory / name) != digest:
            raise ValueError("frozen input changed: " + name)
    if git(directory / "baseline", "rev-parse", "HEAD") != contract["baseline"]:
        raise ValueError("baseline changed")
    return contract


def prepare(directory: Path, model: str) -> dict:
    directory = directory.resolve()
    if not model.strip():
        raise ValueError("an exact model ID from Zed is required")
    with locked(directory):
        contract = load(directory)
        journal = read(directory / "journal.json")
        attempts = journal["attempts"]
        if len(attempts) >= contract["max_attempts"]:
            raise ValueError("attempt budget exhausted")
        if sum(a["state"] in ACTIVE for a in attempts) >= contract["max_active"]:
            raise ValueError("active attempt limit reached")
        identifier = f"{len(attempts) + 1:06d}"
        base = directory / identifier
        row = dict(
            id=identifier,
            state="prepared",
            requested_model=model,
            prepared_at=now(),
            session_id=None,
            replay_eligible=False,
            usage=None,
            cost_usd=None,
            result=None,
            artifacts={},
        )
        # Claim before clone/dispatch: interruptions remain visible and consume admission.
        attempts.append(row)
        write(directory / "journal.json", journal)
        base.mkdir(mode=0o700)
        clone(directory / "baseline", base / "candidate", contract["baseline"])
        prompt = (directory / "brief.txt").read_text()
        prompt += (
            "\n\nZed worker contract:\n"
            f"Work only in {base / 'candidate'}. Read applicable repository instructions.\n"
            "Do not delegate, commit, push, merge, access sibling directories or change the verifier.\n"
            "Make only the requested changes. Return a factual report of changes, checks and limitations.\n"
            "Your completion report is not independent acceptance.\n"
        )
        (base / "prompt.txt").write_text(prompt)
        (base / "prompt.txt").chmod(0o600)
        row["artifacts"]["prompt.txt"] = sha(base / "prompt.txt")
        write(directory / "journal.json", journal)
        return dict(
            attempt_id=identifier,
            candidate=str(base / "candidate"),
            model=model,
            message=prompt,
            replay_eligible=False,
        )


def finish(directory: Path, identifier: str, session_id: str, report: Path) -> dict:
    directory = directory.resolve()
    safe_id(identifier)
    if not session_id.strip():
        raise ValueError("record the actual returned Zed session ID")
    report_bytes = report.read_bytes()
    with locked(directory):
        contract = load(directory)
        journal = read(directory / "journal.json")
        row = next((a for a in journal["attempts"] if a["id"] == identifier), None)
        if row is None or row["state"] != "prepared":
            raise ValueError(
                "attempt is not prepared; verification is never automatically retried"
            )
        base = directory / identifier
        for name, digest in row["artifacts"].items():
            if sha(base / name) != digest:
                raise ValueError("attempt evidence changed: " + name)
        if "prompt.txt" not in row["artifacts"]:
            raise ValueError("preparation was interrupted")
        candidate = base / "candidate"
        if git(candidate, "rev-parse", "HEAD") != contract["baseline"]:
            raise ValueError(
                "worker changed HEAD; preserve for diagnosis rather than guessing lineage"
            )
        (base / "report.txt").write_bytes(report_bytes)
        (base / "report.txt").chmod(0o600)
        row.update(state="verifying", session_id=session_id, returned_at=now())
        row["artifacts"]["report.txt"] = sha(base / "report.txt")
        write(directory / "journal.json", journal)
        git(candidate, "add", "-A")
        tree = git(candidate, "write-tree")
        snapshot = git(
            candidate,
            "commit-tree",
            tree,
            "-p",
            contract["baseline"],
            "-m",
            "Zed candidate " + identifier,
        )
        git(candidate, "update-ref", "refs/heads/zed-snapshot", snapshot)
        row["output_commit"] = snapshot
        write(directory / "journal.json", journal)
        clone(candidate, base / "verification-checkout", snapshot)
        expected_verifier = contract["files"]["verifier.py"]
        if sha(directory / "verifier.py") != expected_verifier:
            raise ValueError("frozen verifier changed before execution")
        result = verify(
            base / "verification-checkout",
            base / "verification",
            directory / "verifier.py",
            contract["verifier_timeout"],
            dict(
                attempt_id=identifier,
                baseline_commit=contract["baseline"],
                output_commit=snapshot,
            ),
        )
        if (
            result["verifier_sha256"] != expected_verifier
            or sha(directory / "verifier.py") != expected_verifier
        ):
            raise ValueError("frozen verifier changed during verification")
        row.update(state="finished", finished_at=now(), result=result)
        for name in (
            "verification/result.json",
            "verification/stdout.log",
            "verification/stderr.log",
        ):
            row["artifacts"][name] = sha(base / name)
        write(directory / "journal.json", journal)
        return row


def abandon(directory: Path, identifier: str, reason: str) -> dict:
    directory = directory.resolve()
    if not reason.strip():
        raise ValueError("record why this attempt was abandoned")
    with locked(directory):
        load(directory)
        journal = read(directory / "journal.json")
        row = next((a for a in journal["attempts"] if a["id"] == identifier), None)
        if row is None or row["state"] not in ACTIVE:
            raise ValueError("attempt is not active")
        row.update(state="abandoned", reason=reason, finished_at=now())
        write(directory / "journal.json", journal)
        return row


def status(directory: Path) -> dict:
    directory = directory.resolve()
    with locked(directory):
        contract = load(directory)
        journal = read(directory / "journal.json")
        for row in journal["attempts"]:
            row["snapshot_integrity"] = "not_captured"
            if row.get("output_commit"):
                try:
                    candidate = directory / row["id"] / "candidate"
                    retained = git(candidate, "rev-parse", "refs/heads/zed-snapshot")
                    git(candidate, "fsck", "--full")
                    row["snapshot_integrity"] = (
                        "intact"
                        if retained == row["output_commit"]
                        else "missing_or_changed"
                    )
                except (OSError, subprocess.SubprocessError):
                    row["snapshot_integrity"] = "missing_or_changed"
            row["integrity"] = {}
            for name, digest in row["artifacts"].items():
                path = directory / row["id"] / name
                row["integrity"][name] = (
                    "intact"
                    if path.is_file() and sha(path) == digest
                    else "missing_or_changed"
                )
        return dict(
            contract=contract,
            **journal,
            automatic_integration=False,
            usage_complete=False,
            replay_eligible=False,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("repository", type=Path)
    init.add_argument("directory", type=Path)
    init.add_argument("--brief", type=Path, required=True)
    init.add_argument("--verifier", type=Path, required=True)
    init.add_argument("--max-attempts", type=int, default=3)
    init.add_argument("--max-active", type=int, default=1)
    init.add_argument("--timeout", type=int, default=120)
    start = sub.add_parser("prepare")
    start.add_argument("directory", type=Path)
    start.add_argument("--model", required=True)
    end = sub.add_parser("finish")
    end.add_argument("directory", type=Path)
    end.add_argument("identifier")
    end.add_argument("--session-id", required=True)
    end.add_argument("--report", type=Path, required=True)
    stop = sub.add_parser("abandon")
    stop.add_argument("directory", type=Path)
    stop.add_argument("identifier")
    stop.add_argument("--reason", required=True)
    show = sub.add_parser("status")
    show.add_argument("directory", type=Path)
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        result = {
            "init": initialize,
            "prepare": prepare,
            "finish": finish,
            "abandon": abandon,
            "status": status,
        }[command](**args)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("zed workflow: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
