"""Independent behavioral check: cache identity pins evaluator implementation bytes."""
from contextlib import contextmanager
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

request = json.loads(Path(sys.argv[1]).read_text())
with tempfile.TemporaryDirectory() as temporary:
    library = Path(temporary) / 'lib'
    shutil.copytree(Path(request['worktree']) / 'lib', library)
    sys.path.insert(0, str(library))
    dream = importlib.import_module('dream')
    evaluation = importlib.import_module('dream_evaluation')
    @contextmanager
    def capture(identifier, keys):
        yield {'cached': None, 'result': None}
    document = {'data_kind': 'real', 'worlds': [{'instance_id': 'fixture', 'split': 'heldout', 'prompt_sha256': 'a'*64, 'verifier_sha256': 'b'*64}]}
    with patch.object(dream, 'validate'), patch.object(dream, '_optimize', return_value={}), patch.object(evaluation, 'evaluation', capture):
        first = dream.optimize(document, register=True)['evaluation_id']
        assert first == dream.optimize(document, register=True)['evaluation_id'], 'identical evaluator must reuse its identity'
        statistics = library / 'dream_statistics.py'
        statistics.write_text(statistics.read_text() + '\n# independent evaluator revision fixture\n')
        second = dream.optimize(document, register=True)['evaluation_id']
        assert first != second, 'changed evaluator incorrectly reuses previous evidence identity'
