import os
import sys
import stat
import io
import tty
import termios
import socket
import select
import signal
import threading
import subprocess
import json
import errno
import traceback
from pathlib import Path
from functools import partial
from collections import deque

from darkwing.utils import (
    get_runtime_path, ensure_dirs,
    compute_returncode, set_subreaper,
    output_isatty, resize_tty, send_tty_eof,
)

def _noop_sighandler(signum, frame):
    pass

def iopump(read_from, write_to, stop_event=None, tty_eof=False,
           pipe_eof=True, select_timeout=0.1, future=None, print_exc=False):
    # Allow giving raw fds
    if isinstance(read_from, int):
        read_from = open(read_from, 'rb', buffering=0)
    if isinstance(write_to, int):
        write_to = open(write_to, 'wb', buffering=0)

    # Anything else to init?
    buf = bytearray()
    bufsize = io.DEFAULT_BUFFER_SIZE // 2
    last_byte = None
    exc = None

    # Specialty EOF handling
    if tty_eof:
        tty_eof = write_to.isatty()
        pipe_eof = False
    elif pipe_eof:
        # If write end is a pipe, we can do the reader trick
        # to get notified of the other side's closing
        # (Taken from asyncio)
        mode = os.fstat(write_to.fileno()).st_mode
        pipe_eof = stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)
        if pipe_eof:
            # Ensure we use at most the max pipe writeable size
            # TODO: set write_to non-blocking?
            bufsize = min(bufsize, select.BUF_SIZE)

    try:
        # Set future running, or bail if cancelled
        if future:
            if not future.set_running_or_notify_cancel():
                return
        # Are we cool yet
        while read_from or buf:
            rlist, wlist = [], []
            if read_from:
                # Check for closed read end
                if read_from.closed:
                    try:
                        read_from.close()
                    except OSError as e:
                        pass
                    read_from = None
                # Read if room in buffer
                elif len(buf) < bufsize:
                    rlist.append(read_from)
            # Write if data in buffer
            if buf:
                wlist.append(write_to)

            # Everything's dead, Dave...
            if not rlist and not wlist:
                break

            # Watch for write-end closing
            if pipe_eof:
                rlist.append(write_to)

            # Wait for something available
            rlist, wlist, _ = select.select(
                rlist, wlist, [], select_timeout
            )

            # Write end in readable list means pipe closed
            if write_to in rlist:
                raise BrokenPipeError

            # Now we read
            if read_from:
                if read_from in rlist:
                    try:
                        # Buffered fileobjs might have .read1(), so use that
                        if hasattr(read_from, 'read1'):
                            data = read_from.read1(bufsize)
                        else:
                            data = read_from.read(bufsize)
                    except (BlockingIOError, InterruptedError):
                        data = None
                    except OSError as e:
                        if e.errno == errno.EIO:
                            # Stream closed
                            data = b''
                        else:
                            raise
                elif write_to.closed:
                    # Write end already closed, stop now
                    data = b''
                elif stop_event and stop_event.is_set():
                    # External stop event, assume closed
                    data = b''
                else:
                    data = None

                if data is None:
                    pass
                elif data:
                    buf.extend(data)
                else:
                    # Closed
                    try:
                        read_from.close()
                    except OSError as e:
                        pass
                    read_from = None

            # And now we write
            if write_to in wlist:
                try:
                    data = buf[:bufsize]
                    sent = write_to.write(data)
                except BlockingIOError as e:
                    sent = e.characters_written
                except InterruptedError:
                    sent = 0
                # In case write_to is buffered
                try:
                    write_to.flush()
                except (BlockingIOError, InterruptedError):
                    # Don't care about internal buffer bytes written
                    pass
                # Update buffer
                if sent:
                    last_byte = bytes(buf[sent - 1:sent])
                    del buf[:sent]

            # Bit of housekeeping
            data = None
            sent = None
    except Exception as e:
        exc = e
        if print_exc:
            msg = f'I/O exception, from={read_from!r}, to={write_to!r}\r\n'
            msg += traceback.format_exc().replace('\n', '\r\n')
            print(msg, file=sys.stderr)
    finally:
        # We done
        buf.clear()

        # TODO: set exception?
        if read_from:
            try:
                read_from.close()
            except OSError as e:
                pass
        if tty_eof:
            try:
                send_tty_eof(write_to, last_sent=last_byte)
            except OSError as e:
                pass
        try:
            write_to.close()
        except OSError as e:
            pass
        read_from = None
        write_to = None

        if future and not future.cancelled():
            if exc:
                future.set_exception(exc)
            else:
                future.set_result(True)
            # I've seen similar in the threading executor code,
            # so let's avoid any potential refcycle problems
            del exc
            del future


