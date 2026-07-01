"""Best-effort in-place zeroization for bytearray key material.

CPython GC gives no hard guarantees, but overwriting the backing buffer via
`ctypes.memset` at least removes the bytes from the live process heap before
they're freed.
"""

from __future__ import annotations

import ctypes


def secure_zero(buf: bytearray) -> None:
    """Overwrite `buf` with zeros in place.

    Falls back to a pure-Python loop if ctypes cannot address the backing buffer
    (e.g. exotic buffer types on PyPy).
    """
    if not isinstance(buf, bytearray):
        raise TypeError("secure_zero expects a bytearray")

    n = len(buf)
    if n == 0:
        return

    try:
        addr = (ctypes.c_char * n).from_buffer(buf)
        ctypes.memset(ctypes.addressof(addr), 0, n)
    except (TypeError, ValueError):
        for i in range(n):
            buf[i] = 0
