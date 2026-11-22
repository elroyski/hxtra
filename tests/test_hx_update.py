"""Protocol invariants and failure paths; never imports pyusb or opens a device."""
import struct
import unittest
from unittest.mock import patch

import hx_update_proto as p
from hx_update_transport import Stream
from hx_update_session import Entry, Firmware, ORDER, CAPTURE_ORDER, checked_result, identify, install, replay_full_install, check_updater_code
from hx_update_capture import Replay


class FakeDevice:
    mtu = 512
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.writes = []
    def send(self, raw):
        self.writes.append(raw)
    def recv(self, timeout_ms=100):
        return self.replies.pop(0) if self.replies else None


def incoming(seq, window=4096, flags=p.F_WIN, data=b''):
    return p.frame(seq, window, flags, data, src=p.SRC_DEVICE, dst=p.DEST_HOST)


class ProtocolTests(unittest.TestCase):
    def test_fletcher_wrap_and_word_order(self):
        self.assertEqual(p.fletcher(struct.pack('<3I', 0xffffffff, 2, 3)), (4, 4))
        with self.assertRaises(ValueError):
            p.fletcher(b'x')

    def test_checkpoint_describes_following_short_block(self):
        data = struct.pack('<3I', 1, 2, 3)
        self.assertEqual(p.checkpoint_header(0, data), b'\xaa\0\0\7' + struct.pack('<4I', 0, 6, 10, 3))
        self.assertEqual(list(p.update_blocks(data)), [p.checkpoint_header(0, data) + data])

    def test_chunks_include_final_20_byte_fragment(self):
        block = bytes(16384 + 20)
        chunks = list(p.block_chunks(block))
        self.assertEqual([len(c) for c in chunks[:4]], [496, 16, 496, 16])
        self.assertEqual(len(chunks[-1]), 20)
        self.assertEqual(b''.join(chunks), block)

    def test_router_coalescing_padding_and_full_window(self):
        a = p.frame(255, 0x12345678, p.F_DATA, b'abc')
        b = p.frame(0, 0x12345678, p.F_HB)
        messages = p.parse_messages(a + b)
        self.assertEqual([m.seq for m in messages], [255, 0])
        self.assertEqual(messages[0].data, b'abc')
        self.assertEqual(messages[0].window, 0x12345678)
        for broken in (a[:7], a[:16], b'\0' * 16):
            with self.assertRaises(p.ProtocolError):
                p.parse_messages(broken)

    def test_read_only_identity_is_required(self):
        class Probe:
            def connect(self): pass
            def write(self, *args): pass
            def close_stream(self): pass
            def read(self, *args): return bytes(32)
        with self.assertRaises(p.ProtocolError):
            identify(Probe())


class TransportTests(unittest.TestCase):
    def connected(self, dev, **kw):
        st = Stream(dev, **kw)
        st.connected, st.rx_seq, st.peer_window = True, 254, 512
        return st

    def test_no_one_ack_per_packet_assumption(self):
        dev = FakeDevice([incoming(255, 1024), incoming(0, 1536)])
        st = self.connected(dev)
        st.write(bytes(1024), 'two sectors')
        self.assertEqual(st.sent, 1024)
        self.assertEqual([p.parse_messages(b)[0].seq for b in dev.writes], [1, 2, 3, 4])
        self.assertEqual(len(dev.replies), 1)  # one window update covered two USB messages

    def test_partial_window_is_used_before_waiting(self):
        class NarrowDevice(FakeDevice):
            def recv(self, timeout_ms=100):
                sent = sum(len(p.parse_messages(w)[0].data) for w in self.writes)
                if sent != 20:
                    raise AssertionError('sender waited before using the available 20 bytes')
                return incoming(255, 1000)
        dev = NarrowDevice()
        st = self.connected(dev)
        st.peer_window = 20
        st.write(bytes(100), 'partial window')
        self.assertEqual([len(p.parse_messages(w)[0].data) for w in dev.writes], [20, 80])

    def test_independent_seq_wrap_and_consumed_window(self):
        dev = FakeDevice([incoming(255, 512, p.F_DATA, bytes(8)), incoming(0, 1024)])
        st = self.connected(dev)
        st.tx_seq = 255
        self.assertEqual(st.read(8, 'block result'), bytes(8))
        st.poll()
        self.assertEqual(st.rx_seq, 0)
        ack = p.parse_messages(dev.writes[0])[0]
        self.assertEqual((ack.seq, ack.window, ack.flags), (255, 4104, p.F_WIN))
        self.assertEqual(st.tx_seq, 0)

    def test_sequence_gap_and_close_abort(self):
        for raw in (incoming(0), incoming(255, flags=p.F_FIN)):
            st = self.connected(FakeDevice([raw]))
            with self.assertRaises(p.ProtocolError): st.poll()

    def test_window_decrease_aborts(self):
        st = self.connected(FakeDevice([incoming(255, 511)]))
        with self.assertRaises(p.ProtocolError): st.poll()

    def test_timeout_does_not_write_data_or_retry(self):
        ticks = iter(range(100))
        dev = FakeDevice()
        st = self.connected(dev, timeout=2, clock=lambda: next(ticks))
        st.peer_window = 0
        with self.assertRaisesRegex(p.ProtocolError, 'no automatic retry'):
            st.write(bytes(4), 'blocked')
        self.assertTrue(all(p.parse_messages(raw)[0].flags == p.F_HB for raw in dev.writes))

    def test_usb_failure_propagates(self):
        dev = FakeDevice()
        dev.recv = lambda **kw: (_ for _ in ()).throw(OSError('disconnected'))
        with self.assertRaises(OSError): self.connected(dev).poll()


