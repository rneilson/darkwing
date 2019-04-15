#!/usr/bin/env python3

'''
darkwing - run container
'''

__version__ = '0.0.1'

def run_cmd(args):
    
    # Find & parse context config
        # Base dirs: $CWD/.darkwing, $HOME/.darkwing, /etc/darkwing
        # Filename: {context}.toml

    # Find & parse container config
        # Default dir: {basedir}/{context}
        # Filename: {container}.toml

    # Parse target namespaces
        # Check if newuidmap/newgidmap available
            # Parse /etc/subuid & /etc/subgid
            # Honor configured (alternate) mappings in config
        # NOTE: done before other operations in order to ensure
        # full user/group mappings available when unpacking
        # TODO: how best to handle image specs with non-root users?

    # Ensure bundle dir
        # Conflicting-state check
        # TODO: auto-fetch?
        # if --recreate:
            # Remove old rootfs
            # Remove old spec
        # if not bundle:
            # Unpack new rootfs
            # Create spec

    # Parse & update spec
        # Cmd, args, workdir
        # User mappings
        # Env vars
        # Hostname
        # Terminal
        # Mounts
            # resolvconf
            # hosts file
            # volumes
        # TODO: network

    # Prepare runtime dir
        # Write files to mount
        # if not terminal:
            # Make named pipes
        # Create secrets dir

    # Set subreaper

    # Install signal handlers
        # SIGWINCH, SIGCHILD handled
        # Others forwarded

    # Init container
        # if terminal:
            # Start console socket listener
        # else:
            # Open named pipes
            # Start stdio forwarders
        # Launch `runc create` subprocess
            # Pass pidfile option (runtime dir)
            # if terminal:
                # Pass --console-socket option (runtime dir)
            # else:
                # Pass named pipes as stdio
        # if terminal:
            # Get socket from listener (recvmsg)
            # Start stdio forwarders
            # Close socket
        # Await subprocess...

    # Intermezzo
        # Decrypt & extract secrets
            # (Placed here to minimize exposure)
        # TODO: network
            # Request interface from network daemon

    # Start container
        # Launch `runc start` subprocess
        # Await subprocess...

    # Await container exit...

    # Stop stdio forwarders
        # if not terminal:
            # Close named pipes

    # Cleanup runtime dir
        # Remove secrets dir
        # Remove pidfile
        # Remove mounted files
        # if not terminal:
            # Remove named pipes

    # if --remove:
        # Cleanup bundle dir
            # Remove rootfs
            # Remove spec

    # Return container exit status

    raise NotImplementedError

def exec_cmd(args):
    raise NotImplementedError

def stop_cmd(args):
    raise NotImplementedError

def fetch_cmd(args):
    raise NotImplementedError

def rm_cmd(args):
    raise NotImplementedError

def help_cmd(args):
    raise NotImplementedError

def version_cmd(args):
    raise NotImplementedError

def main():
    # Parse subcommand

    # Parse args

    raise NotImplementedError

if __name__ == '__main__':
    main()
