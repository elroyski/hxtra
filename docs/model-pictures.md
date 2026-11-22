# Build and install HXtra Model Pictures

**Experimental, HX Stomp only. Use at your own risk.** I tested Model Pictures on a physical HX Stomp using RAM boot and a full firmware installation. HX Stomp XL, HX Effects and other Helix devices are outside the tested hardware.

## Build on Linux

Start in the root of this fork. You need the original **HX Stomp 3.80.0** file `HX_Stomp_v3.80.0_0x103966115.hxf` from your Line 6 Updater cache. Setup for Debian or Ubuntu:

```sh
sudo apt install clang ffmpeg dfu-util libusb-1.0-0 usbutils python3-venv
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-model-pictures.txt
python tools/build_model_pictures.py /path/to/HX_Stomp_v3.80.0_0x103966115.hxf
```

The output is `build/model-pictures/HX_Stomp_3.80.0_Model_Pictures.hxf`. Keep the original and built HXF files locally. The repository contains the patcher, and the builder uses your original firmware as its input.

The first build downloads the catalog illustrations from Line 6 and the TONE3000 logo from their site. It keeps the downloads under `build/model-pictures/art/` and checks the catalog's recorded SHA-256 hashes. `--art-cache DIR` reuses a local cache. If your FFmpeg lacks the SVG decoder, download the TONE3000 logo as a PNG and pass `--tone3000-logo PATH`. The displayed gallery uses a custom boot splash. You can pass your own 320 × 240 PNG with `--splash PATH`.

The build enables HXtra's real names and the usual two NAM A2 Lite slots for this profile. On the pedal, **Global Settings → Displays → page 2 → Model Pictures** switches the illustrated views on or off. The default is **On**.

## Preview the install in the terminal

```sh
python tools/hxflash.py flash build/model-pictures/HX_Stomp_3.80.0_Model_Pictures.hxf
python tools/hxflash.py flash build/model-pictures/HX_Stomp_3.80.0_Model_Pictures.hxf --demo-progress
```

Both commands work with the pedal unplugged. The first validates the image and prints the write plan. The second also animates the progress bar.

## Install permanently with `hxflash`

Back up your presets and IRs using HX Edit. Close HX Edit and Line 6 Updater, disconnect other Line 6 devices, and connect the HX Stomp directly to the Linux system. The user running the tool needs USB access to the HX Stomp (`0e41:4246`) and the boot ROM (`1fc9:000c`). If Linux denies access, a local desktop session can use these udev rules under `/etc/udev/rules.d/70-hx-stomp.rules`:

```text
SUBSYSTEM=="usb", ATTR{idVendor}=="0e41", ATTR{idProduct}=="4246", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="1fc9", ATTR{idProduct}=="000c", TAG+="uaccess"
```

Reload the rules with `sudo udevadm control --reload-rules` and reconnect the pedal before starting.

```sh
python tools/hxflash.py status
python tools/hxflash.py flash build/model-pictures/HX_Stomp_3.80.0_Model_Pictures.hxf --device
```

`--device` performs the permanent write to internal storage. The installer validates the image, identifies the original Line 6 updater on USB, and writes 14 application and data entries. The write plan excludes the bootloader. The progress bar counts bytes acknowledged by the device. **Keep USB and power connected until `All 14 application/data entries and final device result accepted` appears**, then check that the Stomp starts normally.

If the update stops, record the error and the entry number. For stock recovery, hold **PAGE ▶** while powering on and reinstall the original HXF with Line 6 Updater. The locally built HXF can also be selected from Line 6 Updater in place of the Linux install command.
