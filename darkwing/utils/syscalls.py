import ctypes
import ctypes.util
import errno
import os

_libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)

PR_SET_PDEATHSIG = 1
PR_SET_NAME = 15
PR_SET_CHILD_SUBREAPER = 36

def set_subreaper(target=True):
    arg2 = 1 if target else 0
    _libc.prctl(PR_SET_CHILD_SUBREAPER, arg2, 0, 0, 0)

def unshare_namespaces():
    raise NotImplementedError
