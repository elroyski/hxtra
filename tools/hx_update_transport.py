#!/usr/bin/env python3
"""Flow-controlled HX Stomp stream. No USB action happens on import.

WIN advertises an absolute byte limit. It is not an ACK of the last USB packet.
Only application replies establish completion of update commands/blocks.
"""
import time
import hx_update_proto as p


class Device:
    def __init__(self):
        import usb.core
        import usb.util
        self.core, self.util = usb.core, usb.util
        devices = list(usb.core.find(find_all=True, idVendor=0x0e41, idProduct=0x4246))
        if len(devices) != 1:
            raise p.ProtocolError('expected exactly one HX Stomp 0e41:4246')
        self.dev = devices[0]
        try:
            cfg = self.dev.get_active_configuration()
        except usb.core.USBError:
            self.dev.set_configuration()
            cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        endpoints = [e for e in intf if usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
        if intf.bInterfaceClass != 255 or len(endpoints) != 2:
            raise p.ProtocolError('interface 0 is not the expected bulk message interface')
        by_address = {e.bEndpointAddress: e for e in endpoints}
        if set(by_address) != {1, 0x81}:
            raise p.ProtocolError('unexpected message endpoint addresses')
        self.out, self.into = by_address[1], by_address[0x81]
        if self.out.wMaxPacketSize != 512 or self.into.wMaxPacketSize != 512:
            raise p.ProtocolError('unsupported USB MTU (expected 512)')
        self.mtu = 512
        self.detached = False
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
                self.detached = True
            usb.util.claim_interface(self.dev, 0)
        except Exception:
            try:
                self.close()
            except usb.core.USBError:
                pass
            raise

    def send(self, raw, timeout_ms=2000):
        if self.dev.write(1, raw, timeout=timeout_ms) != len(raw):
            raise p.ProtocolError('short USB write; stream state is uncertain')

    def recv(self, timeout_ms=100):
        try:
            return bytes(self.dev.read(0x81, self.mtu, timeout=timeout_ms))
        except self.core.USBTimeoutError:
            return None

    def close(self):
        try:
            self.util.release_interface(self.dev, 0)
        finally:
            try:
                if self.detached:
                    self.dev.attach_kernel_driver(0)
            finally:
                self.util.dispose_resources(self.dev)


class Stream:
    def __init__(self, device, timeout=60, clock=time.monotonic):
        self.device, self.timeout, self.clock = device, timeout, clock
        self.tx_seq, self.rx_seq = 1, None
        self.sent = self.consumed = self.peer_window = 0
        self.rx = bytearray()
        self.last_tx = clock()
        self.connected = False
        self.closed = False

    def send(self, flags, data=b''):
        if self.closed:
            raise p.ProtocolError('stream is closed')
        self.device.send(p.frame(self.tx_seq, (4096 + self.consumed) & 0xffffffff,
                                 flags, data, mtu=self.device.mtu))
        self.tx_seq = (self.tx_seq + 1) & 255
        self.sent += len(data)
        self.last_tx = self.clock()

    def handle(self, m):
        if (m.src, m.dst) != (p.SRC_DEVICE, p.DEST_HOST):
            raise p.ProtocolError('unexpected router addresses')
        if m.flags & p.F_FIN:
            raise p.ProtocolError('device closed/refused the stream')
        if m.flags & p.F_SYN:
            if self.connected or m.mtu != self.device.mtu:
                raise p.ProtocolError('unexpected SYN/MTU')
            self.connected, self.rx_seq, self.peer_window = True, m.seq, m.window
            return
        if not self.connected or m.seq != (self.rx_seq + 1) & 255:
            raise p.ProtocolError('device sequence gap')
        self.rx_seq = m.seq
        if m.flags & (p.F_WIN | p.F_DATA):
            if m.window < self.peer_window or m.window < self.sent:
                raise p.ProtocolError('device receive window moved backwards')
            self.peer_window = m.window
        if m.flags & p.F_DATA:
            if len(self.rx) + len(m.data) > 4096:
                raise p.ProtocolError('device exceeded host receive window')
            self.rx.extend(m.data)
        elif m.data:
            raise p.ProtocolError('unexpected data in control message')

    def poll(self):
        raw = self.device.recv(timeout_ms=100)
        if raw:
            for m in p.parse_messages(raw):
                self.handle(m)
        if self.connected and self.clock() - self.last_tx >= 1:
            self.send(p.F_HB)

    def until(self, condition, what):
        deadline = self.clock() + self.timeout
        while not condition():
            if self.clock() >= deadline:
                raise p.ProtocolError(f'timeout waiting for {what}; no automatic retry')
            self.poll()

    def connect(self):
        if self.connected:
            raise p.ProtocolError('stream already connected')
        self.send(p.F_SYN | p.F_HBREQ)
        self.until(lambda: self.connected, 'SYN')

    def write(self, data, what):
        for chunk in p.block_chunks(data, self.device.mtu):
            pos = 0
            while pos < len(chunk):
                self.until(lambda: self.sent < self.peer_window, f'{what}: receive window')
                n = min(len(chunk) - pos, self.peer_window - self.sent)
                self.send(p.F_DATA, chunk[pos:pos + n])
                pos += n

    def read(self, n, what):
        self.until(lambda: len(self.rx) >= n, what)
        result = bytes(self.rx[:n])
        del self.rx[:n]
        self.consumed += n
        self.send(p.F_WIN)
        return result

    def close_stream(self):
        self.send(p.F_FIN)
        self.closed = True
