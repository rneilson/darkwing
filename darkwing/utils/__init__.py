import os
from pathlib import Path

def probably_root():
    '''
    Checks if current process is root, or functionally
    equivalent (ie inside a container).
    '''
    euid = os.geteuid()
    if euid != 0:
        return False
    # Now we have to check if we're in a user namespace
    # other than that of PID 1
    pid = os.getpid()
    user_ns = os.readlink(Path('/proc', str(pid), 'ns', 'user'))
    try:
        root_ns = os.readlink(Path('/proc/1/ns/user'))
    except (OSError, PermissionError):
        # Permission error *definitely* means we're not root
        return False
    # If our user namespace is equivalent to PID 1's namespace,
    # then as far as filesystem access (etc) goes, we're root
    return user_ns == root_ns
