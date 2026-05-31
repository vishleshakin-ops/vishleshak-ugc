const $ = (id) => document.getElementById(id);

// Set when app initialises — used by renderOrderCard to build public customer links
let _railwayUrl = "";
let _clientPackages = [];

const state = {
  productFile: null,
  productPreviewUrl: "",
  jobId: null,
  pollTimer: null,
  presenterSource: "uploaded",
  autoMode: true,
  selectedGender: "female",
  selectedSkinTone: "wheatish",
  selectedScene: "studio",
  selectedLanguage: "hindi",
  selectedRatio: "9:16",
  outputType: "video",
  videoDuration: "5",
  videoQuality: "high",
  generatedScript: "",
  generatedAvatarPrompt: "",
  generatedProductType: "",
};

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

const views = {
  compose: $("compose-view"),
  script: $("script-view"),
  progress: $("progress-view"),
  result: $("result-view"),
  error: $("error-view"),
};

const stepOrder = [
  "analyzing",
  "compositing_product",
  "generating_audio",
  "generating_video",
  "processing_video",
];

const stepLabels = {
  analyzing: "Analyzing product and writing script",
  compositing_product: "Creating presenter image with AI (2-5 min)…",
  generating_audio: "Generating voiceover",
  generating_video: "Generating lip-sync video",
  processing_video: "Exporting final MP4",
  completed: "Done",
};

function showView(name) {
  Object.values(views).forEach((view) => view.classList.add("hidden"));
  views[name].classList.remove("hidden");
}

function setProductFile(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    showError("Please upload an image file.");
    return;
  }
  if (file.size > 15 * 1024 * 1024) {
    showError("Product image must be under 15 MB.");
    return;
  }

  state.productFile = file;
  if (state.productPreviewUrl) URL.revokeObjectURL(state.productPreviewUrl);
  state.productPreviewUrl = URL.createObjectURL(file);

  $("product-preview").src = state.productPreviewUrl;
  $("product-preview").classList.remove("hidden");
  $("upload-placeholder").classList.add("hidden");
  $("change-product-btn").classList.remove("hidden");
  $("generate-btn").disabled = false;
  $("ready-note").textContent = "Ready to generate. Use Auto for the most stable first run.";
  showView("compose");
}

function resetProduct() {
  state.productFile = null;
  $("file-input").value = "";
  $("product-preview").removeAttribute("src");
  $("product-preview").classList.add("hidden");
  $("upload-placeholder").classList.remove("hidden");
  $("change-product-btn").classList.add("hidden");
  $("generate-btn").disabled = true;
  $("ready-note").textContent = "Upload a product image to begin.";
}

function setMode(mode) {
  state.autoMode = mode === "auto";
  $("mode-auto").classList.toggle("selected", state.autoMode);
  $("mode-manual").classList.toggle("selected", !state.autoMode);
  $("auto-panel").classList.toggle("hidden", !state.autoMode);
  $("manual-panel").classList.toggle("hidden", state.autoMode);
}

function setOutputType(type) {
  state.outputType = type;
  document.querySelectorAll("[data-output]").forEach((b) =>
    b.classList.toggle("selected", b.dataset.output === type)
  );
  const isImage = type === "image";
  $("language-field").classList.toggle("hidden", isImage);
  $("video-options-field").classList.toggle("hidden", isImage);
  $("tool-hint").classList.toggle("hidden", isImage);
  $("generate-btn").textContent = isImage ? "Generate Script & Image" : "Generate Script";
  $("confirm-generate-btn").textContent = isImage ? "Generate Image →" : "Generate Video →";
  if (isImage) {
    const el = $("cost-estimate");
    if (el) el.textContent = "Image mode: ~2-3 min | ~₹8 per image | No video generation";
  } else {
    loadPipelineInfo();
    updateToolHint();
  }
}

function setVideoDuration(dur) {
  state.videoDuration = dur;
  document.querySelectorAll("[data-duration]").forEach((b) =>
    b.classList.toggle("selected", b.dataset.duration === dur)
  );
  updateToolHint();
}

function setVideoQuality(q) {
  state.videoQuality = q;
  document.querySelectorAll("[data-quality]").forEach((b) =>
    b.classList.toggle("selected", b.dataset.quality === q)
  );
  updateToolHint();
}

function updateToolHint() {
  const el = $("tool-hint-text");
  if (!el) return;
  const dur = state.videoDuration;
  const q   = state.videoQuality;
  const durMap = { "5": "Short, 4-6 sec", "10": "Standard, 8-10 sec" };
  const durLabel = durMap[dur] || "Short, 4-6 sec";
  const qLabel = q === "ultra" ? "premium polish" : q === "standard" ? "standard polish" : "high polish";
  el.innerHTML = `AI will prepare a <strong>${durLabel} · ${qLabel}</strong> product-safe creative`;
}

function setPresenterSource(source) {
  state.presenterSource = source;
  document.querySelectorAll("[data-presenter-source]").forEach((button) => {
    button.classList.toggle("selected", button.dataset.presenterSource === source);
  });

  const usingAi = source === "ai";
  const productOnly = source === "product";
  $("model-upload-btn").disabled = usingAi || productOnly;
  $("model-upload-btn").textContent = usingAi ? "Auto" : productOnly ? "No model" : "Change";
  $("model-preview").classList.toggle("hidden", usingAi || productOnly);
  $("model-empty").classList.toggle("hidden", !(usingAi || productOnly));

  if (usingAi) {
    $("model-empty").textContent = "AI will create";
    $("model-status-text").textContent = "AI presenter selected";
    $("model-status-sub").textContent = "The app will generate a male or female presenter automatically from the product and settings.";
  } else if (productOnly) {
    $("model-empty").textContent = "Product only";
    $("model-status-text").textContent = "No presenter selected";
    $("model-status-sub").textContent = "The app will create a product-first creative without a human presenter.";
  } else {
    $("model-empty").textContent = "No photo";
    loadModelStatus();
  }
}

function setSelected(buttons, activeButton, className) {
  buttons.forEach((button) => button.classList.remove(className));
  activeButton.classList.add(className);
}

async function loadModelStatus() {
  try {
    const response = await fetch("/api/model-status");
    const data = await response.json();
    updateModelStatus(data.configured, data.image_url);
  } catch (error) {
    updateModelStatus(false, "");
    $("model-status-text").textContent = "Server not reachable";
    $("model-status-sub").textContent = "Start FastAPI and reload this page.";
  }
}

