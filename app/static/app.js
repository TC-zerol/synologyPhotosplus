/* Synology Photos+ 前端逻辑（原生 JS，无依赖） */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
let CFG = null;
let LOG_LAST_ID = 0;
let searchSeq = 0;

/* ---------------- 基础请求 ---------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...opts,
  });
  if (res.status === 401) { $("#login").classList.remove("hidden"); throw new Error("需要登录"); }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}
const GET = (p) => api(p);
const POST = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.style.borderColor = isErr ? "rgba(248,113,113,.5)" : "";
  t.classList.remove("hidden");
  clearTimeout(t._tm);
  t._tm = setTimeout(() => t.classList.add("hidden"), 3200);
}

/* ---------------- 下拉框（Tom Select，成熟组件，深色皮肤见 style.css） ---------------- */
function initSelects() {
  if (typeof TomSelect === "undefined") { console.error("TomSelect 未加载"); return; }
  document.querySelectorAll("select").forEach((sel) => {
    if (sel.tomselect) return;
    sel.tomselect = new TomSelect(sel, {
      controlInput: null,        // 不显示搜索输入框，纯下拉
      maxOptions: 1000,
      openOnFocus: true,
      allowEmptyOption: true,
    });
  });
}
function refreshSelects() {
  // 选项被动态重建（如模型列表）后调用，让组件重新读取 <option>
  document.querySelectorAll("select").forEach((s) => s.tomselect && s.tomselect.sync());
}

/* ---------------- 登录 ---------------- */
async function tryLogin(pw) {
  try {
    await POST("/api/login", { password: pw });
    $("#login").classList.add("hidden");
    boot();
  } catch (e) { $("#login-err").classList.remove("hidden"); }
}
$("#login-btn").onclick = () => tryLogin($("#login-pw").value);
$("#login-pw").addEventListener("keydown", (e) => e.key === "Enter" && tryLogin(e.target.value));

/* ---------------- 导航 ---------------- */
$$(".nav-item").forEach((el) => {
  el.onclick = () => {
    $$(".nav-item").forEach((x) => x.classList.remove("active"));
    $$(".tab").forEach((x) => x.classList.remove("active"));
    el.classList.add("active");
    $("#tab-" + el.dataset.tab).classList.add("active");
    if (el.dataset.tab === "logs") refreshLogs(true);
    if (el.dataset.tab === "db") loadBackups();
    if (el.dataset.tab === "recent") loadRecent();
  };
});

