let TOKEN = localStorage.getItem("token") || "";
let PUB = null;
let MODE = "auto";
let VOICEMODE = "preset";
let AVATAR = "a1";
let authMode = "login";
let pollTimer = null;
let pollVersion = 0;
let activeJobId = null;
let SOURCEMODE = "link";
let voiceRevision = "";
let savedVoiceReference = "";
const pendingUploads = new Set();
let startingJob = false;
let currentUser = "";
let formReady = false;
let formTouched = false;
let historyReady = false;
let jobHistory = [];
let selectionVersion = 0;
let lastDraftSnapshot = "";
let historySignature = "";

const $ = (id) => document.getElementById(id);
async function api(path, opts = {}) {
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const error = new Error(data.detail || "请求失败");
    error.status = r.status;
    throw error;
  }
  return data;
}

// ---------- 登录 ----------
function switchTab(m) {
  authMode = m;
  $("tabLogin").classList.toggle("on", m === "login");
  $("tabReg").classList.toggle("on", m === "reg");
  $("authBtn").textContent = m === "login" ? "登 录" : "注册并领点数";
  $("authErr").textContent = "";
}
async function doAuth() {
  if ($("authBtn").disabled) return;
  $("authBtn").disabled = true;
  const username = $("authUser").value.trim();
  const password = $("authPwd").value;
  $("authErr").textContent = "";
  try {
    const ep = authMode === "login" ? "/api/login" : "/api/register";
    const d = await api(ep, { method: "POST", body: JSON.stringify({ username, password }) });
    TOKEN = d.token;
    localStorage.setItem("token", TOKEN);
    await boot();
  } catch (e) {
    $("authErr").textContent = e.message;
  } finally {
    $("authBtn").disabled = false;
  }
}
async function logout() {
  saveDraft();
  pollVersion++;
  selectionVersion++;
  clearTimeout(pollTimer);
  try {
    await fetch("/api/logout", {method: "POST", headers: TOKEN ? {"Authorization": "Bearer " + TOKEN} : {}});
  } catch (_) {}
  localStorage.removeItem("token");
  TOKEN = "";
  location.reload();
}

// ---------- 初始化 ----------
async function loadPublic() {
  PUB = await api("/api/public");
  $("brandName").textContent = PUB.brand.name;
  $("topBrand").textContent = PUB.brand.name;
  $("footBrand").textContent = PUB.brand.name;
  $("brandSlogan").textContent = PUB.brand.slogan;
  $("topSlogan").textContent = PUB.brand.slogan;
  // 音色
  $("voice").innerHTML = PUB.voices.map(v => `<option value="${v.id}">${v.name}</option>`).join("");
  // 形象
  $("avatars").innerHTML = PUB.avatars.map((a, i) =>
    `<div class="avatar ${i === 0 ? "on" : ""}" data-id="${a.id}" onclick="pickAvatar('${a.id}')">
       <img src="${a.img}"><div class="an">${a.name}</div></div>`).join("");
  // 步骤条
  const names = ["提取文案", "改写文案", "生成配音", "检查底版", "换口型", "校验成片"];
  $("steps").innerHTML = names.map((n, i) =>
    `<div class="step" id="st${i + 1}"><div class="dot">${i + 1}</div><div class="nm">${n}</div></div>`).join("");
}
function pickAvatar(id) {
  AVATAR = id;
  document.querySelectorAll(".avatar").forEach(el =>
    el.classList.toggle("on", el.dataset.id === id));
  saveDraft();
}
function setSourceMode(mode) {
  SOURCEMODE = mode;
  $("sourceLink").classList.toggle("on", mode === "link");
  $("sourceText").classList.toggle("on", mode === "text");
  $("link").classList.toggle("hidden", mode !== "link");
  $("sourceCopy").classList.toggle("hidden", mode !== "text");
  saveDraft();
}

function notice(id, message) {
  $(id).textContent = message;
  $(id).classList.toggle("hidden", !message);
}

function storageKey(kind) {
  return `yijian:${kind}:${currentUser}`;
}

function readStored(kind) {
  try { return JSON.parse(localStorage.getItem(storageKey(kind)) || "null"); }
  catch (_) { return null; }
}

function writeStored(kind, value) {
  try { localStorage.setItem(storageKey(kind), JSON.stringify(value)); return true; }
  catch (_) { notice("workspaceNotice", "浏览器未能保存草稿，请勿关闭页面。"); return false; }
}

