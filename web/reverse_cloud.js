/**
 * ReverseLayoutCaption 前端 — 完整接管节点 UI。
 *
 * 节点本体只保留 image / text 两个端口；全部参数由内嵌面板承载。
 * 原生 widget 从 node.widgets 摘除（视觉完全接管），值仍通过
 * serialize 钩子按原顺序写回 widgets_values —— 工作流保存 / 队列执行不受影响。
 *
 * 面板结构（分区卡片）：
 *   顶栏   provider 分段 + 状态灯
 *   本地页 模型 / 量化 / rule / 生成长度 / batch_index / 清场开关 / 标签模式
 *   云端页 账号卡（绑定 / 测试）
 *   端点页 地址 / 密钥 / 模型 / 思考开关
 */
import { app } from "../../scripts/app.js";

const NODE_CLASS = "ReverseLayoutCaption (AssembledRecipes)";
const PROP = "arReverseCloud";
const LS_ACCOUNT = "arReverseDeepseekAccount";   // 只记上次输过哪个邮箱（不是秘密）
const DS_API = "/assembled-recipes/ds"; // 内置网关的控制面（同源，走 ComfyUI 自已在的端口）

const STYLE_ID = "ar-reverse-cloud-css";

// 旧版把 DeepSeek 密码与 device_id 缓在浏览器 localStorage 里；真值本来就在本机
// ds_gateway.json，这份缓存没意义而且多一份可读文件 → 见到就抹掉（一次性）。
const LS_STALE = ["arReverseDeepseekPassword", "arReverseDeepseekDeviceId", "arReverseOpenaiModels"];
function purgeStaleLocalStorage() {
	for (const k of LS_STALE) {
		try {
			if (localStorage.getItem(k) !== null) localStorage.removeItem(k);
		} catch (e) { /* 隐私模式等拿不到 localStorage，忽略 */ }
	}
	try {
		for (let i = localStorage.length - 1; i >= 0; i--) {
			const k = localStorage.key(i);
			if (k && k.startsWith("arReverseOpenaiModels:")) localStorage.removeItem(k);
		}
	} catch (e) { /* 同上 */ }
}
const CSS = `
/* ===== 面板壳（照抄画廊节点的防回环配方）=====
 * .dom-widget 容器高度由 LiteGraph 从 widget 测量回写；壳必须与内容尺寸解耦：
 *   min-height:0 + grid minmax(0,1fr) 让滚动区可以小于内容高
 *   content-visibility + contain-intrinsic-size 切断「内容高 → 容器高」的测量反馈 */
.dom-widget:has(> .arshell) { pointer-events: none !important; }

/* ===== 设计变量（极简：一根主色 + 三级灰度 + 1px 发丝线，不用卡片盒）===== */
.arshell {
  --ar-fg: var(--fg-color, #dcdcde);
  --ar-dim: rgba(220, 220, 226, .58);
  --ar-faint: rgba(220, 220, 226, .38);
  --ar-line: rgba(255, 255, 255, .10);
  --ar-line-soft: rgba(255, 255, 255, .055);
  --ar-accent: #6f9bff;
  --ar-ok: #58c176;
  --ar-warn: #dcb657;
  --ar-bad: #e86b6b;
  width: 100%; height: 100%; min-width: 0; min-height: 0;
  display: grid; grid-template-rows: minmax(0, 1fr); overflow: hidden;
  box-sizing: border-box; pointer-events: auto;
  font: 12px/1.55 -apple-system, "Segoe UI", system-ui, "Microsoft YaHei", sans-serif;
  color: var(--ar-fg); background: var(--comfy-input-bg, #2b2b30);
  content-visibility: auto; contain-intrinsic-size: 430px 560px;
  -webkit-font-smoothing: antialiased; }
.arbody { min-height: 0; overflow-y: auto; overflow-x: hidden;
  padding: 14px 16px 16px; display: flex; flex-direction: column; gap: 14px; }
.arbody::-webkit-scrollbar { width: 6px; }
.arbody::-webkit-scrollbar-thumb { background: rgba(255,255,255,.12); border-radius: 3px; }
.arbody::-webkit-scrollbar-track { background: transparent; }
.arshell.is-resizing, .arshell.is-resizing * { pointer-events: none !important; user-select: none !important; }

/* 顶栏：只留一个标题 + 一个状态点 */
.ar-head { display: flex; align-items: center; gap: 8px; }
.ar-head .t { font-weight: 600; font-size: 12px; letter-spacing: .04em; }
.ar-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ar-faint); flex: none; }
.ar-dot.ok { background: var(--ar-ok); }
.ar-dot.err { background: var(--ar-bad); }
.ar-dot.busy { background: var(--ar-warn); animation: arPulse 1.1s ease-in-out infinite; }
@keyframes arPulse { 50% { opacity: .3; } }
.ar-head .sp { flex: 1; }

/* 顶部导航：主界面（文字页签）+ 模式下拉，下面一条发丝线 */
.ar-seg { display: flex; align-items: center; gap: 14px;
  border-bottom: 1px solid var(--ar-line); padding-bottom: 9px; }
.ar-seg button { position: relative; flex: none; padding: 2px 0 8px; border: none;
  background: transparent; color: var(--ar-faint); font: 500 12px/1.2 inherit;
  cursor: pointer; transition: color .14s; white-space: nowrap; }
.ar-seg button.on { color: var(--ar-fg); font-weight: 600; }
.ar-seg button:hover { color: var(--ar-fg); }
/* 选中页签在发丝线上压一小段主色 */
.ar-seg button.on::after { content: ""; position: absolute; left: 0; right: 0; bottom: -10px;
  height: 2px; background: var(--ar-accent); border-radius: 1px; }

.ar-modesel { flex: 1; display: flex; align-items: center; justify-content: flex-end; gap: 7px; }
.ar-modesel .lbl { font-size: 10.5px; color: var(--ar-faint); letter-spacing: .04em; white-space: nowrap; }
.ar-modesel select { background: transparent; border: none; color: var(--ar-fg);
  font: 600 12px inherit; padding: 2px 15px 2px 4px; cursor: pointer; outline: none;
  appearance: none; -webkit-appearance: none; text-align: right; text-align-last: right; max-width: 62%;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='9' height='6'><path d='M1 1l3.5 3.5L8 1' stroke='%238a8a92' fill='none' stroke-width='1.3' stroke-linecap='round'/></svg>");
  background-repeat: no-repeat; background-position: right 2px center; }
.ar-modesel select option { background: #2b2b33; color: #e6e6ee; font-weight: 400; }

/* 手风琴（规则） */
.ar-details { margin-top: 2px; }
.ar-details summary { font-size: 10.5px; opacity: .45; cursor: pointer; letter-spacing: .04em; }
.ar-details summary:hover { opacity: .75; }
.ar-details .ar-f { margin-top: 8px; }

/* 分区：不再用盒子，靠小标题 + 发丝线分隔 */
details.ar-card>summary { list-style: none; }
details.ar-card>summary::-webkit-details-marker { display: none; }
details.ar-card[open]>summary { margin-bottom: 8px; }

.ar-card { display: flex; flex-direction: column; gap: 11px; }
.ar-card + .ar-card { border-top: 1px solid var(--ar-line-soft); padding-top: 13px; }
.ar-card>.cap { font-size: 10px; font-weight: 600; letter-spacing: .1em;
  text-transform: uppercase; color: var(--ar-faint); }

/* 表单 */
.ar-row { display: flex; gap: 12px; }
.ar-row>* { flex: 1; min-width: 0; }
.ar-f { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.ar-f>label { font-size: 10.5px; color: var(--ar-dim); letter-spacing: .02em; }
.ar-f input, .ar-f select, .ar-f textarea {
  background: transparent; border: none; border-bottom: 1px solid var(--ar-line);
  border-radius: 0; padding: 5px 1px; color: var(--ar-fg);
  font: 12px inherit; width: 100%; box-sizing: border-box; transition: border-color .14s; }
.ar-f input::placeholder, .ar-f textarea::placeholder { color: rgba(220,220,226,.26); }
.ar-f textarea { resize: vertical; min-height: 58px; line-height: 1.5; }
.ar-f input:hover, .ar-f select:hover, .ar-f textarea:hover { border-color: rgba(255,255,255,.22); }
.ar-f input:focus, .ar-f select:focus, .ar-f textarea:focus {
  outline: none; border-color: var(--ar-accent); }
.ar-f select { cursor: pointer; appearance: none; -webkit-appearance: none; padding-right: 17px;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='9' height='6'><path d='M1 1l3.5 3.5L8 1' stroke='%238a8a92' fill='none' stroke-width='1.3' stroke-linecap='round'/></svg>");
  background-repeat: no-repeat; background-position: right 1px center; }
.ar-f input[type=number]::-webkit-inner-spin-button { opacity: .3; }

/* 开关：细线条版 */
.ar-sw { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.ar-sw .lab { font-size: 11.5px; color: var(--ar-dim); }
.ar-tg { width: 30px; height: 16px; border-radius: 8px; background: rgba(255,255,255,.13);
  position: relative; cursor: pointer; flex: none; transition: background .16s; }
.ar-tg.on { background: var(--ar-accent); }
.ar-tg::after { content: ""; position: absolute; top: 3px; left: 3px; width: 10px; height: 10px;
  border-radius: 50%; background: #eceef3; transition: left .16s; }
.ar-tg.on::after { left: 17px; }

/* 按钮：默认就是文字按钮，主按钮一层淡底 */
.ar-btn { padding: 6px 14px; border: 1px solid var(--ar-line); border-radius: 6px;
  background: transparent; color: var(--ar-dim);
  font: 500 11.5px inherit; cursor: pointer; white-space: nowrap; transition: color .14s, border-color .14s; }
.ar-btn:hover { color: var(--ar-fg); border-color: rgba(255,255,255,.28); }
.ar-btn:disabled { opacity: .35; cursor: default; }
.ar-btn.pri { background: rgba(111,155,255,.14); border-color: rgba(111,155,255,.42);
  color: var(--ar-accent); font-weight: 600; }
.ar-btn.pri:hover { background: rgba(111,155,255,.22); color: #9dbaff; }

/* 辅助文字 / 日志 / 状态 */
.ar-hint { font-size: 10.5px; line-height: 1.5; color: var(--ar-faint); word-break: break-all; }
.ar-logs { font-size: 10px; line-height: 1.5; margin: 2px 0 0; padding: 7px 9px; max-height: 88px;
  overflow: auto; background: rgba(0,0,0,.22); border-radius: 5px; color: var(--ar-dim);
  white-space: pre-wrap; word-break: break-all; font-family: "Cascadia Mono", Consolas, Menlo, monospace; }
.ar-status { font-size: 11px; min-height: 15px; word-break: break-all; color: var(--ar-dim); }
.ar-status.ok { color: var(--ar-ok); }
.ar-status.err { color: var(--ar-bad); }

/* 账号池：行式列表（无盒），靠发丝线分组 */
.ar-accts { display: flex; flex-direction: column; gap: 0; margin: 0 0 2px; }
.ar-acct { display: grid; grid-template-columns: auto 1fr auto auto; gap: 3px 8px; align-items: center;
  padding: 7px 0; border-bottom: 1px solid var(--ar-line-soft); font-size: 11.5px; }
.ar-acct:last-of-type { border-bottom: none; }
.ar-badge { font-size: 10px; letter-spacing: .02em; white-space: nowrap; color: var(--ar-faint);
  background: transparent; padding: 0; display: inline-flex; align-items: center; gap: 4px; }
.ar-badge::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.ar-badge.ok { color: var(--ar-ok); }
.ar-badge.err { color: var(--ar-bad); }
.ar-badge.busy { color: var(--ar-warn); }
.ar-mail { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ar-fg); }
.ar-meta { grid-column: 1 / -1; font-size: 10px; color: var(--ar-faint); margin: -1px 0 0 13px; }
.ar-acct .ar-mini { font-size: 10.5px; padding: 2px 0; border: none; border-radius: 0;
  background: transparent; color: var(--ar-faint); cursor: pointer; text-decoration: none; }
.ar-acct .ar-mini:hover { color: var(--ar-fg); filter: none; }
.ar-acct .ar-mini.danger:hover { color: var(--ar-bad); }

.ar-page { display: flex; flex-direction: column; gap: 14px; }
.ar-page.hidden { display: none; }
`;