function updateModelStatus(configured, imageUrl) {
  const img = $("model-preview");
  const empty = $("model-empty");
  if (configured) {
    img.src = imageUrl || "/api/model-image";
    img.classList.remove("hidden");
    empty.classList.add("hidden");
    $("model-status-text").textContent = "Model photo ready";
    $("model-status-sub").textContent = "Using your local presenter photo for generation.";
  } else {
    img.removeAttribute("src");
    img.classList.add("hidden");
    empty.classList.remove("hidden");
    $("model-status-text").textContent = "No model photo set";
    $("model-status-sub").textContent = "Upload the presenter photo before generating a video.";
  }
}

async function uploadModel(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    showError("Presenter photo must be an image file.");
    return;
  }

  $("model-uploading").classList.remove("hidden");
  $("model-upload-btn").disabled = true;
  const formData = new FormData();
  formData.append("image", file);

  try {
    const response = await fetch("/api/upload-model", { method: "POST", body: formData });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "Model upload failed.");
    updateModelStatus(true, data.image_url || "/api/model-image");
    showToast("✅ Model photo uploaded! Ready to generate videos.", 4000);
  } catch (error) {
    showError(error.message || "Model upload failed.");
  } finally {
    $("model-uploading").classList.add("hidden");
    $("model-upload-btn").disabled = false;
    $("model-file-input").value = "";
  }
}

function buildCustomizationForm(includeScript = false) {
  const formData = new FormData();
  formData.append("image", state.productFile);
  formData.append("presenter_source", state.presenterSource);
  formData.append("output_type", state.outputType);
  formData.append("video_duration", state.videoDuration);
  formData.append("video_quality", state.videoQuality);
  formData.append("auto_mode", state.autoMode ? "true" : "false");
  formData.append("language", state.selectedLanguage);
  formData.append("aspect_ratio", state.selectedRatio);
  formData.append("model_gender", state.selectedGender);

  if (!state.autoMode) {
    formData.append("skin_tone", state.selectedSkinTone);
    formData.append("scene", state.selectedScene);
    formData.append("custom_scene", $("custom-scene-input").value.trim());
    formData.append("model_action", $("model-action-input").value.trim());
    formData.append("custom_instructions", $("additional-notes-input").value.trim());
  }

  if (includeScript && state.generatedScript) {
    formData.append("custom_script", $("script-edit-area").value.trim() || state.generatedScript);
  }

  return formData;
}

// ── Step 1: Generate Script (called when user clicks "Generate Script") ──────

