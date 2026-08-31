const $ = (selector) => document.querySelector(selector);

const METEOR_SPAN_HZ = 1_024_000;
const METEOR_CENTER_OFFSET_HZ = 160_000;
const RADIO_CHECK_SPAN_HZ = 250_000;
const FM_LOW_HZ = 87_500_000;
const FM_HIGH_HZ = 108_000_000;
const POWER_WINDOW_MS = 30_000;

const elements = {
  connection: $("#connection"),
  connectionLabel: $("#connection-label"),
  receiverName: $("#receiver-name"),
  receiverDetail: $("#receiver-detail"),
  sourceBanner: $("#source-banner"),
  sourceTitle: $("#source-title"),
  sourceDetail: $("#source-detail"),
  bufferValue: $("#buffer-value"),
  bufferMeter: $("#buffer-meter"),
  detectorState: $("#detector-state"),
  detectorDetail: $("#detector-detail"),
  headerTarget: $("#header-target"),
  excess: $("#excess-value"),
  thresholdValue: $("#threshold-value"),
  candidateCount: $("#candidate-count"),
  meteorFrequency: $("#meteor-frequency"),
  sampleRate: $("#sample-rate"),
  gain: $("#gain"),
  detection: $("#detection-frequency"),
  threshold: $("#threshold"),
  sensitivityButtons: [...document.querySelectorAll("[data-threshold]")],
  calibrate: $("#calibrate-button"),
  watch: $("#watch-button"),
  retry: $("#retry-button"),
  message: $("#message"),
  power: $("#power-chart"),
  powerThresholdLabel: $("#power-threshold-label"),
  waterfall: $("#waterfall"),
  waterfallWrap: $("#waterfall-wrap"),
  watchMarker: $("#watch-marker"),
  centerMarker: $("#center-marker"),
  canvasEmpty: $("#canvas-empty"),
  canvasSummary: $("#canvas-summary"),
  modeOverlay: $("#mode-overlay"),
  retuneOverlay: $("#retune-overlay"),
  retuneFrequency: $("#retune-frequency"),
  low: $("#low-frequency"),
  targetAxis: $("#axis-target"),
  high: $("#high-frequency"),
  audioButton: $("#audio-button"),
  audioNote: $("#audio-note"),
  tooltips: [...document.querySelectorAll(".info-trigger")],
  visualInfoButtons: [...document.querySelectorAll(".visual-info-button")],
};

let currentStatus = null;
let monitorState = "stopped";
let pendingFrame = null;
let renderedSequence = 0;
let radioCheckActive = false;
let hardwareRetryBusy = false;
let lastHardwareRetry = 0;
let waterfallStarted = false;
let waterfallRow = null;
let powerHistory = [];
let audioEnabled = false;
let audioContext = null;
let audioGainNode = null;
let audioAbort = null;
let audioNextTime = 0;
let meteorSettingsBeforeAudio = null;
let audioTransitionBusy = false;

function mhz(value, digits = 3) {
  return (Number(value) / 1e6).toFixed(digits);
}

function frequency(value) {
  return `${mhz(value)} MHz`;
}

function showMessage(text, error = false) {
  elements.message.hidden = false;
  elements.message.textContent = text;
  elements.message.classList.toggle("error", error);
}

function clearMessage() {
  elements.message.hidden = true;
  elements.message.textContent = "";
  elements.message.classList.remove("error");
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function meteorTargetHz() {
  return Math.round(Number(elements.meteorFrequency.value) * 1e6);
}

function meteorCenterHz(targetHz) {
  const halfSpan = METEOR_SPAN_HZ / 2;
  const minimumCenter = FM_LOW_HZ + halfSpan;
  const maximumCenter = FM_HIGH_HZ - halfSpan;
  const positive = targetHz + METEOR_CENTER_OFFSET_HZ;
  if (positive >= minimumCenter && positive <= maximumCenter) return positive;

  const negative = targetHz - METEOR_CENTER_OFFSET_HZ;
  if (negative >= minimumCenter && negative <= maximumCenter) return negative;

  return Math.min(maximumCenter, Math.max(minimumCenter, positive));
}

function meteorPayload() {
  const target = meteorTargetHz();
  return {
    center_frequency_hz: meteorCenterHz(target),
    sample_rate_hz: METEOR_SPAN_HZ,
    gain_db: elements.gain.value === "auto" ? "auto" : Number(elements.gain.value),
    detection_frequency_hz: target,
    threshold_db: Number(elements.threshold.value),
    observation_mode: "meteor",
  };
}

function meteorSettingsFromStatus(status) {
  const settings = status?.settings;
  if (!settings || settings.observation_mode !== "meteor") return null;
  return {
    center_frequency_hz: Number(settings.center_frequency_hz),
    sample_rate_hz: Number(settings.sample_rate_hz),
    gain_db: settings.gain_db,
    detection_frequency_hz: Number(settings.detection_frequency_hz),
    threshold_db: Number(settings.threshold_db),
    observation_mode: "meteor",
  };
}

function radioPayload() {
  const target = meteorTargetHz();
  return {
    center_frequency_hz: target,
    sample_rate_hz: RADIO_CHECK_SPAN_HZ,
    gain_db: elements.gain.value === "auto" ? "auto" : Number(elements.gain.value),
    detection_frequency_hz: target,
    threshold_db: Number(elements.threshold.value),
    observation_mode: "radio_check",
  };
}

function postSettings(url, payload) {
  return request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function setSensitivity(value) {
  elements.threshold.value = String(value);
  elements.thresholdValue.textContent = `${Number(value).toFixed(1)} dB`;
  elements.sensitivityButtons.forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.threshold) === Number(value));
  });
}

