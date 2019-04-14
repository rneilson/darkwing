import os
import pwd
from pathlib import Path

from darkwing.utils import probably_root

def default_base_paths(rootless=None):
    if rootless is None:
        rootless = not probably_root()

    if rootless:
        configs = Path('~/.darkwing').expanduser()
        storage = Path('~/.local/share/darkwing').expanduser()
        runtime = Path(os.path.expandvars('$XDG_RUNTIME_DIR/darkwing'))
    else:
        configs = Path('/etc/darkwing')
        storage = Path('/var/lib/darkwing')
        runtime = Path('/run/darkwing')

    return configs, storage, runtime

def default_context(name='default', rootless=None, username=None):
    if rootless is None:
        rootless = not probably_root()

    if username is None:
        uid = os.geteuid()
        gid = os.getegid()
    else:
        user = pwd.getpwnam(username)
        uid = user.pw_uid
        gid = user.pw_gid

    base_cfg, base_sto, base_run = default_base_paths(rootless)

    return {
        'domain': f"{name}.darkwing.local",
        'network': {
            'type': 'host',
        },
        'configs': {
            'base': str(base_cfg / name),
            'secrets': str(base_cfg / name / 'secrets'),
        },
        'storage': {
            'containers': str(base_sto / 'containers'),
            'images': str(base_sto / 'images'),
            'volumes': str(base_sto / 'volumes'),
        },
        'runtime': {
            'base': str(base_run / name),
        },
        'rootless': rootless,
        'user': {
            'uid': uid,
            'gid': gid,
        },
    }

def default_container(name, context, image=None, tag='latest', rootless=None):
    if image is None:
        image = name

    if rootless is None:
        rootless = context['rootless']

    if rootless:
        user = {
            'uid': 0,
            'gid': 0,
            'maps': {
                'uid': [
                    {
                        'container': 0,
                        'host': context['user']['uid'],
                        'size': 1
                    }
                ],
                'gid': [
                    {
                        'container': 0,
                        'host': context['user']['gid'],
                        'size': 1
                    }
                ],
            },
        }
    else:
        user = {
            'uid': context['user']['uid'],
            'gid': context['user']['gid'],
            'maps': {},
        }

    runtime_dir = Path(context['runtime']['base']) / name
    secrets_dir = runtime_dir / 'secrets'
    volumes_dir = runtime_dir / 'volumes'

    return {
        'hostname': f"{name}.{context['domain']}",
        'terminal': False,
        'image': {
            'type': 'oci',
            'image': image,
            'tag': tag,
        },
        'runtime': {
            'base': str(runtime_dir),
            'secrets': str(secrets_dir),
            'volumes': str(volumes_dir),
        },
        'env': {
            'vars': {},
            'files': [],
        },
        'volumes': [
            {
                'source': str(secrets_dir),
                'target': '/run/secrets',
                'type': 'bind',
                'readonly': True,
            },
        ],
        'rootless': rootless,
        'user': user,
        'caps': {
            'add': [],
            'drop': [],
        }
    }
