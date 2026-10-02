"""Independent checks run outside the adapter, against its resulting checkout."""
import importlib.util
import json
from pathlib import Path
import sys

request = json.loads(Path(sys.argv[1]).read_text())
spec = importlib.util.spec_from_file_location('arithmetic', Path(request['worktree'])/'arithmetic.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if request['task']['id'] == 'clamp':
    assert module.clamp(-5, 0, 10) == 0
    assert module.clamp(15, 0, 10) == 10
    assert module.clamp(5, 0, 10) == 5
else:
    assert module.divide(10, 0) is None
    assert module.divide(10, 2) == 5
print('Independent assertions passed.')
