let TOKEN = localStorage.getItem("token") || "";
let PUB = null;
let MODE = "auto";
let VOICEMODE = "preset";
let AVATAR = "a1";
let authMode = "login";
let pollTimer = null;

const $ = (id) => document.getElementById(id);
async function api(path, opts = {}) {
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || "请求失败");
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
  }
}
function logout() {
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
  const names = ["文案生成", "改写文案", "音频生成", "数字人形象", "成片包装", "最终成片"];
  $("steps").innerHTML = names.map((n, i) =>
    `<div class="step" id="st${i + 1}"><div class="dot">${i + 1}</div><div class="nm">${n}</div></div>`).join("");
}
function pickAvatar(id) {
  AVATAR = id;
  document.querySelectorAll(".avatar").forEach(el =>
    el.classList.toggle("on", el.dataset.id === id));
}
function setMode(m) {
  MODE = m;
  $("modeAuto").classList.toggle("on", m === "auto");
  $("modeStep").classList.toggle("on", m === "step");
}

async function boot() {
  try {
    const me = await api("/api/me");
    $("authMask").classList.add("hidden");
    $("appMain").classList.remove("hidden");
    $("creditNum").textContent = me.credits;
    $("hello").textContent = "👤 " + me.username;
    if (me.is_admin) $("adminLink").classList.remove("hidden");
    if (me.has_face) showFace(me.face_url);
    if (me.has_voice) showVoice(me.voice_url);
    // 本地对口型可用时，提示底版的重要性
    if (PUB.lipsync_ready) $("faceReq").textContent = "（上传后即真·对口型）";
    // 克隆引擎就绪与否的提示
    if (PUB.voiceclone_ready) $("cloneEngineTip").classList.add("hidden");
  } catch (e) {
    $("authMask").classList.remove("hidden");
    $("appMain").classList.add("hidden");
  }
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
}

function showVoice(url) {
  $("voiceEmpty").classList.add("hidden");
  const a = $("voicePreview");
  a.src = url; a.classList.remove("hidden");
  $("voiceStatus").textContent = "✅ 声音样本已上传";
  $("voiceStatus").className = "face-status ok";
}

async function uploadVoice() {
  const f = $("voiceFile").files[0];
  if (!f) return;
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
    showVoice(d.voice_url + "?t=" + Date.now());
  } catch (e) {
    $("voiceStatus").textContent = "❌ " + e.message;
  }
}

async function uploadFace() {
  const f = $("faceFile").files[0];
  if (!f) return;
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
  if (!$("link").value.trim()) { alert("请粘贴爆款视频链接"); return; }
  const cost = PUB.credits_per_video;
  if (!confirm(`本次生成将消耗 ${cost} 点数，确认开始？`)) return;

  const body = {
    link: $("link").value.trim(),
    industry: $("industry").value.trim(),
    extra: $("extra").value.trim(),
    voice: $("voice").value,
    speed: parseFloat($("speed").value),
    avatar: AVATAR,
    mode: MODE,
    use_clone: VOICEMODE === "clone",
  };
  $("goBtn").disabled = true;
  $("emptyState").classList.add("hidden");
  $("resultArea").classList.add("hidden");
  $("running").classList.remove("hidden");
  $("progBar").style.width = "3%";
  $("runMsg").textContent = "任务已提交…";

  try {
    const d = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    $("creditNum").textContent = d.credits;
    poll(d.job_id);
  } catch (e) {
    $("runMsg").textContent = "❌ " + e.message;
    $("goBtn").disabled = false;
  }
}

function poll(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    let j;
    try { j = await api("/api/jobs/" + jobId); }
    catch (e) { return; }
    $("progBar").style.width = j.progress + "%";
    $("runMsg").textContent = j.message;
    setSteps(j.step, j.status);
    renderResult(j.result);

    if (j.status === "done" || j.status === "failed") {
      clearInterval(pollTimer);
      $("goBtn").disabled = false;
      if (j.status === "failed") $("runMsg").textContent = "❌ " + j.message;
    }
  }, 1500);
}

function renderResult(res) {
  if (!res) return;
  const has = Object.keys(res).length > 0;
  if (has) $("resultArea").classList.remove("hidden");
  if (res.transcript) $("vTranscript").textContent = res.transcript;
  if (res.rewritten) $("vRewritten").textContent = res.rewritten;
  if (res.audio_url) $("vAudio").src = res.audio_url;
  if (res.video_url) {
    $("vVideo").src = res.video_url;
    $("dlVideo").href = res.video_url;
    $("phTip").classList.toggle("hidden", !res.placeholder);
  }
}

function copyText(id) {
  navigator.clipboard.writeText($(id).textContent);
}

// 启动
loadPublic().then(boot);
