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
const GATEWAY = "http://127.0.0.1:5001";

const STYLE_ID = "ar-reverse-cloud-css";
const CSS = `
/* ===== 节点壳：本体只在标题栏 + 端口，body 全给面板 ===== */
.arshell { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 12px;
  font: 12px/1.5 system-ui, sans-serif; color: var(--fg-color, #dcdcde);
  background: var(--comfy-input-bg, #2b2b30); box-sizing: border-box;
  height: 540px; overflow-y: auto; overflow-x: hidden; }
.arshell::-webkit-scrollbar { width: 8px; }
.arshell::-webkit-scrollbar-thumb { background: #444a; border-radius: 4px; }
.arshell::-webkit-scrollbar { width: 8px; }
.arshell::-webkit-scrollbar-thumb { background: #444a; border-radius: 4px; }

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
.ar-status { font-size: 11.5px; min-height: 16px; word-break: break-all; opacity: .9; }
.ar-status.ok { color: #7bd88a; }
.ar-status.err { color: #ff7373; }

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

// ================= 原生 widget 接管 =================
//
// node.widgets 摘掉全部原生 widget，只在面板 widget 上渲染。
// 序列化（保存工作流 / graphToPrompt 队列）时通过覆写 serializeValue 的集合行为还原。
// 实现：把原 widgets 存到 node[AR_ORIG]，覆写 node.serialize 用「临时换回」策略。

const AR_ORIG = Symbol("ar-orig-widgets");

function detachWidgets(node) {
	// 幂等：无论被调多少次，widgets 里只剩面板；原生按名字收集（去重保序）
	const names = new Set(node[AR_ORIG]?.map((w) => w.name) || []);
	const natives = (node.widgets || []).filter((w) => w.name !== "arcloud_panel" && w.name !== "image");
	const kept = [];
	for (const w of natives) {
		if (!names.has(w.name)) { names.add(w.name); kept.push(w); }
		else {
			// 已有同名原件 → 保留值新的那个（后填的）
			const idx = node[AR_ORIG] ? node[AR_ORIG].findIndex((x) => x.name === w.name) : -1;
			if (idx >= 0 && node[AR_ORIG]) node[AR_ORIG][idx] = w;
		}
	}
	node[AR_ORIG] = (node[AR_ORIG] || []).concat(kept);
	node.widgets = (node.widgets || []).filter((w) => w.name === "arcloud_panel" || w.name === "image");
	// 摘除的 widget 若有已挂载的 DOM（Vue/DOM widget 容器），直接卸载，防 label 残影
	for (const w of node[AR_ORIG]) {
		if (w.element?.parentNode) w.element.parentNode.removeChild(w.element);
		if (w.inputEl?.parentNode) w.inputEl.parentNode.removeChild(w.inputEl);
	}
}

function withOrigWidgets(node, fn) {
	if (!node[AR_ORIG]) return fn();
	const panel = node.widgets || [];
	const own = node.widgets;
	node.widgets = panel.concat(node[AR_ORIG]);
	try {
		return fn();
	} finally {
		node.widgets = own;
	}
}

// ================= 面板控件（值写回原生 widget 对象） =================

function widgetOf(node, name) {
	return (node[AR_ORIG] || []).find((w) => w.name === name);
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
		ui.resize();
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

// ================= 账号绑定 / 测试 =================

function setStatus(ui, cls, text) {
	ui.status.textContent = text;
	ui.status.className = "ar-status" + (cls ? " " + cls : "");
}

async function adminJwt() {
	const key = localStorage.getItem("arReverseAdminKey") || "";
	if (!key) throw new Error("先填网关管理密钥（ds2api admin key）");
	const data = await jsonFetch(`${GATEWAY}/admin/login`, {
		method: "POST", body: JSON.stringify({ admin_key: key }),
	});
	return data.token;
}

async function bindAccount(ui) {
	const account = ui.accountInput.value.trim();
	const password = ui.passwordInput.value;
	if (!account || !password) {
		setStatus(ui, "err", "邮箱和密码都要填");
		return;
	}
	ui.bindBtn.disabled = true;
	ui.dot.className = "ar-dot busy";
	setStatus(ui, "", "正在绑定账号到本机网关…");
	try {
		const jwt = await adminJwt();
		const list = await jsonFetch(`${GATEWAY}/admin/accounts`, { headers: { Authorization: `Bearer ${jwt}` } });
		const exists = (list.items || []).some((a) => a.identifier === account);
		if (!exists) {
			await jsonFetch(`${GATEWAY}/admin/accounts`, {
				method: "POST",
				headers: { Authorization: `Bearer ${jwt}` },
				body: JSON.stringify({ email: account, password, name: "反推节点", remark: "ComfyUI 反推节点绑定" }),
			});
		}
		localStorage.setItem(LS_ACCOUNT, account);
		localStorage.setItem(LS_PASSWORD, password);
		stateFor(ui.node).account = account;
		saveState(ui.node);
		await testAccount(ui, { silent: true });
	} catch (e) {
		ui.dot.className = "ar-dot err";
		setStatus(ui, "err", "绑定失败：" + (e.message || e));
	} finally {
		ui.bindBtn.disabled = false;
		ui.resize();
	}
}

async function testAccount(ui, { silent = false } = {}) {
	const account = localStorage.getItem(LS_ACCOUNT) || ui.accountInput.value.trim();
	if (!account) { setStatus(ui, "err", "没有已绑定的账号"); return null; }
	ui.testBtn.disabled = true;
	ui.dot.className = "ar-dot busy";
	if (!silent) setStatus(ui, "", "测试网关连通与账号状态…");
	try {
		const KEY = widgetOf(ui.node, "openai_key")?.value || "";
		const t0 = performance.now();
		const data = await jsonFetch(`${GATEWAY}/v1/chat/completions`, {
			method: "POST",
			headers: { Authorization: `Bearer ${KEY}` },
			body: JSON.stringify({ model: "deepseek-v4.1-flash-nothinking", max_tokens: 5,
				messages: [{ role: "user", content: "ping" }] }),
		});
		const ms = Math.round(performance.now() - t0);
		ui.dot.className = "ar-dot ok";
		setStatus(ui, "ok", `连通正常（${ms}ms，${data.model || "deepseek"}）`);
		return true;
	} catch (e) {
		ui.dot.className = "ar-dot err";
		setStatus(ui, "err", "测试失败：" + (e.message || e));
		return false;
	} finally {
		ui.testBtn.disabled = false;
		ui.resize();
	}
}

// ================= 结果可视化 =================

function apiUrl(item) {
	const p = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder || "", type: item.type || "input" });
	return "/view?" + p.toString();
}

// ================= 面板 =================

const ui_map = new WeakMap();

function buildPanel(node) {
	const st = stateFor(node);
	const root = el("div", "arshell");
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
	root.append(head);

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
	root.append(seg);

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
	root.append(pageLocal);

	// ---- 云端页 ----
	const pageDs = el("div", "ar-page");
	const acc = el("div", "ar-card");
	acc.append(el("div", "cap", "DeepSeek 账号（密码只存本浏览器）"));
	const r1 = el("div", "ar-row");
	const fAcc = el("div", "ar-f");
	fAcc.append(el("label", "", "邮箱 / 手机号"));
	ui.accountInput = el("input");
	ui.accountInput.value = localStorage.getItem(LS_ACCOUNT) || st.account || "";
	fAcc.append(ui.accountInput);
	r1.append(fAcc);
	acc.append(r1);
	const r2 = el("div", "ar-row");
	const fPw = el("div", "ar-f");
	fPw.append(el("label", "", "密码"));
	ui.passwordInput = el("input");
	ui.passwordInput.type = "password";
	ui.passwordInput.value = localStorage.getItem(LS_PASSWORD) || "";
	fPw.append(ui.passwordInput);
	r2.append(fPw);
	ui.bindBtn = el("button", "ar-btn pri", "绑定");
	ui.bindBtn.addEventListener("click", () => bindAccount(ui));
	r2.append(ui.bindBtn);
	acc.append(r2);
	const r3 = el("div", "ar-row");
	ui.testBtn = el("button", "ar-btn", "测试连通");
	ui.testBtn.addEventListener("click", () => testAccount(ui));
	r3.append(ui.testBtn);
	const fAdmin = el("div", "ar-f");
	fAdmin.append(el("label", "", "网关管理密钥（首次绑定填一次）"));
	const adminInput = el("input");
	adminInput.type = "password";
	adminInput.placeholder = "ds2api admin key";
	adminInput.value = localStorage.getItem("arReverseAdminKey") || "";
	adminInput.addEventListener("change", () => localStorage.setItem("arReverseAdminKey", adminInput.value.trim()));
	fAdmin.append(adminInput);
	r3.append(fAdmin);
	acc.append(r3);
	ui.status = el("div", "ar-status");
	acc.append(ui.status);
	pageDs.append(acc);
	root.append(pageDs);

	// ---- 自定义端点页 ----
	const pageCustom = el("div", "ar-page");
	const cc = el("div", "ar-card");
	cc.append(el("div", "cap", "OpenAI 兼容端点"));
	cc.append(ctlText(ui, "openai_base", "请求地址（base URL）", { placeholder: "https://api.deepseek.com" }));
	cc.append(ctlText(ui, "openai_key", "API 密钥", { placeholder: "sk-..." }));
	cc.append(ctlText(ui, "openai_model", "模型名", { placeholder: "deepseek-v4.1-flash" }));
	cc.append(ctlToggle(ui, "deepseek_thinking", "深度思考（关了更快）"));
	pageCustom.append(cc);
	root.append(pageCustom);

	const PROVIDER_MAP = { local: "本地模型（transformers）", ds: "DeepSeek 云端（ds2api 本地网关）", custom: "自定义 OpenAI 兼容端点" };

	function applyProvider() {
		pageLocal.classList.toggle("hidden", st.provider !== "local");
		pageDs.classList.toggle("hidden", st.provider !== "ds");
		pageCustom.classList.toggle("hidden", st.provider !== "custom");
		const w = widgetOf(node, "provider");
		if (w && w.value !== PROVIDER_MAP[st.provider]) {
			w.value = PROVIDER_MAP[st.provider];
			w.callback?.();
		}
		ui.resize();
	}

	ui.resize = () => {
		requestAnimationFrame(() => {
			if (!document.contains(root)) return;
			const w0 = 430;
			// 高度完全静态（CSS 540px），只保证最小宽度
			if (!(node.size[0] >= w0) || !(node.size[1] >= 580)) {
				node.setSize([Math.max(node.size[0] || w0, w0), Math.max(node.size[1] || 580, 580)]);
			}
			app.graph.setDirtyCanvas(true, true);
		});
	};
	ui.applyProvider = applyProvider;

	applyProvider();
	return ui;
}

// ================= 扩展入口 =================

app.registerExtension({
	name: "AssembledRecipes.ReverseCloudPanel",

	beforeRegisterNodeDef(nodeType, nodeData) {
		if (nodeData?.name !== NODE_CLASS) return;

		// 序列化（保存 / graphToPrompt）时把原生 widget 临时拼回，值按原顺序写出
		// 工作流加载（configure 重建 widget）后清扫：重摘原生、面板复位
		const origConfigure = nodeType.prototype.onConfigure;
		nodeType.prototype.onConfigure = function () {
			const r = origConfigure?.apply(this, arguments);
			if (this[AR_ORIG]) {
				detachWidgets(this);
				this.widgetsChanged?.();
			}
			return r;
		};

		const origSerialize = nodeType.prototype.serialize;
		nodeType.prototype.serialize = function () {
			if (!this[AR_ORIG]) return origSerialize?.apply(this, arguments);
			return withOrigWidgets(this, () => origSerialize.apply(this, arguments));
		};

		const origOn = nodeType.prototype.onNodeCreated;
		nodeType.prototype.onNodeCreated = function () {
			const r = origOn?.apply(this, arguments);
			injectStyle();

			// 先摘掉全部原生 widget（存 AR_ORIG），再建面板（控件从 AR_ORIG 取值），最后挂面板
			detachWidgets(this);
			const ui = buildPanel(this);
			const widget = this.addDOMWidget("arcloud_panel", "custom", ui.root, { serialize: false });
			widget.computeSize = () => [0, 552]; // 静态：面板 CSS 锁 540px + 余量，永不测量
			this.widgetsChanged?.();
			ui.resize();
			// 二次清扫：Vue 前端可能在 createNode 后又填充一轮 widget
			setTimeout(() => { detachWidgets(this); this.widgetsChanged?.(); ui.resize(); }, 0);
			setTimeout(() => { detachWidgets(this); this.widgetsChanged?.(); ui.resize(); }, 300);
		
			return r;
		};
	},
});
