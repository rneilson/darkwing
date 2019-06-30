import os
import toml
from pathlib import Path

from darkwing.utils import probably_root, ensure_dirs
from .defaults import default_base_paths, default_context


class Context(object):

    def __init__(self, name, path, data):
        self.name = name
        self.path = path
        self.data = data
        # TODO: extract (and resolve?) various paths from data
        self.base_path = path.parent

    def __repr__(self):
        return (
            f"<{self.__class__.__name__} name={self.name!r} "
            f"path={str(self.path)!r}>"
        )

    def get_path(self, section, subsection='base', allow_relative=False):
        if section == 'base':
            return self.base_path

        # TODO: allow any kind of fallbacks?
        path = Path(self.data[section][subsection])

        # Handle paths relative to a base path
        if not path.is_absolute() and subsection != 'base':
            path = self.get_path(section, 'base') / path

        # Use parent dir of context file as base path if req'd
        if not allow_relative and not path.is_absolute():
            path = (self.base_path / path).expanduser().resolve()

        return path


def get_context_config(name='', dirs=None, rootless=None, uid=None):
    if rootless is None:
        rootless = not probably_root()

    if dirs is None:
        cwd_base = Path.cwd() / '.darkwing'
        configs_base, _ = default_base_paths(rootless, uid)
        dirs = [cwd_base, configs_base]

    for dirp in dirs:
        # Default to 'darkwing.toml' in search paths if unnamed
        context_path = (Path(dirp) / (name or 'darkwing')).with_suffix('.toml')
        if context_path.exists():
            return Context(name, context_path, toml.load(context_path))

    return None

def make_context_config(name='', rootless=None, ouid=None, ogid=None,
                        configs_dir=None, storage_dir=None,
                        base_domain=None, write_file=True):
    if rootless is None:
        rootless = not probably_root()

    euid = os.geteuid()
    egid = os.getegid()

    if ouid is None:
        ouid = euid
    if ogid is None:
        ogid = egid

    if not configs_dir or not storage_dir:
        configs_base, storage_base = default_base_paths(rootless, ouid)
    if configs_dir:
        configs_base = Path(configs_dir)
    if storage_dir:
        storage_base = Path(storage_dir)

    # Default to 'darkwing.toml' in search paths if unnamed
    context_path = (Path(configs_base) / name or 'darkwing').with_suffix('.toml')
    do_chown = ouid != euid or ogid != egid
    cuid, cgid = (ouid, ogid) if do_chown else (None, None)

    # Start with default config
    # TODO: insert/compare other config elements
    context_data = default_context(
        name=name, rootless=rootless, ouid=ouid, ogid=ogid,
        configs_dir=configs_dir, storage_dir=storage_dir,
        base_domain=base_domain,
    )

    if write_file:
        # Ensure all context's subdirs exist
        # TODO: resolve relative paths from context
        dirs = [
            (configs_base, 0o775),
            (storage_base, 0o775),
            (Path(context_data['configs']['base']), 0o775),
            (Path(context_data['configs']['containers']), 0o775),
            (Path(context_data['configs']['secrets']), 0o770),
            (Path(context_data['storage']['base']), 0o775),
            (Path(context_data['storage']['images']), 0o775),
            (Path(context_data['storage']['containers']), 0o770),
            (Path(context_data['storage']['volumes']), 0o770),
        ]
        ensure_dirs(dirs, uid=cuid, gid=cgid)

        # Write context to file
        # Touch mostly to raise FileExistsError
        context_path.touch(mode=0o660, exist_ok=False)
        if do_chown:
            os.chown(context_path, cuid, cgid)
        context_path.write_text(toml.dumps(context_data))

    return Context(name, context_path, context_data)