async function generateScriptPreview() {
  if (!state.productFile) return;

  $("generate-btn").disabled = true;
  $("generate-btn").textContent = "Analyzing...";

  try {
    const response = await fetch("/api/generate-script", {
      method: "POST",
      body: buildCustomizationForm(false),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Server error ${response.status}`);

    state.generatedScript = data.script || "";
    state.generatedAvatarPrompt = data.avatar_prompt || "";
    state.generatedProductType = data.product_type || "other";

    // In auto mode, adopt the AI's gender decision so voice matches the generated model image
    if (state.autoMode && data.ai_settings && data.ai_settings.model_gender) {
      state.selectedGender = data.ai_settings.model_gender;
    }

    $("script-edit-area").value = state.generatedScript;
    $("product-type-badge").textContent = state.generatedProductType;

    // Set confirm button label based on output type
    const isImage = state.outputType === "image";
    $("confirm-generate-btn").textContent = isImage ? "Generate Image →" : "Generate Video →";

    showView("script");
  } catch (error) {
    showError(error.message || "Could not generate script.");
  } finally {
    $("generate-btn").disabled = false;
    const isImage = state.outputType === "image";
    $("generate-btn").textContent = isImage ? "Generate Script & Image" : "Generate Script";
  }
}

// ── Re-generate script from the script preview panel ─────────────────────────

async function regenScript() {
  if (!state.productFile) return;

  const btn = $("regen-script-btn");
  const spinner = $("regen-spinner");
  btn.disabled = true;
  spinner.classList.remove("hidden");

  try {
    const response = await fetch("/api/generate-script", {
      method: "POST",
      body: buildCustomizationForm(false),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Server error ${response.status}`);

    state.generatedScript = data.script || "";
    state.generatedProductType = data.product_type || "other";
    if (state.autoMode && data.ai_settings && data.ai_settings.model_gender) {
      state.selectedGender = data.ai_settings.model_gender;
    }
    $("script-edit-area").value = state.generatedScript;
    $("product-type-badge").textContent = state.generatedProductType;
  } catch (error) {
    alert("Re-generation failed: " + (error.message || "Unknown error"));
  } finally {
    btn.disabled = false;
    spinner.classList.add("hidden");
  }
}

// ── Step 2: Confirm and generate ─────────────────────────────────────────────

async function confirmGenerateVideo() {
  if (!state.productFile) return;

  const isImage = state.outputType === "image";

  resetTimeline();
  showView("progress");
  $("progress-title").textContent = isImage ? "Creating your image" : "Creating your video";
  setTimelineStep("analyzing");

  try {
    const response = await fetch("/api/generate-video", {
      method: "POST",
      body: buildCustomizationForm(true),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Server error ${response.status}`);

    state.jobId = data.job_id;
    $("job-id-label").textContent = `Job ${state.jobId}`;
    $("job-status-label").textContent = "Job accepted. Waiting for first update.";
    state.pollTimer = window.setInterval(pollStatus, 3000);
    await pollStatus();
  } catch (error) {
    showError(error.message || "Could not start generation.");
  }
}

const COMPOSITING_MSGS = [
  "Generating AI model image… this takes 2-5 min",
  "KIE AI is rendering the presenter with your product…",
  "Still working — GPT-4o image generation in progress…",
  "Almost there — compositing model and product…",
  "AI is crafting a photorealistic presenter image…",
  "Hang tight — high quality takes a moment…",
];
let _stepStartTime = null;
let _stepElapsedTimer = null;
let _compositingMsgIdx = 0;

function startStepTimer(step) {
  clearInterval(_stepElapsedTimer);
  _stepStartTime = Date.now();
  _compositingMsgIdx = 0;

  if (step !== "compositing_product") return;

  _stepElapsedTimer = setInterval(() => {
    const elapsed = Math.floor((Date.now() - _stepStartTime) / 1000);
    const mins = Math.floor(elapsed / 60);
    const secs = elapsed % 60;
    const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
    _compositingMsgIdx = (_compositingMsgIdx + 1) % COMPOSITING_MSGS.length;
    $("job-status-label").textContent =
      `${COMPOSITING_MSGS[_compositingMsgIdx]} (${timeStr} elapsed)`;
  }, 4000);
}

function stopStepTimer() {
  clearInterval(_stepElapsedTimer);
  _stepElapsedTimer = null;
  _stepStartTime = null;
}

let _lastStep = null;

async function pollStatus() {
  if (!state.jobId) return;

  try {
    const response = await fetch(`/api/status/${state.jobId}`);
    if (response.status === 404) {
      stopPolling();
      stopStepTimer();
      showError("The server was restarted and lost track of this job. Please click Back and try generating again.");
      return;
    }
    if (!response.ok) throw new Error(`Status check failed: ${response.status}`);
    const job = await response.json();

    if (job.step !== _lastStep) {
      _lastStep = job.step;
      stopStepTimer();
      startStepTimer(job.step);
      $("job-status-label").textContent = stepLabels[job.step] || `Working: ${job.step || "processing"}`;
    }
    setTimelineStep(job.step);

    if (job.status === "completed") {
      stopPolling();
      stopStepTimer();
      setTimelineStep("completed");
      showResult(job);
      loadHistory();
    }

    if (job.status === "failed") {
      stopPolling();
      stopStepTimer();
      showError(job.error || "Generation failed.");
    }
  } catch (error) {
    $("job-status-label").textContent = "Waiting for server status update...";
  }
}

function resetTimeline() {
  document.querySelectorAll(".timeline-step").forEach((step) => {
    step.classList.remove("active", "done");
  });
}

function setTimelineStep(stepName) {
  const activeIndex = stepOrder.indexOf(stepName);
  document.querySelectorAll(".timeline-step").forEach((step) => {
    const index = stepOrder.indexOf(step.dataset.step);
    step.classList.remove("active", "done");
    if (stepName === "completed" || (activeIndex >= 0 && index < activeIndex)) {
      step.classList.add("done");
    } else if (index === activeIndex) {
      step.classList.add("active");
    }
  });
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  _lastStep = null;
}

function showResult(job) {
  const script = job.script || state.generatedScript || "";
  $("script-text").textContent = script;

  const videoEl = $("video-player");
  const imageEl = $("image-result");

  if (job.image_url) {
    // Image-only result
    $("result-heading").textContent = "Your UGC image is ready";
    videoEl.removeAttribute("src");
    videoEl.classList.add("hidden");
    imageEl.src = job.image_url;
    imageEl.classList.remove("hidden");
    $("download-btn").href = job.image_url;
    $("download-btn").download = "vishleshak-ugc.jpg";
    $("download-btn").textContent = "⬇ Download Image";
  } else {
    // Video result
    $("result-heading").textContent = "Your UGC video is ready";
    imageEl.removeAttribute("src");
    imageEl.classList.add("hidden");
    videoEl.src = job.video_url;
    videoEl.classList.remove("hidden");
    $("download-btn").href = job.video_url;
    $("download-btn").download = "vishleshak-ugc.mp4";
    $("download-btn").textContent = "⬇ Download MP4";
  }

  // WhatsApp share
  const waText = encodeURIComponent(script + "\n\nAbhi order karein! 🛍️");
  $("whatsapp-btn").href = `whatsapp://send?text=${waText}`;

  // Copy caption
  $("copy-caption-btn").dataset.script = script;

  showView("result");
}

function showError(message) {
  stopPolling();
  $("error-message").textContent = message;
  showView("error");
}

function resetAll() {
  stopPolling();
  state.jobId = null;
  state.presenterSource = "uploaded";
  state.generatedScript = "";
  state.generatedAvatarPrompt = "";
  state.generatedProductType = "";
  $("video-player").removeAttribute("src");
  $("video-player").classList.remove("hidden");
  $("image-result").removeAttribute("src");
  $("image-result").classList.add("hidden");
  $("download-btn").removeAttribute("href");
  $("download-btn").textContent = "⬇ Download";
  $("script-text").textContent = "";
  $("job-id-label").textContent = "";
  $("job-status-label").textContent = "";
  $("script-edit-area").value = "";
  $("product-type-badge").textContent = "";
  resetProduct();
  resetTimeline();
  setMode("auto");
  setPresenterSource("uploaded");
  // Reset output type to video
  state.outputType = "video";
  document.querySelectorAll("[data-output]").forEach((b) => b.classList.remove("selected"));
  const videoBtn = document.querySelector("[data-output='video']");
  if (videoBtn) videoBtn.classList.add("selected");
  // Reset platform targets to Instagram only
  document.querySelectorAll("[data-platform-target]").forEach((b) => b.classList.remove("selected"));
  const igBtn = document.querySelector("[data-platform-target='instagram']");
  if (igBtn) igBtn.classList.add("selected");
  // Reset language to Hindi
  state.selectedLanguage = "hindi";
  document.querySelectorAll("[data-lang]").forEach((b) => b.classList.remove("selected"));
  document.querySelector("[data-lang='hindi']").classList.add("selected");
  // Reset ratio to 9:16
  state.selectedRatio = "9:16";
  document.querySelectorAll("[data-ratio]").forEach((b) => b.classList.remove("selected"));
  document.querySelector("[data-ratio='9:16']").classList.add("selected");
  $("generate-btn").disabled = true;
  $("generate-btn").textContent = "Generate Script";
  $("confirm-generate-btn").textContent = "Generate Video →";
  showView("compose");
}

// ── Platform target selection (compose view) ─────────────────────────────────

const PLATFORM_URLS = {
  instagram: "https://www.instagram.com/",
  facebook:  "https://www.facebook.com/",
  youtube:   "https://studio.youtube.com/",
  whatsapp:  "https://wa.me/?text=",
};

const PLATFORM_NAMES = {
  instagram: "Instagram",
  facebook:  "Facebook",
  youtube:   "YouTube Studio",
  whatsapp:  "WhatsApp Status",
};

const PLATFORM_ICON_CLASS = {
  instagram: "insta",
  facebook:  "fb",
  youtube:   "yt",
  whatsapp:  "whatsapp",
};

const PLATFORM_FA_ICON = {
  instagram: "fa-instagram",
  facebook:  "fa-facebook",
  youtube:   "fa-youtube",
  whatsapp:  "fa-whatsapp",
};

// Single callback stored when modal opens — called on confirm, cleared on cancel
let _modalCallback = null;

function _openModal(platform, title, message, confirmLabel, onConfirm) {
  const iconClass = PLATFORM_ICON_CLASS[platform];
  const faIcon    = PLATFORM_FA_ICON[platform];

  $("modal-platform-icon").innerHTML =
    `<span class="plat-icon ${iconClass}"><i class="fa-brands ${faIcon}"></i></span>`;
  $("modal-title").textContent       = title;
  $("modal-message").innerHTML       = message;
  $("modal-confirm-btn").textContent = confirmLabel;

  _modalCallback = onConfirm;
  $("share-modal").classList.remove("hidden");
}

function _closeModal() {
  $("share-modal").classList.add("hidden");
  _modalCallback = null;
}

// ── Compose-view platform target buttons ──────────────────────────────────────
function setPlatformTarget(btn) {
  const platform     = btn.dataset.platformTarget;
  const platformName = PLATFORM_NAMES[platform] || platform;
  const ratio        = btn.dataset.platformRatio || "16:9";
  const ratioLabel   = ratio === "9:16" ? "9:16 vertical Reels" : "16:9 horizontal";
  const contentWord  = state.outputType === "image" ? "image" : "video";
  const alreadySelected = btn.classList.contains("selected");

  // First click: silently select
  document.querySelectorAll(".platform-target").forEach((b) => b.classList.remove("selected"));
  btn.classList.add("selected");
  state.selectedRatio = ratio;
  document.querySelectorAll("[data-ratio]").forEach((b) => {
    b.classList.toggle("selected", b.dataset.ratio === ratio);
  });

  // Second click (was already selected): show confirmation popup
  if (alreadySelected) {
    _openModal(
      platform,
      `Target ${platformName}`,
      `Do you wish to create content for <strong>${platformName}</strong>?<br>` +
      `Your ${contentWord} will be optimised in <strong>${ratioLabel}</strong> format.`,
      `Yes, Confirm ${platformName}`,
      () => {}
    );
  }
}

// ── Toast notification ────────────────────────────────────────────────────────
function showToast(msg, duration = 3500) {
  let toast = document.getElementById("toast-msg");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast-msg";
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.className = "toast visible";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toast.classList.remove("visible"), duration);
}

// ── Result-view platform share buttons ────────────────────────────────────────
function shareToPlatform(platform) {
  const platformName = PLATFORM_NAMES[platform] || platform;
  const mediaType    = $("image-result") && !$("image-result").classList.contains("hidden")
    ? "image" : "video";

  _openModal(
    platform,
    `Share to ${platformName}`,
    `<strong>${platformName}</strong> will open in a new tab.<br>` +
    `Your caption will be copied to clipboard — just paste it when uploading your ${mediaType}.<br><br>` +
    `<small style="opacity:0.7">Note: Instagram &amp; Facebook require manual upload from their app or website.</small>`,
    `Yes, Open ${platformName}`,
    () => {
      const script = $("script-text").textContent || "";
      navigator.clipboard.writeText(script).catch(() => {});
      const base = PLATFORM_URLS[platform] || "";
      const url  = platform === "whatsapp"
        ? base + encodeURIComponent(script + "\n\nOrder now!")
        : base;
      if (url) window.open(url, "_blank", "noopener");
      showToast(`${platformName} opened. Caption copied to clipboard!`);
    }
  );
}

// ── Pipeline cost estimate ───────────────────────────────────────────────────

async function loadPipelineInfo() {
  try {
    const resp = await fetch("/api/pipeline-info");
    const data = await resp.json();
    const el = $("cost-estimate");
    if (el && data.estimate_cost) {
      el.textContent = `Estimated cost: ${data.estimate_cost} | Time: ${data.estimate_time} | Quality: ${data.quality}`;
    }
  } catch (_) {
    // silently ignore
  }
}

// ── History mute state (shared across all cards) ─────────────────────────────
let historyMuted = true;

function toggleHistoryMute(btn, event) {
  event.stopPropagation(); // don't bubble to video
  historyMuted = !historyMuted;
  // Apply to every history video
  document.querySelectorAll(".history-video-wrap video").forEach((v) => {
    v.muted = historyMuted;
  });
  // Sync all mute button icons
  document.querySelectorAll(".history-mute-btn").forEach((b) => {
    b.innerHTML = historyMuted ? "🔇" : "🔊";
    b.title = historyMuted ? "Unmute" : "Mute";
  });
}

// ── Video / Image History Gallery ────────────────────────────────────────────

function formatDate(dateStr) {
  if (!dateStr) return "";
  try {
    const d = new Date(dateStr);
    return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
  } catch (_) {
    return dateStr;
  }
}

function buildHistoryCard(entry) {
  const card = document.createElement("div");
  card.className = "history-card";

  const snippet = entry.script
    ? entry.script.length > 80
      ? entry.script.slice(0, 80) + "..."
      : entry.script
    : "No script";

  const badge = entry.product_type
    ? `<span class="product-badge history-badge">${entry.product_type}</span>`
    : "";

  const isImage = entry.output_type === "image" || (!entry.video_url && entry.image_url);
  const mediaUrl = isImage ? entry.image_url : entry.video_url;
  const downloadName = isImage ? "vishleshak-ugc.jpg" : "vishleshak-ugc.mp4";
  const typeTag = isImage
    ? `<span class="product-badge history-badge" style="background:#8b5cf6">📸 image</span>`
    : "";

  const mediaHtml = isImage
    ? `<img src="${mediaUrl}" alt="Generated image" style="width:100%;height:100%;object-fit:cover;border-radius:8px 8px 0 0;" />`
    : `<video src="${mediaUrl}" playsinline preload="metadata"></video>`;

  const muteBtn = isImage
    ? ""
    : `<button class="history-mute-btn" title="Unmute" onclick="toggleHistoryMute(this, event)">🔇</button>`;

  card.innerHTML = `
    <div class="history-video-wrap">
      ${mediaHtml}
      <div class="history-overlay">${badge}${typeTag}</div>
      ${muteBtn}
    </div>
    <div class="history-card-body">
      <p class="history-script">${snippet}</p>
      <div class="history-card-footer">
        <small class="muted">${formatDate(entry.date)}</small>
        <a href="${mediaUrl}" download="${downloadName}" class="text-button">⬇ Download</a>
      </div>
    </div>
  `;

  // Wire hover play/pause via JS so mute state is respected
  if (!isImage) {
    const video = card.querySelector("video");
    video.muted = historyMuted;
    card.querySelector(".history-video-wrap").addEventListener("mouseenter", () => {
      video.muted = historyMuted;
      video.play().catch(() => {});
    });
    card.querySelector(".history-video-wrap").addEventListener("mouseleave", () => {
      video.pause();
      video.currentTime = 0;
    });
  }

  return card;
}

async function loadHistory() {
  try {
    const resp = await fetch("/api/history");
    const items = await resp.json();
    const section = $("history-section");
    const grid = $("history-grid");
    const countEl = $("history-count");

    if (!items || items.length === 0) {
      section.classList.add("hidden");
      return;
    }

    grid.innerHTML = "";
    items.forEach((entry) => {
      if (entry.video_url || entry.image_url) {
        grid.appendChild(buildHistoryCard(entry));
      }
    });
    countEl.textContent = `${items.length} item${items.length !== 1 ? "s" : ""}`;
    section.classList.remove("hidden");
  } catch (_) {
    // silently ignore
  }
}

// ── Copy Caption ─────────────────────────────────────────────────────────────

function copyCaption() {
  const script = $("copy-caption-btn").dataset.script || $("script-text").textContent;
  if (!script) return;
  navigator.clipboard.writeText(script).then(() => {
    const btn = $("copy-caption-btn");
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = original; }, 2000);
  }).catch(() => {
    alert("Could not copy to clipboard.");
  });
}

// ── Wire events ───────────────────────────────────────────────────────────────

function wireEvents() {
  $("model-upload-btn").addEventListener("click", () => $("model-file-input").click());
  $("model-file-input").addEventListener("change", (event) => uploadModel(event.target.files[0]));

  $("file-input").addEventListener("change", (event) => setProductFile(event.target.files[0]));
  $("change-product-btn").addEventListener("click", resetProduct);

  const dropZone = $("drop-zone");
  dropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropZone.classList.add("dragover");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragover");
    setProductFile(event.dataTransfer.files[0]);
  });

  $("mode-auto").addEventListener("click", () => setMode("auto"));
  $("mode-manual").addEventListener("click", () => setMode("manual"));

  document.querySelectorAll("[data-presenter-source]").forEach((button) => {
    button.addEventListener("click", () => setPresenterSource(button.dataset.presenterSource));
  });

  // Output type toggle
  document.querySelectorAll("[data-output]").forEach((button) => {
    button.addEventListener("click", () => setOutputType(button.dataset.output));
  });

  // Duration + quality selectors
  document.querySelectorAll("[data-duration]").forEach((btn) =>
    btn.addEventListener("click", () => setVideoDuration(btn.dataset.duration))
  );
  document.querySelectorAll("[data-quality]").forEach((btn) =>
    btn.addEventListener("click", () => setVideoQuality(btn.dataset.quality))
  );

  // Platform target (compose view)
  document.querySelectorAll("[data-platform-target]").forEach((btn) => {
    btn.addEventListener("click", () => setPlatformTarget(btn));
  });

  // Platform share (result view)
  document.querySelectorAll("[data-share]").forEach((btn) => {
    btn.addEventListener("click", () => shareToPlatform(btn.dataset.share));
  });

  // Language selector
  document.querySelectorAll("[data-lang]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedLanguage = button.dataset.lang;
      setSelected(document.querySelectorAll("[data-lang]"), button, "selected");
    });
  });

  // Aspect ratio selector
  document.querySelectorAll("[data-ratio]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedRatio = button.dataset.ratio;
      setSelected(document.querySelectorAll("[data-ratio]"), button, "selected");
    });
  });

  document.querySelectorAll("[data-gender]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedGender = button.dataset.gender;
      setSelected(document.querySelectorAll("[data-gender]"), button, "selected");
    });
  });

  document.querySelectorAll("[data-tone]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedSkinTone = button.dataset.tone;
      setSelected(document.querySelectorAll("[data-tone]"), button, "selected");
    });
  });

  document.querySelectorAll("[data-scene]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedScene = button.dataset.scene;
      setSelected(document.querySelectorAll("[data-scene]"), button, "selected");
      $("custom-scene-input").classList.toggle("hidden", state.selectedScene !== "custom");
      if (state.selectedScene === "custom") $("custom-scene-input").focus();
    });
  });

  // Main flow buttons
  $("generate-btn").addEventListener("click", generateScriptPreview);
  $("back-to-compose-btn").addEventListener("click", () => {
    showView("compose");
    $("generate-btn").disabled = false;
  });
  $("regen-script-btn").addEventListener("click", regenScript);
  $("confirm-generate-btn").addEventListener("click", confirmGenerateVideo);

  $("retry-btn").addEventListener("click", () => {
    if (state.productFile) generateScriptPreview();
    else showView("compose");
  });
  $("reset-btn").addEventListener("click", resetAll);
  $("cancel-local-btn").addEventListener("click", () => {
    stopPolling();
    showView("compose");
  });
  $("new-video-btn").addEventListener("click", resetAll);

  // Share buttons
  $("copy-caption-btn").addEventListener("click", copyCaption);

  // Share modal
  $("modal-confirm-btn").addEventListener("click", () => {
    if (_modalCallback) { const fn = _modalCallback; _modalCallback = null; fn(); }
    _closeModal();
  });
  $("modal-cancel-btn").addEventListener("click", _closeModal);
  $("share-modal").addEventListener("click", (e) => {
    if (e.target === $("share-modal")) _closeModal();
  });

  // Orders tab
  $("orders-tab-btn").addEventListener("click", toggleOrdersPanel);
  const refreshClients = $("refresh-clients-btn");
  if (refreshClients) refreshClients.addEventListener("click", loadClientsAdmin);
}

