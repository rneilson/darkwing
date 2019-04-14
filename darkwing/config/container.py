import os
import toml
from pathlib import Path

from darkwing.utils import probably_root
from .defaults import default_base_paths, default_container

def get_container_config(name, context):
    configs_dir = context['configs']['base']
    config_path = (Path(configs_dir) / name).with_suffix('.toml')

    if config_path.exists():
        return toml.load(config_path)

    return None
