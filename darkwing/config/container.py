import os
import toml
from pathlib import Path

from darkwing.utils import probably_root
from .defaults import default_base_paths, default_container

def get_container_config(name, context_name='default',
                         dirs=None, rootless=None, uid=None):
    if rootless is None:
        rootless = not probably_root()

    if dirs is None:
        cwd_base = Path.cwd() / '.darkwing'
        cfg_base, _ = default_base_paths(rootless=rootless, uid=uid)
        dirs = [cwd_base, cfg_base]

    for dirp in dirs:
        cfg_path = (Path(dirp) / context_name / name).with_suffix('.toml')
        if cfg_path.exists():
            return toml.load(cfg_path), cfg_path

    return None, None

def make_container_config(name, context, image=None,
                          tag='latest', uid=0, gid=0):
    euid = os.geteuid()
    egid = os.getegid()
    
    cfg_base = Path(context['configs']['base'])
    cfg_path = (cfg_base / name).with_suffix('.toml')

    owner = context['owner']
    do_chown = owner['uid'] != euid or owner['gid'] != egid

    # Create config parent dir(s)
    if not cfg_base.exists():
        cfg_base.mkdir(mode=0o775, parents=True)
        if do_chown:
            os.chown(cfg_base, uid, gid)

    # Touch mostly to raise FileExistsError
    cfg_path.touch(mode=0o664, exist_ok=False)
    if do_chown:
        os.chown(cfg_path, uid, gid)

    # TODO: insert/compare other config elements

    # Write config to file
    container = default_container(
        name, context, image=image, tag=tag, uid=uid, gid=gid
    )
    cfg_path.write_text(toml.dumps(container))

    # Required storage dirs
    dirs = [
        (Path(container['storage']['base']), 0o770),
        (Path(container['storage']['secrets']), 0o700),
        (Path(container['volumes']['private']), 0o770),
    ]
    # Now ensure all created
    for dir_path, dir_mode in dirs:
        if not dir_path.exists():
            dir_path.mkdir(mode=dir_mode, parents=True)
            if do_chown:
                os.chown(dir_path, owner['uid'], owner['gid'])

    return container, cfg_path
