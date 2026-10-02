"""Deterministic plumbing demo, NOT a simulation of model quality or team behavior."""
import json
from pathlib import Path
import sys

request = json.loads(Path(sys.argv[1]).read_text())
path = Path(request['worktree'])/'arithmetic.py'
text = path.read_text()
if request['task']['id'] == 'clamp':
    text = text.replace('return value', 'return min(upper, max(lower, value))')
else:
    text = text.replace('return numerator / denominator', 'return None if denominator == 0 else numerator / denominator')
path.write_text(text)
# No provider calls or real role scheduling occur in this demo.
usage = {'roles': {role: {**settings, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0}
                   for role, settings in request['configuration']['roles'].items()},
         'coordination_seconds': 0, 'recovery_seconds': 0, 'human_seconds': 0,
         'handover_failures': 0, 'retries': 0}
Path(sys.argv[2]).write_text(json.dumps(usage))
print('Deterministic demo patch applied; no model was invoked.')