function injectStyle() {
	if (document.getElementById(STYLE_ID)) return;
	const s = document.createElement("style");
	s.id = STYLE_ID;
	s.textContent = CSS;
	document.head.appendChild(s);
}

function el(tag, cls, text) {
	const e = document.createElement(tag);
	if (cls) e.className = cls;
	if (text != null) e.textContent = text;
	return e;
}

function stateFor(node) {
	node.properties ||= {};
	node.properties[PROP] ||= { provider: "", tab: "main", lastResult: null, account: "" };
	const st = node.properties[PROP];
	// provider 的唯一真源是 widget：面板里那份只是“上次看的是哪页”。
	// 必须每次都用 widget 覆盖，否则 node.properties 被 loadGraphData 换掉后，
	// 面板缓存的旧对象会和工作流里的模式各说各话（灯、页面都跟着错的走）。
	const fromWidget = providerKeyOf(node);
	if (fromWidget) st.provider = fromWidget;
	else if (!st.provider) st.provider = "local";
	return st;
}

const PROVIDER_LABEL_TO_KEY = {
	"本地模型（transformers）": "local",
	"DeepSeek 云端（内置网关）": "ds",
	"自定义 OpenAI 兼容端点": "custom",
};

// 从 widget 反推模式 key：不能拿面板默认值去盖掉工作流里已存的 provider
function providerKeyOf(node) {
	const w = widgetOf(node, "provider");
	if (!w) return "";
	const key = PROVIDER_LABEL_TO_KEY[String(w.value)];
	if (key) return key;
	const text = String(w.value || "");
	if (text.startsWith("本地")) return "local";
	if (text.includes("DeepSeek")) return "ds";
	if (text.includes("OpenAI")) return "custom";
	return "";
}

