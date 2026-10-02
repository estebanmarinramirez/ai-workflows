"""Auditable Bayesian recommendations; never dispatches or mutates live routing."""
import hashlib
import json
import math

VERSION = 'beta-shadow-v1'
DEFAULTS = {'enabled': False, 'prior_alpha': 1, 'prior_beta': 1, 'min_tasks': 3,
            'success_value': 1.0, 'cost_weight': 1.0, 'latency_weight': 0.001,
            'minimum_success_mean': 0.0}


def policy(manifest):
    result = dict(DEFAULTS, **manifest.get('shadow', {}))
    if not isinstance(result['enabled'], bool):
        raise ValueError('shadow.enabled must be boolean')
    for name in ('prior_alpha', 'prior_beta', 'min_tasks'):
        if isinstance(result[name], bool) or not isinstance(result[name], int) or result[name] < 1:
            raise ValueError(f'shadow.{name} must be a positive integer')
    for name in ('success_value', 'cost_weight', 'latency_weight', 'minimum_success_mean'):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f'Invalid shadow.{name}')
    if result['minimum_success_mean'] > 1:
        raise ValueError('minimum_success_mean must be at most 1')
    if result['enabled']:
        if manifest.get('data_kind') not in ('real', 'synthetic'):
            raise ValueError('Shadow experiments require explicit real/synthetic data_kind')
        seen = set()
        for task in manifest['tasks']:
            for key in ('family', 'instance_id'):
                if not isinstance(task.get(key), str) or not task[key].strip():
                    raise ValueError(f'Shadow tasks require {key}')
            if task['instance_id'] in seen:
                raise ValueError('Task instance IDs must be unique across both splits; use repeats for repetitions')
            seen.add(task['instance_id'])
        for config in manifest['configurations']:
            if not isinstance(config.get('shadow_eligible', True), bool):
                raise ValueError('shadow_eligible must be boolean')
            if any(not isinstance(r.get('model_version'), str) or not r['model_version'].strip()
                   for r in config['roles'].values()):
                raise ValueError('Shadow roles require explicit model_version identifiers')
    return result


def attribution(outcome):
    """Keep observed verification failure distinct from execution/infrastructure failure."""
    failure = outcome.get('failure')
    if failure is None and outcome.get('accepted'):
        return {'failure_category': None, 'capability_label': True}
    if failure == 'verification_failed' and 'telemetry_error' not in outcome:
        return {'failure_category': 'quality', 'capability_label': False}
    category = {'adapter_timeout': 'execution_timeout', 'adapter_failed': 'execution_unknown',
                'verifier_timeout': 'verification_infrastructure', 'verifier_error': 'verification_infrastructure',
                'invalid_adapter_telemetry': 'protocol', 'harness_error': 'harness',
                'interrupted': 'interrupted'}.get(failure, 'unknown')
    return {'failure_category': category, 'capability_label': None}


def fingerprint(manifest, config):
    value = {'topology': config['topology'], 'roles': config['roles'],
             'adapter': manifest['hashes']['adapter'], 'verifier': manifest['hashes']['verifier']}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def beta(values, settings):
    alpha = settings['prior_alpha'] + sum(values)
    beta_ = settings['prior_beta'] + len(values) - sum(values)
    return {'alpha': alpha, 'beta': beta_, 'mean': alpha/(alpha+beta_),
            'stddev': math.sqrt(alpha*beta_/((alpha+beta_)**2*(alpha+beta_+1))),
            'observations': len(values)}