// ── Orders management ─────────────────────────────────────────────────────────

let _ordersPanelOpen = false;
let _ordersTimer = null;

function toggleOrdersPanel() {
  _ordersPanelOpen = !_ordersPanelOpen;
  $("orders-panel").classList.toggle("hidden", !_ordersPanelOpen);
  const clientsPanel = $("clients-panel");
  if (clientsPanel) clientsPanel.classList.toggle("hidden", !_ordersPanelOpen);
  $("history-section") && $("history-section").classList.toggle("hidden", _ordersPanelOpen);
  if (_ordersPanelOpen) {
    loadOrdersAdmin();
    loadClientsAdmin();
    _ordersTimer = setInterval(loadOrdersAdmin, 30000);
  } else {
    clearInterval(_ordersTimer);
    _ordersTimer = null;
  }
}

async function loadOrdersAdmin() {
  try {
    const resp = await fetch("/api/orders");
    let orders = await resp.json();
    orders = orders
      .filter(o => o.status !== "rejected")
      .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
    const pending = orders.filter(o => ["pending", "paid"].includes(o.status)).length;
    const badge = $("orders-badge");
    if (pending > 0) {
      badge.textContent = pending;
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
    const summary = $("orders-summary");
    if (summary) summary.textContent = `${orders.length} total · ${pending} pending`;
    const list = $("orders-list");
    if (!list) return;
    if (orders.length === 0) {
      list.innerHTML = `<p class="muted" style="padding:20px 0">No orders yet. Share <strong>${location.origin}/order</strong> with your customers.</p>`;
      return;
    }
    list.innerHTML = orders.map(renderOrderCard).join("");
    list.querySelectorAll("[data-approve]").forEach(btn =>
      btn.addEventListener("click", () => approveOrder(btn.dataset.approve))
    );

    list.querySelectorAll("[data-approve-veo3]").forEach(btn =>
      btn.addEventListener("click", () => approveVeo3(btn.dataset.approveVeo3))
    );
    list.querySelectorAll("[data-reject]").forEach(btn =>
      btn.addEventListener("click", () => rejectOrder(btn.dataset.reject))
    );
    list.querySelectorAll("[data-recover]").forEach(btn =>
      btn.addEventListener("click", () => recoverVeo3(btn.dataset.recover))
    );
  } catch(e) {
    console.error("Failed to load orders", e);
  }
}

function startOrderNotificationPolling(railwayUrl) {
  // Request browser notification permission
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }

  // Track IDs we've already notified about — never notify twice
  const _notifiedIds = new Set();

  // Seed with orders already on local so we don't notify on page load
  fetch("/api/orders").then(r => r.json()).then(existing => {
    existing.forEach(o => { if (o.id) _notifiedIds.add(o.id); });
  }).catch(() => {});

  // Every 20 seconds: auto-sync from Railway + notify if truly new orders found
  setInterval(async () => {
    try {
      if (!railwayUrl) return;
      const remoteOrders = await fetch(railwayUrl.replace(/\/$/, "") + "/api/orders").then(r => r.json());

      // Only orders we haven't seen/notified about yet
      const newOrders = remoteOrders.filter(o => o.id && !_notifiedIds.has(o.id));
      if (newOrders.length === 0) return;

      // Mark as seen immediately so re-polls don't re-notify
      newOrders.forEach(o => _notifiedIds.add(o.id));

      // Sync each new order to local
      for (const order of newOrders) {
        await fetch("/api/sync-order", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(order)
        });
      }

      // Refresh orders panel
      loadOrdersBadge();
      loadOrdersAdmin();

      // Browser notification — once only
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification("🛍️ New Order — Vishleshak UGC", {
          body: `${newOrders.length} new order(s) waiting for your approval`,
          icon: "/favicon.ico",
        });
      }
      showToast(`🛍️ ${newOrders.length} new order(s) synced from Railway!`);
    } catch(_) {}
  }, 20000);
}

