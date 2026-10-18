"""Deliver prepared workflow prompts only after task-specific configuration acknowledgement."""
import json
import subprocess
import sys
from pathlib import Path
from assignments import atomic, locked, now, ChangeError, PENDING
from capacity import read_object
from routing import Router
import measurement


def command(argv):
    return subprocess.check_output(argv,text=True,stderr=subprocess.PIPE).strip()


def deliver(path, cli):
    path=Path(path).resolve();root=path.parent.parent
    with locked(root/'.coordination/.review-dispatch.lock'):
        state=read_object(path/'state.json')
        if state.get('status')!='awaiting_configuration':return {'status':state.get('status'),'sent':[]}
        slots=[s for s,r in state['roles'].items() if r['state']!='deferred']
        router=Router(root,cli)
        reasons=router.readiness(path,slots)
        for slot in slots:
            reasons.extend(slot + ': ' + reason for reason in measurement.preflight(path, slot)['reasons'])
        # Serialize final readiness and delivery with manual/automatic handovers.
        with locked(root/'.coordination/.agents.lock'):
            for request in router.store.load().get('requests',{}).values():
                if request['slot'] in slots and request['phase'] in PENDING:
                    reasons.append(request['slot']+': pending handover')
            sessions={}
            for line in command(['tmux','list-sessions','-F','#{session_name}\t#{@aw_workspace_id}\t#{@aw_role}']).splitlines():
                parts=line.split('\t')
                if len(parts)==3 and parts[1]==state['workspace_id']:sessions[parts[2]]=parts[0]
            for slot in slots:
                provider=router.current(slot,state,router.store.load())['provider']
                # Admission is independent of routing off/recommend preferences.
                import budget
                try:budget.check(router.config,provider)
                except ValueError as error:reasons.append(str(error))
                if slot not in sessions:reasons.append(slot+': CLI session missing');continue
                if subprocess.run(['agent-workspaces-pane-active',provider,sessions[slot]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
                    reasons.append(slot+': CLI is not running');continue
                capture=command(['tmux','capture-pane','-p','-t',sessions[slot],'-S','-30'])
                # Reuse the same tested composer detection as automatic reviewers.
                check=subprocess.run(['bash','-c','source "$1"; aw_orchestrator_prompt_idle "$2"','dispatch',str(Path(__file__).with_name('core.sh')),provider],input=capture,text=True)
                if check.returncode:reasons.append(slot+': CLI is busy or contains a draft')
            if reasons:
                if state.get('dispatch',{}).get('waiting')!=reasons:
                    with locked(path/'.state.lock'):
                        current=read_object(path/'state.json')
                        if current.get('status')=='awaiting_configuration':
                            current.setdefault('dispatch',{})['waiting']=reasons;atomic(path/'state.json',current)
                return {'status':'awaiting_configuration','waiting':reasons,'sent':[]}
            # Once claimed, a crash must be inspected rather than sending duplicate work.
            with locked(path/'.state.lock'):
                state=read_object(path/'state.json')
                if state.get('status')!='awaiting_configuration':return {'status':state.get('status'),'sent':[]}
                state['status']='dispatching';state['dispatch']={'waiting':[],'sent':[],'started_at':now()};atomic(path/'state.json',state)
            for slot in slots:
                provider=router.current(slot,state,router.store.load())['provider']
                guide={'codex':'AGENTS.md','claude':'CLAUDE.md','grok':'GROK.md'}[provider]
                prompt=(f'Read {path}/PROTOCOL.md, {path}/instructions/{guide}, and {path}/task.md. '
                        f'Follow your {slot} assignment; work only in {state["roles"][slot]["worktree"]}. '
                        f'Record state with agent-workspaces status --task {str(path)!r} --role {slot!r} '
                        '--state in_progress --summary started. Finish with completed or blocked and structured commits, changed_files and evidence. '
                        'Do not modify shared task files or another role status.')
                measurement.start(path, slot, router.current(slot,state,router.store.load()))
                buffer='aw-dispatch-'+state['id']+'-'+slot
                command(['tmux','set-buffer','-b',buffer,'--',prompt])
                try:
                    command(['tmux','paste-buffer','-b',buffer,'-t',sessions[slot]])
                    import time
                    time.sleep(.35);command(['tmux','send-keys','-t',sessions[slot],'Enter'])
                    if provider=='codex':time.sleep(.65);command(['tmux','send-keys','-t',sessions[slot],'Enter'])
                finally:command(['tmux','delete-buffer','-b',buffer])
                measurement.record(path, slot, 'delivered')
                with locked(path/'.state.lock'):
                    current=read_object(path/'state.json')
                    current['dispatch']['sent'].append(slot)
                    atomic(path/'state.json',current)
            # Merge status updates already written by fast workers instead of replacing them.
            with locked(path/'.state.lock'):
                current=read_object(path/'state.json');current['status']='active'
                current['dispatch']['completed_at']=now()
                current.setdefault('transitions',[]).append(dict(at=now(),actor='dispatcher:configuration-ready',from_='awaiting_configuration',to='active'))
                current['transitions'][-1]['from']=current['transitions'][-1].pop('from_')
                atomic(path/'state.json',current)
            if state.get('parent_id'):
                parent=path.parent/state['parent_id']/'state.json'
                with locked(parent.parent/'.state.lock'):
                    previous=read_object(parent)
                    if previous:
                        old=previous['status'];previous.update(status='completed',child_id=state['id'],updated_at=now())
                        previous.setdefault('gates',{})['triage_approved' if state['stage']=='execute' else 'execution_approved']=True
                        previous.setdefault('transitions',[]).append({'from':old,'to':'completed','at':now(),'actor':'dispatcher:child-started'})
                        atomic(parent,previous)
            subprocess.run([cli,'audit',str(path),'task.dispatched','dispatcher',json.dumps({'task_id':state['id'],'roles':slots})],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            return {'status':'active','sent':slots}

if __name__=='__main__':
    try:print(json.dumps(deliver(sys.argv[1],sys.argv[2])))
    except (OSError,ValueError,KeyError,ChangeError,subprocess.SubprocessError) as error:
        print('dispatch: '+str(error),file=sys.stderr);sys.exit(1)
