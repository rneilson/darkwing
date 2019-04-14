import os
import toml
from pathlib import Path

from darkwing.utils import probably_root
from .defaults import default_base_paths, default_context

def get_context_config(name='default', dirs=None, rootless=None, uid=None):
    if rootless is None:
        rootless = not probably_root()

    if dirs is None:
        cwd_base = Path.cwd() / '.darkwing'
        cfg_base, _, _ = default_base_paths(rootless=rootless, uid=uid)
        dirs = [cwd_base, cfg_base]

    for dirp in dirs:
        ctx_path = (Path(dirp) / name).with_suffix('.toml')
        if ctx_path.exists():
            return toml.load(ctx_path), ctx_path

    return None, None

def make_context_config(name='default', rootless=None, uid=None, gid=None):
    if rootless is None:
        rootless = not probably_root()

    euid = os.geteuid()
    egid = os.getegid()

    if uid is None:
        uid = euid
    if gid is None:
        gid = egid

    cfg_base, sto_base, _ = default_base_paths(rootless=rootless, uid=uid)
    ctx_path = (Path(cfg_base) / name).with_suffix('.toml')
    do_chown = uid != euid or gid != egid

    # Create any parent dir(s)
    if not cfg_base.exists():
        cfg_base.mkdir(mode=0o775, parents=True, exist_ok=True)
        if do_chown:
            os.chown(cfg_base, uid, gid)
    if not sto_base.exists():
        sto_base.mkdir(mode=0o775, parents=True, exist_ok=True)
        if do_chown:
            os.chown(sto_base, uid, gid)

    # Touch mostly to raise FileExistsError
    ctx_path.touch(mode=0o664, exist_ok=False)
    if do_chown:
        os.chown(ctx_path, uid, gid)

    context = default_context(name, rootless=rootless, uid=uid, gid=gid)
    ctx_path.write_text(toml.dumps(context))

    return context, ctx_path
