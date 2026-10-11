const CRED_KEY = "n1bus_session", THEME_KEY = "n1bus_theme";
let TOKEN = new URLSearchParams(location.search).get("token") || localStorage.getItem(CRED_KEY) || "";
let ME = "", AUTH = {}, META = {}, ACTIONS = [], UNITS = [], DK_CONTAINERS = [],
    curTab = "overview", pollTimer = null,
    // 用户是否已自行切换过视图。boot() 收尾那次 showView("overview") 只负责「开屏落位」，
    // 一旦它慢于用户操作（首屏需并行预热 11 个 loader，耗时数秒），
    // 无条件执行会把用户刚打开的页面切回主页 —— 该标记就是这道闸。
    viewPinned = false;

/* --- 工具 ------------------------------------------------------------- */
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = n => (n ?? 0).toLocaleString("zh-CN");
function bytes(n){
  if(n == null) return "–";
  const u = ["B","KB","MB","GB","TB"]; let i = 0; n = Number(n);
  while(n >= 1024 && i < u.length-1){ n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i];
}
const rate = n => bytes(n) + "/s";
function dur(sec){
  sec = Number(sec || 0);
  const d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600), m = Math.floor(sec%3600/60);
  if(d) return d + " 天 " + h + " 小时";
  if(h) return h + " 小时 " + m + " 分";
  return m + " 分";
}
/* 手机端把宽表变成「字段: 值」的卡片：给每个单元格补上它那列的表头文字。
   只加属性、不改结构，所以各处渲染函数一行都不用动，轮询出的新行也会被自动标记。 */
function labelTables(){
  document.querySelectorAll("table").forEach(t => {
    const heads = Array.prototype.map.call(t.querySelectorAll("thead th"),
      th => (th.textContent || "").trim());
    if(!heads.length) return;
    t.querySelectorAll("tbody td").forEach(td => {
      if(td.hasAttribute("data-label") || td.hasAttribute("colspan")) return;
      const i = Array.prototype.indexOf.call(td.parentNode.children, td);
      if(heads[i]) td.setAttribute("data-label", heads[i]);
    });
  });
}
/* 折叠卡片的标题行只留标题与状态数字：说明文字（summary 里的 .q）直接移除。
   CSS 里那条 details.fold:not([open])>summary .q{display:none} 仅作为降级保护
   （JS 尚未执行时避免露出内容），并非「展开即应显示」。
   因此向 summary 的 .q 写入内容不会生效，会被此处移除。
   真要展示的摘要，写进 .foldbody。 */
function stripFoldNotes(){
  document.querySelectorAll("details.fold > summary > .q").forEach(q => q.remove());
}
labelTables();
stripFoldNotes();
let _labelPending = false;
new MutationObserver(() => {
  if(_labelPending) return;
  _labelPending = true;
  requestAnimationFrame(() => { labelTables(); stripFoldNotes(); _labelPending = false; });
}).observe(document.body, {childList: true, subtree: true});

/* 状态标只显示简写，完整原文放 data-full：桌面悬停 / 手机按住都能看 */
const ACT_CN = {active: "运行", failed: "失败", inactive: "未运行", activating: "启动中",
                deactivating: "停止中", reloading: "重载中"};
const SUB_CN = {running: "运行中", exited: "已退出", dead: "已停止", failed: "失败",
                waiting: "等待中", listening: "监听中", start: "启动中", stop: "停止中",
                mounted: "已挂载", plugged: "已插入"};
const POL_CN = {always: "总是", "unless-stopped": "除非手动停", "on-failure": "失败时",
                no: "不自动", "": "不自动"};
/* 状态与子状态合并显示：绿=运行、黄=退出、红=失败、灰=停止；原文留在 data-full */
function svcState(u){
  const a = String((u && u.active) || ""), s = String((u && u.sub) || "");
  if(a === "failed" || s === "failed") return {t: "失败", c: "bad"};
  if(s === "running") return {t: "运行", c: "ok"};
  if(s === "exited") return {t: "退出", c: "warn"};
  if(a === "activating" || s === "start") return {t: "启动中", c: "warn"};
  if(a === "deactivating" || s === "stop") return {t: "停止中", c: "warn"};
  if(a === "active") return {t: "运行", c: "ok"};
  return {t: "停止", c: "mute"};
}
function dot(kind, text){
  return `<span class="dot-tag ${kind}" data-full="${esc(text)}"></span>`;
}
/* 点状态色点时别触发所在行的点击（例如服务表整行可点开详情） */
document.addEventListener("click", e => {
  if(e.target && e.target.closest && e.target.closest(".dot-tag")) e.stopPropagation();
}, true);
/* 日志行只留「时间 + 来源」；完整原文进 data-full，点击/长按查看 */
function logParts(s){
  const str = String(s || "");
  let m = str.match(/^([A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+(\S+)\s+([^\s:]+)(?:\[\d+\])?:\s*([\s\S]*)$/);
  if(m) return {t: m[1], src: m[3], msg: m[4]};
  m = str.match(/^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\S*\s+([\s\S]*)$/);
  if(m) return {t: m[1].replace("T", " ").slice(5), src: m[2], msg: m[2]};
  return {t: "", src: "", msg: str};
}
function logLine(text){
  const p = logParts(text);
  const warn = /error|fail|denied|refused/i.test(String(text)) ? " warn" : "";
  const src = p.src || (p.t ? "" : String(text || ""));
  return `<div class="logline${warn}" data-full="${esc(text)}" style="cursor:pointer">`
    + (p.t ? `<span class="t">${esc(p.t)}</span>` : "")
    + `<span class="src">${esc(src)}</span></div>`;
}
function dkState(c){
  const s = String((c && (c.status || c.state)) || "");
  if(c && c.running) return {t: "运行中", c: "ok"};
  if(/restart/i.test(s)) return {t: "重启中", c: "warn"};
  if(/paused/i.test(s)) return {t: "已暂停", c: "warn"};
  if(/created/i.test(s)) return {t: "未启动", c: "mute"};
  if(s) return {t: "已停止", c: "mute"};
  return {t: "未知", c: "mute"};
}
(function(){
  const tip = document.createElement("div");
  tip.className = "tip glass hidden";
  document.body.appendChild(tip);
  let timer = null;
  function hide(){
    tip.classList.add("hidden");
    if(timer){ clearTimeout(timer); timer = null; }
  }
  function show(el){
    const txt = el.getAttribute("data-full");
    if(!txt) return;
    tip.textContent = txt;
    tip.classList.remove("hidden");
    const r = el.getBoundingClientRect();
    const w = tip.offsetWidth;
    tip.style.left = Math.max(8, Math.min(document.documentElement.clientWidth - w - 8,
                                          r.left + r.width / 2 - w / 2)) + "px";
    tip.style.top = (r.bottom + window.scrollY + 6) + "px";
  }
  document.addEventListener("touchstart", e => {
    const el = e.target.closest && e.target.closest("[data-full]");
    if(!el) return;
    timer = setTimeout(() => show(el), 420);          // 按住 0.42 秒
  }, {passive: true});
  ["touchend", "touchmove", "touchcancel", "scroll"].forEach(ev =>
    document.addEventListener(ev, hide, {passive: true}));
  document.addEventListener("click", e => {
    const el = e.target.closest && e.target.closest("[data-full]");
    if(!el) return;
    show(el);
    clearTimeout(tip._t);
    tip._t = setTimeout(hide, 3500);      // 点击查看：几秒后自动收起
  });
  document.addEventListener("mouseover", e => {
    const el = e.target.closest && e.target.closest("[data-full]");
    if(el) show(el);
  });
  document.addEventListener("mouseout", e => {
    if(e.target.closest && e.target.closest("[data-full]")) hide();
  });
})();

function toast(msg, isErr){
  const el = document.getElementById("toast");
  document.getElementById("toastMsg").innerHTML = msg;
  el.querySelector("use").setAttribute("href", isErr ? "#i-x-circle" : "#i-check-circle");
  el.className = "toast glass show" + (isErr ? " err" : "");
  clearTimeout(el._h);
  el._h = setTimeout(() => el.className = "toast glass", 4200);
}

/* --- 询问框 -----------------------------------------------------------
   网页风格的 confirm / alert / prompt 替代实现。原生弹窗无法定制样式，与页面风格不一致。
   同一个 #ask 三种角色：danger 换红色确认键，input 露出输入框，alert 藏掉取消。 */
let _askResolve = null;
function _askOpen(opts){
  const box = document.getElementById("ask");
  if(!box) return Promise.resolve(null);
  box.classList.toggle("danger", !!opts.danger);
  document.getElementById("askTitle").textContent = opts.title || "确认";
  document.getElementById("askBody").innerHTML = opts.body || "";
  document.getElementById("askIco").querySelector("use")
    .setAttribute("href", opts.danger ? "#i-alert" : "#i-check-circle");
  const inp = document.getElementById("askInput");
  inp.classList.toggle("hidden", !opts.input);
  if(opts.input) inp.value = opts.value || "";
  // 多字段表单：每个 field = {key, label, type, placeholder, value}。
  // 用它就不必把「新增账户」「修改密码」这类表单常驻页面占用版面 —— 列表页应保持
  // 「列表 + 按钮」形态，表单放入弹窗（参考 1Panel 的做法）。
  const fbox = document.getElementById("askFields");
  const fields = opts.fields || [];
  fbox.classList.toggle("hidden", !fields.length);
  fbox.innerHTML = fields.map(f =>
    `<div class="field gap-xs"><label>${esc(f.label || "")}</label>
      <input data-key="${esc(f.key)}" type="${esc(f.type || "text")}"
             placeholder="${esc(f.placeholder || "")}" value="${esc(f.value || "")}"
             autocomplete="off"></div>`).join("");
  document.getElementById("askCancel").classList.toggle("hidden", !!opts.alert);
  const ok = document.getElementById("askOk");
  ok.textContent = opts.okText || "确定";
  box.classList.add("show");
  setTimeout(() => {
    const first = fields.length ? fbox.querySelector("input") : null;
    if(first) first.focus();
    else if(opts.input){ inp.focus(); inp.select(); }
    else ok.focus();
  }, 30);
  return new Promise(res => { _askResolve = res; });
}
function _askClose(val){
  const box = document.getElementById("ask");
  if(box) box.classList.remove("show");
  const r = _askResolve;
  _askResolve = null;
  if(r) r(val);
}
function _askValue(){
  const fbox = document.getElementById("askFields");
  if(fbox && !fbox.classList.contains("hidden")){
    const out = {};
    fbox.querySelectorAll("input").forEach(i => { out[i.dataset.key] = i.value; });
    return out;
  }
  const inp = document.getElementById("askInput");
  return inp.classList.contains("hidden") ? true : inp.value;
}
/** 确认。@returns Promise<boolean> */
function askConfirm(body, opts){ return _askOpen(Object.assign({body}, opts || {})); }
/** 提示（只有「知道了」）。@returns Promise<boolean> */
function askAlert(body, opts){ return _askOpen(Object.assign({body, title:"提示", alert:true}, opts || {})); }
/** 输入。@returns Promise<string|null> */
function askPrompt(body, value, opts){ return _askOpen(Object.assign({body, title:"输入", input:true, value}, opts || {})); }
/** 多字段表单。@returns Promise<{key:value}|null> —— 取消返回 null */
function askForm(body, fields, opts){
  return _askOpen(Object.assign({body, fields, title:"填写", okText:"保存"}, opts || {}));
}
(function bindAsk(){
  const box = document.getElementById("ask");
  if(!box) return;
  document.getElementById("askOk").onclick = () => _askClose(_askValue());
  document.getElementById("askCancel").onclick = () => _askClose(null);
  box.addEventListener("click", ev => { if(ev.target === box) _askClose(null); });   // 点遮罩 = 取消
  document.addEventListener("keydown", ev => {
    if(!box.classList.contains("show")) return;
    if(ev.key === "Escape"){ ev.preventDefault(); _askClose(null); }
    else if(ev.key === "Enter"){ ev.preventDefault(); _askClose(_askValue()); }
  });
})();

async function api(path, opts = {}){
  const r = await fetch(path, {...opts, headers: {
    "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", ...(opts.headers || {})}});
  if(r.status === 401){ showLogin(); throw new Error("unauthorized"); }
  // 后端在处理中途抛异常时，连接会被直接关掉，响应体是空的。此时若直接 r.json()，
  // 用户看到的是 "Unexpected end of JSON input" —— 该提示对排查没有帮助，
  // 也掩盖了「动作已执行、审计已写入」这一事实。因此先读取原始响应体再判断。
  const text = await r.text();
  if(!text.trim()){
    throw new Error("服务端没有返回内容（HTTP " + r.status + "）。"
      + "若刚执行过写操作，请先到「日志 · 审计」确认它是否已经生效，再检查服务状态。");
  }
  try{
    return JSON.parse(text);
  }catch(e){
    throw new Error("服务端返回的不是 JSON（HTTP " + r.status + "）：" + text.slice(0, 140));
  }
}

/* --- 登录 / 首次设置 / 主题 ------------------------------------------- */
function showLogin(needSetup){
  document.getElementById("login").classList.remove("hidden");
  document.getElementById("setupBox").classList.toggle("hidden", !needSetup);
  document.getElementById("loginBox").classList.toggle("hidden", !!needSetup);
  document.getElementById("lgTitle").textContent = needSetup ? "第一次使用：设置账户" : "N1bus 服务器面板";
  document.getElementById("loginErr").textContent = "";
  setTimeout(() => {
    const el = document.getElementById(needSetup ? "suName" : "loginName");
    if(el) el.focus();
  }, 50);
}
function logout(){
  if(TOKEN) api("/api/logout", {method:"POST"}).catch(() => {});
  localStorage.removeItem(CRED_KEY);
  TOKEN = ""; ME = "";
  if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
  showLogin(false);
}
function _loginErr(msg){ document.getElementById("loginErr").innerHTML = esc(msg); }
async function _post(path, body){
  const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"},
                               body: JSON.stringify(body || {})});
  let j = {};
  try{ j = await r.json(); }catch(e){}
  return {status: r.status, j};
}
function _entered(name){                      // 登录/首次设置成功后进入面板
  TOKEN = name.session; ME = name.name;
  localStorage.setItem(CRED_KEY, TOKEN);
  document.getElementById("login").classList.add("hidden");
  boot();
}
async function doLogin(){
  const n = document.getElementById("loginName").value.trim();
  const p = document.getElementById("loginPass").value;
  if(!n || !p){ _loginErr("请填用户名和密码"); return; }
  const {j} = await _post("/api/login", {name: n, password: p});
  if(j.ok) _entered(j);
  else _loginErr(j.error || "登录失败");
}
async function doSetup(){
  const n = document.getElementById("suName").value.trim();
  const p1 = document.getElementById("suPass").value;
  const p2 = document.getElementById("suPass2").value;
  const min = AUTH.password_min || 8;
  if(!n){ _loginErr("请填用户名"); return; }
  if((p1 || "").length < min){ _loginErr("密码至少 " + min + " 位"); return; }
  if(p1 !== p2){ _loginErr("两次输入的密码不一样"); return; }
  const {j} = await _post("/api/setup", {name: n, password: p1});
  if(j.ok) _entered(j);
  else _loginErr(j.error || "创建失败");
}
function applyTheme(t){
  document.documentElement.dataset.theme = t;
  document.getElementById("themeBtn").innerHTML = t === "dark"
    ? '<svg class="ico"><use href="#i-sun"/></svg>浅色'
    : '<svg class="ico"><use href="#i-moon"/></svg>深色';
}
function toggleTheme(){
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, t); applyTheme(t);
}

/* --- 概览 ------------------------------------------------------------- */

/* 顶栏面包屑：主标签 + 子视图 */
function setCrumb(main, sub){
  const t = document.getElementById("pageTitle"), s = document.getElementById("pageSub");
  if(t && main) t.textContent = main;
  if(s) s.textContent = sub || "";
}