function setMarkerPositions(lower, upper, target, center) {
  const span = upper - lower;
  if (span <= 0) return;
  const targetPosition = ((target - lower) / span) * 100;
  const centerPosition = ((center - lower) / span) * 100;
  elements.watchMarker.style.left = `${Math.max(0, Math.min(100, targetPosition))}%`;
  elements.centerMarker.style.left = `${Math.max(0, Math.min(100, centerPosition))}%`;
  elements.low.textContent = frequency(lower);
  elements.targetAxis.textContent = `${frequency(target)} TARGET`;
  elements.high.textContent = frequency(upper);
}

function resetVisuals() {
  waterfallStarted = false;
  waterfallRow = null;
  powerHistory = [];
  const waterfallContext = elements.waterfall.getContext("2d");
  waterfallContext.clearRect(0, 0, elements.waterfall.width, elements.waterfall.height);
  drawPowerChart();
}

function applyReceiverStatus(status) {
  const receiver = status.receiver || { connected: false, display_name: "RTL-SDR not connected" };
  const demo = status.source === "simulation";
  const connected = !demo && Boolean(receiver.connected);
  const detected = !demo && Boolean(receiver.detected || connected);
  elements.connection.classList.toggle("live", connected);
  elements.connection.classList.toggle("error", !demo && status.state === "error");
  elements.sourceBanner.classList.toggle("demo", demo);
  elements.sourceBanner.classList.toggle("connected", connected);
  elements.sourceBanner.classList.toggle("disconnected", !demo && !detected);

  if (demo) {
    elements.connectionLabel.textContent = "TRAINING DATA · NO SDR";
    elements.receiverName.textContent = "SIMULATED RECEIVER";
    elements.receiverDetail.textContent = "NOT PHYSICAL RF DATA";
    elements.sourceTitle.textContent = "TRAINING MODE";
    elements.sourceDetail.textContent = "The current signals and saved training candidates are synthetic.";
  } else if (connected) {
    const serial = receiver.serial ? `SERIAL ${receiver.serial}` : "USB RECEIVER";
    elements.connectionLabel.textContent = "PHYSICAL SDR CONNECTED";
    elements.receiverName.textContent = receiver.display_name || "RTL-SDR receiver";
    elements.receiverDetail.textContent = serial;
    elements.sourceTitle.textContent = "PHYSICAL RECEIVER VERIFIED";
    elements.sourceDetail.textContent = `${receiver.display_name || "RTL-SDR receiver"} · ${serial} · receive-only hardware`;
  } else if (detected) {
    const serial = receiver.serial ? `SERIAL ${receiver.serial}` : "USB RECEIVER";
    const setupError = status.state === "error";
    elements.connectionLabel.textContent = setupError ? "SDR DETECTED · SETUP ERROR" : "SDR DETECTED · WATCH PAUSED";
    elements.receiverName.textContent = receiver.display_name || "RTL-SDR receiver";
    elements.receiverDetail.textContent = serial;
    elements.sourceTitle.textContent = setupError ? "PHYSICAL RECEIVER DETECTED" : "METEOR WATCH PAUSED";
    elements.sourceDetail.textContent = setupError
      ? "The USB receiver opened, but its radio settings could not be applied."
      : "The receiver was verified and is currently closed until Meteor Watch starts again.";
  } else {
    elements.connectionLabel.textContent = status.state === "starting" ? "CHECKING USB" : "SDR NEEDS ATTENTION";
    elements.receiverName.textContent = "RTL-SDR NOT CONNECTED";
    elements.receiverDetail.textContent = "NO USB RECEIVER OPEN";
    elements.sourceTitle.textContent = status.state === "starting" ? "CHECKING PHYSICAL RECEIVER" : "NO PHYSICAL RF DATA";
    elements.sourceDetail.textContent = "Reconnect the Nooelec receiver; the app will keep retrying safely.";
  }
  elements.receiverName.title = elements.receiverName.textContent;
}

