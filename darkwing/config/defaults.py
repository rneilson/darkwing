import os
import pwd
from pathlib import Path

from darkwing.utils import probably_root

def default_base_paths(rootless=None, uid=None):
    if rootless is None:
        rootless = not probably_root()

    if rootless:
        euid = os.geteuid()
        if uid is None:
            uid = euid

        if uid != euid:
            home = Path(pwd.getpwuid(uid).pw_dir)
        else:
            home = Path.home()
        configs = home / '.darkwing'
        storage = home / '.local/share/darkwing'
    else:
        configs = Path('/etc/darkwing')
        storage = Path('/var/lib/darkwing')

    return configs, storage

def default_context(name='default', rootless=None, uid=None,
                    gid=None, configs_dir=None, storage_dir=None):
    if rootless is None:
        rootless = not probably_root()

    if uid is None:
        uid = os.geteuid()
    if gid is None:
        gid = os.getegid()

    if not configs_dir or not storage_dir:
        base_cfg, base_sto = default_base_paths(rootless, uid)
    if configs_dir:
        base_cfg = Path(configs_dir)
    if storage_dir:
        base_sto = Path(storage_dir)

    return {
        'configs': {
            'base': str(base_cfg / name),
            'secrets': str(base_cfg / name / '.secrets'),
        },
        'storage': {
            'images': str(base_sto / 'images'),
            'volumes': str(base_sto / 'volumes' / name),
            'containers': str(base_sto / 'containers' / name),
        },
        'dns': {
            'domain': f"{name}.darkwing.local",
        },
        'network': {
            'type': 'host',
        },
        'owner': {
            'uid': uid,
            'gid': gid,
            'rootless': rootless,
        },
    }

def default_container(name, context, image=None, tag='latest', uid=0, gid=0):
    if image is None:
        image = name

    image_path = Path(context['storage']['images']) / 'oci' / image
    storage_path = Path(context['storage']['containers']) / name
    secrets_path = Path(context['configs']['secrets']) / name

    return {
        'image': {
            'type': 'oci',
            'path': str(image_path),
            'tag': tag,
        },
        'storage': {
            'base': str(storage_path),
            'secrets': str(secrets_path),
        },
        'exec': {
            'dir': '',
            'cmd': '',
            'args': [],
            'terminal': False,
        },
        'env': {
            'host': [],
            'vars': [],
            'files': [],
        },
        'user': {
            'uid': uid,
            'gid': gid,
        },
        'caps': {
            'add': [],
            'drop': [],
        },
        'dns': {
            'hostname': f"{name}.{context['dns']['domain']}",
            'domain': context['dns']['domain'],
        },
        'network': { **context['network'] },
        'secrets': {
            'target': '/run/secrets',
            'sources': [
                {
                    'path': str(secrets_path),
                    'type': 'copy',
                    'mode': '400',
                },
            ],
        },
        'volumes': {
            'shared': context['storage']['volumes'],
            'private': str(storage_path / 'volumes'),
            'mounts': [],
        },
    }
