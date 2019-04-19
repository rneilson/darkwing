import os
import toml
from pathlib import Path

from darkwing.utils import probably_root, get_runtime_dir, ensure_dirs
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

def make_container_config(name, context, image=None, tag='latest', uid=0,
                          gid=0, euid=None, egid=None, write_file=True):
    if euid is None:
        euid = os.geteuid()
    if egid is None:
        egid = os.getegid()
    
    cfg_base = Path(context['configs']['base'])
    cfg_path = (cfg_base / name).with_suffix('.toml')

    owner = context['owner']
    do_chown = owner['uid'] != euid or owner['gid'] != egid
    ouid, ogid = (owner['uid'], owner['gid']) if do_chown else (None, None)

    # Start with default config
    # TODO: insert/compare other config elements
    container = default_container(
        name, context, image=image, tag=tag, uid=uid, gid=gid
    )

    if write_file:
        # Ensure all required config, storage dirs created
        dirs = [
            (cfg_base, 0o775),
            (Path(container['storage']['base']), 0o770),
            (Path(container['storage']['secrets']), 0o700),
            (Path(container['volumes']['private']), 0o770),
        ]
        ensure_dirs(dirs, uid=ouid, gid=ogid)

        # Write config to file
        # Touch mostly to raise FileExistsError
        cfg_path.touch(mode=0o664, exist_ok=False)
        if do_chown:
            os.chown(cfg_path, *owner_ids)
        cfg_path.write_text(toml.dumps(container))

    return container, cfg_path
