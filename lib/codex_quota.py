"""Quota-only fallback for native collectors with buffered stdio RPC timeouts."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time

class RPC:
    def __init__(self, process):
        self.process=process
        self.buffer=b''

    def request(self, identifier, method, params=None, timeout=15):
        self.process.stdin.write((json.dumps(dict(id=identifier,method=method,params=params or {}))+'\n').encode())
        self.process.stdin.flush()
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            while b'\n' in self.buffer:
                line,self.buffer=self.buffer.split(b'\n',1)
                message=json.loads(line)
                if message.get('id')==identifier:
                    if 'error' in message:raise ValueError('quota RPC rejected '+method)
                    return message['result']
            if not select.select([self.process.stdout],[],[],min(.25,max(0,deadline-time.monotonic())))[0]:continue
            data=os.read(self.process.stdout.fileno(),65536)
            if not data:raise ValueError('quota RPC closed')
            self.buffer+=data
            if len(self.buffer)>4*1024*1024:raise ValueError('quota RPC response too large')
        raise TimeoutError(method)


def parse(result,now):
    """Subscription windows and a sanitized credit observation from account/rateLimits/read.

    Only the documented fields are copied; identifiers and the raw response are never retained.
    Validation of the credit fields happens where they are consumed (capacity.normalize_credits).
    """
    limits=result.get('rateLimits') if isinstance(result,dict) else None
    if not isinstance(limits,dict):raise ValueError('invalid quota')
    windows=[]
    for name in ('primary','secondary'):
        window=limits.get(name)
        if not window:continue
        used=window['usedPercent']
        if isinstance(used,bool) or not isinstance(used,(float,int)) or not 0<=used<=100:raise ValueError('invalid quota')
        reset=datetime.fromtimestamp(window['resetsAt'],timezone.utc)
        if reset<=now:raise ValueError('expired quota')
        windows.append(dict(label=name,percent=used/100,resetsAt=reset.isoformat()))
    if not windows:raise ValueError('no authoritative limits')
    allowed=result.get('ordinaryUsageAllowed')
    spend=limits.get('spendControlReached',result.get('spendControlReached'))
    # An explicit account denial overrides otherwise permissive windows. A spend
    # control only denies paid usage, which the credit observation records.
    if allowed is False:
        windows.append(dict(label='account restriction',percent=1))
    credits=limits.get('credits')
    credits=credits if isinstance(credits,dict) else {}
    observation=dict(provider='codex',source='codex-app-server',unit='credits',observedAt=now.isoformat(),
                     ordinaryUsageAllowed=allowed,spendControlReached=spend,
                     **{key:credits[key] for key in ('hasCredits','unlimited','balance') if key in credits})
    return windows,observation


def query():
    process=subprocess.Popen(['codex','-s','read-only','-a','on-request','app-server'],
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        rpc=RPC(process)
        rpc.request(1,'initialize',{'clientInfo':{'name':'agent-workspaces-quota','version':'1'}})
        process.stdin.write(b'{"method":"initialized","params":{}}\n');process.stdin.flush()
        return parse(rpc.request(2,'account/rateLimits/read'),datetime.now(timezone.utc))
    finally:
        process.terminate()
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        process.stdin.close();process.stdout.close()


def write_atomic(path,record,prefix):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix=prefix)
    try:
        with os.fdopen(fd,'w') as stream:json.dump(record,stream)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def refresh():
    from capacity import read_object, subscription_row
    root=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'omarchy/agents/usage'
    # Credits and permissions are not part of the native subscription-only record,
    # so they are refreshed on every run even when native limits are fresh.
    limits,credits=query()
    # The owned overlay carries the same observation's limits, so a newer authoritative reading is never
    # discarded behind a still-fresh native cache; capacity uses whichever is newer (ties: more constrained).
    write_atomic(root.parent/'agent-workspaces-credits'/'codex.json',dict(credits,limits=limits),'.codex-credits-')
    path=root/'codex.json';record=read_object(path)
    if subscription_row('codex',record,datetime.now(timezone.utc),300)['state']!='unknown':return
    record.update(schemaVersion=1,id='codex',name='Codex',ready=True,limits=limits,
                  updatedAt=datetime.now(timezone.utc).isoformat(),usageStatusText='',authHelpText='')
    write_atomic(path,record,'.codex-quota-')

if __name__=='__main__':
    try:refresh()
    except (OSError,ValueError,KeyError,TypeError) as error:
        print('Codex quota unavailable: '+type(error).__name__,file=sys.stderr);sys.exit(1)
