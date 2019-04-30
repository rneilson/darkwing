import os
import sys
import shutil
import subprocess
from pathlib import Path

from darkwing.utils import probably_root, simple_command

def fetch_image():
    raise NotImplementedError

def unpack_image(config, rootless=None, write_output=True, exist_ok=True):
    if rootless is None:
        rootless = not probably_root()

    unpack_cmd = [ 'umoci', 'raw', 'unpack' ]
    config_cmd = [ 'umoci', 'raw', 'config' ]
    if rootless:
        unpack_cmd.append('--rootless')
        config_cmd.append('--rootless')

    image = config.data['image']
    if image['type'] != 'oci':
        raise NotImplementedError(
            f'Unsupported image type: "{image["type"]}"'
        )
    image_opt = f"--image={image['path']}:{image['tag']}"
    unpack_cmd.append(image_opt)
    config_cmd.append(image_opt)

    storage = config.data['storage']
    storage_path = Path(storage['base'])
    rootfs_path = storage_path / 'rootfs'
    config_path = storage_path / 'config.json'
    unpack_cmd.append(str(rootfs_path))
    config_cmd.append(f"--rootfs={rootfs_path}")
    config_cmd.append(str(config_path))

    # Clear out existing rootfs (or bail)
    try:
        # Using exist_ok=False here so we can EAFP
        rootfs_path.mkdir(mode=0o770, parents=False, exist_ok=False)
    except FileExistsError:
        if exist_ok:
            # Early return, assume already unpacked
            # TODO: check if directory empty, remove if true
            if write_output:
                print(f"Found existing rootfs at {rootfs_path}", flush=True)
            # TODO: instead of early return, allow regenerating config
            return storage_path
        else:
            if write_output:
                print(f"Removing existing rootfs at {rootfs_path}", flush=True)
            shutil.rmtree(rootfs_path)
            rootfs_path.mkdir(mode=0o770)

    if write_output:
        print(f"Unpacking rootfs into {rootfs_path}", flush=True)
    proc = simple_command(
        unpack_cmd, write_output=write_output, cwd=storage_path
    )
    proc.check_returncode()

    if write_output:
        print(f"Generating config at {config_path}", flush=True)
    proc = simple_command(
        config_cmd, write_output=write_output, cwd=storage_path
    )
    proc.check_returncode()

    return storage_path
