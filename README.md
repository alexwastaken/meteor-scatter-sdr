# Meteor Echo Station

A receive-only, meteor-scatter observation app for a Nooelec NESDR SMArt v5,
a horizontal FM-band Yagi, a Mac, or a Raspberry Pi 4. The app keeps radio data
on the computer running it.

It detects **unconfirmed meteor-echo candidates**, not meteorite impacts. A
distant terrestrial FM broadcast may be reflected briefly by the ionized trail
created when a meteoroid ablates high in the atmosphere. One receiver cannot
locate a meteorite, reconstruct a trajectory, or prove that every transient was
a meteor.

## What this version does

- Opens the physical Nooelec receiver directly through `pyrtlsdr` and shows its
  model and serial number.
- Uses a meteor-only observation profile instead of a general SDR tuning deck.
- Samples the detector internally at up to 50 analyses per second while limiting
  the browser waterfall to 12.5 updates per second.
- Makes a 30-second echo-power timeline the primary scientific display and uses
  the waterfall as supporting frequency-versus-time evidence.
- Keeps exactly the newest 30 seconds of unsigned 8-bit I/Q in RAM.
- Saves raw `.cu8`, JSON metadata, and a waterfall preview only after a
  multi-frame rise has decayed and passed a surrounding-band interference test.
- Stores receiver, antenna, observer, target, duration, rise, and integrity
  metadata with new candidates.
- Uses a fixed 28 dB tuner gain so AGC jumps cannot resemble meteor echoes.
- Separates the local-FM audio check into a no-visual mode that disables meteor
  detection and candidate saving, then restores the meteor profile automatically.
- Keeps simulated training candidates visually separate from physical RF data.

Automatic candidates are never labeled “confirmed meteor.” Aircraft,
electrical noise, tropospheric propagation, direct station reception, and
receiver overload can all produce false positives.

## Hardware

- Raspberry Pi 4 Model B with 4 GB RAM, or a Mac for initial setup
- Nooelec NESDR SMArt v5
- 87.5–108 MHz Yagi antenna, mounted with horizontal elements
- 75-ohm coax with F-male connector
- F-female to SMA-male adapter at the SDR end

The antenna is 75 ohms and the SDR input is 50 ohms. That modest mismatch is
acceptable for this receiving experiment. The RTL-SDR is receive-only; never
connect it to a transmitter.

## Install or update

Use a current 64-bit Raspberry Pi OS. For a first Raspberry Pi installation over
HTTPS:

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

For an existing checkout on the Pi or Mac:

```bash
cd meteor-scatter-sdr
git pull
. .venv/bin/activate
python -m pip install -e .
```

Check the physical receiver:

```bash
meteor-sdr doctor
```

On Raspberry Pi OS, if a TV kernel driver has claimed the dongle, create
`/etc/modprobe.d/blacklist-rtl-sdr.conf` containing:

```text
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
```

Reboot and run `meteor-sdr doctor` again.

## Start the application

```bash
meteor-sdr serve
```

Open `http://127.0.0.1:8787`. For a safe synthetic training session without an
SDR:

```bash
meteor-sdr serve --demo --port 8788
```

To view a Raspberry Pi from a Mac on the same trusted network:

```bash
meteor-sdr serve --host 0.0.0.0 --no-browser
```

Then open `http://raspberrypi.local:8787`. The dashboard has no authentication,
so do not expose it to the public internet.

## Carmel Valley suggested profile

The built-in starting profile is intentionally presented as a **field-test
candidate**, not a guaranteed meteor channel:

- Watched channel: **95.3 MHz**
- Intended transmitter: **KZJH, Jackson, Wyoming**
- Licensed ERP: **100 kW, nondirectional**
- Approximate distance: **1,298 km**
- Approximate true bearing from Carmel Valley: **024°**
- Antenna: horizontal elements, Yagi boom pointed north-northeast
- Capture window: **512 kHz**, centered at **95.460 MHz**
- Fixed tuner gain: **28 dB**

