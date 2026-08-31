# Meteor Echo Monitor: Simple Guide

This guide explains what the app does, what the displays mean, how a candidate
is captured, and what the current limitations are.

## The short version

The app turns a receive-only RTL-SDR and a horizontal Yagi antenna into a
passive meteor-scatter monitor.

A distant FM transmitter is normally below the radio horizon. When a meteoroid
enters the upper atmosphere, it can leave a short-lived ionized trail. Under the
right geometry, that trail briefly reflects part of the distant station's radio
signal toward the Yagi. The app looks for that temporary reflected signal.

The app does **not** transmit. It does not detect a ground impact, locate a
meteorite, or prove that every saved signal came from a meteor. Every automatic
save is an **unconfirmed meteor echo candidate**.

## Meteor, meteoroid, and meteorite

- A **meteoroid** is the small natural object while it is still in space.
- A **meteor** is the visible and atmospheric event produced as it enters the
  atmosphere.
- A **meteorite** is material that survives to the ground.

This receiver observes a possible radio reflection associated with the
atmospheric trail. It is not a meteorite detector.

## The signal path

```text
distant FM transmitter
          |
          | normally passes above the receiver
          v
  ionized meteor trail  ---- reflected radio ---->  Yagi antenna
                                                        |
                                                        v
                                                   RTL-SDR
                                                        |
                                                        v
                                               this local app
```

The radio energy comes from the ground transmitter. The meteor trail acts like
a temporary reflector; the meteor is not broadcasting a radio program.

## What the app does

While Meteor Watch is running, the app:

1. Opens the physical RTL-SDR and reports its actual product name and serial
   number when the driver provides them.
2. Tunes a narrow window around one suggested distant FM channel.
3. Measures the watched channel and the surrounding spectrum up to 50 times per
   second.
4. Learns a rolling background level instead of treating every bright pixel as
   a meteor.
5. Keeps the newest 30 seconds of raw I/Q radio samples in RAM. Older unsaved
   samples are overwritten automatically.
6. Looks for a rise that persists for more than one analysis and is stronger in
   the watched channel than in the surrounding band.
7. Waits for the signal to return toward the background.
8. If the 30-second memory is full and the event passes the basic checks, saves
   the newest 30 seconds as an unconfirmed raw-data candidate. It does not save
   a waterfall image.

The detector analyzes at up to 50 updates per second. The browser paints at
about 12.5 frames per second to keep the Mac and Raspberry Pi interface smooth.

## How the automatic detector decides to save

The detector is deliberately more selective than a simple spike detector:

- It watches roughly 80 kHz on either side of the selected target.
- It uses spectrum farther than roughly 120 kHz from the target as an
  interference reference.
- It learns a median background from 125 quiet analyses.
- It requires at least two consecutive high analyses to start an event.
- Its trigger is the selected sensitivity or a higher adaptive level when the
  background is unstable.
- It rejects a candidate when too much of the surrounding band rises at the
  same time.
- It completes the event after three quiet analyses.
- Accepted durations are approximately 0.08 to 60 seconds.
- It waits 15 seconds after a completed candidate before accepting another.

The sensitivity buttons set the minimum rise above background:

- **Strict:** 10 dB. Fewer false triggers.
- **Balanced:** 8 dB. The default starting point.
- **Sensitive:** 6 dB. More weak candidates, but more false positives to
  review.

These checks reduce simple interference, but they cannot confirm a meteor by
themselves.

## Reading the Echo Power display

Echo Power is the main detection view.

- Left to right is the newest 30 seconds of time.
- The cyan trace is the watched channel's rise above its learned background.
- The dim reference trace shows how much the surrounding spectrum changed.
- The trigger line is the current adaptive threshold.

A useful candidate often has a sudden rise followed by a decay. A high spike by
itself is not enough: its duration, shape, frequency width, surrounding-band
behavior, and repetition all matter.

## Reading the waterfall

The waterfall is supporting evidence. It shows:

- **horizontal position:** radio frequency;
- **vertical position:** time, with the newest row at the top;
- **brightness/color:** received power.

What to look for:

- A short meteor-like candidate may appear suddenly near the watched channel
  and fade within a fraction of a second.
- A longer candidate may remain for seconds and fade irregularly.
- A whole row becoming bright across most of the waterfall is likely broadband
  interference or receiver overload.
- A continuous line is a steady station, local interference, or a receiver
  artifact rather than an isolated meteor echo.
- Aircraft often produce long, smooth, curved, or S-shaped Doppler traces.

The dashed **WATCH MARKER** is only a screen overlay showing the target
frequency. It is not a received signal and the software is not transmitting it.

