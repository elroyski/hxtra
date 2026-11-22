"""Read-only classic pcap/Linux usbmon verifier, without tshark or temporary TSVs."""
import hashlib
import json
from pathlib import Path
import struct

import hx_update_proto as p
from hx_update_session import replay_full_install, install, CAPTURE_ORDER, ORDER


def packets(path, address=10):
    with open(path, 'rb') as f:
        head = f.read(24)
        if len(head) != 24 or head[:4] not in (b'\xd4\xc3\xb2\xa1', b'\x4d\x3c\xb2\xa1'):
            raise p.ProtocolError('expected little-endian classic pcap (usbmon), not pcapng')
        if struct.unpack_from('<I', head, 20)[0] != 220:
            raise p.ProtocolError('expected Linux usbmon mmapped link type 220')
        frame = 0
        while h := f.read(16):
            if len(h) != 16:
                raise p.ProtocolError('truncated pcap record')
            _, _, n, orig = struct.unpack('<4I', h)
            if n > 16 * 1024 * 1024:
                raise p.ProtocolError('oversized pcap packet')
            b = f.read(n)
            frame += 1
            if len(b) != n:
                raise p.ProtocolError('truncated pcap packet')
            if len(b) < 64 or b[11] != address:
                continue
            if n != orig:
                raise p.ProtocolError(f'frame {frame}: snaplen truncated USB packet')
            if b[9] != 3 or b[10] not in (1, 0x81):
                continue
            # OUT submission and IN completion; never count both URB halves.
            if (b[10], b[8]) not in ((1, ord('S')), (0x81, ord('C'))):
                continue
            cap = struct.unpack_from('<I', b, 36)[0]
            if cap and b[8] == ord('C') and struct.unpack_from('<i', b, 28)[0]:
                raise p.ProtocolError(f'frame {frame}: failed USB completion')
            if cap:
                if 64 + cap > len(b):
                    raise p.ProtocolError('truncated usbmon data')
                yield frame, b[10], b[64:64 + cap]


class Replay:
    """Compare generated application bytes to capture, independent of USB segmentation."""
    def __init__(self, out, into):
        self.out, self.into = memoryview(out), memoryview(into)
        self.tx = self.rx = 0

    def connect(self):
        pass

    def write(self, data, what):
        end = self.tx + len(data)
        if self.out[self.tx:end] != data:
            raise p.ProtocolError(f'capture mismatch at OUT byte {self.tx}: {what}')
        self.tx = end

    def read(self, n, what):
        end = self.rx + n
        if end > len(self.into):
            raise p.ProtocolError(f'capture exhausted at IN byte {self.rx}: {what}')
        b = bytes(self.into[self.rx:end])
        self.rx = end
        return b


class CaptureDevice:
    """Expose IN only after the OUT byte threshold observed in the capture.

    This runs the real Stream without hardware. USB message boundaries may
    differ, but application bytes and causal response order must agree.
    """
    mtu = 512

    def __init__(self, expected, events):
        self.expected = memoryview(expected)
        self.events = iter(events)
        self.pending = next(self.events, None)
        self.sent = 0
        self.tx_seq = 0
        self.window = 4096

    def send(self, raw):
        for m in p.parse_messages(raw):
            if m.seq != (self.tx_seq + 1) & 255:
                raise p.ProtocolError('generated host sequence gap')
            self.tx_seq = m.seq
            if m.window < self.window:
                raise p.ProtocolError('generated host receive window decreased')
            self.window = m.window
            if m.flags & p.F_DATA:
                end = self.sent + len(m.data)
                if self.expected[self.sent:end] != m.data:
                    raise p.ProtocolError(f'live Stream output differs at byte {self.sent}')
                self.sent = end

    def recv(self, timeout_ms=100):
        if self.pending is None or self.pending[0] > self.sent:
            raise p.ProtocolError(f'live Stream waited before captured request was complete at {self.sent}')
        _, raw = self.pending
        self.pending = next(self.events, None)
        return raw


