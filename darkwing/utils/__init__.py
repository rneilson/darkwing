import os
import pwd
import grp
from pathlib import Path
from getpass import getuser

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

def user_ids(username=None, groupname=None):
    if username is None:
        username = getuser()

    user = pwd.getpwnam(username)
    uid = user.pw_uid
    gid = user.pw_gid
    # Allow specifying alternate group
    if groupname is not None:
        gid = grp.getgrnam(groupname).gr_gid

    return uid, gid

def get_runtime_dir(uid=None):
    # Give priority to XDG_RUNTIME_DIR
    xdg_dir = os.environ.get('XDG_RUNTIME_DIR')
    if xdg_dir:
        path = Path(xdg_dir)
    else:
        if uid is None:
            uid = os.geteuid()
        if uid:
            path = Path('/run/user') / str(uid)
        else:
            path = Path('/run')

    return path / 'darkwing'