async function loadOrdersBadge() {
  try {
    const resp = await fetch("/api/orders");
    const orders = await resp.json();
    const pending = orders.filter(o => ["pending", "paid"].includes(o.status)).length;
    const badge = $("orders-badge");
    if (badge && pending > 0) { badge.textContent = pending; badge.classList.remove("hidden"); }
  } catch(_) {}
}

async function loadClientPackages() {
  if (_clientPackages.length) return _clientPackages;
  _clientPackages = await fetch("/api/packages").then(r => r.json()).catch(() => []);
  return _clientPackages;
}

function clientCreditLine(client) {
  const used = Number(client.credits_used || 0);
  const total = Number(client.credits_total || 0);
  const left = Number(client.credits_left || Math.max(0, total - used));
  return `Credits ${left} left · ${used}/${total} used`;
}

function renderClientCard(client) {
  const packageOptions = _clientPackages.map(pkg =>
    `<option value="${escapeHtml(pkg.id)}">${escapeHtml(pkg.name)} - Rs. ${pkg.price_inr}</option>`
  ).join("");
  const expiry = client.package_expires_at
    ? new Date(client.package_expires_at).toLocaleDateString("en-IN", {day:"2-digit", month:"short", year:"numeric"})
    : "No package";
  const status = client.active ? "Active" : (client.status || "Lead");
  const automations = [
    client.whatsapp_active ? "WhatsApp" : "",
    client.chatbot_active ? "Chatbot" : "",
    client.payment_flow_active ? "Payments" : "",
    client.followup_active ? "Follow-up" : "",
  ].filter(Boolean).join(" · ") || "No automation";
  return `<div class="order-card client-card">
    <div class="client-avatar">${escapeHtml((client.business_name || "?").slice(0, 1).toUpperCase())}</div>
    <div class="order-info">
      <div class="order-title-row">
        <span class="order-name">${escapeHtml(client.business_name || "Unnamed Client")}</span>
        <span class="order-status ${client.active ? "completed" : "pending"}">${escapeHtml(status)}</span>
      </div>
      <div class="order-tags">
        <span>${escapeHtml(client.package_name || "No package")}</span>
        <span class="price-tag">${escapeHtml(clientCreditLine(client))}</span>
        <span>Expires: ${escapeHtml(expiry)}</span>
      </div>
      <span class="order-meta">${escapeHtml(client.contact_name || "")} · ${escapeHtml(client.phone || "")} · ${escapeHtml(client.niche || "General")}</span>
      <span class="order-notes">${escapeHtml(automations)}</span>
    </div>
    <div class="order-actions">
      <select class="client-package-select" id="pkg-${client.id}">
        <option value="">Select package</option>
        ${packageOptions}
      </select>
      <button class="approve-btn primary-action" data-assign-package="${client.id}">Assign Package</button>
      <button class="approve-btn subtle" data-view-usage="${client.id}">Usage Logs</button>
    </div>
  </div>`;
}