async function loadOverview(){
  try{
    const j = await api("/api/overview");
    const cpu = j.cpu || {}, mem = j.mem || {}, net = j.net || {};
    const sv = j.services || {}, dk = j.docker || {}, se = j.security || {}, pr = j.procs || {};
    const root = (j.disk || [])[0] || {};
    const set = (id, v) => { const el = document.getElementById(id); if(el) el.textContent = v; };
    const ring = (id, pct) => {
      const el = document.getElementById(id);
      if(!el) return;
      const r = 42, c = 2 * Math.PI * r;
      el.style.strokeDasharray = c;
      el.style.strokeDashoffset = c * (1 - Math.max(0, Math.min(100, pct || 0)) / 100);
    };

    /* 顶部四个大数字 */
    set("stSvc", sv.available ? sv.running + " / " + sv.total : "–");
    set("stSvcSub", sv.available ? (sv.failed ? sv.failed + " 个失败单元" : "无失败单元") : "本机未运行 systemd");
    set("stDk", (dk.running || 0) + " / " + (dk.total || 0));
    set("stProc", num(pr.total));
    set("stProcSub", "僵尸 " + num(pr.zombie) +
      (pr.running == null ? "" : " · 运行中 " + num(pr.running)));
    set("stFail", num(se.failed));
    set("stFailSub", se.top ? "最多来自 " + se.top : "暂无记录");

    /* 状态圆环 */
    ring("ringCpu", cpu.percent);
    set("ovCpu", (cpu.percent ?? 0).toFixed(1) + "%");
    set("ovCpuSub", (cpu.count || 1) + " 核" +
      ((cpu.cores || []).length ? " · 各核 " + cpu.cores.map(c => c.toFixed(0) + "%").join(" ") : ""));
    ring("ringMem", mem.percent);
    set("ovMem", (mem.percent ?? 0).toFixed(1) + "%");
    set("ovMemSub", bytes(mem.used) + " / " + bytes(mem.total));
    ring("ringDisk", root.percent);
    set("ovDisk", (root.percent ?? 0).toFixed(1) + "%");
    set("ovDiskSub", bytes(root.free) + " 可用 · " + (root.mount || "–"));
    const load1 = Number((cpu.load || [])[0] || 0);
    ring("ringLoad", load1 / Math.max(1, cpu.count || 1) * 100);
    set("ovLoad", load1.toFixed(2));
    set("ovLoadSub", (cpu.load || []).map(x => Number(x).toFixed(2)).join(" / ") +
      "（1 / 5 / 15 分钟）");

    /* 顶栏 */
    const pill = document.getElementById("hPill");
    if(pill){
      pill.textContent = "负载 " + (cpu.load ? Number(cpu.load[0]).toFixed(2) : "–");
      pill.className = "pill" + ((cpu.load && cpu.load[0] > (cpu.count || 1) * 1.5) ? " bad" : " ok");
    }

    /* 系统信息与磁盘。这里不放「运行时长」（状态卡右上角已经有）与「采样时间」
       （面板的内部节奏，每两秒就变一次，对使用者没有参考价值）。 */
    const info = document.getElementById("ovInfo");
    const host = j.info || {};
    if(info) info.innerHTML = [
      ["主机名", host.hostname], ["系统", host.os], ["内核", host.kernel],
      ["架构", host.arch], ["Python", host.python],
    ].map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v mono">${esc(v || "–")}</div>`).join("");
    set("ovUptime", "已运行 " + (j.uptime_text || "–"));
    const elNet = document.getElementById("ovNet");
    if(elNet) elNet.innerHTML = [
      ["下行", rate(net.rx_bps)], ["上行", rate(net.tx_bps)],
      ["累计接收", bytes(net.rx_total)], ["累计发送", bytes(net.tx_total)],
    ].map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`).join("");
    OV_PROC = {pr, sv, dk}; renderOvProc();
    renderOvSec();
    const dl = document.getElementById("ovDiskList");
    if(dl) dl.innerHTML = (j.disk || []).map(d => {
      const p = Number(d.percent || 0), ip = Number(d.inodes_percent || 0);
      const cls = p >= 90 ? "bad-v" : p >= 75 ? "warn-v" : "";
      return `<tr><td class="mono">${esc(d.mount)}</td><td class="mono hint">${esc(d.device)}</td>
        <td class="num">${bytes(d.used)}</td><td class="num">${bytes(d.total)}</td>
        <td class="num hint">${bytes(d.free)}</td>
        <td><div class="row" style="gap:7px;justify-content:flex-end">
          <span class="bar" style="width:56px"><i style="width:${p.toFixed(1)}%"></i></span>
          <span class="num ${cls}">${p.toFixed(1)}%</span></div></td>
        <td class="num hint">${ip.toFixed(1)}%</td></tr>`;}).join("") ||
      '<tr><td colspan="7" class="empty">无数据</td></tr>';

    drawSpark(j.series || []);
  }catch(e){ if(e.message !== "unauthorized") console.warn(e); }
}
/* 概览折叠块：进程与服务 / 登录失败来源（失败排行复用安全页那份缓存） */
let OV_PROC = null, SECLOG = null;
function renderOvProc(){
  const box = document.getElementById("ovProc");
  if(!box || !OV_PROC) return;
  const pr = OV_PROC.pr || {}, sv = OV_PROC.sv || {}, dk = OV_PROC.dk || {};
  box.innerHTML = [
    ["进程总数", num(pr.total)], ["僵尸进程", num(pr.zombie)],
    ["服务运行中", (sv.running || 0) + " / " + (sv.total || 0)], ["服务失败", num(sv.failed)],
    ["容器运行中", (dk.running || 0) + " / " + (dk.total || 0)],
  ].map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`).join("");
}
function renderOvSec(){
  const box = document.getElementById("ovSec");
  if(!box) return;
  const lg = (SECLOG || {}).logins || {}, ips = lg.top_ips || [];
  const mx = Math.max(1, ...ips.map(x => Number(x.n) || 0));
  const blk = (SECLOG || {}).blocked || {};
  box.innerHTML = ips.map(x =>
    `<tr><td class="mono">${esc(x.ip)}${blk[x.ip]
        ? " " + dot("warn", "已封禁（" + blk[x.ip] + "）") : ""}</td>
      <td class="num">${num(x.n)}</td>
      <td><div class="row" style="gap:7px;justify-content:flex-end">
        <span class="bar" style="width:120px">
          <i style="width:${((Number(x.n) || 0) / mx * 100).toFixed(0)}%"></i></span>
      </div></td></tr>`).join("") ||
    '<tr><td colspan="3" class="empty">无记录</td></tr>';
}

/* 监控曲线：折线 + 面积填充（带底色的图） */
function drawSpark(series){
  const n = series.length;
  const cpu = document.getElementById("spCpu"), netl = document.getElementById("spNet");
  const fc = document.getElementById("fillCpu"), fn = document.getElementById("fillNet");
  if(!cpu || !netl) return;
  if(n < 2){
    cpu.setAttribute("points", ""); netl.setAttribute("points", "");
    if(fc) fc.setAttribute("points", ""); if(fn) fn.setAttribute("points", "");
    return;
  }
  const H = 60, pad = 4;
  const x = i => (i / (n - 1) * 600).toFixed(1);
  const yCpu = s => (H - pad - Math.min(100, s.cpu || 0) / 100 * (H - pad * 2)).toFixed(1);
  const maxNet = Math.max(1, ...series.map(s => (s.rx || 0) + (s.tx || 0)));
  const yNet = s => (H - pad - ((s.rx || 0) + (s.tx || 0)) / maxNet * (H - pad * 2)).toFixed(1);
  const line = (fn2) => series.map((s, i) => `${x(i)},${fn2(s)}`).join(" ");
  const area = (fn2) => `0,${H} ` + line(fn2) + ` 600,${H}`;
  cpu.setAttribute("points", line(yCpu));
  netl.setAttribute("points", line(yNet));
  if(fc) fc.setAttribute("points", area(yCpu));
  if(fn) fn.setAttribute("points", area(yNet));
  const last = series[series.length - 1] || {};
  const el = id => document.getElementById(id);
  if(el("mCpu")) el("mCpu").textContent = (last.cpu ?? 0).toFixed(1) + "%";
  if(el("mNet")) el("mNet").textContent = rate((last.rx || 0) + (last.tx || 0));
  if(el("mPeak")) el("mPeak").textContent = rate(maxNet);
  if(el("ovSparkLegend")) el("ovSparkLegend").textContent =
    "当前 CPU " + (last.cpu ?? 0).toFixed(1) + "% · 网络 " + rate((last.rx || 0) + (last.tx || 0));
}

/* --- 服务 ------------------------------------------------------------- */
async function loadServices(){
  // limit 只是个上限（真机 210 个单元，给 500 是防异常膨胀）；搜索框要在全部单元里找，
  // 所以后端默认不裁，本层也不依赖它裁剪 —— 省略的是每个单元 body 中无用的字段（P9）。
  const j = await api("/api/services?limit=500");
  UNITS = j.units || [];
  document.getElementById("svcMeta").textContent = j.available
    ? (j.units.length + " 个单元 · 运行中 " + j.running + (j.failed ? " · 失败 " + j.failed : "")
       + (j.truncated ? " · 只显示前 " + j.units.length + " / " + j.total : ""))
    : (j.hint || "不可用");
  const fh = document.getElementById("svcFailedHint");
  if(fh) fh.textContent = j.failed ? (j.failed + " 个单元处于失败状态") : "";
  filterUnits();
  const sel = document.getElementById("lgUnit");
  sel.innerHTML = '<option value="">— 选单元 —</option>' +
    UNITS.filter(u => u.unit.endsWith(".service")).map(u =>
      `<option value="${esc(u.unit)}">${esc(u.unit)}</option>`).join("");
}
function filterUnits(){
  const q = document.getElementById("svcQ").value.trim().toLowerCase();
  const only = document.getElementById("svcOnly").value;
  const rows = UNITS.filter(u =>
    (!q || u.unit.toLowerCase().includes(q) || (u.desc || "").toLowerCase().includes(q)) &&
    (!only || u.active === only));
  // 失败项置顶。systemd 本身按字母序给列表，而真机上有 147 个单元 —— 失败的那一两个
  // 会夹在列表中部，需要滚动较长时间才能找到，而失败项恰是本页最需要优先看到的内容。过渡态次之。
  const rank = u => u.active === "failed" ? 0
    : (/^(activating|deactivating|reloading)$/.test(u.active) ? 1 : 2);
  rows.sort((a, b) => rank(a) - rank(b) || a.unit.localeCompare(b.unit));
  // 这张表最长（真机 147 行）又是 10 秒一轮，走增量更新：只有真的变了的格子会被改写
  patchRows(document.getElementById("svcBody"), rows, u => u.unit, u => {
    const st = svcState(u);
    return [
      {c: "mono", h: esc(u.unit)},
      {h: dot(st.c, st.t + "（" + u.active + (u.sub ? " / " + u.sub : "") + "）")},
      {c: "hint", s: "white-space:normal", h: esc(u.desc)},
      {c: "num", a: 'onclick="event.stopPropagation()"', h:
        `<button class="act ghost mini" onclick="runAction('service.restart',{unit:'${esc(u.unit)}'})">重启</button>
        <button class="act danger mini" onclick="runAction('service.stop',{unit:'${esc(u.unit)}'})">停止</button>
        <button class="act ghost mini" onclick="runAction('service.start',{unit:'${esc(u.unit)}'})">启动</button>`},
    ];
  }, u => `onclick="openUnit('${esc(u.unit)}')" style="cursor:pointer"` +
          (u.active === "failed" ? ' class="row-bad"' : ""),
     '<tr><td colspan="4" class="empty">无匹配单元</td></tr>');
}
async function openUnit(unit){
  const j = await api("/api/service?unit=" + encodeURIComponent(unit));
  const box = document.getElementById("svcPanel");
  box.classList.remove("hidden");
  document.getElementById("svcTitle").textContent = unit;
  if(!j.ok){ document.getElementById("svcDesc").textContent = j.error || "";
             document.getElementById("svcKv").innerHTML = ""; }
  else{
    document.getElementById("svcDesc").textContent = j.desc || "";
    document.getElementById("svcKv").innerHTML = [
      ["状态", j.active + " / " + j.sub], ["开机自启", j.enabled || "–"], ["主进程 PID", j.pid || "–"],
      ["启动于", j.since || "–"], ["重启次数", j.restarts || "0"],
      ["内存", j.memory != null ? bytes(j.memory) : "–"], ["CPU 时间", j.cpu_sec != null ? j.cpu_sec + " s" : "–"],
      ["单元文件", j.fragment || "–"],
    ].map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v mono">${esc(v)}</div>`).join("");
  }
  document.getElementById("svcActions").innerHTML =
    `<button class="act" onclick="runAction('service.restart',{unit:'${esc(unit)}'})">
       <svg class="ico"><use href="#i-restart"/></svg>重启</button>
     <button class="act ghost" onclick="runAction('service.start',{unit:'${esc(unit)}'})">
       <svg class="ico"><use href="#i-play"/></svg>启动</button>
     <button class="act danger" onclick="runAction('service.stop',{unit:'${esc(unit)}'})">
       <svg class="ico"><use href="#i-stop"/></svg>停止</button>
     <span class="hint">单元文件：${esc((j && j.fragment) || "–")}</span>`;
  const lg = await api("/api/service/logs?unit=" + encodeURIComponent(unit) + "&lines=200");
  document.getElementById("svcLogs").innerHTML = (lg.lines || []).map(l =>
    `${logLine(l)}`).join("") || '<div class="empty">暂无日志</div>';
  box.scrollIntoView({behavior:"smooth", block:"nearest"});
}

/* --- Docker ----------------------------------------------------------- */
/* 容器统计由服务端后台线程异步采集（docker stats 单条需 1~2 秒）。
   本轮尚无数据时接口会返回 stats_pending，稍后重新请求，
   避免 CPU/内存列长期为空，但不因此阻塞整页渲染。 */
let dkStatsTimer = 0;
function scheduleDockerStats(){
  if(dkStatsTimer) return;
  dkStatsTimer = setTimeout(() => {
    dkStatsTimer = 0;
    if(currentView() === "docker") loadDocker();
  }, 1800);
}
async function loadDocker(){
  const j = await api("/api/docker");
  DK_CONTAINERS = j.containers || [];
  document.getElementById("dkMeta").textContent = j.available
    ? (j.containers.length + " 个容器 · 运行中 " + j.running + " · 点击行查看日志")
    : (j.hint || "不可用");
  patchRows(document.getElementById("dkBody"), j.containers || [], c => c.name, c => [
    {c: "mono", h: esc(c.name)},
    {h: dot(dkState(c).c, dkState(c).t + "（" + (c.status || c.state || "") + "）")},
    {c: "hint", h: esc(c.image)},
    {c: "num", a: 'onclick="event.stopPropagation()"', h:
      `<button class="act ghost mini"
         onclick="runAction('docker.restart',{name:'${esc(c.name)}'})">重启</button>
      ${c.running
        ? `<button class="act danger mini"
             onclick="runAction('docker.stop',{name:'${esc(c.name)}'})">停止</button>`
        : `<button class="act mini"
             onclick="runAction('docker.start',{name:'${esc(c.name)}'})">启动</button>`}`},
  ], c => `onclick="openContainer('${esc(c.name)}')" style="cursor:pointer"`,
     '<tr><td colspan="4" class="empty">无容器（或未安装 docker）</td></tr>');
  if(j.stats_pending) scheduleDockerStats();
}
/* 容器日志：可以填 docker logs 自己的参数（--since/--until/--tail/-t）与关键字，
   参数以列表交给 docker，不过 shell；面板上直接显示实际执行了哪条命令。 */
let dkCur = "";
/* 日志面板里的容器操作；删除要二次确认并把后果说清楚 */
async function dkAction(name){
  if(!dkCur){ toast("请先选择容器", true); return; }
  if(name === "docker.rm" &&
     !await askConfirm("删除容器 <b>" + esc(dkCur) + "</b> ？\n\n容器会被强制停止并移除；数据卷不受影响，" +
              "由 compose 创建的容器可用 docker compose up -d 重建。",
              {title: "删除容器", danger: true, okText: "删除"})) return;
  runAction(name, {name: dkCur});
}
async function openContainer(name){
  dkCur = name;
  const c = (DK_CONTAINERS || []).find(x => x.name === name) || {}, st = c.stats || {};
  const s = document.getElementById("dkStats");
  if(s) s.innerHTML = [["镜像", c.image], ["CPU", st.cpu], ["内存", st.mem],
                       ["网络 I/O", st.net], ["块 I/O", st.block], ["进程数", st.pids]]
    .map(([k, v]) => `<div class="k">${esc(k)}</div>` +
                     `<div class="v mono">${esc(v == null || v === "" ? "–" : v)}</div>`).join("");
  document.getElementById("dkPanel").classList.remove("hidden");
  document.getElementById("dkTitle").textContent = name;
  await reloadDockerLogs();
}
async function reloadDockerLogs(){
  if(!dkCur) return;
  const q = new URLSearchParams({
    name: dkCur,
    lines: document.getElementById("dkLines").value || "200",
    since: document.getElementById("dkSince").value.trim(),
    until: document.getElementById("dkUntil").value.trim(),
    grep: document.getElementById("dkGrep").value.trim(),
    ts: document.getElementById("dkTs").checked ? "1" : "",
  });
  const j = await api("/api/docker/logs?" + q.toString());
  document.getElementById("dkCmd").textContent = j.cmd ? ("实际执行：" + j.cmd) : "";
  const bad = j.error ? `<div class="logline warn"><span>${esc(j.error)}</span></div>` : "";
  const rows = (j.lines || []).map(l => `${logLine(l)}`).join("");
  document.getElementById("dkLogs").innerHTML = bad + (rows ||
    `<div class="empty">${j.grep ? "没有匹配「" + esc(j.grep) + "」的日志" : "暂无日志"}</div>`);
}
/* 镜像 = 容器的模板。此区块有两个用途：查看占用空间、检查是否存在可清理的历史构建残留 */
function parseSize(s){
  const m = /([\d.]+)\s*([KMGTP]?B)/i.exec(String(s || ""));
  if(!m) return 0;
  const u = {B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3, TB: 1024 ** 4, PB: 1024 ** 5};
  return parseFloat(m[1]) * (u[m[2].toUpperCase()] || 1);
}
async function loadImages(){
  const j = await api("/api/docker/images");
  const imgs = j.images || [];
  const dangling = i => (i.tag || "").indexOf("<none>") === 0 || i.repo === "<none>";
  const meta = document.getElementById("dkImagesMeta");
  if(meta) meta.textContent = imgs.length
    ? (imgs.length + " 个 · 约 " + bytes(imgs.reduce((s, i) => s + parseSize(i.size), 0))
       + (imgs.filter(dangling).length ? " · 无标签 " + imgs.filter(dangling).length + " 个" : ""))
    : "无";
  patchRows(document.getElementById("dkImages"), imgs, i => i.id, i => [
    {c: "mono", h: esc(i.repo)},
    {c: "mono", h: dangling(i) ? "—" : esc(i.tag)},
    {c: "mono hint", h: esc(i.id)},
    {c: "num", h: esc(i.size)},
    {c: "hint", h: esc(i.created)},
    {h: dangling(i) ? dot("warn", "无标签，可清理") : dot("mute", "有标签")},
  ], null, '<tr><td colspan="6" class="empty">无镜像</td></tr>');
}

/* --- 安全 ------------------------------------------------------------- */
/* 失败来源排行默认仅列出**尚未封禁**的来源（已封禁记录会挤占需要处置的行）；
   勾选「显示已封禁的」后展示全量。 */
