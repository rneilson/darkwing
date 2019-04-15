import os
import pwd
from pathlib import Path

from darkwing.utils import probably_root

def get_runtime_dir(uid=None):
    # TODO: XDG_RUNTIME_DIR handling?
    if uid is None:
        uid = os.geteuid()
    if uid:
        return Path('/run/user') / str(uid)
    return Path('/run')

def default_base_paths(rootless=None, uid=None):
    if rootless is None:
        rootless = not probably_root()

    euid = os.geteuid()
    if uid is None:
        uid = euid

    if rootless:
        if uid != euid:
            home = Path(pwd.getpwuid(uid).pw_dir)
        else:
            home = Path.home()
        configs = home / '.darkwing'
        storage = home / '.local/share/darkwing'
    else:
        configs = Path('/etc/darkwing')
        storage = Path('/var/lib/darkwing')

    runtime = get_runtime_dir(uid=uid) / 'darkwing'

    return configs, storage, runtime

def default_context(name='default', rootless=None, uid=None,
                    gid=None, configs_dir=None, storage_dir=None):
    if rootless is None:
        rootless = not probably_root()

    if uid is None:
        uid = os.geteuid()
    if gid is None:
        gid = os.getegid()

    base_cfg, base_sto, base_run = default_base_paths(rootless, uid)
    if configs_dir:
        base_cfg = Path(configs_dir)
    if storage_dir:
        base_sto = Path(storage_dir)

    return {
        'domain': f"{name}.darkwing.local",
        'network': {
            'type': 'host',
        },
        'configs': {
            'base': str(base_cfg / name),
            'secrets': str(base_cfg / name / '.secrets'),
        },
        'storage': {
            'images': str(base_sto / 'images'),
            'containers': str(base_sto / 'containers' / name),
            'volumes': str(base_sto / 'volumes' / name),
        },
        'runtime': {
            'base': str(base_run / name),
        },
        'user': {
            'rootless': rootless,
            'uid': uid,
            'gid': gid,
        },
    }

def default_container(name, context, image=None, tag='latest', uid=0, gid=0):
    if image is None:
        image = name

    runtime_path = Path(context['runtime']['base']) / name
    secrets_path = Path(context['configs']['secrets']) / name

    return {
        'hostname': f"{name}.{context['domain']}",
        'terminal': False,
        'image': {
            'type': 'oci',
            'image': image,
            'tag': tag,
        },
        'env': {
            'vars': {},
            'files': [],
        },
        'runtime': {
            'base': str(runtime_path),
            'secrets': str(runtime_path / 'secrets'),
        },
        'secrets': [
            {
                'source': str(secrets_path),
                'target': str(runtime_path / 'secrets'),
                'copy': True,
            },
        ],
        'volumes': [
            {
                'source': str(runtime_path / 'secrets'),
                'target': '/run/secrets',
                'type': 'bind',
                'readonly': True,
            },
        ],
        'user': {
            'uid': uid,
            'gid': gid,
        },
        'caps': {
            'add': [],
            'drop': [],
        }
    }