function updateDetectorStatus(status, frame = null) {
  const mode = status.settings?.observation_mode || "meteor";
  if (mode === "radio_check") {
    elements.detectorState.textContent = "PAUSED";
    elements.detectorDetail.textContent = "RADIO CHECK · SAVING DISABLED";
    return;
  }
  if (status.state === "error") {
    const receiverFound = Boolean(status.receiver?.detected);
    elements.detectorState.textContent = receiverFound ? "TUNING ERROR" : "NEEDS RECEIVER";
    elements.detectorDetail.textContent = receiverFound
      ? "RECEIVER FOUND · CHECK RADIO SETTINGS"
      : "NO PHYSICAL RF STREAM";
    return;
  }
  if (status.state === "stopped") {
    elements.detectorState.textContent = "PAUSED";
    elements.detectorDetail.textContent = "METEOR WATCH IS STOPPED";
    return;
  }
  if (!frame) {
    elements.detectorState.textContent = "PREPARING";
    elements.detectorDetail.textContent = "WAITING FOR RF FRAMES";
    return;
  }
  if (frame.archived) {
    elements.detectorState.textContent = "CANDIDATE SAVED";
    elements.detectorDetail.textContent = "RAW I/Q + METADATA · NO IMAGE";
  } else if (frame.detector_active) {
    elements.detectorState.textContent = "ECHO IN PROGRESS";
    elements.detectorDetail.textContent = "WAITING FOR THE SIGNAL TO DECAY";
  } else if (frame.detector_ready && frame.buffer_seconds >= 29.9) {
    elements.detectorState.textContent = "ARMED";
    elements.detectorDetail.textContent = "WATCHING FOR A RISE AND DECAY";
  } else if (!frame.detector_ready) {
    const readiness = Math.round((Number(frame.detector_readiness) || 0) * 100);
    elements.detectorState.textContent = "CALIBRATING";
    elements.detectorDetail.textContent = `LEARNING BACKGROUND · ${readiness}%`;
  } else {
    elements.detectorState.textContent = "FILLING MEMORY";
    elements.detectorDetail.textContent = `${Math.max(0, 30 - frame.buffer_seconds).toFixed(1)} SECONDS UNTIL ARMED`;
  }
}

function applyStatus(status) {
  currentStatus = status;
  monitorState = status.state;
  const settings = status.settings || {};
  radioCheckActive = settings.observation_mode === "radio_check";
  const live = status.state === "running" || status.state === "starting";
  applyReceiverStatus(status);
  updateDetectorStatus(status);

  elements.bufferValue.innerHTML = `${Number(status.buffer_seconds || 0).toFixed(1)} <small>s / 30 s</small>`;
  elements.bufferMeter.style.width = `${Math.min(100, Number(status.buffer_seconds || 0) / 30 * 100)}%`;
  elements.candidateCount.textContent = `${status.candidate_count || 0} candidate${status.candidate_count === 1 ? "" : "s"}`;

  if (Number.isFinite(settings.detection_frequency_hz)) {
    const target = settings.detection_frequency_hz;
    elements.meteorFrequency.value = (target / 1e6).toFixed(1);
    elements.detection.value = mhz(target);
    elements.headerTarget.textContent = mhz(target);
  }
  if (!radioCheckActive) {
    elements.sampleRate.value = String(settings.sample_rate_hz || METEOR_SPAN_HZ);
    setSensitivity(settings.threshold_db || 8);
  }

  elements.modeOverlay.hidden = !radioCheckActive;
  elements.canvasEmpty.hidden = live || radioCheckActive;
  elements.calibrate.disabled = !live || radioCheckActive;
  elements.watch.disabled = radioCheckActive || status.state === "starting";
  elements.watch.textContent = status.state === "running" ? "Pause meteor watch" : "Start meteor watch";
  elements.retry.hidden = status.state !== "error";
  updateAudioUi();

  if (!live && audioEnabled) stopAudio();
  if (status.error) showMessage(status.error, true);
}

