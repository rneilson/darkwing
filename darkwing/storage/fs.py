import os
import sys
import shutil
import subprocess
from pathlib import Path

from darkwing.utils import probably_root, simple_command

def fetch_image():
    raise NotImplementedError

def unpack_image(config, rootless=None, write_output=True):
    if rootless is None:
        rootless = not probably_root()

    unpack_cmd = [ 'umoci', 'raw', 'unpack' ]
    config_cmd = [ 'umoci', 'raw', 'config' ]
    if rootless:
        unpack_cmd.append('--rootless')
        config_cmd.append('--rootless')

    image = config['image']
    if image['type'] != 'oci':
        raise NotImplementedError(
            f'Unsupported image type: "{image["type"]}"'
        )
    image_opt = f"--image={image['path']}:{image['tag']}"
    unpack_cmd.append(image_opt)
    config_cmd.append(image_opt)

    storage = config['storage']
    storage_path = Path(storage['base'])
    rootfs_path = storage_path / 'rootfs'
    config_path = storage_path / 'config.json'
    unpack_cmd.append(str(rootfs_path))
    config_cmd.append(f"--rootfs={rootfs_path}")
    config_cmd.append(str(config_path))

    # Clear out existing rootfs
    if rootfs_path.exists():
        print(f"Removing existing rootfs at {rootfs_path}", flush=True)
        shutil.rmtree(rootfs_path)

    print(f"Unpacking rootfs into {rootfs_path}", flush=True)
    proc = simple_command(
        unpack_cmd, write_output=write_output, cwd=storage_path
    )
    proc.check_returncode()

    print(f"Generating config at {config_path}", flush=True)
    proc = simple_command(
        config_cmd, write_output=write_output, cwd=storage_path
    )
    proc.check_returncode()

    return storage_path
