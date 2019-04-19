import os
from pathlib import Path

def ensure_dirs(dirs, uid=None, gid=None):
    do_chown = uid is not None or gid is not None
    if do_chown:
        # Leave unchanged if not given
        if uid is None:
            uid = -1
        if gid is None:
            gid = -1

    created = []

    for dir_path, dir_mode in dirs:
        dir_path = Path(dir_path)
        if not dir_path.exists():
            dir_path.mkdir(mode=dir_mode, parents=True)
            if do_chown:
                os.chown(dir_path, uid, gid)
            created.append(dir_path)

    return created

def ensure_files(files, uid=None, gid=None):
    do_chown = uid is not None or gid is not None
    if do_chown:
        # Leave unchanged if not given
        if uid is None:
            uid = -1
        if gid is None:
            gid = -1

    created = []

    for file_path, file_mode in files:
        file_path = Path(file_path)
        if not file_path.exists():
            file_path.touch(mode=file_mode, exist_ok=False)
            if do_chown:
                os.chown(file_path, uid, gid)
            created.append(file_path)

    return created

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