function ensureCanvasSize(canvas) {
  const width = Math.max(1, Math.round(Math.min(canvas.clientWidth, 1280)));
  const height = Math.max(1, Math.round(canvas.clientHeight));
  if (canvas.width === width && canvas.height === height) return false;
  canvas.width = width;
  canvas.height = height;
  return true;
}

const heatLut = Array.from({ length: 256 }, (_, index) => {
  const t = index / 255;
  const stops = [[0, 0, 0], [1, 12, 34], [0, 47, 126], [0, 143, 218], [73, 215, 255], [224, 250, 255]];
  const scaled = t * (stops.length - 1);
  const lower = Math.min(stops.length - 2, Math.floor(scaled));
  const mix = scaled - lower;
  return stops[lower].map((value, channel) => Math.round(value + (stops[lower + 1][channel] - value) * mix));
});

function drawWaterfall(frame) {
  if (radioCheckActive) return;
  const resized = ensureCanvasSize(elements.waterfall);
  const canvas = elements.waterfall;
  const context = canvas.getContext("2d", { alpha: false });
  if (resized || !waterfallStarted) {
    context.fillStyle = "#020507";
    context.fillRect(0, 0, canvas.width, canvas.height);
    waterfallStarted = true;
    waterfallRow = context.createImageData(canvas.width, 1);
  } else {
    context.drawImage(canvas, 0, 0, canvas.width, canvas.height - 1, 0, 1, canvas.width, canvas.height - 1);
  }
  if (!waterfallRow || waterfallRow.width !== canvas.width) waterfallRow = context.createImageData(canvas.width, 1);
  const values = frame.spectrum_db;
  for (let x = 0; x < canvas.width; x += 1) {
    const position = x / Math.max(1, canvas.width - 1) * (values.length - 1);
    const lower = Math.floor(position);
    const upper = Math.min(values.length - 1, lower + 1);
    const mix = position - lower;
    const db = values[lower] + (values[upper] - values[lower]) * mix;
    const color = heatLut[Math.max(0, Math.min(255, Math.round((db + 95) / 75 * 255)))];
    const offset = x * 4;
    waterfallRow.data[offset] = color[0];
    waterfallRow.data[offset + 1] = color[1];
    waterfallRow.data[offset + 2] = color[2];
    waterfallRow.data[offset + 3] = 255;
  }
  context.putImageData(waterfallRow, 0, 0);
}

function drawPowerChart() {
  ensureCanvasSize(elements.power);
  const canvas = elements.power;
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const plotLeft = width < 360 ? 36 : 42;
  const plotRight = width - 1;
  const minimum = -5;
  const rawMaximum = powerHistory.length
    ? Math.max(
      15,
      ...powerHistory.map((item) => item.threshold + 5),
      ...powerHistory.map((item) => item.excess + 3),
      ...powerHistory.map((item) => item.reference + 3),
    )
    : 15;
  const tickStep = rawMaximum <= 30 ? 5 : rawMaximum <= 60 ? 10 : 20;
  const maximum = Math.max(15, Math.ceil(rawMaximum / tickStep) * tickStep);
  const yFor = (value) => height - (Math.max(minimum, Math.min(maximum, value)) - minimum) / (maximum - minimum) * height;

  context.fillStyle = "#020507";
  context.fillRect(0, 0, width, height);
  context.lineWidth = 1;
  context.font = "8px ui-monospace, SFMono-Regular, Menlo, monospace";
  context.textAlign = "right";
  context.textBaseline = "middle";
  const ticks = [minimum, 0];
  for (let value = tickStep; value <= maximum; value += tickStep) ticks.push(value);
  [...new Set(ticks)].sort((a, b) => a - b).forEach((value) => {
    const y = yFor(value);
    context.strokeStyle = "#12242d";
    context.beginPath();
    context.moveTo(plotLeft, y);
    context.lineTo(plotRight, y);
    context.stroke();
    context.fillStyle = value === 0 ? "#8dafbd" : "#617985";
    const labelY = Math.max(6, Math.min(height - 6, y));
    context.fillText(`${value > 0 ? "+" : ""}${value}`, plotLeft - 6, labelY);
  });
  for (let column = 1; column < 6; column += 1) {
    const x = plotLeft + column * (plotRight - plotLeft) / 6;
    context.strokeStyle = "#0d1c23";
    context.beginPath();
    context.moveTo(x, 0);
    context.lineTo(x, height);
    context.stroke();
  }
  if (!powerHistory.length) return;
  const newest = powerHistory[powerHistory.length - 1].time;
  const oldest = newest - POWER_WINDOW_MS;
  const xFor = (time) => Math.max(plotLeft, Math.min(plotRight, plotLeft + (time - oldest) / POWER_WINDOW_MS * (plotRight - plotLeft)));
  const threshold = powerHistory[powerHistory.length - 1].threshold;
  const thresholdY = yFor(threshold);
  context.setLineDash([6, 5]);
  context.strokeStyle = "#d4a94f";
  context.beginPath();
  context.moveTo(0, thresholdY);
  context.lineTo(width, thresholdY);
  context.stroke();
  context.setLineDash([]);
  elements.powerThresholdLabel.style.top = `${Math.max(2, thresholdY - 13)}px`;

  const plot = (field, color, widthValue, dash = []) => {
    context.beginPath();
    powerHistory.forEach((item, index) => {
      const x = xFor(item.time);
      const y = yFor(item[field]);
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    });
    context.strokeStyle = color;
    context.lineWidth = widthValue;
    context.setLineDash(dash);
    context.stroke();
    context.setLineDash([]);
  };
  plot("reference", "rgba(255, 118, 149, .62)", 1, [3, 4]);
  plot("excess", "#38c7ff", 2);
}

