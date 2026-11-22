#!/usr/bin/env python3
"""hxflash.py - a small CLI around the HX Stomp's USB update protocol.

Native flash is experimental and has run on a physical HX Stomp. By default
flash only validates the firmware and displays the plan; --device enables USB writes.

usage:
  hxflash.py status
  hxflash.py probe-updater <built.hxf> --device  # RAM bootstrap + identity only
  hxflash.py flash <built.hxf>             # offline validation/plan
  hxflash.py flash <built.hxf> --demo-progress  # offline progress demo
  hxflash.py flash <built.hxf> --device    # experimental permanent install
  hxflash.py verify-capture <built.hxf> <capture.pcapng>
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))

# (name, vendor:product, meaning)
KNOWN_IDS = (
    ("HX Stomp (normal firmware)", "0e41:4246"),
    ("HX Stomp XL (normal firmware)", "0e41:4253"),
    ("Boot ROM DFU (update mode)", "1fc9:000c"),
    ("HXtra RAM loader", "1209:0001"),
)


def lsusb_lines():
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("hxflash: lsusb not found (install usbutils)")
    return out.splitlines()


def cmd_status(_args):
    lines = lsusb_lines()
    found = False
    for name, vidpid in KNOWN_IDS:
        hit = next((l for l in lines if vidpid in l), None)
        if hit:
            found = True
            print(f"[+] {name} ({vidpid}): {hit.strip()}")
    if not found:
        print("[ ] no known HX Stomp USB state detected (unplugged, powered off, or an unrecognized state)")
    return 0


def wait_for_id(vidpid, timeout=30, want=True):
    end = time.time() + timeout
    while time.time() < end:
        if any(vidpid in l for l in lsusb_lines()) == want:
            return True
        time.sleep(0.5)
    return False


def cmd_verify_capture(args):
    import json
    from hx_update_capture import verify
    from hx_update_session import Firmware
    result = verify(args.capture, Firmware.load(args.firmware), args.address)
    report = json.dumps(result, indent=2)
    if args.report:
        from pathlib import Path
        Path(args.report).write_text(report + "\n")
    print(report)
    return 0


def ensure_usb_python(args):
    if args.device:
        try:
            import usb.core
        except ImportError as exc:
            raise RuntimeError("install pyusb in the active Python environment") from exc


def bootstrap_updater(fw, already_running=False):
    from pathlib import Path
    import shutil
    import tempfile
    from hx_update_proto import ProtocolError
    import usb.core
    if not already_running:
        stomps = list(usb.core.find(find_all=True, idVendor=0x0e41, idProduct=0x4246))
        xl = usb.core.find(idVendor=0x0e41, idProduct=0x4253)
        if len(stomps) != 1 or xl is not None or usb.core.find(idVendor=0x1fc9, idProduct=0x000c) is not None:
            raise ProtocolError('bootstrap requires exactly one running HX Stomp and no pre-existing ROM DFU device; '
                                'disconnect any HX Stomp XL. For an already loaded P36Updater use --updater-running')
        if not shutil.which('dfu-util'):
            raise ProtocolError('dfu-util not installed')
        if not Path(TOOLS, 'hxf_ram.py').is_file():
            raise ProtocolError('HXtra hxf_ram.py not found')
        subprocess.run([sys.executable, str(Path(TOOLS, 'hxf_ram.py')), 'dfu'], check=True)
        with tempfile.TemporaryDirectory(prefix='hxflash-') as tmp:
            image = Path(tmp, 'P36Updater.dfu')
            image.write_bytes(fw.updater)
            r = subprocess.run(['dfu-util', '-d', '1fc9:000c', '-a', '0', '-D', str(image)],
                               capture_output=True, text=True, timeout=120)
            print(r.stdout, end=''); print(r.stderr, end='', file=sys.stderr)
            if 'Download done.' not in r.stdout + r.stderr or r.returncode not in (0, 74):
                raise ProtocolError(f'P36Updater DFU transfer failed (exit {r.returncode})')
        if not wait_for_id('0e41:4246', timeout=60):
            raise ProtocolError('P36Updater did not appear on USB')


def drain_usb(dev):
    end = time.monotonic() + 1
    while time.monotonic() < end:
        dev.recv(timeout_ms=100)


def close_usb(dev):
    try:
        dev.close()
    except dev.core.USBError:
        pass  # The device may already be resetting.


def cmd_probe_updater(args):
    ensure_usb_python(args)
    from hx_update_session import Firmware, identify
    fw = Firmware.load(args.firmware)
    print(f"HX Stomp .hxf validated: SHA-256 {fw.sha256}", flush=True)
    if not args.device:
        print("Offline probe plan: load original P36Updater into RAM, identify service 5, close stream.")
        print("No update service 3 session or firmware-write commands. Add --device to run the probe.")
        return 0
    print("RAM probe: the pedal will leave normal operation. No firmware-write commands will be sent.", flush=True)
    bootstrap_updater(fw, args.updater_running)
    from hx_update_transport import Device, Stream
    dev = Device()
    try:
        drain_usb(dev)
        identify(Stream(dev))
        drain_usb(dev)
    finally:
        close_usb(dev)
    print("PASS: P36Updater identity confirmed (P33Updater); service-5 stream closed.")
    print("No firmware installation started. Power-cycle the pedal and verify normal boot/audio.")
    return 0


def elapsed_time(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


class FlashProgress:
    """Count only the bytes acknowledged by the device across the write plan."""

    def __init__(self, entry_sizes, *, demo=False):
        self.entries = {}
        self.total = 0
        for number, (entry, size) in enumerate(entry_sizes, 1):
            self.entries[entry] = (number, self.total)
            self.total += size
        self.started = time.monotonic()
        self.last_update = self.started
        self.last_entry = None
        self.terminal = sys.stdout.isatty()
        self.line_width = 0
        self.output_closed = False
        self.demo = demo
        self.interval = 1 if self.terminal or demo else 15

    def __call__(self, entry, done, entry_size):
        now = time.monotonic()
        entry_finished = done == entry_size
        if not entry_finished and entry == self.last_entry and now - self.last_update < self.interval:
            return
        self.last_update, self.last_entry = now, entry
        number, offset = self.entries[entry]
        written = offset + done
        elapsed = now - self.started
        eta = elapsed_time(elapsed * (self.total - written) / written) if elapsed >= 5 and written else "--:--:--"
        percent = 100.0 if written == self.total else min(99.9, 100 * written / self.total)
        details = (f" {percent:5.1f}% | {written / 1e6:.2f}/{self.total / 1e6:.2f} MB | "
                   f"{number}/{len(self.entries)} (entry {entry}) | {elapsed_time(elapsed)} | ETA {eta}")
        columns = shutil.get_terminal_size().columns if self.terminal else 120
        width = max(5, min(20, columns - len(details) - 3))
        filled = written * width // self.total
        bar = "#" * filled + "-" * (width - filled)
        line = f"[{bar}]{details}"
        self.write(line, replace=self.terminal and len(line) < columns and not entry_finished)
        if written == self.total:
            if self.demo:
                self.write("Demo complete. No USB connection was opened.")
            else:
                self.write("Transfer complete. Waiting for the final device confirmation.")

    def write(self, line, *, replace=False):
        if self.output_closed:
            return
        try:
            if self.terminal:
                print("\r" + line.ljust(self.line_width), end="" if replace else "\n", flush=True)
                self.line_width = len(line) if replace else 0
            else:
                print(line, flush=True)
        except BrokenPipeError:
            # A closed viewer must not abort an active firmware transfer.
            self.output_closed = True

    def close(self):
        if self.line_width and not self.output_closed:
            try:
                print(flush=True)
            except BrokenPipeError:
                self.output_closed = True
            self.line_width = 0


def demo_flash_progress(entry_sizes):
    progress = FlashProgress(entry_sizes, demo=True)
    progress.write("Offline demo: simulating acknowledged bytes. No device is used.")
    position = 0
    offset = 0
    steps = 80
    try:
        for tick in range(steps + 1):
            target = progress.total * tick // steps
            entry, size = entry_sizes[position]
            while target >= offset + size:
                progress(entry, size, size)
                offset += size
                position += 1
                if position == len(entry_sizes):
                    return
                entry, size = entry_sizes[position]
            progress(entry, target - offset, size)
            time.sleep(0.1)
    finally:
        progress.close()


def cmd_flash(args):
    ensure_usb_python(args)
    from hx_update_session import Firmware, ORDER, identify, install
    if args.restart_interrupted is not None and (
        not (args.device and args.updater_running) or args.restart_interrupted not in ORDER
    ):
        raise ValueError('--restart-interrupted ENTRY requires --device and --updater-running, with a writable entry number')
    fw = Firmware.load(args.firmware)
    entry_sizes = [(i, len(fw.entries[i].body)) for i in ORDER]
    print(f"HX Stomp .hxf validated: SHA-256 {fw.sha256}", flush=True)
    print(f"15 archive entries validated; {len(ORDER)} entries scheduled, "
          f"{sum(size for _, size in entry_sizes):,} bytes; order {ORDER}")
    print("Bootloader entry 1 excluded: no staging write or SPIFI programming requested.")
    print(f"P36Updater DFU image: {len(fw.updater):,} bytes")
    if args.extract_updater:
        # Never overwrite an existing artifact accidentally.
        with open(args.extract_updater, 'xb') as out:
            out.write(fw.updater)
        print(f"Updater extracted to {args.extract_updater}")
    if args.demo_progress:
        demo_flash_progress(entry_sizes)
        return 0
    if not args.device:
        print("Offline plan only. Experimental permanent install requires --device.")
        return 0
    print("Experimental native flash: writes eMMC; keep power connected.", flush=True)
    bootstrap_updater(fw, args.updater_running)
    from hx_update_transport import Device, Stream
    dev = Device()
    try:
        drain_usb(dev)
        identify(Stream(dev), interrupted_session=args.restart_interrupted is not None)
        drain_usb(dev)
        progress = FlashProgress(entry_sizes)
        progress.write("Updater identified. Preparing the write session.")
        try:
            install(Stream(dev), fw, progress, interrupted_entry=args.restart_interrupted)
        finally:
            progress.close()
        progress.write("All 14 application/data entries and final device result accepted. Check normal boot/audio.")
    finally:
        close_usb(dev)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show which USB state the HX Stomp is currently in").set_defaults(func=cmd_status)

    probe = sub.add_parser("probe-updater", help="load/identify P36Updater in RAM without starting firmware installation")
    probe.add_argument("firmware", help="HX Stomp .hxf containing the original updater")
    probe.add_argument("--device", action="store_true", help="perform RAM bootstrap and service-5 identity probe")
    probe.add_argument("--updater-running", action="store_true", help="probe an already loaded updater; skip bootstrap")
    probe.set_defaults(func=cmd_probe_updater)

    fl = sub.add_parser("flash", help="validate/plan a native install; --device writes eMMC (experimental)")
    fl.add_argument("firmware", help="path to the built HX Stomp .hxf")
    flash_mode = fl.add_mutually_exclusive_group()
    flash_mode.add_argument("--device", action="store_true", help="perform the experimental permanent USB install")
    flash_mode.add_argument("--demo-progress", action="store_true", help="animate the write progress offline without using USB")
    fl.add_argument("--updater-running", action="store_true", help="skip DFU bootstrap; require P36Updater identity")
    fl.add_argument("--restart-interrupted", type=int, metavar="ENTRY",
                    help="start over on the same updater; ENTRY is the partition interrupted mid-write")
    fl.add_argument("--extract-updater", metavar="FILE", help="extract embedded P36Updater DFU image (exclusive creation)")
    fl.set_defaults(func=cmd_flash)

    vc = sub.add_parser("verify-capture", help="verify the complete recorded install without USB access")
    vc.add_argument("firmware")
    vc.add_argument("capture")
    vc.add_argument("--address", type=int, default=10, help="captured USB device address (default: 10)")
    vc.add_argument("--report", help="save JSON verification report")
    vc.set_defaults(func=cmd_verify_capture)

    args = p.parse_args()
    try:
        sys.exit(args.func(args) or 0)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"hxflash: {exc}")


if __name__ == "__main__":
    main()