async function loadClientsAdmin() {
  const panel = $("clients-panel");
  if (!panel) return;
  await loadClientPackages();
  const clients = await fetch("/api/clients").then(r => r.json()).catch(() => []);
  const summary = $("clients-summary");
  const active = clients.filter(c => c.active).length;
  if (summary) summary.textContent = `${clients.length} clients · ${active} active`;
  const list = $("clients-list");
  if (!list) return;
  if (!clients.length) {
    list.innerHTML = `<p class="muted" style="padding:20px 0">No clients yet. A client is created automatically when they place an order.</p>`;
    return;
  }
  list.innerHTML = clients.map(renderClientCard).join("");
  list.querySelectorAll("[data-assign-package]").forEach(btn =>
    btn.addEventListener("click", () => assignPackageToClient(btn.dataset.assignPackage))
  );
  list.querySelectorAll("[data-view-usage]").forEach(btn =>
    btn.addEventListener("click", () => viewClientUsage(btn.dataset.viewUsage))
  );
}

async function assignPackageToClient(clientId) {
  const select = $(`pkg-${clientId}`);
  const packageId = select ? select.value : "";
  if (!packageId) {
    showToast("Select a package first.", 3000);
    return;
  }
  const resp = await fetch(`/api/clients/${clientId}/assign-package`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({package_id: packageId, note: "Assigned from admin dashboard"}),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    showToast("Package assign failed: " + (data.detail || resp.status), 5000);
    return;
  }
  showToast("Package assigned.");
  loadClientsAdmin();
}