let SE_SHOW_ALL = false;
async function loadSecurity(){
  const j = await api("/api/security");
  SECLOG = j; renderOvSec();
  const lg = j.logins || {}, fw = j.firewall || {}, f2b = j.fail2ban || {}, ssh = j.sshd || {};
  document.getElementById("seFailed").textContent = num(lg.failed);
  document.getElementById("seOk").textContent = num(lg.accepted);
  document.getElementById("seUfwV").textContent = fw.available ? (fw.active ? "已启用" : "未启用") : "未安装";
  document.getElementById("seUfwV").className = "v " + (fw.active ? "ok-v" : "warn-v");
  document.getElementById("seUfwN").textContent = fw.available ? ((fw.rules || []).length + " 条规则") : (fw.hint || "");
  document.getElementById("seF2bV").textContent = f2b.available ? ((f2b.jails || []).length + " 个 jail") : "未安装";
  document.getElementById("seF2bV").className = "v " + (f2b.available && (f2b.jails || []).length ? "ok-v" : "warn-v");
  document.getElementById("seF2bN").textContent = f2b.available ? (f2b.jails || []).join("、") : (f2b.hint || "");
  const blocked = j.blocked || {};
  // 排行默认仅列出尚未封禁的来源 —— 已封禁记录会挤占真正需要处置的行。
  // 勾选「显示已封禁的」可查看全量。
  const allIps = lg.top_ips || [];
  const openIps = allIps.filter(x => !blocked[x.ip]);
  const listed = SE_SHOW_ALL ? allIps : openIps;
  document.getElementById("seIPs").innerHTML = listed.map(x => {
    const how = blocked[x.ip] || "";
    const state = how ? dot("warn", "已封禁（" + how + "）") : dot("mute", "未封禁");
    // 解封走 /api/security/unban：它在撤销规则之外还会把该 IP 写入豁免名单。
    // 若只撤销 ufw 规则，下一轮巡检会依据同一累计失败次数立即重新封禁。
    const jail = how.indexOf("fail2ban/") === 0 ? how.slice(9) : "";
    const act = !how
      ? `<button class="act danger mini" onclick="runAction('ufw.deny',{ip:'${esc(x.ip)}'})">封禁</button>`
      : `<button class="act ghost mini" onclick="defenderUnban('${esc(x.ip)}','${esc(jail)}')">解除封禁</button>`;
    return `<tr><td class="mono">${esc(x.ip)}</td><td class="num">${num(x.n)}</td>
      <td>${state}</td><td class="num">${act}</td></tr>`;}).join("") ||
    `<tr><td colspan="4" class="empty">${openIps.length || allIps.length ? "全部来源都已封禁" : "无记录"}</td></tr>`;
  {
    const all = document.getElementById("seIPsAll");
    if(all) all.checked = !!SE_SHOW_ALL;
    const hint = document.getElementById("seIPsHint");
    if(hint) hint.textContent = allIps.length
      ? `${openIps.length} 个待处置 / 共 ${allIps.length} 个来源`
      : "";
  }
  document.getElementById("seUsers").innerHTML = (lg.top_users || []).map(x =>
    `<tr><td class="mono">${esc(x.user)}</td><td class="num">${num(x.n)}</td></tr>`).join("") ||
    '<tr><td colspan="2" class="empty">–</td></tr>';
  document.getElementById("seIPsMeta").textContent = allIps.length
    ? ("共 " + allIps.length + " 个来源" + (lg.lines_scanned ? "，扫了 " + num(lg.lines_scanned) + " 行日志" : ""))
    : "";
  document.getElementById("seRecent").innerHTML = (lg.recent || []).map(x =>
    `<tr><td class="mono hint">${esc(x.ts)}</td><td class="mono">${esc(x.user)}</td>
      <td class="mono">${esc(x.ip)}</td></tr>`).join("") ||
    '<tr><td colspan="3" class="empty">没有失败记录</td></tr>';
  document.getElementById("seRecentMeta").textContent = "共 " + num(lg.failed) + " 条 · 最近 " +
    (lg.recent || []).length + " 条";
  document.getElementById("seOkBody").innerHTML = (lg.recent_ok || []).map(x =>
    `<tr><td class="mono hint">${esc(x.ts)}</td><td class="mono">${esc(x.user)}</td>
      <td class="mono">${esc(x.ip)}</td></tr>`).join("") || '<tr><td colspan="3" class="empty">–</td></tr>';
  const cfg = ssh.config || {};
  document.getElementById("seSshd").innerHTML =
    Object.keys(cfg).map(k => `<div class="k">${esc(k)}</div><div class="v mono">${esc(cfg[k])}</div>`).join("") +
    `<div class="k">公钥数</div><div class="v mono">${num(ssh.authorized_keys)}</div>` +
    (ssh.note ? `<div class="k"></div><div class="v warn-v">${esc(ssh.note)}</div>` : "");
  // fail2ban：把每个 jail 的关键计数抽出来做表，原始输出留在下面备查。
  // 原先这些内容与 ufw 规则原文同处一张卡片，该部分文本现已由「防火墙端口」承担。
  const f2bNum = (text, label) => {
    const m = new RegExp(label + ":\\s*(\\d+)").exec(text || "");
    return m ? m[1] : "";
  };
  const cnt = v => /^\d+$/.test(v) ? num(v) : (v || "–");
  const jails = Object.keys(f2b.detail || {});
  const curBanned = jails.reduce(
    (s, j) => s + (parseInt(f2bNum(f2b.detail[j], "Currently banned"), 10) || 0), 0);
  document.getElementById("seF2bMeta").textContent =
    jails.length ? ("当前封禁 " + num(curBanned) + " 个") : (f2b.available ? "无 jail" : "未安装");
  document.getElementById("seF2bInfo").innerHTML = jails.length
    ? jails.map(j => {
        const d = f2b.detail[j];
        return `<div class="k mono">${esc(j)}</div><div class="v mono">
          当前封禁 ${cnt(f2bNum(d, "Currently banned"))} ·
          累计封禁 ${cnt(f2bNum(d, "Total banned"))} ·
          累计失败 ${cnt(f2bNum(d, "Total failed"))}</div>`;
      }).join("")
    : `<div class="k">状态</div><div class="v hint">${esc(f2b.hint || "没有 fail2ban 状态")}</div>`;
  document.getElementById("seF2bRaw").textContent =
    jails.map(k => "== " + k + " ==\n" + f2b.detail[k]).join("\n\n")
    || (f2b.hint || "（没有 fail2ban 状态）");
  renderDefender(j.defender);
  renderPorts(j.allowed);
  try{
    const ef = await api("/api/enforce");
    const last = ef.last || {};
    document.getElementById("seEnf").innerHTML = [
      ["状态", ef.enabled ? "运行中" : "未启用"],
      // 不显示内部服务地址与命令行参数名：那是部署细节，对使用者没有意义
      ["决策来源", ef.enabled ? "外部面板（v1m-dash）" : "未配置"],
      ["检查间隔", ef.interval ? dur(ef.interval) : "–"],
      ["最近一轮", last.ts ? new Date(last.ts * 1000).toLocaleString("zh-CN") : "尚未执行"],
      ["本轮封禁", (last.banned || []).join("、") || "无"],
      ["本轮解封", (last.unbanned || []).join("、") || "无"],
      ["结果", last.ok === false ? "失败：" + (last.error || "") : (last.ts ? "正常" : "–")],
    ].map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v mono">${esc(v)}</div>`).join("") +
      (ef.note ? `<div class="k"></div><div class="v warn-v">${esc(ef.note)}</div>` : "");
  }catch(e){ /* 老版本后端没有这个接口，忽略 */ }
  try{
    const acc = await api("/api/accounts");
    renderGuard(acc.guard);
  }catch(e){ /* 拉不到账户信息就不显示这一段 */ }
}

/* --- 防火墙端口 -------------------------------------------------------- */
/* ufw 的规则区分方向：`ALLOW IN` 与 `ALLOW OUT` 是两条独立规则，`ufw allow 80/tcp`
   与 `ufw allow out 80/tcp` 互不覆盖。IPv4 与 IPv6 同样是两条独立规则（一次 allow
   会同时写入两条），所以后端按「方向 + 端口 + 协议」合并之后再给到界面。
   两个容易误判的点写在这里，以免日后依据界面数字得出错误结论：
     * Docker 发布的端口绕过 ufw —— docker-proxy 把转发规则直接写进 iptables，
       因此「ufw 里没有这条规则」不等于外部访问不了；
     * 只监听 127.0.0.1 的服务本来就不对外，不会被列为「待放行」。 */
function portUnallowBtn(dir, port, proto){
  return `<button class="act ghost mini"
    onclick="portUnallow('${esc(dir)}','${esc(port)}','${esc(proto)}')">移除规则</button>`;
}
function renderPorts(a){
  if(!a) return;
  const list = a.allowed || [];
  const inList  = list.filter(x => x.dir === "in");
  const outList = list.filter(x => x.dir === "out");
  const other   = (a.rules || []).filter(x => x.action === "ALLOW" && x.simple === false);

  document.getElementById("sePortMeta").textContent = a.available
    ? (inList.length + " 入站 / " + outList.length + " 出站")
    : (a.hint || "ufw 不可用");

  const svcCell = x => x.listen
    ? esc(x.listen) + (x.docker ? ' <span class="hint">Docker 发布</span>' : "")
    : '<span class="hint">无服务监听</span>';
  document.getElementById("sePortIn").innerHTML = inList.map(x =>
    `<tr><td class="mono">${num(x.port)}</td><td class="mono">${esc(x.proto)}</td>
      <td class="mono hint">${esc(x.from)}</td>
      <td class="hint">${x.v6 ? "含 v6" : "仅 v4"}</td>
      <td>${svcCell(x)}</td>
      <td class="num">${portUnallowBtn(x.dir, x.port, x.proto)}</td></tr>`).join("")
    || '<tr><td colspan="6" class="empty">没有入站放行规则</td></tr>';
  document.getElementById("sePortOut").innerHTML = outList.map(x =>
    `<tr><td class="mono">${num(x.port)}</td><td class="mono">${esc(x.proto)}</td>
      <td class="mono hint">${esc(x.from)}</td>
      <td class="hint">${x.v6 ? "含 v6" : "仅 v4"}</td>
      <td class="num">${portUnallowBtn(x.dir, x.port, x.proto)}</td></tr>`).join("")
    || '<tr><td colspan="5" class="empty">没有出站放行规则（ufw 出站默认放行，通常无需添加）</td></tr>';
  document.getElementById("sePortOther").innerHTML = other.map(x =>
    `<tr><td class="mono">${esc(x.to)}</td><td class="mono">${esc(x.action)}</td>
      <td class="hint">${esc(x.dir === "in" ? "入站" : x.dir === "out" ? "出站" : "转发")}</td>
      <td class="mono hint">${esc(x.from)}</td></tr>`).join("")
    || '<tr><td colspan="4" class="empty">没有这类规则</td></tr>';

  // 待处理提示：该内容是本卡片的核心价值 —— 直接列出「已监听但未放行」的端口
  const blocks = [];
  if(a.policy) blocks.push(
    '<div class="kv wide"><div class="k">默认策略</div><div class="v mono">' +
    esc(a.policy) + '</div></div>');
  const need = (a.uncovered || []).filter(x => !x.docker);
  if(need.length) blocks.push(
    '<div class="kv wide"><div class="k warn-v">待放行</div><div class="v">' +
    need.map(x => `${num(x.port)}/${esc(x.proto)}　${esc(x.process || "未知进程")} 在监听，但无法从外部访问
      <button class="act mini" style="margin-left:8px"
        onclick="portAllowQuick('${esc(x.port)}','${esc(x.proto)}')">放行</button>`).join("<br>") +
    '</div></div>');
  const dk = (a.uncovered || []).filter(x => x.docker);
  if(dk.length) blocks.push(
    '<div class="kv wide"><div class="k">Docker 发布</div><div class="v hint">' +
    dk.map(x => `${num(x.port)}/${esc(x.proto)}`).join("、") +
    '　由容器端口映射直接暴露，不受 ufw 约束，无需在此放行</div></div>');
  if((a.unmatched || []).length) blocks.push(
    '<div class="kv wide"><div class="k">可清理</div><div class="v hint">' +
    (a.unmatched || []).map(x => `${num(x.port)}/${esc(x.proto)}`).join("、") +
    '　已放行但本机无服务监听，该规则不产生实际效果</div></div>');
  if(!a.active && a.available) blocks.push(
    '<div class="kv wide"><div class="k"></div><div class="v warn-v">ufw 当前未启用，以下为已保存的规则。</div></div>');
  document.getElementById("sePortTips").innerHTML = blocks.join("");
}
function portAllowQuick(port, proto){
  document.getElementById("sePortDir").value = "in";
  document.getElementById("sePortNum").value = port;
  document.getElementById("sePortProto").value = proto;
  portAllow();
}
async function portAllow(){
  const dir = document.getElementById("sePortDir").value;
  const proto = document.getElementById("sePortProto").value;
  const port = parseInt(document.getElementById("sePortNum").value, 10);
  if(!port || port < 1 || port > 65535){ toast("端口须为 1-65535 之间的整数", true); return; }
  await runAction("ufw.allow", {port: String(port), proto, dir});
  document.getElementById("sePortNum").value = "";
}
async function portUnallow(dir, port, proto){
  await runAction("ufw.unallow", {port: String(port), proto, dir});
}

/* --- 自动永久封禁 ------------------------------------------------------ */
/* fail2ban 的 sshd jail 配置为 maxretry=4 / findtime=600 / bantime=3600，即
   "10 分钟内失败 4 次则封禁 1 小时"，到期自动解封。持续低速扫描的来源不会触发该
   窗口，因此该策略不收敛 —— 生产环境实测 154.18.197.29 被反复封禁 19 次、
   累计失败 997 次后仍在持续尝试。此处补充的是另一半：累计失败次数超过阈值的来源
   直接写入 ufw 规则（规则持久化，重启后仍生效），仅在面板上解封才会放行。 */
function renderDefender(d){
  if(!d) return;
  const on = document.getElementById("seDefOn");
  if(on) on.checked = !!d.enabled;
  const th = document.getElementById("seDefTh");
  // 输入过程中不要被轮询结果覆盖：否则用户输入到一半就会被写回原值
  if(th && document.activeElement !== th) th.value = d.threshold;
  if(th){ th.min = d.min_threshold; th.max = d.max_threshold; }

  const last = d.last || {};
  const lastText = !last.ts ? "未执行"
    : last.skipped ? last.skipped
    : last.error ? ("出错：" + last.error)
    : (last.banned || []).length ? ("封禁 " + last.banned.length + " 个")
    : "无新增";
  document.getElementById("seDefMeta").textContent = d.enabled
    ? ("已开启 · 超过 " + num(d.threshold) + " 次") : "已关闭";
  document.getElementById("seDefInfo").innerHTML = [
    ["状态", d.enabled ? "已开启" : "已关闭"],
    ["触发阈值", "累计失败 > " + num(d.threshold) + " 次"],
    ["巡检间隔", d.interval + " 秒"],
    ["可信来源", num(d.own_ips || 0) + " 个成功登录过的来源（永不自动封禁）"],
    ["豁免名单", (d.exempt || []).length + " 个"],
    ["排除网段", (d.skip || []).join("、") || "–"],
    ["最近一轮", last.ts ? (new Date(last.ts * 1000).toLocaleString("zh-CN") + "　" + lastText)
                          : "未执行"],
  ].map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v mono">${esc(v)}</div>`).join("") +
    (d.enabled ? "" : '<div class="k"></div><div class="v warn-v">'
      + '自动封禁已关闭，下表仅为预览，不会实际执行。</div>');

  const pend = d.pending || [];
  document.getElementById("seDefPendMeta").textContent = pend.length ? ("共 " + pend.length + " 个") : "";
  document.getElementById("seDefPend").innerHTML = pend.map(x =>
    `<tr><td class="mono">${esc(x.ip)}</td><td class="num">${num(x.n)}</td>
      <td class="num"><button class="act danger mini"
        onclick="runAction('ufw.deny',{ip:'${esc(x.ip)}'})">封禁</button>
      <button class="act ghost mini" style="margin-left:6px"
        onclick="defenderExempt('${esc(x.ip)}',true)">豁免</button></td></tr>`).join("")
    || `<tr><td colspan="3" class="empty">${d.enabled ? "没有超过阈值的来源"
         : "自动封禁已关闭"}</td></tr>`;

  const log = d.log || [];
  document.getElementById("seDefLog").innerHTML = log.map(x =>
    `<tr><td class="mono hint">${esc(new Date(x.ts * 1000).toLocaleString("zh-CN"))}</td>
      <td class="mono">${esc(x.ip)}</td>
      <td class="num">${x.n ? num(x.n) : "–"}</td>
      <td>${x.ok ? dot("ok", "成功") : dot("warn", "失败")}
        <span class="hint"> ${esc(x.actor || "")} ${esc((x.detail || "").slice(0, 70))}</span></td>
      </tr>`).join("") || '<tr><td colspan="4" class="empty">暂无处置记录</td></tr>';

  const ex = d.exempt || [];
  document.getElementById("seDefExempt").innerHTML = ex.length
    ? ex.map(ip => `<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <span class="mono">${esc(ip)}</span>
        <button class="act ghost mini"
          onclick="defenderExempt('${esc(ip)}',false)">移出</button></div>`).join("")
    : '<span class="hint">名单为空 —— 在面板上解封某地址后，它会自动加入此名单，不再被自动封禁。</span>';
}
async function defenderSet(){
  const enabled = document.getElementById("seDefOn").checked;
  const threshold = parseInt(document.getElementById("seDefTh").value, 10);
  const body = {enabled};
  if(!isNaN(threshold)) body.threshold = threshold;
  try{
    const j = await api("/api/security/defender", {method:"POST", body: JSON.stringify(body)});
    if(j.ok) toast("自动永久封禁已" + (j.config.enabled ? "启用" : "禁用") +
                   "，阈值 " + num(j.config.threshold) + " 次");
    else toast("保存设置失败：" + esc(j.error || ""), true);
    loadSecurity();
  }catch(e){ toast("保存设置失败：" + esc(e.message), true); }
}
async function defenderRun(){
  if(!await askConfirm("立即按当前策略执行一轮封禁？<br><br>命中的来源将被 " +
      "<b>ufw deny</b> <b>永久封禁</b>，此后只能在本面板解封。",
      {title:"立即执行", danger:true, okText:"执行"})) return;
  toast("执行中…");
  try{
    const j = await api("/api/security/defender/run", {method:"POST", body:"{}"});
    const r = j.result || {};
    const msg = r.skipped ? r.skipped
      : (r.banned || []).length ? ("已封禁 " + r.banned.length + " 个来源")
      : "没有需要封禁的来源";
    toast(esc(msg), !j.ok);
    loadSecurity();
  }catch(e){ toast("执行失败：" + esc(e.message), true); }
}
async function defenderExempt(ip, on){
  try{
    const j = await api("/api/security/defender/exempt",
                        {method:"POST", body: JSON.stringify({ip, on})});
    toast(j.ok ? (on ? ("已豁免 " + esc(ip)) : "已移出豁免名单") : "操作失败", !j.ok);
    loadSecurity();
  }catch(e){ toast("操作失败：" + esc(e.message), true); }
}
/* 解封的唯一入口。除撤销规则外还会写入豁免 —— 否则下一轮巡检会立即重新封禁该地址。 */
async function defenderUnban(ip, jail){
  if(!await askConfirm("解除 <b class=\"mono\">" + esc(ip) + "</b> 的封禁？<br><br>" +
      "该地址将同时加入<b>豁免名单</b>，不再被自动永久封禁。",
      {title:"解除封禁", okText:"解封"})) return;
  try{
    const j = await api("/api/security/unban",
                        {method:"POST", body: JSON.stringify({ip, jail: jail || ""})});
    toast(j.ok ? "已解封并加入豁免名单"
               : ("解封失败：" + esc(j.detail || j.error || "")), !j.ok);
    loadSecurity();
  }catch(e){ toast("解封失败：" + esc(e.message), true); }
}