function saveState(node) {
	app.graph?.change?.();
}

async function jsonFetch(url, opts = {}) {
	const r = await fetch(url, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
	const text = await r.text();
	let data = null;
	try { data = JSON.parse(text); } catch { /* ignore */ }
	if (!r.ok) throw new Error((data && (data.error?.message || data.detail || data.error)) || `HTTP ${r.status}`);
	return data;
}

// ================= 原生 widget 隐藏（前端官方可见性机制）=================
//
// 不摘 widget，只让它「不参与绘制与布局」：
//   isWidgetRowVisible → occupiesCanvasRow(visibility) → false（不画、computeSize 不计行高）
//   getLayoutWidgets → 排除（arrange 不再按行数把节点撞高，疯涨根因）
// widget 仍在 node.widgets 里 → 保存工作流 / graphToPrompt 走原生链路，零钩子零风险。
// 显式写 visibility 对象（前端优先读它，后续把 hidden 改回 false 也盖不掉）。

const PANEL_NAME = "arcloud_panel";

function hideWidget(w) {
	if (!w || w.name === PANEL_NAME) return;
	// visibility 是只读派生属性（由 hidden/options.hidden 推导），只能写 hidden：
	// deriveWidgetVisibility → suppression.byExtension = true → 不绘制、不参与布局与 computeSize
	w.hidden = true;
	(w.options ||= {}).hidden = true;
}

function hideNatives(node) {
	for (const w of node.widgets || []) hideWidget(w);
}

// loadGraphData 后前端会重建 widget：在创建处一出场就隐藏（不靠定时清扫）
function installHideGuard(nodeType) {
	const prev = nodeType.prototype.addWidget;
	if (prev?.__arHideGuarded) return;
	function addWidget(type, name, value, options) {
		const w = prev.apply(this, arguments);
		hideWidget(w);
		return w;
	}
	addWidget.__arHideGuarded = true;
	nodeType.prototype.addWidget = addWidget;
}

// ================= 面板控件（值写回原生 widget 对象） =================

function widgetOf(node, name) {
	return (node.widgets || []).find((w) => w.name === name);
}

function ctlText(ui, name, labelText, { multiline = false, placeholder = "" } = {}) {
	const node = ui.node;
	const w = widgetOf(node, name);
	const c = el("div", "ar-f");
	c.append(el("label", "", labelText));
	let input;
	if (multiline) {
		input = el("textarea");
		input.rows = 4;
	} else {
		input = el("input");
		input.type = "text";
	}
	input.placeholder = placeholder;
	input.value = w?.value ?? "";
	input.addEventListener("change", () => {
		if (w) { w.value = input.value; w.callback?.(input.value); saveState(node); }
		syncBinds(ui, name, input);
	});
	bind(ui, name, input, () => w?.value ?? "");
	c.append(input);
	return c;
}

function ctlNum(ui, name, labelText, { min = 0, max = 999999 } = {}) {
	const node = ui.node;
	const w = widgetOf(node, name);
	const c = el("div", "ar-f");
	c.append(el("label", "", labelText));
	const input = el("input");
	input.type = "number";
	input.min = String(min);
	input.max = String(max);
	input.value = w?.value ?? 0;
	input.addEventListener("change", () => {
		let v = Number(input.value) || 0;
		v = Math.min(max, Math.max(min, v));
		input.value = String(v);
		if (w) { w.value = v; w.callback?.(v); saveState(node); }
		syncBinds(ui, name, input);
	});
	bind(ui, name, input, () => w?.value ?? 0);
	c.append(input);
	return c;
}

function ctlSelect(ui, name, labelText) {
	const node = ui.node;
	const w = widgetOf(node, name);
	const c = el("div", "ar-f");
	c.append(el("label", "", labelText));
	const sel = el("select");
	for (const opt of w?.options?.values || []) {
		const o = el("option", "", opt);
		o.value = opt;
		sel.append(o);
	}
	sel.value = w?.value ?? "";
	sel.addEventListener("change", () => {
		if (w) { w.value = sel.value; w.callback?.(sel.value); saveState(node); }
		syncBinds(ui, name, sel);   // 同一个参数可能在两页都露（比如思考强度）
	});
	bind(ui, name, sel, () => w?.value ?? "");
	c.append(sel);
	return c;
}

// 同一个 widget 的多个控件互相同步（谁改了就刷其他几个）
const bindKey = (name, el2) => name + "::" + el2.dataset.arb;

function bind(ui, name, elem, read) {
	ui.binds ||= {};
	elem.dataset.arb = String((ui.bindSeq = (ui.bindSeq || 0) + 1));
	ui.binds[bindKey(name, elem)] = { name, elem, read };
}

function syncBinds(ui, name, from) {
	for (const key of Object.keys(ui.binds || {})) {
		const b = ui.binds[key];
		if (!b || b.name !== name || b.elem === from) continue;
		const v = b.read();
		if (b.elem.tagName === "SELECT") b.elem.value = v;
		else if (b.elem.tagName === "INPUT" || b.elem.tagName === "TEXTAREA") b.elem.value = v;
		else if (b.elem.classList) b.elem.classList.toggle("on", !!v);
	}
}

function ctlToggle(ui, name, labelText) {
	const node = ui.node;
	const w = widgetOf(node, name);
	const row = el("div", "ar-sw");
	row.append(el("span", "lab", labelText));
	const t = el("div", "ar-tg" + (w?.value ? " on" : ""));
	t.addEventListener("click", () => {
		const v = !t.classList.contains("on");
		t.classList.toggle("on", v);
		if (w) { w.value = v; w.callback?.(v); saveState(node); }
		syncBinds(ui, name, t);
	});
	bind(ui, name, t, () => !!w?.value);
	row.append(t);
	return row;
}

// ================= 账号池（多账号轮巡）=================

function setStatus(ui, cls, text) {
	ui.status.textContent = text;
	ui.status.className = "ar-status" + (cls ? " " + cls : "");
}

const STATE_TEXT = {
	ready: ["可用", "ok"],
	muted: ["禁言", "err"],
	cooling: ["限流冷却", "busy"],
	no_device_id: ["缺 device_id", "err"],
};

// 账号列表：每行一个，绿=可用、红=禁言（带解禁时间）、黄=限流冷却
function renderAccounts(ui, st) {
	if (!ui.acctList) return;
	const rows = (st && st.accounts) || [];
	ui.acctList.textContent = "";
	if (!rows.length) {
		ui.acctList.append(el("div", "ar-hint", "账号池是空的——填邮箱+密码点「添加并试跑」。"));
		return;
	}
	rows.forEach((a) => {
		const row = el("div", "ar-acct");
		const [txt, cls] = STATE_TEXT[a.state] || [a.state, ""];
		const dot = el("span", "ar-badge " + cls, txt);
		if (a.state === "muted" && a.muted_until_text) dot.title = "解禁时间 " + a.muted_until_text;
		if (a.state === "cooling" && a.cooldown_until) dot.title = "冷却到 " + fmtWhen(a.cooldown_until);
		const mail = el("span", "ar-mail", a.email);
		const meta = el("span", "ar-meta",
			(a.served ? "跑过" + a.served + "次" : "还没跑过") +
			(a.fail_streak ? " · 连败" + a.fail_streak : "") +
			(!a.has_device_id ? " · 无 device_id" : ""));
		if (a.state === "muted") meta.textContent += " · 到 " + a.muted_until_text;
		const tBtn = el("button", "ar-mini", "试跑");
		tBtn.addEventListener("click", () => testAccount(ui, a.email));
		const xBtn = el("button", "ar-mini danger", "移除");
		xBtn.addEventListener("click", () => removeAccount(ui, a.email));
		row.append(dot, mail, tBtn, xBtn, meta);
		ui.acctList.append(row);
	});
	const line = el("div", "ar-hint");
	const total = rows.length;
	if (st.ready > 0) {
		line.textContent = `${st.ready}/${total} 可用` + (st.last_account ? ` · 上次 ${st.last_account}` : "");
	} else if (total === 1) {
		line.textContent = "不可用" + (st.recover_text ? ` · ${st.recover_text} 恢复` : "");
	} else {
		line.textContent = `池内 ${total} 个全部不可用` + (st.recover_text ? `，最早 ${st.recover_text} 恢复` : "");
	}
	ui.acctList.append(line);
}

function fmtWhen(ts) {
	const d = new Date(ts * 1000);
	return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

async function removeAccount(ui, email) {
	setStatus(ui, "", "正在从账号池移除 " + email + "…");
	try {
		const data = await jsonFetch(`${DS_API}/unbind`, { method: "POST", body: JSON.stringify({ email }) });
		setStatus(ui, data.ok ? "ok" : "err", data.ok ? `已移除 ${email}` : ("移除失败：" + (data.error || "未知错")));
		refreshGatewayInfo(ui);
	} catch (e) {
		setStatus(ui, "err", "移除失败：" + (e.message || e));
	}
}

async function bindAccount(ui) {
	const account = ui.accountInput.value.trim();
	const password = ui.passwordInput.value;
	const deviceId = (ui.deviceIdInput?.value || "").trim();
	if (!account || !password) {
		setStatus(ui, "err", "邮箱和密码都要填");
		return;
	}
	ui.bindBtn.disabled = true;
	ui.dot.className = "ar-dot busy";
	setStatus(ui, "", "正在绑定账号到内置网关…");
	try {
	const data = await jsonFetch(`${DS_API}/bind`, {
			method: "POST",
			body: JSON.stringify({ email: account, password, device_id: deviceId }),
		});
		localStorage.setItem(LS_ACCOUNT, account);
		stateFor(ui.node).account = account;
		saveState(ui.node);
		if (data.ok) {
			setStatus(ui, "ok", `已加入账号池并试跑成功，登录+一枪 ${data.ms}ms（回：${data.echo || "ok"}）`);
		} else {
			setStatus(ui, "err", "这个账号不通：" + (data.error || "未知错") + "（已存进池子，可随时再试跑）");
		}
		refreshModeDot(ui);
	} catch (e) {
		setStatus(ui, "err", "绑定失败：" + (e.message || e));
		refreshModeDot(ui);
	} finally {
		ui.bindBtn.disabled = false;
	}
}

async function testAccount(ui, email) {
	ui.testBtn.disabled = true;
	ui.dot.className = "ar-dot busy";
	setStatus(ui, "", email ? `正在试跑 ${email}…` : "内置网关登录 + 跑一枪极小补全（用第一个可用账号）…");
	try {
		const t0 = performance.now();
		const data = await jsonFetch(`${DS_API}/test`, { method: "POST", body: JSON.stringify({ email: email || "" }) });
		const ms = Math.round(performance.now() - t0);
		if (data.ok) {
			setStatus(ui, "ok", `连通正常（${data.email || ""} · ${data.ms}ms，${data.model}）`);
		} else {
			setStatus(ui, "err", "不通：" + (data.error || "未知错"));
		}
		return data.ok;
	} catch (e) {
		setStatus(ui, "err", "测试失败：" + (e.message || e));
		return false;
	} finally {
		ui.testBtn.disabled = false;
		refreshModeDot(ui);
	}
}

// 状态灯：只跟**当前模式**判定“连上没连上”，而且是唯一写者
//   本地模型 → 选了模型就是绿的（不需要网络）
//   DeepSeek 云端 → 网关在跑且池子里有可用账号才绿
//   自定义端点 → 本机存过拉到的模型列表 = 这个端点通过过
// 上一模式的异步结果不能盖掉新模式的灯（dotSeq 卡一下）。
async function refreshModeDot(ui) {
	const seq = (ui.dotSeq = (ui.dotSeq || 0) + 1);
	const paint = (cls) => {
		if (ui.dotSeq === seq && ui.dot) ui.dot.className = "ar-dot " + cls;
	};
	const st = stateFor(ui.node);
	if (st.provider === "local") {
		const w = widgetOf(ui.node, "model_name");
		paint(w && w.value ? "ok" : "err");
		return;
	}
	if (st.provider === "custom") {
		const base = (ui.epBase?.value || EP.base || "").trim();
		if (!base) return paint("err");
		paint((EP.models || []).length ? "ok" : "");
		return;
	}
	let gw = null;
	try {
		gw = await refreshGatewayInfo(ui);
	} catch (e) { /* 读不到就灰着，不拿旧值骗人 */ }
	if (ui.dotSeq !== seq) return;
	paint(gw && gw.running && gw.ready > 0 ? "ok" : gw ? "err" : "");
}

// 网关服务面：状态行 + 启停按钮（服务是内置的，不需要外部 ds2api）
async function refreshGatewayInfo(ui) {
	let st = null;
	try {
		st = await jsonFetch(`${DS_API}/status`);
	} catch (e) {
		if (ui.svcLine) ui.svcLine.textContent = "内置网关状态读不到：" + (e.message || e);
		return null;
	}
	if (ui.svcLine) {
		ui.svcLine.textContent = st.running
			? `服务运行中 ${st.base} · 代理 ${st.proxy || "无"} · 本会话 ${st.requests} 次（最近 ${st.last_ms}ms）`
			: `服务未运行${st.error ? "（" + st.error + "）" : ""}`;
	}
	if (ui.svcBtn) ui.svcBtn.textContent = st.running ? "停掉服务" : "拉起服务";
	// 灯不在这里画（多个异步写者会互相盖），统一由 refreshModeDot 管
	if (ui.accountInput && !ui.accountInput.value && st.email) ui.accountInput.value = st.email;
	if (ui.deviceIdInput && !ui.deviceIdInput.value && st.has_device_id) ui.deviceIdInput.placeholder = "已绑过 device_id（留空则沿用）";
	if (ui.svcLogs) ui.svcLogs.textContent = (st.logs || []).join("\n");
	renderAccounts(ui, st);
	return st;
}

async function toggleService(ui) {
	let st = null;
	try { st = await jsonFetch(`${DS_API}/status`); } catch (e) { /* 当作未运行 */ }
	const action = st?.running ? "stop" : "start";
	ui.svcBtn.disabled = true;
	setStatus(ui, "", action === "start" ? "正在拉起内置网关…" : "正在停掉内置网关…");
	try {
		const data = await jsonFetch(`${DS_API}/service`, { method: "POST", body: JSON.stringify({ action }) });
		setStatus(ui, data.ok ? "ok" : "err", data.ok
			? (action === "start" ? `服务已拉起（${data.port}）` : "服务已停")
			: ("失败：" + (data.error || "未知错")));
	} catch (e) {
		setStatus(ui, "err", "操作失败：" + (e.message || e));
	} finally {
		ui.svcBtn.disabled = false;
		refreshGatewayInfo(ui);
	}
}

// ================= 自定义端点（地址/密钥/模型名只存本机）=================
// 为什么这么设：这三项当 widget 时，会跟着工作流存盘、被写进每张出图 PNG 的 workflow
// 元数据（发图=发 key）、还进 /history。现在本机一个文件里存真值，工作流只带一个名字。
// 界面上就一个端点：地址 / 密钥 / 模型下拉——没有列表、没有增删、没有多路轮换。
const EP_NAME = "default";
let EP_NAME_ROW = EP_NAME;   // 本机文件里那一条的真实名字（旧数据可能不叫 default）
let EP = { base: "", model: "", has_key: false, models: [] };

async function loadEndpoints(ui) {
	try {
		const data = await jsonFetch(`${DS_API}/endpoints`, { method: "POST", body: JSON.stringify({ action: "list" }) });
		if (data.ok) {
			const row = (data.endpoints || []).find((e) => e.name === EP_NAME) ||
				(data.endpoints || [])[0] || { name: EP_NAME, base: "", model: "", models: [] };
			EP = { base: row.base || "", model: row.model || "", has_key: !!row.has_key, models: row.models || [] };
			EP_NAME_ROW = row.name || EP_NAME;
			// 名字进 widget（工作流里只有它），地址/密钥/模型名不进
			const w = widgetOf(ui.node, "endpoint");
			if (w) {
				if (!w.options.values.includes(EP_NAME_ROW)) w.options.values = [EP_NAME_ROW];
				if (w.value !== EP_NAME_ROW) { w.value = EP_NAME_ROW; w.callback?.(EP_NAME_ROW); }
			}
			if (ui.epBase && !ui.epBase.value) ui.epBase.value = EP.base;
			if (ui.epKey) ui.epKey.placeholder = EP.has_key ? "已存 · 留空不动" : "sk-…";
			renderModelSelect(ui);
		}
	} catch (e) {
		if (ui.epBase) ui.epBase.placeholder = "读不到本机端点配置：" + (e.message || e);
	}
}

// 改地址/密钥就整体存一次（密钥留空 = 不动已存的那把）
async function saveCustomEndpoint(ui) {
	const body = { action: "save", name: EP_NAME_ROW, base: ui.epBase.value.trim(), model: EP.model };
	const key = ui.epKey.value.trim();
	if (key) body.key = key;
	try {
		const data = await jsonFetch(`${DS_API}/endpoints`, { method: "POST", body: JSON.stringify(body) });
		if (data.ok) {
			if (key) ui.epKey.value = "";
			await loadEndpoints(ui);
			setStatus(ui, "ok", "已存到本机");
			refreshModeDot(ui);
		} else setStatus(ui, "err", "保存失败：" + (data.error || "未知错"));
	} catch (e) {
		setStatus(ui, "err", "保存失败：" + (e.message || e));
	}
}

// 模型下拉：选项来自本机缓存的那份（拉过一次就有）
function renderModelSelect(ui) {
	const sel = ui.modelSel;
	if (!sel) return;
	const names = EP.models || [];
	const cur = EP.model || "";
	const opts = cur && !names.includes(cur) ? [cur, ...names] : names;
	sel.textContent = "";
	if (!opts.length) {
		sel.append(el("option", "", "（点拉取模型列表）"));
		sel.disabled = true;
	} else {
		sel.disabled = false;
		for (const nm of opts) sel.append(el("option", "", nm));
	}
	sel.value = cur || (opts[0] || "");
}

async function setModelValue(ui, value) {
	EP.model = value;
	try {
		await jsonFetch(`${DS_API}/endpoints`, {
			method: "POST",
			body: JSON.stringify({ action: "save", name: EP_NAME_ROW, base: ui.epBase.value.trim() || EP.base, model: value }),
		});
		await loadEndpoints(ui);
		setStatus(ui, "ok", "已选 " + value);
		refreshModeDot(ui);
	} catch (e) {
		setStatus(ui, "err", "保存失败：" + (e.message || e));
	}
}

// 拉模型列表：只传端点名，密钥由后端从本机文件取（不进浏览器也不进工作流）
async function fetchModels(ui, quiet) {
	const name = EP_NAME_ROW;
	if (!ui.epBase.value.trim() && !EP.base) {
		setStatus(ui, "err", "先填请求地址");
		return;
	}
	ui.modelFetchBtn.disabled = true;
	if (!quiet) setStatus(ui, "", "正在拉模型列表…");
	try {
		const data = await jsonFetch(`${DS_API}/models`, { method: "POST", body: JSON.stringify({ endpoint: name }) });
		if (data.ok) {
			const models = data.models || [];
			EP.models = models;
			if (models.length && !models.includes(EP.model)) EP.model = models[0];
			if (models.length) {
				await jsonFetch(`${DS_API}/endpoints`, {
					method: "POST",
					body: JSON.stringify({ action: "save", name, base: ui.epBase.value.trim() || EP.base, model: EP.model }),
				});
			}
			await loadEndpoints(ui);
			setStatus(ui, "ok", quiet ? `连通正常（${data.ms}ms，${models.length} 个模型）`
				: `拿到 ${models.length} 个模型（${data.ms}ms）`);
			refreshModeDot(ui);
			return true;
		}
		setStatus(ui, "err", "不通：" + (data.error || "未知错"));
		EP.models = [];
		refreshModeDot(ui);
		return false;
	} catch (e) {
		setStatus(ui, "err", "不通：" + (e.message || e));
		EP.models = [];
		refreshModeDot(ui);
		return false;
	} finally {
		ui.modelFetchBtn.disabled = false;
	}
}

// ================= 结果可视化 =================

function apiUrl(item) {
	const p = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder || "", type: item.type || "input" });
	return "/view?" + p.toString();
}

