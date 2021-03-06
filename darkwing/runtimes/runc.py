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
import array
import json
import errno
import traceback
from pathlib import Path
from functools import partial
from collections import deque

from darkwing.utils import (
    get_runtime_path, ensure_dirs, simple_command, compute_returncode,
    set_subreaper, output_isatty, resize_tty, send_tty_eof,
)
from . import spec

def _noop_sighandler(signum, frame):
    pass

def _raise_sighandler(signum, frame):
    raise Exception(f'Caught signal {signum}')

def iopump(read_from, write_to, stop_event=None, pipe_eof=True,
           select_timeout=0.2, future=None, print_exc=False):
    # Allow giving raw fds
    if isinstance(read_from, int):
        read_from = open(read_from, 'rb', buffering=0)
    if isinstance(write_to, int):
        write_to = open(write_to, 'wb', buffering=0)

    # Anything else to init?
    buf = bytearray()
    bufsize = io.DEFAULT_BUFFER_SIZE // 2
    use_read1 = hasattr(read_from, 'read1')
    exc = None

    # Specialty EOF handling
    if write_to.isatty():
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
            bufsize = min(bufsize, select.PIPE_BUF)

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
            readable, writeable, _ = select.select(
                rlist, wlist, [], select_timeout
            )

            # Write end in readable list means pipe closed
            if write_to in readable:
                raise BrokenPipeError

            # Now we read
            if read_from:
                if stop_event and stop_event.is_set():
                    # External stop event, assume closed
                    data = b''
                elif write_to.closed:
                    # Write end already closed, stop now
                    data = b''
                elif read_from in readable:
                    try:
                        # Buffered fileobjs might have .read1(), so use that
                        if use_read1:
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

            # Intermezzo
            if buf and write_to not in wlist and write_to not in writeable:
                _, writeable, _ = select.select(
                    [], [write_to], [], 0
                )

            # And now we write
            if write_to in writeable:
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
            exc = None
            future = None


class RuncError(Exception):

    def __init__(self, name, message, code=1):
        super().__init__(f'runc error for container "{name}": {message}')
        self.name = name
        self.message = message
        self.code = code

    def __str__(self):
        return f'Error for container "{self.name}": {self.message}'

    # TODO: __repr__()


