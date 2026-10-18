"""Independent no-provider checks of inventory health classification."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

request = json.loads(Path(sys.argv[1]).read_text())
sys.path.insert(0, str(Path(request['worktree']) / 'lib'))
import telemetry
with tempfile.TemporaryDirectory() as temporary:
    base = Path(temporary)
    env = dict(XDG_DATA_HOME=str(base/'data'), XDG_STATE_HOME=str(base/'state'), XDG_CONFIG_HOME=str(base/'config'))
    with patch.dict(os.environ, env):
        for code, stderr, expected, errors in (
            (0, '', 'available', False),
            (1, 'error connecting to /tmp/tmux-test/default (No such file or directory)', 'absent', False),
            (1, 'error connecting to /tmp/tmux-test/default (Permission denied)', 'unavailable', True),
        ):
            with patch.object(telemetry.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, '', stderr)):
                result = telemetry.collect()
            assert result['active_session_observation'] == expected, result
            assert bool(result['errors']) == errors, result