function requestKeyFor(body) {
  const storageName = storageKey("pending-request");
  const encoded = JSON.stringify(body);
  try {
    const saved = JSON.parse(localStorage.getItem(storageName) || "null");
    if (saved && saved.body === encoded && saved.key) return saved.key;
    const key = (globalThis.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    localStorage.setItem(storageName, JSON.stringify({body: encoded, key}));
    return key;
  } catch (_) {
    return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }
}

function clearPendingRequest(key) {
  try {
    const storageName = storageKey("pending-request");
    const saved = JSON.parse(localStorage.getItem(storageName) || "null");
    if (!saved || saved.key === key) localStorage.removeItem(storageName);
  } catch (_) {}
}

function draftSnapshot() {
  return {version: 1, voiceRevision, values: {
    source_mode: SOURCEMODE, link: $("link").value, source_text: $("sourceCopy").value,
    industry: $("industry").value, extra: $("extra").value,
    voice: $("voice").value, speed: $("speed").value, avatar: AVATAR,
    use_clone: VOICEMODE === "clone", voice_ref_text: $("voiceRefText").value,
  }};
}

function saveDraft() {
  if (!currentUser || !formReady) return;
  const draft = draftSnapshot();
  const snapshot = JSON.stringify(draft);
  // An unchanged older tab must not overwrite newer edits on logout/pagehide.
  if (snapshot === lastDraftSnapshot) return;
  formTouched = true;
  if (writeStored("draft", draft)) lastDraftSnapshot = snapshot;
}

function restoreInputs(values, restoreReference = false) {
  const wasReady = formReady;
  formReady = false;
  for (const [id, key] of [["link", "link"], ["sourceCopy", "source_text"],
                          ["industry", "industry"], ["extra", "extra"]]) {
    $(id).value = typeof values[key] === "string" ? values[key] : "";
  }
  setSourceMode(values.source_mode || (values.source_text ? "text" : "link"));
  if (PUB.voices.some(v => v.id === values.voice)) $("voice").value = values.voice;
  const speed = Number(values.speed);
  if ([0.8, 1, 1.25, 1.5].includes(speed)) $("speed").value = speed === 1 ? "1.0" : String(speed);
  if (values.avatar) pickAvatar(values.avatar);
  if (typeof values.use_clone === "boolean") setVoiceMode(values.use_clone ? "clone" : "preset");
  if (restoreReference && typeof values.voice_ref_text === "string") $("voiceRefText").value = values.voice_ref_text;
  formReady = wasReady;
}

function busyJob() {
  return jobHistory.find(j => ["queued", "running", "waiting"].includes(j.status));
}

function updateGenerateButton() {
  $("goBtn").disabled = !historyReady || startingJob || Boolean(busyJob());
}

function renderHistory() {
  const statuses = {failed: "失败", waiting: "待恢复", running: "生成中", queued: "排队中"};
  const signature = JSON.stringify(jobHistory);
  if (signature !== historySignature) {
    const options = jobHistory.map(j => {
      const option = document.createElement("option");
      option.value = j.id;
      const status = j.status === "done"
        ? (j.quality_review === "needs_revision" ? "需修改" : j.quality_review === "pending" ? "待验收" : "已完成")
        : (statuses[j.status] || j.status);
      option.textContent = `${new Date(j.created_at * 1000).toLocaleString()} · ${j.industry || "未填写行业"} · ${status} · ${j.id}`;
      return option;
    });
    if (!options.length) {
      const empty = document.createElement("option"); empty.value = ""; empty.textContent = "暂无任务"; options.push(empty);
    }
    $("jobHistory").replaceChildren(...options);
    historySignature = signature;
  }
  $("jobHistory").disabled = !jobHistory.length || startingJob;
  $("jobHistory").value = activeJobId || "";
  updateGenerateButton();
}

async function refreshJobs(restoreForm = false) {
  const session = TOKEN;
  try {
    const jobs = await api("/api/jobs");
    if (session !== TOKEN) return;
    jobHistory = jobs;
    historyReady = true;
    const saved = readStored("selected-job");
    const selected = busyJob() || jobs.find(j => j.id === activeJobId || j.id === saved) || jobs[0];
    renderHistory();
    if (selected) await selectJob(selected.id, restoreForm);
  } catch (e) {
    if (session !== TOKEN) return;
    historyReady = false;
    notice("workspaceNotice", "任务恢复失败，已有内容未删除：" + e.message);
    $("restoreRetry").classList.remove("hidden");
  }
  updateGenerateButton();
}

async function selectJob(jobId, restoreForm = false) {
  if (!jobId) return;
  const version = ++selectionVersion;
  const session = TOKEN;
  try {
    const job = await api("/api/jobs/" + jobId);
    if (version !== selectionVersion || session !== TOKEN) return;
    activeJobId = job.id;
    writeStored("selected-job", job.id);
    notice("jobError", "");
    if (restoreForm && !formTouched) {
      restoreInputs(job.params || {});
      saveDraft();
    }
    showJob(job);
    const running = jobHistory.find(j => ["queued", "running"].includes(j.status));
    if (running) poll(running.id);
  } catch (e) {
    if (version !== selectionVersion || session !== TOKEN) return;
    $("jobHistory").value = activeJobId || "";
    notice("jobError", "读取任务失败，已保留当前内容：" + e.message);
  }
}

async function boot() {
  const session = TOKEN;
  let me;
  try {
    me = await api("/api/me");
    if (session !== TOKEN) return;
  } catch (e) {
    if (session !== TOKEN) return;
    $("authErr").textContent = e.status === 401 ? "请登录" : "账号信息暂时无法加载：" + e.message;
    return;
  }
  currentUser = me.username;
  formReady = false;
  $("authMask").classList.add("hidden");
  $("appMain").classList.remove("hidden");
  $("creditNum").textContent = me.credits;
  $("hello").textContent = "👤 " + me.username;
  $("adminLink").classList.toggle("hidden", !me.is_admin);
  if (me.has_face) showFace(me.face_url);
  if (me.has_voice) {
    showVoice(me.voice_url);
    $("voiceRefText").value = me.voice_ref_text || "";
    savedVoiceReference = me.voice_ref_text || "";
    voiceRevision = me.voice_revision || "";
    setVoiceMode("clone");
  }
  $("faceReq").textContent = PUB.lipsync_ready ? "" : "（口型服务待配置）";
  if (PUB.voiceclone_ready) $("cloneEngineTip").classList.add("hidden");
  const draft = readStored("draft");
  const hasDraft = draft?.version === 1 && draft.values && typeof draft.values === "object";
  if (hasDraft) restoreInputs(draft.values, draft.voiceRevision === voiceRevision);
  if (hasDraft) lastDraftSnapshot = JSON.stringify(draftSnapshot());
  formReady = true;
  await refreshJobs(!hasDraft);
}

function showFace(url) {
  const isV = /\.(mp4|mov)(\?|$)/i.test(url);
  $("faceEmpty").classList.add("hidden");
  const img = $("facePreview"), vid = $("facePreviewV");
  if (isV) {
    vid.src = url; vid.classList.remove("hidden"); img.classList.add("hidden");
    vid.setAttribute("controls", "");
  } else {
    img.src = url; img.classList.remove("hidden"); vid.classList.add("hidden");
  }
  $("faceStatus").textContent = "✅ 底版已就绪，将用于对口型";
  $("faceStatus").className = "face-status ok";
}

function setVoiceMode(m) {
  VOICEMODE = m;
  $("vmPreset").classList.toggle("on", m === "preset");
  $("vmClone").classList.toggle("on", m === "clone");
  $("cloneBox").classList.toggle("hidden", m !== "clone");
  // 克隆模式下隐藏"预置音色/语速"，避免混淆
  $("presetVoiceRow").classList.toggle("hidden", m === "clone");
  saveDraft();
}

function showVoice(url) {
  $("voiceEmpty").classList.add("hidden");
  const a = $("voicePreview");
  a.src = url; a.classList.remove("hidden");
  $("voiceStatus").textContent = "✅ 声音样本已上传";
  $("voiceStatus").className = "face-status ok";
  setVoiceMode("clone");
}

function setMediaStatus(id, message, kind = "") {
  const el = $(id);
  if (!el) return;
  el.textContent = message;
  el.classList.toggle("hidden", !message);
  el.classList.remove("error");
  if (kind === "error") el.classList.add("error");
}

function setupMediaStates() {
  const video = $("vVideo");
  if (!video) return;
  video.addEventListener("loadstart", () => setMediaStatus("vVideoStatus", "视频加载中…"));
  video.addEventListener("waiting", () => setMediaStatus("vVideoStatus", "视频缓冲中…"));
  video.addEventListener("stalled", () => setMediaStatus("vVideoStatus", "视频缓冲中…"));
  video.addEventListener("canplay", () => setMediaStatus("vVideoStatus", ""));
  video.addEventListener("playing", () => setMediaStatus("vVideoStatus", ""));
  video.addEventListener("error", () => setMediaStatus(
    "vVideoStatus", "视频加载失败，请刷新重试。原任务文件仍保留。", "error"));
}

async function uploadVoice() {
  const f = $("voiceFile").files[0];
  if (!f) return;
  pendingUploads.add("voice");
  $("saveVoiceRef").disabled = true;
  $("voiceStatus").textContent = "上传中…";
  $("voiceStatus").className = "face-status";
  const fd = new FormData();
  fd.append("file", f);
  fd.append("ref_text", $("voiceRefText").value || "");
  try {
    const r = await fetch("/api/voice", {
      method: "POST", headers: { "Authorization": "Bearer " + TOKEN }, body: fd,
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "上传失败");
    voiceRevision = d.voice_revision;
    savedVoiceReference = d.voice_ref_text || "";
    showVoice(d.voice_url + "?t=" + Date.now());
    saveDraft();
  } catch (e) {
    $("voiceStatus").textContent = "❌ " + e.message;
  } finally {
    pendingUploads.delete("voice");
    $("saveVoiceRef").disabled = false;
  }
}

async function saveVoiceReference() {
  if (pendingUploads.has("voice")) return false;
  $("saveVoiceRef").disabled = true;
  try {
    const result = await api("/api/voice/reference", {method:"POST", body:JSON.stringify({
      ref_text: $("voiceRefText").value.trim(), voice_revision: voiceRevision,
    })});
    savedVoiceReference = result.voice_ref_text;
    saveDraft();
    $("voiceStatus").textContent = "参考原文已保存";
    $("voiceStatus").className = "face-status ok";
    return true;
  } catch (e) {
    $("voiceStatus").textContent = e.message;
    $("voiceStatus").className = "face-status";
    return false;
  } finally {
    $("saveVoiceRef").disabled = false;
  }
}

async function uploadFace() {
  const f = $("faceFile").files[0];
  if (!f) return;
  pendingUploads.add("face");
  $("faceStatus").textContent = "上传中…";
  $("faceStatus").className = "face-status";
  const fd = new FormData();
  fd.append("file", f);
  try {
    const r = await fetch("/api/face", {
      method: "POST", headers: { "Authorization": "Bearer " + TOKEN }, body: fd,
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "上传失败");
    showFace(d.face_url + "?t=" + Date.now());
  } catch (e) {
    $("faceStatus").textContent = "❌ " + e.message;
  } finally {
    pendingUploads.delete("face");
  }
}

// ---------- 任务 ----------
function setSteps(step, status) {
  for (let i = 1; i <= 6; i++) {
    const el = $("st" + i);
    el.classList.remove("active", "done");
    if (i < step || (status === "done" && i <= step)) el.classList.add("done");
    else if (i === step) el.classList.add(status === "done" ? "done" : "active");
  }
}

async function startJob() {
  if (startingJob || $("goBtn").disabled) return;
  startingJob = true;
  try {
    await submitJob();
  } finally {
    startingJob = false;
    updateGenerateButton();
    renderHistory();
  }
}

function confirmGeneration(cost) {
  const dialog = $("generationConfirm");
  dialog.returnValue = "";
  $("generationConfirmCost").textContent = `本次生成将消耗 ${cost} 点数。`;
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once:true});
    dialog.showModal();
  });
}

