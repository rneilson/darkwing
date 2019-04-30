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

def send_tty_eof(fd, last_sent=None):
    if not isinstance(fd, int):
        try:
            fd = fd.fileno()
        except ValueError:
            return False

    try:
        # Get terminal's current EOF character
        cc = termios.tcgetattr(fd)[6]
        eof = cc[termios.VEOF]
        # # If the last sent byte wasn't an EOL, the EOF will only
        # # end the current line, not actually function as close
        # if last_sent is not None:
        #     # VEOL/VEOL2 as null bytes don't seem to matter...
        #     # eol = { b'\n' }
        #     # eol = { eof }
        #     eol = { b'\n', eof }
        #     if last_sent[-1:] not in eol:
        #         eof = eof * 2
        try:
            n = os.write(fd, eof)
        except (BlockingIOError, InterruptedError):
            n = None

        return n is not None and n > len(eof)

    # Will also catch BlockingIOError
    except OSError:
        return False
