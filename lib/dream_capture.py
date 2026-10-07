"""Frozen, isolated measured discovery attempts for Dream replay."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

import dream
import provider_adapter as adapter
import telemetry

VERSION = 'dream-capture-v1'
ENGINE = ('dream.py', 'dream_capture.py', 'provider_adapter.py', 'telemetry.py')


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def read(path):
    return json.loads(Path(path).read_text())


def git(path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME='Dream capture', GIT_AUTHOR_EMAIL='dream@localhost',
               GIT_COMMITTER_NAME='Dream capture', GIT_COMMITTER_EMAIL='dream@localhost')
    return subprocess.check_output(['git', '-c', 'core.hooksPath=' + os.devnull,
        '-c', 'protocol.file.allow=always', '-C', str(path), *args],
        env=env, text=True, stderr=subprocess.PIPE, timeout=60).strip()


def clone(source, target, commit):
    git(target.parent, 'clone', '--no-hardlinks', '--no-checkout', '--', str(source), str(target))
    git(target, 'checkout', '--detach', commit)
    # No remote is needed for a recorded attempt.
    git(target, 'remote', 'remove', 'origin')


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise ValueError('IDs must be short alphanumeric names with dash/underscore')
    return value


def load(directory):
    directory = Path(directory).resolve()
    manifest = read(directory / 'capture.json')
    if adapter.sha(directory / 'capture.json') != (directory / 'capture.sha256').read_text().strip():
        raise ValueError('frozen capture manifest changed')
    for name, expected in manifest['engine'].items():
        if adapter.sha(Path(__file__).parent / name) != expected:
            raise ValueError('capture engine changed; use the dataset engine/dream.py')
    for world in manifest['worlds']:
        base = directory / 'worlds' / safe_id(world['id'])
        for name, expected in world['files'].items():
            if adapter.sha(base / name) != expected:
                raise ValueError('frozen input changed: ' + name)
        if git(base / 'root', 'rev-parse', 'HEAD') != world['root_snapshot']:
            raise ValueError('root snapshot changed')
    return directory, manifest


def verify(worktree, directory, verifier, timeout, request):
    directory.mkdir(mode=0o700)
    frozen_hash = adapter.sha(verifier)
    write(directory / 'request.json', dict(request, worktree=str(worktree), artifacts=str(directory)))
    code, timed, wall = adapter.bounded([sys.executable, str(verifier), str(directory / 'request.json')],
        worktree, directory / 'stdout.log', directory / 'stderr.log', timeout)
    changed = adapter.sha(verifier) != frozen_hash
    result = {'exit_code': code, 'timed_out': timed, 'wall_seconds': wall,
              'verifier_sha256': frozen_hash, 'verifier_changed': changed,
              'stdout_sha256': adapter.sha(directory / 'stdout.log'),
              'stderr_sha256': adapter.sha(directory / 'stderr.log'),
              'accepted': code == 0 and not timed and not changed,
              'valid': code in (0, 1) and not timed and not changed}
    write(directory / 'result.json', result)
    return result


def initialize(spec_file, directory):
    spec_file = Path(spec_file).resolve(); directory = Path(directory).resolve()
    spec = dream.object_value(read(spec_file), 'capture spec')
    dream.array_value(spec['worlds'], 'worlds')
    for world in spec['worlds']: dream.object_value(world, 'world')
    dream.object_value(spec['agent'], 'agent')
    # Reuse replay validation before any verifier execution, with placeholder nodes.
    trial = dict(spec)
    trial['worlds'] = [dict(w, root_score=0, root_snapshot='pending', nodes=[{
        'id':'check', 'parent':'root', 'score':0, 'wall_seconds':0, 'cost_usd':0,
        'snapshot':'pending', 'attempt_id':'check', 'configuration':'pending',
        'verifier_version':'pending', 'evidence_sha256':'0'*64}]) for w in spec['worlds']]
    dream.validate(trial)
    dream.positive(spec['timeout_seconds'], 'timeout_seconds')
    dream.positive(spec['max_attempts_per_world'], 'max_attempts_per_world')
    adapter.command(spec['agent'], directory, False)
    prepared = []
    for world in spec['worlds']:
        safe_id(world['id'])
        paths = {key: (spec_file.parent / world[key]).resolve() for key in ('repository', 'prompt_file', 'verifier')}
        if directory.is_relative_to(paths['repository']):
            raise ValueError('capture directory must be outside source repositories')
        if git(paths['repository'], 'status', '--porcelain'):
            raise ValueError('source repository must be clean; commit the intended baseline first')
        commit = git(paths['repository'], 'rev-parse', '--verify', world.get('ref', 'HEAD') + '^{commit}')
        for name in ('prompt_file', 'verifier'):
            if not paths[name].is_file(): raise ValueError('missing ' + name)
        if git(paths['repository'], 'ls-tree', '-r', commit).find('160000 commit ') >= 0:
            raise ValueError('submodules are not supported by frozen capture')
        prepared.append((world, paths, commit))
    directory.mkdir(mode=0o700)  # Refuse reuse, even for an incomplete initialization.
    (directory / 'worlds').mkdir(mode=0o700)
    (directory / 'engine').mkdir(mode=0o700)
    engine = {}
    for name in ENGINE:
        shutil.copyfile(Path(__file__).parent / name, directory / 'engine' / name)
        engine[name] = adapter.sha(directory / 'engine' / name)
    result = dict(spec, capture_version=VERSION, dataset_id=uuid.uuid4().hex, engine=engine, worlds=[])
    for world, paths, commit in prepared:
        base = directory / 'worlds' / world['id']; base.mkdir(mode=0o700)
        (base / 'attempts').mkdir(mode=0o700)
        for source, target in [('prompt_file', 'prompt.txt'), ('verifier', 'verifier.py')]:
            shutil.copyfile(paths[source], base / target); os.chmod(base / target, 0o600)
        clone(paths['repository'], base / 'root', commit)
        clone(base / 'root', base / 'root-check', commit)
        check = verify(base / 'root-check', base / 'root-verification', base / 'verifier.py',
                       spec['timeout_seconds'], {'schema_version':1, 'task':{'id':world['id']}, 'attempt_id':'root'})
        if not check['valid']:
            raise ValueError('baseline verifier failed operationally; inspect root-verification')
        result['worlds'].append({'id':world['id'], 'instance_id':world['instance_id'], 'split':world['split'],
            'root_snapshot':commit, 'root_score':float(check['accepted']),
            'files':{name:adapter.sha(base / name) for name in ('prompt.txt','verifier.py','root-verification/result.json')}})
    write(directory / 'capture.json', result)
    (directory / 'capture.sha256').write_text(adapter.sha(directory / 'capture.json') + '\n')
    return {'directory':str(directory), 'dataset_id':result['dataset_id'], 'worlds':len(result['worlds'])}


@contextmanager
def locked(base):
    with (base / '.capture.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('another capture operation owns this world')
        yield


def nodes(base):
    values = []
    for attempt in sorted((base / 'attempts').iterdir()):
        if not attempt.is_dir(): continue
        if not (attempt / 'node.json').is_file():
            raise ValueError('incomplete attempt retained at ' + str(attempt) + '; export is blocked')
        node = read(attempt / 'node.json')
        if adapter.sha(attempt / 'evidence.json') != node['evidence_sha256']:
            raise ValueError('attempt evidence changed')
        evidence = read(attempt / 'evidence.json')
        if evidence['node'] != {k:v for k,v in node.items() if k != 'evidence_sha256'}:
            raise ValueError('node metadata changed')
        for name, expected in evidence['files'].items():
            if adapter.sha(attempt / name) != expected: raise ValueError('attempt artifact changed: ' + name)
        # Check that all reachable objects, including result trees, remain available.
        git(attempt / 'worktree', 'fsck', '--full', '--no-reflogs')
        if git(attempt / 'worktree', 'rev-parse', 'refs/heads/dream-snapshot') != node['snapshot']:
            raise ValueError('result snapshot changed')
        values.append(node)
    return values


def ingest(directory):
    directory, manifest = load(directory)
    if manifest['data_kind'] != 'real': return {'recorded':0, 'reason':'synthetic_data'}
    _, ledger_dir, config = telemetry.roots()
    if config.exists() and read(config).get('telemetry', {}).get('enabled', True) is False:
        return {'recorded':0, 'reason':'telemetry_disabled'}
    records = []
    for world in manifest['worlds']:
        base = directory / 'worlds' / world['id']
        with locked(base):
            for node in nodes(base):
                records.extend(read(base / 'attempts' / node['id'] / 'receipts.json'))
    ledger = telemetry.Ledger(ledger_dir)
    try:
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            before = ledger.db.total_changes
            for record in records: telemetry.receipt(ledger, record)
            return {'recorded':ledger.db.total_changes - before}
    finally: ledger.close()


def attempt(directory, world_id, parent):
    directory, manifest = load(directory)
    world = next((w for w in manifest['worlds'] if w['id'] == world_id), None)
    if world is None: raise ValueError('unknown world')
    base = directory / 'worlds' / safe_id(world_id)
    with locked(base):
        history = nodes(base)
        if len(history) >= manifest['max_attempts_per_world']:
            raise ValueError('frozen attempt budget exhausted')
        prior = next((n for n in history if n['id'] == parent), None)
        if parent != 'root' and prior is None: raise ValueError('unknown parent')
        if parent != 'root' and any(n['parent'] == parent for n in history):
            raise ValueError('non-root parent already has a continuation')
        source = base / 'root' if parent == 'root' else base / 'attempts' / parent / 'worktree'
        commit = world['root_snapshot'] if prior is None else prior['snapshot']
        identifier = f'{len(history)+1:06d}'
        output = base / 'attempts' / identifier; output.mkdir(mode=0o700)
        write(output / 'started.json', {'id':identifier,'parent':parent,'snapshot':commit,'at':adapter.stamp()})
        clone(source, output / 'worktree', commit)
        prompt = (base / 'prompt.txt').read_text()
        prompt += '\nWork only in this checkout. Do not delegate, commit, or access sibling directories.'
        started = time.monotonic()
        receipt = adapter.run_role(manifest['agent'], output / 'worktree', output / 'provider', prompt, manifest['timeout_seconds'])
        wall = time.monotonic() - started
        # Freeze the candidate BEFORE running the independent verifier on another clone.
        git(output / 'worktree', 'add', '-A')
        tree = git(output / 'worktree', 'write-tree')
        snapshot = git(output / 'worktree', 'commit-tree', tree, '-p', commit, '-m', 'Measured candidate ' + identifier)
        git(output / 'worktree', 'update-ref', 'refs/heads/dream-snapshot', snapshot)
        check = None
        failure = {'timeout':'timeout','cancelled':'cancelled','protocol_or_launch_error':'protocol'}.get(receipt['status'], 'unknown')
        if receipt['status'] == 'completed':
            clone(output / 'worktree', output / 'verification-worktree', snapshot)
            check = verify(output / 'verification-worktree', output / 'verification', base / 'verifier.py',
                manifest['timeout_seconds'], {'schema_version':1,'task':{'id':world_id},'attempt_id':identifier})
            wall += check['wall_seconds']
            failure = (None if check['accepted'] else 'quality') if check['valid'] else ('timeout' if check['timed_out'] else 'infrastructure')
        accepted = failure is None
        at = adapter.stamp()
        common = {'workspace':'dream:' + manifest['dataset_id'], 'task':world_id, 'attempt_id':identifier,
                  'source':VERSION, 'at':at}
        usage = {k:receipt.get(k) for k in ('input_tokens','output_tokens','cached_input_tokens','cost_usd','model_version')}
        records = [dict(common, receipt_id=identifier+'-usage', kind='usage', role='discovery',
                        provider=manifest['agent']['provider'], model=manifest['agent']['model'],
                        effort=manifest['agent']['effort'], wall_seconds=wall, **usage)]
        if check and check['valid']:
            records.append(dict(common, receipt_id=identifier+'-verification', kind='verification', accepted=accepted,
                verifier_id='binary-acceptance', verifier_version=world['files']['verifier.py'],
                evidence_sha256=adapter.sha(output / 'verification/result.json')))
        if failure:
            records.append(dict(common, receipt_id=identifier+'-failure', kind='failure', failure_category=failure))
        write(output / 'receipts.json', records)
        node = {'id':identifier,'parent':parent,'parent_snapshot':commit,'snapshot':snapshot,
                'attempt_id':identifier,'configuration':telemetry.digest({'requested':manifest['agent'],
                    'model_version':receipt.get('model_version'),'cli_version':receipt.get('cli_version')}),
                'requested_agent':manifest['agent'], 'observed_model_version':receipt.get('model_version'),
                'cli_version':receipt.get('cli_version'), 'verifier_version':world['files']['verifier.py'],
                'score':float(accepted), 'accepted':accepted, 'failure_category':failure,
                'wall_seconds':wall, 'cost_usd':receipt.get('cost_usd')}
        files = {str(p.relative_to(output)):adapter.sha(p) for p in output.rglob('*')
                 if p.is_file() and p.relative_to(output).parts[0] in ('provider','verification','receipts.json','started.json')}
        write(output / 'evidence.json', {'node':node,'files':files})
        write(output / 'node.json', dict(node, evidence_sha256=adapter.sha(output / 'evidence.json')))
    try: ingestion = ingest(directory)
    except (OSError, ValueError, sqlite3.Error) as error:
        ingestion = {'recorded':0,'error':str(error),'retry':'dream ingest DIRECTORY'}
    return {'attempt':identifier,'accepted':accepted,'failure_category':failure,'ingestion':ingestion}


def export(directory):
    directory, manifest = load(directory)
    result = {key:manifest[key] for key in ('schema_version','data_kind','evaluation_protocol','objective','incumbent','policies')}
    result['capture_dataset_id'] = manifest['dataset_id']; result['worlds'] = []
    for world in manifest['worlds']:
        base = directory / 'worlds' / world['id']
        with locked(base):
            history = nodes(base)
            for node in history:
                expected = world['root_snapshot'] if node['parent'] == 'root' else next(n['snapshot'] for n in history if n['id'] == node['parent'])
                if expected != node['parent_snapshot']: raise ValueError('parent snapshot mismatch')
            result['worlds'].append({**{k:world[k] for k in ('id','instance_id','split','root_score','root_snapshot')},'nodes':history})
    dream.validate(result)
    return result


def status(directory):
    directory, manifest = load(directory)
    worlds = []
    for world in manifest['worlds']:
        base = directory / 'worlds' / world['id']
        with locked(base):
            entries = sorted((base / 'attempts').iterdir())
            pending = [p.name for p in entries if p.is_dir() and not (p / 'node.json').exists()]
            history = nodes(base) if not pending else []
            observed = {'root':{'score':world['root_score'], 'depth':0}}
            for node in history:
                observed[node['id']] = dict(node, depth=observed[node['parent']]['depth'] + 1)
            policy = next(p for p in manifest['policies'] if p['id'] == manifest['incumbent'])
            worlds.append({'id':world['id'], 'split':world['split'], 'completed_attempts':len(entries)-len(pending),
                'incomplete_attempts':pending, 'remaining_budget':max(0,manifest['max_attempts_per_world']-len(entries)),
                'suggested_parents':dream.choose(observed,set(),policy) if not pending and len(entries)<manifest['max_attempts_per_world'] else []})
    return {'dataset_id':manifest['dataset_id'], 'data_kind':manifest['data_kind'], 'worlds':worlds,
            'automatic_live_promotion':False}
