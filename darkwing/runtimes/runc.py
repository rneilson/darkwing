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
    output_isatty, resize_tty,
)

class RuncExecutor(object):

    FORWARD_SIGNALS = ['SIGABRT', 'SIGINT', 'SIGHUP', 'SIGTERM', 'SIGQUIT']

    def __init__(self, stdin=None, stdout=None, stderr=None):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        # Host process state
        self._signals = {}
        self._signal_rfd = None
        self._signal_wfd = None
        self._is_subreaper = False
        self._console_socket = None
        self._old_tty_settings = None
        # Container process state
        self._containers = {}
        self._other_pids = {}
        self._condition = threading.Condition()
        self._waiters = deque()

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

    def _close_stdio(self):
        for fd in [self.stdin, self.stdout, self.stderr]:
            if fd:
                try:
                    fd.close()
                except OSError as e:
                    # TODO: log
                    continue

    # Setup/teardown
    def _set_tty_raw(self):
        fd = self.stdin.fileno()

        if not os.isatty(fd):
            return False

        if self._old_tty_settings is not None:
            return True

        self._old_tty_settings = termios.tcgetattr(fd)
        tty.setraw(fd)

        return True

    def _reset_tty(self):
        fd = self.stdin.fileno()

        if not os.isatty(fd):
            return False

        if self._old_tty_settings is None:
            return True

        termios.tcsetattr(fd, termios.TCSAFLUSH, self._old_tty_settings)
        self._old_tty_settings = None

        return True

    def _setup_signals(self):
        # Create signal socketpair

        # Set signal fd

        # Setup signal handlers

        pass

    def _restore_signals(self):
        # Unset signal fd

        # Restore signal handlers

        # Close signal socketpair

        pass

    def _set_subreaper(self, target=True):
        if self._is_subreaper != target:
            self._is_subreaper = set_subreaper(target)

        return self._is_subreaper

    def _process_signals(self):
        # Read from signal fd
        # If SIGCHLD, reap
        # If SIGWINCH, resize container tty
        # If in FORWARD_SIGNALS, forward to container
        # Otherwise ignore

        # End once all containers exited
        pass

    # Main loop

    def run_until_complete(self, container):
        self._setup_stdio()

        try:
            # Internal setup
            self._set_tty_raw()
            self._setup_signals()
            self._set_subreaper()

            # Assuming container unpacked and ready
            # TODO: allow running multiple containers
            self.create_container(container)

            self.start_container(container)

            # Loop reading from signal fd, forwarding signals
            # and waitpid()'ing on SIGCHLD
            self._process_signals()

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

    def run_container(self, container):
        raise NotImplementedError

    # Other methods

    def exec_in_container(self, container):
        raise NotImplementedError

    def remove_container(self, container):
        raise NotImplementedError
