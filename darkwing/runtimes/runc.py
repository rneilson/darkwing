import os
import sys
import json
import socket
import threading
import subprocess
import tty
import termios
from pathlib import Path
from functools import partial
from collections import deque

from darkwing.utils import (
    compute_returncode, set_subreaper,
    output_isatty, resize_tty, send_tty_eof,
)

def _noop_sighandler():
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
            # For now, don't actually close underlying fd when closing
            # fileobj, so we can reset terminal settings if req'd
            self.stdin = open(self.stdin, 'rb', buffering=0, closefd=False)

        if self.stdout is None:
            # Override any possible text/buffered stdout, reopen as binary
            self.stdout = sys.stdout.fileno()
        elif (not hasattr(self.stdout, 'write') and
                hasattr(self.stdout, 'detach')):
            # Socket object, so remake into normal(ish) fileobj
            self.stdout = self.stdout.detach()
        # (Re)open raw fd
        if isinstance(self.stdout, int):
            # We *do* want to close stdout once complete, so that
            # piped output properly propagates closure
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
            if os.isatty(fd.fileno()):
                self.tty = fd.fileno()
                break

    def _close_stdio(self):
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
        if self.tty is None:
            return False

        if self._old_tty_settings is None:
            return True

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
                if container.returncode is None and container.tty:
                    resize_tty(container.tty, columns, lines)

    def _send_signal(self, sig=signal.SIGTERM):
        # Send signal to all still-running containers
        with self._condition:
            for pid, container in self._containers.items():
                if container.returncode is None:
                    os.kill(pid, sig)

    def _process_signals(self):
        complete = False

        while not complete:
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