async function submitJob() {
  if (pendingUploads.size) {
    alert("素材正在上传，请完成后再生成"); return;
  }
  if (!(SOURCEMODE === "link" ? $("link").value : $("sourceCopy").value).trim()) {
    alert("请填写视频链接或原始文案"); return;
  }
  const body = {
    link: SOURCEMODE === "link" ? $("link").value.trim() : "",
    source_text: SOURCEMODE === "text" ? $("sourceCopy").value.trim() : "",
    industry: $("industry").value.trim(),
    extra: $("extra").value.trim(),
    voice: $("voice").value,
    speed: parseFloat($("speed").value),
    avatar: AVATAR,
    mode: MODE,
    use_clone: VOICEMODE === "clone",
    voice_ref_text: $("voiceRefText").value.trim(),
  };
  if (body.use_clone && !body.voice_ref_text) {
    alert("用克隆声音时，请先填写「这段录音里你说的那句话」");
    return;
  }
  if (body.use_clone && body.voice_ref_text !== savedVoiceReference && !(await saveVoiceReference())) return;
  const cost = PUB.credits_per_video;
  if (!(await confirmGeneration(cost))) return;
  saveDraft();
  const session = TOKEN;
  selectionVersion++;
  $("goBtn").disabled = true;
  $("jobHistory").disabled = true;
  notice("jobError", "正在提交任务…");
  const requestKey = requestKeyFor(body);

  try {
    const d = await api("/api/jobs", {
      method: "POST",
      headers: {"Idempotency-Key": requestKey},
      body: JSON.stringify(body),
    });
    if (session !== TOKEN) return;
    clearPendingRequest(requestKey);
    activeJobId = d.job_id;
    writeStored("selected-job", d.job_id);
    jobHistory.unshift({id: d.job_id, status: "queued", created_at: Date.now() / 1000, industry: body.industry});
    showJob({id: d.job_id, status: "queued", progress: 0, step: 0, message: "任务已提交…", result: {}});
    notice("jobError", "");
    $("creditNum").textContent = d.credits;
    poll(d.job_id);
  } catch (e) {
    if (session !== TOKEN) return;
    notice("jobError", "提交未完成，已保留原任务和草稿：" + e.message);
    if (!e.status || e.status >= 500) {
      historyReady = false;
      notice("workspaceNotice", "提交结果暂不明确，请重新加载核对原任务，不要重复生成。");
      $("restoreRetry").classList.remove("hidden");
    } else {
      clearPendingRequest(requestKey);
    }
  }
}

