# Meteor Scatter SDR

A small, dependable starting point for recording raw radio-frequency data from a
Nooelec NESDR SMArt v5 on a Raspberry Pi 4. The first version deliberately does
one thing: capture a finite block of complex I/Q samples and save enough metadata
to interpret it later.

Meteor detection and a live waterfall will be built on top of this capture layer.

## Hardware

- Raspberry Pi 4 Model B with 4 GB RAM
- Nooelec NESDR SMArt v5
- 87.5-108 MHz Yagi antenna
- 75-ohm coax with F-male connectors
- F-female to SMA-male adapter at the SDR end

The antenna is 75 ohms and the SDR input is 50 ohms. For reception this mismatch
is small enough for the first version; no matching transformer is required. The
receiver should never be connected to a transmitter.

## Install on Raspberry Pi OS

Use a current 64-bit Raspberry Pi OS installation, then run:

```bash
sudo apt update
sudo apt install rtl-sdr python3-venv
git clone git@github.com:alexwastaken/meteor-scatter-sdr.git
cd meteor-scatter-sdr
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

The Python package has no third-party runtime dependencies. It uses the native
`rtl_sdr` utility from Raspberry Pi OS to communicate with the USB receiver.

## Connect and check the receiver

Plug the SDR directly into a Pi USB port, connect the antenna, and run:

```bash
meteor-sdr doctor
```

A successful check ends with `RTL-SDR device check passed.`

If the output says that a kernel driver has claimed the device, create
`/etc/modprobe.d/blacklist-rtl-sdr.conf` containing:

```text
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
```

Then reboot the Pi and run `meteor-sdr doctor` again.

## Make the first capture

The default command records ten seconds centered at 100 MHz with a 1.024 MHz
span and automatic gain:

```bash
meteor-sdr capture
```

This produces two files under `captures/`:

- `*.cu8`: alternating unsigned 8-bit I and Q samples
- `*.cu8.json`: tuning settings, timestamps, sample count, and SHA-256 checksum

The default capture is about 20.5 MB. Raw I/Q grows quickly—about 123 MB per
minute at the default sample rate—so the first version records one bounded file
instead of running forever.

Examples:

```bash
# Five seconds around 98.1 MHz
meteor-sdr capture --center-frequency 98.1M --duration 5

# Manual gain is useful once repeatable signal levels matter
meteor-sdr capture --center-frequency 98.1M --gain 28 --duration 10

# Use a project configuration file
cp config.example.toml config.toml
meteor-sdr capture --config config.toml
```

## Use the local dashboard

Start the browser interface on the Raspberry Pi:

```bash
meteor-sdr serve
```

The command opens `http://127.0.0.1:8787` in the Pi's browser. From the
dashboard you can choose the frequency, span, duration, and gain; start one
capture; follow its progress; and download the raw `.cu8` data and JSON metadata
together as a ZIP file. The ZIP is assembled only when requested, so it does not
permanently double the storage used by each capture.

To open the dashboard from another computer on the same trusted network, run:

```bash
meteor-sdr serve --host 0.0.0.0 --no-browser
```

Then visit `http://raspberrypi.local:8787` from that computer. This first local
version has no sign-in screen, so do not expose the port to the public internet.
Press `Ctrl+C` in the Pi terminal to stop the dashboard.

The antenna and current validation constrain captures to the 87.5-108 MHz FM
band. We will select a quiet local frequency and a distant transmitter before
adding meteor-event detection.

## Run tests

The tests use a fake SDR executable, so they run without radio hardware:

```bash
python -m unittest discover -s tests -v
```

## Capture format

`cu8` is the native byte format written by `rtl_sdr`. Each complex sample is two
bytes: unsigned I followed by unsigned Q, both centered near 127.5. To convert a
byte value `x` to an approximately normalized component later, use
`(x - 127.5) / 127.5`.

The metadata sidecar is the source of truth for center frequency, sample rate,
gain, timing, and file integrity. Capture files are intentionally excluded from
Git because they become large very quickly.
