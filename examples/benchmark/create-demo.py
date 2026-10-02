#!/usr/bin/env python3
"""Create a disposable benchmark source and manifest, without model invocations."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=False)
repo = root/'source'
repo.mkdir()
(repo/'arithmetic.py').write_text('def clamp(value, lower, upper):\n    return value\n\ndef divide(numerator, denominator):\n    return numerator / denominator\n')
for args in (['init','-q'], ['add','arithmetic.py'], ['-c','user.name=Benchmark','-c','user.email=benchmark@example.invalid','commit','-qm','Benchmark baseline']):
    subprocess.run(['git','-C',str(repo),*args],check=True)
for name in ('adapter.py','verifier.py'):
    shutil.copyfile(Path(__file__).parent/name, root/name)
roles = {
    'solo': ['implementer'], 'review': ['implementer','reviewer'],
    'parallel': ['implementer-a','implementer-b','verifier'],
    'investigate': ['investigator-a','investigator-b','adjudicator'],
}
manifest = {'schema_version':1, 'repository':'source', 'revision':'HEAD',
    'adapter':'adapter.py', 'verifier':'verifier.py', 'seed':42, 'repeats':2, 'timeout_seconds':10,
    'tasks':[{'id':'clamp','split':'development','objective':'Clamp values to the inclusive bounds.'},
             {'id':'divide','split':'heldout','objective':'Return None for a zero divisor.'}],
    'configurations':[{'id':name,'topology':name,'roles':{
        role:{'provider':'demo','model':'deterministic-fixture','effort':'none'}
        for role in ['coordinator',*workers]}} for name,workers in roles.items()]}
(root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(root/'manifest.json')
