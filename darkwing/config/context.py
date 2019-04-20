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
        configs_base, _ = default_base_paths(rootless, uid)
        dirs = [cwd_base, configs_base]

    for dirp in dirs:
        context_path = (Path(dirp) / name).with_suffix('.toml')
        if context_path.exists():
            return toml.load(context_path), context_path

    return None, None

def make_context_config(name='default', rootless=None, ouid=None,
                        ogid=None, configs_dir=None, storage_dir=None,
                        base_domain=None, write_file=True):
    if rootless is None:
        rootless = not probably_root()

    euid = os.geteuid()
    egid = os.getegid()

    if ouid is None:
        ouid = euid
    if ogid is None:
        ogid = egid

    if not configs_dir or not storage_dir:
        configs_base, storage_base = default_base_paths(rootless, ouid)
    if configs_dir:
        configs_base = Path(configs_dir)
    if storage_dir:
        storage_base = Path(storage_dir)

    context_path = (Path(configs_base) / name).with_suffix('.toml')
    do_chown = ouid != euid or ogid != egid
    cuid, cgid = (ouid, ogid) if do_chown else (None, None)

    # Start with default config
    # TODO: insert/compare other config elements
    context = default_context(
        name=name, rootless=rootless, ouid=ouid, ogid=ogid,
        configs_dir=configs_dir, storage_dir=storage_dir,
        base_domain=base_domain,
    )

    if write_file:
        # Ensure all context's subdirs exist
        dirs = [
            (configs_base, 0o775),
            (storage_base, 0o775),
            (Path(context['configs']['base']), 0o775),
            (Path(context['configs']['secrets']), 0o770),
            (Path(context['storage']['images']), 0o775),
            (Path(context['storage']['containers']), 0o770),
            (Path(context['storage']['volumes']), 0o770),
        ]
        ensure_dirs(dirs, uid=cuid, gid=cgid)

        # Write context to file
        # Touch mostly to raise FileExistsError
        context_path.touch(mode=0o664, exist_ok=False)
        if do_chown:
            os.chown(context_path, cuid, cgid)
        context_path.write_text(toml.dumps(context))

    return context, context_path
