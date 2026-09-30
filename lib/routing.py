"""Deterministic quota-aware choices; application only requests safe handovers."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

from assignments import Assignments, ChangeError, PENDING, atomic, locked, root_for
from capacity import TERMINAL, capacities, pool_id, provider_for_slot, read_object, timestamp
from models import validate

DIFFICULTIES = ('simple', 'standard', 'complex')


def validate_policy(policy):
    for key in ('reserve_fraction', 'pressure_fraction', 'switch_improvement'):
        value = policy.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value < 1:
            raise ChangeError(f'routing.{key} must be a fraction from 0 to less than 1')
    for key in ('telemetry_ttl_seconds', 'cooldown_seconds'):
        value = policy.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ChangeError(f'routing.{key} must be a positive integer')
    if policy.get('mode') not in ('off', 'recommend', 'automatic'):
        raise ChangeError('routing.mode must be off, recommend or automatic')
    for value in policy.get('account_pools', {}).values():
        if not isinstance(value, str) or not value:
            raise ChangeError('Account pool IDs must be nonempty strings')


class Router:
    def __init__(self, root, cli, now=None):
        self.store = Assignments(root, cli)
        self.root = self.store.root
        self.config = read_object(self.store.config_root / 'config.json')
        self.policy = self.config.get('routing', {'mode': 'off'})
        self.now = now or datetime.now(timezone.utc)
        if self.policy.get('mode', 'off') != 'off': validate_policy(self.policy)
        self.providers, self.pools = capacities(self.config, now=self.now)

    def task(self, path=None):
        if path:
            return self.store.task(path)
        tasks = [(p.parent, read_object(p)) for p in (self.root / '.coordination').glob('*/state.json')]
        tasks = [(p, t) for p, t in tasks if t.get('status') not in TERMINAL]
        return max(tasks, key=lambda pair: (pair[1].get('created_at', ''), str(pair[0])), default=(None, None))

    def eligible(self):
        result = []
        for provider in self.config.get('providers', []):
            manifest = read_object(self.store.config_root / 'providers' / f'{provider}.json')
            if (manifest and self.config.get('provider_overrides', {}).get(provider, {}).get('enabled') is not False
                    and shutil.which(manifest.get('executable', provider))):
                result.append(provider)
        return result

    def load_counts(self):
        # One count per occupied slot, but a single quota pool per account.
        counts = Counter()
        data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'agent-workspaces'
        for manifest in data.glob('*/*/workspace.json'):
            slots = {}
            for path in (manifest.parent / '.coordination').glob('*/state.json'):
                task = read_object(path)
                if task.get('status') in TERMINAL: continue
                for slot, role in task.get('roles', {}).items():
                    if role.get('state') == 'completed': continue
                    slots[slot] = provider_for_slot(manifest.parent, slot, task, self.config)
            if slots:
                slots['orchestrator'] = provider_for_slot(manifest.parent, 'orchestrator', config=self.config)
            for provider in slots.values():
                if provider: counts[pool_id(provider, self.policy)] += 1
        return counts

    def current(self, slot, task, state):
        provider = provider_for_slot(self.root, slot, task, self.config)
        value = dict(self.config.get('model_policy', {}).get(provider, {}))
        value['provider'] = provider
        if slot == 'orchestrator' and provider == self.config.get('orchestrator_provider'):
            value['model'] = self.config.get('orchestrator_model', value.get('model'))
        value.update(state.get('active', {}).get(slot, {}))
        return {key: value.get(key) for key in ('provider', 'model', 'effort')}

    def plan(self, path=None):
        task_path, task = self.task(path)
        state = self.store.load()
        difficulty = (task or {}).get('routing', {}).get('difficulty', 'standard')
        if difficulty not in DIFFICULTIES: raise ChangeError('Invalid task routing difficulty')
        report = dict(schema_version=1, workspace_id=self.store.workspace, mode=self.policy.get('mode', 'off'),
                      task=str(task_path) if task_path else None, difficulty=difficulty,
                      pools=self.pools, decisions=[], warnings=[])
        workspace_policy = read_object(self.root / 'workspace.json').get('routing', {})
        effort_mode = workspace_policy.get('effort_mode', self.policy.get('effort_mode', 'fixed'))
        if effort_mode not in ('auto', 'fixed'): raise ChangeError('Effort mode must be auto or fixed')
        report['effort_mode'] = effort_mode
        report['intent_revision'] = workspace_policy.get('revision', 0)
        if report['mode'] == 'off' or not task or task.get('status') in TERMINAL:
            return report
        if task.get('lead') not in task.get('roles', {}):
            report['warnings'].append('Task has no valid lead; routing is observation-only until its metadata is repaired')
            return report
        slots = [task['lead']] + sorted(s for s in task.get('roles', {}) if s != task['lead'])
        if self.policy.get('manage_orchestrator', False): slots.append('orchestrator')
        counts = self.load_counts()
        lead_provider = provider_for_slot(self.root, task['lead'], task, self.config)
        eligible = self.eligible()
        for slot in slots:
            role = 'orchestrator' if slot == 'orchestrator' else ('lead' if slot == task['lead'] else 'worker')
            current = self.current(slot, task, state)
            current_pool = pool_id(current['provider'], self.policy)
            review = role == 'worker' or task.get('stage') in ('review', 'verify')
            row = dict(slot=slot, role=role, current=current, target=current, action='keep', reason='',
                       pool=current_pool, review_reserve=review)
            report['decisions'].append(row)
            if slot != 'orchestrator' and task['roles'][slot].get('state') == 'completed':
                row.update(action='idle', reason='role already completed')
                continue
            pending = [r for r in state['requests'].values() if r['slot'] == slot and r['phase'] in PENDING]
            if pending:
                row.update(action='pending', target=pending[-1]['target'], reason=f'checkpointed handover {pending[-1]["id"]} is pending')
                if role == 'lead': lead_provider = row['target']['provider']
                continue
            active = state.get('active', {}).get(slot)
            if active and active.get('source', 'manual') != 'routing':
                row.update(action='pinned', reason='explicit user assignment is preserved')
                continue
            # Never infer spare capacity from missing, stale or invalid evidence.
            current_capacity = self.pools.get(current_pool, {})
            if current_capacity.get('state', 'unknown') == 'unknown':
                row.update(action='unknown', reason='current account capacity is unverified; keep settings')
                continue
            recent = [r for r in state['requests'].values() if r['slot'] == slot and r.get('source') == 'routing'
                      and r['phase'] in ('active', 'cancelled')]
            if recent:
                latest = max(recent, key=lambda r: r.get('requested_at', ''))
                changed = timestamp(latest.get('accepted_at') or latest.get('cancelled_at') or latest.get('requested_at'))
                same_policy = latest.get('routing', {}).get('difficulty') == difficulty and latest.get('routing', {}).get('effort_mode') == effort_mode
                if same_policy and changed and (self.now-changed).total_seconds() < self.policy['cooldown_seconds'] and current_capacity.get('remaining', 0) > self.policy['reserve_fraction']:
                    row.update(action='cooldown', reason='recent routing change; avoid switching churn')
                    continue
            candidates = []
            for provider in eligible:
                pool = pool_id(provider, self.policy)
                capacity = self.pools[pool]
                if capacity['state'] != 'available': continue
                reserve = 0 if review else self.policy['reserve_fraction']
                usable = capacity['remaining'] - reserve
                if usable <= 0: continue
                profile = self.policy.get('profiles', {}).get(difficulty, {}).get(provider)
                if not isinstance(profile, dict): continue
                model = profile.get('model') or self.config.get('model_policy', {}).get(provider, {}).get('model')
                effort = profile.get('effort', 'high')
                pressure = capacity['used'] >= self.policy['pressure_fraction']
                if pressure: effort = profile.get('pressure_effort', effort)
                target = dict(provider=provider, model=model, effort=effort)
                if effort_mode == 'fixed':
                    settings = self.config.get('model_policy', {}).get(provider, {})
                    target = dict(provider=provider, model=settings.get('model'), effort=settings.get('effort', 'default'))
                    if provider == current['provider']: target = current.copy()
                    model, effort = target['model'], target['effort']
                allowed = self.policy.get('allowed_models', {}).get(provider)
                if allowed is not None and model not in allowed: continue
                try: validate(provider, model, effort, self.store.config_root)
                except (ValueError, OSError): continue
                load = max(0, counts[pool] - (1 if pool == current_pool else 0))
                candidates.append(dict(target=target, pool=pool, score=usable/(load+1), pressure=pressure))
            if review:
                independent = [c for c in candidates if c['pool'] != pool_id(lead_provider, self.policy)]
                if independent: candidates = independent
                elif candidates: report['warnings'].append(f'{slot}: no independent account with verified capacity; reduced review independence')
            if not candidates:
                row.update(action='wait', reason='no allowed model/account with fresh capacity above the applicable reserve')
                continue
            best = max(candidates, key=lambda c: (c['score'], c['target']['provider'] == current['provider'], c['target']['provider']))
            incumbent = next((c for c in candidates if c['target']['provider'] == current['provider']), None)
            if incumbent and best['score'] - incumbent['score'] < self.policy['switch_improvement']:
                best = incumbent
            row.update(target=best['target'], pool=best['pool'],
                       action='keep' if current == best['target'] else 'handover',
                       reason=f'{difficulty} task; {"fixed settings" if effort_mode == "fixed" else "pressure effort" if best["pressure"] else "quality profile"}; fresh capacity weighted by shared slot load')
            if row['action'] == 'handover' and any(r['slot'] == slot and r['phase'] == 'cancelled' and
                    r.get('source') == 'routing' and r['target'] == row['target'] and
                    r.get('routing', {}).get('task') == report['task'] and
                    r.get('routing', {}).get('difficulty') == difficulty and
                    r.get('routing', {}).get('intent_revision', 0) == report['intent_revision']
                    for r in state['requests'].values()):
                row.update(action='cancelled', target=current, pool=current_pool, reason='user cancelled this routing choice; change routing preferences to reconsider')
                continue
            if role == 'lead': lead_provider = best['target']['provider']
            counts[current_pool] = max(0, counts[current_pool]-1)
            counts[best['pool']] += 1
        return report

    def reconcile(self, path=None):
        # Same lock as manual selection, so automatic/manual requests cannot race.
        with locked(self.store.file.parent / '.agents.lock'):
            report = self.plan(path)
            if report['mode'] != 'automatic': return report
            state = self.store.load()
            for row in report['decisions']:
                if row['action'] != 'handover': continue
                args = argparse.Namespace(role=row['role'], slot=row['slot'], provider=row['target']['provider'],
                                          model=row['target']['model'], effort=row['target']['effort'],
                                          when='checkpoint', task=None if row['role'] == 'orchestrator' else report['task'])
                try:
                    record = self.store.select(args)
                    state = self.store.load()
                    record = state['requests'][record['id']]
                    record.update(source='routing', routing=dict(reason=row['reason'], difficulty=report['difficulty'],
                                  pool=row['pool'], task=report['task'], review_reserve=row['review_reserve'],
                                  effort_mode=report['effort_mode'], intent_revision=report['intent_revision']))
                    self.store.save(state)
                    row.update(action='pending', change_id=record['id'])
                except ChangeError as error:
                    row.update(action='unavailable', reason=str(error))
            # Stable signature prevents monitor ticks from producing unbounded logs.
            evidence = {k: report[k] for k in ('mode', 'effort_mode', 'intent_revision', 'task', 'difficulty', 'decisions', 'warnings')}
            signature = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
            previous = read_object(self.root / '.coordination/routing.json')
            if previous.get('signature') != signature:
                record = dict(report, signature=signature, at=self.now.isoformat())
                atomic(self.root / '.coordination/routing.json', record)
                with (self.root / '.coordination/routing-audit.jsonl').open('a') as stream:
                    stream.write(json.dumps(record, sort_keys=True)+'\n')
            return report

    def guard(self, slots, path=None, review=False):
        task_path, task = self.task(path) if path else (None, None)
        if self.policy.get('mode', 'off') != 'automatic': return []
        state = self.store.load()
        reasons = []
        for slot in slots:
            if any(r['slot'] == slot and r['phase'] in PENDING for r in state['requests'].values()):
                reasons.append(f'{slot}: finish or cancel pending handover')
            provider = provider_for_slot(self.root, slot, task, self.config)
            pool = self.pools.get(pool_id(provider, self.policy), {})
            is_review = review or bool(task and (slot != task.get('lead') or task.get('stage') in ('review', 'verify')))
            reserve = 0 if is_review else self.policy['reserve_fraction']
            if pool.get('state') == 'exhausted' or (pool.get('state') == 'available' and pool['remaining'] <= reserve):
                reasons.append(f'{slot}: account quota is exhausted or reserved for review; wait for fresh capacity or route to another account')
        return reasons


def display(report):
    lines = [f'Routing: {report["mode"]}; effort: {report["effort_mode"]}; task difficulty: {report["difficulty"]}']
    for name, pool in sorted(report['pools'].items()):
        # Quantize display to avoid waking the orchestrator for tiny usage changes.
        remaining = 'unknown' if pool['remaining'] is None else f'{int(pool["remaining"]*100)//5*5}–{min(100, int(pool["remaining"]*100)//5*5+5)}%'
        lines.append(f'- Account {name}: {pool["state"]}; remaining {remaining}; next reset {pool["resets_at"] or "unknown"}')
    for row in report['decisions']:
        target = row['target']
        lines.append(f'- {row["slot"]}: {row["action"]} → {target["provider"]}/{target["model"]}/{target["effort"]}: {row["reason"]}')
    lines.extend('- '+warning for warning in report['warnings'])
    if any(row['action'] == 'pending' for row in report['decisions']):
        lines.append('Pending routing changes need an outgoing handover note and clean worker checkpoint, CLI exit, then successor acknowledgement. Do not interrupt running commands. Deferred reviewers must remain idle until activation.')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace')
    parser.add_argument('--cli', required=True)
    parser.add_argument('action', choices=('show', 'reconcile', 'guard', 'difficulty', 'effort-mode'), nargs='?', default='show')
    parser.add_argument('--mode', choices=('auto', 'fixed'))
    parser.add_argument('--task')
    parser.add_argument('--difficulty', choices=DIFFICULTIES)
    parser.add_argument('--roles', nargs='+')
    parser.add_argument('--review', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        router = Router(root_for(args.workspace), args.cli)
        if args.action == 'effort-mode':
            if not args.mode: raise ChangeError('Specify --mode auto or fixed')
            with locked(router.store.file.parent / '.agents.lock'):
                state = router.store.load()
                if any(r.get('source') == 'routing' and r['phase'] in ('launching', 'awaiting_ack') for r in state['requests'].values()):
                    raise ChangeError('Finish or cancel the launched handover before changing effort mode')
                manifest = read_object(router.root / 'workspace.json')
                settings = manifest.setdefault('routing', {})
                settings['effort_mode'] = args.mode
                settings['revision'] = settings.get('revision', 0) + 1
                atomic(router.root / 'workspace.json', manifest)
                # Unlaunched automatic requests can be cancelled without touching processes.
                for record in state['requests'].values():
                    if record.get('source') == 'routing' and record['phase'] in ('requested', 'ready'):
                        record.update(phase='cancelled', cancelled_at=router.now.isoformat())
                if args.mode == 'auto':
                    # Choosing Auto explicitly releases existing manual settings
                    # to policy control without changing ownership or processes.
                    for value in state['active'].values(): value['source'] = 'routing'
                router.store.save(state)
        if args.action == 'difficulty':
            if not args.task or not args.difficulty: raise ChangeError('Specify --task and --difficulty')
            path, _ = router.store.task(args.task)
            with locked(path / '.state.lock'):
                state = read_object(path / 'state.json')
                state.setdefault('routing', {})['difficulty'] = args.difficulty
                atomic(path / 'state.json', state)
        if args.action == 'guard':
            if not args.roles: raise ChangeError('Specify --roles')
            errors = router.guard(args.roles, args.task, args.review)
            if errors: parser.exit(2, '\n'.join(errors)+'\n')
            return
        result = router.reconcile(args.task) if args.action == 'reconcile' else router.plan(args.task)
        print(json.dumps(result) if args.json else display(result))
    except (ChangeError, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'agent-workspaces routing: {error}\n')


if __name__ == '__main__':
    main()
