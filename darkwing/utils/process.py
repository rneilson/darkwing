import os
import sys
import subprocess

def simple_command(args, write_output=True, exit_on_failure=False, **kwargs):
    p = subprocess.run(args, capture_output=True, text=True, **kwargs)

    if write_output:
        if p.stdout:
            sys.stdout.write(p.stdout)
            sys.stdout.flush()
        if p.stderr:
            sys.stderr.write(p.stderr)
            sys.stderr.flush()

    if exit_on_failure and p.returncode != 0:
        sys.exit(e.returncode)

    return p

def compute_returncode(status):
    if os.WIFSIGNALED(status):
        returncode = -os.WTERMSIG(status)
    elif os.WIFEXITED(status):
        returncode = os.WEXITSTATUS(status)
    elif os.WIFSTOPPED(status):
        returncode = os.WSTOPSIG(status)

    return returncode