The **RECEIVER CENTER** line marks the hardware tuning center. RTL-SDR receivers
often show a stationary center/DC artifact there. The meteor target is
intentionally offset from that point so the detector does not mistake the
center artifact for an echo.

## What gets saved

For every automatic candidate, the app creates two local files in
`captures/events/`:

```text
meteor_candidate_....cu8
meteor_candidate_....cu8.json
```

### Raw `.cu8`

This is the actual radio evidence. Each complex sample is stored as one unsigned
8-bit I value followed by one unsigned 8-bit Q value. At the default 1,024,000
samples per second, a full 30-second candidate is about 61.4 MB.

### JSON metadata

The JSON records the UTC event times, estimated duration, short- or long-trail
profile, peak rise, surrounding-band rise, tuned frequencies, sample rate,
gain, frequency correction, receiver identity, antenna description, suggested
transmitter, data format, and a SHA-256 integrity hash of the raw file.

### No image snapshot

This version does not generate a PNG snapshot or display a saved-image gallery.
Use the live Echo Power trace and waterfall for manual visual review. The raw
I/Q is retained so a better analysis or image can be generated later without
depending on an inaccurate automatic snapshot.

The app does not automatically delete saved raw evidence. Check the
`captures/events/` folder because long sessions can use many gigabytes. Existing
PNG files made by an older version are left untouched, but no new PNGs are
created.

## Candidate labels

- **Short-trail / underdense-like:** no more than about 0.7 seconds, usually a
  fast rise and decay.
- **Long-trail / overdense-like:** longer than about 0.7 seconds and possibly
  irregular fading.
- **Simulation / training:** generated data from `--demo`, not an antenna.
- **Unconfirmed candidate:** physical data that still needs human review.

These are descriptions of the signal profile, not proof of its cause.

## Carmel Valley starting profile

The current built-in starting point is:

- **Watched frequency:** 95.3 MHz
- **Intended transmitter:** KZJH, Jackson, Wyoming
- **Licensed power:** 100 kW, nondirectional
- **Approximate distance:** 1,298 km
- **Approximate true bearing from Carmel Valley:** 024 degrees
- **Yagi polarization:** horizontal elements
- **Yagi direction:** boom toward north-northeast, approximately 024 degrees
  true
- **Capture span:** 1.024 MHz
- **Receiver center:** 95.460 MHz
- **Fixed tuner gain:** 28 dB

This is a **field-test suggestion**, not a guaranteed meteor channel. Another
95.3 MHz station, KLLY near Oildale, California, is much closer and may be
received directly or through enhanced propagation. If speech, music, or a
persistent station is normally present on 95.3 MHz at the chosen antenna
heading, it is not a good meteor target at that location and time.

The former 98.5 MHz default was removed because licensed same-channel stations
in Cathedral City and Las Vegas are close enough to create a higher risk of
direct or tropospheric reception.

The 1.024 MHz span is a driver-supported rate that keeps the complete target
and enough surrounding spectrum available for interference comparison. The
RTL-SDR driver rejects the range above 300 kS/s through 900 kS/s, which includes
512 kS/s. Browser drawing remains rate-limited so this wider capture does not
force the interface to redraw every radio block. The 160 kHz tuning offset keeps
95.3 MHz away from the receiver's center artifact.

## Calibration and a simple observing routine

1. Connect the Yagi to the RTL-SDR and connect the RTL-SDR to the computer.
2. Confirm the app shows the real receiver name rather than Demo or Not
   connected.
3. Mount the Yagi with its elements horizontal.
4. Aim the boom approximately 024 degrees true.
5. Click **Listen** in the live panel to hear the selected target as filtered
   mono wide FM.
6. Expect hiss or weak noise when the channel is suitably absent. If continuous
   recognizable speech or music is present, choose another target frequency or
   antenna heading.
7. Stop audio when finished. The graph, waterfall, detector, and evidence
   memory have continued running throughout the listening check.
8. Select **Calibrate meteor target** after any target, gain, cable, or antenna
   change.
9. Wait until the detector and evidence memory both report ready.
10. Leave the antenna, frequency, gain, and cables unchanged while observing.
11. Review every saved candidate as possible meteor, interference, or unsure.

Calibrating does not magically prove the channel is quiet. It learns the local
background; the operator still needs to check for continuous reception and
repeating interference.

## The selected-target speaker monitor

The **Listen** control is inside the live Meteor Watch panel. It plays the
currently selected FM target, 95.3 MHz by default, as filtered mono wide FM.
Audio is extracted from the same 1.024 MHz RF capture used by the meteor
displays and detector, so starting audio does not retune the SDR.
Meteor Watch continues while you listen: the graph, waterfall, detector, and
30-second RF memory all remain active.

