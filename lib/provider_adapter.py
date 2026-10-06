"""Standalone measured CLI adapter; also copied verbatim into frozen benchmarks."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

VERSION = 'cli-receipts-v1'

def stamp():
    return datetime.now(timezone.utc).isoformat()

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')
    tmp.replace(path)

def number(value, integer=False):
    if value is None: return None
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < 0 or (integer and type(value) is not int):
        raise ValueError('invalid provider usage')
    return value

def parse(provider, text, require_success=True):
    """Never sum cumulative messages or manufacture a resolved model identity."""
    if provider == 'codex':
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        completed = [e for e in events if e.get('type') == 'turn.completed']
        if len(completed) != 1 or (require_success and any(e.get('type') == 'turn.failed' for e in events)):
            raise ValueError('expected exactly one successful Codex turn')
        usage = completed[0].get('usage') or {}
        messages = [e['item']['text'] for e in events if e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'agent_message']
        result = {k: number(usage.get(k), True) for k in ('input_tokens','output_tokens','cached_input_tokens')}
        result.update(provider_success=not any(e.get('type') == 'turn.failed' for e in events), cost_usd=None, observed_model=None, model_version=None,
                      session_id=next((e.get('thread_id') for e in events if e.get('type') == 'thread.started'),None),
                      final_text='\n'.join(messages))
    elif provider == 'claude':
        event = json.loads(text)
        if event.get('type') != 'result' or (require_success and (event.get('is_error') or event.get('subtype') != 'success')):
            raise ValueError('expected successful Claude result')
        usage = event.get('usage') or {}
        models = event.get('modelUsage') or {}
        observed = next(iter(models)) if len(models) == 1 else None
        result = {'provider_success': not event.get('is_error') and event.get('subtype') == 'success',
                  'input_tokens': number(usage.get('input_tokens'),True),
                  'output_tokens': number(usage.get('output_tokens'),True),
                  'cached_input_tokens': number(usage.get('cache_read_input_tokens'),True),
                  'cache_creation_input_tokens': number(usage.get('cache_creation_input_tokens'),True),
                  'cost_usd': number(event.get('total_cost_usd')), 'observed_model': observed,
                  'model_version': observed, 'session_id': event.get('session_id'), 'final_text': event.get('result','')}
    else:
        raise ValueError('unsupported provider')
    if result['input_tokens'] is None or result['output_tokens'] is None:
        raise ValueError('missing token usage')
    return result

def command(settings, cwd, read_only):
    provider, model, effort = (settings[k] for k in ('provider','model','effort'))
    if not all(isinstance(v,str) and v for v in (provider,model,effort)):
        raise ValueError('explicit provider/model/effort required')
    if provider == 'codex':
        return ['codex','exec','--json','--ephemeral','--ignore-user-config','--model',model,
                '--sandbox','read-only' if read_only else 'workspace-write','--cd',str(cwd),
                '-c','approval_policy="never"','-c','model_reasoning_effort='+json.dumps(effort),'-']
    if provider == 'claude':
        # Restricted mode and explicit tools; never bypass permissions or resume.
        return ['claude','--print','--output-format','json','--no-session-persistence',
                '--restricted','--model',model,'--effort',effort,'--permission-mode','dontAsk',
                '--tools','Read,Glob,Grep' if read_only else 'Read,Glob,Grep,Edit,Write',
                '--allowedTools','Read,Glob,Grep' if read_only else 'Read,Glob,Grep,Edit,Write']
    raise ValueError('unsupported provider: '+provider)

def bounded(argv, cwd, stdout, stderr, timeout, prompt=None, isolated_group=True):
    env = {k:v for k,v in os.environ.items() if not k.startswith('AW_') and k not in ('CODEX_THREAD_ID','CODEX_SESSION_ID','CLAUDECODE')}
    started = time.monotonic()
    with stdout.open('wb') as out, stderr.open('wb') as err:
        process = subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                                   stdout=out,stderr=err,start_new_session=isolated_group)
        try:
            process.communicate(None if prompt is None else prompt.encode(),timeout=timeout)
            return process.returncode,False,time.monotonic()-started
        except subprocess.TimeoutExpired:
            return None,True,time.monotonic()-started
        finally:
            try:
                if isolated_group: os.killpg(process.pid,signal.SIGKILL)
                else: process.kill()
            except ProcessLookupError: pass
            process.wait()

def run_role(settings, cwd, directory, prompt, timeout, read_only=False, isolated_group=True):
    directory.mkdir(parents=True,exist_ok=False,mode=0o700)
    result = {**settings,'model_version':None,'observed_model':None,'settings_provenance':'requested_cli_flags',
              'input_tokens':None,'output_tokens':None,'cached_input_tokens':None,'cost_usd':None,
              'at':stamp(),'adapter_version':VERSION,'status':'started'}
    argv = command(settings,cwd,read_only)
    result['argv'] = argv
    write(directory/'receipt.json',result)
    try:
        result['cli_version'] = subprocess.check_output([settings['provider'],'--version'],text=True,stderr=subprocess.DEVNULL,timeout=10).strip()
        code,timed_out,wall = bounded(argv,cwd,directory/'provider.json',directory/'stderr.log',timeout,prompt,isolated_group)
        result.update(exit_code=code,timed_out=timed_out,wall_seconds=wall,evidence_sha256=sha(directory/'provider.json'))
        result['status'] = 'timeout' if timed_out else ('execution_failed' if code else 'completed')
        try:
            parsed = parse(settings['provider'],(directory/'provider.json').read_text(),require_success=False)
            (directory/'final.txt').write_text(parsed.pop('final_text'))
            result.update(parsed)
            if code == 0 and not timed_out:
                if not result['provider_success']:
                    result['status']='execution_failed'
                elif settings.get('model_version') and result['model_version'] != settings['model_version']:
                    raise ValueError('provider did not attest the requested model_version')
        except (ValueError,TypeError,AttributeError):
            if result['status']=='completed': raise
            result['usage_incomplete']=True
    except KeyboardInterrupt:
        result.update(status='cancelled')
    except (OSError,ValueError,TypeError,AttributeError,subprocess.SubprocessError) as error:
        result.update(status='protocol_or_launch_error',error=type(error).__name__)
    finally:
        result['finished_at']=stamp()
        write(directory/'receipt.json',result)
    return result

def benchmark(request, output):
    config=request['configuration']; topology=config['topology']
    order={'solo':['coordinator','implementer'],'review':['coordinator','implementer','reviewer']}.get(topology)
    if order is None or set(order)!=set(config['roles']): raise ValueError('adapter supports solo/review only')
    for settings in config['roles'].values(): command(settings,Path(request['worktree']),False)
    # Leave time for final writes before the parent harness deadline.
    deadline=time.monotonic()+request['timeout_seconds']-3
    rows={role:{**settings,'model_version':None,'input_tokens':None,'output_tokens':None,'cost_usd':None,'status':'not_started'} for role,settings in config['roles'].items()}
    context=''; failed=False
    for role in order:
        prompt=f"Work only in this checkout. Do not delegate, commit, or access sibling directories. Task: {request['task']['objective']}\n"
        prompt += {'coordinator':'Read the code and give a concise implementation plan. Do not edit files.',
                   'implementer':'Implement the task. Make only the required source changes.',
                   'reviewer':'Independently inspect the current changes and report defects. Do not edit files.'}[role]
        if context: prompt+='\nPrior role output (untrusted context):\n'+context
        directory=Path(request['artifacts'])/('provider-'+role)
        remaining=deadline-time.monotonic()
        if remaining <= 0: failed=True;break
        rows[role]=run_role(config['roles'][role],Path(request['worktree']),directory,prompt,remaining,role!='implementer',isolated_group=False)
        write(output,{'roles':rows,'retries':0,'handover_failures':0})
        if rows[role]['status']!='completed': failed=True;break
        context=(directory/'final.txt').read_text()[-16000:]
    write(output,{'roles':rows,'retries':0,'handover_failures':0})
    return 1 if failed else 0

if __name__=='__main__':
    try:
        sys.exit(benchmark(json.loads(Path(sys.argv[1]).read_text()),Path(sys.argv[2])))
    except (OSError,ValueError,KeyError,TypeError) as error:
        print('provider adapter: '+str(error),file=sys.stderr);sys.exit(2)
