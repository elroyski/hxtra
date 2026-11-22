"""Validated HX Stomp .hxf and P36Updater application protocol.

No captured packets are replayed. Metadata and checksums come from the selected
firmware. The live installer has run on a physical HX Stomp and is experimental.
"""
from array import array
from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct
import sys
import zlib

import hx_update_proto as p

# Full historical Windows transfer order, used only by offline capture verification.
CAPTURE_ORDER = (4, 0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
# Never request entry 1: finishing that entry can program the SPIFI bootloader.
ORDER = tuple(i for i in CAPTURE_ORDER if i != 1)
PART_START = (0, 0x8001, 0x8802, 0x8c03, 0x940c, 0x11005, 0x11406, 0x19407,
              0x19c08, 0x11f753, 0x120956, 0x1209f7, 0x120a98, 0x1a14da, 0x222856)


VERIFIED_UPDATER_CODE_SHA256 = '7069c32b99c6c2236b1a1cdff29850f38a6c2510804acdf762391c2026235f1e'


def check_updater_code(updater):
    """Safety findings apply only to this exact original P36Updater code."""
    if hashlib.sha256(updater[16:]).hexdigest() != VERIFIED_UPDATER_CODE_SHA256:
        raise p.ProtocolError('unverified P36Updater code; refusing an updater outside the bootloader audit')


def swap32(raw):
    a = array('I')
    a.frombytes(raw)
    if a.itemsize != 4:
        raise p.ProtocolError('platform has non-32-bit unsigned int')
    a.byteswap()
    return a.tobytes()


@dataclass(frozen=True)
class Entry:
    index: int
    words: int
    sums: tuple
    body: bytes

    def select(self):
        return b'\xaa\0\0\x05' + struct.pack('<7I', self.index, 0, self.words, *self.sums, 0, 0)


@dataclass(frozen=True)
class Firmware:
    entries: tuple
    updater: bytes
    sha256: str

    @classmethod
    def load(cls, path):
        raw = Path(path).read_bytes()
        if len(raw) < 12 or raw[:4] != b'FORM' or raw[8:12] != b'L6FF':
            raise p.ProtocolError('expected an HX Stomp .hxf container')
        if struct.unpack_from('>I', raw, 4)[0] + 8 != len(raw):
            raise p.ProtocolError('FORM length mismatch')
        chunks, dinfs, pos = {}, [], 12
        while pos < len(raw):
            if pos + 8 > len(raw):
                raise p.ProtocolError('truncated IFF header')
            tag, n = struct.unpack_from('>4sI', raw, pos)
            end = pos + 8 + n
            if end > len(raw):
                raise p.ProtocolError('truncated IFF chunk')
            body = raw[pos + 8:end]
            if tag == b'dinf':
                dinfs.append(body)
            elif tag in chunks:
                raise p.ProtocolError('duplicate IFF chunk')
            else:
                chunks[tag] = body
            pos = end + (n & 1)
        head = chunks.get(b'HEAD', b'')
        if len(head) < 36 or struct.unpack_from('>H', head, 10)[0] != 6:
            raise p.ProtocolError('only HX Stomp product 0x0006 is supported (not XL)')
        dec = zlib.decompressobj()
        # A corrupt container must not expand beyond the supported image size.
        data = dec.decompress(chunks.get(b'data', b''), 128 * 1024 * 1024 + 1)
        if len(data) > 128 * 1024 * 1024 or not dec.eof or dec.unused_data:
            raise p.ProtocolError('invalid/oversized compressed image')
        if len(data) != struct.unpack_from('>I', head, 16)[0] or hashlib.md5(data).digest() != head[20:36]:
            raise p.ProtocolError('HXF size/MD5 mismatch')
        if data[:4] != b'L6IA' or len(data) < 24 or struct.unpack_from('>5I', data, 4) != (2, 1, 1, 64, 15):
            raise p.ProtocolError('unsupported L6IA header')
        entries, pos = [], 24
        for i in range(15):
            if pos + 36 > len(data):
                raise p.ProtocolError('truncated entry header')
            idx, marker, kind, unit, words, a, b, r0, r1 = struct.unpack_from('>9I', data, pos)
            end = pos + 36 + words * 4
            if (idx, marker, unit, r0, r1) != (i, 0x03800000, 4, 0, 0) or kind not in (1, 2, 6) or words < 128 or end > len(data):
                raise p.ProtocolError(f'invalid entry {i}')
            body = swap32(data[pos + 36:end])
            magic, version, crc, length = struct.unpack_from('<4I', body)
            if magic != 0x56112603 or length != len(body) - 512:
                raise p.ProtocolError(f'entry {i}: invalid partition subheader')
            if zlib.crc32(body[512:]) != crc or p.fletcher(body) != (a, b):
                raise p.ProtocolError(f'entry {i}: CRC/Fletcher mismatch')
            if i < 14 and words * 4 > (PART_START[i + 1] - PART_START[i]) * 512:
                raise p.ProtocolError(f'entry {i} exceeds partition')
            entries.append(Entry(i, words, (a, b), body))
            pos = end
        updater = data[pos:]
        # LPC43xx boot image header: unencrypted image, number of 512-byte blocks.
        if len(updater) < 24 or updater[:2] != b'\x1a\0' or any(updater[4:16]):
            raise p.ProtocolError('invalid P36Updater DFU header')
        blocks = struct.unpack_from('<H', updater, 2)[0]
        if len(updater) != 16 + blocks * 512:
            raise p.ProtocolError('P36Updater DFU length mismatch')
        sp, reset = struct.unpack_from('<2I', updater, 16)
        if sp != 0x10081000 or reset != 0x10008081:
            raise p.ProtocolError('unsupported P36Updater entry vectors')
        check_updater_code(updater)
        expected = [(0, 65536, 0, pos), (5, 65536, pos, len(updater))]
        if len(dinfs) != 2 or any(len(d) != 16 for d in dinfs) or [struct.unpack('>4I', d) for d in dinfs] != expected:
            raise p.ProtocolError('HXF region table does not match archive/updater')
        return cls(tuple(entries), updater, hashlib.sha256(raw).hexdigest())


def checked_result(st, n, what):
    raw = st.read(n, what)
    if any(raw):
        raise p.ProtocolError(f'{what}: device returned {raw.hex()}')


def identify(st, *, interrupted_session=False):
    """Read-only service 5 probe distinguishes P36Updater from normal firmware."""
    st.connect()
    st.write(struct.pack('<HHIB', 1, 5, 1, 5), 'identify P36Updater')
    raw = st.read(32, 'P36Updater identity')
    st.close_stream()
    expected = bytes.fromhex('000000001800000005')
    # The running updater reports the previous service in the first reply.
    # It can be install (3) or identify (5) during interrupted-session recovery.
    recovery_headers = (bytes.fromhex('000003001800000005'), bytes.fromhex('000005001800000005'))
    valid_header = raw[:9] == expected or (interrupted_session and raw[:9] in recovery_headers)
    if not valid_header or raw[9:20] != b'P33Updater\0':
        raise p.ProtocolError(f'not the captured P36Updater identity: {raw.hex()}')


def install(st, firmware, progress=None, *, interrupted_entry=None):
    """Live installation excludes entry 1, including its eMMC staging partition."""
    return _install(st, firmware, ORDER, progress, interrupted_entry=interrupted_entry)


def replay_full_install(st, firmware):
    """Historical full install, restricted to the offline replay transports.

    The live CLI exposes no bootloader-write option.
    """
    from hx_update_capture import Replay, CaptureDevice
    from hx_update_transport import Stream
    if not (type(st) is Replay or (type(st) is Stream and type(st.device) is CaptureDevice)):
        raise p.ProtocolError('full bootloader-inclusive replay requires an offline capture transport')
    return _install(st, firmware, CAPTURE_ORDER)


def _install(st, firmware, entry_order, progress=None, *, interrupted_entry=None):
    """Service 3 session. Every block requires its 8-byte application result."""
    st.connect()
    st.write(struct.pack('<HHIB', 0, 3, 1, 3), 'select service 3')
    st.write(struct.pack('<HHI', 1, 0, 2) + bytes(2), 'version')
    hello = st.read(10, 'hello')
    if hello[:8] != struct.pack('<HHI', 0, 3, 2):
        raise p.ProtocolError('unsupported update service version')
    st.write(b'\xaa\0\0\x01', 'protocol info')
    if st.read(4, 'protocol info') != bytes((1, 4, 4, 4)):
        raise p.ProtocolError('unsupported update protocol')
    st.write(b'\xaa\0\0\x03', 'device info')
    info = st.read(36, 'device info')
    if struct.unpack_from('<I', info)[0] != 15:
        raise p.ProtocolError('device does not expose 15 entries')
    st.write(b'\xaa\0\0\x02', 'begin update')
    checked_result(st, 4, 'begin update')
    for i in range(15):
        st.write(b'\xaa\0\0\x04' + struct.pack('<I', i), 'query entry')
        reply = st.read(28, 'query entry')
        status = struct.unpack_from('<I', reply)[0]
        # The updater returns 4 when the entry header is invalid. Accept it
        # only for the partition known to have been interrupted mid-write.
        if status not in (1, 5) and not (i == interrupted_entry and status == 4):
            raise p.ProtocolError(f'entry {i}: unsupported query result {status}')
    for i in entry_order:
        e = firmware.entries[i]
        st.write(e.select(), 'select entry')
        reply = st.read(60, 'select entry')
        if reply != struct.pack('<15I', 1, 4096, 4, 1, 1, 10000, 2, 1, 10000, 3, 1, 5000, 4, 2, 1):
            raise p.ProtocolError(f'entry {i}: unknown transfer parameters {reply.hex()}')
        st.write(b'\xaa\0\0\x06', 'open entry')
        checked_result(st, 4, 'open entry')
        done = 0
        for block in p.update_blocks(e.body):
            st.write(block, 'write block')
            checked_result(st, 8, f'entry {i} block {done // 16384}')
            done += len(block) - 20
            if progress:
                progress(i, done, len(e.body))
        st.write(b'\xaa\0\0\x09', 'finish entry')
        checked_result(st, 4, 'finish entry')
    st.write(b'\xaa\0\0\x0a' + struct.pack('<2I', 1, 0), 'finish update')
    checked_result(st, 12, 'finish update')