/* 面板登录限流：状态、档位、在封来源 */
function renderGuard(g){
  const el = document.getElementById("seGuard");
  if(!el) return;
  if(!g){
    el.innerHTML = '<div class="k">面板登录限流</div><div class="v hint">服务端未提供该数据</div>';
    return;
  }
  const blocked = g.blocked || [];
  const tiers = (g.tiers || []).map(s => dur(s)).join(" → ");
  el.innerHTML = [
    ["触发条件", num(g.limit) + " 次失败 / " + dur(g.window)],
    ["封禁时长", tiers || "–"],
    ["跟踪来源", num(g.tracked) + " 个 IP"],
    ["当前封禁", blocked.length ? (blocked.length + " 个来源") : "无"],
  ].map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v mono">${esc(v)}</div>`).join("") +
    (blocked.length ? blocked.map(b =>
      `<div class="k mono">${esc(b.ip)}</div><div class="v">第 ${num(b.level)} 档 · 剩余 ${dur(b.seconds)}
        <button class="act ghost mini" style="margin-left:8px"
                onclick="unblock('${esc(b.ip)}')">解封</button></div>`).join("") : "") +
    // 这里原先贴了一整条 curl 命令。界面上不该出现这种部署细节：
    // 列表里本来就能直接点「解封」，命令行请查文档。
    '<div class="k"></div><div class="v hint">按来源 IP 限制，不锁定账户；' +
    '封禁期间该来源无法登录面板。</div>';
}
async function unblock(ip){
  if(!await askConfirm("解封 <b>" + esc(ip) + "</b> ？\n\n失败计数清零，封禁档位保留。",
                       {title: "解封来源 IP", okText: "解封"})) return;
  const j = await api("/api/accounts/unblock", {method:"POST", body: JSON.stringify({ip})});
  if(j.ok){ toast("已解封 " + esc(ip)); loadSecurity(); }
  else toast(esc(j.error || "解封失败"), true);
}

/* --- 端口 / 进程 ------------------------------------------------------ */
async function loadNet(){
  const j = await api("/api/ports");
  const L = j.listening || {}, C = j.established || {};
  document.getElementById("ptMeta").textContent = L.available
    ? (L.ports.length + " 个监听 · 对外 " + L.exposed) : (L.hint || "");
  patchRows(document.getElementById("ptBody"), L.ports || [], p => p.proto + "|" + p.local, p => [
    {c: "mono", h: esc(p.proto)},
    {c: "mono", h: esc(p.local)},
    {c: "num mono", h: esc(p.port)},
    {h: esc(p.process || "–")},
    {c: "mono hint", h: esc(p.pid || "–")},
    {h: p.exposed ? dot("warn", "对外（" + p.local + "）") : dot("mute", "仅本机")},
  ], null, '<tr><td colspan="6" class="empty">无监听端口（或缺 ss）</td></tr>');
  document.getElementById("ptConn").innerHTML = (C.connections || []).map(c =>
    `<tr><td class="mono">${esc(c.proto)}</td><td class="mono">${esc(c.local)}</td>
      <td class="mono">${esc(c.peer)}</td></tr>`).join("") ||
    '<tr><td colspan="3" class="empty">无连接</td></tr>';
  document.getElementById("ptConnMeta").textContent = (C.connections || []).length + " 条（最多 40 条）";
}
/* --- 通用渲染小工具 --------------------------------------------------- */
/* 表格增量更新（P8）。
   以前每次刷新都是 `tbody.innerHTML = rows.map(...)`：服务表两百来行、每 10 秒整块重建，
   浏览器需丢弃全部旧节点并重新解析 HTML —— 产生无谓的 GC 与布局抖动（在 2 vCPU 机器上更明显）。
   现在按行的 key 比对：key 序列一致就只改真的变了的单元格，行数或顺序变了才整块重建。
   cellsOf(item, i) 返回每格的 {c: class, s: style, h: html}；列表为空时用 emptyRow 作为降级输出。 */
/* 单元格上的额外属性（data-full / onclick 之类）整串换掉：
   先把上次写上的摘下来，再写上这次的。 */
function setCellAttrs(td, want){
  const spec = want || "";
  if(td.__a === spec) return;
  for(const n of (td.__an || [])) td.removeAttribute(n);
  const names = [];
  if(spec){
    const probe = document.createElement("div");
    probe.innerHTML = "<i " + spec + "></i>";
    const el = probe.firstElementChild;
    if(el) for(const at of el.attributes){
      td.setAttribute(at.name, at.value);
      names.push(at.name);
    }
  }
  td.__a = spec; td.__an = names;
}
function patchRows(tbody, items, keyOf, cellsOf, attrOf, emptyRow){
  if(!tbody) return;
  if(!items.length){
    tbody.__keys = null;
    tbody.innerHTML = emptyRow || '<tr><td class="empty">无数据</td></tr>';
    return;
  }
  const keys = items.map(keyOf);
  const old = tbody.__keys;
  const rows = tbody.children;
  const shaped = !!old && old.length === keys.length && rows.length === keys.length &&
                 old.every((k, i) => k === keys[i]);
  const cells = items.map((it, i) => cellsOf(it, i));
  if(shaped && cells.every((cs, r) => cs.length === rows[r].children.length)){
    for(let r = 0; r < rows.length; r++){
      const tds = rows[r].children;
      for(let c = 0; c < cells[r].length; c++){
        const w = cells[r][c] || {}, td = tds[c];
        const cls = w.c || "", sty = w.s || "", attrs = w.a || "";
        const html = String(w.h == null ? "" : w.h);
        if(td.__v === html && td.__c === cls && td.__s === sty && td.__a === attrs)
          continue;                                       // 该单元格无变化，跳过 DOM 写入
        if(td.__c !== cls) td.className = cls;
        if(td.__s !== sty) td.setAttribute("style", sty);
        setCellAttrs(td, attrs);
        td.innerHTML = html;
        td.__v = html; td.__c = cls; td.__s = sty;
      }
    }
    return;
  }
  tbody.__keys = keys;
  tbody.innerHTML = cells.map((cs, r) =>
    `<tr${attrOf ? " " + attrOf(items[r], r) : ""}>` + cs.map(x => {
      const w = x || {};
      return `<td${w.c ? ' class="' + w.c + '"' : ""}${w.s ? ' style="' + w.s + '"' : ""}`
             + `${w.a ? " " + w.a : ""}>` + String(w.h == null ? "" : w.h) + "</td>";
    }).join("") + "</tr>").join("");
  [...tbody.children].forEach((tr, r) => [...tr.children].forEach((td, c) => {
    const w = cells[r][c] || {};
    td.__v = String(w.h == null ? "" : w.h); td.__c = w.c || "";
    td.__s = w.s || ""; td.__a = w.a || ""; td.__an = [];
  }));
}
/* --- 系统（1Panel 参考切片：清理 / 网络 / 磁盘） ---------------------- */
/* 键值两列表的通用渲染；pairs 为空时输出一行降级文案 */
function kvRows(pairs, empty){
  if(!pairs || !pairs.length) return `<div class="hint">${esc(empty || "无数据")}</div>`;
  return '<div class="kv wide">' + pairs.map(([k, v]) =>
    `<div class="k mono">${esc(k)}</div><div class="v mono">${esc(v == null || v === "" ? "–" : v)}</div>`
  ).join("") + "</div>";
}
const CLEAN_VERB = {"clean.journal": "收缩", "docker.prune_images": "清理",
                    "clean.docker_builder": "清理", "clean.apt_cache": "清理",
                    "clean.old_gz_logs": "删除"};
