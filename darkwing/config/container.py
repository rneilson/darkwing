import os
import toml
from pathlib import Path

from darkwing.utils import (
    probably_root, ensure_dirs, ensure_files, get_runtime_dir,
)
from .defaults import default_base_paths, default_container

def get_container_config(name, context_name='default',
                         dirs=None, rootless=None, uid=None):
    if rootless is None:
        rootless = not probably_root()

    if dirs is None:
        cwd_base = Path.cwd() / '.darkwing'
        config_base, _ = default_base_paths(rootless, uid)
        dirs = [cwd_base, config_base]

    for dirp in dirs:
        config_path = (Path(dirp) / context_name / name).with_suffix('.toml')
        if config_path.exists():
            return toml.load(config_path), config_path

    return None, None

def make_container_config(name, context, image=None, tag='latest', uid=0,
                          gid=0, euid=None, egid=None, write_file=True):
    if euid is None:
        euid = os.geteuid()
    if egid is None:
        egid = os.getegid()
    
    config_base = Path(context['configs']['base'])
    config_path = (config_base / name).with_suffix('.toml')

    owner = context['owner']
    do_chown = owner['uid'] != euid or owner['gid'] != egid
    ouid, ogid = (owner['uid'], owner['gid']) if do_chown else (None, None)

    # Start with default config
    # TODO: insert/compare other config elements
    config = default_container(
        name, context, image=image, tag=tag, uid=uid, gid=gid
    )

    if write_file:
        # Ensure all required config, storage dirs created
        dirs = [
            (config_base, 0o775),
            (Path(config['storage']['base']), 0o770),
            (Path(config['storage']['secrets']), 0o700),
            (Path(config['volumes']['private']), 0o770),
        ]
        ensure_dirs(dirs, uid=ouid, gid=ogid)

        # Write config to file
        # Touch mostly to raise FileExistsError
        config_path.touch(mode=0o664, exist_ok=False)
        if do_chown:
            os.chown(config_path, uid=ouid, gid=ogid)
        config_path.write_text(toml.dumps(config))

    return config, config_path

def make_runtime_dir(name, config, context_name='default',
                     runtime_dir=None, uid=None, gid=None):
    if runtime_dir is None:
        runtime_base = get_runtime_dir(uid=uid)
    else:
        runtime_base = Path(runtime_base)

    runtime_path = runtime_base / context_name / name
    secrets_path = runtime_path / 'secrets'
    volumes_path = runtime_path / 'volumes'

    # Create runtime dirs
    dirs = [
        (runtime_path, 0o770),
        (secrets_path, 0o700),
        (volumes_path, 0o770),
    ]
    # TODO: parse temp volumes from config, add to dirs
    ensure_dirs(dirs, uid=uid, gid=gid)

    # Determine runtime mounts
    resolvconf = runtime_path / 'resolv.conf'
    hostname = runtime_path / 'hostname'
    # hosts = runtime_path / 'hosts'
    mounts = [
        {
            'source': str(secrets_path),
            'target': config['secrets']['target'],
            'type': 'bind',
            'readonly': True,
        },
        {
            'source': str(resolvconf),
            'target': '/etc/resolv.conf',
            'type': 'bind',
            'readonly': True,
        },
        {
            'source': str(hostname),
            'target': '/etc/hostname',
            'type': 'bind',
            'readonly': False,
        },
        # TODO: /etc/hosts?
    ]
    # TODO: parse temp volumes from config, add to mounts

    # Create runtime files
    files = [
        (resolvconf, 0o644),
        (hostname, 0o644),
        # TODO: hosts
    ]
    ensure_files(files, uid=uid, gid=gid)

    # Copy host's resolvconf
    # TODO: handle when symlink, or not present, or weird
    # TODO: alternate source when not using host network
    with open('/etc/resolv.conf', 'r') as f:
        resolvconf.write_text(f.read())
    # Write container's hostname
    hostname.write_text(config['dns']['hostname'])
    # TODO: write hosts?

    runtime = {
        'base': str(runtime_path),
        'secrets': str(secrets_path),
        'volumes': str(volumes_path),
        'resolvconf': str(resolvconf),
        'hostname': str(hostname),
        'mounts': mounts,
    }

    return runtime, runtime_path
