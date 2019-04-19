import os
import toml
from pathlib import Path

from darkwing.utils import probably_root, ensure_dirs
from .defaults import default_base_paths, default_context

def get_context_config(name='default', dirs=None, rootless=None, uid=None):
    if rootless is None:
        rootless = not probably_root()

    if dirs is None:
        cwd_base = Path.cwd() / '.darkwing'
        cfg_base, _ = default_base_paths(rootless=rootless, uid=uid)
        dirs = [cwd_base, cfg_base]

    for dirp in dirs:
        ctx_path = (Path(dirp) / name).with_suffix('.toml')
        if ctx_path.exists():
            return toml.load(ctx_path), ctx_path

    return None, None

def make_context_config(name='default', rootless=None, uid=None, gid=None,
                        configs_dir=None, storage_dir=None, write_file=True):
    if rootless is None:
        rootless = not probably_root()

    euid = os.geteuid()
    egid = os.getegid()

    if uid is None:
        uid = euid
    if gid is None:
        gid = egid

    if not configs_dir or not storage_dir:
        cfg_base, sto_base = default_base_paths(rootless=rootless, uid=uid)
    if configs_dir:
        cfg_base = Path(configs_dir)
    if storage_dir:
        sto_base = Path(storage_dir)

    ctx_path = (Path(cfg_base) / name).with_suffix('.toml')
    do_chown = uid != euid or gid != egid
    ouid, ogid = (uid, gid) if do_chown else (None, None)

    # Start with default config
    # TODO: insert/compare other config elements
    context = default_context(
        name=name, rootless=rootless, uid=uid, gid=gid,
        configs_dir=configs_dir, storage_dir=storage_dir,
    )

    if write_file:
        # Ensure all context's subdirs exist
        dirs = [
            (cfg_base, 0o775),
            (sto_base, 0o775),
            (Path(context['configs']['base']), 0o775),
            (Path(context['configs']['secrets']), 0o770),
            (Path(context['storage']['images']), 0o775),
            (Path(context['storage']['containers']), 0o770),
            (Path(context['storage']['volumes']), 0o770),
        ]
        ensure_dirs(dirs, uid=ouid, gid=ogid)

        # Write context to file
        # Touch mostly to raise FileExistsError
        ctx_path.touch(mode=0o664, exist_ok=False)
        if do_chown:
            os.chown(ctx_path, uid, gid)
        ctx_path.write_text(toml.dumps(context))

    return context, ctx_path