// ================= 尺寸处理（照搬画廊节点已验证配方）=================

const MIN_SIZE = [430, 320]; // 面板最小尺寸（用户可拖大，不可拖小）
const DEFAULT_SIZE = [430, 560]; // 新建时的初始尺寸
let instanceSeq = 0;

function clampSize(size) {
	if (!size) return;
	size[0] = Math.max(MIN_SIZE[0], size[0]);
	size[1] = Math.max(MIN_SIZE[1], size[1]);
}

// 拖节点边框时让事件透传到画布（面板自己不能吃缩放手柄）
function installResizePassthrough(node, ...elements) {
	node._arResizeElements = [...new Set([...(node._arResizeElements || []), ...elements.filter(Boolean)])];
	if (node._arResizePatched) return;
	node._arResizePatched = true;

	const prevGetWidgetOnPos = node.getWidgetOnPos;
	node.getWidgetOnPos = function (x, y) {
		// 命中测试必须无副作用：共享指针按下时另一手势会查询本节点
		if (this.findResizeDirection?.(x, y)) return undefined;
		return prevGetWidgetOnPos?.apply(this, arguments);
	};

	const prevResize = node.onResize;
	node.onResize = function () {
		if (app.canvas?.resizing_node === this) beginResizePassthrough(this);
		return prevResize?.apply(this, arguments);
	};
}

