"""Process-wide singleton lock.

Implementation: bind a TCP socket to a unique localhost port. If another
instance is already running, the bind fails and we exit. When this process
dies — graceful exit, crash, or kill — the OS releases the port immediately,
so the next legitimate launch succeeds with no stale-lockfile cleanup needed.

Each caller picks an arbitrary unused port and keeps the returned socket
alive for the lifetime of the process (store at module level).
"""
import socket
import sys


def acquire(name: str, port: int) -> socket.socket:
    """Bind localhost:port as a singleton lock. Exit with code 1 on conflict."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sys.stderr.write(
            f"\n[singleton] another {name} instance is already running "
            f"(localhost:{port} is bound). Exiting.\n\n"
        )
        sys.exit(1)
    return sock
