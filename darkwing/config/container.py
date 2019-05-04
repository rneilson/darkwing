import os
import toml
import shutil
from pathlib import Path
from collections import deque

from darkwing.utils import (
    probably_root, ensure_dirs, ensure_files,
    get_runtime_path, compute_returncode,
)
from darkwing.runtimes import spec
from darkwing import storage
from .defaults import default_base_paths, default_container


class Config(object):

    def __init__(self, name, path, data):
        self.name = name
        self.path = path
        # TODO: expand
        self.data = data

    def __repr__(self):
        return (
            f"<{self.__class__.__name__} name={self.name!r} "
            f"path={str(self.path)!r}>"
        )


class Rundir(object):

    def __init__(self, path, data):
        self.path = path
        # TODO: expand
        self.data = data

    def __repr__(self):
        return f"<{self.__class__.__name__} path={str(self.path)!r}>"

    def remove(self):
        if self.path.exists():
            shutil.rmtree(self.path)
            return True
        return False


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
        self.tty = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.returncode = None
        self.status = 'new'
        # Internal state
        self._waiter = None
        self._runtime = None
        self._closing = None
        self._kill_sent = None
        self._close_fds = deque()
        self._io_threads = deque()
        self._stop_event = None
        # TODO: waitpid lock?

    def __repr__(self):
        info = [self.__class__.__name__, f"name={self.name!r}"]
        if self.pid:
            info.append(f"pid={self.pid!r}")
        if self.context:
            info.append(f"context={self.context_name!r}")
        if self.config:
            info.append(f"config={self.config.name!r}")
        info.append(f"path={str(self.path)!r}")
        if self.rundir:
            info.append(f"rundir={str(self.rundir_path)!r}")
        return '<{}>'.format(' '.join(info))

    @property
    def use_tty(self):
        return self.config.data['exec'].get('terminal', False)

    @use_tty.setter
    def use_tty(self, value):
        self.config.data['exec']['terminal'] = value

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
        if self.context:
            return self.context.name
        return None

    def make_rundir(self, recreate=False):
        if self.context is None:
            raise ValueError(f'Cannot create runtime dir without context')

        if self.rundir and not recreate:
            return self

        self.rundir = make_runtime_dir(
            self.name, self.config, self.context, recreate=recreate
        )

        return self

    def unpack_image(self, storage_type='fs', make_rundir=True,
                     recreate=False, reconfig=False, quiet=False):
        try:
            storage_lib = getattr(storage, storage_type)
        except AttributeError:
            raise ValueError(f'Invalid storage type: {storage_type!r}')

        if make_rundir:
            self.make_rundir(recreate=recreate)

        # Unpack image (possibly remove existing)
        storage_path = storage_lib.unpack_image(
            self.config, write_output=not quiet,
            refresh_rootfs=recreate, refresh_config=reconfig,
        )
        # Update storage path if required (future feature)
        if self.path != storage_path:
            self.path = storage_path
            self.config.data['storage']['base'] = storage_path

        # # Update spec file
        # spec.update_spec_file(self.config, self.rundir)

        return self

    def _wait(self, blocking=True):
        if self.returncode is not None or self.pid is None:
            return self.returncode

        try:
            pid, sts = os.waitpid(self.pid, 0 if blocking else os.WNOHANG)
        except ChildProcessError:
            # Child already reaped somewhere else
            self.returncode = 255
        else:
            if pid == 0:
                # Child still alive
                return None

            self.returncode = compute_returncode(sts)

        return self.returncode

    def wait(self, blocking=True):
        # For now, just a public wrapper
        # TODO: return future?
        return self._wait(blocking=blocking)

    def _close(self):
        try:
            self._closing = True
            returncode = self.wait()
            # TODO: attempt kill if child not yet dead
            # TODO: self._waiter here?
            # TODO: catch any weird exceptions?
        finally:
            if self._stop_event:
                self._stop_event.set()

            while True:
                try:
                    t = self._io_threads.popleft()
                    t.join()
                except IndexError:
                    break

            while True:
                try:
                    fd = self._close_fds.popleft()
                    try:
                        if isinstance(fd, int):
                            os.close(fd)
                        else:
                            fd.close()
                    except OSError as e:
                        # TODO: log
                        pass
                except IndexError:
                    break

            # TODO: or self._waiter here instead?
            self._closing = False

    def close(self):
        # Any preamble? Lock?
        if self._closing is None:
            self._close()

        return self.returncode

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
    
    config_base = Path(context.data['configs']['base'])
    config_path = (config_base / name).with_suffix('.toml')

    owner = context.data['owner']
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
                     uid=None, gid=None, recreate=False):
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

    if recreate and rundir_path.exists():
        shutil.rmtree(rundir_path)

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

def load_container(name, context, make_rundir=False):
    config = get_container_config(name, context)
    if not config:
        raise FileNotFoundError(
            f'No config found for {name!r} in context {context!r}'
        )

    container = Container(name, config, context=context)

    if make_rundir:
        container.make_rundir()

    return container