def replay_without_bootloader(out, events, firmware):
    """Delete entry-1 exchange from the validated trace and rebase stream counters.

    This is a derived controlled replay of the 14-entry session. It is not a
    recording of a 14-entry physical install.
    """
    def span(entry):
        return 32 + 4 + len(entry.body) + 20 * ((len(entry.body) + 16383) // 16384) + 4
    prefix = len(out) - 12 - sum(span(firmware.entries[i]) for i in CAPTURE_ORDER)
    start = prefix + sum(span(firmware.entries[i]) for i in CAPTURE_ORDER[:CAPTURE_ORDER.index(1)])
    end = start + span(firmware.entries[1])
    if out[start:start + 32] != firmware.entries[1].select():
        raise p.ProtocolError('could not locate validated bootloader exchange')
    filtered_out = out[:start] + out[end:]
    removed, into, filtered, seq = end - start, bytearray(), [], None
    for threshold, raw in events:
        if start < threshold <= end:
            continue
        for m in p.parse_messages(raw):
            seq = m.seq if seq is None else (seq + 1) & 255
            after = threshold > end
            window = m.window - removed if after else m.window
            raw = p.frame(seq, window, m.flags, m.data, src=m.src, dst=m.dst, mtu=m.mtu or 512)
            filtered.append((threshold - removed if after else threshold, raw))
            if m.flags & p.F_DATA:
                into.extend(m.data)
    replay = Replay(filtered_out, into)
    install(replay, firmware)
    if replay.tx != len(filtered_out) or replay.rx != len(into):
        raise p.ProtocolError('14-entry application replay has trailing bytes')
    from hx_update_transport import Stream
    device = CaptureDevice(filtered_out, filtered)
    st = Stream(device)
    install(st, firmware)
    while device.pending is not None:
        st.poll()
    if device.sent != len(filtered_out) or st.rx:
        raise p.ProtocolError('14-entry transport replay incomplete')
    return {'result': 'PASS', 'entries': len(ORDER), 'excluded_entries': [1],
            'out_bytes': len(filtered_out), 'in_bytes': len(into),
            'scope': 'derived capture with entry-1 exchange removed, sequence/window counters rebased; no hardware test'}


def verify(path, firmware, address=10):
    # Session is identified by service-3 selection, not hard-coded frame numbers.
    out, into = bytearray(), bytearray()
    seq, window, sent = {}, {}, {1: 0, 0x81: 0}
    active = False
    events = []
    tx_threshold = 0
    frames = 0
    sessions = 0
    for fn, ep, raw in packets(path, address):
        for m in p.parse_messages(raw):
            expected = (p.DEST_HOST, p.SRC_DEVICE) if ep == 1 else (p.SRC_DEVICE, p.DEST_HOST)
            if (m.src, m.dst) != expected:
                raise p.ProtocolError(f'frame {fn}: unexpected router addresses')
            if m.flags & p.F_SYN:
                if ep == 1:
                    seq.clear(); window.clear(); sent = {1: 0, 0x81: 0}
                    events = []
                    tx_threshold = 0
                seq[ep], window[ep] = m.seq, m.window
                if ep == 0x81:
                    events.append((tx_threshold, p.frame(m.seq, m.window, m.flags, m.data,
                                                        src=m.src, dst=m.dst, mtu=m.mtu or 512)))
                continue
            if ep not in seq or m.seq != (seq[ep] + 1) & 255:
                raise p.ProtocolError(f'frame {fn}: sequence gap')
            seq[ep] = m.seq
            if ep == 0x81:
                events.append((tx_threshold, p.frame(m.seq, m.window, m.flags, m.data,
                                                        src=m.src, dst=m.dst, mtu=m.mtu or 512)))
            if m.flags & (p.F_DATA | p.F_WIN):
                if m.window < window[ep]:
                    raise p.ProtocolError(f'frame {fn}: receive window decreased')
                window[ep] = m.window
            if m.flags & p.F_DATA:
                if sent[ep] + len(m.data) > window.get(0x81 if ep == 1 else 1, 0):
                    raise p.ProtocolError(f'frame {fn}: sender exceeded peer window')
                sent[ep] += len(m.data)
                if ep == 1:
                    tx_threshold += len(m.data)
                if ep == 1 and m.data == struct.pack('<HHIB', 0, 3, 1, 3):
                    sessions += 1
                    if sessions > 1:
                        raise p.ProtocolError('multiple update sessions; select a narrower capture')
                    active = True
                if active:
                    (out if ep == 1 else into).extend(m.data)
            frames += 1
    if not active:
        raise p.ProtocolError('no service-3 update session in selected USB address')
    replay = Replay(out, into)
    replay_full_install(replay, firmware)
    if replay.tx != len(out) or replay.rx != len(into):
        raise p.ProtocolError('unexpected trailing application bytes')
    from hx_update_transport import Stream
    device = CaptureDevice(out, events)
    stream = Stream(device)
    replay_full_install(stream, firmware)
    while device.pending is not None:
        stream.poll()
    if stream.rx:
        raise p.ProtocolError('unexpected final application data')
    if device.sent != len(out) or device.pending is not None:
        raise p.ProtocolError('live Stream did not consume the complete captured exchange')
    safe_replay = replay_without_bootloader(out, events, firmware)
    with Path(path).open('rb') as source:
        capture_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    return dict(firmware_sha256=firmware.sha256, capture_sha256=capture_hash,
                messages=frames, entries=15, blocks=sum((len(e.body) + 16383) // 16384 for e in firmware.entries),
                out_bytes=len(out), in_bytes=len(into), result='PASS',
                transport_replay='PASS', bootloader_excluded_replay=safe_replay,
                scope='complete application byte comparison, sequence/window checks and real Stream with causally ordered captured IN; no live hardware test')