The offset center keeps the 95.3 MHz target away from the RTL-SDR center/DC
artifact. The 512 kHz window is narrow enough to reduce unrelated FM activity
while retaining surrounding spectrum for broadband-interference rejection. At
this rate, the 30-second memory and each raw candidate are about 30.7 MB.

The main known complication is KLLY on 95.3 MHz near Oildale, California,
approximately 324 km away. Before leaving the station armed, use the receiver
audio check and observe the channel. If continuous programming or persistent
reception is present at the actual antenna heading, 95.3 MHz is unsuitable and
another distant, normally absent transmitter must be selected.

The former 98.5 MHz default was removed because licensed transmitters in
Cathedral City and Las Vegas are close enough to increase direct and
tropospheric contamination risk.

## Using the meteor workflow

1. Connect the receiver and horizontal Yagi.
2. Aim the Yagi toward the candidate transmitter bearing.
3. Select **Calibrate meteor target**. This clears mixed-frequency samples,
   learns a new background, and refills the 30-second RAM memory.
4. Wait for the detector state to reach **Armed**.
5. Leave the station running. Recent unconfirmed candidates appear
   automatically; there is no manual refresh or download workflow.

Sensitivity presets are meteor-specific:

- **Strict:** 10 dB minimum rise; fewer false alarms.
- **Balanced:** 8 dB; default.
- **Sensitive:** 6 dB; more weak candidates and more review work.

Changing sensitivity or target frequency deliberately restarts background
learning and the evidence memory.

The dashed **watch marker** is a screen overlay, not a transmitted or received
signal. The muted **receiver center** marker identifies where a stationary
RTL-SDR center artifact may appear.

## Receiver audio check

Open the collapsed **Receiver audio check** section and select **Play local FM
94.9**. This is only a hardware test:

- no spectrum or waterfall is shown for the audio check;
- meteor detection and candidate saving are disabled;
- browser audio uses a filtered mono-FM decoder;
- stopping the check restores 95.3 MHz, the offset meteor center, detector
  calibration, and the 30-second memory.

Audio is not the scientific meteor record. Raw I/Q, the derived echo-power
profile, metadata, and waterfall evidence are the useful observation products.

## Candidate evidence

Candidate files are stored under:

```text
captures/events/
  meteor_candidate_....cu8
  meteor_candidate_....cu8.json
  meteor_candidate_....waterfall.png
```

The rolling RAM memory overwrites older unsaved samples automatically. Saved
candidates remain until deliberately removed; the dashboard reports archive
size and free disk space but never deletes evidence automatically.

Candidate labels:

- **Short-trail candidate / underdense-like:** fast rise and decay, often only
  a fraction of a second.
- **Long-trail candidate / overdense-like:** lasts seconds or longer and may
  fade irregularly.
- **Likely interference:** repeating spikes, continuous reception, or much of
  the monitored spectrum rising together.

The JSON SHA-256 hash allows the raw file’s integrity to be checked later.
`cu8` is native RTL-SDR data: one unsigned I byte followed by one unsigned Q
byte, both centered near 127.5.

## Research basis

- [NASA meteor terminology](https://science.nasa.gov/solar-system/meteors-meteorites/facts/)
- [International Meteor Organization radio-observation introduction](https://www.imo.net/observations/methods/radio-observation/intro/)
- [IMO practical receiver and transmitter guidance](https://www.imo.net/observations/methods/radio-observation/practical/)
- [IMO meteor-reflection signatures](https://www.imo.net/observations/methods/radio-observation/reflection/)
- [BRAMS annotated meteor and interference spectrograms](https://brams.aeronomie.be/theory/spectrograms)
- [FCC KZJH record](https://transition.fcc.gov/fcc-bin/fmq?facid=65279)
- [FCC KLLY record](https://transition.fcc.gov/fcc-bin/fmq?facid=7709)

Distances and bearings are calculated from FCC transmitter coordinates and are
approximate. Actual reception at the installed antenna remains the deciding
test.

## Run tests

Tests do not require SDR hardware:

```bash
python -m unittest discover -s tests -v
```

Raw captures and candidate files are excluded from Git because they become
large quickly.