class RuncExecutor(object):

    # TODO: add (configurable) signal to force-kill containers
    FORWARD_SIGNALS = (
        # signal.SIGABRT,
        signal.SIGINT,
        signal.SIGHUP,
        signal.SIGTERM,
        signal.SIGQUIT,
    )
    RAISE_ON_SIGNALS = (
        signal.SIGABRT,
        # signal.SIGALRM,
    )

    def __init__(self, context_name='default', state_dir=None,
                 stdin=None, stdout=None, stderr=None, close_stdio=False,
                 uid=None, gid=None, debug=False, log_file=sys.stderr):
        # Stdio
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.tty_fd = None
        self.tty_raw = None
        self.debug = bool(debug)
        self._log_file = log_file
        self._log_isatty = log_file and log_file.isatty()
        # Host process state
        self.uid = uid
        self.gid = gid
        self.pid = os.getpid()
        self.returncode = None
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

    def _write_log(self, message, flush=True):
        if self._log_file:
            if self._log_isatty:
                # Bit of a correction for tty newline weirdness
                message = '\r\n'.join(message.splitlines()) + '\r'
            print(message, file=self._log_file, flush=flush)

    def _debug_log(self, message, flush=True):
        if self.debug:
            self._write_log(message, flush=flush)

    def _ensure_state_dir(self):
        return bool(ensure_dirs(
            [(self._state_dir, 0o770)],
            uid=self.uid, gid=self.gid,
        ))

    def _setup_stdio(self, close_stdin=True, close_stdout=True):
        if self.stdin is None:
            # Override any possible text/buffered stdin, reopen as binary
            self.stdin = sys.stdin.fileno()
            close_stdin = close_stdin and not sys.stdin.isatty()
        elif (not hasattr(self.stdin, 'read') and
                hasattr(self.stdin, 'detach')):
            # Socket object, so remake into normal(ish) fileobj
            self.stdin = self.stdin.detach()
        # (Re)open raw fd
        if isinstance(self.stdin, int):
            self.stdin = open(
                self.stdin, 'rb', buffering=0, closefd=close_stdin
            )

        if self.stdout is None:
            # Override any possible text/buffered stdout, reopen as binary
            self.stdout = sys.stdout.fileno()
            close_stdout = close_stdout and not sys.stdout.isatty()
        elif (not hasattr(self.stdout, 'write') and
                hasattr(self.stdout, 'detach')):
            # Socket object, so remake into normal(ish) fileobj
            self.stdout = self.stdout.detach()
        # (Re)open raw fd
        if isinstance(self.stdout, int):
            self.stdout = open(
                self.stdout, 'wb', buffering=0, closefd=close_stdout
            )

        if self.stderr is None:
            # Override any possible text/buffered stderr, reopen as binary
            self.stderr = sys.stderr.fileno()
        elif (not hasattr(self.stderr, 'write') and
                hasattr(self.stderr, 'detach')):
            # Socket object, so remake into normal(ish) fileobj
            self.stderr = self.stderr.detach()
        # (Re)open raw fd
        if isinstance(self.stderr, int):
            # If this proc's stderr, don't actually close underlying fd
            # when closing fileobj, so we can write debug info if req'd
            self.stderr = open(
                self.stderr, 'wb', buffering=0,
                closefd=(self.stderr != sys.stderr.fileno())
            )

        # Check if we're in a tty
        for fd in [self.stdin, self.stdout, self.stderr]:
            if not fd:
                continue
            if fd.isatty():
                # Grab a fresh copy of the tty for resizing/settings
                self.tty_fd = os.open(
                    os.ttyname(fd.fileno()), os.O_NOCTTY | os.O_CLOEXEC)
                # # If stdin is a tty, we'll also need to set input raw
                # self.tty_raw = (fd is self.stdin)
                break

    def _close_stdio(self):
        # Close our extra tty fd
        if self.tty_fd is not None:
            os.close(self.tty_fd)
            self.tty_fd = None

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
        if self.tty_fd is None or not self.tty_raw:
            return False

        if self._old_tty_settings is not None:
            return True

        self._old_tty_settings = termios.tcgetattr(self.tty_fd)
        tty.setraw(self.tty_fd, termios.TCSANOW)
        self._debug_log('Set TTY to raw mode')

        return True

    def _setup_tty(self, container):
        # Determine TTY
        # Only use tty mode if both host and container using ttys
        if self.tty_fd is not None:
            self.tty_raw = (
                container and
                container.use_tty and
                output_isatty(self.stdin, self.stdout)
            )
        if container and container.use_tty and not self.tty_raw:
            container.use_tty = False
        self._debug_log(f'TTY fd: {self.tty_fd}, raw: {self.tty_raw}')

        return self._set_tty_raw()

    def _reset_tty(self):
        if self.tty_fd is None or not self.tty_raw:
            return False

        if self._old_tty_settings is None:
            return True

        # TODO: use TCSANOW?
        termios.tcsetattr(self.tty_fd, termios.TCSAFLUSH, self._old_tty_settings)
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
        # Escape-hatch signals
        for sig in self.RAISE_ON_SIGNALS:
            self._signals[sig] = signal.signal(sig, _raise_sighandler)

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
                    # TODO: let bubble up instead?
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
        if self.tty_fd is None:
            return

        # Get new terminal size
        columns, lines = os.get_terminal_size(self.tty_fd)

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
                        # TODO: catch ChildProcessError, and
                        # wait() all still-running containers
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

    def _get_returncode(self):
        # Return first nonzero return code, or 0
        with self._condition:
            for pid, con in self._containers.items():
                if con.returncode:
                    return con.returncode
            # If any containers and no errors, return 0
            if self._containers:
                return 0
        # No containers to speak of
        return None

    def _close(self):
        with self._condition:
            self._closing = True
        # Close all containers
        for pid, con in self._containers.items():
            # TODO: send sigkill if still running?
            con.close()
        # TODO: terminate & wait for other processes
        # Notify waiters
        with self._condition:
            self._condition.notify_all()

    # Main loop

    def run_until_complete(self, container, remove=True):
        with self._condition:
            if self._closing:
                raise RuntimeError('Cannot run when closing')

        # Runc setup
        self._ensure_state_dir()

        try:
            # Internal setup
            self._setup_stdio()
            self._setup_tty(container)
            self._setup_signals()
            self._set_subreaper(True)
            self._debug_log('Internal setup complete')

            # Assuming container unpacked and ready
            # TODO: allow running multiple containers
            self.create_container(container)
            self._debug_log(f'Container created (tty: {container.use_tty})')

            # TODO: setup container networking
            
            # Initial tty resize
            # TODO: set tty raw here instead?
            self._resize_tty()

            # Anything else in particular before we start?
            # TODO: multiple
            self.start_container(container)
            self._debug_log('Container started')

            # Loop reading from signal fd, forwarding signals
            # and waitpid()'ing on SIGCHLD
            self._process_signals()
            self._debug_log('Container finished')

            # TODO: teardown container networking

            # Get container return code before removing
            self.returncode = self._get_returncode()

            # Cleanup container remnants
            # TODO: multiple
            if remove:
                self.remove_container(container)
                self._debug_log('Container removed')

        except Exception as e:
            self._write_log(traceback.format_exc())
            if isinstance(e, RuncError):
                self.returncode = e.code
            else:
                self.returncode = 1

        finally:
            # Internal teardown
            self._close()
            self._set_subreaper(False)
            self._restore_signals()
            self._reset_tty()
            self._close_stdio()
            self._debug_log('Internal teardown complete')

        return self.returncode

    # Container lifecycle methods

    def _base_runc_cmd(self, command):
        return ['runc', '--root', str(self._state_dir), command]

    def _create_container_tty(self, container, runc_cmd):
        tty_socket_path = container.rundir_path / 'tty.sock'
        runc_cmd += [
            '--console-socket',
            str(tty_socket_path),
            container.name,
        ]

        # Init socket
        try:
            tty_socket_path.unlink()
        except FileNotFoundError:
            pass
        tty_socket = socket.socket(socket.AF_UNIX)
        tty_socket.settimeout(0.2)
        tty_socket.bind(str(tty_socket_path))
        tty_socket.listen()

        # Run create command
        try:
            sock = None
            proc = subprocess.Popen(
                runc_cmd, cwd=container.path, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

            try:
                # Get new tty through socket
                sock, _ = tty_socket.accept()
                sock.settimeout(0.2)
                fds = array.array('i')
                msg, ancdata, flags, _ = sock.recvmsg(
                    4096, socket.CMSG_LEN(fds.itemsize)
                )
                for cmsg_level, cmsg_type, cmsg_data in ancdata:
                    if (cmsg_level == socket.SOL_SOCKET and
                            cmsg_type == socket.SCM_RIGHTS):
                        # Only expecting a single fd
                        fd_len = len(cmsg_data) - (len(cmsg_data) % fds.itemsize)
                        fds.fromstring(cmsg_data[:fd_len])
            finally:
                if sock:
                    sock.close()
                # Now handle start process
                returncode = proc.wait()
                if returncode:
                    errmsg = proc.stderr.read().decode(errors='surrogateescape')
                    raise RuncError(container.name, errmsg, code=returncode)
        finally:
            tty_socket.close()
            tty_socket_path.unlink()

        if len(fds) == 0:
            if msg:
                errmsg = msg.decode(errors='surrogateescape')
            else:
                errmsg = "Couldn't get tty"
            raise RuncError(container.name, errmsg)

        # Set up container stdio with new socket
        tty = fds[0]
        container.tty = tty
        container._close_fds.append(tty)
        container.stdin = open(tty, 'wb', buffering=0, closefd=False)
        container.stdout = open(tty, 'rb', buffering=0, closefd=False)
        container.stderr = open(tty, 'rb', buffering=0, closefd=False)

        return container

    def _create_container_notty(self, container, runc_cmd):
        runc_cmd += [container.name]

        # Create pipes
        stdin_p, stdin_c = socket.socketpair()
        stdout_p, stdout_c = socket.socketpair()
        stderr_p, stderr_c = socket.socketpair()

        try:
            proc = subprocess.Popen(
                runc_cmd, stdin=stdin_c, stdout=stdout_c, stderr=stderr_c,
            )
            proc.wait()
            if proc.returncode:
                try:
                    # Get error message
                    msg = bytearray()
                    stdout_p.setblocking(False)
                    stderr_p.setblocking(False)
                    for fd in (stdout_p, stderr_p):
                        try:
                            while True:
                                data = stderr_p.recv(4096)
                                if data:
                                    msg.extend(data)
                                else:
                                    break
                        except BlockingIOError:
                            if msg:
                                break
                            continue
                        else:
                            break
                    errmsg = msg.decode(errors='surrogateescape')
                except:
                    errmsg = f"Command 'runc create' returned {proc.returncode}"
                raise RuncError(container.name, errmsg, code=proc.returncode)
        except Exception as e:
            for fileobj in [stdin_p, stdout_p, stderr_p]:
                fileobj.close()
            if isinstance(e, RuncError):
                raise
            errmsg = f'Error creating container: {e!r}'
            raise RuncError(container.name, errmsg) from None
        finally:
            for fileobj in [stdin_c, stdout_c, stderr_c]:
                fileobj.close()

        container.stdin = open(stdin_p.detach(), 'wb', buffering=0)
        container.stdout = open(stdout_p.detach(), 'rb', buffering=0)
        container.stderr = open(stderr_p.detach(), 'rb', buffering=0)

        return container

    def _setup_container_stdio(self, container):
        # Event to help stop i/o threads
        stop_ev = threading.Event()
        container._stop_event = stop_ev
        # Shorter select timeout if a tty
        select_time = 0.1 if container.use_tty else 0.2
        # Setup each i/o thread
        if container.stdin:
            container._io_threads.append(threading.Thread(
                name=f"{container.name}-stdin", daemon=False,
                target=iopump, args=(self.stdin, container.stdin),
                kwargs={'stop_event': stop_ev, 'select_timeout': select_time},
            ))
        if container.stdout:
            container._io_threads.append(threading.Thread(
                name=f"{container.name}-stdout", daemon=False,
                target=iopump, args=(container.stdout, self.stdout),
                kwargs={'stop_event': stop_ev, 'select_timeout': select_time},
            ))
        if container.stderr:
            container._io_threads.append(threading.Thread(
                name=f"{container.name}-stderr", daemon=False,
                target=iopump, args=(container.stderr, self.stderr),
                kwargs={'stop_event': stop_ev, 'select_timeout': select_time},
            ))
        for t in container._io_threads:
            t.start()

        return container

    def _check_container_pidfile(self, container):
        # Check if container pidfile exists, remove if stopped
        try:
            pid = container.pidfile_path.read_text()
            if pid:
                pid = int(pid)
            # Check if container process running
            if pid and pid != container.pid:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    # PID not running, carry on
                    container.pidfile_path.unlink()
                else:
                    # Technically this doesn't guarantee the pid
                    # in question is this container, but /shrug
                    # TODO: check state
                    errmsg = f"Container already exists"
                    raise RuncError(container.name, errmsg)
        except FileNotFoundError:
            # Good to go
            pass

    def _check_container_lockfile(self, container):
        # Check if container lockfile exists, remove if stopped
        try:
            pid = container.lockfile_path.read_text()
            if pid:
                pid = int(pid)
            # Check if darkwing process running
            if pid and pid != self.pid:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    # PID not running, carry on
                    container.lockfile_path.unlink()
                else:
                    # Technically this doesn't guarantee the pid
                    # in question is this container, but /shrug
                    # TODO: check state
                    errmsg = f"Container already in use"
                    raise RuncError(container.name, errmsg)
        except FileNotFoundError:
            # Good to go
            pass

    def _write_container_lockfile(self, container):
        # TODO: use open() with an opener to set mode
        container.lockfile_path.write_text(str(self.pid))

    def _get_container_state(self, container, update=False,
                             raise_on_failure=True):
        runc_cmd = self._base_runc_cmd('state') + [container.name]
        proc = simple_command(runc_cmd, write_output=False)
        proc_out = proc.stdout.encode(errors='surrogateescape')
        if proc.returncode:
            if raise_on_failure:
                # TODO: figure out how to kill container w/o pid
                errmsg = proc_out or f'Error getting container state'
                raise RuncError(container.name, errmsg, code=proc.returncode)
            return None

        # Parse state
        state = json.loads(proc_out)
        
        if update:
            container.status = state['status']
            if state['pid']:
                container.pid = state['pid']

        return state

    def create_container(self, container):
        with self._condition:
            if self._closing:
                raise RuntimeError('Cannot create container when closing')
            if container.pid:
                raise RuntimeError(
                    f'Container {container.name} already '
                    f'created (pid {container.pid})'
                )
            # TODO: internally lock container

        if not container.rundir:
            container.make_rundir()

        # Ensure not clobbering another process
        self._check_container_pidfile(container)
        self._check_container_lockfile(container)
        # Lock unpacked container now
        self._write_container_lockfile(container)

        # Update OCI spec file
        spec.update_spec_file(
            container.config, container.rundir,
            allow_tty=container.use_tty,
            # TODO: force_tty?
        )

        # Create container runc-side
        runc_cmd = self._base_runc_cmd('create')
        # TODO: other options? Specify '--rootless'?
        runc_cmd.extend([
            '--bundle', str(container.path),
            '--pid-file', str(container.pidfile_path),
        ])

        # TODO: only setup tty if stdin and stdout are (the same?) tty
        if container.use_tty:
            self._create_container_tty(container, runc_cmd)
        else:
            self._create_container_notty(container, runc_cmd)

        # Now get state
        state = self._get_container_state(container, update=True)
        # TODO: check pid too?
        if state['status'] != 'created':
            raise RuncError(
                container.name, f"Unexpected status {state['status']}"
            )

        with self._condition:
            # TODO: ensure exclusive
            # TODO: by name as well?
            self._containers[container.pid] = container

        # Start up i/o thread(s)
        self._setup_container_stdio(container)

        return container

    def start_container(self, container):
        if not container.pid or container.status != 'created':
            raise RuntimeError(
                f'Cannot start {container.status} '
                f'container "{container.name}"'
            )
        
        runc_cmd = self._base_runc_cmd('start') + [container.name]
        proc = simple_command(runc_cmd, write_output=False)
        proc_out = proc.stdout.encode(errors='surrogateescape')
        if proc.returncode:
            # TODO: Kill container?
            errmsg = proc_out or f'Error starting container'
            raise RuncError(container.name, errmsg, code=proc.returncode)

        # Now get state
        state = self._get_container_state(container, update=True)
        if state['status'] != 'running':
            raise RuncError(
                container.name, f"Unexpected status {state['status']}"
            )

        return container

    def stop_container(self, container):
        raise NotImplementedError

    def remove_container(self, container):
        state = self._get_container_state(container, raise_on_failure=False)
        if not state:
            return False
        if state['status'] != 'stopped':
            raise RuncError(
                container.name, 'Cannot remove container unless stopped'
            )

        runc_cmd = self._base_runc_cmd('delete') + [container.name]
        proc = simple_command(runc_cmd, write_output=False)
        proc_out = proc.stdout.encode(errors='surrogateescape')
        if proc.returncode:
            errmsg = proc_out or f'Error removing container'
            raise RuncError(container.name, errmsg, code=proc.returncode)

        # Remove pidfile, lockfile
        with self._condition:
            try:
                container.pidfile_path.unlink()
            except FileNotFoundError:
                pass
            try:
                container.lockfile_path.unlink()
            except FileNotFoundError:
                pass
            if container.pid in self._containers:
                del self._containers[container.pid]

        container.status = 'removed'
        return container

    # Other methods

    def run_container(self, container):
        raise NotImplementedError

    def exec_in_container(self, container):
        raise NotImplementedError