async function viewClientUsage(clientId) {
  const rows = await fetch(`/api/clients/${clientId}/usage`).then(r => r.json()).catch(() => []);
  const text = rows.slice(0, 12).map(row =>
    `${new Date(row.created_at).toLocaleString("en-IN")} - ${row.usage_type} - ${row.note || row.order_id || ""}`
  ).join("\n") || "No usage yet.";
  alert(text);
}

function estimateOrderPrice(order) {
  if (order.output_type === "image") return 49;
  let price = order.video_style === "cinematic" ? 599 : 499;
  if (String(order.video_duration || "5") === "10") price += 200;
  if (order.presenter_source === "uploaded") price += 100;
  return price;
}

function renderOrderCard(order) {
  const thumb = `/order_uploads/${order.id}.jpg`;
  const date = new Date(order.created_at).toLocaleString("en-IN", {day:"2-digit", month:"short", hour:"2-digit", minute:"2-digit"});
  const statusLabel = {
    payment_pending: "Payment Pending",
    payment_error: "Payment Error",
    payment_failed: "Payment Failed",
    paid: "Paid",
    pending: "Pending",
    processing: "Processing",
    completed: "Done",
    rejected: "Rejected",
    failed: "Failed"
  }[order.status] || order.status;
  const outputLabel = order.output_type === "image" ? "Image creative" : "Video ad";
  const styleLabel = order.output_type === "image"
    ? "Static"
    : (order.video_style === "cinematic" ? "Cinematic" : "Talking");
  const presenterLabel = {ai:"AI model", uploaded:"Reference model", product:"Product only"}[order.presenter_source] || "AI model";
  const durationLabel = order.output_type === "image" ? "No duration" : `${order.video_duration || "5"}s`;
  const priceLabel = `Rs. ${estimateOrderPrice(order)}`;
  const notePreview = order.notes ? order.notes.replace(/\s+/g, " ").slice(0, 240) : "";

  let actions = "";
  if (order.status === "payment_pending") {
    actions = `<div class="order-actions">
      ${order.razorpay_payment_link_url ? `<a class="approve-btn secondary-action" href="${order.razorpay_payment_link_url}" target="_blank">Open Payment Link</a>` : ""}
      <span class="processing-chip">Waiting for customer payment</span>
      <button class="reject-btn" data-reject="${order.id}">Reject</button>
    </div>`;
  } else if (order.status === "payment_error" || order.status === "payment_failed") {
    actions = `<div class="order-actions">
      <span class="processing-chip">${order.payment_error || "Payment did not complete"}</span>
      <button class="reject-btn" data-reject="${order.id}">Reject</button>
    </div>`;
  } else if (order.status === "pending" || order.status === "paid" || order.status === "failed") {
    const kieTaskId = order.kie_task_id || "";
    const recoverRow = order.video_style === "cinematic" || kieTaskId ? `
      <div class="recovery-row">
        <input id="recover-task-${order.id}" type="text" placeholder="Recovery task ID" value="${kieTaskId}" />
        <button class="approve-btn subtle" data-recover="${order.id}">Recover</button>
      </div>` : "";
    const approveButtons = order.output_type === "image"
      ? `<button class="approve-btn primary-action" data-approve="${order.id}">Approve Image</button>`
      : `<button class="approve-btn primary-action" data-approve="${order.id}">Approve Talking Ad</button>
         <button class="approve-btn secondary-action" data-approve-veo3="${order.id}">Approve Cinematic Ad</button>`;
    actions = `<div class="order-actions">
      ${approveButtons}
      <button class="reject-btn" data-reject="${order.id}">Reject</button>
      ${recoverRow}
    </div>`;
  } else if (order.status === "processing") {
    actions = `<div class="order-actions"><span class="processing-chip">${order.job_step || "Starting"}...</span></div>`;
  } else if (order.status === "completed") {
    const resultBase = _railwayUrl ? _railwayUrl.replace(/\/$/, "") : location.origin;
    const resultUrl = `${resultBase}/order/result/${order.id}`;
    const waMsg = encodeURIComponent(`Hi ${order.customer_name}! Your UGC ${order.output_type} is ready. Download here: ${resultUrl}`);
    const waLink = `https://wa.me/${(order.customer_phone||"").replace(/\D/g,"")}?text=${waMsg}`;
    actions = `<div class="order-actions">
      <a href="${resultUrl}" target="_blank" class="approve-btn primary-action">View Result</a>
      <a href="${waLink}" target="_blank" class="whatsapp-send">Send on WhatsApp</a>
    </div>`;
  }

  return `<div class="order-card">
    <img class="order-thumb" src="${thumb}" onerror="this.style.background='#e2e8f0';this.removeAttribute('src')" alt="Product">
    <div class="order-info">
      <div class="order-title-row">
        <span class="order-name">${order.customer_name}</span>
        <span class="order-status ${order.status}">${statusLabel}</span>
      </div>
      <div class="order-tags">
        <span>${outputLabel}</span>
        <span>${styleLabel}</span>
        <span>${presenterLabel}</span>
        <span>${durationLabel}</span>
        <span>${order.language || "English"}</span>
        <span class="price-tag">${priceLabel}</span>
        ${order.payment_status ? `<span>Payment: ${order.payment_status}</span>` : ""}
      </div>
      <span class="order-meta">${order.customer_phone || ""} · ${date}</span>
      ${notePreview ? `<span class="order-notes">${notePreview}${order.notes.length > 240 ? "..." : ""}</span>` : ""}
    </div>
    ${actions}
  </div>`;
}

