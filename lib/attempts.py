"""Explicit measured task attempts with frozen independent verifier artifacts."""
import argparse
import fcntl
import subprocess
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid
import provider_adapter as adapter
import telemetry

def ingest(directory):
    data,ledger_dir,config=telemetry.roots()
    if config.exists() and telemetry.read(config).get('telemetry',{}).get('enabled',True) is False:
        raise ValueError('telemetry disabled; artifacts retained without ledger ingestion')
    records=json.loads((directory/'receipts.json').read_text())
    ledger=telemetry.Ledger(ledger_dir)
    try:
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            for record in records: telemetry.receipt(ledger,record)
    finally: ledger.close()
    return len(records)

def run_locked(args):
    task=args.task.resolve(); state=telemetry.read(task/'state.json')
    role=state.get('roles',{}).get(args.role)
    if not isinstance(role,dict): raise ValueError('role is not in this task')
    root=task.parent.parent
    data,_,_=telemetry.roots()
    workspace=str(root.relative_to(data.resolve()))
    worktree=Path(role.get('worktree') or root/args.role).resolve()
    if not worktree.is_dir() or not worktree.is_relative_to(root.resolve()): raise ValueError('worktree must belong to this workspace')
    verifier=args.verifier.resolve()
    if not verifier.is_file() or verifier.is_relative_to(worktree): raise ValueError('verifier must be a Python file outside the agent worktree')
    if args.timeout<1: raise ValueError('timeout must be positive')
    # Explicit new attempt, never attach to the user's running provider session.
    identifier=uuid.uuid4().hex
    directory=task/'attempts'/identifier;directory.mkdir(parents=True,mode=0o700)
    frozen=directory/'verifier.py';shutil.copyfile(verifier,frozen)
    verifier_hash=adapter.sha(frozen)
    settings={'provider':args.provider,'model':args.model,'effort':args.effort}
    prompt=args.prompt_file.read_text()
    request={'schema_version':1,'attempt_id':identifier,'task':state,'role':args.role,'worktree':str(worktree),'artifacts':str(directory)}
    adapter.write(directory/'request.json',request)
    base={'workspace':workspace,'task':task.name,'role':args.role,'attempt_id':identifier,'source':adapter.VERSION}
    receipt=adapter.run_role(settings,worktree,directory/'provider',prompt,args.timeout)
    records=[dict(base,receipt_id=identifier+'-usage',kind='usage',at=receipt['at'],**settings,
                  model_version=receipt.get('model_version'),input_tokens=receipt.get('input_tokens'),
                  output_tokens=receipt.get('output_tokens'),cached_input_tokens=receipt.get('cached_input_tokens'),
                  cost_usd=receipt.get('cost_usd'),wall_seconds=receipt.get('wall_seconds'),evidence_sha256=receipt.get('evidence_sha256'))]
    failure=None;accepted=False
    if receipt['status']=='completed':
        try:
            if adapter.sha(frozen)!=verifier_hash: raise ValueError('frozen verifier changed')
            adapter.write(directory/'request.json',request)
            code,timed_out,wall=adapter.bounded([sys.executable,str(frozen),str(directory/'request.json')],worktree,directory/'verifier.log',directory/'verifier-stderr.log',args.timeout)
            if adapter.sha(frozen)!=verifier_hash: raise ValueError('verifier changed during execution')
            evidence={'verifier_sha256':verifier_hash,'stdout_sha256':adapter.sha(directory/'verifier.log'),
                      'stderr_sha256':adapter.sha(directory/'verifier-stderr.log'),'exit_code':code,'timed_out':timed_out,'wall_seconds':wall}
            adapter.write(directory/'verification.json',evidence)
            if not timed_out and code in (0,1):
                accepted=code==0
                records.append(dict(base,receipt_id=identifier+'-verification',kind='verification',at=adapter.stamp(),accepted=accepted,
                                    verifier_id=verifier.name,verifier_version=verifier_hash,evidence_sha256=adapter.sha(directory/'verification.json'),
                                    exit_code=code,wall_seconds=wall))
                if not accepted: failure='quality'
            else: failure='timeout' if timed_out else 'infrastructure'
        except KeyboardInterrupt: failure='cancelled'
        except (OSError,ValueError): failure='infrastructure'
    else:
        failure={'timeout':'timeout','cancelled':'cancelled','protocol_or_launch_error':'protocol'}.get(receipt['status'],'unknown')
    if failure:
        records.append(dict(base,receipt_id=identifier+'-failure',kind='failure',at=adapter.stamp(),failure_category=failure))
    adapter.write(directory/'receipts.json',records)
    adapter.write(directory/'outcome.json',{'accepted':accepted,'failure_category':failure,'attempt_id':identifier})
    ingest(directory)
    print(json.dumps({'attempt':str(directory),'accepted':accepted,'failure_category':failure},indent=2))
    return 0 if accepted else 1

def run(args):
    task=args.task.resolve()
    if not (task/'state.json').is_file(): raise ValueError('missing task state')
    state=telemetry.read(task/'state.json')
    if args.role not in state.get('roles',{}): raise ValueError('unknown role')
    # Key is hashed to prevent a role name becoming a lock-file path.
    lockname=telemetry.digest(args.role)
    with (task.parent/('.attempt-'+lockname+'.lock')).open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('another measured attempt owns this workspace role')
        assignments=task.parent/'agents.json'
        if assignments.exists():
            changes=telemetry.read(assignments).get('requests',{}).values()
            if any(v.get('slot')==args.role and v.get('phase') in ('requested','ready','launching','awaiting_ack') for v in changes):
                raise ValueError('role has a pending handover')
        try:
            sessions=subprocess.run(['tmux','list-sessions','-F','#{session_name}\t#{@aw_workspace_id}\t#{@aw_role}'],capture_output=True,text=True,timeout=5)
            for line in sessions.stdout.splitlines():
                parts=line.split('\t')
                if len(parts)==3 and parts[1]==state.get('workspace_id') and parts[2]==args.role:
                    # A managed session may contain a live interactive agent. Do
                    # not guess from its screen or inject another turn into it.
                    raise ValueError('role already has a managed session; use an idle workspace role')
        except FileNotFoundError: pass
        return run_locked(args)

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    r=sub.add_parser('run');r.add_argument('task',type=Path)
    for key in ('role','provider','model','effort'):r.add_argument('--'+key,required=True)
    r.add_argument('--prompt-file',required=True,type=Path);r.add_argument('--verifier',required=True,type=Path)
    r.add_argument('--timeout',type=int,default=180)
    i=sub.add_parser('ingest');i.add_argument('directory',type=Path)
    a=p.parse_args()
    if a.command=='ingest': print(json.dumps({'receipts':ingest(a.directory)}));return 0
    return run(a)

if __name__=='__main__':
    try:sys.exit(main())
    except (OSError,ValueError,KeyError,sqlite3.Error,subprocess.SubprocessError) as error:
        print('attempts: '+str(error),file=sys.stderr);sys.exit(2)
