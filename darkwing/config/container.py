import os
import toml
from pathlib import Path

from darkwing.utils import probably_root
from .defaults import default_base_paths, default_container

def get_container_config(name, context):
    cfg_base = context['configs']['base']
    con_path = (Path(cfg_base) / name).with_suffix('.toml')

    if con_path.exists():
        return toml.load(con_path), con_path

    return None, None

def make_container_config(name, context, image=None,
                          tag='latest', uid=0, gid=0):
    euid = os.geteuid()
    egid = os.getegid()
    
    cfg_base = Path(context['configs']['base'])
    sec_base = Path(context['configs']['secrets'])
    con_path = (cfg_base / name).with_suffix('.toml')
    cfg_user = context['user']
    do_chown = cfg_user['uid'] != euid or cfg_user['gid'] != egid

    # Create any parent dir(s)
    dirs = [
        (cfg_base, 0o775),
        (sec_base, 0o770),
    ]
    for dir_path, dir_mode in dirs:
        if not dir_path.exists():
            dir_path.mkdir(mode=dir_mode, parents=True)
            if do_chown:
                os.chown(dir_path, uid, gid)

    # Touch mostly to raise FileExistsError
    con_path.touch(mode=0o664, exist_ok=False)
    if do_chown:
        os.chown(con_path, uid, gid)

    # Write config to file
    container = default_container(
        name, context, image=image, tag=tag, uid=uid, gid=gid
    )
    con_path.write_text(toml.dumps(container))

    # Ensure any secrets dirs created
    for secret in container['secrets']:
        if secret.get('source') and secret.get('copy') is True:
            sec_path = Path(secret['source'])
            if not sec_path.exists():
                sec_path.mkdir(mode=0o770, parents=True)
                if do_chown:
                    os.chown(sec_path, cfg_user['uid'], cfg_user['gid'])

    return container, con_path
