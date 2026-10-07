"""Dream-RSI-inspired offline replay. Never calls providers or changes live routing."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import subprocess

VERSION = 'dream-replay-v2'


def number(value, name, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError('invalid ' + name)
    return value


def positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(name + ' must be a positive integer')
    return value


def identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing ' + name)
    return value


def object_value(value, name):
    if not isinstance(value, dict):
        raise ValueError(name + ' must be an object')
    return value


def array_value(value, name):
    if not isinstance(value, list):
        raise ValueError(name + ' must be an array')
    return value


def validate(document):
    object_value(document, 'manifest')
    if document.get('schema_version') != 1 or document.get('data_kind') not in ('real', 'synthetic'):
        raise ValueError('expected schema_version 1 and real/synthetic data_kind')
    identifier(document.get('evaluation_protocol'), 'evaluation_protocol')
    weights = object_value(document['objective'], 'objective')
    if set(weights) != {'quality', 'cost_usd', 'wall_seconds', 'attempts'}:
        raise ValueError('objective requires quality, cost_usd, wall_seconds, attempts weights')
    for name, value in weights.items():
        number(value, name)
    if weights['quality'] == 0:
        raise ValueError('quality weight must be positive')
    policies = array_value(document['policies'], 'policies')
    if not policies:
        raise ValueError('at least one policy required')
    seen = set()
    for policy in policies:
        object_value(policy, 'policy')
        name = identifier(policy.get('id'), 'policy.id')
        if name in seen:
            raise ValueError('duplicate policy ID')
        seen.add(name)
        if policy.get('strategy') not in ('breadth', 'best_leaf'):
            raise ValueError('strategy must be breadth or best_leaf')
        positive(policy.get('workers'), 'workers'); positive(policy.get('rounds'), 'rounds')
        if policy.get('stop_score') is not None:
            number(policy['stop_score'], 'stop_score')
    if document.get('incumbent') not in seen:
        raise ValueError('incumbent must name a candidate policy')
    seen, instances = set(), set()
    if not document['worlds']:
        raise ValueError('at least one discovery world required')
    for world in array_value(document['worlds'], 'worlds'):
        object_value(world, 'world')
        name = identifier(world.get('id'), 'world.id')
        instance = identifier(world.get('instance_id'), 'instance_id')
        if name in seen or instance in instances:
            raise ValueError('world IDs and task instances must be unique across splits')
        seen.add(name); instances.add(instance)
        if world.get('split') not in ('development', 'heldout'):
            raise ValueError('invalid split')
        number(world.get('root_score'), 'root_score')
        identifier(world.get('root_snapshot'), 'root_snapshot')
        nodes = {'root'}; children = set()
        if not world['nodes']:
            raise ValueError('world requires measured nodes')
        for node in array_value(world['nodes'], 'nodes'):
            object_value(node, 'node')
            name = identifier(node.get('id'), 'node.id')
            parent = node.get('parent')
            if name in nodes or parent not in nodes:
                raise ValueError('nodes must be unique and ordered after their parent')
            if parent != 'root' and parent in children:
                raise ValueError('only root may have multiple children in this replay interface')
            children.add(parent); nodes.add(name)
            number(node.get('score'), 'score')
            number(node.get('wall_seconds'), 'wall_seconds')
            if node.get('cost_usd') is not None:
                number(node['cost_usd'], 'cost_usd')
            if weights['cost_usd'] and node.get('cost_usd') is None:
                raise ValueError('cost-weighted replay requires measured costs, including failed attempts')
            for field in ('snapshot', 'attempt_id', 'configuration', 'verifier_version', 'evidence_sha256'):
                identifier(node.get(field), field)
            digest = node['evidence_sha256']
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('evidence_sha256 must be lowercase SHA-256')
        if len({n['attempt_id'] for n in world['nodes']}) != len(world['nodes']):
            raise ValueError('attempt IDs must be unique within each world')
    return document


def choose(observed, blocked, policy):
    """Receives revealed scores only; cannot inspect future outcomes or topology."""
    if policy.get('stop_score') is not None and max(n['score'] for n in observed.values()) >= policy['stop_score']:
        return []
    parents = {n['parent'] for n in observed.values() if n.get('parent')}
    eligible = [key for key in observed if key not in blocked and (key == 'root' or key not in parents)]
    if policy['strategy'] == 'breadth':
        eligible.sort(key=lambda key: (key != 'root', observed[key]['depth'], key))
    else:
        eligible.sort(key=lambda key: (-observed[key]['score'], key == 'root', key))
    return eligible[:policy['workers']]


def replay(world, policy, weights):
    children = {}
    for node in world['nodes']:
        children.setdefault(node['parent'], []).append(node)
    observed = {'root': {'score': world['root_score'], 'depth': 0}}
    blocked, trace = set(), []
    cost, wall, attempts, unsupported = 0.0, 0.0, 0, 0
    for _ in range(policy['rounds']):
        batch = choose(observed, blocked, policy)
        if not batch:
            break
        revealed, durations = [], []
        for parent in batch:
            node = next((n for n in children.get(parent, []) if n['id'] not in observed), None)
            if node is None:
                blocked.add(parent); unsupported += 1
                continue
            observed[node['id']] = dict(node, depth=observed[parent]['depth'] + 1)
            revealed.append(node['id']); durations.append(node['wall_seconds'])
            cost = None if cost is None or node.get('cost_usd') is None else cost + node['cost_usd']
            attempts += 1
        wall += max(durations, default=0)
        trace.append({'selected_parents': batch, 'revealed': revealed})
    best = max(n['score'] for n in observed.values())
    utility = weights['quality'] * best - weights['cost_usd'] * (cost or 0) - weights['wall_seconds'] * wall - weights['attempts'] * attempts
    return {'world': world['id'], 'policy': policy['id'], 'best_score': best,
            'attempts': attempts, 'rounds': len(trace), 'cost_usd': cost,
            'simulated_wall_seconds': wall, 'unsupported_continuations': unsupported,
            'utility': utility, 'trace': trace}


def optimize(document):
    validate(document)
    dev = [w for w in document['worlds'] if w['split'] == 'development']
    heldout = [w for w in document['worlds'] if w['split'] == 'heldout']
    results = []
    for policy in document['policies']:
        runs = [replay(w, policy, document['objective']) for w in dev]
        results.append({'policy': policy['id'], 'runs': runs,
                        'mean_utility': sum(r['utility'] for r in runs)/len(runs) if runs else None,
                        'supported': bool(runs) and all(r['unsupported_continuations'] == 0 for r in runs)})
    eligible = [r for r in results if r['supported']]
    baseline = next(r for r in results if r['policy'] == document['incumbent'])
    # Include the incumbent; ties preserve it. Never select on held-out results.
    winner = max(eligible, key=lambda r: (r['mean_utility'], r['policy'] == document['incumbent'], r['policy'])) if eligible and baseline['supported'] else None
    selected = winner['policy'] if winner else None
    policy_by_id = {p['id']: p for p in document['policies']}
    validation = []
    for name in dict.fromkeys([document['incumbent'], selected]):
        if name is not None:
            validation.extend(replay(w, policy_by_id[name], document['objective']) for w in heldout)
    reasons = []
    if document['data_kind'] != 'real': reasons.append('synthetic_data')
    if len(dev) < 3: reasons.append('fewer_than_three_development_instances')
    if len(heldout) < 3: reasons.append('fewer_than_three_heldout_instances')
    if winner is None: reasons.append('unsupported_development_replay')
    elif selected == document['incumbent']:
        reasons.append('no_development_improvement')
    if selected is not None and selected != document['incumbent'] and heldout:
        challenger = [r['utility'] for r in validation if r['policy'] == selected]
        incumbent = [r['utility'] for r in validation if r['policy'] == document['incumbent']]
        if sum(challenger) <= sum(incumbent):
            reasons.append('no_heldout_improvement')
    if any(r['unsupported_continuations'] for r in validation): reasons.append('unsupported_heldout_replay')
    return {'version': VERSION, 'mode': 'offline_shadow',
            'input_sha256': hashlib.sha256(json.dumps(document, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            'objective': document['objective'], 'development': results,
            'development_winner': selected, 'heldout': validation,
            'recommendation': selected if not reasons else None, 'abstention_reasons': reasons,
            'automatic_promotion': False,
            'limitations': ['Replay covers recorded branches only; it cannot estimate unseen models or actions.',
                'Scores and evidence references are supplied by the trace producer, not authenticated here.',
                'Batch latency assumes independent parallel workers; shared quota and queue contention are unmodelled.',
                'Held-out results are descriptive; minimum sample counts are not a significance test.',
                'Repeated inspection of held-out results contaminates that holdout; use fresh instances.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('replay'); run.add_argument('file', type=Path)
    init = sub.add_parser('init'); init.add_argument('spec', type=Path); init.add_argument('directory', type=Path)
    attempt_parser = sub.add_parser('attempt'); attempt_parser.add_argument('directory', type=Path)
    attempt_parser.add_argument('world'); attempt_parser.add_argument('--parent', default='root')
    for name in ('export', 'ingest', 'status'):
        command = sub.add_parser(name); command.add_argument('directory', type=Path)
    args = parser.parse_args()
    if args.command == 'replay':
        result = optimize(json.loads(args.file.read_text()))
    else:
        import dream_capture as capture
        if args.command == 'init': result = capture.initialize(args.spec, args.directory)
        elif args.command == 'attempt': result = capture.attempt(args.directory, args.world, args.parent)
        elif args.command == 'status': result = capture.status(args.directory)
        else: result = getattr(capture, args.command)(args.directory)
    print(json.dumps(result, indent=2, allow_nan=False))

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('dream: ' + str(error), file=sys.stderr)
        sys.exit(1)
