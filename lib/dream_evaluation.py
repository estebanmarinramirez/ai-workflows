"""Local, transactional held-out exposure ledger. Never deletes an interrupted evaluation."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3


def holdout_keys(worlds):
    # Evidence/protocol changes must not make the same task a fresh holdout.
    return [hashlib.sha256(json.dumps(['dream-heldout-task-v1',w['instance_id']]).encode()).hexdigest()
            for w in worlds if w['split']=='heldout']


@contextmanager
def evaluation(identifier, instances):
    root=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'agent-workspaces/dream-evaluation'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=root/'ledger.sqlite'
    db=sqlite3.connect(path,timeout=10);os.chmod(path,0o600)
    db.executescript('CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY, result TEXT);'
                     'CREATE TABLE IF NOT EXISTS exposures(instance TEXT PRIMARY KEY, evaluation TEXT NOT NULL);')
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT result FROM evaluations WHERE id=?',(identifier,)).fetchone()
            if old is not None:
                if old[0] is None:raise ValueError('interrupted evaluation retained; use fresh held-out tasks')
                cached=json.loads(old[0])
            else:
                cached=None
                if any(db.execute('SELECT 1 FROM exposures WHERE instance=?',(x,)).fetchone() for x in instances):
                    raise ValueError('held-out tasks already exposed; use fresh held-out tasks')
                db.execute('INSERT INTO evaluations VALUES(?,NULL)',(identifier,))
                db.executemany('INSERT INTO exposures VALUES(?,?)',[(x,identifier) for x in instances])
        holder={'cached':cached,'result':None}
        yield holder
        if cached is None and holder['result'] is not None:
            with db:db.execute('UPDATE evaluations SET result=? WHERE id=?',
                               (json.dumps(holder['result'],allow_nan=False),identifier))
    finally:db.close()
