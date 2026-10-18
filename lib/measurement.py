"""Durable interactive attempts; reported completion is not verified acceptance."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from assignments import atomic, locked, now
import telemetry
import provider_adapter

VERSION = 'interactive-measurement-v1'
TERMINAL = {'completed', 'blocked', 'cancelled', 'delivery_failed'}


def read(path, default=None):
    return telemetry.read(path) if path.exists() else (default if default is not None else {})


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(tree, *args):
    return subprocess.check_output(['git', '-C', str(tree), *args], text=True,
                                   stderr=subprocess.PIPE, timeout=20,
                                   env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}).strip()


def snapshot(tree):
    try:
        return {'commit': git(tree, 'rev-parse', 'HEAD'),
                'dirty': bool(git(tree, 'status', '--porcelain', '--untracked-files=normal'))}
    except (OSError, subprocess.SubprocessError):
        return {'commit': None, 'dirty': None}


def enabled():
    return read(telemetry.roots()[2]).get('telemetry', {}).get('enabled', True)


def location(task):
    return Path(task).resolve() / 'measurement'


def role_info(task, role):
    state = read(task / 'state.json')
    if role not in state.get('roles', {}):
        raise ValueError('unknown task role')
    value = state['roles'][role]
    tree = Path(value.get('worktree') or task.parent.parent / role).resolve()
    return state, value, tree


def selected(task, role):
    state, value, _ = role_info(task, role)
    active = read(task.parent / 'agents.json').get('active', {}).get(role, {})
    provider = active.get('provider', value.get('provider'))
    policy = read(telemetry.roots()[2]).get('model_policy', {}).get(provider, {})
    return {k: active.get(k, policy.get(k) if k != 'provider' else provider)
            for k in ('provider', 'model', 'effort')}


def workspace_key(task):
    try: return str(task.parent.parent.relative_to(telemetry.roots()[0].resolve()))
    except ValueError: return read(task / 'state.json').get('workspace_id') or str(task.parent.parent)


def emit(task, attempt, event, **fields):
    record = dict(receipt_id=uuid.uuid4().hex, kind='lifecycle', at=now(),
                  workspace=workspace_key(task), task=task.name,
                  role=attempt['role'], attempt_id=attempt['id'], source=VERSION,
                  event=event, **fields)
    attempt['events'].append(record)
    return record


def prepare(task, role, criteria, verifier, max_attempts=2, timeout=120):
    task = Path(task).resolve(); directory = location(task)
    if max_attempts < 1 or timeout < 1:
        raise ValueError('attempt limit and verifier timeout must be positive')
    _, _, tree = role_info(task, role)
    verifier = Path(verifier).resolve(); text = Path(criteria).read_text().strip()
    if not text or not verifier.is_file() or verifier.is_relative_to(tree):
        raise ValueError('criteria must be nonempty and verifier must be outside the worker checkout')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(task.parent / '.review-dispatch.lock'), locked(directory / '.lock'):
        if read(task/'state.json').get('status') not in ('prepared', 'awaiting_configuration'):
            raise ValueError('freeze criteria before dispatch; use a fresh prepared task')
        if (directory / 'contract.json').exists() or read(directory / 'journal.json').get('attempts'):
            raise ValueError('measurement already started or prepared; create a fresh task to change the contract')
        base = snapshot(tree)
        if not base['commit'] or base['dirty']:
            raise ValueError('freeze requires a clean committed worker checkout')
        (directory / 'criteria.txt').write_text(text + '\n')
        (directory / 'verifier.py').write_bytes(verifier.read_bytes())
        contract = dict(schema_version=1, role=role, baseline_commit=base['commit'],
                        criteria_sha256=sha(directory / 'criteria.txt'), verifier_sha256=sha(directory / 'verifier.py'),
                        task_sha256=sha(task / 'task.md'), max_attempts=max_attempts,
                        verifier_timeout_seconds=timeout, prepared_at=now())
        atomic(directory / 'contract.json', contract)
    return contract


def preflight(task, role, capacity=False):
    task = Path(task).resolve(); directory = location(task)
    state, _, tree = role_info(task, role)
    contract = read(directory / 'contract.json'); reasons = []; warnings = []
    current = snapshot(tree)
    existing = [a for a in read(directory / 'journal.json').get('attempts', []) if a['role'] == role]
    if existing and existing[-1]['state'] not in TERMINAL:
        reasons.append('attempt already open; inspect delivery before retrying')
    if not current['commit']: warnings.append('repository snapshot unavailable')
    if current['dirty']: warnings.append('worker checkout has uncommitted changes')
    if not contract:
        warnings.append('no frozen criteria or verifier; observational capture only')
    elif contract['role'] == role:
        if not enabled(): reasons.append('telemetry is disabled for a measured task')
        for name, key in (('criteria.txt', 'criteria_sha256'), ('verifier.py', 'verifier_sha256')):
            if not (directory / name).is_file() or sha(directory / name) != contract[key]:
                reasons.append('frozen ' + name + ' changed or missing')
        if not (task / 'task.md').is_file() or sha(task / 'task.md') != contract['task_sha256']:
            reasons.append('frozen task instructions changed or missing')
        attempts = [a for a in read(directory / 'journal.json').get('attempts', []) if a['role'] == role]
        if len(attempts) >= contract['max_attempts']: reasons.append('task attempt allowance exhausted')
        if current['dirty'] is not False: reasons.append('measured dispatch requires a clean checkout')
        if not attempts and current['commit'] != contract['baseline_commit']:
            reasons.append('worker baseline changed after preparation')
        if any(r.get('kind') == 'verification' and r.get('accepted') is True
               for a in attempts for r in a['events']): reasons.append('task already independently accepted')
    if capacity:
        import budget
        choice = selected(task, role)
        try:
            config, _ = budget.configuration(); budget.check(config, choice['provider'])
        except (ValueError, KeyError) as error: reasons.append(str(error))
        requests = read(task.parent / 'agents.json').get('requests', {}).values()
        if any(r.get('slot') == role and r.get('phase') in ('requested', 'ready', 'launching', 'awaiting_ack') for r in requests):
            reasons.append('pending role handover')
    return dict(ready=not reasons, reasons=reasons, warnings=warnings, repository=current,
                measured=bool(contract and contract['role'] == role))


def start(task, role, choice=None):
    task = Path(task).resolve(); directory = location(task)
    if not enabled() and not (directory / 'contract.json').exists(): return None
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(directory / '.lock'):
        journal = read(directory / 'journal.json', {'attempts': []})
        previous = next((a for a in reversed(journal['attempts']) if a['role'] == role), None)
        if previous and previous['state'] not in TERMINAL:
            raise ValueError('attempt already open; inspect interrupted delivery before retrying')
        check = preflight(task, role)
        if not check['ready']: raise ValueError('; '.join(check['reasons']))
        _, _, tree = role_info(task, role)
        attempt = dict(id=uuid.uuid4().hex, role=role, state='prepared', events=[],
                       previous_attempt_id=previous['id'] if previous else None,
                       choice=choice or selected(task, role), baseline=snapshot(tree))
        contract = read(directory / 'contract.json')
        attempt['contract_sha256'] = telemetry.digest(contract) if contract else None
        emit(task, attempt, 'prepared', **attempt['choice'], baseline_commit=attempt['baseline']['commit'],
             repository_dirty=attempt['baseline']['dirty'], contract_sha256=attempt['contract_sha256'],
             previous_attempt_id=attempt['previous_attempt_id'])
        journal['attempts'].append(attempt); atomic(directory / 'journal.json', journal)
        return attempt['id']


def record(task, role, event, choice=None):
    task = Path(task).resolve(); directory = location(task)
    if not enabled(): return None
    if not (directory / 'journal.json').exists(): return None  # Never fabricate historical attempts.
    with locked(directory / '.lock'):
        journal = read(directory / 'journal.json')
        attempt = next((a for a in reversed(journal['attempts']) if a['role'] == role), None)
        if not attempt: return None
        if event == 'settings_observed':
            choice = choice or selected(task, role)
            choice = {k: choice.get(k) for k in ('provider', 'model', 'effort')}
            if attempt['state'] in (TERMINAL - {'blocked'}) or choice == attempt['choice']: return attempt['id']
            attempt['choice'] = choice
            emit(task, attempt, event, **choice)
        elif event == 'delivered':
            if not any(r.get('event') == 'delivered' for r in attempt['events']):
                emit(task, attempt, event)
            if attempt['state'] == 'prepared': attempt['state'] = 'delivered'
        elif event != attempt['state'] and (attempt['state'] not in TERMINAL or (attempt['state'] == 'blocked' and event in ('in_progress', 'completed'))):
            if event not in {'delivered', 'in_progress', *TERMINAL}: raise ValueError('invalid lifecycle event')
            fields = {}
            if event in ('completed', 'blocked'):
                _, value, tree = role_info(task, role)
                status = read(Path(value.get('status_json') or value.get('status_file') or task / (role + '.status.json')))
                fields['evidence_sha256'] = telemetry.digest(status)
                commits = status.get('commits', [])
                commit = commits[-1] if commits else None
                fields['output_commit'] = None
                if isinstance(commit, str) and re.fullmatch(r'[0-9a-fA-F]{7,64}', commit):
                    try: fields['output_commit'] = git(tree, 'rev-parse', '--verify', commit + '^{commit}')
                    except (OSError, subprocess.SubprocessError): pass
            emit(task, attempt, event, **fields)
            attempt['state'] = event
        atomic(directory / 'journal.json', journal)
        return attempt['id']


def sync(task, role):
    task = Path(task).resolve()
    _, value, _ = role_info(task, role)
    status = read(Path(value.get('status_json') or value.get('status_file') or task / (role + '.status.json')))
    record(task, role, 'settings_observed')
    state = status.get('state')
    if state in ('in_progress', 'blocked', 'completed'): return record(task, role, state)
    return None


def verify(task, role, commit=None):
    task = Path(task).resolve(); directory = location(task)
    contract = read(directory / 'contract.json')
    if not contract or contract['role'] != role: return {'status': 'unconfigured'}
    if not enabled(): return {'status': 'disabled'}
    with locked(directory / '.lock'):
        journal = read(directory / 'journal.json')
        attempt = next((a for a in reversed(journal.get('attempts', [])) if a['role'] == role), None)
        if not attempt or attempt['state'] != 'completed': return {'status': 'awaiting_completion'}
        if attempt.get('verification'): return attempt['verification']
        if attempt.get('verification_started_at'):
            return {'status': 'interrupted', 'reason': 'verification interrupted; not automatically retried'}
        if telemetry.digest(contract) != attempt['contract_sha256']:
            raise ValueError('frozen contract changed')
        if sha(directory / 'verifier.py') != contract['verifier_sha256'] or sha(directory / 'criteria.txt') != contract['criteria_sha256']:
            raise ValueError('frozen verification artifacts changed')
        _, value, tree = role_info(task, role)
        status = read(Path(value.get('status_json') or value.get('status_file') or task / (role + '.status.json')))
        commits = status.get('commits', [])
        commit = commit or (commits[-1] if commits else None)
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-fA-F]{7,64}', commit):
            return {'status': 'missing_commit', 'reason': 'completion needs an immutable output commit'}
        commit = git(tree, 'rev-parse', '--verify', commit + '^{commit}')
        # Do not verify unrelated history or dirty working files in place of the submitted artifact.
        subprocess.run(['git', '-C', str(tree), 'merge-base', '--is-ancestor', contract['baseline_commit'], commit], check=True, capture_output=True, timeout=20)
        attempt['verification_started_at'] = now(); atomic(directory / 'journal.json', journal)
        artifacts = directory / attempt['id']; artifacts.mkdir(exist_ok=True, mode=0o700)
        code = None; timed_out = False; seconds = None; failure = None
        try:
            with tempfile.TemporaryDirectory(prefix='aw-verify-') as temporary:
                checkout = Path(temporary) / 'checkout'
                subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--quiet', '--no-local', '--no-checkout', str(tree), str(checkout)], check=True, capture_output=True, timeout=20)
                git(checkout, '-c', 'core.hooksPath=/dev/null', 'checkout', '--quiet', '--detach', commit)
                request = dict(worktree=str(checkout), baseline_commit=contract['baseline_commit'], output_commit=commit,
                               task_id=task.name, role=role, attempt_id=attempt['id'])
                atomic(artifacts / 'request.json', request)
                code, timed_out, seconds = provider_adapter.bounded(
                    [sys.executable, str(directory / 'verifier.py'), str(artifacts / 'request.json')],
                    checkout, artifacts / 'stdout.log', artifacts / 'stderr.log', contract['verifier_timeout_seconds'])
                if sha(directory / 'verifier.py') != contract['verifier_sha256']:
                    raise ValueError('verifier changed during execution')
                if timed_out: failure = 'timeout'
                elif code not in (0, 1): failure = 'infrastructure'
        except (OSError, ValueError, subprocess.SubprocessError): failure = 'infrastructure'
        evidence = dict(output_commit=commit, verifier_sha256=contract['verifier_sha256'], exit_code=code,
                        timed_out=timed_out, wall_seconds=seconds,
                        stdout_sha256=sha(artifacts / 'stdout.log') if (artifacts / 'stdout.log').exists() else None,
                        stderr_sha256=sha(artifacts / 'stderr.log') if (artifacts / 'stderr.log').exists() else None)
        atomic(artifacts / 'verification.json', evidence)
        base = dict(receipt_id=uuid.uuid4().hex, at=now(), workspace=workspace_key(task),
                    task=task.name, role=role, attempt_id=attempt['id'], source=VERSION)
        if failure:
            receipt = dict(base, kind='failure', failure_category=failure)
            result = dict(status='error', failure_category=failure)
        else:
            receipt = dict(base, kind='verification', accepted=code == 0, verifier_id='frozen-task-verifier',
                           verifier_version=contract['verifier_sha256'], evidence_sha256=sha(artifacts / 'verification.json'),
                           wall_seconds=seconds, exit_code=code)
            result = dict(status='verified', accepted=code == 0, output_commit=commit)
        attempt['events'].append(receipt); attempt['verification'] = result
        atomic(directory / 'journal.json', journal)
        return result


def report(task):
    directory = location(task)
    journal = read(directory / 'journal.json', {'attempts': []})
    rows = []
    for attempt in journal['attempts']:
        delivered = next((r for r in attempt['events'] if r.get('event') == 'delivered'), None)
        end = next((r for r in reversed(attempt['events']) if r.get('event') in TERMINAL), None)
        elapsed = None
        if delivered and end:
            elapsed = (datetime.fromisoformat(end['at']) - datetime.fromisoformat(delivered['at'])).total_seconds()
            if elapsed < 0: elapsed = None
        rows.append(dict(attempt_id=attempt['id'], role=attempt['role'], state=attempt['state'],
                         elapsed_observed_seconds=elapsed, verification=attempt.get('verification'),
                         usage_known=False))
    return dict(attempts=rows, contract=read(directory / 'contract.json'),
                limitations=['Elapsed time includes idle and human waits; it is not model compute.',
                             'Interactive provider tokens and costs remain unknown without provider receipts.',
                             'Settings are controller observations, not provider-attested model versions.',
                             'Verifier executes trusted local code in a separate checkout, not an OS sandbox.'])


def stage(workspace, role, template, objective, seam='general', difficulty='standard'):
    """Prepare work from saved workspace metadata without requiring live agents."""
    data, _, config_path = telemetry.roots()
    roots = [p.parent for p in data.glob('*/*/workspace.json') if read(p).get('workspace_id') == workspace]
    if len(roots) != 1: raise ValueError('workspace ID must identify one saved workspace')
    root = roots[0]; config = read(config_path); manifest = read(root / 'workspace.json')
    if not isinstance(manifest.get('repository'), str) or not manifest['repository']:
        raise ValueError('saved workspace must identify its source repository')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', template): raise ValueError('invalid template ID')
    template_doc = read(config_path.parent / 'templates' / (template + '.json'))
    if not template_doc or not objective.strip(): raise ValueError('template and objective required')
    if seam not in config.get('seams', {}) or difficulty not in ('simple', 'standard', 'complex'):
        raise ValueError('invalid ownership seam or difficulty')
    active = read(root / '.coordination/agents.json').get('active', {})
    roles = {}
    for tree in sorted(root.iterdir()):
        if not tree.is_dir() or tree.name.startswith('.') or tree.name == 'integration' or not (tree / '.git').exists(): continue
        provider = active.get(tree.name, {}).get('provider')
        if provider is None:
            provider = next((p for p in config.get('providers', []) if tree.name == p or tree.name.startswith(p + '-')), None)
        if provider is None: continue
        roles[tree.name] = dict(provider=provider, worktree=str(tree.resolve()), state='dispatched' if tree.name == role else 'deferred')
    if role not in roles: raise ValueError('lead must identify an existing worker checkout')
    task = root / '.coordination' / ('prepared-' + uuid.uuid4().hex[:16])
    task.mkdir(parents=True, mode=0o700)
    for slot, value in roles.items():
        value['status_json'] = str(task / (slot + '.status.json'))
        assignment = 'lead' if slot == role else ('verifier' if any(v.get('assignment') == template_doc['assignments'].get('reviewer') for v in roles.values()) else 'reviewer')
        value['assignment'] = template_doc['assignments'][assignment]
        if template_doc['stage'] not in ('execute', 'integrate') or config.get('delivery_policy', {}).get('lead_first', True) is False:
            value['state'] = 'dispatched'
        value['report_file'] = str(task / (slot + '.status.md'))
        Path(value['report_file']).write_text('# ' + slot + ' status\n\nState: ' + value['state'] + '\n')
        atomic(Path(value['status_json']), dict(schema_version=1, task_id=task.name, role=slot,
               state=value['state'], summary='', evidence=[], commits=[], changed_files=[], blockers=[], updated_at=now()))
    shutil.copyfile(config_path.parent / 'protocol.md', task / 'PROTOCOL.md')
    shutil.copytree(config_path.parent / 'instructions', task / 'instructions')
    (task / 'task.md').write_text('# Task\n\n' + objective.strip() + '\n\n' +
        '\n'.join(slot + ': ' + value['assignment'] for slot, value in roles.items()) + '\n')
    atomic(task / 'state.json', dict(schema_version=2, id=task.name, workspace_id=workspace,
           stage=template_doc['stage'], status='prepared', lead=role, roles=roles,
           repository=manifest['repository'], set=manifest.get('set', root.name), objective=objective.strip(),
           parent_id='', issue='', pr={}, updated_at=now(),
           gates={key: False for key in ('triage_approved', 'execution_approved', 'integration_approved', 'pr_prepared', 'pr_approved')},
           transitions=[], delivery=dict(mode='quota-aware', continue_while_capacity_remains=True, repair_round=0),
           seam=seam, allowed_paths=config['seams'][seam]['paths'], routing={'difficulty':difficulty},
           template_id=template, template_version=template_doc['schema_version'], profile=manifest.get('profile', 'trusted'), created_at=now()))
    return {'status': 'prepared', 'task': str(task), 'roles': list(roles)}


def release(task, role):
    task = Path(task).resolve()
    with locked(task.parent / '.review-dispatch.lock'), locked(task / '.state.lock'):
        state = read(task / 'state.json')
        if state.get('status') != 'prepared': raise ValueError('only prepared tasks may be released')
        if not read(location(task) / 'contract.json'): raise ValueError('prepare a measurement contract first')
        contract = read(location(task) / 'contract.json')
        if contract['role'] != role: raise ValueError('release must name the measured role')
        for path in task.parent.glob('*/state.json'):
            other = read(path)
            if path.parent == task or other.get('status') not in ('active', 'blocked', 'awaiting_configuration', 'dispatching'): continue
            ours, theirs = state.get('seam', 'general'), other.get('seam', 'general')
            if ours in ('general', 'shared') or theirs in ('general', 'shared') or ours == theirs:
                return dict(ready=False, reasons=['ownership conflict with task ' + path.parent.name])
        check = preflight(task, role, capacity=True)
        if not check['ready']: return check
        state['status'] = 'awaiting_configuration'
        state.setdefault('transitions', []).append(dict(at=now(), actor='measurement:release',
                                                        **{'from': 'prepared', 'to': 'awaiting_configuration'}))
        atomic(task / 'state.json', state)
    return dict(ready=True, status='awaiting_configuration')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('prepare', 'preflight', 'start', 'record', 'sync', 'verify', 'status', 'release'):
        p = sub.add_parser(command); p.add_argument('task', type=Path)
        if command != 'status': p.add_argument('--role', required=True)
        if command == 'prepare':
            p.add_argument('--criteria', type=Path, required=True); p.add_argument('--verifier', type=Path, required=True)
            p.add_argument('--max-attempts', type=int, default=2); p.add_argument('--timeout', type=int, default=120)
        if command == 'record': p.add_argument('--event', required=True)
        if command == 'verify': p.add_argument('--commit')
    p = sub.add_parser('stage'); p.add_argument('workspace'); p.add_argument('--role', required=True)
    p.add_argument('--template', required=True); p.add_argument('--objective', required=True)
    p.add_argument('--seam', default='general'); p.add_argument('--difficulty', default='standard')
    args = parser.parse_args(); kw = vars(args).copy(); command = kw.pop('command')
    if command == 'status': result = report(**kw)
    elif command == 'preflight': result = preflight(**kw, capacity=True)
    else: result = globals()[command](**kw)
    if command != 'verify' or result.get('status') != 'unconfigured':
        print(json.dumps(result, indent=2))
    return 1 if command in ('preflight', 'release') and not result['ready'] else 0


if __name__ == '__main__':
    try: sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('measurement: ' + str(error), file=sys.stderr); sys.exit(2)
