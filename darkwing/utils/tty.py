import os
import sys
import termios
import struct
import fcntl

def output_isatty(stdout=None, stderr=None):
    if stdout is None:
        stdout = sys.stdout
    if not isinstance(stdout, int):
        stdout = stdout.fileno()

    if stderr is None:
        stderr = sys.stderr
    if not isinstance(stderr, int):
        stderr = stderr.fileno()

    return (
        os.isatty(stdout) and
        os.isatty(stderr) and
        os.ttyname(stdout) == os.ttyname(stderr)
    )

def resize_tty(fd, columns, lines):
    if not isinstance(fd, int):
        fd = fd.fileno()

    oldsize = os.get_terminal_size(fd)
    newsize = struct.pack('HHHH', lines, columns, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, newsize)

    return oldsize
