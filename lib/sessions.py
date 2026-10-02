"""Select Codex conversations by verified worktree identity, never repository."""
import json
import os
from pathlib import Path
import sys


def discover(directory, preferred=''):
    """Return a local interactive session whose initial and latest cwd match."""
    directory = Path(directory).resolve()
    root = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
    candidates = []
    for path in root.rglob('*.jsonl'):
        try:
            with path.open() as stream:
                first = json.loads(next(stream))
                meta = first.get('payload', {})
                if (first.get('type') != 'session_meta' or
                        meta.get('source', 'cli') != 'cli' or
                        not meta.get('cwd') or Path(meta['cwd']).resolve() != directory):
                    continue
                identifier = meta.get('session_id') or meta.get('id')
                if not isinstance(identifier, str) or not identifier:
                    continue
                latest = meta['cwd']
                for line in stream:
                    if '"turn_context"' not in line and '"session_meta"' not in line:
                        continue
                    event = json.loads(line)
                    if event.get('type') in ('turn_context', 'session_meta'):
                        latest = event.get('payload', {}).get('cwd', latest)
                if not latest or Path(latest).resolve() != directory:
                    continue
                candidates.append((identifier == preferred, path.stat().st_mtime_ns, identifier))
        except (OSError, ValueError, TypeError, AttributeError, StopIteration):
            continue
    return max(candidates)[2] if candidates else ''


if __name__ == '__main__':
    print(discover(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ''))
