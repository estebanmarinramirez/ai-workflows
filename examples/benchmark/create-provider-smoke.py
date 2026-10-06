#!/usr/bin/env python3
"""Create a small real-CLI smoke manifest; does not invoke providers itself."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('directory',type=Path);p.add_argument('--provider',choices=['codex','claude'],default='codex')
p.add_argument('--model',required=True);p.add_argument('--effort',default='low')
a=p.parse_args();root=a.directory.resolve()
subprocess.run([sys.executable,str(Path(__file__).with_name('create-demo.py')),str(root)],check=True)
shutil.copyfile(Path(__file__).resolve().parents[2]/'lib/provider_adapter.py',root/'adapter.py')
m=json.loads((root/'manifest.json').read_text())
m.update(repeats=1,timeout_seconds=180,shadow={'enabled':False},data_kind='synthetic')
m['configurations']=[{'id':'provider-solo','topology':'solo','roles':{role:{'provider':a.provider,'model':a.model,'effort':a.effort} for role in ('coordinator','implementer')}}]
(root/'manifest.json').write_text(json.dumps(m,indent=2)+'\n')
print('Smoke workload only; real provider calls happen when benchmark run is invoked. Shadow learning disabled.')
