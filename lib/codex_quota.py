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


def query():
    process=subprocess.Popen(['codex','-s','read-only','-a','on-request','app-server'],
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        rpc=RPC(process)
        rpc.request(1,'initialize',{'clientInfo':{'name':'agent-workspaces-quota','version':'1'}})
        process.stdin.write(b'{"method":"initialized","params":{}}\n');process.stdin.flush()
        result=rpc.request(2,'account/rateLimits/read')
        limits=result.get('rateLimits') or {}
        windows=[]
        for name in ('primary','secondary'):
            window=limits.get(name)
            if not window:continue
            used=window['usedPercent']
            if isinstance(used,bool) or not isinstance(used,(float,int)) or not 0<=used<=100:raise ValueError('invalid quota')
            reset=datetime.fromtimestamp(window['resetsAt'],timezone.utc)
            if reset<=datetime.now(timezone.utc):raise ValueError('expired quota')
            windows.append(dict(label=name,percent=used/100,resetsAt=reset.isoformat()))
        if not windows:raise ValueError('no authoritative limits')
        # Explicit account denial overrides otherwise permissive windows.
        if result.get('ordinaryUsageAllowed') is False or limits.get('spendControlReached') is True:
            windows.append(dict(label='account restriction',percent=1))
        return windows
    finally:
        process.terminate()
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        process.stdin.close();process.stdout.close()


def refresh():
    from capacity import read_capacity, read_object
    root=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'omarchy/agents/usage'
    # Avoid an extra account query when the native collector already works.
    if read_capacity('codex',root,datetime.now(timezone.utc),300)['state']!='unknown':return
    limits=query()
    path=root/'codex.json';record=read_object(path)
    record.update(schemaVersion=1,id='codex',name='Codex',ready=True,limits=limits,
                  updatedAt=datetime.now(timezone.utc).isoformat(),usageStatusText='',authHelpText='')
    root.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=root,prefix='.codex-quota-')
    try:
        with os.fdopen(fd,'w') as stream:json.dump(record,stream)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

if __name__=='__main__':
    try:refresh()
    except (OSError,ValueError,KeyError,TypeError) as error:
        print('Codex quota unavailable: '+type(error).__name__,file=sys.stderr);sys.exit(1)