class ProbeTests(unittest.TestCase):
    def test_cli_probe_sends_only_service_five_and_closes(self):
        import hxflash
        from types import SimpleNamespace
        from contextlib import ExitStack
        identity = bytes.fromhex('000000001800000005') + b'P33Updater\0' + bytes(12)
        self.assertEqual(len(identity), 32)
        dev = FakeDevice([incoming(1, 512, p.F_SYN), incoming(2, 521, p.F_DATA, identity)])
        closed = []
        dev.close = lambda: closed.append(True)
        fw = SimpleNamespace(sha256='test', updater=b'test')
        args = SimpleNamespace(firmware='fixture.hxf', device=True, updater_running=False)
        with ExitStack() as stack:
            stack.enter_context(patch.object(hxflash, 'ensure_usb_python'))
            stack.enter_context(patch.object(hxflash, 'bootstrap_updater'))
            stack.enter_context(patch.object(hxflash, 'drain_usb'))
            stack.enter_context(patch('hx_update_session.Firmware.load', return_value=fw))
            stack.enter_context(patch('hx_update_transport.Device', return_value=dev))
            forbidden = stack.enter_context(patch('hx_update_session.install', side_effect=AssertionError('probe must never install')))
            self.assertEqual(hxflash.cmd_probe_updater(args), 0)
            forbidden.assert_not_called()
        messages = [p.parse_messages(raw)[0] for raw in dev.writes]
        self.assertEqual([m.flags for m in messages], [p.F_SYN | p.F_HBREQ, p.F_DATA, p.F_WIN, p.F_FIN])
        self.assertEqual(b''.join(m.data for m in messages), struct.pack('<HHIB', 1, 5, 1, 5))
        self.assertEqual(closed, [True])

    def test_offline_probe_never_bootstraps(self):
        import hxflash
        from types import SimpleNamespace
        args = SimpleNamespace(firmware='fixture.hxf', device=False, updater_running=False)
        fw = SimpleNamespace(sha256='test')
        with patch.object(hxflash, 'ensure_usb_python'), patch('hx_update_session.Firmware.load', return_value=fw), patch.object(hxflash, 'bootstrap_updater') as bootstrap:
            self.assertEqual(hxflash.cmd_probe_updater(args), 0)
            bootstrap.assert_not_called()


class SessionTests(unittest.TestCase):
    def fixture(self):
        entries = tuple(Entry(i, 128, p.fletcher(bytes([i]) * 512), bytes([i]) * 512) for i in range(15))
        fw = Firmware(entries, b'', 'test')
        rx = struct.pack('<HHI', 0, 3, 2) + bytes(2) + bytes((1, 4, 4, 4))
        rx += struct.pack('<I', 15) + bytes(32) + bytes(4)
        rx += (struct.pack('<I', 1) + bytes(24)) * 15
        params = struct.pack('<15I', 1, 4096, 4, 1, 1, 10000, 2, 1, 10000, 3, 1, 5000, 4, 2, 1)
        tx = struct.pack('<HHIB', 0, 3, 1, 3) + struct.pack('<HHI', 1, 0, 2) + bytes(2)
        tx += b'\xaa\0\0\1' + b'\xaa\0\0\3' + b'\xaa\0\0\2'
        tx += b''.join(b'\xaa\0\0\4' + struct.pack('<I', i) for i in range(15))
        for i in ORDER:
            e = fw.entries[i]
            tx += e.select() + b'\xaa\0\0\6' + next(p.update_blocks(e.body)) + b'\xaa\0\0\11'
            rx += params + bytes(4 + 8 + 4)
        tx += b'\xaa\0\0\12' + struct.pack('<2I', 1, 0)
        rx += bytes(12)
        return fw, tx, rx

    def test_full_install_and_final_status(self):
        fw, tx, rx = self.fixture()
        st = Replay(tx, rx)
        install(st, fw)
        self.assertEqual((st.tx, st.rx), (len(tx), len(rx)))

    def test_bootloader_excluded_from_live_order(self):
        self.assertEqual(len(ORDER), 14)
        self.assertNotIn(1, ORDER)
        self.assertEqual(ORDER, tuple(i for i in CAPTURE_ORDER if i != 1))
        fw, tx, rx = self.fixture()
        selections = []
        class ObservedReplay(Replay):
            def write(self, data, what):
                if what == 'select entry':
                    selections.append(struct.unpack_from('<I', data, 4)[0])
                super().write(data, what)
        install(ObservedReplay(tx, rx), fw)
        self.assertEqual(tuple(selections), ORDER)
        self.assertNotIn(1, selections)

    def test_full_replay_refuses_a_live_stream(self):
        fw, _, _ = self.fixture()
        dev = FakeDevice()
        with self.assertRaisesRegex(p.ProtocolError, 'offline capture'):
            replay_full_install(Stream(dev), fw)
        self.assertEqual(dev.writes, [])

    def test_final_device_error_is_failure(self):
        fw, tx, rx = self.fixture()
        rx = rx[:-12] + b'\xff' * 4 + bytes(8)
        with self.assertRaisesRegex(p.ProtocolError, 'finish update'):
            install(Replay(tx, rx), fw)

    def test_unknown_updater_refused_despite_matching_vectors(self):
        updater = bytearray(528)
        updater[:4] = bytes.fromhex('1a000100')
        struct.pack_into('<2I', updater, 16, 0x10081000, 0x10008081)
        with self.assertRaisesRegex(p.ProtocolError, 'unverified P36Updater'):
            check_updater_code(updater)

    def test_corrupt_container_rejected_before_usb(self):
        with patch('pathlib.Path.read_bytes', return_value=b'not firmware'):
            with self.assertRaises(p.ProtocolError): Firmware.load('bad.hxf')


if __name__ == '__main__':
    unittest.main()