def predict(manifest, history, task, actual_configuration, cutoff):
    """Use only completed development repetition zero; exclude the target instance."""
    settings = policy(manifest)
    tasks = {t['id']: t for t in manifest['tasks']}
    synthetic = (manifest.get('data_kind') != 'real' or
                 any(r['provider'] == 'demo' for c in manifest['configurations'] for r in c['roles'].values()))
    candidates = []
    all_training = []
    for config in manifest['configurations']:
        rows = []
        if not synthetic:
            rows = [row for row in history if row['status'] == 'finished' and row['configuration'] == config['id']
                    and row['repetition'] == 0 and tasks[row['task']]['split'] == 'development'
                    and tasks[row['task']]['family'] == task['family']
                    and tasks[row['task']]['instance_id'] != task['instance_id']]
        outcomes = [json.loads(row['result']) for row in rows]
        end_to_end = beta([bool(o['accepted']) for o in outcomes], settings)
        capability = beta([attribution(o)['capability_label'] for o in outcomes
                           if attribution(o)['capability_label'] is not None], settings)
        cost = sum(o['cost_usd'] for o in outcomes)/len(outcomes) if outcomes and all(o.get('cost_usd') is not None for o in outcomes) else None
        latency = sum(o['wall_seconds'] for o in outcomes)/len(outcomes) if outcomes else None
        missing = ((settings['cost_weight'] > 0 and cost is None) or
                   (settings['latency_weight'] > 0 and latency is None))
        score = None if missing else (settings['success_value']*end_to_end['mean'] -
                settings['cost_weight']*(cost or 0) - settings['latency_weight']*(latency or 0))
        candidates.append({'configuration': config['id'], 'fingerprint': fingerprint(manifest, config),
            'eligible': config.get('shadow_eligible', True), 'end_to_end': end_to_end, 'capability': capability,
            'mean_cost_usd': cost, 'mean_wall_seconds': latency, 'expected_utility': score,
            'training_trials': [r['id'] for r in rows],
            'excluded_capability_outcomes': len(outcomes)-capability['observations']})
        all_training.extend(r['id'] for r in rows)
    eligible = [c for c in candidates if c['eligible']]
    reason, chosen = None, None
    if synthetic:
        reason = 'synthetic_or_demo_data'
    elif not eligible:
        reason = 'no_eligible_configuration'
    elif any(c['end_to_end']['observations'] < settings['min_tasks'] for c in eligible):
        reason = 'insufficient_distinct_development_tasks'
    elif any(c['expected_utility'] is None for c in eligible):
        reason = 'unknown_cost_or_latency'
    else:
        feasible = [c for c in eligible if c['end_to_end']['mean'] >= settings['minimum_success_mean']]
        if not feasible:
            reason = 'success_mean_floor_not_met'
        else:
            chosen = max(feasible, key=lambda c: (c['expected_utility'], c['configuration']))['configuration']
    return {'policy_version': VERSION, 'policy': settings, 'task_family': task['family'],
            'task_instance': task['instance_id'], 'actual_configuration': actual_configuration,
            'recommended_configuration': chosen, 'abstention_reason': reason, 'candidates': candidates,
            'training_trials': sorted(set(all_training)), 'evidence_cutoff_event': cutoff,
            'mode': 'shadow', 'synthetic': synthetic,
            'limitation': 'Conditional Beta-Bernoulli approximation, not a proof of Bayes consistency. Repetition zero only; same-instance evidence excluded.'}


def evaluate(manifest, events, split):
    """Score stored pre-outcome predictions, never retroactively fitted estimates."""
    forecasts, scores, excluded, failures = {}, [], 0, {}
    for event in events:
        payload = json.loads(event['payload'])
        if event['kind'] == 'shadow_prediction':
            forecasts[event['trial']] = payload
        elif event['kind'] == 'finished' and event['trial'] in forecasts:
            prediction = forecasts[event['trial']]
            if prediction['split'] != split: continue
            if prediction['synthetic']:
                excluded += 1
                continue
            candidate = next(c for c in prediction['candidates'] if c['configuration'] == prediction['actual_configuration'])
            p = candidate['end_to_end']['mean']
            y = float(bool(payload['accepted']))
            category = attribution(payload)['failure_category'] or 'success'
            failures[category] = failures.get(category, 0) + 1
            scores.append({'trial': event['trial'], 'probability': p, 'outcome': y,
                           'brier': (p-y)**2, 'log_loss': -math.log(p if y else 1-p),
                           'recommended_matches_actual': prediction['recommended_configuration'] == prediction['actual_configuration']})
    return {'policy_version': VERSION, 'split': split, 'scored_trials': len(scores),
            'excluded_synthetic_trials': excluded, 'failure_categories': failures,
            'brier_score': sum(s['brier'] for s in scores)/len(scores) if scores else None,
            'log_loss': sum(s['log_loss'] for s in scores)/len(scores) if scores else None,
            'predictions': scores,
            'limitations': 'Calibration of observed executed configurations only. Repetitions are correlated; no causal or counterfactual claim about recommendation benefit.'}