function beginResizePassthrough(node) {
	if (node._arResizeCleanup) return;
	for (const el of node._arResizeElements || []) el?.classList?.add("is-resizing");
	const cleanup = () => {
		document.removeEventListener("pointerup", cleanup, true);
		document.removeEventListener("pointercancel", cleanup, true);
		for (const el of node._arResizeElements || []) el?.classList?.remove("is-resizing");
		node._arResizeCleanup = null;
	};
	node._arResizeCleanup = cleanup;
	document.addEventListener("pointerup", cleanup, true);
	document.addEventListener("pointercancel", cleanup, true);
}

// 尺寸接管：一手证据——前端 ComfyNode._arrangeWidgets → arrange 会按 widget 行数回写 setSize，
// 而 loadGraphData 后 restore 会绕过 addWidget 把原生 widget 重新 push 回 node.widgets（实测 29 个），
// 于是每次 arrange 都按旧行数把节点撞高（只增不减 = 疯涨）。
// 做法：arrange/_arrangeWidgets 前先清扫，执行完把尺寸强制回到「用户尺寸」。
function takeOverSize(node) {
	if (node._arSizeOwned) return;
	node._arSizeOwned = true;
	node._arDesiredSize = [Math.max(node.size[0], MIN_SIZE[0]), Math.max(node.size[1], MIN_SIZE[1])];
	// 用户拖动是唯一改尺寸的路径：记下来，任何内部重排都不能覆盖它
	const prevResize = node.onResize;
	node.onResize = function (size) {
		clampSize(size);
		node._arDesiredSize = [size[0], size[1]];
		prevResize?.apply(node, arguments);
	};
}

