import os
import json
import shlex
from pathlib import Path

from darkwing.utils.files import ensure_dirs

def _split_args(args):
    if isinstance(args, str):
        return shlex.split(args)
    elif not args:
        return []
    return args

def _update_capabilities(caps, caps_config):
    new_caps = {}

    for kind, cap_list in caps.items():
        new_list = [c for c in cap_list if c not in set(caps_config['drop'])]
        new_set = set(new_list)
        new_list.extend(c for c in caps_config['add'] if c not in new_set)
        new_caps[kind] = new_list

    return new_caps

def _update_environment(env, env_config):
    env_vars = {}

    # First expand into dictionary
    for var in env:
        name, sep, value = var.partition('=')
        env_vars[name] = value

    # Set/unset fixed
    for var in env_config['vars']:
        name, sep, value = var.partition('=')
        if sep:
            env_vars[name] = value
        else:
            env_vars.pop(name, None)

    # Set from host env
    for var in env_config['host']:
        name, sep, value = var.partition('=')
        hostval = os.environ.get(name)
        # Set if present in host, use default if given, or unset if not
        if hostval is not None:
            env_vars[name] = hostval
        elif sep:
            env_vars[name] = value
        else:
            env_vars.pop(name, None)

    # TODO: read & parse files

    return [ f"{name}={value}" for name, value in env_vars.items() ]

def _mount_source_path(mount_type, mount_src, volumes, rundir_data):
    if mount_type == 'bind':
        mount_path = Path(mount_src)
        # Only absolute paths allowed
        if not mount_path.is_absolute():
            raise ValueError(f'Bind mount "{mount_path}" must be absolute')
    elif mount_type == 'shared':
        mount_path = Path(volumes['shared']) / mount_src.lstrip('/')
    elif mount_type == 'private':
        mount_path = Path(volumes['private']) / mount_src.lstrip('/')
    elif mount_type == 'runtime':
        if rundir_data:
            mount_path = Path(rundir_data['volumes']) / mount_src.lstrip('/')
        else:
            raise ValueError(
                f'Runtime volume mount requested for '
                f'"{mount_src}", but no runtime directory given'
            )
    else:
        raise ValueError(f'Unknown mount type: "{mount_type}"')

    return mount_path

def _mount_spec(mount, volumes, rundir_data):
    # Vary based on mount type
    mount_type = mount['type']
    mount_src = mount['source']
    mount_path = _mount_source_path(
        mount_type, mount_src, volumes, rundir_data
    )

    # Base spec
    mount_spec = {
        'destination': mount['target'],
        'type': 'bind',
        'source': str(mount_path),
        'options': ['nodev', 'nosuid'],
    }
    # Determine bind type
    if mount.get('recursive'):
        mount_spec['options'].append('rbind')
    else:
        mount_spec['options'].append('bind')
    # Add read-only option
    if mount.get('readonly'):
        mount_spec['options'].append('ro')

    return mount_spec

def _update_mounts(mounts, volumes, rundir_data):
    mount_map = { m['destination']: m for m in mounts }
    
    for mount in volumes['mounts']:
        spec = _mount_spec(mount, volumes, rundir_data)
        mount_map[spec['destination']] = spec

    if rundir_data:
        for mount in rundir_data['mounts']:
            spec = _mount_spec(mount, volumes, rundir_data)
            mount_map[spec['destination']] = spec

    return list(mount_map.values())

def _ensure_mounts(volumes, rundir_data, ouid=None, ogid=None):
    mount_dirs = []

    for mount in volumes['mounts']:
        mount_type = mount['type']
        mount_path = _mount_source_path(
            mount_type, mount['source'], volumes, rundir_data
        )
        if mount_type == 'bind':
            # For bind mounts, ensure exists
            if not mount_path.exists():
                raise ValueError(f'Bind mount "{mount_path}" must exist')
        else:
            # For volumes, ensure directory created
            dir_mode = mount.get('mode', '0770')
            if isinstance(dir_mode, str):
                dir_mode = int(dir_mode, 8)
            mount_dirs.append((mount_path, dir_mode))

    return ensure_dirs(mount_dirs, uid=ouid, gid=ogid)

def _update_id_maps(id_maps, container_id, host_id):
    new_maps = []

    # TODO: handle other mappings, sizes
    for m in id_maps:
        if m['containerID'] == container_id:
            new_map = m.copy()
            new_map['hostID'] = host_id
        else:
            new_map = m
        new_maps.append(new_map)

    return new_maps

def update_spec_file(config, rundir, ouid=None, ogid=None,
                     allow_tty=None, force_tty=None, ensure_mounts=True):
    assert allow_tty is None or force_tty is None
    # Get config file
    spec_path = Path(config.data['storage']['container']) / 'config.json'
    orig_path = Path(config.data['storage']['container']) / 'config.orig.json'
    try:
        # If original/backup present, prefer as clean version
        spec_str = orig_path.read_text()
    except FileNotFoundError:
        # No backup, so assume config is fresh
        spec_str = spec_path.read_text()
        # Write backup
        orig_path.write_text(spec_str)

    spec = json.loads(spec_str)

    # Set some basic things
    spec['hostname'] = config.data['dns']['hostname']

    # Command/execution things
    proc = spec['process']
    proc['user']['uid'] = config.data['user']['uid']
    proc['user']['gid'] = config.data['user']['gid']
    # TTY settings
    if force_tty is not None:
        proc['terminal'] = force_tty
    else:
        proc['terminal'] = config.data['exec']['terminal']
        if allow_tty is not None:
            proc['terminal'] = allow_tty and proc['terminal']

    # TODO: make more idempotent (instead of depending on
    # current config file state)
    if config.data['exec']['dir']:
        proc['cwd'] = config.data['exec']['dir']
    if config.data['exec']['cmd']:
        # Overwrite args, even if empty
        proc['args'] = [
            config.data['exec']['cmd'],
            *_split_args(config.data['exec']['args'])
        ]
    elif config.data['exec']['args']:
        # Leave command alone, replace args
        proc['args'][1:] = _split_args(config.data['exec']['args'])

    # Capabilities, slightly special
    if config.data['caps']['add'] or config.data['caps']['drop']:
        proc['capabilities'] = _update_capabilities(
            proc['capabilities'], config.data['caps']
        )

    # TODO: rlimits

    # Update environment
    proc['env'] = _update_environment(proc['env'], config.data['env'])

    # Update mounts
    spec['mounts'] = _update_mounts(
        spec['mounts'], config.data['volumes'],
        rundir.data if rundir else None
    )
    if ensure_mounts:
        _ensure_mounts(
            config.data['volumes'],
            rundir.data if rundir else None,
            ouid=ouid, ogid=ogid
        )

    # Update rootless mapped uid/gid
    # TODO: additional mappings
    linux = spec['linux']
    if ouid is not None:
        linux['uidMappings'] = _update_id_maps(
            linux['uidMappings'], config.data['user']['uid'], ouid
        )
    if ogid is not None:
        linux['gidMappings'] = _update_id_maps(
            linux['gidMappings'], config.data['user']['gid'], ogid
        )

    # Write updated file
    spec_path.write_text(json.dumps(spec, indent='\t'))

    return spec_path