async function loadCleanup(){
  const j = await api("/api/cleanup");
  const el = document.getElementById("sysCleanBody");
  if(!el) return;
  el.innerHTML = (j.items || []).map(i =>
    `<tr><td>${esc(i.label)}${i.count ? ` <span class="tag mute">${num(i.count)} 项</span>` : ""}</td>
      <td class="num">${bytes(i.current)}</td>
      <td class="num ${i.reclaimable ? "warn-v" : "hint"}">${bytes(i.reclaimable)}</td>
      <td class="hint" style="white-space:normal">${esc(i.note || "")}</td>
      <td class="num">${i.action
        ? `<button class="act ghost mini"
             onclick="runAction('${esc(i.action)}',{})">${esc(CLEAN_VERB[i.action] || "执行")}</button>`
        : "–"}</td></tr>`).join("") ||
    '<tr><td colspan="5" class="empty">无法获取清理数据</td></tr>';
  const meta = document.getElementById("sysCleanMeta");
  if(meta) meta.textContent = "可回收 " + bytes(j.reclaimable_total);
  const au = document.getElementById("sysAutoremove");
  if(au){
    const c = j.autoremove_candidates || [];
    au.innerHTML = c.length
      ? '<div class="hint">autoremove 候选（面板不代为执行）：' + c.map(esc).join("、") + "</div>"
      : '<div class="hint">autoremove 无候选包</div>';
  }
}
async function loadBackup(){
  const j = await api("/api/backup");
  const el = document.getElementById("sysBkBody");
  if(!el) return;
  el.innerHTML = (j.items || []).map(i =>
    `<tr><td>${esc(i.label)}</td>
      <td class="num">${i.exists ? bytes(i.size) : dot("mute", "不存在")}</td></tr>`
  ).join("") || '<tr><td colspan="2" class="empty">无法获取备份数据</td></tr>';
  const a = j.archive || {}, meta = document.getElementById("sysBkMeta");
  if(meta) meta.textContent = a.exists ? ("已打包 " + bytes(a.size)) : "未生成备份";
  const hint = document.getElementById("sysBkHint");
  if(hint) hint.innerHTML = a.exists
    ? ('恢复方式（在 SSH 中以 root 执行）：<code class="mono">cd /var/lib/n1bus/backups && ' +
       esc(j.restore_hint) + "</code>")
    : "点击「立即打包」将生成 <code class=\"mono\">latest.tar.gz</code>（面板数据 + KMS 数据卷 + Caddy 配置）。";
}
async function downloadBackup(){
  const r = await fetch("/api/backup/file", {headers: {"Authorization": "Bearer " + TOKEN}});
  if(!r.ok){ toast("备份不存在，请先执行打包", true); return; }
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "n1bus-backup.tar.gz";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  toast("备份包开始下载");
}
async function loadAutostart(){
  const j = await api("/api/autostart");
  const el = document.getElementById("sysBootBody");
  if(!el) return;
  el.innerHTML = (j.units || []).map(u => {
    // 三态：
    //   受保护            → 不可操作
    //   static / masked  → systemctl 开关不了（snap.lxd.daemon 那种没有 [Install] 段的），
    //                      显示「–」而不是按钮 —— 否则点下去必然失败、状态还会跳回去
    //   enabled/disabled → 双向开关
    const canToggle = !u.protected && u.togglable !== false;
    const action = u.enabled ? "boot.disable" : "boot.enable";
    const text = u.enabled ? "取消开机自启" : "开机自启";
    const stateCell = u.protected ? dot("warn", "受保护，面板不允许改")
                                  : (u.enabled ? dot("info", "已启用")
                                               : (canToggle ? dot("mute", "未启用")
                                                            : dot("mute", "不可切换")));
    const actCell = canToggle
      ? `<button class="act ghost mini"
           onclick="runAction('${action}',{unit:'${esc(u.unit)}'},this)">${text}</button>`
      : "–";
    return `<tr><td class="mono">${esc(u.unit)}</td>
      <td>${u.running ? dot("ok", "运行中") : dot("mute", "未运行")}</td>
      <td>${stateCell}</td>
      <td class="num">${actCell}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="4" class="empty">没有可管理的服务单元</td></tr>';
  const meta = document.getElementById("sysBootMeta");
  if(meta){
    const n = (j.units || []).filter(u => u.enabled).length;
    meta.textContent = n + " 个开机自启服务";
  }
  const dk = document.getElementById("sysBootDk");
  if(dk) dk.innerHTML = (j.containers || []).map(c =>
    `<tr><td class="mono">${esc(c.name)}</td>
      <td class="mono hint" data-full="${esc(c.policy)}">${esc(POL_CN[c.policy] || c.policy)}</td>
      <td>${c.autostart ? dot("ok", "会自启") : dot("mute", "不会自启")}</td>
      <td class="num"><button class="act ghost mini"
        onclick="runAction('docker.autostart',{name:'${esc(c.name)}',on:${c.autostart ? "false" : "true"}},this)">
        ${c.autostart ? "取消开机自启" : "开机自启"}</button></td></tr>`).join("") ||
    '<tr><td colspan="4" class="empty">没有容器</td></tr>';
}
/* DeepSeek Harness 的进场链接与内嵌。
   dsh 只认「一次性启动 token 换会话 cookie」这一种进场方式：没有 token，
   /dsh/ 一律返回纯文本 401。该 token 每次启动时生成、仅输出到容器日志、
   不落盘 —— 因此链接必须由后端实时获取（后端从日志中提取最新一条），不能在页面中硬编码。 */
let _dshWatch = null;
/* 盯着 iframe 里的界面什么时候真的起来。
   为什么要盯而不是听 load 事件：那个事件在文档级 onload 就触发了，而 SPA 那时
   还停在「Loading plugins…」。同源，读得到 contentDocument。
   起来之后要做的事只有一件：撤掉预热态（让这一页回到正常的显示/隐藏逻辑）。
   刻意不显示"正在加载"之类文案 —— 预热在用户查看概览时已完成，
   实测热缓存下点开是 0.2 秒，再挂个转圈提示反而显得慢。 */
function watchDshFrame(){
  const box = document.getElementById("dshFrame");
  if(!box) return;
  if(_dshWatch) clearInterval(_dshWatch);
  const t0 = Date.now();
  _dshWatch = setInterval(() => {
    let ready = false;
    try{
      const d = box.contentDocument;
      const txt = d && d.body ? (d.body.innerText || "") : "";
      ready = txt.length > 20 && !/Loading plugins/i.test(txt);
    }catch(e){ /* 读不到（还没文档/跨源）就继续等，最多等到超时 */ }
    if(ready || Date.now() - t0 > 90000){
      clearInterval(_dshWatch); _dshWatch = null;
      const sec = document.getElementById("sub-harness");
      if(sec && currentView() !== "harness") sec.classList.remove("dsh-preload");
    }
  }, 1000);
}
/* 预热：趁用户仍在查看概览，先在后台初始化好 Harness。
   注意不能就挂在隐藏容器里 —— 见上面 .dsh-preload 那段注释。 */
function preloadDsh(){
  const sec = document.getElementById("sub-harness");
  if(!sec || currentView() === "harness") return;
  sec.classList.add("dsh-preload");
  loadDsh();
}
/* Esc 退出全屏：仅在外层 document 上监听不够 —— 用户一旦点击过 iframe 内的界面，
   焦点即进入该文档，按键事件不会冒泡到此处。因此以同源方式为其内部 document
   也挂一份（每次 iframe 内部导航都会换文档，靠 load 事件重新挂）。 */
function bindFrameEsc(box){
  if(box._dshEscBound) return;
  box._dshEscBound = true;
  box.addEventListener("load", () => {
    try{
      const d = box.contentDocument;
      if(!d) return;
      d.addEventListener("keydown", ev => {
        if(ev.key === "Escape" && document.body.classList.contains("dsh-focusing")) toggleDshFocus(false);
      });
    }catch(e){ /* 无法获取内部文档时忽略；工具条按钮同样可退出 */ }
  });
}
let DSH_URL = "", DSH_AT = 0;
/**
 * 重启 DeepSeek Harness 容器。
 *
 * 这一页就是 DSH 的控制面，重启目标固定为 dsh —— 在这儿选"重启 caddy"没有意义。
 * 别的容器要重启去「运行 → Docker」，那里本来就有每个容器自己的按钮。
 *
 * 走已有的白名单动作 docker.restart：是**重启**不是重建，容器文件系统不动、
 * 容器里的改动不会丢。想让改过的 /opt/dsh/.env 生效光靠它不够（Docker 只在创建容器时
 * 注入环境变量），那条路走 /opt/dsh/change-key.sh。
 */
/* Harness 的启动 / 停止 / 重启。三者只差一个 docker 动作名和事后的等待时间，
   合成一处 —— 复制三遍的话，以后改提示文案要改三个地方。
   与 KMS 那页的操作按钮保持同样的顺序和样式（见界面上的「启动 / 停止 / 重启」）。 */
async function dshPower(action){
  const META = {
    start:   {label: "启动", btn: "dshStart",   wait: 6000},
    stop:    {label: "停止", btn: "dshStop",    wait: 0},
    restart: {label: "重启", btn: "dshRestart", wait: 8000},
  }[action];
  if(!META) return;
  if(action === "stop" && !await askConfirm(
      "停止 <b>DeepSeek Harness</b> 容器？<br><br>容器停止后本页将无法访问，需点击「启动」恢复。",
      {title: "停止 DeepSeek Harness", danger: true, okText: "停止"})) return;
  const btn = document.getElementById(META.btn);
  const hint = document.getElementById("dshHint");
  if(btn) btn.disabled = true;
  if(hint){ hint.textContent = "正在" + META.label + " dsh …"; hint.classList.remove("hidden"); }
  try{
    const j = await api("/api/action", {method:"POST",
      body: JSON.stringify({name: "docker." + action, params: {name: "dsh"}})});
    const detail = j.error || j.detail || "";
    toast(j.ok ? ("已" + META.label + " dsh") : (META.label + "失败：" + detail), !j.ok);
    if(j.ok){
      if(action === "stop"){
        if(hint) hint.textContent = "容器已停止，界面无法访问；点击「启动」可恢复。";
      }else{
        if(hint) hint.textContent = "容器正在" + META.label + "，恢复后将自动获取新的入口链接…";
        // dsh 重启会换一次性 token；到点重新取一次
        setTimeout(() => { loadDsh(true); }, META.wait);
      }
    }else if(hint){
      hint.textContent = META.label + "失败：" + detail;
    }
  }catch(e){
    if(hint) hint.textContent = "";
    toast(META.label + "失败：" + e.message, true);
  }finally{
    setTimeout(() => { if(btn) btn.disabled = false; }, 12000);
  }
}
function startDsh(){ return dshPower("start"); }
function stopDsh(){ return dshPower("stop"); }
function restartDsh(){ return dshPower("restart"); }

/* --- KMS 授权服务（数据来自 v1m-dash）------------------------------- */
/* 合并之后对外只有一个面板，但 KMS 的数据仍由它原来的进程提供：那个进程同时管着
   vlmcsd 的 ini、supervisord 的进程控制与激活日志解析。所以这里只做展示，
   与 DeepSeek Harness 采用同一思路 —— 都作为「面板中的一页」，不伪装为自身实现。 */
/* vlmcsd 配置项的中文标签。展示与编辑表单共用这一份 —— 两处各写一遍的话，
   改了一处忘了另一处，界面上就会出现同一个键两种叫法。
   键名以真机的 vlmcsd.ini 为准（是 MaxIdleTime / WhitelistingLevel，不是
   ConnectionTimeout / WhiteListingLevel）。 */
const KMS_CFG_CN = {
  Port: "客户端端口", Listen: "监听地址", MaxWorkers: "并发上限",
  MaxIdleTime: "空闲断开（秒）", WhitelistingLevel: "白名单级别",
  CheckClientTime: "客户端校时", LogFile: "日志去向", LogVerbose: "记录激活明细",
};
async function loadKms(){
  const j = await api("/api/kms");
  const sm = j.summary || {}, lim = j.limits || {};
  const svc = sm.service || {}, st = sm.stats || {};
  const box = id => document.getElementById(id);
  const rowsHtml = pairs => pairs.map(([k, v]) =>
    `<div class="k">${esc(k)}</div><div class="v mono">${esc(v ?? "–")}</div>`).join("");
  /* 一组 kv 对半分成两列。所有卡片都用这一种排布 —— 之前第一张是两列、其余是单列，
     同一页里两种密度，看着不整齐。 */
  const kv2 = (pairs, a, b) => {
    const half = Math.ceil(pairs.length / 2);
    if(box(a)) box(a).innerHTML = rowsHtml(pairs.slice(0, half));
    if(box(b)) box(b).innerHTML = rowsHtml(pairs.slice(half));
  };
  // 时长文本：原样透传会把英文 "(up 1 天 9 小时)" 带进界面
  const upText = t => String(t || "").replace(/\s*\(up\s*/i, " · 已运行 ").replace(/\)$/, "");

  box("kmsMeta").textContent = sm.available ? (svc.running ? "运行中" : "已停止") : "不可用";
  // 「服务状态」「激活统计」本身就是并排的两块，各自的 kv 内部是单列，不再对半切
  if(box("kmsSvc")){
    box("kmsSvc").innerHTML = rowsHtml(sm.available ? [
      ["运行状态", svc.running ? "运行中" : "已停止"],
      ["服务单元", svc.unit || "–"],
      ["启动于", upText(svc.since)],
      ["版本", svc.version || "–"],
    ] : [["状态", sm.hint || "读不到数据"]]);
  }
  if(box("kmsStats")){
    box("kmsStats").innerHTML = rowsHtml(sm.available ? [
      ["累计激活", num(st.total || 0) + " 次"],
      ["今日", num(st.today || 0) + " 次"],
      ["30 天客户端", num(st.clients_30d || 0) + " 个"],
      ["最近一次", st.last || "–"],
    ] : [["状态", "–"]]);
  }

  /* --- vlmcsd 配置：键名配中文标签，两列排 --- */
  const cfg = sm.config || {};
  const ck = Object.keys(cfg);
  if(box("kmsCfgMeta")) box("kmsCfgMeta").textContent = ck.length ? (ck.length + " 项") : "";
  kv2(ck.map(k => [KMS_CFG_CN[k] || k, cfg[k]]), "kmsConfigA", "kmsConfigB");

  /* --- 激活限流 --- */
  if(box("kmsLimMeta")){
    box("kmsLimMeta").textContent = lim.available
      ? (lim.enabled ? ("每" + (lim.window_text || "天") + " " + num(lim.limit) + " 次") : "已关闭")
      : "不可用";
  }
  kv2(lim.available ? [
    ["限流状态", lim.enabled ? "已启用" : "已关闭"],
    ["触发条件", "单个来源在" + (lim.window_text || "同一周期") + "内超过 " + num(lim.limit) + " 次"],
    ["封禁时长", lim.ban_hours ? dur(lim.ban_hours * 3600) : "不限制"],
    ["待执行", "封 " + (lim.pending_ban || []).length + " · 解 " + (lim.pending_unban || []).length],
    ["豁免网段", (lim.skip || []).join("、") || "–"],
  ] : [["状态", lim.hint || "读不到数据"]], "kmsLimitsA", "kmsLimitsB");

  const banned = lim.banned || [];
  if(box("kmsBanMeta")) box("kmsBanMeta").textContent = banned.length ? (banned.length + " 个来源") : "";
  if(box("kmsBanned")){
    const why = {};
    (lim.pending_ban || []).forEach(x => { why[x.ip] = x.detail || x.reason || ""; });
    (lim.log || []).forEach(x => { if(x.action === "ban" && !why[x.ip]) why[x.ip] = x.detail || ""; });
    box("kmsBanned").innerHTML = banned.map(ip =>
      `<tr><td class="mono">${esc(ip)}</td><td class="hint">${esc(why[ip] || "—")}</td>
        <td class="num"><button class="act ghost mini"
          onclick="kmsAction('unban',{ip:'${esc(ip)}'},'解除 ${esc(ip)} 的激活限流封禁')">解除封禁</button>
        </td></tr>`).join("")
      || '<tr><td colspan="3" class="empty">当前没有封禁中的来源</td></tr>';
  }

  /* --- 激活明细 --- */
  const evs = sm.events || [];
  if(box("kmsEvMeta")) box("kmsEvMeta").textContent = evs.length ? ("最近 " + evs.length + " 条") : "";
  if(box("kmsEvents")){
    box("kmsEvents").innerHTML = evs.map(x =>
      `<tr><td class="mono hint">${esc(x.t || "")}</td><td class="mono">${esc(x.ip || "")}</td>
        <td class="mono hint">${esc(String(x.cmid || "").slice(0, 20))}</td>
        <td>${x.ok ? dot("ok", "成功") : dot("warn", "失败")}</td></tr>`).join("")
      || '<tr><td colspan="4" class="empty">还没有激活记录</td></tr>';
  }
}
/* KMS 的写操作统一走这里：一次确认 + 一次转发 + 刷新。
   面板自己不实现这些动作，它们由 v1m-dash 执行（见 n1bus.py 的 KMS_WRITES）。 */
async function kmsAction(op, params, label, danger){
  // 只有会中断服务的操作（停止、清空统计）才拦一道；启动与重启属于恢复性操作，
  // 点错也就是多等十几秒 —— 与 Harness 那页用同一条规则。
  if(danger && !await askConfirm("确认" + esc(label || op) + " ？<br><br>"
        + "该操作会改变 KMS 服务状态，结果记入审计。",
        {title: "KMS 授权服务", danger: true, okText: "执行"})) return;
  try{
    const j = await api("/api/kms/action", {method: "POST",
                                            body: JSON.stringify({op, params: params || {}})});
    if(j.ok) toast(esc(label || op) + " 已完成");
    else toast(esc(label || op) + " 失败：" + esc(j.error || (j.errors || []).join("；") || ""), true);
    loadKms();
  }catch(e){ toast("执行失败：" + esc(e.message), true); }
}
async function kmsEditConfig(){
  const j = await api("/api/kms");
  const cfg = ((j.summary || {}).config) || {};
  const keys = Object.keys(cfg);
  if(!keys.length) return toast("无法读取 vlmcsd 配置，不能编辑", true);
  // 字段直接从当前配置中生成，不硬编码一份列表 —— 真机的键名是 MaxIdleTime /
  // WhitelistingLevel，值与 ini 里原样一致（false/true 而不是 0/1）。
  // 若硬编码该列表，键名不一致时会出现「已保存但未生效」这类难以定位的问题。
  const f = await askForm(
    "保存后会自动重载；只有改 <b>Port</b> 或 <b>Listen</b> 才会重启进程。",
    keys.map(k => ({key: k, label: (KMS_CFG_CN[k] ? KMS_CFG_CN[k] + "（" + k + "）" : k),
                    value: cfg[k] != null ? String(cfg[k]) : ""})),
    {title: "vlmcsd 配置", okText: "保存"});
  if(!f) return;
  // 只提交真正改动过的项，以免重写整个 ini
  const params = {};
  keys.forEach(k => {
    const v = String(f[k] == null ? "" : f[k]).trim();
    if(v !== "" && v !== String(cfg[k])) params[k] = v;
  });
  if(!Object.keys(params).length) return toast("没有改动");
  await kmsAction("config", params, "保存 vlmcsd 配置");
}
async function kmsClearStats(){
  if(!await askConfirm("<b>清空全部激活统计</b>？<br><br>记录不可恢复，限流判断也会从零开始。",
                       {title: "清空统计", danger: true, okText: "确认清空"})) return;
  await kmsAction("stats_clear", {confirm: true}, "清空激活统计");
}
async function loadDsh(force){
  const meta = document.getElementById("dshMeta");
  const hint = document.getElementById("dshHint");
  const btn  = document.getElementById("dshOpen");
  const box  = document.getElementById("dshFrame");
  if(!meta || !box) return;
  // 切来切去不必每次都问后端：刚拿到过就直接用。
  // （容器重启会换 token，那时点「重新载入」即可。）
  if(DSH_URL && force !== true && Date.now() - DSH_AT < 60000) return;
  try{
    const j = await api("/api/dsh");
    DSH_AT = Date.now();
    DSH_URL = j.url || "";
    if(DSH_URL){
      meta.textContent = j.state || "running";
      meta.classList.remove("bad");
      hint.classList.add("hidden");
      btn.disabled = false;
      // 只在「链接变了」（容器重启换了 token，或首次进来）时才动 iframe。
      // 否则每次切回该视图都会把 Harness 整个重载一遍 —— 正在运行的会话即会中断。
      if(force === true || box.getAttribute("src") !== DSH_URL){
        box.setAttribute("src", DSH_URL);
        // 这里主动起轮询，而不是给 iframe 挂 onload：
        // iframe 在 HTML 里就位、脚本在页面底部，初始 about:blank 的 load 事件
        // 会赶在函数定义之前触发，挂 onload 只会得到一个 ReferenceError。
        watchDshFrame();
        bindFrameEsc(box);
        sizeDshFrame();
      }
    }else{
      DSH_URL = "";
      meta.textContent = "未接入";
      meta.classList.add("bad");
      hint.textContent = j.hint || "无法获取入口链接";
      hint.classList.remove("hidden");
      btn.disabled = true;
      box.removeAttribute("src");
    }
  }catch(e){
    DSH_URL = "";
    meta.textContent = "未接入";
    meta.classList.add("bad");
    hint.textContent = "读取失败：" + (e && e.message ? e.message : e);
    hint.classList.remove("hidden");
    btn.disabled = true;
    box.removeAttribute("src");
  }
}
function openDsh(){
  if(!DSH_URL){ toast("暂无可用入口链接", true); return; }
  // 新标签页打开：token 在 URL 中，使用 noopener 避免将 opener 交出
  window.open(DSH_URL, "_blank", "noopener");
}
/* 给 iframe 算一个**确定**的高度。
   为什么不靠 CSS：手机端 N1bus 是「.shell 改 column + 高度靠 min-height」的布局，
   整条链给不出确定高度，`height:100%` 会退化成 auto，iframe 就只剩 min-height
   那一小块（实测 340×360 —— DSH 界面严重变形即由该原因导致）。
   这里直接按视口减掉实际占用，简单可靠，窗口怎么变都跟着变。 */
function sizeDshFrame(){
  const box = document.getElementById("dshFrame");
  if(!box) return;
  // 卡片顶部现在有两行：h2 标题 + toolbar 操作条（原先是单行 .dsh-bar）。
  // 测量它们的实际高度而非硬编码数值 —— 操作条中的按钮数量还会变化。
  const head = document.querySelector("#sub-harness h2");
  const bar = document.querySelector("#sub-harness .toolbar");
  const headH = head ? head.getBoundingClientRect().height : 20;
  const barH = bar ? bar.getBoundingClientRect().height : 40;
  let reserved = headH + barH + 48;               // 标题 + 操作条 + 卡片自己的内边距
  if(!document.body.classList.contains("dsh-focusing")){
    const side = document.querySelector(".side");
    const topbar = document.querySelector(".topbar");
    if(side && window.innerWidth < 900) reserved += side.getBoundingClientRect().height;
    if(topbar) reserved += topbar.getBoundingClientRect().height;
    reserved += 34;                               // main / panel 的上下 padding
  }
  box.style.height = Math.max(300, Math.round(window.innerHeight - reserved)) + "px";
}
window.addEventListener("resize", sizeDshFrame);
/* 沉浸模式：把侧边栏和顶栏收起来，只留 Harness 这张卡片铺满当前页面。
   仅为「覆盖当前页面」，并非浏览器原生全屏 —— 后者需要用户手势授权，且会切换整个窗口，
   返回时易被误认为页面已丢失。按钮文字与样式随状态变化，Esc 也可退出。 */
function toggleDshFocus(on){
  const btn = document.getElementById("dshFull");
  const next = (typeof on === "boolean") ? on : !document.body.classList.contains("dsh-focusing");
  document.body.classList.toggle("dsh-focusing", next);
  if(btn){
    btn.innerHTML = next
      ? '<svg class="ico"><use href="#i-collapse"/></svg>退出全屏'
      : '<svg class="ico"><use href="#i-expand"/></svg>全屏';
    btn.title = next ? "退出全屏（Esc）" : "铺满整页（Esc 退出）";
    btn.classList.toggle("ghost", !next);   // 全屏时按钮变实心，好找
  }
  sizeDshFrame();                           // 沉浸/退出都会改变可用高度
}
document.addEventListener("keydown", e => {
  if(e.key === "Escape" && document.body.classList.contains("dsh-focusing")) toggleDshFocus(false);
});
async function loadSysSettings(){
  const j = await api("/api/system_settings");
  const el = document.getElementById("sysSetBody");
  if(!el) return;
  el.innerHTML = kvRows([
    ["时区", j.timezone],
    ["NTP", j.ntp ? (j.ntp_synced ? "已同步" : "已开启未同步") : "已关闭"],
    ["swap", (j.swaps || []).length
      ? j.swaps.map(s => s.device + " " + bytes(s.used) + "/" + bytes(s.size)).join("；")
      : (j.swap_total ? bytes(j.swap_used) + "/" + bytes(j.swap_total) : "没有")],
  ]);
  const meta = document.getElementById("sysSetMeta");
  if(meta) meta.textContent = (j.timezone || "?");
  const hint = document.getElementById("sysSetHint");
  if(hint) hint.textContent = (j.note || "") + "  " + (j.timezones_note || "");
}
function saveTimezone(){
  const v = (document.getElementById("sysTz").value || "").trim();
  if(!v){ toast("请填写时区（如 Asia/Shanghai）", true); return; }
  runAction("time.set_timezone", {tz: v});
}
/* --- 文件管理器 -------------------------------------------------------- */
/* 服务端所有写操作都过 safe_write_path（realpath 之后仍在白名单根内）并进审计；
   这里的前端只负责避免用户误操作 —— 删除/覆盖一律 askConfirm()。 */
let FMPATH = "";
let FMROOTS = [];
let FMEDITING = "";

function fmJoin(dir, name){
  return (dir.replace(/\/+$/, "") + "/" + name).replace(/\/+/g, "/");
}
function fmEscape(s){
  return String(s == null ? "" : s).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}
async function loadFiles(path){
  const j = await api("/api/files" + (path ? ("?path=" + encodeURIComponent(path)) : ""));
  const body = document.getElementById("fmBody");
  if(!body) return;
  if(j.error){
    body.innerHTML = `<tr><td colspan="4" class="empty">${esc(j.error)}</td></tr>`;
    const meta1 = document.getElementById("fmMeta");
    if(meta1) meta1.textContent = "";
    // 故意不覆盖 FMPATH：保留上一次成功的位置。否则输错一次，
    // 「刷新」与面包屑也会一并失效，需重新手动输入路径才能返回。
    if(!FMPATH){
      const crumb0 = document.getElementById("fmCrumb");
      if(crumb0) crumb0.innerHTML = `<span class="hint">尚未进入任何目录 —— 请从上方选择根目录，或直接填写绝对路径。</span>`;
    }
    toast(j.error, true);
    return;
  }
  FMPATH = j.path || "";
  FMROOTS = j.roots || [];

  const rootSel = document.getElementById("fmRoot");
  const writable = j.roots_writable || {};
  if(rootSel && rootSel.dataset.roots !== FMROOTS.join("|")){
    rootSel.dataset.roots = FMROOTS.join("|");
    // 该控件是「跳转器」而非状态显示：选择后即跳转，随后退回占位项。
    // 此前它兼作状态显示，在子目录中看起来像未生效，容易造成误解。
    rootSel.innerHTML = `<option value="">跳转到根目录…</option>` +
      FMROOTS.map(r => `<option value="${esc(r)}">${esc(r)}${writable[r] === false ? "（只读）" : ""}</option>`).join("");
  }
  rootSel && (rootSel.value = "");
  const rootsHint = document.getElementById("fmRoots");
  if(rootsHint){
    rootsHint.innerHTML = FMROOTS.map(r => writable[r] === false
      ? `<span style="opacity:.65">${esc(r)}（只读）</span>` : esc(r)).join("  ·  ");
  }
  const writeHint = document.getElementById("fmWriteHint");
  if(writeHint){
    const ro = FMROOTS.filter(r => writable[r] === false);
    writeHint.textContent = ro.length
      ? "注：" + ro.join("、") + " 由 systemd 的 ProtectSystem 挂成只读，仅可查看与下载，无法写入。"
      : "";
  }
  // 面包屑：当前路径逐级可点，可直接看出所处位置与可回退层级
  const crumb = document.getElementById("fmCrumb");
  if(crumb){
    const rootHit = FMROOTS.find(r => FMPATH === r || FMPATH.startsWith(r.replace(/\/+$/, "") + "/"));
    let html = "";
    if(rootHit){
      html += `<button class="act ghost mini" onclick="loadFiles('${fmEscape(rootHit)}')">${esc(rootHit)}</button>`;
      const rest = FMPATH.slice(rootHit.length).replace(/^\/+/, "");
      let acc = rootHit;
      for(const p of (rest ? rest.split("/") : [])){
        acc = acc.replace(/\/+$/, "") + "/" + p;
        html += `<span class="sep">/</span>` +
          `<button class="act ghost mini" onclick="loadFiles('${fmEscape(acc)}')">${esc(p)}</button>`;
      }
    } else {
      html += `<span class="hint mono">${esc(FMPATH || "（未选择目录）")}</span>`;
    }
    crumb.innerHTML = html;
  }
  const pathBox = document.getElementById("fmPath");
  if(pathBox && document.activeElement !== pathBox) pathBox.value = FMPATH;
  const meta = document.getElementById("fmMeta");
  if(meta) meta.textContent = (j.entries || []).length + " 项" +
    (j.truncated ? "（只显示前 200 条）" : "");

  body.innerHTML = (j.entries || []).map(e => {
    const full = fmJoin(FMPATH, e.name);
    const q = fmEscape(full);
    const acts = e.dir
      ? `<button class="act ghost mini" onclick="loadFiles('${q}')">打开</button>
         <button class="act ghost mini" onclick="fmRename('${q}', true)">重命名</button>
         <button class="act ghost mini" onclick="fmDelete('${q}', true)">删除</button>`
      : `<button class="act ghost mini" onclick="fmEdit('${q}')">编辑</button>
         <button class="act ghost mini" onclick="fmDownload('${q}')">下载</button>
         <button class="act ghost mini" onclick="fmRename('${q}', false)">重命名</button>
         <button class="act ghost mini" onclick="fmDelete('${q}', false)">删除</button>`;
    return `<tr>
      <td class="mono">${e.dir ? '<span class="tag info">目录</span> ' : ""}${esc(e.name)}${
        e.link ? ' <span class="tag mute">链接</span>' : ""}</td>
      <td class="num">${e.dir ? "–" : bytes(e.size)}</td>
      <td class="hint">${esc(e.mtime)}</td>
      <td class="num" style="white-space:nowrap">${acts}</td>
      </tr>`;}).join("") || '<tr><td colspan="4" class="empty">目录为空</td></tr>';
}
function fmReload(){ return loadFiles(FMPATH); }
/** 路径框内按回车等同点击「前往」。缺少该处理时，输入后按回车不会有任何响应。 */
function fmKey(ev){
  if(ev.key === "Enter" || ev.keyCode === 13){
    ev.preventDefault();
    fmGo();
  }
}
/** 下拉框是「跳转器」：选择后即跳转，随后退回占位项。 */
function fmGotoRoot(root){
  if(root) loadFiles(root);
}
function fmGo(){
  const box = document.getElementById("fmPath");
  if(!box) return;
  const v = box.value.trim();
  if(!v){ toast("请先填写绝对路径", true); return; }
  loadFiles(v);
}
function fmUp(){
  if(!FMPATH) return;
  const parent = FMPATH.replace(/\/[^/]+\/?$/, "") || "/";
  loadFiles(parent);
}
function fmPickUpload(){
  const box = document.getElementById("fmUpload");
  if(box) box.click();
}
async function fmUploadChosen(input){
  const files = Array.from(input.files || []);
  input.value = "";
  if(!files.length) return;
  if(!FMPATH){ toast("请先进入目录再上传", true); return; }
  let ok = 0, fail = [];
  for(const f of files){
    const target = fmJoin(FMPATH, f.name);
    try{
      // 走 api() 以复用 Bearer 与 401 处理；Content-Type 覆盖成二进制，
      // 服务端按原始 body 接收（不套 JSON，省去 base64 的编码膨胀）。
      const j = await api("/api/file/upload?path=" + encodeURIComponent(target), {
        method: "POST",
        headers: {"Content-Type": "application/octet-stream"},
        body: f,
      });
      if(j.ok) ok++; else fail.push(f.name + "：" + (j.error || "失败"));
    }catch(e){ fail.push(f.name + "：" + e.message); }
  }
  toast(`上传完成：成功 ${ok} 个` + (fail.length ? `，失败 ${fail.length} 个` : ""), fail.length > 0);
  if(fail.length){
    const hint = document.getElementById("fmHint");
    if(hint) hint.textContent = fail.join("；");
  }
  fmReload();
}
async function fmEdit(path){

  const j = await api("/api/file/read?path=" + encodeURIComponent(path));
  const box = document.getElementById("fmEditor");
  const ta = document.getElementById("fmEditText");
  if(!box || !ta) return;
  if(!j.ok){
    toast(j.error || "无法打开", true);
    return;
  }
  FMEDITING = j.path || path;
  box.classList.remove("hidden");
  document.getElementById("fmEditPath").textContent = FMEDITING;
  document.getElementById("fmEditInfo").textContent = bytes(j.size) + " · UTF-8 文本";
  document.getElementById("fmEditHint").textContent = "保存时会自动保留一份 .bak；写入操作记入审计。";
  ta.value = j.text || "";
  ta.focus();
}
function fmCloseEditor(){
  const box = document.getElementById("fmEditor");
  if(box) box.classList.add("hidden");
  FMEDITING = "";
}
async function fmSave(){
  if(!FMEDITING) return;
  const ta = document.getElementById("fmEditText");
  const r = await api("/api/file/write", {
    method: "POST",
    body: JSON.stringify({path: FMEDITING, text: ta ? ta.value : ""}),
  });
  if(!r.ok){ toast(r.error || "保存失败", true); return; }
  toast("已保存" + (r.backed ? "（原件存为 .bak）" : ""));
  fmReload();
}
async function fmDownload(path){
  // 不能用 <a href> 直接指过去：那条路不带 Bearer，服务端会 401。
  // 也不把 token 放入查询串 —— URL 会进入浏览器历史与访问日志。因此取回 blob 后下载。
  try{
    const r = await fetch("/api/file/download?path=" + encodeURIComponent(path), {
      headers: {"Authorization": "Bearer " + TOKEN},
    });
    if(!r.ok){
      toast("下载失败（HTTP " + r.status + "）", true);
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = path.split("/").pop() || "download";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }catch(e){
    toast("下载失败：" + e.message, true);
  }
}
async function fmNewFile(){
  const name = await askPrompt("新建文件名（仅填写名称）：", "", {title: "新建文件"});
  if(!name) return;
  const r = await api("/api/file/touch", {
    method: "POST",
    body: JSON.stringify({path: fmJoin(FMPATH, name.trim())}),
  });
  if(!r.ok){ toast(r.error || "新建失败", true); return; }
  toast("已新建 " + name.trim());
  fmReload();
}
async function fmNewDir(){
  const name = await askPrompt("新建目录名（仅填写名称）：", "", {title: "新建目录"});
  if(!name) return;
  const r = await api("/api/file/mkdir", {
    method: "POST",
    body: JSON.stringify({path: fmJoin(FMPATH, name.trim())}),
  });
  if(!r.ok){ toast(r.error || "新建失败", true); return; }
  toast("已新建目录 " + name.trim());
  fmReload();
}
async function fmRename(path, isDir){
  const old = path.split("/").pop();
  const name = await askPrompt("新名称（仅填写名称，不含路径）：", old, {title: "重命名"});
  if(!name || name === old) return;
  const r = await api("/api/file/rename", {
    method: "POST",
    body: JSON.stringify({path: path, name: name.trim()}),
  });
  if(!r.ok){ toast(r.error || "重命名失败", true); return; }
  toast("已重命名为 " + name.trim());
  fmReload();
}
async function fmDelete(path, isDir){
  // body 走 innerHTML（和 toast 一致），所以路径必须转义后再拼
  const body = isDir
    ? `删除目录 <b>${esc(path)}</b> ？\n目录必须为空；如需连同内容一并删除，下一步将再次确认。`
    : `删除文件 <b>${esc(path)}</b> ？\n此操作不可撤销。`;
  if(!await askConfirm(body, {title: "删除", danger: true, okText: "删除"})) return;
  let recursive = false;
  if(isDir){
    recursive = await askConfirm("连同目录里的<b>全部内容</b>一起删除？\n\n" +
                        "「确定」：连同全部内容删除（不可撤销）\n「取消」：仅删除空目录（目录非空时会失败）",
                        {title: "递归删除", danger: true, okText: "连同内容删除"});
  }
  const r = await api("/api/file/delete", {
    method: "POST",
    body: JSON.stringify({path: path, recursive: recursive}),
  });
  if(!r.ok){ toast(r.error || "删除失败", true); return; }
  toast("已删除 " + path.split("/").pop());
  fmReload();
}

/* --- 计划任务 ---------------------------------------------------------- */
let MTTASKS = [], MTACTIONS = [];
async function loadTasks(){
  const j = await api("/api/tasks");
  MTTASKS = j.tasks || []; MTACTIONS = j.actions || [];
  const meta = document.getElementById("mtMeta");
  if(meta) meta.textContent = MTTASKS.length + " 个任务 · " + (j.now || "");
  const sel = document.getElementById("mtAction");
  if(sel && sel.dataset.n !== String(MTACTIONS.length)){
    sel.dataset.n = String(MTACTIONS.length);
    sel.innerHTML = MTACTIONS.map(a =>
      `<option value="${esc(a.name)}">${esc(a.label)}（${esc(a.name)}）</option>`).join("");
    mtParams();
  }
  const body = document.getElementById("mtBody");
  if(body) body.innerHTML = MTTASKS.map(t => `<tr>
    <td class="mono">${esc(t.action)}</td>
    <td class="hint mono">${esc(JSON.stringify(t.params || {}))}</td>
    <td class="num">${t.every >= 3600 ? (t.every / 3600).toFixed(1) + " 小时"
      : Math.round(t.every / 60) + " 分"}</td>
    <td class="hint">${esc(t.last_run || "未执行")}</td>
    <td>${t.last_ok === true ? dot("ok", "成功") : t.last_ok === false
      ? dot("bad", "失败") : dot("mute", "未执行")}
      <span class="hint">${esc((t.last_result || "").slice(0, 40))}</span></td>
    <td class="num">
      <button class="act ghost mini" onclick="runTask(${Number(t.id)})">立即执行</button>
      <button class="act ghost mini" onclick="toggleTask(${Number(t.id)})">${
        t.enabled ? "停用" : "启用"}</button>
      <button class="act danger mini" onclick="delTask(${Number(t.id)})">删除</button></td>
    </tr>`).join("") || '<tr><td colspan="6" class="empty">暂无任务</td></tr>';
  const rec = document.getElementById("mtRecent");
  if(rec) rec.innerHTML = (j.recent || []).map(r => `<tr>
    <td class="hint mono">${esc(r.ts)}</td><td class="mono">${esc(r.action)}</td>
    <td>${r.ok ? dot("ok", "成功") : dot("bad", "失败")}
      <span class="hint">${esc((r.detail || "").slice(0, 60))}</span></td></tr>`).join("") ||
    '<tr><td colspan="3" class="empty">暂无执行记录</td></tr>';
  const hint = document.getElementById("mtHint");
  if(hint) hint.textContent = "周期 ≥ " + (j.min_every || 60) + " 秒；仅面板运行时执行";
}
function mtParams(){
  const sel = document.getElementById("mtAction");
  const box = document.getElementById("mtInputs");
  if(!sel || !box) return;
  const a = MTACTIONS.find(x => x.name === sel.value);
  box.innerHTML = ((a && a.params) || []).map(p =>
    `<input data-p="${esc(p)}" type="text" placeholder="${esc(p)}" style="width:140px">`).join("")
    || '<span class="hint">这个动作不需要参数</span>';
}
function _mtValues(){
  const sel = document.getElementById("mtAction");
  const a = MTACTIONS.find(x => x.name === sel.value) || {};
  const out = {};
  const box = document.getElementById("mtInputs");
  const inputs = box ? Array.prototype.slice.call(box.querySelectorAll("[data-p]")) : [];
  inputs.forEach(el => { if(el.value.trim()) out[el.getAttribute("data-p")] = el.value.trim(); });
  return out;
}
async function _taskPost(path, body, okMsg){
  const r = await fetch(path, {method: "POST",
    headers: {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  const bad = !j.ok;
  toast(bad ? ((j.error || (j.result && j.result.detail) || "操作失败"))
            : (okMsg || "完成"), bad);
  loadTasks();
}
function addTask(){
  const sel = document.getElementById("mtAction");
  if(!sel || !sel.value){ toast("请选择动作", true); return; }
  _taskPost("/api/tasks/add", {action: sel.value, params: _mtValues(),
    every: Number(document.getElementById("mtEvery").value || 3600)}, "任务已添加");
}
function runTask(id){ _taskPost("/api/tasks/run", {id: id}, "已触发执行"); }
function toggleTask(id){ _taskPost("/api/tasks/toggle", {id: id}, "已切换"); }
async function delTask(id){
  if(!await askConfirm("删除这个计划任务？", {title: "删除任务", danger: true, okText: "删除"})) return;
  _taskPost("/api/tasks/del", {id: id}, "任务已删除");
}

/* --- 告警与阈值 -------------------------------------------------------- */
async function loadAlerts(){
  const j = await api("/api/alerts");
  const c = j.config || {}, r = c.rules || {};
  const set = (id, v) => { const el = document.getElementById(id); if(el) el.value = v; };
  const chk = document.getElementById("alEnabled");
  if(chk) chk.checked = !!c.enabled;
  set("alWebhook", c.webhook || "");
  set("alCooldown", c.cooldown_min || 30);
  set("alDisk", r.disk_percent || 0);
  set("alMem", r.mem_percent || 0);
  set("alLoad", r.load_per_core || 0);
  set("alFailed", r.failed_units || 0);
  const meta = document.getElementById("alMeta");
  if(meta) meta.textContent = c.enabled ? ("已启用 · " + (c.webhook || "未填写 Webhook"))
                                        : "未启用";
  const hint = document.getElementById("alHint");
  if(hint) hint.textContent = j.note || "";
  const tbody = document.getElementById("alRecent");
  if(tbody) tbody.innerHTML = (j.recent || []).map(x => `<tr>
    <td class="hint mono">${esc(x.ts)}</td><td class="mono">${esc(x.metric)}</td>
    <td class="num">${x.value == null ? "–" : esc(x.value) + " / " + esc(x.threshold)}</td>
    <td>${x.ok ? dot("ok", "已发出") : dot("bad", "发送失败")}
      <span class="hint">${esc((x.detail || "").slice(0, 50))}</span></td></tr>`).join("") ||
    '<tr><td colspan="4" class="empty">暂无告警记录</td></tr>';
}
async function saveAlerts(){
  const g = id => document.getElementById(id);
  const body = {enabled: g("alEnabled").checked, webhook: g("alWebhook").value.trim(),
    cooldown_min: Number(g("alCooldown").value || 30),
    rules: {disk_percent: Number(g("alDisk").value || 0),
            mem_percent: Number(g("alMem").value || 0),
            load_per_core: Number(g("alLoad").value || 0),
            failed_units: Number(g("alFailed").value || 0)}};
  const r = await fetch("/api/alerts", {method: "POST",
    headers: {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  toast(j.ok ? "告警设置已保存" : ("保存失败：" + (j.error || r.status)), !j.ok);
  loadAlerts();
}
async function testAlert(){
  const r = await fetch("/api/alerts/test", {method: "POST",
    headers: {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    body: "{}"});
  const j = await r.json().catch(() => ({}));
  toast(j.ok ? "测试告警已发出" : ("发送失败：" + (j.detail || r.status)), !j.ok);
  loadAlerts();
}

async function loadNetInfo(){
  const j = await api("/api/netinfo");
  const el = document.getElementById("sysNetBody");
  if(!el) return;
  const ifs = j.interfaces || [];
  // 物理网卡需要优先可见；虚拟网卡（veth* 是 Docker 为每个容器创建的一对虚拟接口）数量会
  // 随容器数增长，且地址栏常为空，平铺显示会淹没真正重要的条目。
  // 所以分两组，虚拟那组默认收起、只留一个数量。
  const phys = ifs.filter(i => i.kind !== "virtual");
  const virt = ifs.filter(i => i.kind === "virtual");
  const ifRow = i => [i.name + (i.up ? "" : "（已断开）"),
                      (i.addrs || []).filter(a => a.indexOf("fe80:") !== 0).join(" ") || "无地址"];
  const pub = j.docker_published || [];
  el.innerHTML =
    "<h2>网卡</h2>" + kvRows(phys.map(ifRow), "没有网卡信息") +
    (virt.length
      ? '<details class="fold gap-sm"><summary>虚拟网卡 ' + virt.length +
        ' 个<span class="q">容器与会话专用</span></summary><div class="foldbody">' +
        kvRows(virt.map(ifRow)) + "</div></details>"
      : "") +
    "<h2>路由</h2>" + kvRows((j.routes || []).map(r => [
      r.dst || "default",
      (r.gateway ? "via " + r.gateway + " " : "") + "dev " + (r.dev || "?")]),
      "没有路由表") +
    "<h2>DNS</h2>" + kvRows((j.nameservers || []).map(n => ["nameserver", n]),
                            "未读到 resolv.conf") +
    (pub.length
      ? '<h2 class="gap-lg">Docker 发布的端口</h2>' +
        kvRows(pub.map(c => [c.container, c.ports]))
      : "") +
    // 这句是解释性说明，放在末尾作注解；原先顶在卡片最上方，像一条报错
    (j.note ? '<div class="hint gap-md">' + esc(j.note) + "</div>" : "");
}
async function loadDisks(){
  const j = await api("/api/disks");
  const el = document.getElementById("sysDiskBody");
  if(!el) return;
  const rows = [];
  const walk = (list, depth) => (list || []).forEach(d => {
    if(d.type === "loop" || d.type === "rom") return;      // snap 的 loop 与光驱，没必要看
    rows.push(["　".repeat(depth) + (d.name || "?"),
               [d.size, d.fstype, d.mountpoint].filter(Boolean).join(" ")]);
    walk(d.children, depth + 1);
  });
  walk(j.blockdevices, 0);
  // 用量改为条形展示。原先的单行文本需要读者自行比较，
  // 条形可直接看出哪个分区接近占满；超过 80% / 90% 切换警示色。
  const mounts = j.mounts || [];
  el.innerHTML = "<h2>块设备</h2>" + kvRows(rows, "无块设备信息") +
    '<h2 class="gap-lg">挂载点用量</h2>' +
    (mounts.length
      ? '<div class="disk-use">' + mounts.map(m => {
          const p = Math.max(0, Math.min(100, +m.percent || 0));
          const cls = p >= 90 ? "bad" : (p >= 80 ? "warn" : "");
          return '<div class="du-row">' +
            '<span class="mono du-mount">' + esc(m.mount) + "</span>" +
            '<span class="bar ' + cls + '"><i style="width:' + p.toFixed(1) + '%"></i></span>' +
            '<span class="mono du-num">' + bytes(m.used) + " / " + bytes(m.total) +
            " · " + p.toFixed(1) + "%</span></div>";
        }).join("") + "</div>"
      : '<div class="hint">没有挂载点</div>') +
    (j.smart_note ? '<div class="hint gap-md">' + esc(j.smart_note) + "</div>" : "");
}
/* 进程表命令列只显示程序名；完整命令行放 data-full，点击/悬停/长按查看 */
function cmdName(a){
  const first = String(a || "").trim().split(/\s+/)[0] || "";
  return first.split("/").pop() || "?";
}
/* 进程表的「用户」列：系统账户名往往很长（systemd-resolve / systemd-timesync），
   而进程表本身列就多。长名字统一显示成 sys，把宽度让给「命令」列；
   root / docker 这种一眼认得出的短名保留。 */
function userLabel(u){
  const s = String(u || "").trim();
  return s.length > 6 ? "sys" : (s || "?");
}
async function loadProcesses(){
  const by = document.getElementById("prBy").value;
  const j = await api("/api/processes?by=" + by);
  const c = j.counts || {};
  document.getElementById("prMeta").textContent = "共 " + num(c.total) + " 个进程 · 僵尸 " + num(c.zombie);
  patchRows(document.getElementById("prBody"), (j.top || {}).top || [], p => p.pid, p => [
    {c: "mono", h: esc(p.pid)},
    {h: esc(userLabel(p.user))},
    {c: "num", h: p.cpu.toFixed(1)},
    {c: "num", h: p.mem.toFixed(1)},
    {c: "num", h: bytes(p.rss)},
    {c: "hint", h: dur(p.etimes)},
    {c: "mono", s: "cursor:pointer", a: `data-full="${esc(p.args)}"`, h: esc(cmdName(p.args))},
    {c: "num", h:
      `<button class="act danger mini" title="结束这个进程（SIGKILL：立即终止，进程无法拦截）"
        onclick="runAction('process.kill',{pid:'${esc(p.pid)}'})">终止</button>`},  ], null, '<tr><td colspan="8" class="empty">无数据</td></tr>');
}

/* --- 日志 ------------------------------------------------------------- */
function logKindChanged(){
  const file = document.getElementById("lgKind").value === "file";
  document.getElementById("lgPath").classList.toggle("hidden", !file);
  document.getElementById("lgUnit").classList.toggle("hidden", file);
}
async function loadLogs2(){
  const kind = document.getElementById("lgKind").value;
  const lines = document.getElementById("lgLines").value;
  let url, meta;
  if(kind === "file"){
    const p = document.getElementById("lgPath").value.trim();
    if(!p) return toast("路径必填，且需在 /var/log 下", true);
    url = "/api/log?lines=" + lines + "&path=" + encodeURIComponent(p);
    meta = p;
  } else {
    const unit = document.getElementById("lgUnit").value;
    url = unit ? ("/api/service/logs?lines=" + lines + "&unit=" + encodeURIComponent(unit))
               : ("/api/journal?lines=" + lines);
    meta = unit || "journald 全量";
  }
  const j = await api(url);
  const metaText = meta + " · " + (j.lines || []).length + " 行";
  document.getElementById("lgMeta").textContent = metaText;
  const head = document.getElementById("lgMetaHead");
  if(head) head.textContent = metaText;          // 折叠着也能看到来源与行数
  document.getElementById("lgBox").innerHTML = (j.lines || []).map(l =>
    `${logLine(l)}`
    ).join("") || `<div class="empty">${esc(j.error || "无内容")}</div>`;
}

/* --- 动作与审计 ------------------------------------------------------- */
async function loadActions(){
  const j = await api("/api/meta");
  ACTIONS = j.actions || [];
  // 动作按名称前缀归到所属模块 —— 30 个动作平铺会形成一条几百像素的长列表，
  // 分组后能一眼看出"清理类有哪些、服务类有哪些"。
  const GROUP_LABELS = { apt: "软件包 · apt", backup: "备份", boot: "开机自启",
                         clean: "清理", docker: "Docker", journal: "日志",
                         panel: "面板自身", process: "进程", service: "systemd 服务",
                         system: "系统", systemd: "systemd", time: "时间 · 时区",
                         ufw: "防火墙", other: "其它" };
  const ORDER = ["clean", "journal", "apt", "docker", "systemd", "service", "process",
                 "ufw", "time", "boot", "backup", "panel", "system", "other"];
  const groups = new Map();
  for(const a of ACTIONS){
    const key = String(a.name || "").split(".")[0] || "other";
    const label = GROUP_LABELS[key] || key;
    if(!groups.has(label)) groups.set(label, []);
    groups.get(label).push(a);
  }
  const keys = [...groups.keys()].sort((x, y) => {
    const ix = ORDER.indexOf(ORDER.find(k => GROUP_LABELS[k] === x) || "other");
    const iy = ORDER.indexOf(ORDER.find(k => GROUP_LABELS[k] === y) || "other");
    return ix - iy;
  });
  // 默认展开的组：本机日常动作都在其中，其余折叠 —— 一屏可看全 13 个模块名
  const OPEN_BY_DEFAULT = new Set(["清理", "Docker", "systemd 服务"]);
  document.getElementById("acList").innerHTML = keys.map(k => `
    <details class="agroup"${OPEN_BY_DEFAULT.has(k) ? " open" : ""}>
      <summary>${esc(k)}<span class="cnt">${groups.get(k).length} 个动作</span></summary>
      <div class="aglist">
      ${groups.get(k).map(a => `
        <div class="aitem">
          <div class="alabel">
            <b>${esc(a.label)}</b>
            ${a.danger ? '<span class="tag bad">危险</span>' : '<span class="tag mute">安全</span>'}
            <span class="hint mono adesc">${esc(a.name)} · ${esc(a.desc)}</span>
          </div>
          <div class="aparams">
            ${(a.params || []).map(p => `<input type="text" id="p_${esc(a.name)}_${esc(p)}" placeholder="${esc(p)}">`).join("")}
            <button class="act ${a.danger ? "danger" : ""} mini" onclick="runAction('${esc(a.name)}')">
              <svg class="ico"><use href="#i-play"/></svg>执行</button>
          </div>
        </div>`).join("")}
      </div>
    </details>`).join("");
}
function collectParams(name){
  const a = ACTIONS.find(x => x.name === name) || {};
  const out = {};
  for(const p of (a.params || [])){
    const el = document.getElementById("p_" + name + "_" + p);
    out[p] = el ? el.value.trim() : "";
  }
  return out;
}
/* 开机自启开关的乐观更新：点击的瞬间就把按钮翻过去。
   为什么需要它 —— /api/autostart 走的是 `systemctl list-unit-files`，这条命令本身在
   本机（2 核）上就需要 ~0.95 秒；写操作后服务端缓存失效，等它回来再重建 DOM，
   用户感知就是「点击后要等一秒才变化」。先就地切换，再用真值校正。 */
function optimisticBoot(btn, name, params){
  if(!btn) return;
  const isBoot = name === "boot.enable" || name === "boot.disable";
  const isDk = name === "docker.autostart";
  if(!isBoot && !isDk) return;
  const on = isBoot ? (name === "boot.enable") : !!params.on;
  // 只翻按钮文字。
  // 不要修改「状态」列 —— 该列由 dot() 绘制的圆点构成，手工改 class 会产生一个
  // 无人维护的孤立节点：先出现，待真值返回重建 DOM 时又消失，表现为闪烁。
  // 状态列交给 loadAutostart 用后端真值统一重绘。
  btn.textContent = on ? "取消开机自启" : "开机自启";
}
async function runAction(name, params, btn){
  const a = ACTIONS.find(x => x.name === name);
  if(!a){ toast("未知动作：" + esc(name), true); return; }
  const p = params || collectParams(name);
  const label = a.label + "　" + a.name + (Object.keys(p).length ? "\n参数：" + JSON.stringify(p) : "");
  if(a.danger && !await askConfirm("确认执行 <b>" + esc(a.label) + "</b> ？\n\n" +
      "该动作会改变系统状态，结果记入审计。" +
      (Object.keys(p).length ? "\n参数：" + esc(JSON.stringify(p)) : ""),
      {title: "危险动作", danger: true, okText: "执行"})) return;
  optimisticBoot(btn, name, p);          // 先翻按钮，别等后端
  toast("执行中：" + esc(a.label));
  try{
    const j = await api("/api/action", {method:"POST", body: JSON.stringify({name, params: p})});
    const head = (j.ok ? "✔ 成功" : "✘ 失败") + "　" + a.label + "　(" + (j.argv || []).join(" ") + ")" +
                 (j.demo ? "　[演示模式]" : "") + (j.audit ? "　审计已记录" : "");
    document.getElementById("acOut").textContent =
      head + "\n" + "─".repeat(60) + "\n" + ((j.out || "") + (j.err ? "\n[stderr]\n" + j.err : "") ||
      (j.error || j.detail || "（无输出）"));
    document.getElementById("acMeta").textContent = nowText();
    toast(j.ok ? esc(a.label) + " 成功" : esc(a.label) + " 失败：" + esc(j.error || j.detail || ""), !j.ok);
    loadAudit();
    refreshRelated(name);
  }catch(e){ toast("执行失败：" + esc(e.message), true); }
}
/* 动作成功之后要刷哪些面板。
   第一版是一串 name.startsWith("service.") / ===("ufw.deny") 的逐个判断，
   漏掉的动作（clean.* / boot.* / time.* / fail2ban.unban / backup.create …）
   执行完面板上仍是旧值 —— 表现为「点击按钮后状态不刷新」。
   第二版改为"按模块前缀映射到对应**视图**"，不再遗漏，但粒度太粗：点击一次
   「取消开机自启」会调 loadTab("system")，而那个视图一次要拉 7 个接口
   （CDP 实测一个动作带出 8 个请求、5 秒）。现在直接映射到**要重载的那几个 loader**。 */
const ACTION_RELOAD = [
  // 动作前缀 → loader 名。更具体的写在前面（docker.autostart / docker.prune_images）。
  [["docker.autostart"], ["autostart"]],            // 容器开机自启开关，在「系统」页
  [["docker.prune_images"], ["images", "cleanup"]],
  [["docker.rm"], ["docker", "autostart"]],
  [["docker."], ["docker"]],                        // 其余 docker.* 是容器列表
  [["service.", "systemd."], ["services"]],
  [["boot."], ["autostart"]],                       // 服务开机自启开关
  [["process."], ["net"]],
  [["ufw.", "fail2ban."], ["security"]],
  [["clean.", "journal.", "apt."], ["cleanup"]],
  [["backup."], ["backup"]],
  [["time."], ["syssettings"]],
];
function refreshRelated(name){
  // 面板自身/整机将要重启，刷新没有意义（页面随后会断开）
  if(name === "panel.restart" || name === "system.reboot") return;
  // 容器被删掉了：日志面板要收起来，否则还挂着一个不存在的容器
  if(name === "docker.rm"){
    const panel = document.getElementById("dkPanel");
    if(panel) panel.classList.add("hidden");
    dkCur = "";
  }
  for(const [prefixes, keys] of ACTION_RELOAD){
    if(prefixes.some(p => name.startsWith(p))){
      for(const k of keys){
        const fn = LOADER_FN[k];
        if(fn) fn();
      }
      return;
    }
  }
  // 没登记的动作（account.* / 以后新增的）：只刷当前看得见的那些 loader，
  // 不做"整个视图重来"—— 那是该版本要修正的问题。
  const view = currentView();
  for(const it of (VIEWS[view] || [])){
    if(it.fold && !foldOpen(it.fold)) continue;
    const fn = LOADER_FN[it.k];
    if(fn) fn();
  }
}
/* 批量重启：逐个走已有的白名单动作，每个都单独过一次审计；失败各自可见 */
async function restartFailedUnits(){
  const bad = (UNITS || []).filter(u => u.active === "failed");
  if(!bad.length){ toast("没有处于失败状态的单元"); return; }
  if(!await askConfirm("重启这 <b>" + bad.length + "</b> 个失败单元？\n\n" +
              esc(bad.map(u => "· " + u.unit).join("\n")),
              {title: "批量重启", okText: "重启"})) return;
  bad.forEach((u, i) => setTimeout(() => runAction("service.restart", {unit: u.unit}), i * 1200));
}
async function restartAllContainers(){
  const names = (DK_CONTAINERS || []).map(c => c.name).filter(Boolean);
  if(!names.length){ toast("没有容器"); return; }
  if(!await askConfirm("重启这 <b>" + names.length + "</b> 个容器？\n\n" + esc(names.map(n => "· " + n).join("\n")) +
              "\n\n反代容器也在里面的，界面会短暂断开。",
              {title: "批量重启", okText: "重启"})) return;
  names.forEach((n, i) => setTimeout(() => runAction("docker.restart", {name: n}), i * 1200));
}
/* 设置页「关于」：版本与主机信息。
   面板自身的单元状态也放在这里 —— 它原先属于「面板与系统」卡片，那张卡随重启按钮
   一起移到了顶栏，剩下这点信息归到「关于」最合适，因为它描述的就是这个面板自己。
   注意：这个函数与下面那个是同一个（历史上重复定义过一次，靠函数提升互相覆盖，
   结果是新版代码写入后未生效）—— 仅保留下方那一处。 */
function nowText(){
  const d = new Date();
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
async function loadAudit(){
  // 多取一些：加了筛选之后要保持列表里还是有内容可看（每条都能点开看全文）
  const j = await api("/api/audit?lines=200");
  const all = j.entries || [];
  // 动作名本身就是「模块.动词」（service.restart / ufw.deny / file.write …），
  // 前缀即模块 —— 于是分组不用额外维护一张表，从数据里推出来就行。
  const groups = [...new Set(all.map(x => String(x.action || "").split(".")[0]).filter(Boolean))].sort();
  const sel = document.getElementById("auGroup");
  if(sel){
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部模块</option>' +
      groups.map(g => `<option value="${esc(g)}">${esc(g)}</option>`).join("");
    if(groups.indexOf(cur) >= 0) sel.value = cur;     // 保留用户已选的筛选
  }
  const wantGroup = sel ? sel.value : "";
  const failOnly = !!(document.getElementById("auFailOnly") || {}).checked;
  const e = all.filter(x =>
    (!wantGroup || String(x.action || "").indexOf(wantGroup + ".") === 0) &&
    (!failOnly || !x.ok));
  const ah = document.getElementById("auHint");
  if(ah) ah.textContent = (wantGroup || failOnly)
    ? ("筛出 " + e.length + " / " + all.length + " 条") : "";
  document.getElementById("auMeta").textContent = all.length
    ? (e.length === all.length ? ("最近 " + all.length + " 条")
                               : (e.length + " / " + all.length + " 条"))
    : "暂无记录";
  // 动作、对象、说明都直接铺在表里。原先它们塞在一个 data-full 里、只在点击时
  // 仅在点击时以悬浮提示显示，表格「详情」列固定显示「点击查看」—— 关键信息被隐藏，
  // 而该列本身不提供任何信息。
  document.getElementById("auBody").innerHTML = e.map(x => {
    const other = [x.target, x.detail].filter(Boolean).join(" · ");
    return `<tr>
      <td class="mono hint">${esc(String(x.ts || "").slice(5, 16))}</td>
      <td class="mono">${esc(x.action || "")}</td>
      <td class="mono">${esc(x.ip)}</td>
      <td>${x.ok ? dot("ok", "成功") : dot("bad", "失败")}</td>
      <td class="hint" style="white-space:normal">${esc(other || "—")}</td></tr>`;
  }).join("") || '<tr><td colspan="5" class="empty">没有匹配的记录</td></tr>';
}

/* --- 账户 ------------------------------------------------------------- */
const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString("zh-CN") : "–";
/* 设置页「关于」：只放**静态**的身份信息 —— 版本、启动时间、主机与运行环境。
   刻意不放两类东西：
     * 面板自身的 active/running：面板若不可用则无法打开本页面，这行信息永远显示正常；
     * 失败单元：那是动态状态，归属在「运行 → 服务」（那里有完整列表与批量重启）。
   把动态状态放入「关于」，只会让这张卡片既不像静态信息、又不如专职页面实用。 */
async function loadAbout(){
  const el = document.getElementById("stAbout");
  if(!el) return;
  let info = {}, up = {};
  try{ const j = await api("/api/overview"); info = j.info || {}; up = j; }catch(e){ /* 忽略 */ }
  const ver = "v" + (META.version || "?");
  const bv = document.getElementById("abVer");
  if(bv) bv.textContent = ver;
  el.innerHTML = [
    ["版本", ver],
    ["启动于", META.started],
    ["运行时长", up.uptime_text],
    ["主机名", info.hostname],
    ["系统", info.os],
    ["内核", info.kernel],
    ["架构", info.arch],
    ["Python", info.python],
  ].map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v mono">${esc(v ?? "–")}</div>`).join("");
}
async function loadAccounts(){
  const j = await api("/api/accounts");
  document.getElementById("acBody").innerHTML = (j.users || []).map(u =>
    `<tr><td class="mono">${esc(u.name)}${u.name === j.me ? ' <span class="tag info">自己</span>' : ""}</td>
      <td class="hint">${esc(fmtTime(u.created))}</td>
      <td class="hint">${esc(fmtTime(u.last_login))}</td>
      <td class="num">${num(u.sessions)}</td>
      <td class="num">
        <button class="act ghost mini" onclick="changePassword('${esc(u.name)}')">修改密码</button>
        <button class="act danger mini" onclick="deleteAccount('${esc(u.name)}')">删除</button>
      </td></tr>`).join("") || '<tr><td colspan="5" class="empty">无账户</td></tr>';
  document.getElementById("acMe").textContent = "当前登录：" + (j.me || "?");
}
/* 表单进弹窗：列表页只留「列表 + 按钮」，不把表单常驻页面占用版面。
   这是 1Panel 一类面板的通行做法，也让账户卡片展开后依然短。 */
async function createAccount(){
  const f = await askForm("请输入新账户的用户名与密码，密码至少 8 位。",
    [{key: "name", label: "用户名", placeholder: "例如 admin"},
     {key: "password", label: "密码（至少 8 位）", type: "password"}],
    {title: "新增账户", okText: "创建"});
  if(!f) return;
  const name = (f.name || "").trim(), password = f.password || "";
  if(!name || !password) return toast("用户名与密码均不能为空", true);
  const j = await api("/api/accounts/create", {method:"POST",
                                               body: JSON.stringify({name, password})});
  if(j.ok){ toast("已创建账户 " + esc(name)); loadAccounts(); }
  else toast(esc(j.error || "创建失败"), true);
}
/* name 为空表示修改本人密码；由表格行内的「修改密码」按钮或工具条按钮调用。
   两种情形都要用**操作者本人的当前密码**确认一次，这一点在弹窗里写明。 */
async function changePassword(name){
  const target = name || ME;
  const f = await askForm(
    "修改 <b>" + esc(target) + "</b> 的密码。需使用<b>本人当前密码</b>验证身份。",
    [{key: "password", label: "新密码（至少 8 位）", type: "password"},
     {key: "actor_password", label: "本人当前密码", type: "password"}],
    {title: "修改密码", okText: "保存"});
  if(!f) return;
  const password = f.password || "", actor_password = f.actor_password || "";
  if(!password) return toast("请填写新密码", true);
  if(!actor_password) return toast("需提供本人当前密码", true);
  const j = await api("/api/accounts/password", {method:"POST",
    body: JSON.stringify({name: name || "", password, actor_password})});
  if(j.ok){
    toast("密码已更新" + (name && name !== ME ? "：" + esc(name) : "") +
          ((name || ME) === ME ? "，会话已失效，请重新登录" : ""));
    if((name || ME) === ME) logout(); else loadAccounts();
  } else toast(esc(j.error || "修改失败"), true);
}
async function deleteAccount(name){
  if(!await askConfirm("删除账户 <b>" + esc(name) + "</b> ？\n该账户的会话立即失效；最后一个账户不可删除。",
                       {title: "删除账户", danger: true, okText: "删除"})) return;
  const j = await api("/api/accounts/delete", {method:"POST", body: JSON.stringify({name})});
  if(j.ok){ toast("已删除 " + esc(name)); if(name === ME) logout(); else loadAccounts(); }
  else toast(esc(j.error || "删除失败"), true);
}
async function logoutAll(){
  if(!await askConfirm("退出本账户的全部会话？\n当前浏览器也会一起退出，需要重新登录。",
                       {title: "退出全部会话", danger: true, okText: "退出"})) return;
  const j = await api("/api/accounts/logout-all", {method:"POST"});
  toast("已退出 " + num(j.dropped) + " 个会话");
  logout();
}

/* --- 标签页与轮询 ----------------------------------------------------- */
/* 9 个主标签；「运行」「安全」「系统」各带三个子视图，一次只加载看得见的那一个 */
const TABS = ["overview", "ext", "runtime", "security", "system", "files", "tasks", "logs", "settings"];
/* 一个 loader = 刷新一块数据。这份"名字 → 函数"的表是唯一的索引：
   视图加载（VIEWS）和「动作之后刷什么」（ACTION_RELOAD）都从这里取，
   于是既不会漏（新增 loader 只写一处），也不会一刷就刷一整个视图。 */
const LOADER_FN = {
  overview: loadOverview,
  services: loadServices,
  docker: loadDocker,
  images: loadImages,
  net: loadNet,
  processes: loadProcesses,
  security: loadSecurity,
  cleanup: loadCleanup,
  backup: loadBackup,
  files: () => loadFiles(FMPATH),
  dsh: loadDsh,
  kms: loadKms,
  netinfo: loadNetInfo,
  autostart: loadAutostart,
  syssettings: loadSysSettings,
  disks: loadDisks,
  tasks: loadTasks,
  logs: loadLogs2,
  audit: loadAudit,
  accounts: loadAccounts,
  about: loadAbout,
  alerts: loadAlerts,
};
/* 视图 = 由哪几块数据拼成。fold 写的那个 id 属于某个折叠块：
   轮询时它收着就跳过（后台请求数少三到五成），展开时立刻补一次。
   点导航进视图、点「刷新」仍是全量拉齐 —— 收起块的汇总数字不该是空的。

   安全与系统两页各自再分子视图。原因是单页容纳八张卡片时只能以「默认折叠」掩盖页面长度，
   而折叠只是隐藏问题：展开后仍是一屏放不下的长页。拆成三组之后每页两三张卡，
   卡片就能平铺展开。参考 1Panel 的做法 —— 它不折叠，靠的是每页只放少量卡片。 */
const VIEWS = {
  overview: [{k: "overview"}],
  // Harness 这个视图只有一块内容：把容器里的 agent 面板嵌进 iframe
  // —— 扩展功能：KMS 授权服务与 DeepSeek Harness ——
  kms: [{k: "kms"}],
  harness: [{k: "dsh"}],
  // —— 运行 ——
  services: [{k: "services"}],
  docker: [{k: "docker"}, {k: "images", fold: "dkImages"}],
  net: [{k: "net"}, {k: "processes", fold: "prBody"}],
  // —— 安全：三组都是同一份 /api/security，拆的只是展示 ——
  guard: [{k: "security"}],
  faillog: [{k: "security"}],
  svcstate: [{k: "security"}],
  // —— 系统 ——
  maintain: [{k: "cleanup", fold: "sysCleanBody"}, {k: "backup", fold: "sysBkBody"}],
  netstore: [{k: "netinfo", fold: "sysNetBody"}, {k: "disks", fold: "sysDiskBody"}],
  syscfg: [{k: "autostart", fold: "sysBootBody"}, {k: "syssettings", fold: "sysSetBody"}],
  // 文件页以手动刷新为主（写操作自身会 reload），不纳入轮询
  files: [{k: "files"}],
  tasks: [{k: "tasks"}],
  logs: [{k: "logs", fold: "lgBox"}, {k: "audit", fold: "auBody"}],
  settings: [{k: "accounts"}, {k: "about", fold: "stAbout"}, {k: "alerts"}],
};
/* 哪些主标签带子视图。子视图 id 必须全局唯一 —— 全局的 subview 容器靠它定位。 */
const SUBS = {
  ext: ["kms", "harness"],
  runtime: ["services", "docker", "net"],
  security: ["guard", "faillog", "svcstate"],
  system: ["maintain", "netstore", "syscfg"],
};
const SUB_DEFAULT = {ext: "kms", runtime: "services",
                     security: "guard", system: "maintain"};
/* 子视图 id → 它属于哪个主标签，由 SUBS 推出来，以免两处各自编写而失去同步 */
const SUB_OWNER = {};
for(const m of Object.keys(SUBS)) for(const id of SUBS[m]) SUB_OWNER[id] = m;
/* 每个主标签各自记住上次看的子视图，互不干扰（以前只有一个全局字符串，
   加上第二、第三个带子标签的页面就会互相串）。 */
const curSub = {};
/* 各视图自己的刷新间隔（毫秒）；没列出来的（日志）不自动刷新 */
const REFRESH = {overview: 5000, services: 10000, docker: 10000, net: 15000,
                 guard: 15000, faillog: 15000, svcstate: 30000,
                 kms: 60000,
                 maintain: 60000, netstore: 60000, syscfg: 60000,
                 accounts: 30000, tasks: 30000};
let lastPoll = {};
/* 这块数据在不在看得见的折叠块里？拿块里某个元素的 id 去找最近的 details.fold。
   元素不在页面上（视图还没渲染/被移除）就当它是开的 —— 宁可多拉一次，
   也不能把该显示的内容留空。 */
function foldOpen(elId){
  const el = document.getElementById(elId);
  if(!el || !el.closest) return true;
  const box = el.closest("details.fold");
  return !box || box.open;
}
function loadTab(id, lazy){
  lastPoll[id] = Date.now();          // 刚加载过，轮询别立刻又来一次
  for(const it of (VIEWS[id] || [])){
    if(lazy && it.fold && !foldOpen(it.fold)) continue;   // 收着的块，这一轮不拉
    const fn = LOADER_FN[it.k];
    if(fn) try{ fn(); }catch(e){ toast("加载失败：" + esc(e.message), true); }
  }
}
/* 展开一个折叠块：如果它里面的数据刚才被轮询跳过了，展开就立刻补一次。
   （toggle 不冒泡，所以在捕获阶段听。） */
document.addEventListener("toggle", ev => {
  const box = ev.target;
  if(!box || !box.matches || !box.matches("details.fold") || !box.open) return;
  for(const it of (VIEWS[currentView()] || [])){
    const el = it.fold ? document.getElementById(it.fold) : null;
    if(el && box.contains(el)){
      const fn = LOADER_FN[it.k];
      if(fn) try{ fn(); }catch(e){ /* 展开时拉不到就等下一轮 */ }
    }
  }
}, true);
/* ---------- 视图切换：导航与子导航都走这里 ---------- */
function viewLabel(main, sub){
  // 每个带子标签的页面都有自己的 .subnav，所以按页面 id 精确定位
  const sel = sub ? "#subnav-" + main + " button[data-sub='" + sub + "']"
                  : "nav button[data-tab='" + main + "']";
  const el = document.querySelector(sel);
  return el ? el.textContent.trim() : (sub || main);
}
/* fromUser=true 表示这是用户亲自触发的导航。
   只有这种调用才「钉住」视图，让 boot() 的收尾落位不再覆盖它。 */
function showView(main, sub, fromUser){
  if(fromUser) viewPinned = true;
  // 沉浸模式下离开 Harness 就自动退出，避免将整个页面锁定在该页面上
  if(document.body.classList.contains("dsh-focusing")) toggleDshFocus(false);
  curTab = main;
  // 子视图：先把这一页当前该看哪个确定下来（每页各记各的），再只显示它、隐藏其余
  const subs = SUBS[main];
  if(subs){
    if(sub && subs.includes(sub)) curSub[main] = sub;
    if(!curSub[main]) curSub[main] = SUB_DEFAULT[main] || subs[0];
  }
  document.querySelectorAll("nav button").forEach(x =>
    x.classList.toggle("active", x.dataset.tab === main));
  // 手机上导航是横向滚动的，7 个标签放不下；切到靠右的视图（系统/计划任务/日志/设置）时
  // 把激活项滚进视野，否则用户看到的高亮可能是屏幕外的那个。桌面端侧栏是 sticky 的，不该跟着滚。
  if(window.innerWidth < 900){
    const navBtn = document.querySelector('nav button[data-tab="' + main + '"]');
    if(navBtn) try{ navBtn.scrollIntoView({block:"nearest", inline:"nearest"}); }catch(e){}
  }
  for(const id of TABS) document.getElementById("tab-" + id).classList.toggle("hidden", id !== main);
  // 真要展示 Harness 了，撤掉预热态（那层是 opacity:0 的，留着反而看不见）
  const hs = document.getElementById("sub-harness");
  if(hs) hs.classList.remove("dsh-preload");
  // 三页各有自己的子导航，标记激活项时要按 data-sub 找，不能只认某一个容器
  document.querySelectorAll(".subnav button").forEach(x =>
    x.classList.toggle("active", x.dataset.sub === curSub[main]));
  // 全局的 subview 容器：只显示「属于当前页、且是当前子视图」的那一个
  document.querySelectorAll(".subview").forEach(x => {
    const id = x.id.replace(/^sub-/, "");
    x.classList.toggle("hidden", !(SUB_OWNER[id] === main && id === curSub[main]));
  });
  const sub2 = subs ? curSub[main] : null;
  setCrumb(viewLabel(main, null), sub2 ? viewLabel(main, sub2) : "");
  loadTab(sub2 || main);
}
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  const m = b.dataset.tab;
  showView(m, SUBS[m] ? curSub[m] : null, true);
});
document.querySelectorAll(".subnav button").forEach(b => b.onclick = () => {
  // 按钮属于哪一页：从它所在的 section 反推，这样三页共用一个绑定逻辑
  const sec = b.closest("section");
  const m = sec && sec.id.indexOf("tab-") === 0 ? sec.id.slice(4) : "runtime";
  showView(m, b.dataset.sub, true);
});
function refreshNow(btn){
  if(btn) btn.classList.add("flash");
  setTimeout(() => btn && btn.classList.remove("flash"), 800);
  loadTab(currentView());
}