async function checkModelReady(orderId) {
  /** Returns true if a model photo is available (global OR order-specific). */
  try {
    // First check if this specific order has its own customer model photo
    if (orderId) {
      const order = await fetch(`/api/orders/${orderId}`).then(r => r.json()).catch(() => ({}));
      if (order.model_image_path) return true;          // customer uploaded their own photo
      if (order.presenter_source === "ai") return true; // AI mode — no photo needed
      if (order.presenter_source === "product") return true; // Product-only mode — no presenter photo needed
    }
    // Fall back to checking global admin model
    const status = await fetch("/api/model-status").then(r => r.json());
    if (!status.configured) {
      showToast("⚠️ No model photo uploaded! Upload your presenter photo first, or the customer must select 'Use Our Model' on the order form.", 7000);
      const modelSection = document.querySelector(".model-card");
      if (modelSection) {
        modelSection.scrollIntoView({behavior: "smooth", block: "center"});
        modelSection.style.outline = "3px solid #ef4444";
        modelSection.style.borderRadius = "12px";
        setTimeout(() => { modelSection.style.outline = ""; }, 3000);
      }
      return false;
    }
    return true;
  } catch(e) {
    return true; // Don't block on network error
  }
}

async function approveOrder(orderId) {
  if (!(await checkModelReady(orderId))) return;
  try {
    const resp = await fetch(`/api/orders/${orderId}/approve`, {method: "POST"});
    if (!resp.ok) { const d = await resp.json(); throw new Error(d.detail); }
    showToast("Order approved! Generation started.");
    loadOrdersAdmin();
  } catch(e) {
    showToast("Error: " + e.message);
  }
}


async function approveVeo3(orderId) {
  if (!(await checkModelReady(orderId))) return;
  try {
    const resp = await fetch(`/api/orders/${orderId}/approve-veo3`, {method: "POST"});
    if (!resp.ok) { const d = await resp.json(); throw new Error(d.detail); }
    showToast("Cinematic ad started. This usually takes 5-8 min.");
    loadOrdersAdmin();
  } catch(e) {
    showToast("Error: " + e.message);
  }
}

async function rejectOrder(orderId) {
  if (!confirm("Reject this order?")) return;
  await fetch(`/api/orders/${orderId}/reject`, {method: "POST", body: new URLSearchParams({reason: ""})});
  showToast("Order rejected.");
  loadOrdersAdmin();
}

async function recoverVeo3(orderId) {
  const taskInput = document.getElementById(`recover-task-${orderId}`);
  const taskId = taskInput ? taskInput.value.trim() : "";
  if (!taskId) { showToast("Enter the recovery task ID first"); return; }
  showToast("Recovering the submitted cinematic video. This may take a minute.");
  try {
    const resp = await fetch("/api/recover-veo3", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({order_id: orderId, task_id: taskId}),
    });
    const data = await resp.json();
    if (data.status === "recovered") {
      showToast("✅ Video recovered! Check View Result.", 5000);
      loadOrdersAdmin();
    } else {
      showToast("❌ Recovery failed: " + (data.msg || "unknown error"), 6000);
    }
  } catch(e) {
    showToast("❌ Recovery error: " + e.message, 6000);
  }
}

wireEvents();
loadModelStatus();
loadPipelineInfo();

// ── Mode detection (admin vs client) ─────────────────────────────────────────
async function initAppMode() {
  try {
    const cfg = await fetch("/api/config").then(r => r.json());
    const isClient = cfg.mode === "client";
    const railwayUrl = cfg.railway_url || "";
    _railwayUrl = railwayUrl;   // store globally for renderOrderCard

    if (isClient) {
      // CLIENT mode (Railway): hide history, orders, generate UI — show order form link
      const ordersBtn = $("orders-tab-btn");
      if (ordersBtn) ordersBtn.style.display = "none";
      const hist = $("history-section");
      if (hist) hist.style.display = "none";
      // Hide the full compose/admin shell — redirect root to /order
      if (window.location.pathname === "/" || window.location.pathname === "") {
        window.location.replace("/order");
      }
    } else {
      // ADMIN mode (local): show everything + add Sync button if Railway URL is set
      loadHistory();
      loadOrdersBadge();
      startOrderNotificationPolling(railwayUrl);
      if (railwayUrl) addSyncButton(railwayUrl);
      // Auto-open orders panel on load
      _ordersPanelOpen = true;
      $("orders-panel").classList.remove("hidden");
      const clientsPanel = $("clients-panel");
      if (clientsPanel) clientsPanel.classList.remove("hidden");
      loadOrdersAdmin();
      loadClientsAdmin();
      _ordersTimer = setInterval(loadOrdersAdmin, 30000);
    }
  } catch(e) {
    // fallback: treat as admin
    loadHistory();
    loadOrdersBadge();
    startOrderNotificationPolling("");
  }
}

function addSyncButton(railwayUrl) {
  const btn = $("sync-railway-btn");
  if (btn) {
    btn.style.display = "inline-block";
    btn.addEventListener("click", () => syncRailwayOrders(railwayUrl));
  }
  const pushBtn = $("push-railway-btn");
  if (pushBtn) {
    pushBtn.style.display = "inline-block";
    pushBtn.addEventListener("click", pushToRailway);
  }
}

async function pushToRailway() {
  try {
    showToast("⬆️ Pushing completed orders to Railway…");
    const resp = await fetch("/api/resync-railway", {method: "POST"});
    const data = await resp.json();
    if (data.status === "done") {
      showToast(`✅ Pushed ${data.pushed} order(s) to Railway!`, 4000);
    } else {
      showToast("❌ " + (data.msg || "Push failed"), 5000);
    }
  } catch(e) {
    showToast("Push failed: " + e.message);
  }
}

async function syncRailwayOrders(railwayUrl) {
  try {
    showToast("Syncing orders from Railway...");
    const url = railwayUrl.replace(/\/$/, "") + "/api/orders";
    const remoteOrders = await fetch(url).then(r => r.json());
    const localResp = await fetch("/api/orders").then(r => r.json());
    const localIds = new Set(localResp.map(o => o.id));
    const newOrders = remoteOrders.filter(o => !localIds.has(o.id));
    if (newOrders.length === 0) {
      showToast("Already up to date — no new orders.");
      return;
    }
    // Push each new order to local server
    for (const order of newOrders) {
      await fetch("/api/sync-order", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(order)
      });
    }
    showToast(`✅ Synced ${newOrders.length} new order(s) from Railway!`);
    loadOrdersAdmin();
  } catch(e) {
    showToast("Sync failed: " + e.message);
  }
}

initAppMode();
