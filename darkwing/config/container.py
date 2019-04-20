import os
import toml
from pathlib import Path
from collections import deque

from darkwing.utils import (
    probably_root, ensure_dirs, ensure_files, get_runtime_path,
)
from .defaults import default_base_paths, default_container


class Config(object):

    def __init__(self, name, path, data):
        self.name = name
        self.path = path
        # TODO: expand
        self.data = data


class Rundir(object):

    def __init__(self, path, data):
        self.path = path
        # TODO: expand
        self.data = data


class Container(object):

    def __init__(self, name, config, rundir=None, context=None):
        # Manager state
        self.name = name
        self.path = Path(config.data['storage']['base'])
        self.config = config
        self.rundir = rundir
        self.context = context
        # Executor state
        self.pid = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.returncode = None
        self.status = 'new'
        # Internal state
        self._waiter = None
        self._runtime = None
        self._close_fds = deque()
        self._io_threads = deque()

    @property
    def config_path(self):
        return self.config.path

    @property
    def rundir_path(self):
        return self.rundir.path if self.rundir else None
    
    @property
    def context_name(self):
        if isinstance(self.context, (str, bytes)):
            return self.context
        if context:
            return self.context.name
        return None


def get_container_config(name, context, dirs=None, rootless=None, uid=None):
    if rootless is None:
        rootless = not probably_root()

    if isinstance(context, (str, bytes)):
        context_name = context
    else:
        context_name = context.name

    if dirs is None:
        cwd_base = Path.cwd() / '.darkwing'
        config_base, _ = default_base_paths(rootless, uid)
        dirs = [cwd_base, config_base]

    for dirp in dirs:
        config_path = (Path(dirp) / context_name / name).with_suffix('.toml')
        if config_path.exists():
            return Config(name, config_path, toml.load(config_path))

    return None

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
    cuid, cgid = (owner['uid'], owner['gid']) if do_chown else (None, None)

    # Start with default config
    # TODO: insert/compare other config elements
    config_data = default_container(
        name, context, image=image, tag=tag, uid=uid, gid=gid
    )

    if write_file:
        # Ensure all required config, storage dirs created
        dirs = [
            (config_base, 0o775),
            (Path(config_data['storage']['base']), 0o770),
            (Path(config_data['storage']['secrets']), 0o700),
            (Path(config_data['volumes']['private']), 0o770),
        ]
        ensure_dirs(dirs, uid=cuid, gid=cgid)

        # Write config to file
        # Touch mostly to raise FileExistsError
        config_path.touch(mode=0o664, exist_ok=False)
        if do_chown:
            os.chown(config_path, uid=cuid, gid=cgid)
        config_path.write_text(toml.dumps(config_data))

    return Config(name, config_path, config_data)

def make_runtime_dir(name, config, context, base_path=None,
                     uid=None, gid=None):
    if base_path is None:
        rundir_base = get_runtime_path(uid=uid)
    else:
        rundir_base = Path(base_path)

    if isinstance(context, (str, bytes)):
        context_name = context
    else:
        context_name = context.name

    rundir_path = rundir_base / context_name / name
    secrets_path = rundir_path / 'secrets'
    volumes_path = rundir_path / 'volumes'

    # Create runtime dirs
    dirs = [
        (rundir_path, 0o770),
        (secrets_path, 0o700),
        (volumes_path, 0o770),
    ]
    # TODO: parse temp volumes from config, add to dirs
    ensure_dirs(dirs, uid=uid, gid=gid)

    # Determine runtime mounts
    resolvconf = rundir_path / 'resolv.conf'
    hostname = rundir_path / 'hostname'
    # hosts = rundir_path / 'hosts'
    mounts = [
        {
            'source': str(secrets_path),
            'target': config.data['secrets']['target'],
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
    hostname.write_text(config.data['dns']['hostname'])
    # TODO: write hosts?

    rundir_data = {
        'base': str(rundir_path),
        'secrets': str(secrets_path),
        'volumes': str(volumes_path),
        'resolvconf': str(resolvconf),
        'hostname': str(hostname),
        'mounts': mounts,
    }

    return Rundir(rundir_path, rundir_data)
