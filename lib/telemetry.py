"""Local observational ledger. Never trains a policy or reads conversation content."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

SCHEMA = 1

def now():
    return datetime.now(timezone.utc).isoformat()

def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()

def pick(value, keys):
    return {k: value[k] for k in keys.split() if k in value}

def roots():
    home = Path.home()
    return (Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')) / 'agent-workspaces',
            Path(os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 'agent-workspaces/telemetry',
            Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'agent-workspaces/config.json')

def read(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('expected JSON object')
    return value

# Structured operational fields only. Prompts, summaries, commands, paths to
# changed files, blockers and arbitrary event details are deliberately omitted.
CHOICE = 'provider model effort model_version cli_version selected_at acknowledged_at'
TASK = 'schema_version id workspace_id stage status profile created_at updated_at lead difficulty family instance_id'
ROLE = 'state provider model effort model_version updated_at started_at completed_at'
TRANSITION = 'from to at actor'

def task_view(obj):
    result = pick(obj, TASK)
    result['gates'] = {k: v for k, v in obj.get('gates', {}).items() if isinstance(v, bool)}
    result['roles'] = {k: pick(v, ROLE) for k, v in obj.get('roles', {}).items() if isinstance(v, dict)}
    result['transitions'] = [pick(v, TRANSITION) for v in obj.get('transitions', []) if isinstance(v, dict)]
    result['routing'] = pick(obj.get('routing', {}), 'difficulty revision effort_mode')
    result['integration'] = pick(obj.get('integration', {}), 'branch selected_commits integrated_at')
    return result

def role_view(obj):
    result = pick(obj, 'schema_version task_id role state updated_at commits')
    result['changed_file_count'] = len(obj.get('changed_files', []))
    result['blocker_count'] = len(obj.get('blockers', []))
    result['evidence'] = [dict(pick(v, 'exit_code at'), command_sha256=digest(v.get('command')))
                          for v in obj.get('evidence', []) if isinstance(v, dict)]
    result['verification_provenance'] = 'agent_reported_not_independent'
    return result

def agents_view(obj):
    result = {'active': {k: pick(v, CHOICE) for k, v in obj.get('active', {}).items() if isinstance(v, dict)},
              'requests': {}}
    for key, v in obj.get('requests', {}).items():
        result['requests'][key] = dict(pick(v, 'id slot role phase source outgoing_provider requested_at ready_at applied_at accepted_at'),
                                      target=pick(v.get('target', {}), CHOICE))
    return result

def routing_view(obj):
    result = pick(obj, 'at mode effort_mode intent_revision task_revision difficulty review_reserve signature')
    result['decisions'] = [dict(pick(v, 'role slot action pool change_id'), target=pick(v.get('target', {}), CHOICE))
                           for v in obj.get('decisions', []) if isinstance(v, dict)]
    return result

def quota_view(obj):
    return dict(pick(obj, 'schemaVersion id ready updatedAt hasLocalStats hasPromptStats todayTotalTokens todayPrompts todaySessions totalPrompts totalSessions'),
                scope='account_not_task',
                modelUsage={k: pick(v, 'inputTokens outputTokens cacheReadInputTokens cacheCreationInputTokens') for k, v in obj.get('modelUsage', {}).items() if isinstance(v, dict)},
                todayTokensByModel={k: v for k, v in obj.get('todayTokensByModel', {}).items() if type(v) in (int, float)},
                limits=[pick(v, 'percent resetsAt') for v in obj.get('limits', []) if isinstance(v, dict)])

class Ledger:
    def __init__(self, directory, timeout=30):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.path = directory / 'ledger.sqlite'
        self.db = sqlite3.connect(self.path, timeout=timeout)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL,
              observed_at TEXT NOT NULL, source_at TEXT, workspace TEXT, task TEXT,
              kind TEXT NOT NULL, source TEXT NOT NULL, provenance TEXT NOT NULL,
              event_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_scope ON events(workspace,task,kind);
            CREATE TABLE IF NOT EXISTS snapshots(source TEXT PRIMARY KEY, signature TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS health(scope TEXT PRIMARY KEY, checked_at TEXT, report TEXT);
            CREATE TRIGGER IF NOT EXISTS immutable_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only events'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only events'); END;
        ''')

    def append(self, kind, source, payload, workspace=None, task=None, provenance='observed', key=None):
        key = key or digest([source, payload])
        self.db.execute('INSERT OR IGNORE INTO events(schema_version,observed_at,source_at,workspace,task,kind,source,provenance,event_key,payload) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (SCHEMA, now(), payload.get('at') or payload.get('updated_at') or payload.get('updatedAt'), workspace, task, kind, source, provenance, key, encoded(payload)))

    def snapshot(self, kind, source, payload, workspace=None, task=None):
        # Compare only to the last state: A -> B -> A is three observations.
        previous = self.db.execute('SELECT signature FROM snapshots WHERE source=?', (source,)).fetchone()
        signature = digest(payload)
        if previous and previous['signature'] == signature:
            return
        sequence = self.db.execute('SELECT coalesce(max(id),0)+1 FROM events').fetchone()[0]
        self.append(kind, source, payload, workspace, task,
                    'initial_observation' if previous is None else 'observed_change', digest([source, signature, sequence]))
        self.db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?)', (source, signature))

    def close(self):
        self.db.close()


def collect(data=None, directory=None, config=None, workspace=None, fast=False):
    default_data, default_dir, default_config = roots()
    data, directory, config = Path(data or default_data), Path(directory or default_dir), Path(config or default_config)
    settings = read(config) if config.exists() else {}
    if settings.get('telemetry', {}).get('enabled', True) is False:
        return {'enabled': False}
    ledger = Ledger(directory, timeout=2 if fast else 30)
    report = {'enabled': True, 'workspaces': 0, 'tasks': 0, 'errors': [], 'active_session_observation': 'unavailable', 'scope': str(workspace) if workspace else 'all', 'fast': fast}
    try:
        ledger.db.execute('BEGIN IMMEDIATE')
        before = ledger.db.execute('SELECT count(*) FROM events').fetchone()[0]
        def capture(path, kind, view, ws=None, task=None):
            if not path.is_file(): return
            try:
                ledger.snapshot(kind, str(path), view(read(path)), ws, task)
            except (OSError, ValueError, TypeError, AttributeError) as error:
                report['errors'].append({'source': str(path), 'error': type(error).__name__})
        capture(config, 'configuration', lambda v: {
            'model_policy': {k: pick(x, CHOICE) for k, x in v.get('model_policy', {}).items() if isinstance(x, dict)},
            'routing': pick(v.get('routing', {}), 'mode effort_mode allowed_models review_reserve'),
            **pick(v, 'default_profile default_layout orchestrator_provider orchestrator_model orchestrator_profile')})
        ledger.snapshot('collector', 'agent-workspaces', {'schema_version': SCHEMA, 'version': (Path(__file__).resolve().parent.parent / 'VERSION').read_text().strip()})
        manifests = [Path(workspace) / 'workspace.json'] if workspace else sorted(data.glob('*/*/workspace.json'))
        for manifest in manifests:
            root = manifest.parent
            ws = str(root.relative_to(data))  # project + workspace avoids ID collisions
            report['workspaces'] += 1
            capture(manifest, 'workspace', lambda v: dict(pick(v, 'schema_version workspace_id created_at profile layout machine_id'), routing=pick(v.get('routing', {}), 'mode effort_mode revision')), ws)
            for checkout in ([] if fast else sorted(root.iterdir())):
                if not checkout.is_dir() or not (checkout / '.git').exists(): continue
                try:
                    def git(*args):
                        return subprocess.check_output(['git', '-C', str(checkout), *args], text=True, stderr=subprocess.DEVNULL, timeout=5, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}).strip()
                    head = git('rev-parse', 'HEAD')
                    branch = git('rev-parse', '--abbrev-ref', 'HEAD')
                    changes = git('status', '--porcelain', '-z')
                    # NUL output avoids newline-containing filenames distorting counts.
                    ledger.snapshot('worktree', str(checkout), {'slot': checkout.name, 'head': head, 'branch': branch, 'dirty': bool(changes)}, ws)
                except (OSError, subprocess.SubprocessError):
                    report['errors'].append({'source': str(checkout), 'error': 'git_observation_unavailable'})
            coord = root / '.coordination'
            capture(coord / 'agents.json', 'assignments', agents_view, ws)
            capture(coord / 'routing.json', 'routing', routing_view, ws)
            for state in sorted(coord.glob('*/state.json')):
                task = state.parent.name
                report['tasks'] += 1
                capture(state, 'task', task_view, ws, task)
                for status in sorted(state.parent.glob('*.status.json')):
                    capture(status, 'role', role_view, ws, task)
            for name in ('audit.jsonl', 'routing-audit.jsonl'):
                path = coord / name
                if not path.is_file(): continue
                occurrences = Counter()
                try:
                    with path.open() as stream:
                        for line in stream:
                            try:
                                raw = json.loads(line)
                                if name == 'audit.jsonl':
                                    payload = dict(pick(raw, 'schema_version at event actor'), details=pick(raw.get('details', {}), 'task_id role state from to workspace_id change_id'))
                                else:
                                    payload = routing_view(raw)
                                signature = digest(raw)
                                occurrences[signature] += 1
                                ledger.append('audit' if name == 'audit.jsonl' else 'routing_history', str(path), payload, ws,
                                              raw.get('details', {}).get('task_id'), 'imported_source_event',
                                              digest([str(path), signature, occurrences[signature]]))
                            except (ValueError, TypeError, AttributeError):
                                report['errors'].append({'source': str(path), 'error': 'invalid_event'})
                except OSError as error:
                    report['errors'].append({'source': str(path), 'error': type(error).__name__})
        usage = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'omarchy/agents/usage'
        for path in ([] if fast else sorted(usage.glob('*.json'))):
            capture(path, 'quota', quota_view)
        try:
            if fast: raise OSError('runtime observation deferred to background scan')
            run = subprocess.run(['tmux', 'list-sessions', '-F', '#{session_name}\t#{@aw_workspace_id}\t#{@aw_role}\t#{@aw_provider}'], capture_output=True, text=True, timeout=5)
            if run.returncode == 0:
                sessions = []
                for line in run.stdout.splitlines():
                    fields = line.split('\t')
                    if len(fields) == 4 and fields[1]:
                        sessions.append(dict(zip(('session', 'workspace_id', 'role', 'provider'), fields)))
                ledger.snapshot('session_inventory', 'tmux', {'sessions': sorted(sessions, key=lambda x: x['session'])})
                report['active_session_observation'] = 'available'
        except (OSError, subprocess.TimeoutExpired):
            pass
        report['total_events'] = ledger.db.execute('SELECT count(*) FROM events').fetchone()[0]
        report['events_added'] = report['total_events'] - before
        # Publish collection health separately; errors are never silently converted to empty state.
        ledger.db.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)', ('workspace:' + str(workspace) if workspace else 'all', now(), encoded(report)))
        ledger.db.commit()
        return report
    finally:
        ledger.close()

RECEIPT_FIELDS = {'receipt_id', 'kind', 'workspace', 'task', 'role', 'attempt_id', 'at', 'provider', 'model', 'model_version', 'effort',
                  'input_tokens', 'output_tokens', 'cached_input_tokens', 'cost_usd', 'wall_seconds', 'exit_code',
                  'accepted', 'failure_category', 'verifier_id', 'verifier_version', 'evidence_sha256', 'source', 'supersedes'}

def receipt(ledger, payload):
    unknown = set(payload) - RECEIPT_FIELDS
    if unknown: raise ValueError('unknown receipt fields: ' + ', '.join(sorted(unknown)))
    for field in ('receipt_id', 'workspace', 'task', 'attempt_id', 'source', 'at'):
        if not isinstance(payload.get(field), str) or not payload[field].strip(): raise ValueError('missing ' + field)
    if payload.get('kind') not in ('usage', 'verification', 'failure'):
        raise ValueError('kind must be usage, verification or failure')
    at = datetime.fromisoformat(payload['at'].replace('Z', '+00:00'))
    if at.tzinfo is None: raise ValueError('at must include timezone')
    for field in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cost_usd', 'wall_seconds'):
        v = payload.get(field)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0):
            raise ValueError('invalid ' + field)
        if field.endswith('tokens') and v is not None and not isinstance(v, int): raise ValueError('tokens must be integers')
    if payload.get('accepted') is not None and type(payload['accepted']) is not bool: raise ValueError('accepted must be boolean')
    if payload['kind'] == 'verification':
        for field in ('verifier_id', 'verifier_version', 'evidence_sha256'):
            if not isinstance(payload.get(field), str) or not payload[field]: raise ValueError('missing ' + field)
        if type(payload.get('accepted')) is not bool: raise ValueError('verification needs accepted')
        evidence = payload['evidence_sha256']
        if len(evidence) != 64 or any(c not in '0123456789abcdef' for c in evidence): raise ValueError('evidence_sha256 must be a lowercase SHA-256')
    if payload.get('failure_category') not in (None, 'quality', 'quota', 'timeout', 'infrastructure', 'cancelled', 'protocol', 'unknown'):
        raise ValueError('invalid failure_category')
    if payload.get('exit_code') is not None and type(payload['exit_code']) is not int: raise ValueError('exit_code must be integer')
    if payload['kind'] == 'usage':
        for field in ('role', 'provider', 'model'):
            if not isinstance(payload.get(field), str) or not payload[field]: raise ValueError('missing ' + field)
    if payload.get('accepted') is True and payload.get('failure_category') is not None: raise ValueError('accepted receipt cannot also assert a failure')
    if payload['kind'] == 'failure' and not payload.get('failure_category'): raise ValueError('failure needs category')
    key = digest(['receipt', payload['workspace'], payload['task'], payload['source'], payload['receipt_id']])
    previous = ledger.db.execute('SELECT payload FROM events WHERE event_key=?', (key,)).fetchone()
    if previous and previous[0] != encoded(payload): raise ValueError('receipt ID reused with different content; submit a new superseding receipt')
    if payload.get('supersedes'):
        previous_key = digest(['receipt', payload['workspace'], payload['task'], payload['source'], payload['supersedes']])
        prior = ledger.db.execute('SELECT payload FROM events WHERE event_key=?', (previous_key,)).fetchone()
        if not prior or previous_key == key: raise ValueError('supersedes must reference an existing different receipt in this source/task')
        prior = json.loads(prior[0])
        if prior['attempt_id'] != payload['attempt_id'] or prior['kind'] != payload['kind']:
            raise ValueError('correction must keep the same attempt and kind')
    ledger.append('receipt.' + payload['kind'], payload['source'], payload, payload['workspace'], payload['task'], 'submitted_receipt_not_independently_authenticated', key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, help='override ledger directory')
    sub = parser.add_subparsers(dest='command', required=True)
    scan = sub.add_parser('collect'); scan.add_argument('--workspace', type=Path); scan.add_argument('--fast', action='store_true')
    sub.add_parser('status')
    export = sub.add_parser('export'); export.add_argument('--after', type=int, default=0)
    record = sub.add_parser('record'); record.add_argument('file', type=Path)
    args = parser.parse_args()
    data, directory, config = roots()
    directory = args.directory or directory
    if args.command == 'collect':
        result = collect(data, directory, config, args.workspace, args.fast)
        print(json.dumps(result, indent=2))
        return 1 if result.get('errors') else 0
    ledger = Ledger(directory)
    try:
        if args.command == 'record':
            settings = read(config) if config.exists() else {}
            if settings.get('telemetry', {}).get('enabled', True) is False: raise ValueError('telemetry disabled')
            with ledger.db:
                ledger.db.execute('BEGIN IMMEDIATE')
                receipt(ledger, read(args.file))
            print('Receipt recorded (idempotent).')
        elif args.command == 'export':
            for row in ledger.db.execute('SELECT * FROM events WHERE id>? ORDER BY id', (args.after,)):
                obj = dict(row); obj['payload'] = json.loads(obj['payload']); print(encoded(obj))
        else:
            health = ledger.db.execute('SELECT checked_at,report FROM health ORDER BY checked_at DESC LIMIT 1').fetchone()
            full = ledger.db.execute("SELECT checked_at,report FROM health WHERE scope='all'").fetchone()
            if full:
                full = dict(full); full['report'] = json.loads(full['report'])
            settings = read(config) if config.exists() else {}
            if health:
                health = dict(health); health['report'] = json.loads(health['report'])
            counts = dict(ledger.db.execute('SELECT kind,count(*) FROM events GROUP BY kind'))
            print(json.dumps({'ledger': str(ledger.path), 'schema_version': SCHEMA, 'counts': counts,
                              'last_collection': health if health else None, 'last_full_collection': full if full else None,
                              'enabled': settings.get('telemetry', {}).get('enabled', True),
                              'automatic_bayesian_training': False}, indent=2))
    finally:
        ledger.close()
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, sqlite3.Error) as error:
        print('telemetry: ' + str(error), file=sys.stderr)
        sys.exit(1)