function acceptFrame(frame) {
  if (radioCheckActive || frame.observation_mode === "radio_check") {
    elements.modeOverlay.hidden = false;
    return;
  }
  elements.canvasEmpty.hidden = true;
  elements.modeOverlay.hidden = true;
  drawWaterfall(frame);
  const timestamp = Date.parse(frame.timestamp_utc) || Date.now();
  powerHistory.push({
    time: timestamp,
    excess: Number(frame.excess_db) || 0,
    reference: Number(frame.reference_excess_db) || 0,
    threshold: Number(frame.threshold_db) || Number(elements.threshold.value),
  });
  powerHistory = powerHistory.filter((item) => item.time >= timestamp - POWER_WINDOW_MS);
  drawPowerChart();

  elements.bufferValue.innerHTML = `${Number(frame.buffer_seconds).toFixed(1)} <small>s / 30 s</small>`;
  elements.bufferMeter.style.width = `${Math.min(100, frame.buffer_seconds / 30 * 100)}%`;
  elements.excess.textContent = `${frame.excess_db >= 0 ? "+" : ""}${Number(frame.excess_db).toFixed(1)} dB`;
  elements.thresholdValue.textContent = `${Number(frame.threshold_db).toFixed(1)} dB`;
  updateDetectorStatus(currentStatus || { state: monitorState, settings: { observation_mode: "meteor" } }, frame);

  const center = (frame.lower_edge_hz + frame.upper_edge_hz) / 2;
  setMarkerPositions(frame.lower_edge_hz, frame.upper_edge_hz, frame.detection_frequency_hz, center);
  elements.headerTarget.textContent = mhz(frame.detection_frequency_hz);
  elements.canvasSummary.textContent = frame.detector_active
    ? `Candidate in progress at ${frequency(frame.detection_frequency_hz)} · target ${frame.excess_db >= 0 ? "+" : ""}${Number(frame.excess_db).toFixed(1)} dB · surroundings ${Number(frame.reference_excess_db).toFixed(1)} dB · trigger ${Number(frame.threshold_db).toFixed(1)} dB.`
    : `Watching ${frequency(frame.detection_frequency_hz)} · target ${frame.excess_db >= 0 ? "+" : ""}${Number(frame.excess_db).toFixed(1)} dB · surroundings ${Number(frame.reference_excess_db).toFixed(1)} dB · trigger ${Number(frame.threshold_db).toFixed(1)} dB.`;

  if (elements.retuneOverlay.hidden === false) elements.retuneOverlay.hidden = true;
  if (frame.archived) {
    showMessage("Unconfirmed candidate queued as raw I/Q and JSON metadata. No image snapshot was created.");
  }
}

function animationLoop() {
  if (pendingFrame && pendingFrame.sequence !== renderedSequence) {
    renderedSequence = pendingFrame.sequence;
    acceptFrame(pendingFrame);
  }
  requestAnimationFrame(animationLoop);
}

async function calibrateMeteorTarget(message = "Meteor target applied") {
  if (radioCheckActive) return;
  const payload = meteorPayload();
  if (payload.detection_frequency_hz < FM_LOW_HZ || payload.detection_frequency_hz > FM_HIGH_HZ) {
    showMessage("Choose a target inside the 87.5–108 MHz FM band.", true);
    return;
  }
  clearMessage();
  elements.retuneFrequency.textContent = frequency(payload.detection_frequency_hz);
  elements.retuneOverlay.hidden = false;
  try {
    const endpoint = monitorState === "running" || monitorState === "starting" ? "/api/live/tune" : "/api/live/start";
    const status = await postSettings(endpoint, payload);
    applyStatus(status);
    resetVisuals();
    showMessage(`${message}. Background learning and the 30-second evidence memory restarted.`);
  } catch (error) {
    elements.retuneOverlay.hidden = true;
    showMessage(error.message, true);
  }
}

