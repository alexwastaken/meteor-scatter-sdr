const elements = {
  form: document.querySelector("#capture-form"),
  frequency: document.querySelector("#frequency"),
  sampleRate: document.querySelector("#sample-rate"),
  duration: document.querySelector("#duration"),
  gain: document.querySelector("#gain"),
  autoDownload: document.querySelector("#auto-download"),
  sizeEstimate: document.querySelector("#size-estimate"),
  frequencySpan: document.querySelector("#frequency-span"),
  startButton: document.querySelector("#start-button"),
  startButtonLabel: document.querySelector("#start-button-label"),
  progress: document.querySelector("#capture-progress"),
  progressDetail: document.querySelector("#progress-detail"),
  progressPercent: document.querySelector("#progress-percent"),
  progressBar: document.querySelector("#progress-bar"),
  message: document.querySelector("#message"),
  captureList: document.querySelector("#capture-list"),
  refreshButton: document.querySelector("#refresh-button"),
  connectionPill: document.querySelector("#connection-pill"),
  connectionLabel: document.querySelector("#connection-label"),
};

let activeJobId = null;
let downloadedJobId = null;

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  if (bytes < 1_000_000) return `${(bytes / 1_000).toFixed(0)} KB`;
  if (bytes < 1_000_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  return `${(bytes / 1_000_000_000).toFixed(2)} GB`;
}

function formatDate(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function updateEstimate() {
  const frequencyHz = Number(elements.frequency.value) * 1_000_000;
  const sampleRate = Number(elements.sampleRate.value);
  const duration = Number(elements.duration.value);
  elements.sizeEstimate.textContent = formatBytes(sampleRate * duration * 2);
  const low = (frequencyHz - sampleRate / 2) / 1_000_000;
  const high = (frequencyHz + sampleRate / 2) / 1_000_000;
  elements.frequencySpan.textContent = `Listening across approximately ${low.toFixed(3)}–${high.toFixed(3)} MHz`;
}

function setConnection(state, label) {
  elements.connectionPill.classList.toggle("connected", state === "connected");
  elements.connectionPill.classList.toggle("error", state === "error");
  elements.connectionLabel.textContent = label;
}

function showMessage(text, isError = false) {
  elements.message.hidden = false;
  elements.message.textContent = text;
  elements.message.classList.toggle("error", isError);
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

function renderJob(job) {
  if (!job) {
    elements.progress.hidden = true;
    elements.startButton.disabled = false;
    elements.startButtonLabel.textContent = "Start collection";
    return;
  }
  if (job.state === "capturing") {
    activeJobId = job.id;
    const percent = Math.round(job.progress * 100);
    elements.progress.hidden = false;
    elements.progressPercent.textContent = `${percent}%`;
    elements.progressBar.style.width = `${percent}%`;
    elements.progressDetail.textContent = `${Math.min(job.elapsed_seconds, job.duration_seconds).toFixed(1)} of ${job.duration_seconds} seconds`;
    elements.startButton.disabled = true;
    elements.startButtonLabel.textContent = "Collection in progress";
    return;
  }

  elements.progress.hidden = true;
  elements.startButton.disabled = false;
  elements.startButtonLabel.textContent = "Start another collection";
  if (job.state === "error") {
    showMessage(job.error || "Capture failed. Check the terminal for more detail.", true);
    return;
  }
  if (job.state === "complete" && activeJobId === job.id) {
    activeJobId = null;
    showMessage(`Collection complete. ${formatBytes(job.result.byte_count)} saved on this Pi.`);
    loadCaptures();
    if (elements.autoDownload.checked && downloadedJobId !== job.id) {
      downloadedJobId = job.id;
      window.location.assign(`/api/captures/${encodeURIComponent(job.result.data_file)}/download`);
    }
  }
}

async function pollStatus() {
  try {
    const payload = await request("/api/status");
    setConnection("connected", payload.active?.state === "capturing" ? "Recording" : "Receiver ready");
    renderJob(payload.active);
  } catch (error) {
    setConnection("error", "Dashboard offline");
  }
}

function captureItem(capture) {
  const row = document.createElement("article");
  row.className = "capture-item";

  const primary = document.createElement("div");
  primary.className = "capture-primary";
  const filename = document.createElement("strong");
  filename.textContent = capture.data_file;
  const timestamp = document.createElement("small");
  timestamp.textContent = formatDate(capture.started_at_utc);
  primary.append(filename, timestamp);

  const frequency = stat(`${(capture.center_frequency_hz / 1_000_000).toFixed(3)} MHz`, "Center");
  const duration = stat(`${capture.duration_seconds ?? "—"} sec`, "Duration");
  const size = stat(formatBytes(capture.byte_count), "Raw size");

  const download = document.createElement("a");
  download.className = "download-button";
  download.href = capture.download_url;
  download.textContent = "Download ZIP";
  download.setAttribute("download", "");
  row.append(primary, frequency, duration, size, download);
  return row;
}

function stat(value, label) {
  const container = document.createElement("div");
  container.className = "capture-stat";
  const strong = document.createElement("strong");
  strong.textContent = value;
  const small = document.createElement("small");
  small.textContent = label;
  container.append(strong, small);
  return container;
}

async function loadCaptures() {
  try {
    const payload = await request("/api/captures");
    elements.captureList.replaceChildren();
    if (!payload.captures.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.innerHTML = '<span class="empty-icon" aria-hidden="true"></span><strong>No captures yet</strong><p>Your first RF collection will appear here.</p>';
      elements.captureList.append(empty);
      return;
    }
    payload.captures.forEach((capture) => elements.captureList.append(captureItem(capture)));
  } catch (error) {
    showMessage("Could not load saved captures.", true);
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearMessage();
  elements.startButton.disabled = true;
  elements.startButtonLabel.textContent = "Starting receiver…";
  const gain = elements.gain.value === "auto" ? "auto" : Number(elements.gain.value);
  const payload = {
    center_frequency_hz: Math.round(Number(elements.frequency.value) * 1_000_000),
    sample_rate_hz: Number(elements.sampleRate.value),
    duration_seconds: Number(elements.duration.value),
    gain_db: gain,
  };
  try {
    const response = await request("/api/captures", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    activeJobId = response.job.id;
    downloadedJobId = null;
    renderJob(response.job);
  } catch (error) {
    elements.startButton.disabled = false;
    elements.startButtonLabel.textContent = "Start collection";
    showMessage(error.message, true);
  }
});

elements.frequency.addEventListener("input", updateEstimate);
elements.sampleRate.addEventListener("change", updateEstimate);
elements.duration.addEventListener("change", updateEstimate);
elements.refreshButton.addEventListener("click", loadCaptures);

updateEstimate();
pollStatus();
loadCaptures();
window.setInterval(pollStatus, 600);
