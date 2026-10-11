"""Shared local admission control. Account quota is not an invoice or per-turn cap."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from capacity import capacities, pool_id, read_object
from models import validate
from assignments import ChangeError

DEFAULTS = {'enabled':False, 'unknown_interactive':'warn', 'unknown_managed_providers':[], 'external_reserve_fraction':.30, 'effort_pressure_fraction':.50, 'experiment_reserve_fraction':.50,
            'experiment_attempts_24h':2, 'experiment_concurrency':1,
            'managed_concurrency_per_pool':2, 'boost_minutes_24h':20, 'boost_max_minutes':10, 'experiment_timeout_seconds':120}
PROFILES = {'codex':{'simple':('gpt-6-luna','low'),'standard':('gpt-6.1-sol','medium'),'complex':('gpt-6.1-sol','high')},
            'claude':{'simple':('sonnet','low'),'standard':('sonnet','medium'),'complex':('opus','high')},
            'grok':{'simple':('grok-4.7-build-fast','low'),'standard':('grok-4.7-build-fast','medium'),'complex':('grok-4.6','high')}}


def configuration():
    root=Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'agent-workspaces'
    path=root/'config.json'
    try: config=json.loads(path.read_text())
    except FileNotFoundError: config={}
    except (OSError,ValueError) as error: raise ValueError('cannot read budget configuration') from error
    if not isinstance(config,dict): raise ValueError('budget configuration must be an object')
    return config, root


def settings(config):
    value=dict(DEFAULTS,**config.get('budget',{}))
    if type(value['enabled']) is not bool: raise ValueError('budget.enabled must be boolean')
    if not isinstance(value['unknown_managed_providers'],list) or any(x not in PROFILES for x in value['unknown_managed_providers']): raise ValueError('invalid budget.unknown_managed_providers')
    if value['unknown_interactive'] not in ('warn','block'): raise ValueError('invalid budget.unknown_interactive')
    for key in ('external_reserve_fraction','experiment_reserve_fraction','effort_pressure_fraction'):
        if type(value[key]) not in (int,float) or not 0<=value[key]<1: raise ValueError('invalid budget.'+key)
    if value['effort_pressure_fraction'] >= 1-value['external_reserve_fraction']:
        raise ValueError('effort pressure must begin before the external reserve threshold')
    if value['experiment_reserve_fraction']<value['external_reserve_fraction']:
        raise ValueError('experiment reserve must protect the external reserve')
    for key in ('experiment_attempts_24h','experiment_concurrency','managed_concurrency_per_pool','boost_minutes_24h','boost_max_minutes','experiment_timeout_seconds'):
        if type(value[key]) is not int or value[key]<1: raise ValueError('invalid budget.'+key)
    return value


def reserve(config, purpose='managed'):
    p=settings(config)
    return p['experiment_reserve_fraction' if purpose=='experiment' else 'external_reserve_fraction'] if p['enabled'] else 0


def under_pressure(config,pool):
    policy=settings(config)
    threshold=policy['effort_pressure_fraction'] if policy['enabled'] else config.get('routing',{}).get('pressure_fraction',.7)
    return pool.get('used') is not None and pool['used']>=threshold


def allows_unknown_managed(config,provider,pool,purpose='managed'):
    return (purpose=='managed' and pool.get('state')=='unknown'
            and provider in settings(config)['unknown_managed_providers']
            and (pool.get('remaining') is None or pool['remaining']>reserve(config)))


def check(config, provider, purpose='managed', interactive=False):
    if not settings(config)['enabled']: return
    _, pools=capacities(config)
    pool=pools.get(pool_id(provider,config.get('routing',{})),{})
    # Partial authoritative evidence can forbid spending even when another
    # account alias has unknown limits. Never turn exhaustion into a warning.
    if pool.get('remaining') is not None and pool['remaining']<=reserve(config,purpose):
        raise ValueError('budget: '+purpose+' would use protected quota; wait for reset or use another account')
    if pool.get('state')!='available':
        if allows_unknown_managed(config,provider,pool,purpose):
            print('budget: '+provider+' quota unknown; user-authorized managed-work exception active. Reserve is unverified.',file=sys.stderr)
            return
        if (interactive and purpose=='managed' and pool.get('state')=='unknown'
                and settings(config)['unknown_interactive']=='warn'):
            print('budget: quota unknown for '+provider+'; opening interactive CLI with concurrency limits. '
                  'Reserve cannot be verified; automated work and RSI remain blocked.',file=sys.stderr)
            return
        raise ValueError('budget: fresh authoritative quota is required for '+provider)


def check_experiment(config,agent,timeout):
    p=settings(config)
    if not p['enabled']:return
    if agent.get('model') != PROFILES[agent['provider']]['simple'][0] or agent.get('effort') not in ('low','medium'):
        raise ValueError('budget: RSI requires an economical model at low/medium effort; initialize a new frozen dataset')
    if timeout>p['experiment_timeout_seconds']:
        raise ValueError('budget: frozen experiment timeout exceeds the experiment allowance')


def observed_sessions(config,pool,claimed=(),exclude_pane=None):
    """Count busy managed tmux panes, including providers launched before this release."""
    data=Path(os.environ.get('XDG_DATA_HOME',str(Path.home()/'.local/share')))/'agent-workspaces'
    workspaces={read_object(p).get('workspace_id') for p in data.glob('*/*/workspace.json')}
    if not workspaces:return 0
    try:
        result=subprocess.run(['tmux','list-panes','-a','-F','#{@aw_workspace_id}\t#{@aw_provider}\t#{pane_current_command}\t#{pane_id}\t#{@aw_budget_claim}'],capture_output=True,text=True,timeout=3)
    except (OSError,subprocess.SubprocessError):return 0
    count=0
    for line in result.stdout.splitlines():
        parts=line.split('\t')
        if len(parts)!=5:continue
        workspace,provider,command,pane,claim=parts
        if claim in claimed:continue
        if workspace not in workspaces or not provider or pane==exclude_pane:continue
        if Path(command).name in ('bash','sh','zsh','fish','nu','tmux',''):continue
        if pool_id(provider,config.get('routing',{}))==pool:count+=1
    return count


def database():
    directory=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'agent-workspaces/budget'
    directory.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(directory,0o700)
    db=sqlite3.connect(directory/'ledger.sqlite',timeout=10);os.chmod(directory/'ledger.sqlite',0o600)
    db.row_factory=sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY,purpose TEXT,pool TEXT,pid INTEGER,started REAL,finished REAL);
        CREATE TABLE IF NOT EXISTS boosts(id TEXT PRIMARY KEY,workspace TEXT,provider TEXT,model TEXT,effort TEXT,started REAL,expires REAL,minutes INTEGER);''')
    return db


def alive(pid):
    try: os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True


@contextmanager
def admission(config,provider,purpose,exclude_pane=None,interactive=False):
    if purpose not in ('managed','experiment'):raise ValueError('invalid budget purpose')
    p=settings(config)
    if not p['enabled']:
        yield None;return
    db=database();identifier=uuid.uuid4().hex
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            now=time.time()
            for row in db.execute('SELECT id,pid FROM claims WHERE finished IS NULL').fetchall():
                if not alive(row['pid']):db.execute('UPDATE claims SET finished=? WHERE id=?',(now,row['id']))
            check(config,provider,purpose,interactive=interactive)
            pool=pool_id(provider,config.get('routing',{}))
            claimed={r[0] for r in db.execute('SELECT id FROM claims WHERE finished IS NULL')}
            observed=observed_sessions(config,pool,claimed,exclude_pane)
            if purpose=='experiment':
                count=db.execute("SELECT count(*) FROM claims WHERE purpose='experiment' AND started>?",(now-86400,)).fetchone()[0]
                if count>=p['experiment_attempts_24h']:raise ValueError('budget: rolling 24-hour RSI attempt allowance exhausted')
                count=db.execute("SELECT count(*) FROM claims WHERE purpose='experiment' AND finished IS NULL").fetchone()[0]
                if count>=p['experiment_concurrency']:raise ValueError('budget: another measured experiment is running')
                if observed or db.execute("SELECT count(*) FROM claims WHERE purpose='managed' AND pool=? AND finished IS NULL",(pool,)).fetchone()[0]:
                    raise ValueError('budget: experiment waits behind managed sessions on this account')
            count=db.execute('SELECT count(*) FROM claims WHERE pool=? AND finished IS NULL',(pool,)).fetchone()[0]
            if count+observed>=p['managed_concurrency_per_pool']:raise ValueError('budget: shared-account concurrency limit reached')
            db.execute('INSERT INTO claims VALUES(?,?,?,?,?,NULL)',(identifier,purpose,pool,os.getpid(),now))
        try:yield identifier
        finally:
            with db:db.execute('UPDATE claims SET finished=? WHERE id=?',(time.time(),identifier))
    finally:db.close()


def boost(config, workspace, provider, now=None):
    if not settings(config)['enabled']:return None
    db=database()
    try:
        row=db.execute('SELECT * FROM boosts WHERE workspace=? AND provider=? AND expires>? ORDER BY started DESC LIMIT 1',
                       (workspace,provider,time.time() if now is None else now)).fetchone()
        return dict(row) if row else None
    finally:db.close()


def grant(config,workspace,provider,model,effort,minutes):
    p=settings(config)
    if not p['enabled']:raise ValueError('enable budget control first')
    if type(minutes) is not int or not 1<=minutes<=p['boost_max_minutes']:raise ValueError('boost exceeds per-boost time limit')
    _,root=configuration();validate(provider,model,effort,root)
    if effort not in ('low','medium','high'):raise ValueError('temporary boosts are capped at high effort')
    check(config,provider,purpose='boost')
    db=database()
    try:
        with db:
            db.execute('BEGIN IMMEDIATE');now=time.time()
            if db.execute('SELECT count(*) FROM boosts WHERE expires>?',(now,)).fetchone()[0]:raise ValueError('another temporary boost is active')
            used=db.execute('SELECT coalesce(sum(minutes),0) FROM boosts WHERE started>?',(now-86400,)).fetchone()[0]
            if used+minutes>p['boost_minutes_24h']:raise ValueError('rolling 24-hour boost allowance exhausted')
            identifier=uuid.uuid4().hex
            db.execute('INSERT INTO boosts VALUES(?,?,?,?,?,?,?,?)',(identifier,workspace,provider,model,effort,now,now+minutes*60,minutes))
        return {'id':identifier,'workspace':workspace,'provider':provider,'model':model,'effort':effort,'expires':now+minutes*60}
    finally:db.close()


def target(config,provider,difficulty,pressure=False,workspace=None,lead=False,now=None):
    model,effort=PROFILES[provider][difficulty]
    profiles=config.get('budget',{}).get('profiles',{})
    if not isinstance(profiles,dict):raise ValueError('budget profiles must be an object')
    provider_profiles=profiles.get(provider,{})
    if not isinstance(provider_profiles,dict):raise ValueError('provider profiles must be an object')
    override=provider_profiles.get(difficulty,{})
    if not isinstance(override,dict) or set(override)-{'model','effort'}:raise ValueError('invalid budget profile')
    model,effort=override.get('model',model),override.get('effort',effort)
    if override:
        _,root=configuration();validate(provider,model,effort,root)
    if pressure:effort={'medium':'low','high':'medium'}.get(effort,effort)
    temporary=boost(config,workspace,provider,now) if lead and workspace and not pressure else None
    if temporary:model,effort=temporary['model'],temporary['effort']
    return {'provider':provider,'model':model,'effort':effort}


def launch_settings(config,provider,model,effort,workspace,slot):
    # Existing acknowledged/manual selections and handover targets must remain exact.
    from assignments import root_for
    root=root_for(workspace)
    manifest=read_object(root/'workspace.json')
    mode=manifest.get('routing',{}).get('effort_mode',config.get('routing',{}).get('effort_mode','fixed'))
    selected=read_object(root/'.coordination/agents.json').get('active',{}).get(slot)
    if mode=='balanced' and not selected and not os.environ.get('AW_HANDOVER_TARGET'):
        _,pools=capacities(config)
        pool=pools.get(pool_id(provider,config.get('routing',{})),{})
        return target(config,provider,'standard',under_pressure(config,pool))
    return dict(provider=provider,model=model,effort=effort)


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('status')
    g=sub.add_parser('check');g.add_argument('provider')
    b=sub.add_parser('boost');b.add_argument('workspace');b.add_argument('provider');b.add_argument('--model',required=True);b.add_argument('--effort',default='high');b.add_argument('--minutes',type=int,default=10)
    g=sub.add_parser('resolve');g.add_argument('provider');g.add_argument('model');g.add_argument('effort');g.add_argument('workspace');g.add_argument('slot')
    r=sub.add_parser('launch');r.add_argument('provider');r.add_argument('command',nargs=argparse.REMAINDER)
    args=p.parse_args();config,_=configuration()
    if args.action=='check':
        check(config,args.provider);return 0
    if args.action=='resolve':result=launch_settings(config,args.provider,args.model,args.effort,args.workspace,args.slot)
    elif args.action=='boost':
        from assignments import root_for
        root=root_for(args.workspace)
        if read_object(root/'workspace.json').get('routing',{}).get('effort_mode',config['routing'].get('effort_mode'))!='balanced':raise ValueError('temporary boosts require Balanced Auto')
        result=grant(config,args.workspace,args.provider,args.model,args.effort,args.minutes)
    elif args.action=='launch':
        command=args.command[1:] if args.command[:1]==['--'] else args.command
        if not command:raise ValueError('missing provider command')
        with admission(config,args.provider,'managed',exclude_pane=os.environ.get('TMUX_PANE'),interactive=True) as claim:
            if claim and os.environ.get('TMUX_PANE'):
                try:subprocess.run(['tmux','set-option','-p','-t',os.environ['TMUX_PANE'],'@aw_budget_claim',claim],capture_output=True,timeout=3)
                except (OSError,subprocess.SubprocessError):pass
            # Keep the provider's PID as the lease owner, even if its launcher dies.
            os.execvp(command[0],command)
    else:
        db=database()
        try:result={'settings':settings(config),'pools':capacities(config)[1],
            'experiment_claims_24h':db.execute("SELECT count(*) FROM claims WHERE purpose='experiment' AND started>?",(time.time()-86400,)).fetchone()[0],
            'active_claims':[dict(r) for r in db.execute('SELECT purpose,pool,pid,started FROM claims WHERE finished IS NULL') if alive(r['pid'])],
            'boost_minutes_used_24h':db.execute('SELECT coalesce(sum(minutes),0) FROM boosts WHERE started>?',(time.time()-86400,)).fetchone()[0],
            'active_boosts':[dict(r) for r in db.execute('SELECT * FROM boosts WHERE expires>?',(time.time(),))],
            'limitation':'Admission and checkpoint controls, not per-turn billing caps; unmanaged sessions are not controlled.'}
        finally:db.close()
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,sqlite3.Error,KeyError,ChangeError) as error:
        print(str(error) if str(error).startswith('budget:') else 'budget: '+str(error),file=sys.stderr);sys.exit(2)
