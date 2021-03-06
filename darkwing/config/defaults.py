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

def default_context(name='default', rootless=None, ouid=None, ogid=None,
                    configs_dir=None, storage_dir=None, base_domain=None):
    if rootless is None:
        rootless = not probably_root()

    if ouid is None:
        ouid = os.geteuid()
    if ogid is None:
        ogid = os.getegid()

    if not configs_dir or not storage_dir:
        configs_base, storage_base = default_base_paths(rootless, ouid)
    if configs_dir:
        configs_base = Path(configs_dir)
    if storage_dir:
        storage_base = Path(storage_dir)

    if base_domain is None:
        base_domain = 'darkwing.local'

    return {
        'configs': {
            'base': str(configs_base / name),
            'secrets': str(configs_base / name / '.secrets'),
        },
        'storage': {
            'images': str(storage_base / 'images'),
            'volumes': str(storage_base / 'volumes' / name),
            'containers': str(storage_base / 'containers' / name),
        },
        'dns': {
            'domain': '.'.join(filter(None, [name, base_domain])),
        },
        'network': {
            'type': 'host',
        },
        'owner': {
            'uid': ouid,
            'gid': ogid,
            'rootless': rootless,
        },
    }

def default_container(name, context, image=None, tag='latest', uid=0, gid=0):
    if image is None:
        image = name

    image_path = Path(context.data['storage']['images']) / 'oci' / image
    storage_path = Path(context.data['storage']['containers']) / name
    secrets_path = Path(context.data['configs']['secrets']) / name

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
            'terminal': True,
        },
        'env': {
            'vars': [],
            'host': [],
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
            'hostname': f"{name}.{context.data['dns']['domain']}",
            'domain': context.data['dns']['domain'],
        },
        'network': { **context.data['network'] },
        'secrets': {
            'target': '/run/secrets',
            'sources': [
                {
                    'path': str(secrets_path),
                    'type': 'copy',
                    'mode': '0400',
                },
            ],
        },
        'volumes': {
            'shared': context.data['storage']['volumes'],
            'private': str(storage_path / 'volumes'),
            'mounts': [],
        },
    }
