"""Reproducible, opt-in agent workflow experiments with an outcome ledger."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

SCHEMA = 1
TOPOLOGIES = {
    'solo': ['implementer'],
    'review': ['implementer', 'reviewer'],
    'parallel': ['implementer-a', 'implementer-b', 'verifier'],
    'investigate': ['investigator-a', 'investigator-b', 'adjudicator'],
}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args):
    return subprocess.check_output(['git', *map(str, args)], stderr=subprocess.PIPE, text=True).strip()


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{name} must be a positive integer')


def validate(manifest):
    """Require explicit experimental factors and distinct development/test tasks."""
    if not isinstance(manifest, dict) or manifest.get('schema_version') != SCHEMA:
        raise ValueError('Unsupported benchmark schema')
    positive(manifest.get('repeats'), 'repeats')
    positive(manifest.get('timeout_seconds'), 'timeout_seconds')
    if isinstance(manifest.get('seed'), bool) or not isinstance(manifest.get('seed'), int):
        raise ValueError('seed must be an integer')
    for section in ('tasks', 'configurations'):
        rows = manifest.get(section)
        if not isinstance(rows, list) or not rows:
            raise ValueError(f'{section} must not be empty')
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f'{section} entries must be objects')
            identifier = row.get('id', '')
            if not isinstance(identifier, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', identifier) or identifier in seen:
                raise ValueError(f'Invalid or duplicate {section} ID')
            seen.add(identifier)
    for task in manifest['tasks']:
        if task.get('split') not in ('development', 'heldout') or not isinstance(task.get('objective'), str) or not task['objective'].strip():
            raise ValueError('Each task needs a split and objective')
    for config in manifest['configurations']:
        topology = config.get('topology')
        if topology not in TOPOLOGIES:
            raise ValueError('Unknown topology')
        roles = config.get('roles', {})
        required = set(TOPOLOGIES[topology]) | {'coordinator'}
        if not isinstance(roles, dict) or set(roles) != required:
            raise ValueError(f'{topology} requires roles: {sorted(required)}')
        for role, settings in roles.items():
            if not isinstance(settings, dict) or any(not isinstance(settings.get(k), str) or not settings[k] for k in ('provider', 'model', 'effort')):
                raise ValueError(f'{role} needs explicit provider/model/effort')
    return manifest


def database(root):
    connection = sqlite3.connect(root / 'ledger.sqlite', timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize(source, root):
    """Freeze inputs and repository objects before creating a balanced trial plan."""
    source, root = Path(source).resolve(), Path(root).resolve()
    manifest = validate(json.loads(source.read_text()))
    repository = (source.parent / manifest['repository']).resolve()
    if root == repository or repository in root.parents:
        raise ValueError('Experiment directory must be outside the source repository')
    revision = git('-C', repository, 'rev-parse', '--verify', manifest.get('revision', 'HEAD') + '^{commit}')
    scripts = {key: (source.parent / manifest[key]).resolve() for key in ('adapter', 'verifier')}
    if not all(p.is_file() for p in scripts.values()):
        raise ValueError('Adapter and verifier must be Python script files')
    root.mkdir(parents=True, exist_ok=False)
    try:
        git('clone', '--bare', '--no-hardlinks', repository, root / 'repository.git')
        git('--git-dir', root / 'repository.git', 'cat-file', '-e', revision)
        for key, path in scripts.items():
            shutil.copyfile(path, root / (key + '.py'))
            manifest[key] = key + '.py'
        shutil.copyfile(Path(__file__), root / 'harness.py')
        manifest['harness_sha256'] = digest(root / 'harness.py')
        manifest.update(repository=str(repository), revision=revision, created_at=stamp())
        manifest['environment'] = {'python': sys.version, 'platform': platform.platform(), 'git': git('--version')}
        manifest['hashes'] = {key: digest(root / (key + '.py')) for key in scripts}
        write(root / 'manifest.json', manifest)
        db = database(root)
        with db:
            db.executescript('''
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE trials (id TEXT PRIMARY KEY, ordinal INTEGER UNIQUE, task TEXT,
                    configuration TEXT, repetition INTEGER, seed INTEGER, status TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT, result TEXT);
                CREATE TABLE events (id INTEGER PRIMARY KEY, trial TEXT NOT NULL, at TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TRIGGER immutable_events_update BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'events are append-only'); END;
                CREATE TRIGGER immutable_events_delete BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'events are append-only'); END;
            ''')
            db.execute('INSERT INTO metadata VALUES (?, ?)', ('manifest_sha256', digest(root / 'manifest.json')))
            rng = random.Random(manifest['seed'])
            trials = []
            for task in manifest['tasks']:
                for repetition in range(manifest['repeats']):
                    seed = rng.randrange(2**31)
                    for config in manifest['configurations']:
                        trials.append((task['id'], config['id'], repetition, seed))
            rng.shuffle(trials)
            for number, trial in enumerate(trials, 1):
                db.execute('INSERT INTO trials VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)',
                           (f't{number:05}', number, *trial, 'planned'))
        db.close()
    except BaseException:
        shutil.rmtree(root)
        raise
    return {'experiment': str(root), 'revision': revision, 'trials': len(trials)}


def load(root):
    db = database(root)
    expected = db.execute("SELECT value FROM metadata WHERE key='manifest_sha256'").fetchone()[0]
    db.close()
    if digest(root / 'manifest.json') != expected:
        raise ValueError('Frozen experiment manifest changed; create a new experiment')
    manifest = validate(json.loads((root / 'manifest.json').read_text()))
    for key in ('adapter', 'verifier'):
        if digest(root / manifest[key]) != manifest['hashes'][key]:
            raise ValueError(f'Frozen {key} changed; create a new experiment')
    return manifest


def execute(command, cwd, logfile, timeout):
    """Bound a subprocess and its process group, including timeout descendants."""
    with logfile.open('wb') as log:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('AW_') and key not in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID')}
        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, env=env)
        try:
            return process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            return None, True
        finally:
            # Adapters must not leave background provider turns running.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def usage(path, config):
    """Validate adapter telemetry without treating missing costs as free work."""
    result = json.loads(path.read_text())
    rows = result.get('roles')
    if not isinstance(rows, dict) or set(rows) != set(config['roles']):
        raise ValueError('Adapter telemetry must account for every configured role')
    for role, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError('Invalid role telemetry')
        if any(row.get(key) != config['roles'][role][key] for key in ('provider', 'model', 'effort')):
            raise ValueError(f'{role} did not report the configured provider/model/effort')
        for name in ('input_tokens', 'output_tokens', 'cost_usd'):
            value = row.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                      not math.isfinite(value) or value < 0 or
                                      (name.endswith('tokens') and not isinstance(value, int))):
                raise ValueError(f'Invalid {role}.{name}')
    for name in ('recovery_seconds', 'human_seconds', 'coordination_seconds'):
        value = result.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                  not math.isfinite(value) or value < 0):
            raise ValueError(f'Invalid {name}')
    for name in ('handover_failures', 'retries'):
        value = result.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f'Invalid {name}')
    total = {name: sum(row[name] for row in rows.values()) if all(row.get(name) is not None for row in rows.values()) else None
             for name in ('input_tokens', 'output_tokens', 'cost_usd')}
    return {'roles': rows, **total, **{key: result.get(key) for key in
            ('recovery_seconds', 'human_seconds', 'coordination_seconds', 'handover_failures', 'retries')}}


def run_trial(root, trial_id=None, split='development'):
    """Claim one trial once, execute in a private clone, and record every outcome."""
    manifest = load(root)
    if digest(Path(__file__)) != manifest.get('harness_sha256'):
        raise ValueError('Harness version changed; use the experiment harness.py or create a new experiment')
    db = database(root)
    db.execute('BEGIN IMMEDIATE')
    rows = db.execute('SELECT * FROM trials WHERE status=? ORDER BY ordinal', ('planned',)).fetchall()
    tasks = {t['id']: t for t in manifest['tasks']}
    row = next((r for r in rows if tasks[r['task']]['split'] == split and (trial_id is None or r['id'] == trial_id)), None)
    if row is None:
        db.rollback(); db.close()
        if trial_id: raise ValueError('Trial unavailable or belongs to a different split')
        return None
    identifier = row['id']
    db.execute("UPDATE trials SET status='running', started_at=? WHERE id=?", (stamp(), identifier))
    db.execute('INSERT INTO events(trial,at,kind,payload) VALUES (?,?,?,?)', (identifier, stamp(), 'started', '{}'))
    db.commit()
    directory = root / 'trials' / identifier
    directory.mkdir(parents=True)
    worktree = directory / 'worktree'
    config = next(c for c in manifest['configurations'] if c['id'] == row['configuration'])
    started = time.monotonic()
    outcome = {'accepted': False, 'failure': None, 'cost_usd': None}
    try:
        git('clone', '--no-hardlinks', root / 'repository.git', worktree)
        git('-C', worktree, 'checkout', '--detach', manifest['revision'])
        # No live workspace identifiers or resumable provider sessions are supplied.
        request = {'schema_version': SCHEMA, 'trial_id': identifier, 'task': tasks[row['task']],
                   'configuration': config, 'seed': row['seed'], 'repetition': row['repetition'],
                   'baseline': manifest['revision'], 'worktree': str(worktree),
                   'artifacts': str(directory), 'timeout_seconds': manifest['timeout_seconds']}
        write(directory / 'request.json', request)
        outcome['setup_seconds'] = time.monotonic() - started
        command = [sys.executable, str(root / manifest['adapter']), str(directory / 'request.json'), str(directory / 'usage.json')]
        before = time.monotonic()
        code, timed_out = execute(command, worktree, directory / 'adapter.log', manifest['timeout_seconds'])
        outcome.update(adapter_seconds=time.monotonic()-before, adapter_exit=code, timed_out=timed_out)
        try:
            outcome.update(usage(directory / 'usage.json', config))
        except (OSError, ValueError, TypeError, AttributeError) as error:
            outcome['telemetry_error'] = str(error)
        if timed_out or code != 0:
            outcome['failure'] = 'adapter_timeout' if timed_out else 'adapter_failed'
        else:
            # The verifier gets the original task, not adapter-edited metadata.
            write(directory / 'request.json', request)
            before = time.monotonic()
            code, timed_out = execute([sys.executable, str(root / manifest['verifier']), str(directory / 'request.json')],
                                      worktree, directory / 'verifier.log', manifest['timeout_seconds'])
            outcome['verifier_seconds'] = time.monotonic() - before
            outcome.update(verifier_exit=code, verifier_timed_out=timed_out)
            outcome['verified'] = code == 0 and not timed_out
            outcome['accepted'] = outcome['verified'] and 'telemetry_error' not in outcome
            if not outcome['verified']:
                outcome['failure'] = 'verifier_timeout' if timed_out else 'verification_failed'
            elif 'telemetry_error' in outcome:
                outcome['failure'] = 'invalid_adapter_telemetry'
        (directory / 'changes.patch').write_text(git('-C', worktree, 'diff', manifest['revision']))
        (directory / 'git-status.txt').write_text(git('-C', worktree, 'status', '--porcelain'))
        outcome['head'] = git('-C', worktree, 'rev-parse', 'HEAD')
    except (Exception, KeyboardInterrupt) as error:
        outcome.update(accepted=False, failure='interrupted' if isinstance(error, KeyboardInterrupt) else 'harness_error', error=str(error))
    finally:
        outcome['wall_seconds'] = time.monotonic() - started
        write(directory / 'outcome.json', outcome)
        encoded = json.dumps(outcome)
        with db:
            db.execute("UPDATE trials SET status='finished', finished_at=?, result=? WHERE id=?", (stamp(), encoded, identifier))
            db.execute('INSERT INTO events(trial,at,kind,payload) VALUES (?,?,?,?)', (identifier, stamp(), 'finished', encoded))
        db.close()
    return {'trial': identifier, **outcome}


def interval(success, count):
    """Wilson interval; repeated tasks are not independent population samples."""
    if not count: return None
    z = 1.96
    p = success/count
    center = (p+z*z/(2*count))/(1+z*z/count)
    radius = z*math.sqrt(p*(1-p)/count+z*z/(4*count*count))/(1+z*z/count)
    return [max(0, center-radius), min(1, center+radius)]


def report(root, split):
    manifest = load(root)
    task_ids = {t['id'] for t in manifest['tasks'] if t['split'] == split}
    db = database(root)
    rows = [dict(r) for r in db.execute('SELECT * FROM trials ORDER BY ordinal') if r['task'] in task_ids]
    db.close()
    summaries, matched = [], defaultdict(dict)
    for config in manifest['configurations']:
        group = [r for r in rows if r['configuration'] == config['id']]
        done = [r for r in group if r['status'] == 'finished']
        outcomes = [json.loads(r['result']) for r in done]
        success = sum(bool(o['accepted']) for o in outcomes)
        costs_known = bool(outcomes) and all(o.get('cost_usd') is not None for o in outcomes)
        total_cost = sum(o['cost_usd'] for o in outcomes) if costs_known else None
        summaries.append({'configuration': config['id'], 'planned': len(group), 'finished': len(done),
            'complete': bool(group) and len(done) == len(group),
            'running': sum(r['status']=='running' for r in group), 'accepted': success,
            'acceptance_rate': success/len(done) if done else None,
            'descriptive_wilson_95': interval(success,len(done)),
            'mean_wall_seconds': sum(o['wall_seconds'] for o in outcomes)/len(done) if done else None,
            'total_cost_usd': total_cost, 'cost_per_accepted_usd': total_cost/success if costs_known and success else None,
            'unknown_cost_trials': sum(o.get('cost_usd') is None for o in outcomes),
            'telemetry_error_trials': sum('telemetry_error' in o for o in outcomes),
            'operational_totals': {key: sum(o[key] for o in outcomes) if outcomes and all(o.get(key) is not None for o in outcomes) else None
                for key in ('coordination_seconds', 'recovery_seconds', 'human_seconds', 'handover_failures', 'retries')},
            'failures': dict((key, sum(o.get('failure') == key for o in outcomes)) for key in sorted({o['failure'] for o in outcomes if o.get('failure')}))})
        for r, o in zip(done, outcomes):
            matched[(r['task'],r['repetition'])][config['id']] = o
    comparisons = []
    ids = [c['id'] for c in manifest['configurations']]
    for i, left in enumerate(ids):
        for right in ids[i+1:]:
            pairs = [(g[left],g[right]) for g in matched.values() if left in g and right in g]
            comparisons.append({'left': left, 'right': right, 'matched_trials': len(pairs),
                'left_only_accepted': sum(a['accepted'] and not b['accepted'] for a,b in pairs),
                'right_only_accepted': sum(b['accepted'] and not a['accepted'] for a,b in pairs),
                'mean_wall_delta_left_minus_right': sum(a['wall_seconds']-b['wall_seconds'] for a,b in pairs)/len(pairs) if pairs else None})
    return {'split': split, 'revision': manifest['revision'], 'configurations': summaries, 'paired_comparisons': comparisons,
            'limitations': 'Descriptive results only; repeated tasks are correlated. No winner or production policy is inferred. Unknown cost is not zero.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    demo = sub.add_parser('demo'); demo.add_argument('directory')
    init = sub.add_parser('init'); init.add_argument('manifest'); init.add_argument('experiment')
    for name in ('run', 'report'):
        command = sub.add_parser(name); command.add_argument('experiment', type=Path)
        command.add_argument('--split', choices=['development','heldout'], default='development')
        if name == 'run':
            command.add_argument('--trial'); command.add_argument('--all', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'demo':
            subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'examples/benchmark/create-demo.py'), args.directory], check=True)
            return
        if args.action == 'init': result = initialize(args.manifest, args.experiment)
        elif args.action == 'report': result = report(args.experiment.resolve(), args.split)
        else:
            result = []
            while True:
                outcome = run_trial(args.experiment.resolve(), args.trial, args.split)
                if outcome is None: break
                result.append(outcome)
                if not args.all or args.trial or outcome.get('failure') == 'interrupted': break
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        parser.exit(1, f'agent-workspaces benchmark: {error}\n')


if __name__ == '__main__':
    main()
