"""One card mutation lock per base; nested helpers reuse it within a process.

Lock order: cards -> allocator / ledger / views. Views takes cards then views, so
no builder can publish an old read after a newer writer. Lockfiles are host-local;
only durable mutation/rebuild receipts live in the synchronized base.
"""
from contextlib import contextmanager
import fcntl
import functools
import os
from pathlib import Path
import tempfile
import threading

import khms_paths as P
from khms_runtime import default_runtime

_local = threading.local()
_threads = threading.RLock()


def runtime(root=None):
    root = root or P.ROOT
    return P.RUNTIME if P.same_path(root, P.soft('ROOT')) else default_runtime(root)


@contextmanager
def lock(name, root=None):
    directory = Path(runtime(root))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = str(directory.resolve() / (name + '.lock'))
    with _threads:
        held = getattr(_local, 'held', {})
        _local.held = held
        if path in held:
            yield
            return
        with open(path, 'a') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            held[path] = fh
            try:
                yield
            finally:
                del held[path]
                fcntl.flock(fh, fcntl.LOCK_UN)


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def dirty(root=None):
    root = root or P.ROOT
    shared = P.SHARED_STATE if P.same_path(root, P.soft('ROOT')) else os.path.join(root, 'state', 'shared')
    path = os.path.join(shared, 'views-dirty')
    atomic_text(path, 'card transaction started; rebuild views before claiming fresh\n')
    return path


def serialized(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        P.require_writable(fn.__name__)
        with lock('cards'):
            return fn(*args, **kwargs)
    return wrapped
