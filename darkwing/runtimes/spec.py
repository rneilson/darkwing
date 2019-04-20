import os
import json
import shlex
from pathlib import Path

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
    for var in env_config['fixed']:
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

def _mount_spec(mount, volumes, runtime):
    # Vary based on mount type
    mount_type = mount['type']

    if mount_type == 'bind':
        mount_path = Path(mount['source'])
    elif mount_type == 'shared':
        mount_path = Path(volumes['shared']) / mount['source']
    elif mount_type == 'private':
        mount_path = Path(volumes['private']) / mount['source']
    elif mount_type == 'runtime':
        if runtime:
            mount_path = Path(runtime['volumes']) / mount['source']
        else:
            raise ValueError(
                f'Runtime volume mount requested for '
                f'{mount["source"]}, but no runtime config given'
            )
    else:
        raise ValueError(f'Unknown mount type: "{mount_type}"')

    # Base spec
    mount_spec = {
        'destination': mount['target'],
        'type': 'bind',
        'source': str(mount_path),
        'options': ['bind', 'nodev', 'nosuid'],
    }
    # Add read-only option
    if mount.get('readonly'):
        mount_spec['options'].append('ro')

    return mount_spec

def _update_mounts(mounts, volumes, runtime):
    mount_map = { m['destination']: m for m in mounts }
    
    for mount in volumes['mounts']:
        spec = _mount_spec(mount, volumes, runtime)
        mount_map[spec['destination']] = spec

    if runtime:
        for mount in runtime['mounts']:
            spec = _mount_spec(mount, volumes, runtime)
            mount_map[spec['destination']] = spec

    return list(mount_map.values())

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

def update_spec_file(config, runtime, ouid=None, ogid=None):
    # Get config file
    spec_path = Path(config['storage']['base']) / 'config.json'
    spec = json.loads(spec_path.read_text())

    # Set some basic things
    spec['hostname'] = config['dns']['hostname']

    # Command/execution things
    proc = spec['process']
    proc['user']['uid'] = config['user']['uid']
    proc['user']['gid'] = config['user']['gid']
    proc['terminal'] = config['exec']['terminal']

    if config['exec']['dir']:
        proc['cwd'] = config['exec']['dir']
    if config['exec']['cmd']:
        # Overwrite args, even if empty
        proc['args'] = [
            config['exec']['cmd'],
            *_split_args(config['exec']['args'])
        ]
    elif config['exec']['args']:
        # Leave command alone, replace args
        proc['args'][1:] = _split_args(config['exec']['args'])

    # Capabilities, slightly special
    if config['caps']['add'] or config['caps']['drop']:
        proc['capabilities'] = _update_capabilities(
            proc['capabilities'], config['caps']
        )

    # Update environment
    proc['env'] = _update_environment(proc['env'], config['env'])

    # Update mounts
    spec['mounts'] = _update_mounts(
        spec['mounts'], config['volumes'], runtime
    )

    # Update rootless mapped uid/gid
    # TODO: additional mappings?
    linux = spec['linux']
    if ouid is not None:
        linux['uidMappings'] = _update_id_maps(
            linux['uidMappings'], config['user']['uid'], ouid
        )
    if ogid is not None:
        linux['gidMappings'] = _update_id_maps(
            linux['gidMappings'], config['user']['gid'], ogid
        )

    # Write updated file
    spec_path.write_text(json.dumps(spec, indent=2))

    return spec_path