async function toggleMeteorWatch() {
  clearMessage();
  try {
    if (monitorState === "running" || monitorState === "starting") {
      if (audioEnabled) await stopAudio();
      applyStatus(await postSettings("/api/live/stop", {}));
      showMessage("Meteor watch paused. The unsaved RAM memory was cleared.");
    } else {
      applyStatus(await postSettings("/api/live/start", meteorPayload()));
      resetVisuals();
      showMessage("Meteor watch starting. The detector will calibrate before arming.");
    }
  } catch (error) {
    showMessage(error.message, true);
  }
}

async function retryHardware(status) {
  if (status.source !== "hardware" || status.state !== "error" || status.receiver?.detected || hardwareRetryBusy || Date.now() - lastHardwareRetry < 4000) return;
  hardwareRetryBusy = true;
  lastHardwareRetry = Date.now();
  try {
    applyStatus(await postSettings("/api/live/start", meteorPayload()));
  } catch (error) {
    // The visible receiver state remains authoritative while USB is unavailable.
  } finally {
    hardwareRetryBusy = false;
  }
}

async function pollStatus() {
  try {
    const status = await request("/api/live/status");
    applyStatus(status);
    retryHardware(status);
  } catch (error) {
    elements.connection.classList.add("error");
    elements.connectionLabel.textContent = "DASHBOARD OFFLINE";
  }
}

function schedulePcm(bytes, sampleRate) {
  if (!audioEnabled || !audioContext || !audioGainNode || bytes.byteLength < 2) return;
  const sampleCount = Math.floor(bytes.byteLength / 2);
  const buffer = audioContext.createBuffer(1, sampleCount, sampleRate);
  const channel = buffer.getChannelData(0);
  const view = new DataView(bytes.buffer, bytes.byteOffset, sampleCount * 2);
  for (let index = 0; index < sampleCount; index += 1) channel[index] = view.getInt16(index * 2, true) / 32768;
  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(audioGainNode);
  if (audioNextTime <= audioContext.currentTime + .005) audioNextTime = audioContext.currentTime + .1;
  source.start(audioNextTime);
  audioNextTime += buffer.duration;
}

async function streamAudio(signal) {
  try {
    const response = await fetch("/api/live/audio", { signal, cache: "no-store" });
    if (!response.ok) {
      let detail = "Receiver audio unavailable";
      try { detail = (await response.json()).error || detail; } catch (error) { /* no JSON body */ }
      throw new Error(detail);
    }
    const sampleRate = Number(response.headers.get("X-Audio-Sample-Rate")) || 32000;
    const reader = response.body.getReader();
    let chunks = [];
    let queuedBytes = 0;
    while (audioEnabled && !signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value?.byteLength) { chunks.push(value); queuedBytes += value.byteLength; }
      if (queuedBytes < 8192) continue;
      const combined = new Uint8Array(queuedBytes);
      let offset = 0;
      chunks.forEach((chunk) => { combined.set(chunk, offset); offset += chunk.byteLength; });
      const usable = combined.byteLength - combined.byteLength % 2;
      schedulePcm(combined.subarray(0, usable), sampleRate);
      chunks = usable < combined.byteLength ? [combined.subarray(usable)] : [];
      queuedBytes = combined.byteLength - usable;
    }
    if (audioEnabled && !signal.aborted) throw new Error("Receiver audio stream ended");
  } catch (error) {
    if (error.name !== "AbortError") {
      const detail = error.message || "Receiver audio stopped";
      const shouldRestore = radioCheckActive && monitorState === "running";
      if (shouldRestore) {
        audioTransitionBusy = true;
        updateAudioUi();
      }
      await stopAudio();
      if (shouldRestore) {
        try {
          const restore = meteorSettingsBeforeAudio || meteorPayload();
          applyStatus(await postSettings("/api/live/tune", restore));
          radioCheckActive = false;
          meteorSettingsBeforeAudio = null;
          resetVisuals();
          showMessage(`${detail}. Meteor Watch was restored safely.`, true);
        } catch (restoreError) {
          showMessage(
            `${detail}. Meteor Watch could not be restored: ${restoreError.message}`,
            true,
          );
        } finally {
          audioTransitionBusy = false;
          updateAudioUi();
        }
      } else {
        audioTransitionBusy = false;
        updateAudioUi();
        showMessage(detail, true);
      }
    }
  }
}

