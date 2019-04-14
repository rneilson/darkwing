import os
import toml
from pathlib import Path

from darkwing.utils import probably_root
from .defaults import default_base_paths, default_context

def get_context_config(name='default', dirs=None, rootless=None, uid=None):
    if rootless is None:
        rootless = not probably_root()

    if dirs is None:
        curr_cfg = Path.cwd() / '.darkwing'
        base_cfg, _, _ = default_base_paths(rootless=rootless, uid=uid)
        dirs = [curr_cfg, base_cfg]

    for dirp in dirs:
        cfgp = (Path(dirp) / name).with_suffix('.toml')
        if cfgp.exists():
            return toml.load(cfgp)

    return None