function poll(jobId) {
  clearTimeout(pollTimer);
  const version = ++pollVersion;
  let queryFailed = false;
  async function tick() {
    try {
      const j = await api("/api/jobs/" + jobId);
      if (version !== pollVersion) return;
      if (queryFailed) { notice("jobError", ""); queryFailed = false; }
      updateHistoryJob(j);
      if (activeJobId === j.id) showJob(j);
      else renderHistory();
      if (!["queued", "running"].includes(j.status)) return;
    } catch (e) {
      if (version !== pollVersion) return;
      queryFailed = true;
      notice("jobError", "进度查询失败，已有内容已保留，正在重试：" + e.message);
    }
    pollTimer = setTimeout(tick, 1500);
  }
  tick();
}

function updateHistoryJob(j) {
  const entry = jobHistory.find(item => item.id === j.id);
  if (entry) entry.status = j.status;
}

function showJob(j) {
  updateHistoryJob(j);
  renderHistory();
  $("emptyState").classList.add("hidden");
  $("running").classList.remove("hidden");
  $("progBar").style.width = j.progress + "%";
  $("runMsg").textContent = j.message;
  $("resumeBtn").classList.toggle("hidden", !j.can_resume);
  setSteps(j.step, j.status);
  renderResult(j.result);
}

async function resumeJob() {
  if (!activeJobId) return;
  const jobId = activeJobId;
  $("resumeBtn").disabled = true;
  try {
    await api("/api/jobs/" + jobId + "/resume", {method: "POST"});
    poll(jobId);
  } catch (e) {
    $("runMsg").textContent = e.message;
  } finally {
    $("resumeBtn").disabled = false;
  }
}

