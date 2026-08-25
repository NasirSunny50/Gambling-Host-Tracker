"""The portal launcher must start even when its usual port is taken.

Port 8000 gets held out from under the portal in two ways on the operators' machines: a
previous portal that would not die, and - the one that has no process to kill - a port the
OS has bind-reserved for Hyper-V / WSL / Docker. Both surface as a bind failure, so the
launcher probes the port by binding it (the same thing uvicorn does) and moves to the next
free one rather than dying. These tests hold a real socket to prove it does.
"""

from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

_SERVE = Path(__file__).resolve().parents[1] / "scripts" / "internal" / "serve.py"
_spec = importlib.util.spec_from_file_location("_ght_serve", _SERVE)
serve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serve)


def _bind_a_port() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s, s.getsockname()[1]


def test_a_free_port_is_used_as_is():
    held, port = _bind_a_port()
    held.close()  # now free again
    assert serve._port_is_free("127.0.0.1", port) is True
    assert serve._resolve_port("127.0.0.1", port) == port


def test_a_busy_port_is_stepped_over():
    held, port = _bind_a_port()
    try:
        assert serve._port_is_free("127.0.0.1", port) is False
        resolved = serve._resolve_port("127.0.0.1", port)
        assert resolved > port  # moved on rather than failing
        assert serve._port_is_free("127.0.0.1", resolved) is True
    finally:
        held.close()
