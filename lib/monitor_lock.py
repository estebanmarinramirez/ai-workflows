"""Exec a workspace monitor under a kernel lock that survives no reboot."""
import fcntl
import os
from pathlib import Path
import sys


def main():
    path=Path(sys.argv[1]);path.parent.mkdir(parents=True,exist_ok=True)
    # Keep a stable inode; unlinking a lock file permits concurrent owners.
    lock=path.open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:return 0
    os.set_inheritable(lock.fileno(),True)
    os.execv(sys.argv[2],sys.argv[2:])

if __name__=='__main__':sys.exit(main())
