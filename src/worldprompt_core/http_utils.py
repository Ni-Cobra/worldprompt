"""Bounded request cleanup for structured loopback HTTP error responses."""
from __future__ import annotations

import time


def discard_rejected_body(handler, max_body):
    # Closing a socket with an unread request body can reset the TCP
    # connection on Windows before the client receives our JSON error.
    # Drain only bounded Content-Length bodies, without parsing them,
    # and never let an unauthenticated slow sender occupy this thread.
    if getattr(handler, "body_read", False) or handler.headers.get("Transfer-Encoding"):
        return
    try:
        remaining = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return
    if not 0 < remaining <= max_body:
        return
    previous_timeout = handler.connection.gettimeout()
    deadline = time.monotonic() + 1
    try:
        while remaining and time.monotonic() < deadline:
            handler.connection.settimeout(max(.001, deadline - time.monotonic()))
            # read1 performs at most one socket read, so a trickling sender
            # cannot keep a buffered read(length) alive beyond the deadline.
            chunk = handler.rfile.read1(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
    except OSError:
        pass
    finally:
        handler.connection.settimeout(previous_timeout)
        handler.body_read = True
