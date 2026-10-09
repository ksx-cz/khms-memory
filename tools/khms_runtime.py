"""Host-local location: same base + same host, independent of agent HOME or clone."""
import hashlib
import os
import sys


def default_runtime(root):
    return '/tmp/khms-runtime-' + str(os.getuid()) + '-' + hashlib.sha256(
        os.path.realpath(root).encode()).hexdigest()[:24]


if __name__ == '__main__':
    print(default_runtime(sys.argv[1]))