function renderResult(res = {}) {
  res = res || {};
  const has = Object.keys(res).length > 0;
  $("resultArea").classList.toggle("hidden", !has);
  for (const [field, key] of [["fTranscript", "transcript"], ["fRewritten", "rewritten"],
                             ["fAudio", "audio_url"], ["fVideo", "video_url"]]) {
    $(field).classList.toggle("hidden", !res[key]);
  }
  $("vTranscript").textContent = res.transcript || "";
  $("vRewritten").textContent = res.rewritten || "";
  for (const [id, key] of [["vAudio", "audio_url"], ["vVideo", "video_url"]]) {
    const url = res[key] || null;
    if ($(id).getAttribute("src") !== url) {
      $(id).pause();
      if (id === "vVideo") setMediaStatus("vVideoStatus", url ? "视频加载中…" : "");
      if (url) $(id).src = url;
      else $(id).removeAttribute("src");
      $(id).load();
    }
  }
  if (res.video_url) $("dlVideo").href = res.video_url;
  else $("dlVideo").removeAttribute("href");
  $("phTip").classList.toggle("hidden", !res.placeholder);
  const qualityMessages = {
    pending: "效果待验收：声音、读音、口型和动作自然度尚未确认。",
    needs_revision: "效果需修改：此版本未通过效果验收。",
  };
  notice("qualityStatus", res.video_url && !res.placeholder
    ? qualityMessages[res.quality_review] || "未记录效果验收结果。生成完成不代表效果已通过。"
    : "");
}

function copyText(id) {
  navigator.clipboard.writeText($(id).textContent);
}

// 启动
for (const id of ["link", "sourceCopy", "industry", "extra", "voice", "speed", "voiceRefText"]) {
  $(id).addEventListener("input", saveDraft);
  $(id).addEventListener("change", saveDraft);
}
window.addEventListener("pagehide", saveDraft);
setupMediaStates();
loadPublic().then(boot).catch(e => { $("authErr").textContent = "页面加载失败：" + e.message; });
