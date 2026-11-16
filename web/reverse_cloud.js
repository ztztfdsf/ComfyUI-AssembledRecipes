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
const LS_PASSWORD = "arReverseDeepseekPassword";
const LS_ACCOUNT = "arReverseDeepseekAccount";
const LS_DEVICE_ID = "arReverseDeepseekDeviceId";
const DS_API = "/assembled-recipes/ds"; // 内置网关的控制面（同源，走 ComfyUI 自已在的端口）

const STYLE_ID = "ar-reverse-cloud-css";
const CSS = `
/* ===== 面板壳（照抄画廊节点的防回环配方）=====
 * .dom-widget 容器高度由 LiteGraph 从 widget 测量回写；壳必须与内容尺寸解耦：
 *   min-height:0 + grid minmax(0,1fr) 让滚动区可以小于内容高
 *   content-visibility + contain-intrinsic-size 切断「内容高 → 容器高」的测量反馈 */
.dom-widget:has(> .arshell) { pointer-events: none !important; }
.arshell { width: 100%; height: 100%; min-width: 0; min-height: 0;
  display: grid; grid-template-rows: minmax(0, 1fr); overflow: hidden;
  box-sizing: border-box; pointer-events: auto;
  font: 12px/1.5 system-ui, sans-serif; color: var(--fg-color, #dcdcde);
  background: var(--comfy-input-bg, #2b2b30);
  content-visibility: auto; contain-intrinsic-size: 430px 560px; }
.arbody { min-height: 0; overflow-y: auto; overflow-x: hidden;
  padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 12px; }
.arbody::-webkit-scrollbar { width: 8px; }
.arbody::-webkit-scrollbar-thumb { background: #444a; border-radius: 4px; }
.arshell.is-resizing, .arshell.is-resizing * { pointer-events: none !important; user-select: none !important; }

/* 顶栏 */
.ar-head { display: flex; align-items: center; gap: 10px; }
.ar-head .t { font-weight: 700; font-size: 12.5px; letter-spacing: .5px; }
.ar-dot { width: 9px; height: 9px; border-radius: 50%; background: #6a6a72; flex: none; }
.ar-dot.ok { background: #4fc46a; box-shadow: 0 0 8px #4fc46a77; }
.ar-dot.err { background: #e05555; box-shadow: 0 0 8px #e0555577; }
.ar-dot.busy { background: #e0b855; animation: arPulse 1s infinite; }
@keyframes arPulse { 50% { opacity: .35; } }
.ar-head .sp { flex: 1; }

/* 分段选择 */
.ar-seg { display: flex; gap: 4px; background: rgba(0,0,0,.28); padding: 3px; border-radius: 9px; }
.ar-seg button { flex: 1; padding: 7px 6px; border: none; border-radius: 7px;
  background: transparent; color: var(--fg-color, #bbb); font: 11.5px system-ui;
  cursor: pointer; transition: background .12s, color .12s; white-space: nowrap; }
.ar-seg button.on { background: #3f66cc; color: #fff; font-weight: 600; }
.ar-seg button:hover:not(.on) { background: rgba(255,255,255,.07); }

/* 分区卡片 */
details.ar-card>summary { list-style: none; }
details.ar-card>summary::-webkit-details-marker { display: none; }
details.ar-card[open]>summary { margin-bottom: 6px; }

.ar-card { border: 1px solid rgba(255,255,255,.09); border-radius: 10px;
  background: rgba(255,255,255,.025); padding: 12px 14px; display: flex;
  flex-direction: column; gap: 10px; }
.ar-card>.cap { font-size: 10.5px; font-weight: 700; letter-spacing: 1px;
  text-transform: uppercase; opacity: .5; margin-bottom: 2px; }

/* 表单行 */
.ar-row { display: flex; gap: 10px; }
.ar-row>* { flex: 1; min-width: 0; }
.ar-f { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.ar-f>label { font-size: 10.5px; opacity: .55; }
.ar-f input, .ar-f select, .ar-f textarea {
  background: rgba(0,0,0,.3); border: 1px solid rgba(255,255,255,.12);
  border-radius: 7px; padding: 7px 10px; color: var(--fg-color, #ddd);
  font: 12px system-ui; width: 100%; box-sizing: border-box; }
.ar-f textarea { resize: vertical; min-height: 58px; line-height: 1.45; }
.ar-f input:focus, .ar-f select:focus, .ar-f textarea:focus {
  outline: none; border-color: #3f66cc; }
.ar-f input[type=number]::-webkit-inner-spin-button { opacity: .4; }

/* 开关行 */
.ar-sw { display: flex; align-items: center; justify-content: space-between;
  padding: 3px 0; }
.ar-sw .lab { font-size: 12px; opacity: .85; }
.ar-tg { width: 38px; height: 20px; border-radius: 10px; background: rgba(255,255,255,.14);
  position: relative; cursor: pointer; flex: none; transition: background .15s; }
.ar-tg.on { background: #3f66cc; }
.ar-tg::after { content: ""; position: absolute; top: 3px; left: 3px; width: 14px; height: 14px;
  border-radius: 50%; background: #e8e8ea; transition: left .15s; }
.ar-tg.on::after { left: 21px; }

/* 按钮 */
.ar-btn { padding: 7px 16px; border: 1px solid rgba(255,255,255,.14); border-radius: 7px;
  background: rgba(255,255,255,.06); color: var(--fg-color, #ddd);
  font: 12px system-ui; cursor: pointer; white-space: nowrap; transition: filter .12s; }
.ar-btn:hover { filter: brightness(1.25); }
.ar-btn:disabled { opacity: .4; cursor: default; }
.ar-btn.pri { background: #3f66cc; border-color: #5580e0; color: #fff; font-weight: 600; }

/* 状态行 */
.ar-hint { font-size: 10.5px; opacity: .62; margin: 4px 0 2px; word-break: break-all; }
.ar-logs { font-size: 10px; line-height: 1.45; margin: 4px 0 0; padding: 6px 8px; max-height: 96px;
  overflow: auto; background: rgba(0,0,0,.28); border-radius: 6px; color: #cfd6e4;
  white-space: pre-wrap; word-break: break-all; font-family: Consolas, Menlo, monospace; }
.ar-status { font-size: 11.5px; min-height: 16px; word-break: break-all; opacity: .9; }
.ar-status.ok { color: #7bd88a; }
.ar-status.err { color: #ff7373; }

/* 账号池列表 */
.ar-accts { display: flex; flex-direction: column; gap: 4px; margin: 2px 0 6px; }
.ar-acct { display: grid; grid-template-columns: auto 1fr auto auto; gap: 6px; align-items: center;
  padding: 4px 6px; background: rgba(255,255,255,.045); border-radius: 6px; font-size: 11px; }
.ar-badge { font-size: 10px; padding: 1px 6px; border-radius: 999px; white-space: nowrap;
  background: rgba(255,255,255,.10); color: #d8dee9; }
.ar-badge.ok { background: rgba(96,190,110,.20); color: #86e08f; }
.ar-badge.err { background: rgba(220,90,90,.22); color: #ff9090; }
.ar-badge.busy { background: rgba(220,180,70,.20); color: #f0cf7a; }
.ar-mail { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; opacity: .95; }
.ar-meta { grid-column: 1 / -1; font-size: 10px; opacity: .55; margin: -2px 0 0 2px; }
.ar-acct .ar-mini { font-size: 10px; padding: 2px 7px; border-radius: 5px; border: 1px solid rgba(255,255,255,.16);
  background: rgba(255,255,255,.06); color: inherit; cursor: pointer; }
.ar-acct .ar-mini:hover { filter: brightness(1.3); }
.ar-acct .ar-mini.danger { border-color: rgba(220,90,90,.45); color: #ff9090; }

.ar-page { display: flex; flex-direction: column; gap: 12px; }
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
	node.properties[PROP] ||= { provider: "local", lastResult: null, account: "" };
	return node.properties[PROP];
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
	});
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
	});
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
	});
	c.append(sel);
	return c;
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
	});
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
		ui.acctList.append(el("div", "ar-hint", "账号池是空的——下面填一个邮箱+密码点「添加并试跑」。多个账号会自动轮巡。"));
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
		line.textContent = `池内 ${total} 个，${st.ready} 个可用，按顺序轮巡（上一个跑完自动转下一个，风控压力摊开）` +
			(st.last_account ? ` · 上一次用的是 ${st.last_account}` : "");
	} else if (total === 1) {
		line.textContent = "唯一账号不可用" + (st.recover_text ? `，最早 ${st.recover_text} 恢复` : "") +
			"——再添加一个就能接着跑";
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
		localStorage.setItem(LS_PASSWORD, password);
		if (deviceId) localStorage.setItem(LS_DEVICE_ID, deviceId);
		stateFor(ui.node).account = account;
		saveState(ui.node);
		if (data.ok) {
			ui.dot.className = "ar-dot ok";
			setStatus(ui, "ok", `已加入账号池并试跑成功，登录+一枪 ${data.ms}ms（回：${data.echo || "ok"}）`);
		} else {
			ui.dot.className = "ar-dot err";
			setStatus(ui, "err", "这个账号不通：" + (data.error || "未知错") + "（已存进池子，可随时再试跑）");
		}
		refreshGatewayInfo(ui);
	} catch (e) {
		ui.dot.className = "ar-dot err";
		setStatus(ui, "err", "绑定失败：" + (e.message || e));
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
			ui.dot.className = "ar-dot ok";
			setStatus(ui, "ok", `连通正常（${data.email || ""} · ${data.ms}ms，${data.model}）`);
		} else {
			ui.dot.className = "ar-dot err";
			setStatus(ui, "err", "不通：" + (data.error || "未知错"));
		}
		return data.ok;
	} catch (e) {
		ui.dot.className = "ar-dot err";
		setStatus(ui, "err", "测试失败：" + (e.message || e));
		return false;
	} finally {
		ui.testBtn.disabled = false;
		refreshGatewayInfo(ui);
	}
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
	if (ui.dot && !ui.bindBtn.disabled && !ui.testBtn.disabled) {
		// 灯色跟池子实况走：有可用账号=绿，服务在但池子全忙=红，服务没起=红
		ui.dot.className = "ar-dot " + (st.running && st.ready > 0 ? "ok" : "err");
	}
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
	const st = stateFor(node);
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

	// ---- provider 分段 ----
	const seg = el("div", "ar-seg");
	const segBtns = [];
	const options = [["local", "本地模型"], ["ds", "DeepSeek 云端"], ["custom", "自定义端点"]];
	for (const [key, label] of options) {
		const b = el("button", st.provider === key ? "on" : "", label);
		b.addEventListener("click", () => {
			st.provider = key;
			saveState(node);
			segBtns.forEach((x) => x.classList.remove("on"));
			b.classList.add("on");
			applyProvider();
		});
		segBtns.push(b);
		seg.append(b);
	}
	body.append(seg);

	// ---- 本地页 ----
	const pageLocal = el("div", "ar-page");
	const c1 = el("div", "ar-card");
	c1.append(el("div", "cap", "本地模型"));
	const g1 = el("div", "ar-row");
	g1.append(ctlSelect(ui, "model_name", "模型（models/LLM）"), ctlSelect(ui, "quant", "量化"));
	c1.append(g1);
	const g2 = el("div", "ar-row");
	g2.append(ctlNum(ui, "max_new_tokens", "最大生成长度", { min: 64, max: 65536 }), ctlNum(ui, "batch_index", "batch_index", { min: 0, max: 64 }));
	c1.append(g2);
	pageLocal.append(c1);

	const c2 = el("div", "ar-card");
	c2.append(el("div", "cap", "运行"));
	c2.append(ctlToggle(ui, "pre_clear", "反推前清场（腾显存）"));
	c2.append(ctlToggle(ui, "offload_after", "结束后卸载模型"));
	const g3 = el("div", "ar-row");
	g3.append(ctlSelect(ui, "tag_mode", "画廊标签模式"));
	pageLocal.append(c2, g3);

	const c3 = el("details", "ar-card");
	const sum = el("summary", "cap", "规则提示词 rule（点开编辑）");
	sum.style.cursor = "pointer";
	c3.append(sum);
	c3.append(ctlText(ui, "rule", "布局规则（框选 / 前后关系 / 输出格式）", { multiline: true }));
	pageLocal.append(c3);
	body.append(pageLocal);

	// ---- 云端页 ----
	const pageDs = el("div", "ar-page");
	const acc = el("div", "ar-card");
	acc.append(el("div", "cap", "DeepSeek 账号池（多个账号自动轮巡；账密存插件本地配置，不进日志也不进工作流文件）"));
	ui.acctList = el("div", "ar-accts");
	acc.append(ui.acctList);
	const r1 = el("div", "ar-row");
	const fAcc = el("div", "ar-f");
	fAcc.append(el("label", "", "邮箱 / 手机号"));
	ui.accountInput = el("input");
	ui.accountInput.value = localStorage.getItem(LS_ACCOUNT) || st.account || "";
	fAcc.append(ui.accountInput);
	r1.append(fAcc);
	const fDev = el("div", "ar-f");
	fDev.append(el("label", "", "device_id（数美指纹，可留空沿用）"));
	ui.deviceIdInput = el("input");
	ui.deviceIdInput.placeholder = "BK…（真实浏览器抓的 89 字串）";
	ui.deviceIdInput.value = localStorage.getItem(LS_DEVICE_ID) || "";
	fDev.append(ui.deviceIdInput);
	r1.append(fDev);
	acc.append(r1);
	const r2 = el("div", "ar-row");
	const fPw = el("div", "ar-f");
	fPw.append(el("label", "", "密码"));
	ui.passwordInput = el("input");
	ui.passwordInput.type = "password";
	ui.passwordInput.value = localStorage.getItem(LS_PASSWORD) || "";
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
	ui.svcBtn = el("button", "ar-btn", "服务状态待读");
	ui.svcBtn.addEventListener("click", () => toggleService(ui));
	r3.append(ui.svcBtn);
	acc.append(r3);
	ui.svcLine = el("div", "ar-hint");
	ui.svcLine.textContent = "正在读内置网关状态…";
	acc.append(ui.svcLine);
	ui.svcLogs = el("pre", "ar-logs");
	acc.append(ui.svcLogs);
	ui.status = el("div", "ar-status");
	acc.append(ui.status);
	pageDs.append(acc);
	body.append(pageDs);
	refreshGatewayInfo(ui);

	// ---- 自定义端点页 ----
	const pageCustom = el("div", "ar-page");
	const cc = el("div", "ar-card");
	cc.append(el("div", "cap", "OpenAI 兼容端点"));
	cc.append(ctlText(ui, "openai_base", "请求地址（base URL）", { placeholder: "https://api.deepseek.com" }));
	cc.append(ctlText(ui, "openai_key", "API 密钥", { placeholder: "sk-..." }));
	cc.append(ctlText(ui, "openai_model", "模型名", { placeholder: "deepseek-v4.1-flash" }));
	cc.append(ctlToggle(ui, "deepseek_thinking", "深度思考（关了更快）"));
	pageCustom.append(cc);
	body.append(pageCustom);

	const PROVIDER_MAP = { local: "本地模型（transformers）", ds: "DeepSeek 云端（内置网关）", custom: "自定义 OpenAI 兼容端点" };

	function applyProvider() {
		pageLocal.classList.toggle("hidden", st.provider !== "local");
		pageDs.classList.toggle("hidden", st.provider !== "ds");
		pageCustom.classList.toggle("hidden", st.provider !== "custom");
		const w = widgetOf(node, "provider");
		if (w && w.value !== PROVIDER_MAP[st.provider]) {
			w.value = PROVIDER_MAP[st.provider];
			w.callback?.();
		}
	}

	// 节点尺寸完全由用户拖动（同画廊节点）；面板 height:100% 跟随，内容内部滚动
	ui.resize = () => {};
	ui.applyProvider = applyProvider;

	applyProvider();
	return ui;
}

// ================= 扩展入口 =================

app.registerExtension({
	name: "AssembledRecipes.ReverseCloudPanel",

	beforeRegisterNodeDef(nodeType, nodeData) {
		if (nodeData?.name !== NODE_CLASS) return;

		// loadGraphData 后前端会重建 widget：在创建处一出场就隐藏
		installHideGuard(nodeType);

		// 工作流加载：重隐（防重建的 widget 漏网）+ 旧尺寸收敛
		const origConfigure = nodeType.prototype.onConfigure;
		nodeType.prototype.onConfigure = function () {
			const r = origConfigure?.apply(this, arguments);
			hideNatives(this);
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
