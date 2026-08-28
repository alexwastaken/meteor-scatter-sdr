# Meteor Scatter SDR

A local live spectrum, waterfall, and meteor-scatter candidate recorder for a
Nooelec NESDR SMArt v5 and Raspberry Pi 4. The browser is a display only: radio
samples stay on the Pi.

## What this version does

- streams unsigned 8-bit I/Q directly from USB through the maintained `pyrtlsdr` module
- starts a detailed 512-bin live spectrum and waterfall automatically
- animates the trace smoothly while limiting Raspberry Pi and browser workload
- provides fixed ±200 kHz, exact-frequency, span, preset, and watch buttons;
  the plots themselves are view-only
- demodulates live mono FM audio for the watched frequency after you press
  `Enable audio` (browser autoplay rules require the click)
- holds exactly the newest 30 seconds of raw I/Q in RAM
- automatically overwrites older non-event samples instead of filling the disk
- archives the newest 30 seconds as `.cu8`, JSON, and a waterfall PNG only after
  a multi-frame channel rise has decayed and passed a broadband-RFI check

An automatic save is **not proof of a meteor**. Local interference, aircraft,
tropospheric propagation, and transmitter changes can all produce candidates.

## Hardware

- Raspberry Pi 4 Model B with 4 GB RAM
- Nooelec NESDR SMArt v5
- 87.5–108 MHz Yagi antenna
- 75-ohm coax with F-male connector
- F-female to SMA-male adapter at the SDR end

The antenna is 75 ohms and the SDR input is 50 ohms. That mismatch is acceptable
for this receiving experiment. Never connect the receiver to a transmitter.

## Install or update on Raspberry Pi OS

Use a current 64-bit Raspberry Pi OS. For a first installation over HTTPS:

```bash
sudo apt update
sudo apt install rtl-sdr python3-venv
git clone https://github.com/alexwastaken/meteor-scatter-sdr.git
cd meteor-scatter-sdr
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

For an existing checkout:

```bash
cd meteor-scatter-sdr
git pull
. .venv/bin/activate
python -m pip install -e .
```

The last command installs NumPy, which converts I/Q time samples into the
frequency spectrum shown by the app.

## Check the receiver

Plug the SDR into a Pi USB port, connect the antenna, and run:

```bash
meteor-sdr doctor
```

If a kernel driver has claimed the device, create
`/etc/modprobe.d/blacklist-rtl-sdr.conf` containing:

```text
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
```

Reboot and run `meteor-sdr doctor` again.

## Start the live application

To explore the complete interface before the USB receiver is available, start
the synthetic RF source:

```bash
meteor-sdr serve --demo --port 8788
```

Demo mode drives the real spectrum, waterfall, tuning controls, rolling buffer,
and detector with repeatable noise, carriers, and recurring meteor-like pings.
The top-right indicator reads `SIMULATION LIVE`, so simulated samples cannot be
mistaken for antenna data.

On the Pi desktop:

```bash
meteor-sdr serve
```

This opens `http://127.0.0.1:8787`. To view it from a Mac on the same trusted
network:

```bash
meteor-sdr serve --host 0.0.0.0 --no-browser
```

Then open `http://raspberrypi.local:8787` on the Mac. There is no sign-in screen,
so do not expose this port to the public internet.

The receiver starts automatically with `meteor-sdr serve`. The detector first
learns a short baseline; saving is enabled after both the baseline and
30-second buffer are ready.

The spectrum and waterfall are view-only. Tune with the fixed ±200 kHz buttons,
type an exact center frequency, use the Carmel Valley research-target button,
or change the span between 0.250, 1.024, and 2.048 MHz. `Watch center` moves the
detector to the receiver's center without retuning. Live audio follows that
watch frequency; press `Enable audio` again to turn it off.
Retuning deliberately clears and refills the 30-second memory so samples from
different frequency windows are never mixed; moving only the watch line does
not clear it. Frequency, span, and gain commands are applied to the existing
radio stream, so normal tuning no longer stops and relaunches the receiver.
The direct USB binding removes the extra TCP process and its queue from the live
waterfall path.

At the recommended 1.024 MS/s setting, the RAM ring is about 61.4 MB and each
saved candidate is about 61.4 MB. Candidate files are stored under:

```text
captures/events/
  meteor_candidate_....cu8
  meteor_candidate_....cu8.json
  meteor_candidate_....waterfall.png
```

Stopping monitoring discards the current unsaved ring. Archived candidates are
kept until you deliberately remove them.

## Important tuning limitation

The RTL-SDR cannot display all 20.5 MHz of the FM broadcast band at once. The
recommended 1.024 MS/s setting shows only a 1.024 MHz window around the center
frequency. Meteor-scatter observing works best when the watched frequency is a
specific distant transmitter that is normally absent at your location, not an
arbitrary busy local FM station.

For the current Carmel Valley setup, start with **98.5 MHz** and point the Yagi
roughly east-northeast. The intended test transmitter is KRFM in Show Low,
Arizona, roughly 690 km away. A local SDR survey found 98.5 MHz materially
quieter than the other researched high-power targets, 100.5 MHz near Sacramento
and 106.9 MHz in San Francisco. This is a test target rather than guaranteed
station attribution: co-channel transmitters and antenna direction matter, so
re-run a local survey after moving the antenna.

## Reading the display

The spectrum is the current signal strength from low frequency on the left to
high frequency on the right. The waterfall repeats those spectra over time:
the newest row is at the top and older rows move downward.

- A possible **underdense meteor echo** is a thin, bright, short vertical dash at
  or near the watched frequency, often less than a second long.
- A possible **overdense echo** is brighter and lasts longer, often with an
  irregular, curved, split, or slowly fading shape.
- A full-width horizontal flash is broadband interference. An always-present
  line, regularly repeating pattern, or activity across many frequencies is
  also less likely to be a meteor.

The detector learns robust median baselines for the watched FM channel and for
the surrounding spectrum. It requires at least two elevated frames, waits for
three frames near baseline, rejects a simultaneous surrounding-band rise, and
records the peak, duration, and `underdense_like` or `overdense_like` profile in
JSON. Use the saved PNG for a quick visual review. The matching 30-second `.cu8`
file remains the full-fidelity source and contains the entire displayed span.

## Raw capture compatibility

The original one-shot command remains available for controlled experiments:

```bash
meteor-sdr capture --center-frequency 100M --duration 10
```

`cu8` is the native `rtl_sdr` format. Each complex sample is two bytes: unsigned
I followed by unsigned Q, both centered near 127.5. Convert a byte `x` to an
approximately normalized component with `(x - 127.5) / 127.5`.

## Run tests

Tests do not require SDR hardware:

```bash
python -m unittest discover -s tests -v
```

Raw capture and candidate files are excluded from Git because they become large
quickly.
