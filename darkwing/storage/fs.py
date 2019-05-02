import os
import sys
import shutil
import subprocess
from pathlib import Path

from darkwing.utils import probably_root, simple_command

def fetch_image():
    raise NotImplementedError

def unpack_image(config, rootless=None, write_output=True,
                 refresh_rootfs=False, refresh_config=True):
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
    config_orig = storage_path / 'config.orig.json'
    unpack_cmd.append(str(rootfs_path))
    config_cmd.append(f"--rootfs={rootfs_path}")
    config_cmd.append(str(config_path))

    # Clear out existing rootfs (or bail)
    do_unpack = True
    try:
        # Using exist_ok=False here so we can EAFP
        rootfs_path.mkdir(mode=0o770, parents=False, exist_ok=False)
    except FileExistsError:
        # Check if directory empty
        file_list = os.listdir(rootfs_path)
        if file_list and not refresh_rootfs:
            # Early return, assume already unpacked
            do_unpack = False
            if write_output:
                print(f"Found existing rootfs at {rootfs_path}", flush=True)
        elif file_list:
            if write_output:
                print(f"Removing existing rootfs at {rootfs_path}", flush=True)
            shutil.rmtree(rootfs_path)
            rootfs_path.mkdir(mode=0o770)

    if do_unpack:
        if write_output:
            print(f"Unpacking rootfs into {rootfs_path}", flush=True)
        proc = simple_command(
            unpack_cmd, write_output=write_output, cwd=storage_path
        )
        proc.check_returncode()

    if refresh_config:
        if write_output:
            print(f"Generating config at {config_path}", flush=True)
        proc = simple_command(
            config_cmd, write_output=write_output, cwd=storage_path
        )
        proc.check_returncode()
        # Clear backup/original config
        try:
            config_orig.unlink()
        except FileNotFoundError:
            pass

    return storage_path