/* ---------------- 工具 ---------------- */
function fmtDur(sec) {
  if (sec == null) return "";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}时${m}分` : m ? `${m}分${s}秒` : `${s}秒`;
}
function ts(t) { return new Date(t * 1000).toLocaleTimeString("zh-CN"); }

/* ---------------- 启动 ---------------- */
async function boot() {
  try {
    const data = await GET("/api/bootstrap");
    CFG = data.config;
    renderStats(data);
    renderJob(data.status);
    renderTopTags(data.top_tags);
    fillModels(data.models);
    renderModels(data.models, CFG);
    fillSettings(CFG);
    fillDb(CFG);
    fillMounts(data.mounts);
    fillVocab(data.vocab);
    refreshSelects();
    $("#foot-status").textContent = "已连接";
    $("#ws-dot").classList.remove("bad");
  } catch (e) {
    $("#foot-status").textContent = "连接失败";
    $("#ws-dot").classList.add("bad");
    if (!String(e).includes("登录")) toast(e.message || e, true);
  }
}


/* ---------------- 模型列表渲染 ---------------- */
function fmtSize(n) {
  return n >= (1 << 20) ? (n / (1 << 20)).toFixed(0) + " MB"
       : n >= 1024 ? (n / 1024).toFixed(0) + " KB" : n + " B";
}
function renderModels(models, cfg) {
  const el = $("#model-lists");
  if (!el) return;
  const row = (m, inUse, kind) => `
    <div class="row" style="display:flex;gap:10px;align-items:center;padding:6px 10px;border-radius:8px;background:rgba(255,255,255,.03);margin-bottom:5px">
      <span class="mono small">${m.name}</span>
      <span class="muted small">${fmtSize(m.size)}</span>
      ${inUse ? '<span class="chip ok">使用中</span>' : ""}
    </div>`;
  const sec = (title, items, current) =>
    `<div style="margin-bottom:10px"><b class="small">${title}</b>` +
    (items.length ? items.map((m) => row(m, m.name === current)).join("")
                  : '<div class="muted small">无</div>') + "</div>";
  el.innerHTML =
    sec("物体检测（YOLO）", models.yolo || [], cfg.detect.model) +
    sec("语义识别（CLIP）", models.clip || [], cfg.clip.model) +
    '<p class="hint">"使用中"对应识别设置里的当前选择。上传新模型到 /config/models 后也会出现在这里。</p>';
}

/* ---------------- 仪表盘 ---------------- */
function renderStats(data) {
  const s = data.stats;
  $("#st-processed").textContent = (s.written ?? 0) + (s.empty ?? 0);
  $("#st-written-sub").textContent =
    `成功 ${s.written ?? 0} · 无标签 ${s.empty ?? 0} · 共记录 ${s.processed ?? 0}`;
  $("#st-tags").textContent = s.tag_links ?? "–";
  $("#st-tags-sub").textContent = (s.tag_links ?? 0) === 0 && (s.pending_write ?? 0) > 0
    ? "有结果待落库（Dry-run 或写库失败？）" : "general_tag 关联关系";
  const exhausted = s.error_exhausted ?? 0;
  $("#st-pending").textContent = (s.pending_write ?? 0) + (s.error ?? 0);
  $("#st-pending-sub").textContent = `待补写 ${s.pending_write ?? 0} · 失败 ${s.error ?? 0}`
    + (exhausted > 0 ? `（${exhausted} 已放弃重试）` : "");
  $("#btn-retry-failed").style.display = exhausted > 0 ? "" : "none";
  $("#st-files").textContent = s.embeddings ?? "–";
  // 过期提示条（已入库的旧结果需手动替换；待写/失败的旧结果自动重跑）
  const stale = data.stale || 0;
  const stalePending = data.stale_pending || 0;
  $("#stale-banner").classList.toggle("hidden", stale <= 0 && stalePending <= 0);
  $("#stale-count").textContent = stale;
  $("#stale-pending-note").textContent =
    stalePending > 0 ? `另有 ${stalePending} 条待写结果也来自旧模型（下次扫描自动重新分析）。` : "";
}
$("#btn-stale-replace").onclick = async () => {
  if (!confirm("将移除本工具已写入的全部标签，并按当前模型/词表全量重新分析。继续？")) return;
  try { await POST("/api/job", { type: "replace", confirm: "REPLACE" }); toast("开始重新分析"); }
  catch (e) { toast(e.message, true); }
};

function renderJob(status) {
  const j = status.running, last = status.last;
  $("#btn-cancel").disabled = !j;
  if (j) {
    $("#job-empty").classList.add("hidden");
    $("#job-box").classList.remove("hidden");
    $("#job-type").textContent = { incremental: "增量扫描", replace: "重新分析", write_pending: "补写标签" }[j.type] || j.type;
    $("#job-phase").textContent = { enumerate: "读取数据库", index: "索引文件", plan: "规划任务", analyze: "分析中", write: "写库中", cleanup: "清理旧标签", done: "完成" }[j.phase] || j.phase;
    const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
    $("#job-bar").style.width = pct + "%";
    $("#job-count").textContent = `${j.done} / ${j.total}（${pct}%）`;
    $("#job-cur").textContent = j.current || "";
    $("#job-eta").textContent = j.eta_sec != null && j.phase === "analyze" ? "预计剩余 " + fmtDur(j.eta_sec) : "";
    $("#job-stats").textContent =
      `匹配 ${j.matched} · 未匹配 ${j.unmatched} · 已写标签(累计) ${j.tags_written} · 错误 ${j.errors}`;
  } else {
    $("#job-empty").classList.remove("hidden");
    $("#job-box").classList.add("hidden");
    if (last) {
      const cls = last.error ? "err" : (last.canceled ? "" : "ok");
      $("#last-job").innerHTML =
        `<span class="chip ${cls}">${last.type}</span> ` +
        `${ts(last.finished || last.started)} · 处理 ${last.done}/${last.total} · 标签 ${last.tags_written} · 错误 ${last.errors}` +
        (last.error ? `<div class="small" style="color:var(--err);margin-top:6px">${last.error}</div>` : "");
    }
  }
}

function renderTopTags(tags) {
  const el = $("#top-tags");
  if (!tags || !tags.length) { el.innerHTML = '<span class="muted">暂无数据</span>'; return; }
  el.innerHTML = tags.map((t) =>
    `<span class="tag">${t.name} <b>${t.count}</b></span>`).join("");
}

$("#btn-scan").onclick = async () => {
  try { await POST("/api/job", { type: "incremental" }); toast("已开始增量扫描"); tick(); }
  catch (e) { toast(e.message, true); }
};
$("#btn-write").onclick = async () => {
  try { await POST("/api/job", { type: "write_pending" }); toast("开始补写标签"); tick(); }
  catch (e) { toast(e.message, true); }
};
$("#btn-replace").onclick = async () => {
  if (!confirm("重新分析会先移除本工具已写入的全部标签，再按当前模型/设置全量重跑。继续？")) return;
  try { await POST("/api/job", { type: "replace", confirm: "REPLACE" }); toast("开始重新分析"); tick(); }
  catch (e) { toast(e.message, true); }
};
$("#btn-cancel").onclick = async () => { await POST("/api/job/cancel"); toast("已请求取消，将在当前文件处理后停止"); };
$("#btn-retry-failed").onclick = async () => {
  if (!confirm("将清除失败项的重试计数并重新扫描这些照片（此前已重试 3 次未成功）。继续？")) return;
  try {
    const r = await POST("/api/job", { type: "retry_failed" });
    toast(`已重置 ${r.reset} 个失败项，开始重新扫描`);
    tick();
  } catch (e) { toast(e.message, true); }
};

/* ---------------- 设置 ---------------- */
function fillModels(models) {
  const y = $("#cfg-detect-model"), c = $("#cfg-clip-model");
  // models 现在是 [{name, size, mtime}]；兼容旧版纯文件名数组
  const toOptions = (items) => (items || []).map((m) => {
    const name = String((m && m.name) ?? m);
    return { value: name, text: name };
  });
  setSelectOptions(y, toOptions(models.yolo));
  setSelectOptions(c, toOptions(models.clip));
}

function setSelectOptions(sel, options) {
  if (sel.tomselect) {
    const current = sel.tomselect.getValue();
    sel.tomselect.clear(true);
    sel.tomselect.clearOptions();
    sel.tomselect.addOptions(options);
    sel.tomselect.refreshOptions(false);
    if (current) sel.tomselect.setValue(current, true);
    return;
  }
  const opt = (o) => `<option value="${escapeHtml(o.value)}">${escapeHtml(o.text)}</option>`;
  sel.innerHTML = options.map(opt).join("");
}

function setSelectValue(sel, value) {
  value = value == null ? "" : String(value);
  if (sel.tomselect) {
    if (!sel.tomselect.options[value] && value) {
      sel.tomselect.addOption({ value, text: value });
    }
    sel.tomselect.setValue(value, true);
  } else {
    sel.value = value;
  }
}

function fillSettings(cfg) {
  $("#cfg-detect-enabled").checked = cfg.detect.enabled;
  setSelectValue($("#cfg-detect-model"), cfg.detect.model);
  $("#cfg-detect-conf").value = cfg.detect.confidence;
  $("#v-detect-conf").textContent = Math.round(cfg.detect.confidence * 100) + "%";
  $("#cfg-detect-max").value = cfg.detect.max_tags;
  $("#cfg-clip-enabled").checked = cfg.clip.enabled;
  setSelectValue($("#cfg-clip-model"), cfg.clip.model);
  $("#cfg-clip-max").value = cfg.clip.max_tags;
  $("#cfg-clip-prob").value = cfg.clip.prob_thr;
  $("#v-clip-prob").textContent = Math.round(cfg.clip.prob_thr * 100) + "%";
  $("#cfg-clip-sim").value = cfg.clip.sim_floor;
  $("#cfg-ocr-enabled").checked = cfg.ocr.enabled;
  $("#cfg-ocr-conf").value = cfg.ocr.confidence;
  $("#cfg-ocr-minlen").value = cfg.ocr.min_kw_len;
  $("#cfg-ocr-maxkw").value = cfg.ocr.max_kw;
  $("#cfg-video-enabled").checked = cfg.video.enabled;
  $("#cfg-video-int").value = cfg.video.sample_interval;
  $("#cfg-video-max").value = cfg.video.max_frames;
  setSelectValue($("#cfg-lang"), cfg.tagging.language);
  $("#cfg-prefix").value = cfg.tagging.tag_prefix;
  $("#cfg-poll").value = cfg.scan.poll_interval_min;
  $("#cfg-batch").value = cfg.scan.batch_size;
  $("#cfg-maxtags").value = cfg.tagging.max_tags_per_photo ?? 15;
  $("#cfg-dryrun").checked = cfg.tagging.dry_run;
  $("#cfg-backup").checked = cfg.tagging.backup_before_write;
}
$("#cfg-detect-conf").oninput = (e) => $("#v-detect-conf").textContent = Math.round(e.target.value * 100) + "%";
$("#cfg-clip-prob").oninput = (e) => $("#v-clip-prob").textContent = Math.round(e.target.value * 100) + "%";

$("#btn-save-settings").onclick = async () => {
  const body = {
    detect: {
      enabled: $("#cfg-detect-enabled").checked,
      model: $("#cfg-detect-model").value,
      confidence: parseFloat($("#cfg-detect-conf").value),
      max_tags: parseInt($("#cfg-detect-max").value),
    },
    clip: {
      enabled: $("#cfg-clip-enabled").checked,
      model: $("#cfg-clip-model").value,
      max_tags: parseInt($("#cfg-clip-max").value),
      prob_thr: parseFloat($("#cfg-clip-prob").value),
      sim_floor: parseFloat($("#cfg-clip-sim").value),
    },
    ocr: {
      enabled: $("#cfg-ocr-enabled").checked,
      confidence: parseFloat($("#cfg-ocr-conf").value),
      min_kw_len: parseInt($("#cfg-ocr-minlen").value),
      max_kw: parseInt($("#cfg-ocr-maxkw").value),
    },
    video: {
      enabled: $("#cfg-video-enabled").checked,
      sample_interval: parseFloat($("#cfg-video-int").value),
      max_frames: parseInt($("#cfg-video-max").value),
    },
    tagging: {
      language: $("#cfg-lang").value,
      tag_prefix: $("#cfg-prefix").value,
      max_tags_per_photo: parseInt($("#cfg-maxtags").value),
      dry_run: $("#cfg-dryrun").checked,
      backup_before_write: $("#cfg-backup").checked,
    },
    scan: {
      poll_interval_min: parseInt($("#cfg-poll").value),
      batch_size: parseInt($("#cfg-batch").value),
    },
  };
  try { CFG = await POST("/api/config", body); toast("设置已保存"); }
  catch (e) { toast(e.message, true); }
};

/* ---------------- 数据库 ---------------- */
function fillDb(cfg) {
  $("#cfg-transport").value = cfg.db.transport;
  $("#cfg-ssh-host").value = cfg.db.ssh.host;
  $("#cfg-ssh-port").value = cfg.db.ssh.port;
  $("#cfg-ssh-user").value = cfg.db.ssh.user;
  $("#cfg-ssh-pw").value = "";
  $("#cfg-ssh-pw").placeholder = cfg.db.ssh.password ? "已设置（留空不变）" : "未设置";
  $("#cfg-ssh-sudo").checked = cfg.db.ssh.use_sudo;
  $("#cfg-tcp-host").value = cfg.db.tcp.host;
  $("#cfg-tcp-port").value = cfg.db.tcp.port;
  $("#cfg-tcp-user").value = cfg.db.tcp.user;
  $("#cfg-tcp-pw").value = "";
  $("#cfg-tcp-pw").placeholder = cfg.db.tcp.password ? "已设置（留空不变）" : "未设置";
  $("#ssh-box").classList.toggle("hidden", cfg.db.transport !== "ssh");
  $("#tcp-box").classList.toggle("hidden", cfg.db.transport !== "tcp");
}
$("#cfg-transport").onchange = (e) => {
  $("#ssh-box").classList.toggle("hidden", e.target.value !== "ssh");
  $("#tcp-box").classList.toggle("hidden", e.target.value !== "tcp");
};

async function saveDb() {
  const body = {
    db: {
      transport: $("#cfg-transport").value,
      ssh: {
        host: $("#cfg-ssh-host").value, port: parseInt($("#cfg-ssh-port").value),
        user: $("#cfg-ssh-user").value, use_sudo: $("#cfg-ssh-sudo").checked,
      },
      tcp: {
        host: $("#cfg-tcp-host").value, port: parseInt($("#cfg-tcp-port").value),
        user: $("#cfg-tcp-user").value,
      },
    },
  };
  if ($("#cfg-ssh-pw").value) body.db.ssh.password = $("#cfg-ssh-pw").value;
  if ($("#cfg-tcp-pw").value) body.db.tcp.password = $("#cfg-tcp-pw").value;
  CFG = await POST("/api/config", body);
  toast("连接配置已保存");
}
$("#btn-save-db").onclick = () => saveDb().catch((e) => toast(e.message, true));

$("#btn-test-db").onclick = async () => {
  const btn = $("#btn-test-db");
  btn.disabled = true;
  const box = $("#db-test-result");
  box.classList.remove("hidden");
  box.innerHTML = '<div class="muted small">测试中…（枚举数据库与 unit 表，可能需要十几秒）</div>';
  try {
    await saveDb();
    await POST("/api/db/test");
    pollBg("dbtest", (r) => {
      if (!r.result) return;
      const steps = r.result.steps || [];
      box.innerHTML = steps.map((s) =>
        `<div class="row ${s.ok ? "" : "bad"}">${s.ok ? "✔" : "✘"} <b>${s.name}</b><span class="muted">${s.detail}</span></div>`
      ).join("") + (r.result.ok ? '<div class="row">🎉 一切就绪，可以开始扫描</div>' : "");
    }, () => { btn.disabled = false; }, (err) => {
      box.innerHTML = `<div class="row bad">✘ ${err}</div>`;
      btn.disabled = false;
    });
  } catch (e) {
    box.innerHTML = `<div class="row bad">✘ ${e.message}</div>`;
    btn.disabled = false;
  }
};

/* 后台任务轮询：onTick(r) 收到状态，onDone 完成后停止，onErr 出错 */
function pollBg(kind, onTick, onDone, onErr) {
  const t0 = Date.now();
  const tm = setInterval(async () => {
    try {
      const r = await GET(`/api/bg/status?kind=${kind}`);
      onTick(r);
      if (!r.running) {
        clearInterval(tm);
        if (r.error && onErr) onErr(r.error);
        else onDone && onDone(r);
      }
    } catch (e) {
      if (Date.now() - t0 > 120000) { clearInterval(tm); onErr && onErr(e.message); }
    }
  }, 1500);
}

$("#btn-backup").onclick = async () => {
  const btn = $("#btn-backup");
  btn.disabled = true;
  btn.textContent = "备份中…";
  try {
    await POST("/api/db/backup");
    pollBg("backup", () => {}, (r) => {
      btn.disabled = false;
      btn.textContent = "立即备份";
      const files = (r.result && r.result.files) || [];
      toast("备份完成：" + files.map((f) => f.split("/").pop()).join(", "));
      loadBackups();
    }, (err) => {
      btn.disabled = false;
      btn.textContent = "立即备份";
      toast("备份失败：" + err, true);
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "立即备份";
    toast(e.message, true);
  }
};

/* ---------------- 备份列表与还原 ---------------- */
async function loadBackups() {
  const el = $("#backup-list");
  try {
    const r = await GET("/api/db/backups");
    if (!r.files.length) {
      el.innerHTML = '<span class="muted">暂无备份。点"立即备份"，或首次正式写库前会自动备份。</span>';
      return;
    }
    el.innerHTML = r.files.map((f) => {
      const db = f.file.split("_").slice(0, -2).join("_");
      return `<div class="row" style="display:flex;gap:10px;align-items:center;padding:6px 10px;border-radius:8px;background:rgba(255,255,255,.03);margin-bottom:5px">
        <span class="mono small">${f.file}</span>
        <span class="muted small">${fmtSize(f.size)} · ${new Date(f.mtime * 1000).toLocaleString("zh-CN")}</span>
        <span class="chip ghost">${db}</span>
        <button class="btn danger" style="margin-left:auto;padding:4px 12px" onclick="restoreBackup('${f.file}')">还原此备份</button>
      </div>`;
    }).join("");
  } catch (e) { el.innerHTML = `<span class="bad">加载失败：${e.message}</span>`; }
}
$("#btn-backup-refresh").onclick = loadBackups;

async function restoreBackup(file) {
  const input = prompt(
    `高危操作！用 ${file} 覆盖还原对应数据库，现有数据将被替换。\n` +
    `还原期间请勿运行扫描。确认请输入完整文件名：`);
  if (input !== file) return toast("已取消（文件名不匹配）", true);
  try {
    await POST("/api/db/restore", { file, confirm: file });
    toast("还原已在后台开始，完成后可在日志中确认");
    pollBg("restore", () => {}, () => {
      toast("还原完成 ✓ 建议到 Synology Photos 里抽查搜索结果");
    }, (err) => toast("还原失败：" + err, true));
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 挂载 ---------------- */
function fillMounts(mounts) {
  $("#cfg-mounts").value = (CFG.mounts || []).map((m) => m.path).join("\n");
  $("#mount-status").innerHTML = mounts.map((m) =>
    `<div class="${m.exists ? "ok" : "bad"}">${m.exists ? "✔" : "✘"} ${m.path}</div>`).join("");
}
$("#btn-save-mounts").onclick = async () => {
  const paths = $("#cfg-mounts").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const seen = new Set();
  const mounts = paths.filter((p) => !seen.has(p) && seen.add(p))
    .map((p) => ({ name: p.split("/").pop() || p, path: p }));
  CFG = await POST("/api/config", { mounts });
  toast("挂载配置已保存");
  boot();
};

/* ---------------- 模型与词表 ---------------- */
function fillVocab(vocab) {
  const lines = vocab.tags.map((t) => `${t.zh},${t.en}`).join("\n");
  $("#cfg-vocab").value = lines;
  $("#vocab-count").textContent = `${vocab.tags.length} 类 · v${vocab.version}`;
}
$("#btn-save-vocab").onclick = async () => {
  const tags = $("#cfg-vocab").value.split("\n").map((l) => l.trim())
    .filter(Boolean).map((l) => {
      const [zh, en] = l.split(/[,,]/);
      return { zh: (zh || "").trim(), en: (en || "").trim() };
    });
  try {
    const r = await PUT("/api/vocab", { tags });
    toast(`词表已保存：${r.count} 类 v${r.version}`);
    boot();
  } catch (e) { toast(e.message, true); }
};
const PUT = (p, body) => api(p, { method: "PUT", body: JSON.stringify(body || {}) });

$("#btn-upload").onclick = async () => {
  const f = $("#model-file").files[0];
  if (!f) return toast("先选择 .onnx 文件", true);
  const fd = new FormData();
  fd.append("file", f);
  const res = await fetch("/api/models/upload", { method: "POST", body: fd });
  if (res.ok) { toast("模型已上传"); boot(); }
  else toast("上传失败", true);
};


/* ---------------- 结果预览（实时检查打标效果） ---------------- */
function tagChip(t) {
  if (typeof t === "string") return `<span>${t}</span>`;
  const pct = (t.s != null && !isNaN(t.s)) ? ` ${Math.round(t.s * 100)}%` : "";
  return `<span>${t.n}${pct}</span>`;
}
const STATUS_CN = {written: "已写库", analyzed: "待写库", empty: "无标签",
                   write_error: "写库失败", error: "分析失败", dryrun: "Dry-run"};
function statusChip(s) {
  const cls = s === "written" ? "ok" : (s === "error" || s === "write_error") ? "err" : "";
  return `<span class="chip ${cls}">${STATUS_CN[s] || s}</span>`;
}
let recentSig = "";
async function loadRecent() {
  const el = $("#recent-results");
  const status = $("#recent-status").value;
  try {
    const r = await GET(`/api/recent?limit=60&status=${encodeURIComponent(status)}`);
    // 数据没变化就不重绘（避免每 2 秒重建缩略图请求）
    const sig = r.items.map((i) => i.unit_id + ":" + i.status + ":" + (i.processed_at || 0)).join("|");
    if (sig === recentSig && el.childElementCount) return;
    recentSig = sig;
    if (!r.items.length) {
      el.innerHTML = '<div class="muted" style="grid-column:1/-1">暂无分析记录——点"立即扫描"后这里会实时出现结果</div>';
      return;
    }
    el.innerHTML = r.items.map((it) => {
      const tags = (it.tags || []).map((t) => tagChip(t)).join("");
      return `<div class="result">
        <a href="${it.thumb}" target="_blank" rel="noopener">
          <img class="thumb" loading="lazy" src="${it.thumb}" onerror="this.style.opacity=.15">
        </a>
        <div class="meta">
          ${statusChip(it.status)}
          <div class="tags" style="margin-top:5px">${tags || '<span class="muted">无标签</span>'}</div>
        </div></div>`;
    }).join("");
  } catch (e) {}
}
$("#recent-status").onchange = () => { recentSig = ""; loadRecent(); };

/* ---------------- 搜索 ---------------- */
$("#btn-search").onclick = doSearch;
$("#search-q").addEventListener("keydown", (e) => e.key === "Enter" && doSearch());

async function doSearch() {
  const q = $("#search-q").value.trim();
  if (!q) return;
  const seq = ++searchSeq;
  $("#search-mode-chip").textContent = "搜索中…";
  try {
    const r = await GET(`/api/search?q=${encodeURIComponent(q)}&limit=60`);
    if (seq !== searchSeq) return;
    $("#search-mode-chip").textContent =
      r.mode === "semantic" ? "CLIP 语义匹配" : "标签/OCR 关键词";
    const el = $("#search-results");
    if (!r.items.length) {
      el.innerHTML = '<div class="muted" style="grid-column:1/-1">无结果（未分析的图片不会出现）</div>';
      return;
    }
    el.innerHTML = r.items.map((it) => {
      const tags = (it.tags || []).map((t) => tagChip(t)).join("");
      return `<div class="result">
        <img class="thumb" loading="lazy" src="${it.thumb}" onerror="this.style.opacity=.15">
        <div class="meta">
          <div class="score">${it.score != null ? "相关度 " + it.score : ""}</div>
          <div class="tags">${tags}</div>
        </div></div>`;
    }).join("");
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 日志（tail -f 式：时间正序，自动滚动到最新） ---------------- */
async function refreshLogs(reset) {
  const el = $("#log-view");
  if (reset) { el.innerHTML = ""; LOG_LAST_ID = 0; }
  // 用户滚到底部附近（或刚打开）才自动跟随最新；回看历史时不打扰
  const stick = reset ||
    (el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  try {
    const r = await GET(`/api/logs?after_id=${LOG_LAST_ID}&limit=300`);
    if (!r.rows.length) {
      if (reset && !el.childElementCount)
        el.innerHTML = '<div class="info">暂无日志</div>';
      return;
    }
    LOG_LAST_ID = Math.max(LOG_LAST_ID, ...r.rows.map((x) => x.id));
    const block = r.rows.slice().reverse()   // 接口返回倒序 → 转为时间正序
      .map((x) => `<div class="${x.level}"><span class="ts">${ts(x.ts)}</span>${escapeHtml(x.msg)}</div>`)
      .join("");
    const wasEmpty = !el.childElementCount;
    el.insertAdjacentHTML("beforeend", block);   // 追加到末尾（正序）
    if (wasEmpty) el.querySelectorAll(".info").forEach((n) => n.remove());
    while (el.childElementCount > 800) el.removeChild(el.firstChild);
    if (stick) el.scrollTop = el.scrollHeight;
  } catch (e) {}
}
function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
$("#btn-clear-view").onclick = () => { $("#log-view").innerHTML = ""; };

/* ---------------- 轮询（轻量接口，避免高频轮询拖累服务） ---------------- */
async function tick() {
  if (!$("#login").classList.contains("hidden")) return;
  try {
    const st = await GET("/api/status");
    try { renderStats(st); } catch (e) { console.error("renderStats", e); }
    try { renderJob(st.status); } catch (e) { console.error("renderJob", e); }
    if ($("#tab-logs").classList.contains("active")) refreshLogs(false);
    if ($("#tab-recent").classList.contains("active")
        && $("#recent-auto").checked) loadRecent();
    $("#foot-status").textContent = "已连接";
    $("#ws-dot").classList.remove("bad");
  } catch (e) {
    $("#foot-status").textContent = "连接断开";
    $("#ws-dot").classList.add("bad");
  }
}


initSelects();
boot();
setInterval(tick, 2000);   // tick 内含：日志页激活时增量拉取并跟随滚动