async function prepareAudio() {
  const AudioApi = window.AudioContext || window.webkitAudioContext;
  if (!AudioApi) throw new Error("This browser does not support live receiver audio");
  audioContext = new AudioApi();
  await audioContext.resume();
  audioGainNode = audioContext.createGain();
  audioGainNode.gain.value = .4;
  audioGainNode.connect(audioContext.destination);
  audioNextTime = 0;
}

function beginAudio() {
  audioAbort = new AbortController();
  audioEnabled = true;
  updateAudioUi();
  streamAudio(audioAbort.signal);
}

async function stopAudio() {
  audioEnabled = false;
  if (audioAbort) audioAbort.abort();
  audioAbort = null;
  const closing = audioContext;
  audioContext = null;
  audioGainNode = null;
  audioNextTime = 0;
  updateAudioUi();
  if (closing && closing.state !== "closed") {
    try { await closing.close(); } catch (error) { /* already closed */ }
  }
}

function updateAudioUi() {
  const activeTarget = Number(currentStatus?.settings?.detection_frequency_hz);
  const targetHz = radioCheckActive && Number.isFinite(activeTarget)
    ? activeTarget
    : meteorTargetHz();
  const targetLabel = frequency(targetHz);
  const playing = radioCheckActive && audioEnabled;
  const live = monitorState === "running";
  elements.audioButton.classList.toggle("active", radioCheckActive);
  elements.audioButton.classList.toggle("playing", playing);
  elements.audioButton.classList.toggle("busy", audioTransitionBusy);
  elements.audioButton.disabled = !live || audioTransitionBusy;
  elements.audioButton.setAttribute("aria-pressed", String(radioCheckActive));
  elements.audioButton.setAttribute(
    "aria-label",
    audioTransitionBusy
      ? "Changing receiver audio mode"
      : radioCheckActive
      ? `Stop listening to ${targetLabel} and restore meteor watch`
      : `Listen to the selected ${targetLabel} FM target`,
  );
  elements.audioButton.title = audioTransitionBusy
    ? "Changing receiver mode"
    : radioCheckActive
    ? `Stop ${targetLabel} audio and restore meteor watch`
    : `Listen to ${targetLabel} as wide FM using a 250 kHz RF span`;
  elements.audioButton.dataset.tooltip = audioTransitionBusy
    ? "TUNING RECEIVER…"
    : radioCheckActive
    ? `${playing ? "LISTENING" : "AUDIO MODE"} · ${targetLabel} · CLICK TO RESTORE WATCH`
    : `LISTEN · ${targetLabel} · WFM 250 kHz`;
  elements.audioNote.textContent = audioTransitionBusy
    ? "Changing receiver mode."
    : radioCheckActive
    ? `${targetLabel} AUDIO ${playing ? "PLAYING" : "ACTIVE"}. Meteor detection and candidate saving are paused.`
    : `Audio monitor off. Meteor watch is active. The speaker uses a 250 kHz wide-FM span around ${targetLabel}.`;
}

async function toggleRadioCheck() {
  clearMessage();
  if (radioCheckActive) {
    audioTransitionBusy = true;
    updateAudioUi();
    try {
      await stopAudio();
      const restore = meteorSettingsBeforeAudio || meteorPayload();
      applyStatus(await postSettings("/api/live/tune", restore));
      radioCheckActive = false;
      meteorSettingsBeforeAudio = null;
      resetVisuals();
      updateAudioUi();
      showMessage("Meteor target restored. Background learning and evidence memory restarted.");
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      audioTransitionBusy = false;
      updateAudioUi();
    }
    return;
  }
  if (audioTransitionBusy) return;
  audioTransitionBusy = true;
  updateAudioUi();
  try {
    const payload = radioPayload();
    const targetLabel = frequency(payload.detection_frequency_hz);
    meteorSettingsBeforeAudio = meteorSettingsFromStatus(currentStatus) || meteorPayload();
    await prepareAudio();
    applyStatus(await postSettings("/api/live/tune", payload));
    radioCheckActive = true;
    beginAudio();
    showMessage(`Listening to the monitored ${targetLabel} FM target with a 250 kHz RF span. Meteor detection and candidate saving are paused; continuous clear programming means this is not a quiet meteor target.`);
  } catch (error) {
    const detail = error.message || "Receiver audio could not start";
    await stopAudio();
    let restored = false;
    try {
      const status = await request("/api/live/status");
      applyStatus(status);
      if (
        status.settings?.observation_mode === "radio_check"
        && meteorSettingsBeforeAudio
      ) {
        applyStatus(
          await postSettings("/api/live/tune", meteorSettingsBeforeAudio),
        );
        resetVisuals();
        restored = true;
      } else if (status.settings?.observation_mode === "meteor") {
        restored = true;
      }
    } catch (statusError) {
      if (meteorSettingsBeforeAudio) {
        try {
          applyStatus(
            await postSettings("/api/live/tune", meteorSettingsBeforeAudio),
          );
          resetVisuals();
          restored = true;
        } catch (restoreError) {
          showMessage(
            `${detail}. Meteor Watch recovery also failed: ${restoreError.message}`,
            true,
          );
        }
      }
    }
    if (restored) {
      radioCheckActive = false;
      meteorSettingsBeforeAudio = null;
      showMessage(`${detail}. Meteor Watch was restored safely.`, true);
    } else if (!elements.message.classList.contains("error")) {
      showMessage(detail, true);
    }
    updateAudioUi();
  } finally {
    audioTransitionBusy = false;
    updateAudioUi();
  }
}

