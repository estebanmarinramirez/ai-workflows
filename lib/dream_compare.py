"""Compare measured model/effort captures on paired tasks; never invent counterfactuals."""
import hashlib
import json
import math
from pathlib import Path
import dream
from dream_statistics import paired_evidence
from dream_evaluation import evaluation, holdout_keys


def _compare(path, expose, spec_raw, exports):
    path=Path(path);spec=json.loads(spec_raw)
    if spec.get('schema_version')!=1:raise ValueError('unsupported comparison schema')
    difficulty=spec['difficulty']
    if difficulty not in ('simple','standard','complex'):raise ValueError('invalid difficulty')
    minimum=spec.get('min_instances',3);dream.positive(minimum,'min_instances')
    if minimum<3:raise ValueError('at least three instances per split required')
    penalty=dream.number(spec.get('uncertainty_weight',1),'uncertainty_weight')
    evidence_settings=spec.get('evidence',{})
    paired_evidence([],**evidence_settings)
    rows=[];reference=None;weights=None;protocol=None;strategy=None;identities=set();all_real=True;sources=[]
    for candidate in spec['candidates']:
        identifier=dream.identifier(candidate['id'],'candidate.id')
        if identifier in identities:raise ValueError('duplicate candidate')
        identities.add(identifier)
        raw=exports[candidate['export']]
        if hashlib.sha256(raw).hexdigest()!=candidate['sha256']:raise ValueError('comparison export changed')
        doc=dream.validate(json.loads(raw));all_real &= doc['data_kind']=='real'
        if protocol is None:protocol=doc['evaluation_protocol']
        if protocol!=doc['evaluation_protocol']:raise ValueError('evaluation protocol differs across candidates')
        sources.append({'candidate':identifier,'sha256':candidate['sha256']})
        if weights is None:weights=doc['objective']
        if weights!=doc['objective']:raise ValueError('objective differs across candidates')
        versions={n.get('observed_model_version') for w in doc['worlds'] for n in w['nodes'] if n.get('observed_model_version')}
        if len(versions)>1:raise ValueError('mixed served model versions within candidate')
        policy=next((p for p in doc['policies'] if p['id']==candidate['policy']),None)
        if policy is None:raise ValueError('unknown frozen policy')
        settings={k:v for k,v in policy.items() if k!='id'}
        if strategy is None:strategy=settings
        if strategy!=settings:raise ValueError('replay policy differs across candidates')
        # Capture exports carry the actual requested model/effort and frozen task evidence.
        agent=doc.get('agent')
        if not agent or any(not agent.get(k) for k in ('provider','model','effort')):raise ValueError('capture export requires agent settings')
        worlds={w['instance_id']:w for w in doc['worlds']}
        signatures={i:(w['split'],w['root_snapshot'],w['root_score'],w.get('prompt_sha256'),w.get('verifier_sha256')) for i,w in worlds.items()}
        if any(v[-1] is None or v[-2] is None for v in signatures.values()):raise ValueError('capture task evidence missing')
        if reference is None:reference=signatures
        if signatures!=reference:raise ValueError('candidates must use the same independent instances, splits, baseline, prompts and verifiers')
        runs=[]
        for instance,w in worlds.items():
            if w['root_score']!=0:raise ValueError('comparison requires initially failing tasks')
            if any(n.get('requested_agent')!=agent for n in w['nodes']):raise ValueError('node configuration differs from capture agent')
            if w['root_score'] not in (0,1) or any(n['score'] not in (0,1) for n in w['nodes']):raise ValueError('comparison requires binary acceptance')
            if w['split']!='development':continue
            result=dream.replay(w,policy,weights);result.update(instance_id=instance,split=w['split']);runs.append(result)
        dev=[r for r in runs if r['split']=='development']
        a=1+sum(r['best_score'] for r in dev);b=1+len(dev)-sum(r['best_score'] for r in dev)
        mean=a/(a+b);std=math.sqrt(a*b/((a+b)**2*(a+b+1)))
        costs=[r['cost_usd'] for r in dev]
        cost=sum(costs)/len(costs) if costs and all(c is not None for c in costs) else None
        latency=sum(r['simulated_wall_seconds'] for r in dev)/len(dev) if dev else 0
        attempts=sum(r['attempts'] for r in dev)/len(dev) if dev else 0
        utility=(weights['quality']*(mean-penalty*std)-weights['cost_usd']*(cost or 0)-weights['wall_seconds']*latency-weights['attempts']*attempts)
        rows.append(dict(id=identifier,agent=agent,runs=runs,_worlds=worlds,_policy=policy,posterior=dict(alpha=a,beta=b,mean=mean,stddev=std),
                         mean_cost_usd=cost,mean_wall_seconds=latency,development_utility=utility))
    if len(rows)<2:raise ValueError('comparison requires at least two measured configurations')
    incumbent=next((r for r in rows if r['id']==spec['incumbent']),None)
    if incumbent is None:raise ValueError('missing incumbent')
    winner=max(rows,key=lambda r:(r['development_utility'],r['id']==incumbent['id'],r['id']))
    reasons=[]
    if not all_real:reasons.append('synthetic_data')
    if any(not w.get('execution') for row in rows for w in row['_worlds'].values()):reasons.append('unverified_execution_model')
    for split in ('development','heldout'):
        if sum(s[0]==split for s in reference.values())<minimum:reasons.append('insufficient_'+split+'_instances')
    if any(r['unsupported_continuations'] for row in rows for r in row['runs'] if r['split']=='development'):
        reasons.append('unsupported_development_replay')
    if winner['id']==incumbent['id'] or winner['development_utility']<=incumbent['development_utility']:reasons.append('no_development_improvement')
    # Commit the fixed choice and source/spec hashes before evaluating any holdout.
    exposure=holdout_keys([dict(instance_id=i,split=s[0]) for i,s in reference.items()])
    expose(exposure)
    paired=[];utilities=[]
    for row in rows:
        if row['id'] in (winner['id'],incumbent['id']):
            for instance,w in row['_worlds'].items():
                if w['split']=='heldout':
                    result=dream.replay(w,row['_policy'],weights)
                    result.update(instance_id=instance,split='heldout');row['runs'].append(result)
        del row['_worlds'];del row['_policy']
    baseline={r['instance_id']:r for r in incumbent['runs'] if r['split']=='heldout'}
    for r in winner['runs']:
        if r['split']!='heldout':continue
        old=baseline[r['instance_id']]
        paired.append({'instance_id':r['instance_id'],'utility_delta':r['utility']-old['utility']})
        utilities.append((r['utility'],old['utility']))
        if r['unsupported_continuations'] or old['unsupported_continuations']:reasons.append('unsupported_heldout_replay')
    if not paired or sum(r['utility_delta'] for r in paired)<=0:reasons.append('no_heldout_improvement')
    evidence=paired_evidence(utilities,**evidence_settings)
    if not evidence['supported']:reasons.append('insufficient_paired_evidence')
    if evidence['bounds_breached']:reasons.append('utility_bounds_breached')
    proposal=None
    if not reasons:
        agent=winner['agent'];proposal={'budget':{'profiles':{agent['provider']:{difficulty:{'model':agent['model'],'effort':agent['effort']}}}}}
    # Do not expose unselected candidates' heldout outcomes for iterative tuning.
    for row in rows:row['runs']=[r for r in row['runs'] if r['split']=='development']
    return dict(version='dream-configuration-comparison-v2',mode='offline_shadow',comparison_sha256=hashlib.sha256(spec_raw).hexdigest(),sources=sources,objective=weights,
                uncertainty_weight=penalty,development_score_kind='heuristic_screening_not_confidence_bound',paired_evidence=evidence,development=rows,selected=winner['id'],heldout_paired=paired,
                recommendation=winner['id'] if not reasons else None,routing_profile_proposal=proposal,
                abstention_reasons=sorted(set(reasons)),automatic_promotion=False,
                limitations=['Requires fresh prospective validation before applying a profile.',
                            'Preference weights are explicit, not inferred from activity counts.',
                            'Paired bounds require the stated independence and fixed-sample assumptions.',
                            'Provider costs can be list-price equivalents rather than subscription credit charges.',
                            'Budget admission and queue contention are not simulated.'])


def compare(path):
    # Hash all frozen input bytes; changed evidence or preferences require fresh holdouts.
    path=Path(path);raw=path.read_bytes();spec=json.loads(raw)
    content=[hashlib.sha256(raw).hexdigest(),'dream-configuration-comparison-v2']
    content.extend(hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ('dream_compare.py','dream.py','dream_statistics.py'))
    exports={}
    for row in spec['candidates']:
        exports[row['export']]=(path.parent/row['export']).read_bytes()
        content.append(hashlib.sha256(exports[row['export']]).hexdigest())
    identifier=hashlib.sha256(json.dumps(content).encode()).hexdigest()
    from contextlib import ExitStack
    with ExitStack() as stack:
        holder=None
        def expose(instances):
            nonlocal holder
            holder=stack.enter_context(evaluation(identifier,instances))
        result=_compare(path,expose,raw,exports)
        if holder['cached'] is not None:return holder['cached']
        result['evaluation_id']=identifier
        result['holdout_reuse_protection']='local_ledger_not_global_authentication'
        holder['result']=result
        return result
