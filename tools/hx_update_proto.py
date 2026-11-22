#!/usr/bin/env python3
"""HX Stomp router/stream and update blocks. Pure bytes; no USB I/O.

Verified against the 2026-11-19 capture. Sequence numbers are per direction;
the second stream word is an absolute receive window, NOT a stream handle.
"""
from dataclasses import dataclass
import struct

DEST_HOST, SRC_DEVICE = 0x1001, 0x03EF
CHECKPOINT_EVERY = 16384
F_SYN, F_FIN, F_DATA, F_WIN, F_HB, F_HBREQ = 1, 2, 4, 8, 16, 32


class ProtocolError(RuntimeError):
    pass


def fletcher(data):
    if len(data) % 4:
        raise ValueError('Fletcher input must contain whole 32-bit words')
    a = b = 0
    for (word,) in struct.iter_unpack('<I', data):
        a = (a + word) & 0xffffffff
        b = (b + a) & 0xffffffff
    return a, b


def envelope(src, dst, payload, kind=1):
    """Router header; src precedes dst on the wire (legacy positional API)."""
    if len(payload) > 0xffff or kind not in (1, 2):
        raise ValueError('invalid router length/type')
    return struct.pack('<II', kind << 28 | 2 << 26 | len(payload), src | dst << 16) + payload


def stream_header(seq, window, flags=F_DATA):
    if not 0 <= seq <= 255 or not 0 <= window <= 0xffffffff:
        raise ValueError('invalid stream sequence/window')
    return struct.pack('<II', flags << 24 | seq << 8, window)


def frame(seq, window, flags, data=b'', src=DEST_HOST, dst=SRC_DEVICE, mtu=512):
    body = stream_header(seq, window, flags) + data
    if flags & F_SYN:
        body = struct.pack('<I', mtu << 16) + body
    raw = envelope(src, dst, body, 2 if flags & F_SYN else 1)
    return raw + bytes(-len(raw) % 4)


@dataclass(frozen=True)
class Message:
    src: int
    dst: int
    seq: int
    flags: int
    window: int
    data: bytes
    mtu: int | None = None


def parse_messages(raw):
    """Decode every aligned router message in one USB transfer, rejecting truncation."""
    result, pos = [], 0
    while pos < len(raw):
        if len(raw) - pos < 8:
            raise ProtocolError('truncated router header')
        w, addr = struct.unpack_from('<II', raw, pos)
        kind, n = w >> 28, w & 0xffff
        if kind not in (1, 2) or w & 0x0fff0000 != 0x08000000:
            raise ProtocolError(f'unknown router header {w:#x}')
        end = pos + 8 + n
        p = pos + 8
        if end > len(raw) or n < (12 if kind == 2 else 8):
            raise ProtocolError('truncated stream message')
        mtu = None
        if kind == 2:
            mtu = struct.unpack_from('<I', raw, p)[0] >> 16
            p += 4
        s, window = struct.unpack_from('<II', raw, p)
        flags = s >> 24
        if s & 0x00ff00ff or flags & ~63 or bool(flags & F_SYN) != (kind == 2):
            raise ProtocolError('invalid stream flags/reserved bits')
        result.append(Message(addr & 0xffff, addr >> 16, s >> 8 & 255,
                              flags, window, raw[p + 8:end], mtu))
        # Some captures omit the final alignment padding; internal messages must be aligned.
        pos = min((end + 3) & ~3, len(raw))
    return result


def checkpoint_header(block_counter, block):
    """Checksum describes the FOLLOWING block, including its actual word count."""
    if not block or len(block) > CHECKPOINT_EVERY or len(block) % 4:
        raise ValueError('invalid update block length')
    return b'\xaa\0\0\x07' + struct.pack('<4I', block_counter, *fletcher(block), len(block) // 4)


def update_blocks(body):
    """body includes the 512-byte partition subheader, then little-endian payload."""
    for i, pos in enumerate(range(0, len(body), CHECKPOINT_EVERY)):
        block = body[pos:pos + CHECKPOINT_EVERY]
        yield checkpoint_header(i, block) + block


def block_chunks(block, mtu=512):
    """Updater writes 512 stream bytes at a time; router overhead leaves mtu-16."""
    if mtu <= 16 or mtu % 4:
        raise ValueError('invalid MTU')
    for pos in range(0, len(block), 512):
        sector = block[pos:pos + 512]
        for k in range(0, len(sector), mtu - 16):
            yield sector[k:k + mtu - 16]