/* 卡片状态按各自间隔自动刷新：每 1 秒一个 tick，仅刷新「当前可见」的区块。
   日志页不自动刷新（避免打断正在查看的内容）；页面切到后台时暂停，返回后立即补一次。
   刷新时保留各滚动条位置，避免列表跳回顶部。 */
function currentView(){
  const subs = SUBS[curTab];
  return subs ? (curSub[curTab] || SUB_DEFAULT[curTab] || subs[0]) : curTab;
}
function pollRefresh(key){
  const boxes = [...document.querySelectorAll(".scroll, .logbox")];
  const tops = boxes.map(b => b.scrollTop);
  loadTab(key, true);                 // 轮询采用惰性模式：折叠状态的分块不拉取
  requestAnimationFrame(() => boxes.forEach((b, i) => { b.scrollTop = tops[i]; }));
}
function startPoll(){
  if(pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if(document.hidden) return;
    const key = currentView(), ms = REFRESH[key];
    if(!ms) return;
    const now = Date.now();
    if(now - (lastPoll[key] || 0) < ms) return;
    lastPoll[key] = now;
    pollRefresh(key);
  }, 1000);
}
document.addEventListener("visibilitychange", () => { if(!document.hidden) lastPoll = {}; });

/* --- 入口 ------------------------------------------------------------- */
applyTheme(localStorage.getItem(THEME_KEY) || "light");
async function boot(){
  try{
    META = await api("/api/meta");
    document.getElementById("modeLine").textContent =
      "v" + META.version + (META.real ? "" : " · 演示");
    if(!ME){                       // 带令牌问一次 /api/auth 就知道自己是谁；还拿不到就问 /api/accounts
      try{ ME = (await api("/api/accounts")).me || ""; }catch(e){ /* 忽略 */ }
    }
    const pill = document.getElementById("hUser");
    if(pill) pill.textContent = ME || "—";        // 侧栏外层已经有图标了，别再来一个
    // 打开页面就把每个视图（含折叠块）预热一遍，切过去就是现成的；单个失败不影响其它
    await Promise.allSettled([loadOverview(), loadServices(), loadDocker(), loadImages(),
                              loadNet(), loadProcesses(), loadSecurity(), loadAccounts(),
                              loadActions(), loadAudit(), loadLogs2()]);
    // 预热这几秒内用户可能已打开其他页面 —— 此时应保留用户的选择。
    // 这一行以前是无条件的，于是「刷新后马上点别的栏」会被拽回主页。
    if(!viewPinned) showView("overview");
    startPoll();
    // 面板自己先热一遍 Harness：它首次要拉一批前端插件（实测十几秒），
    // 在空闲时于后台加载完成，用户打开时即可直接使用。不打开也没有额外成本 —— 仅一次请求 + 一次加载。
    setTimeout(() => { try{ preloadDsh(); }catch(e){} }, 4000);
  }catch(e){ if(e.message !== "unauthorized") toast("加载失败：" + esc(e.message), true); }
}
(async () => {
  try{
    // 必须带令牌问：不带的话服务端不知道你是谁，me 就是空的（顶栏会显示成问号）
    AUTH = await fetch("/api/auth", TOKEN ? {headers: {"Authorization": "Bearer " + TOKEN}} : undefined)
      .then(r => r.json());
  }catch(e){ AUTH = {}; }
  ME = AUTH.me || "";
  const minEl = document.getElementById("pwMin");
  if(minEl) minEl.textContent = AUTH.password_min || 8;
  if(AUTH.need_setup){ showLogin(true); return; }      // 第一次进来：先设账户
  if(TOKEN) boot(); else showLogin(false);
})();
