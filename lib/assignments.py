"""Persistent workspace agent choices and checkpointed ownership handovers."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from models import validate
from capacity import TERMINAL

PENDING = {'requested', 'ready', 'launching', 'awaiting_ack'}


class ChangeError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        if default is not None:
            return default
        raise ChangeError(f'Missing file: {path}')


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=10).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ChangeError(f'Command failed: {shlex.join(args[:2])}') from error


def root_for(workspace):
    base = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'agent-workspaces'
    for manifest in base.glob('*/*/workspace.json'):
        if read(manifest).get('workspace_id') == workspace:
            return manifest.parent
    raise ChangeError(f'Unknown workspace: {workspace}')


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


class Assignments:
    def __init__(self, root, cli):
        self.root = root.resolve()
        self.workspace = read(root / 'workspace.json')['workspace_id']
        self.file = root / '.coordination/agents.json'
        self.cli = cli
        self.config_root = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'agent-workspaces'

    def load(self):
        return read(self.file, {'schema_version': 1, 'active': {}, 'requests': {}, 'default_lead': None})

    def save(self, state):
        atomic(self.file, state)
        try:
            from telemetry import collect
            report = collect(workspace=self.root, fast=True)
            if report.get('errors'):
                print('agent-workspaces: telemetry collection incomplete; inspect telemetry status', file=sys.stderr)
        except (OSError, ValueError, sqlite3.Error):
            print('agent-workspaces: telemetry unavailable; assignment saved', file=sys.stderr)

    def task(self, task):
        path = Path(task).resolve()
        if path.parent != self.root / '.coordination':
            raise ChangeError('Task must belong to this workspace')
        return path, read(path / 'state.json')

    def sessions(self, slot):
        rows = run(['tmux', 'list-sessions', '-F', '#{session_name}\t#{@aw_role}\t#{@aw_workspace_id}\t#{@aw_provider}'])
        result = []
        for row in rows.splitlines():
            fields = row.split('\t')
            if len(fields) == 4 and fields[1:3] == [slot, self.workspace]:
                result.append((fields[0], fields[3] or slot))
        if len(result) != 1:
            raise ChangeError(f'Expected one managed session for {slot}; open the workspace first')
        return result[0]

    def active_tasks(self, slot):
        result = []
        for path in (self.root / '.coordination').glob('*/state.json'):
            state = read(path)
            if state.get('status') not in TERMINAL and (slot == 'orchestrator' or slot in state.get('roles', {})):
                result.append(path.parent)
        return result

    def select(self, args):
        policy = read(self.config_root / 'config.json')
        if args.provider not in policy.get('providers', []):
            raise ChangeError('Provider is not configured')
        provider = read(self.config_root / 'providers' / f'{args.provider}.json')
        if policy.get('provider_overrides', {}).get(args.provider, {}).get('enabled') is False or not shutil.which(provider.get('executable', args.provider)):
            raise ChangeError('Provider is disabled or not installed')
        try:
            validate(args.provider, args.model, args.effort, self.config_root)
        except ValueError as error:
            raise ChangeError(str(error)) from error
        task_path, task = self.task(args.task) if args.task else (None, None)
        if args.role == 'orchestrator' and task:
            raise ChangeError('Orchestrator selection belongs to the workspace, not a task')
        if args.role == 'lead' and args.when != 'next-task' and not task:
            raise ChangeError('Choose the current task for a lead handover')
        state = self.load()
        slot = 'orchestrator' if args.role == 'orchestrator' else (task['lead'] if task else state.get('default_lead') or args.provider)
        if args.role == 'worker':
            slot = getattr(args, 'slot', None)
            if not task or not slot or slot not in task.get('roles', {}) or slot == task.get('lead'):
                raise ChangeError('Worker handover requires a non-lead --slot in the selected task')
        if any(r['slot'] == slot and r['phase'] in PENDING for r in state['requests'].values()):
            raise ChangeError('This role already has a pending change; cancel it first')
        session, outgoing = self.sessions(slot)
        change_id = uuid.uuid4().hex[:12]
        record = dict(id=change_id, role=args.role, slot=slot, session=session,
                      outgoing_provider=outgoing, target=dict(provider=args.provider, model=args.model, effort=args.effort),
                      task=str(task_path) if task_path else None, when=args.when, phase='requested',
                      requested_at=now(), checkpoint=None)
        record['outgoing_session_id'] = run(['tmux', 'show-option', '-qv', '-t', session, '@aw_provider_session_id'])
        state['requests'][change_id] = record
        self.save(state)
        wake = self.root / '.orchestrator/.pending-wake'
        atomic(wake, 'handover-' + change_id)
        return record

    def change(self, state, change_id, phases):
        record = state['requests'].get(change_id)
        if not record or record['phase'] not in phases:
            raise ChangeError(f'Change must be in one of: {", ".join(phases)}')
        return record

    def checkpoint(self, change_id, note):
        state = self.load()
        record = self.change(state, change_id, {'requested'})
        note_text = Path(note).read_text().strip()
        if len(note_text) < 40:
            raise ChangeError('Handover note must explain objective, progress, remaining work and validation')
        tasks = self.active_tasks(record['slot'])
        if record['when'] == 'next-task' and tasks:
            raise ChangeError('Next-task change waits until existing tasks finish')
        if record['role'] != 'orchestrator' and any(str(task) != record['task'] for task in tasks):
            raise ChangeError('Other unfinished tasks use this role; checkpoint those tasks first')
        session, provider = self.sessions(record['slot'])
        if (session, provider) != (record['session'], record['outgoing_provider']):
            raise ChangeError('Outgoing session changed; cancel and select again')
        directory = run(['tmux', 'display-message', '-p', '-t', session, '#{pane_current_path}'])
        commit = None
        if record['role'] != 'orchestrator':
            if record['task']:
                _, task = self.task(record['task'])
                expected = Path(task['roles'][record['slot']]['worktree']).resolve()
                if Path(directory).resolve() != expected:
                    raise ChangeError('Lead pane is not in its assigned worktree')
            if run(['git', '-C', directory, 'status', '--porcelain']):
                raise ChangeError('Commit or save work before handover; the worktree must be clean')
            commit = run(['git', '-C', directory, 'rev-parse', 'HEAD'])
        record['checkpoint'] = dict(note=note_text, directory=directory, commit=commit, at=now())
        record['phase'] = 'ready'
        self.save(state)
        return record

    def stopped(self, session):
        # A visible composer does not establish idleness. Require the old
        # process to exit, and refuse shells with jobs or nested processes.
        panes = run(['tmux', 'list-panes', '-t', session, '-F', '#{pane_id}\t#{pane_pid}\t#{pane_current_command}']).splitlines()
        if len(panes) != 1:
            raise ChangeError('Handover requires one pane in the managed session')
        pane, pid, command = panes[0].split('\t')
        if command not in {'bash', 'zsh', 'fish', 'sh'}:
            raise ChangeError('Outgoing agent must exit after checkpointing')
        try:
            children = subprocess.run(['pgrep', '-P', pid], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as error:
            raise ChangeError('Cannot verify outgoing process has exited') from error
        if children.returncode != 1:
            raise ChangeError('Pane still has child processes; wait for them to exit')
        return pane

    def apply(self, change_id):
        state = self.load()
        record = self.change(state, change_id, {'ready', 'launching', 'awaiting_ack'})
        session, provider = self.sessions(record['slot'])
        if session != record['session']:
            raise ChangeError('Session changed; cannot apply this handover')
        if record['phase'] == 'ready' and provider != record['outgoing_provider']:
            raise ChangeError('Outgoing provider changed; cancel and select again')
        if record['phase'] != 'ready':
            generation = run(['tmux', 'show-option', '-qv', '-t', session, '@aw_handover_id'])
            if generation and generation != change_id:
                raise ChangeError('Cannot retry a different handover generation')
        pane = self.stopped(session)
        checkpoint = record['checkpoint']
        if record['role'] != 'orchestrator':
            if run(['git', '-C', checkpoint['directory'], 'status', '--porcelain']) or run(['git', '-C', checkpoint['directory'], 'rev-parse', 'HEAD']) != checkpoint['commit']:
                raise ChangeError('Worktree changed after checkpoint; cancel and checkpoint again')
        profile = run(['tmux', 'show-option', '-qv', '-t', session, '@aw_profile'])
        if not profile:
            profile = read(self.root / 'workspace.json').get('profile', 'trusted')
        if profile not in {'safe', 'trusted', 'yolo'}:
            raise ChangeError('Outgoing permission profile is invalid; handover cannot widen permissions')
        target = record['target']
        policy = read(self.config_root / 'config.json')
        manifest = read(self.config_root / 'providers' / f'{target["provider"]}.json')
        if (target['provider'] not in policy.get('providers', []) or
                policy.get('provider_overrides', {}).get(target['provider'], {}).get('enabled') is False or
                not shutil.which(manifest.get('executable', target['provider']))):
            raise ChangeError('Selected provider is no longer enabled or installed; cancel and select again')
        try:
            validate(target['provider'], target['model'], target['effort'], self.config_root)
        except ValueError as error:
            raise ChangeError(str(error)) from error
        if record.get('source') == 'routing':
            from capacity import capacities, pool_id, read_object
            routing = policy.get('routing', {})
            allowed = routing.get('allowed_models', {}).get(target['provider'])
            if allowed is not None and target['model'] not in allowed:
                raise ChangeError('Target model is no longer allowed; cancel and replan this handover')
            routing_task = record['routing'].get('task')
            if not routing_task:
                raise ChangeError('Routing task intent is missing; cancel and replan this handover')
            _, task = self.task(routing_task)
            intent = task.get('routing', {})
            if (task.get('status') in TERMINAL or
                    intent.get('difficulty', 'standard') != record['routing'].get('difficulty') or
                    intent.get('revision', 0) != record['routing'].get('task_revision', 0)):
                raise ChangeError('Task routing intent changed; cancel and replan this handover')
            workspace_policy = read_object(self.root / 'workspace.json').get('routing', {})
            effort_mode = workspace_policy.get('effort_mode', routing.get('effort_mode', 'fixed'))
            if (routing.get('mode') != 'automatic' or effort_mode != record['routing'].get('effort_mode') or
                    workspace_policy.get('revision', 0) != record['routing'].get('intent_revision', 0)):
                raise ChangeError('Routing preferences changed; cancel and replan this handover')
            _, pools = capacities(policy)
            pool = pools.get(pool_id(target['provider'], routing), {})
            reserve = 0 if record['routing'].get('review_reserve') else routing.get('reserve_fraction', 0.15)
            if pool.get('state') != 'available' or pool['remaining'] <= reserve:
                raise ChangeError('Target account no longer has fresh available capacity; cancel and replan')
        # Resolve explicitly against the selected settings without publishing
        # them as active ownership before the successor acknowledges.
        env = dict(os.environ, AW_HANDOVER_TARGET=json.dumps(target))
        command = subprocess.check_output([self.cli, 'provider-command', target['provider'], 'fresh', profile,
                                           '', target['model'], record['slot'], self.workspace], env=env, text=True).strip()
        if record['role'] == 'orchestrator':
            if target['provider'] in {'codex', 'claude'}:
                command += ' --add-dir ' + shlex.quote(str(self.root / '.coordination'))
            if target['provider'] == 'claude':
                command += ' --remote-control ' + shlex.quote(f'Workspaces · {self.workspace}')
        note_path = self.root / '.coordination/handovers' / f'{change_id}.md'
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(f'# Agent handover {change_id}\n\n{checkpoint["note"]}\n\n'
                             f'Workspace: {self.workspace}\nRole slot: {record["slot"]}\nTask: {record["task"] or "workspace coordination"}\n'
                             f'Worktree: {checkpoint["directory"]}\nCommit: {checkpoint["commit"] or "not applicable"}\n')
        instruction = (f'Read {note_path} and the workspace snapshot at {self.root}/.orchestrator/snapshot.md. '
                       f'You replace the {record["role"]} in role slot {record["slot"]}. '
                       'Keep its worktree, reports, commits and task scope. Before doing any work, acknowledge with '
                       f'agent-workspaces agents {self.workspace} accept {change_id}. '
                       'The acknowledgement authorizes continuation of the recorded assignment only. '
                       'If your task role is deferred or completed, remain idle until the dispatcher activates it.')
        command += ' ' + shlex.quote(instruction)
        launch = f'export AW_WORKSPACE_ID={shlex.quote(self.workspace)} AW_HANDOVER_ID={shlex.quote(change_id)}; {command}; {shlex.quote(self.cli)} remember-session {shlex.quote(session)} {shlex.quote(target["provider"])} {shlex.quote(checkpoint["directory"])} >/dev/null 2>&1 || true; exec bash'
        record['phase'] = 'launching'
        record['launched_at'] = now()
        self.save(state)  # A crash here is pending, never permission to launch twice.
        run(['tmux', 'set-option', '-t', session, '@aw_provider', target['provider']])
        run(['tmux', 'set-option', '-t', session, '@aw_provider_session_id', ''])
        run(['tmux', 'set-option', '-t', session, '@aw_handover_id', change_id])
        run(['tmux', 'respawn-pane', '-k', '-t', pane, '-c', checkpoint['directory'], 'bash', '-lc', launch])
        record['phase'] = 'awaiting_ack'
        self.save(state)
        return record

    def accept(self, change_id):
        state = self.load()
        record = self.change(state, change_id, {'awaiting_ack', 'launching'})
        if os.environ.get('AW_HANDOVER_ID') != change_id:
            raise ChangeError('Only the launched successor can acknowledge this handover')
        session, provider = self.sessions(record['slot'])
        if session != record['session'] or provider != record['target']['provider']:
            raise ChangeError('Successor session does not match the requested agent')
        if run(['tmux', 'show-option', '-qv', '-t', session, '@aw_handover_id']) != change_id:
            raise ChangeError('Successor session generation does not match')
        record['phase'] = 'active'
        record['accepted_at'] = now()
        state['active'][record['slot']] = dict(record['target'], change_id=change_id, source=record.get('source', 'manual'))
        if record['role'] == 'lead':
            state['default_lead'] = record['slot']
        self.save(state)
        return record

    def reconcile(self):
        results = []
        for record in list(self.load()['requests'].values()):
            if record['phase'] != 'ready':
                continue
            try:
                results.append(self.apply(record['id']))
            except ChangeError:
                # Readiness is checked again next time; no process is stopped.
                continue
        return results

    def cancel(self, change_id):
        state = self.load()
        record = self.change(state, change_id, PENDING)
        if record['phase'] in {'launching', 'awaiting_ack'}:
            session, _ = self.sessions(record['slot'])
            if session != record['session']:
                raise ChangeError('Session changed; cannot cancel this handover')
            self.stopped(session)
            run(['tmux', 'set-option', '-t', session, '@aw_provider', record['outgoing_provider']])
            run(['tmux', 'set-option', '-t', session, '@aw_provider_session_id', record.get('outgoing_session_id', '')])
            run(['tmux', 'set-option', '-t', session, '@aw_handover_id', ''])
        record['phase'] = 'cancelled'
        record['cancelled_at'] = now()
        self.save(state)
        return record


def show(state):
    lines = ['Agent assignments:']
    for slot, value in sorted(state['active'].items()):
        lines.append(f"- {slot}: {value['provider']} · {value['model']}/{value['effort']} (acknowledged)")
    for record in state['requests'].values():
        if record['phase'] in PENDING:
            target = record['target']
            lines.append(f"- {record['role']} ({record['slot']}): pending {target['provider']} · {target['model']}/{target['effort']}; "
                         f"{record['when']}; {record['phase']}; change {record['id']}")
    return '\n'.join(lines) if len(lines) > 1 else 'Agent assignments: using configured defaults; no pending handovers.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace')
    parser.add_argument('--cli', required=True)
    parser.add_argument('--json', action='store_true')
    sub = parser.add_subparsers(dest='action')
    sub.add_parser('show')
    sub.add_parser('reconcile')
    select = sub.add_parser('select')
    select.add_argument('--role', choices=['orchestrator', 'lead', 'worker'], required=True)
    select.add_argument('--slot')
    select.add_argument('--provider', choices=['codex', 'claude', 'grok'], required=True)
    select.add_argument('--model', required=True)
    select.add_argument('--effort', choices=['default', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'], required=True)
    select.add_argument('--when', choices=['now', 'checkpoint', 'next-task'], default='checkpoint')
    select.add_argument('--task')
    for action in ('checkpoint', 'apply', 'accept', 'cancel'):
        command = sub.add_parser(action)
        command.add_argument('change_id')
        if action == 'checkpoint':
            command.add_argument('--note', required=True)
    args = parser.parse_args()
    try:
        store = Assignments(root_for(args.workspace), args.cli)
        if args.action in (None, 'show'):
            state = store.load()
            print(json.dumps(state) if args.json else show(state))
            return
        with locked(store.file.parent / '.agents.lock'):
            if args.action == 'reconcile':
                result = store.reconcile()
            elif args.action == 'select':
                result = store.select(args)
            elif args.action == 'checkpoint':
                result = store.checkpoint(args.change_id, args.note)
            else:
                result = getattr(store, args.action)(args.change_id)
        print(json.dumps(result, indent=2))
    except (ChangeError, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f'agent-workspaces: {error}\n')


if __name__ == '__main__':
    main()