// ================= 面板 =================

const ui_map = new WeakMap();

function buildPanel(node) {
	// 每次都现取：node.properties 会被 loadGraphData 整个换掉，缓存对象就成孤儿了
	const st = () => stateFor(node);
	const root = el("div", "arshell");
	const body = el("div", "arbody");
	root.append(body);
	const ui = {
		node, root,
		resize: null, // 后面挂
	};
	ui_map.set(node, ui);

	// ---- 顶栏 ----
	const head = el("div", "ar-head");
	head.append(el("span", "t", "布局反推"));
	ui.dot = el("span", "ar-dot");
	head.append(ui.dot, el("span", "sp"));
	body.append(head);

	// ---- 顶部：两个控件（主界面 + 模式下拉），不再三个分段按钮 ----
	const MODES = [["local", "本地模型"], ["ds", "DeepSeek 云端"], ["custom", "自定义端点"]];
	const bar = el("div", "ar-seg");
	const mainBtn = el("button", st().tab === "mode" ? "" : "on", "主界面");
	mainBtn.title = "运行 / 画廊标签 / 提示词规则";
	bar.append(mainBtn);
	const modeWrap = el("div", "ar-modesel");
	modeWrap.append(el("span", "lbl", "模式"));
	const modeSel = el("select");
	for (const [key, label] of MODES) {
		const o = el("option", "", label);
		o.value = key;
		modeSel.append(o);
	}
	modeSel.value = st().provider;
	modeWrap.append(modeSel);
	bar.append(modeWrap);
	body.append(bar);

	// 状态行放在页面外面：不管当前看哪一页都看得到反馈
	ui.status = el("div", "ar-status");

	// ---- 主界面：运行 + 画廊标签 + 提示词规则 ----
	const pageMain = el("div", "ar-page");
	const cRun = el("div", "ar-card");
	cRun.append(el("div", "cap", "运行"));
	const gRun1 = el("div", "ar-row");
	gRun1.append(ctlNum(ui, "max_new_tokens", "最大生成长度", { min: 64, max: 65536 }),
		ctlNum(ui, "batch_index", "多图时取第几张", { min: 0, max: 64 }));
	cRun.append(gRun1);
	cRun.append(ctlToggle(ui, "pre_clear", "反推前清场（腾显存）"));
	cRun.append(ctlToggle(ui, "offload_after", "结束后卸载模型"));
	pageMain.append(cRun);

	const cTag = el("div", "ar-card");
	cTag.append(el("div", "cap", "画廊标签"));
	cTag.append(ctlSelect(ui, "tag_mode", "喂给模型多少标签"));
	pageMain.append(cTag);

	const c3 = el("details", "ar-card");
	const sum = el("summary", "", "提示词规则 rule");
	sum.style.cursor = "pointer";
	c3.append(sum);
	c3.append(ctlText(ui, "rule", "布局规则（框选 / 前后关系 / 输出格式）", { multiline: true }));
	pageMain.append(c3);
	body.append(pageMain);

	// ---- 本地模型页（只留模型相关）----
	const pageLocal = el("div", "ar-page");
	const c1 = el("div", "ar-card");
	c1.append(el("div", "cap", "本地模型"));
	const g1 = el("div", "ar-row");
	g1.append(ctlSelect(ui, "model_name", "模型（models/LLM）"), ctlSelect(ui, "quant", "量化"));
	c1.append(g1);
	pageLocal.append(c1);
	body.append(pageLocal);

	// ---- 云端页 ----
	const pageDs = el("div", "ar-page");
	const acc = el("div", "ar-card");
	acc.append(el("div", "cap", "DeepSeek 账号池"));
	ui.acctList = el("div", "ar-accts");
	acc.append(ui.acctList);
	const r1 = el("div", "ar-row");
	const fAcc = el("div", "ar-f");
	fAcc.append(el("label", "", "邮箱 / 手机号"));
	ui.accountInput = el("input");
	ui.accountInput.value = localStorage.getItem(LS_ACCOUNT) || st().account || "";
	fAcc.append(ui.accountInput);
	r1.append(fAcc);
	const fDev = el("div", "ar-f");
	fDev.append(el("label", "", "device_id（数美指纹，可留空沿用）"));
	ui.deviceIdInput = el("input");
	ui.deviceIdInput.placeholder = "BK…（真实浏览器抓的 89 字串）";
	ui.deviceIdInput.placeholder = "BK…（真实浏览器抓的 89 字串；已绑过可留空沿用）";
	fDev.append(ui.deviceIdInput);
	r1.append(fDev);
	acc.append(r1);
	const r2 = el("div", "ar-row");
	const fPw = el("div", "ar-f");
	fPw.append(el("label", "", "密码"));
	ui.passwordInput = el("input");
	ui.passwordInput.type = "password";
	// 密码不回显也不缓：真值只存在本机 ds_gateway.json 里
	ui.passwordInput.placeholder = "已绑过可留空（留空 = 不动已存的密码）";
	fPw.append(ui.passwordInput);
	r2.append(fPw);
	ui.bindBtn = el("button", "ar-btn pri", "添加并试跑");
	ui.bindBtn.addEventListener("click", () => bindAccount(ui));
	r2.append(ui.bindBtn);
	acc.append(r2);
	const r3 = el("div", "ar-row");
	ui.testBtn = el("button", "ar-btn", "试跑可用账号");
	ui.testBtn.addEventListener("click", () => testAccount(ui, ""));
	r3.append(ui.testBtn);
	acc.append(r3);
	// 思考强度：与自定义端点页同一个参数（DeepSeek 官方只有布尔开关，这里把五档映成开/关）
	const gEff = el("div", "ar-row");
	gEff.append(ctlSelect(ui, "custom_effort", "思考强度（五档）"));
	acc.append(gEff);
	acc.append(ctlToggle(ui, "deepseek_thinking", "深度思考（仅强度选「不思考」时生效）"));
	// 服务状态与日志收到一个默认收起的面板里，主视图只留账号相
	const svc = el("details", "ar-details");
	svc.append(el("summary", "", "内置网关服务与日志"));
	ui.svcBtn = el("button", "ar-btn", "服务状态待读");
	ui.svcBtn.addEventListener("click", () => toggleService(ui));
	svc.append(ui.svcBtn);
	ui.svcLine = el("div", "ar-hint");
	ui.svcLine.textContent = "正在读内置网关状态…";
	svc.append(ui.svcLine);
	ui.svcLogs = el("pre", "ar-logs");
	svc.append(ui.svcLogs);
	acc.append(svc);
	pageDs.append(acc);
	body.append(pageDs);
	refreshGatewayInfo(ui);

	// ---- 自定义端点页：一个端点，地址/密钥/模型下拉 ----
	const pageCustom = el("div", "ar-page");
	const cc = el("div", "ar-card");
	cc.append(el("div", "cap", "OpenAI 兼容端点"));

	// 一个端点：地址 / 密钥 / 模型下拉（真值存本机，不进工作流）
	const fBase = el("div", "ar-f");
	fBase.append(el("label", "", "请求地址（base URL）"));
	ui.epBase = el("input");
	ui.epBase.placeholder = "https://…/v1";
	ui.epBase.addEventListener("change", () => saveCustomEndpoint(ui));
	fBase.append(ui.epBase);
	cc.append(fBase);

	const fKey = el("div", "ar-f");
	fKey.append(el("label", "", "API 密钥"));
	ui.epKey = el("input");
	ui.epKey.type = "password";
	ui.epKey.placeholder = "已存则留空不动";
	ui.epKey.addEventListener("change", () => saveCustomEndpoint(ui));
	fKey.append(ui.epKey);
	cc.append(fKey);

	const mRow = el("div", "ar-row");
	const fModel = el("div", "ar-f");
	fModel.append(el("label", "", "模型"));
	ui.modelSel = el("select");
	ui.modelSel.addEventListener("change", () => setModelValue(ui, ui.modelSel.value));
	fModel.append(ui.modelSel);
	mRow.append(fModel);
	const fFetch = el("div", "ar-f ar-fbtn");
	fFetch.append(el("label", "", "　"));
	ui.modelFetchBtn = el("button", "ar-btn", "拉取模型列表");
	ui.modelFetchBtn.addEventListener("click", () => fetchModels(ui));
	fFetch.append(ui.modelFetchBtn);
	mRow.append(fFetch);
	cc.append(mRow);

	cc.append(ctlSelect(ui, "custom_effort", "思考强度（五档）"));
	cc.append(ctlToggle(ui, "custom_stream", "流式返回（默认非流式）"));
	const testRow = el("div", "ar-row");
	ui.customTestBtn = el("button", "ar-btn", "测试连通");
	ui.customTestBtn.addEventListener("click", () => fetchModels(ui, true));
	testRow.append(ui.customTestBtn);
	cc.append(testRow);
	pageCustom.append(cc);
	body.append(pageCustom);
	loadEndpoints(ui);
	renderModelSelect(ui);
	setTimeout(() => refreshModeDot(ui), 600);

	body.append(ui.status);

	const PROVIDER_MAP = { local: "本地模型（transformers）", ds: "DeepSeek 云端（内置网关）", custom: "自定义 OpenAI 兼容端点" };

	// 顶部只剩两个控件：“主界面”按钮 + “模式”下拉。
	// 选模式 = 同时把 provider widget 写回（跟旧三按钮分段同一条写入链），并跳到那一页的配置。
	function applyView() {
		const cur = st();                     // 现取：provider 永远来自 widget
		const onMain = cur.tab !== "mode";
		pageMain.classList.toggle("hidden", !onMain);
		pageLocal.classList.toggle("hidden", onMain || cur.provider !== "local");
		pageDs.classList.toggle("hidden", onMain || cur.provider !== "ds");
		pageCustom.classList.toggle("hidden", onMain || cur.provider !== "custom");
		mainBtn.classList.toggle("on", onMain);
		if (modeSel.value !== cur.provider) modeSel.value = cur.provider;
	}

	mainBtn.addEventListener("click", () => {
		st().tab = "main";
		saveState(node);
		applyView();
	});
	modeSel.addEventListener("change", () => {
		// 先写 widget（唯一真源），再刷面板；只写面板自己那份会被 widget 反向覆盖
		const w = widgetOf(node, "provider");
		if (w) {
			w.value = PROVIDER_MAP[modeSel.value];
			w.callback?.(w.value);
		}
		stateFor(node).tab = "mode";
		saveState(node);
		applyView();
		if (modeSel.value === "custom") { loadEndpoints(ui); renderModelSelect(ui); }
		refreshModeDot(ui);   // 灯只跟当前模式走（唯一写者，ds 那档会自己拉网关状态）
	});

	function applyProvider() {
		applyView();
	}

	// 节点尺寸完全由用户拖动（同画廊节点）；面板 height:100% 跟随，内容内部滚动
	ui.resize = () => {};
	ui.applyProvider = applyProvider;
	ui.applyView = applyView;

	applyView();
	return ui;
}

