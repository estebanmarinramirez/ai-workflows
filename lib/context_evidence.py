"""Private snapshots of dispatcher inputs, never claims of full model context."""
import base64
import hashlib
from pathlib import Path
import stat
from assignments import atomic
import telemetry

LIMIT = 1024 * 1024
GAPS = ['Prior conversation, compaction and provider system prompts are not captured.',
        'Tool responses, later user messages and dynamically read files are not captured.',
        'Global and nested repository instructions may affect the session.',
        'Observed files are not proof the provider read or obeyed them.']


def file_record(path, allowed_root):
    path, allowed_root = Path(path), Path(allowed_root).resolve()
    if not path.resolve().is_relative_to(allowed_root):
        return {'status': 'outside_scope'}
    try:
        # No symlink following or unbounded reads of FIFOs/devices.
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode): return {'status': 'not_regular'}
        with path.open('rb') as stream: content = stream.read(LIMIT + 1)
        if len(content) > LIMIT: return {'status': 'too_large'}
        return {'status': 'captured', 'sha256': hashlib.sha256(content).hexdigest(),
                'content_base64': base64.b64encode(content).decode(), 'bytes': len(content)}
    except FileNotFoundError: return {'status': 'missing'}
    except OSError: return {'status': 'unreadable'}


def capture(task, attempt, prompt, boundary, session=None, handover=None):
    state = telemetry.read(task / 'state.json'); role = attempt['role']
    value = state['roles'][role]; tree = Path(value.get('worktree') or task.parent.parent / role)
    files = {}
    for name in ('task.md', 'PROTOCOL.md', 'instructions/AGENTS.md', 'instructions/CLAUDE.md', 'instructions/GROK.md', 'measurement/criteria.txt'):
        files['task/' + name] = file_record(task / name, task)
    for name in ('AGENTS.md', 'CLAUDE.md', 'GROK.md'):
        files['repository/' + name] = file_record(tree / name, tree)
    for name, slot in (('worker-status', role), ('lead-status', state.get('lead'))):
        info = state.get('roles', {}).get(slot, {})
        if slot:
            path = Path(info.get('status_json') or info.get('status_file') or task / (slot + '.status.json'))
            files[name] = file_record(path, task)
    return dict(schema_version=1, scope='dispatcher_inputs_only', replay_eligible=False,
                workspace=state.get('workspace_id'), task=task.name, attempt_id=attempt['id'], role=role,
                boundary=boundary, session_reference=session, requested_settings=attempt['choice'],
                repository_baseline=attempt['baseline'], prompt=prompt,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest() if prompt is not None else None,
                files=files, handover=handover, limitations=GAPS)


def store(directory, document):
    digest = telemetry.digest(document)
    folder = directory / 'contexts'; folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (digest + '.json')
    if path.exists():
        if telemetry.digest(telemetry.read(path)) != digest: raise ValueError('existing context artifact changed')
    else: atomic(path, document)
    return digest


def inspect(directory, digest):
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        return 'invalid_reference'
    path = directory / 'contexts' / (digest + '.json')
    try:
        if not path.is_file(): return 'missing'
        if telemetry.digest(telemetry.read(path)) != digest: return 'changed'
        return 'intact'
    except (OSError, ValueError): return 'unreadable'