class RuncExecutor(object):

    # TODO: add (configurable) signal to force-kill containers
    FORWARD_SIGNALS = (
        signal.SIGABRT,
        signal.SIGINT,
        signal.SIGHUP,
        signal.SIGTERM,
        signal.SIGQUIT,
    )

    def __init__(self, context_name='default', state_dir=None,
                 stdin=None, stdout=None, stderr=None, uid=None, gid=None):
        # Stdio
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.tty = None
        self.tty_raw = None
        # Host process state
        self.uid = uid
        self.gid = gid
        self._signals = {}
        self._sig_rsock = None
        self._sig_wsock = None
        self._is_subreaper = False
        self._console_socket = None
        self._old_tty_settings = None
        # Container process state
        self._containers = {}
        self._other_pids = {}
        self._condition = threading.Condition()
        self._running = None
        self._closing = None
        # Runc state dir
        if state_dir is None:
            self._state_dir = get_runtime_path(uid) / context_name / '.runc'
        else:
            self._state_dir = Path(state_dir) / context_name / '.runc'

    def _ensure_state_dir(self):
        return bool(ensure_dirs(
            [(self._state_dir, 0o770)],
            uid=self.uid, gid=self.gid,
        ))

    def _setup_stdio(self):
        if self.stdin is None:
            # Override any possible text/buffered stdin, reopen as binary
            self.stdin = sys.stdin.fileno()
        elif (not hasattr(self.stdin, 'read') and
                hasattr(self.stdin, 'detach')):
            # Socket object, so remake into normal(ish) fileobj
            self.stdin = self.stdin.detach()
        # (Re)open raw fd
        if isinstance(self.stdin, int):
            self.stdin = open(self.stdin, 'rb', buffering=0)

        if self.stdout is None:
            # Override any possible text/buffered stdout, reopen as binary
            self.stdout = sys.stdout.fileno()
        elif (not hasattr(self.stdout, 'write') and
                hasattr(self.stdout, 'detach')):
            # Socket object, so remake into normal(ish) fileobj
            self.stdout = self.stdout.detach()
        # (Re)open raw fd
        if isinstance(self.stdout, int):
            self.stdout = open(self.stdout, 'wb', buffering=0)

        if self.stderr is None:
            # Override any possible text/buffered stderr, reopen as binary
            self.stderr = sys.stderr.fileno()
        elif (not hasattr(self.stderr, 'write') and
                hasattr(self.stderr, 'detach')):
            # Socket object, so remake into normal(ish) fileobj
            self.stderr = self.stderr.detach()
        # (Re)open raw fd
        if isinstance(self.stderr, int):
            # For now, don't actually close underlying fd when closing
            # fileobj, so we can write debug info if req'd
            self.stderr = open(self.stderr, 'wb', buffering=0, closefd=False)

        # Check if we're in a tty
        for fd in [self.stdin, self.stdout, self.stderr]:
            if not fd:
                continue
            if fd.isatty():
                # Grab a fresh copy of the tty for resizing/settings
                self.tty = os.open(
                    os.ttyname(fd.fileno()), os.O_NOCTTY | os.O_CLOEXEC)
                # If stdin is a tty, we'll also need to set input raw
                self.tty_raw = (fd is self.stdin)
                break

    def _close_stdio(self):
        # Close our extra tty fd
        if self.tty is not None:
            os.close(self.tty)

        for fd in [self.stdin, self.stdout, self.stderr]:
            if not fd:
                continue
            try:
                fd.close()
            except OSError as e:
                # TODO: log
                continue

    # Setup/teardown
    def _set_tty_raw(self):
        if self.tty is None or not self.tty_raw:
            return False

        if self._old_tty_settings is not None:
            return True

        self._old_tty_settings = termios.tcgetattr(self.tty)
        tty.setraw(self.tty, termios.TCSANOW)

        return True

    def _reset_tty(self):
        if self.tty is None or not self.tty_raw:
            return False

        if self._old_tty_settings is None:
            return True

        # TODO: use TCSANOW?
        termios.tcsetattr(self.tty, termios.TCSAFLUSH, self._old_tty_settings)
        self._old_tty_settings = None

        return True

    def _setup_signals(self):
        # Create signal socketpair
        self._sig_rsock, self._sig_wsock = socket.socketpair()
        # Write end of self-pipe must be non-blocking
        self._sig_wsock.setblocking(False)
        # TODO: for now we want read end blocking, but maybe
        # we'll change that later

        # Set signal fd
        signal.set_wakeup_fd(self._sig_wsock.fileno())

        # Setup signal handlers
        # TODO: include signal to force-kill containers
        sigs = self.FORWARD_SIGNALS + (signal.SIGWINCH, signal.SIGCHLD)
        for sig in sigs:
            # Store old handler for later
            self._signals[sig] = signal.signal(sig, _noop_sighandler)

    def _restore_signals(self):
        # Restore signal handlers
        for sig in list(self._signals.keys()):
            handler = self._signals.pop(sig)
            # Restore old handler
            signal.signal(sig, handler)

        # Unset signal fd
        signal.set_wakeup_fd(-1)

        # Close signal socketpair
        if self._sig_rsock is not None:
            self._sig_rsock.close()
        if self._sig_wsock is not None:
            self._sig_wsock.close()
        self._sig_rsock, self._sig_wsock = None, None

    def _set_subreaper(self, target=True):
        if self._is_subreaper != target:
            self._is_subreaper = set_subreaper(target)

        return self._is_subreaper

    def _reap(self):
        with self._condition:
            # Do waitpid() until no waitable children left
            while True:
                try:
                    pid, sts = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    # No more children
                    break
                if pid == 0:
                    # No more zombies
                    break
                if pid in self._containers:
                    container = self._containers[pid]
                    # Set return code here; container's wait() won't see it
                    if container.returncode is None:
                        container.returncode = compute_returncode(sts)
                elif pid in self._other_pids:
                    other_proc = self._other_pids[pid]
                    returncode = compute_returncode(sts)
                    if other_proc is None:
                        self._other_pids[pid] = returncode
                    elif isinstance(other_proc, subprocess.Popen):
                        # Have to manually update subproc state
                        with other_proc._waitpid_lock:
                            if other_proc.returncode is None:
                                other_proc.returncode = returncode
                    elif callable(other_proc):
                        other_proc(returncode)
                else:
                    # TODO: log martians
                    pass

    def _resize_tty(self):
        if self.tty is None:
            return

        # Get new terminal size
        columns, lines = os.get_terminal_size(self.tty)

        # Send resize to any containers with ttys
        with self._condition:
            for pid, container in self._containers.items():
                if container.returncode is None and container.tty is not None:
                    resize_tty(container.tty, columns, lines)

    def _send_signal(self, sig=signal.SIGTERM):
        # Send signal to all still-running containers
        with self._condition:
            for pid, container in self._containers.items():
                if container.returncode is None:
                    os.kill(pid, sig)

    def _process_signals(self):
        try:
            with self._condition:
                self._running = True
            while True:
                # Read from signal fd
                try:
                    data = self._sig_rsock.recv(4096)
                except InterruptedError:
                    continue
                # Handle signals
                for sig in data:
                    if sig == signal.SIGCHLD:
                        # Wait for all children exited since last
                        self._reap()
                    elif sig == signal.SIGWINCH:
                        # Resize container tty
                        self._resize_tty()
                    elif sig in self.FORWARD_SIGNALS:
                        # Forward to container(s)
                        self._send_signal(sig)
                    else:
                        # TODO: some signal to break and/or force-kill
                        # all still-running (possibly hung) containers?
                        # Otherwise ignore
                        continue
                # End once all containers exited
                with self._condition:
                    alive = [
                        pid for pid, con in self._containers.items()
                        if con.returncode is None
                    ]
                    # TODO: anything to do with still-running containers?
                    if not alive:
                        break
        finally:
            with self._condition:
                self._running = False

    def _close(self):
        with self._condition:
            self._closing = True
        # Close all containers
        for pid, con in self._containers.items():
            # TODO: send sigkill if still running?
            con.close()
        # TODO: terminate & wait for other processes
        # TODO: notify waiters (or after closing?)

    # Main loop

    def run_until_complete(self, container, remove=True):
        # Runc setup
        self._ensure_state_dir()

        try:
            # Internal setup
            self._setup_stdio()
            self._set_tty_raw()
            self._setup_signals()
            self._set_subreaper(True)

            # Assuming container unpacked and ready
            # TODO: allow running multiple containers
            self.create_container(container)

            # TODO: setup container networking
            
            # Initial tty resize
            self._resize_tty()

            # Anything else in particular before we start?
            # TODO: multiple
            self.start_container(container)

            # Loop reading from signal fd, forwarding signals
            # and waitpid()'ing on SIGCHLD
            self._process_signals()

            # TODO: teardown container networking

            # Cleanup container remnants
            # TODO: multiple
            if remove:
                self.remove_container(container)

        finally:
            # Internal teardown
            self._close()
            self._set_subreaper(False)
            self._restore_signals()
            self._reset_tty()
            self._close_stdio()

    # Container lifecycle methods

    def create_container(self, container):
        raise NotImplementedError

    def start_container(self, container):
        raise NotImplementedError

    def stop_container(self, container):
        raise NotImplementedError

    def remove_container(self, container):
        raise NotImplementedError

    # Other methods

    def run_container(self, container):
        raise NotImplementedError

    def exec_in_container(self, container):
        raise NotImplementedError