// ================= 扩展入口 =================

app.registerExtension({
	name: "AssembledRecipes.ReverseCloudPanel",

	async init() {
		purgeStaleLocalStorage();
	},

	beforeRegisterNodeDef(nodeType, nodeData) {
		if (nodeData?.name !== NODE_CLASS) return;

		// loadGraphData 后前端会重建 widget：在创建处一出场就隐藏
		installHideGuard(nodeType);

		// 工作流加载：重隐（防重建的 widget 漏网）+ 旧尺寸收敛
		const origConfigure = nodeType.prototype.onConfigure;
		nodeType.prototype.onConfigure = function () {
			const r = origConfigure?.apply(this, arguments);
			hideNatives(this);
			// 工作流里的 provider 优先：面板状态跟着它走，别让加载把模式重置成本地
			const key = providerKeyOf(this);
			const ui = ui_map.get(this);
			if (key && ui) {
				const st = stateFor(this);
				if (st.provider !== key) st.provider = key;
				ui.applyView?.();
			}
			// 旧工作流里存的尺寸是原生 widget 时代留下的（过高/过矮都不适用），收敛到可用区间；
			// 区间内的尺寸尊重用户自己的拖动结果
			if (this.size[1] < 500 || this.size[1] > DEFAULT_SIZE[1] + 240) {
				this.setSize([Math.max(this.size[0], MIN_SIZE[0]), DEFAULT_SIZE[1]]);
			}
			this._arDesiredSize = [this.size[0], this.size[1]];
			return r;
		};

		const origOn = nodeType.prototype.onNodeCreated;
		nodeType.prototype.onNodeCreated = function () {
			const r = origOn?.apply(this, arguments);
			injectStyle();

			// 原生 widget 全部隐入面板（仍在 node.widgets 里，序列化链路不变）
			hideNatives(this);
			const ui = buildPanel(this);

			// —— 以下尺寸处理照搬画廊节点（ComfyUI-Aaalice-Nodes）的已验证配方 ——
			// ① widget type 加实例后缀：Nodes 2.0 按 node id + widget name + type 缓存渲染器，
			//    撤销重建时稳定 key 会复用旧组件导致 element 挂不上
			const widget = this.addDOMWidget("arcloud_panel", `custom__ar_instance_${++instanceSeq}`, ui.root, {
				serialize: false, hideOnZoom: true, margin: 0,
				getMinHeight: () => MIN_SIZE[1], getValue: () => "", setValue: () => {},
			});
			// ② 宽度绑定到节点：前端（#12443）会把陈旧的测量值回写 widget.width ——
			//    回写成 0/小值时 DOM 容器塔成细条，高度测量无限增长
			Object.defineProperty(widget, "width", {
				configurable: true, enumerable: true,
				get: () => this.width, set(_w) {},
			});
			// ③ 不定义 widget.computeSize：高度由 getMinHeight + 节点尺寸决定，面板 CSS 自己填满

			// ④ 拖拽命中透传：命中节点缩放手柄时交给画布，不让面板抢事件
			installResizePassthrough(this, ui.root);
			// ⑤ 尺寸接管：用户拖动是唯一改尺寸的路径
			takeOverSize(this);
			// ⑥ 初始尺寸只设一次
			if (!this._arSized) {
				this._arSized = true;
				const w = Math.max(this.size[0] || MIN_SIZE[0], MIN_SIZE[0]);
				const h = Math.max(this.size[1] || DEFAULT_SIZE[1], DEFAULT_SIZE[1]);
				this.setSize([w, h]);
				this._arDesiredSize = [w, h];
			}
			// ⑦ 补隐：前端可能在 createNode 之后才填完 widget
			setTimeout(() => hideNatives(this), 0);
			setTimeout(() => hideNatives(this), 300);

			return r;
		};
	},
});