Turn the computer volume down before starting. With no receivable station,
wide-FM normally produces hiss rather than literal silence. A possible
forward-scatter burst could sound like a very short fragment of distant speech
or music, a flutter, or a pop. Broadcast FM is modulated, so it may not sound
like a clean radar-style “ping.”

The purpose of this mode is to decide whether the selected target is normally
absent. Continuous intelligible programming means that a local, co-channel, or
persistently propagated station is present and the frequency is unsuitable for
simple transient monitoring at that antenna heading.

While audio is active:

- The graph and waterfall continue updating.
- Candidate detection, saving, calibration, and the 30-second memory continue.
- Audio itself is not saved, but the normal raw candidate evidence can still be
  archived if the detector completes an event.
- Stopping audio does not reset or discard the observation session.

Audio alone cannot confirm a meteor echo. Aircraft, tropospheric propagation,
other same-frequency transmitters, and local interference can all create brief
sounds. Treat audio only as a setup and review aid; the candidate evidence is
the raw I/Q, relative-power profile, timing, metadata, and waterfall context.

## Common false positives

- **Local FM or direct reception:** continuous music, speech, or steady power.
- **Tropospheric propagation:** a distant station remains present for minutes
  or hours instead of appearing as one short event.
- **Aircraft:** a long, smooth curved or S-shaped trace.
- **Broadband interference:** many frequencies rise together.
- **Lightning or electrical switching:** a very short broadband flash.
- **Receiver overload:** the entire noise floor rises, mirrors appear, or many
  signals change together.
- **Center/DC artifact:** a line remains at the receiver center and follows the
  center when retuned.
- **Computer, USB, or power-supply noise:** repeating pulses, evenly spaced
  lines, or mechanical/chopped patterns.

Repetition at exact intervals is a strong clue that a signal is human-made or
local interference.

## Important limits of this version

- One receiver cannot locate the trail, reconstruct its trajectory, determine
  whether material reached the ground, or reliably associate one echo with a
  named meteor shower.
- A single radio observation cannot prove the signal was a meteor. Confidence
  improves with a synchronized second radio station or optical camera.
- The selected transmitter cannot be identified with certainty from one
  same-frequency receiver.
- The detector does not yet calculate an aircraft track or automatically
  classify every kind of interference.
- Dropped-sample and clock-quality measurements are not yet stored with each
  event.
- The archive is the newest 30 seconds at event completion. An event longer
  than the memory can lose its beginning, and this version does not add a
  separate post-event recording tail.
- Candidate writing happens in the background. The interface can report a
  trigger before a large raw file has completely finished writing.
- Saved candidates remain on disk until the operator deliberately moves or
  removes them.

These limitations are why the app consistently says **candidate** rather than
**confirmed meteor**.

## If the receiver is not detected

Run:

```bash
meteor-sdr doctor
```

On a Mac, reconnect the dongle directly or through a known-good powered adapter
and close any other SDR software that could own it. On Raspberry Pi OS, the TV
kernel driver may need to be blacklisted as described in the project README.

## Research behind these choices

- [NASA: meteor terminology](https://science.nasa.gov/solar-system/meteors-meteorites/facts/)
- [NASA: Meteoroid Engineering Model technical review](https://ntrs.nasa.gov/api/citations/20200000049/downloads/20200000049.pdf)
- [International Meteor Organization: radio observation introduction](https://www.imo.net/observations/methods/radio-observation/intro/)
- [IMO: meteor reflection signatures](https://www.imo.net/observations/methods/radio-observation/reflection/)
- [IMO: practical receiver, antenna, cadence, and transmitter guidance](https://www.imo.net/observations/methods/radio-observation/practical/)
- [IMO: false-positive and statistical cautions](https://www.imo.net/observations/methods/radio-observation/reduction/)
- [BRAMS: annotated meteor, aircraft, and interference examples](https://brams.aeronomie.be/theory/spectrograms)
- [BRAMS: receiver and timing information](https://brams.aeronomie.be/brams-network/fact-sheet)
- [FCC: KZJH 95.3 MHz record](https://transition.fcc.gov/fcc-bin/fmq?facid=65279)
- [FCC: KLLY 95.3 MHz record](https://transition.fcc.gov/fcc-bin/fmq?facid=7709)
- [FCC: KDES-FM 98.5 MHz record](https://transition.fcc.gov/fcc-bin/fmq?facid=24253)
- [FCC: KLUC-FM 98.5 MHz record](https://transition.fcc.gov/fcc-bin/fmq?facid=47744)

Distances and bearings above were calculated from FCC transmitter coordinates
and are approximate. Actual reception at the installed antenna is the deciding
test.