function closeTooltips(except = null) {
  elements.tooltips.forEach((trigger) => {
    const tooltip = document.getElementById(trigger.getAttribute("aria-describedby"));
    if (trigger !== except) {
      trigger.setAttribute("aria-expanded", "false");
      tooltip?.classList.remove("visible");
    }
  });
}

function closeVisualInfoPanels(except = null) {
  elements.visualInfoButtons.forEach((button) => {
    if (button === except) return;
    const panel = document.getElementById(button.getAttribute("aria-controls"));
    button.setAttribute("aria-expanded", "false");
    if (panel) panel.hidden = true;
  });
}

elements.tooltips.forEach((trigger) => {
  trigger.setAttribute("aria-expanded", "false");
  trigger.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const tooltip = document.getElementById(trigger.getAttribute("aria-describedby"));
    const open = !tooltip?.classList.contains("visible");
    closeTooltips(trigger);
    tooltip?.classList.toggle("visible", open);
    trigger.setAttribute("aria-expanded", String(open));
  });
});
elements.visualInfoButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const panel = document.getElementById(button.getAttribute("aria-controls"));
    if (!panel) return;
    const opening = panel.hidden;
    closeVisualInfoPanels(button);
    panel.hidden = !opening;
    button.setAttribute("aria-expanded", String(opening));
  });
});
document.addEventListener("click", () => closeTooltips());
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  closeTooltips();
  closeVisualInfoPanels();
});

elements.calibrate.addEventListener("click", () => calibrateMeteorTarget());
elements.watch.addEventListener("click", toggleMeteorWatch);
elements.retry.addEventListener("click", () => calibrateMeteorTarget("Physical receiver retry started"));
elements.audioButton.addEventListener("click", toggleRadioCheck);
elements.meteorFrequency.addEventListener("input", updateAudioUi);
elements.sensitivityButtons.forEach((button) => {
  button.addEventListener("click", () => {
    setSensitivity(Number(button.dataset.threshold));
    if (monitorState === "running" && !radioCheckActive) calibrateMeteorTarget(`${button.textContent} meteor sensitivity selected`);
  });
});

const resizeObserver = new ResizeObserver(() => {
  if (ensureCanvasSize(elements.waterfall)) { waterfallStarted = false; waterfallRow = null; }
  drawPowerChart();
});
resizeObserver.observe(elements.waterfallWrap);
resizeObserver.observe(elements.power.parentElement);

const stream = new EventSource("/api/live/events");
stream.addEventListener("frame", (event) => { pendingFrame = JSON.parse(event.data); });
stream.onerror = () => {
  if (monitorState === "running") {
    elements.connection.classList.add("error");
    elements.connectionLabel.textContent = "RF STREAM RECONNECTING";
  }
};

window.addEventListener("pagehide", () => {
  if (
    radioCheckActive
    && monitorState === "running"
    && meteorSettingsBeforeAudio
  ) {
    fetch("/api/live/tune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(meteorSettingsBeforeAudio),
      keepalive: true,
    }).catch(() => {});
  }
});

setSensitivity(8);
updateAudioUi();
ensureCanvasSize(elements.power);
ensureCanvasSize(elements.waterfall);
drawPowerChart();
pollStatus();
requestAnimationFrame(animationLoop);
window.setInterval(pollStatus, 2000);
