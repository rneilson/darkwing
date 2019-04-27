import os
import sys
import stat
import io
import tty
import termios
import socket
import select
import threading
import subprocess
import json
from pathlib import Path
from functools import partial
from collections import deque

from darkwing.utils import (
    compute_returncode, set_subreaper,
    output_isatty, resize_tty, send_tty_eof,
)

def _noop_sighandler():
    pass

def iopump(read_from, write_to, tty_eof=False, pipe_eof=True,
           select_timeout=None, future=None):
    # Allow giving raw fds
    if isinstance(read_from, int):
        read_from = open(read_from, 'rb', buffering=0)
    if isinstance(write_to, int):
        write_to = open(write_to, 'wb', buffering=0)

    # Anything else to init?
    buf = bytearray()
    bufsize = io.DEFAULT_BUFFER_SIZE / 2
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
        # Are we cool yet
        while read_from or buf:
            rlist, wlist = [], []
            # Read if room in buffer
            if read_from and len(buf) < buf_size:
                rlist.append(read_from)
            # Write if data in buffer
            if buf:
                wlist.append(write_to)

            # This _shouldn't_ ever happen, but...
            if not rlist and not wlist:
                raise RuntimeError("Can't read and can't write...")

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
            if read_from and read_from in rlist:
                try:
                    # Buffered fileobjs might have .read1(), so use that
                    if hasattr(read_from, 'read1'):
                        data = read_from.read1(buf_size)
                    else:
                        data = read_from.read(buf_size)
                except (BlockingIOError, InterruptedError):
                    pass
                else:
                    if data is None:
                        # Lovely race condition between 'readable'
                        # and actually reading...
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
                    data = buf[:buf_size]
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
                    last_byte = buf[sent - 1:sent]
                    del buf[:sent]
            # Bit of housekeeping
            data = None
            sent = None
    except Exception as e:
        exc = e
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

        # TODO: set result/exception
        if future:
            pass


class RuncExecutor(object):

    FORWARD_SIGNALS = (
        signal.SIGABRT,
        signal.SIGINT,
        signal.SIGHUP,
        signal.SIGTERM,
        signal.SIGQUIT,
    )

    def __init__(self, stdin=None, stdout=None, stderr=None):
        # Stdio
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.tty = None
        self.tty_raw = None
        # Host process state
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
        self._waiters = deque()
        self._closing = False

    def _setup_stdio(self):
        if self.stdin is None:
            # Override any possible text/buffered stdin, reopen as binary
            self.stdin = self.stdin.fileno()
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
        if self.tty is None:
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

        # Set signal fd
        signal.set_wakeup_fd(self._sig_wsock.fileno())

        # Setup signal handlers
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
        self._sig_rsock.close()
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
                    # TODO: possible callback?
                    if self._other_pids[pid] is None:
                        self._other_pids[pid] = compute_returncode(sts)
                else:
                    # TODO: log
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
        while True:
            # Read from signal fd
            try:
                data = self._sig_rsock.recv(4096)
            except InterruptedError:
                continue

            for sig in data:
                if sig == signal.SIGCHLD:
                    # Wait for all children exited since last
                    self._reap()
                elif sig == signal.SIGWINCH:
                    # Resize container tty
                    self._resize_tty()
                elif sig in self.FORWARD_SIGNALS:
                    # Forward to container
                    self._send_signal(sig)
                else:
                    # Otherwise ignore
                    continue

            # End once all containers exited
            with self._condition:
                alive = [
                    pid for pid, con in self._containers
                    if con.returncode is None
                ]
                # TODO: anything to do with still-running containers?
                if not alive:
                    self._closing = True

            if self._closing:
                break

    # Main loop

    def run_until_complete(self, container):
        self._setup_stdio()

        # Internal setup
        self._set_tty_raw()
        self._setup_signals()
        self._set_subreaper()

        try:
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

            # Cleanup container remnants
            # TODO: make optional
            # TODO: multiple
            self.remove_container(container)

        finally:
            # Internal teardown
            self._set_subreaper(False)
            self._restore_signals()
            self._reset_tty()

        # Notify waiters

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
