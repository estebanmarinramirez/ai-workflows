#!/usr/bin/env python3
"""Create an isolated synthetic capture pilot; does not call any provider."""
import argparse
import json
from pathlib import Path
import subprocess

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('directory',type=Path)
p.add_argument('--provider',choices=('codex','claude'),required=True)
p.add_argument('--model',required=True)
p.add_argument('--effort',required=True)
a=p.parse_args()
root=a.directory.resolve();root.mkdir(mode=0o700)
repo=root/'source';repo.mkdir()
(repo/'calc.py').write_text('def add(a, b):\n    return a - b\n')
for args in (['init'],['add','.'],['-c','user.name=Dream pilot','-c','user.email=dream@localhost','-c','core.hooksPath=/dev/null','commit','-m','Synthetic broken arithmetic baseline']):
    subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
(root/'prompt.txt').write_text('Repair add(a,b) in calc.py to return the sum. Change only calc.py.\n')
(root/'verifier.py').write_text('''import importlib.util,json,pathlib,sys
request=json.load(open(sys.argv[1]))
path=pathlib.Path(request['worktree'])/'calc.py'
try:
    spec=importlib.util.spec_from_file_location('candidate',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    accepted=all(module.add(a,b)==a+b for a,b in [(1,2),(-1,4),(0,0)])
except (SyntaxError,AttributeError,TypeError):
    accepted=False
except Exception as error:
    print(type(error).__name__);sys.exit(2)
print('independent arithmetic acceptance:',accepted)
sys.exit(0 if accepted else 1)
''')
spec={'schema_version':1,'data_kind':'synthetic','evaluation_protocol':'binary-acceptance-v1',
      'agent':{'provider':a.provider,'model':a.model,'effort':a.effort},
      'timeout_seconds':120,'max_attempts_per_world':4,
      'objective':{'quality':1,'cost_usd':0,'wall_seconds':.001,'attempts':.01},
      'incumbent':'breadth','policies':[
          {'id':'breadth','strategy':'breadth','workers':1,'rounds':2},
          {'id':'refine','strategy':'best_leaf','workers':1,'rounds':2,'stop_score':1}],
      'worlds':[{'id':'smoke','instance_id':'arithmetic-smoke','split':'development',
                 'repository':'source','prompt_file':'prompt.txt','verifier':'verifier.py'}]}
(root/'spec.json').write_text(json.dumps(spec,indent=2)+'\n')
print(root/'spec.json')
