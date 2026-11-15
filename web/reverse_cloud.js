/**
 * ReverseLayoutCaption 前端 — 节点内嵌云端面板。
 *
 * 框架对齐画廊节点（BooruGalleryNode）的做法：
 *   - 状态持久化在 node.properties（随工作流保存）
 *   - addDOMWidget 挂实体面板，节点可拉高
 *   - 账号密码不进工作流（密码只存浏览器 localStorage）
 *
 * 面板结构：
 *   [provider 分段选择：本地 | DeepSeek 云端 | 自定义端点]
 *   [云端页]
 *     ├ 账号卡：邮箱 / 密码 / 绑定账号 + 测试 / 状态徽标
 *     └ 连接信息：网关地址（本机 ds2api）、模型、最近一次反推结果（框数/耗时）
 *   [结果页（特色）]：最近一次反推的框叠在缩略图上可视化
 */
import { app } from "../../scripts/app.js";

const NODE_CLASS = "ReverseLayoutCaption (AssembledRecipes)";
const PROP = "arReverseCloud";
const LS_PASSWORD = "arReverseDeepseekPassword";
const LS_ACCOUNT = "arReverseDeepseekAccount";
const GATEWAY = "http://127.0.0.1:5001";
const ADMIN_KEY_DEFAULT = ""; // 首次绑定时填一次，存 localStorage

const STYLE_ID = "ar-reverse-cloud-css";
const CSS = `
.arcloud { display:flex; flex-direction:column; gap:8px; padding:10px 12px;
  font:12px/1.45 system-ui,sans-serif; color:var(--fg-color,#ddd);
  background:var(--comfy-input-bg,#2a2a2e); border-radius:8px; height:100%; box-sizing:border-box; }
.arcloud-head { display:flex; align-items:center; gap:8px; }
.arcloud-title { font-weight:600; font-size:12px; letter-spacing:.4px; opacity:.9; }
.arcloud-dot { width:8px; height:8px; border-radius:50%; background:#777; flex:none; }
.arcloud-dot.ok { background:#59c159; box-shadow:0 0 6px #59c15988; }
.arcloud-dot.err { background:#e05555; }
.arcloud-dot.busy { background:#e0b855; animation:arcloudPulse 1s infinite; }
@keyframes arcloudPulse { 50% { opacity:.4; } }
.arcloud-seg { display:flex; border:1px solid var(--border-color,#444); border-radius:6px; overflow:hidden; }
.arcloud-seg button { flex:1; padding:5px 4px; background:transparent; border:none; color:var(--fg-color,#ccc);
  font:11px system-ui; cursor:pointer; white-space:nowrap; }
.arcloud-seg button.on { background:var(--comfy-input-bg-active,#4a6cd4); color:#fff; }
.arcloud-page { display:flex; flex-direction:column; gap:8px; min-height:0; }
.arcloud-page.hidden { display:none; }
.arcloud-card { border:1px solid var(--border-color,#444); border-radius:8px; padding:8px 10px;
  display:flex; flex-direction:column; gap:6px; background:rgba(255,255,255,.02); }
.arcloud-card-title { font-size:11px; font-weight:600; opacity:.75; letter-spacing:.3px; }
.arcloud-row { display:flex; gap:6px; align-items:center; }
.arcloud-field { flex:1; display:flex; flex-direction:column; gap:2px; min-width:0; }
.arcloud-field label { font-size:10px; opacity:.6; }
.arcloud-field input { background:var(--comfy-input-bg,#1e1e22); border:1px solid var(--border-color,#444);
  border-radius:5px; padding:4px 8px; color:var(--fg-color,#ddd); font:11px system-ui; min-width:0; }
.arcloud-btn { padding:5px 12px; border:1px solid var(--border-color,#444); border-radius:6px;
  background:var(--comfy-input-bg,#333); color:var(--fg-color,#ddd); font:11px system-ui; cursor:pointer; white-space:nowrap; }
.arcloud-btn:hover { filter:brightness(1.15); }
.arcloud-btn:disabled { opacity:.45; cursor:default; }
.arcloud-btn.primary { background:#3b63c4; border-color:#4a77e0; color:#fff; }
.arcloud-status { font-size:10.5px; opacity:.85; min-height:14px; word-break:break-all; }
.arcloud-status.ok { color:#7dd87d; }
.arcloud-status.err { color:#ff7070; }
.arcloud-preview { position:relative; border:1px solid var(--border-color,#444); border-radius:8px;
  overflow:hidden; background:#111; min-height:90px; display:flex; align-items:center; justify-content:center; }
.arcloud-preview img { max-width:100%; max-height:220px; display:block; }
.arcloud-preview .empty { font-size:11px; opacity:.5; padding:20px; }
.arcloud-boxes { position:absolute; inset:0; pointer-events:none; }
.arcloud-box { position:absolute; border:1.5px solid #4a9ff0; background:rgba(74,159,240,.08); }
.arcloud-box .n { position:absolute; top:-1px; left:-1px; background:#4a9ff0; color:#fff;
  font:9px system-ui; padding:0 4px; border-radius:0 0 4px 0; }
.arcloud-meta { display:flex; gap:10px; font-size:10.5px; opacity:.75; flex-wrap:wrap; }
.arcloud-meta b { opacity:1; }
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

async function adminJwt() {
	const key = localStorage.getItem("arReverseAdminKey") || ADMIN_KEY_DEFAULT;
	const data = await jsonFetch(`${GATEWAY}/admin/login`, {
		method: "POST", body: JSON.stringify({ admin_key: key }),
	});
	return data.token;
}

// ---------- 账号绑定 ----------

async function bindAccount(ui) {
	const account = ui.accountInput.value.trim();
	const password = ui.passwordInput.value;
	if (!account || !password) {
		setStatus(ui, "err", "邮箱和密码都要填");
		return;
	}
	ui.bindBtn.disabled = true;
	ui.dot.className = "arcloud-dot busy";
	setStatus(ui, "", "正在绑定账号到本机网关…");
	try {
		const jwt = await adminJwt();
		// 已存在就更新（PUT），否则新增（POST）
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
		ui.accountInput.value = account;
		await testAccount(ui, { silentBind: true });
	} catch (e) {
		ui.dot.className = "arcloud-dot err";
		setStatus(ui, "err", "绑定失败：" + (e.message || e));
	} finally {
		ui.bindBtn.disabled = false;
	}
}

async function testAccount(ui, { silentBind = false } = {}) {
	const account = localStorage.getItem(LS_ACCOUNT) || ui.accountInput.value.trim();
	if (!account) { setStatus(ui, "err", "没有已绑定的账号"); return null; }
	ui.testBtn && (ui.testBtn.disabled = true);
	ui.dot.className = "arcloud-dot busy";
	if (!silentBind) setStatus(ui, "", "测试网关连通与账号状态…");
	try {
		const KEY = ui.node.widgets?.find((w) => w.name === "openai_key")?.value || "";
		const t0 = performance.now();
		const data = await jsonFetch(`${GATEWAY}/v1/chat/completions`, {
			method: "POST",
			headers: { Authorization: `Bearer ${KEY}` },
			body: JSON.stringify({ model: "deepseek-v4.1-flash-nothinking", max_tokens: 5,
				messages: [{ role: "user", content: "ping" }] }),
		});
		const ms = Math.round(performance.now() - t0);
		ui.dot.className = "arcloud-dot ok";
		const who = data.model || "deepseek";
		setStatus(ui, "ok", `连通正常（${ms}ms，${who}）`);
		return true;
	} catch (e) {
		ui.dot.className = "arcloud-dot err";
		setStatus(ui, "err", "测试失败：" + (e.message || e));
		return false;
	} finally {
		ui.testBtn && (ui.testBtn.disabled = false);
	}
}

function setStatus(ui, cls, text) {
	ui.status.textContent = text;
	ui.status.className = "arcloud-status" + (cls ? " " + cls : "");
}

// ---------- 结果可视化（特色）：框叠缩略图 ----------

function renderPreview(ui, node) {
	const st = stateFor(node);
	const holder = ui.preview;
	holder.innerHTML = "";
	const result = st.lastResult;
	if (!result || !Array.isArray(result.boxes) || !result.boxes.length) {
		holder.append(el("div", "empty", "跑一次反推后，布局框会叠在缩略图上显示在这里"));
		return;
	}
	if (result.thumb) {
		const img = el("img");
		img.src = result.thumb;
		holder.append(img);
	}
	const layer = el("div", "arcloud-boxes");
	const boxes = result.boxes.slice(0, 24);
	for (const b of boxes) {
		const [x0, y0, x1, y1] = b.bbox || [0, 0, 0, 0];
		const d = el("div", "arcloud-box");
		d.style.left = (x0 / 10) + "%";
		d.style.top = (y0 / 10) + "%";
		d.style.width = Math.max(1, (x1 - x0) / 10) + "%";
		d.style.height = Math.max(1, (y1 - y0) / 10) + "%";
		const idx = boxes.indexOf(b) + 1;
		d.title = (b.desc || b.text || "").slice(0, 220);
		d.append(el("span", "n", String(idx)));
		layer.append(d);
	}
	holder.append(layer);
}

function wireResultCapture(node, api) {
	// 拦截节点执行结果：214 的 text 输出就是反推 JSON
	const orig = node.onExecuted;
	node.onExecuted = function (msg) {
		orig?.apply(this, arguments);
		try {
			const texts = msg?.text || (msg && Object.values(msg).find((v) => Array.isArray(v) && typeof v[0] === "string"));
			const raw = Array.isArray(texts) ? texts[0] : texts;
			if (typeof raw !== "string" || !raw.trim().startsWith("[")) return;
			const boxes = JSON.parse(raw);
			const st = stateFor(node);
			st.lastResult = {
				boxes: Array.isArray(boxes) ? boxes : [],
				dur: msg?._ar_dur || null,
				thumb: st.lastResult?.thumb || null,
				ts: Date.now(),
			};
			saveState(node);
			if (ui_map.get(node)) { renderPreview(ui_map.get(node), node); updateMeta(ui_map.get(node), st); }
		} catch { /* 非 JSON 输出忽略 */ }
	};
}

function updateMeta(ui, st) {
	const r = st.lastResult;
	const n = r && Array.isArray(r.boxes) ? r.boxes.length : 0;
	ui.meta.innerHTML = "";
	ui.meta.append(
		el("span", "", "框数 "), el("b", "", String(n)),
		el("span", "", "账号 "), el("b", "", st.account || localStorage.getItem(LS_ACCOUNT) || "未绑定"),
	);
}

// ---------- 面板构建 ----------

const ui_map = new WeakMap();

function buildPanel(node) {
	const st = stateFor(node);
	const root = el("div", "arcloud");
	const ui = { node, root };
	ui_map.set(node, ui);

	// 头部
	const head = el("div", "arcloud-head");
	ui.dot = el("span", "arcloud-dot");
	head.append(el("span", "arcloud-title", "DeepSeek 云端反推"), ui.dot);
	root.append(head);

	// provider 分段
	const seg = el("div", "arcloud-seg");
	const options = [
		["local", "本地模型"],
		["ds", "DeepSeek 云端"],
		["custom", "自定义端点"],
	];
	const segBtns = [];
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

	// 页面：本地
	const pageLocal = el("div", "arcloud-page");
	pageLocal.append(el("div", "arcloud-status", "使用 ComfyUI/models/LLM 下的模型在显卡上反推，参数用节点本体的 widget。"));
	root.append(pageLocal);

	// 页面：云端
	const pageDs = el("div", "arcloud-page");
	const accountCard = el("div", "arcloud-card");
	accountCard.append(el("div", "arcloud-card-title", "DeepSeek 账号（密码只存在本浏览器）"));
	const rowAcc = el("div", "arcloud-row");
	const fAcc = el("div", "arcloud-field");
	fAcc.append(el("label", "", "邮箱 / 手机号"));
	ui.accountInput = el("input");
	ui.accountInput.value = localStorage.getItem(LS_ACCOUNT) || st.account || "";
	fAcc.append(ui.accountInput);
	rowAcc.append(fAcc);
	accountCard.append(rowAcc);
	const rowPw = el("div", "arcloud-row");
	const fPw = el("div", "arcloud-field");
	fPw.append(el("label", "", "密码"));
	ui.passwordInput = el("input");
	ui.passwordInput.type = "password";
	ui.passwordInput.value = localStorage.getItem(LS_PASSWORD) || "";
	fPw.append(ui.passwordInput);
	rowPw.append(fPw);
	ui.bindBtn = el("button", "arcloud-btn primary", "绑定");
	ui.bindBtn.addEventListener("click", () => bindAccount(ui));
	rowPw.append(ui.bindBtn);
	accountCard.append(rowPw);
	const rowOps = el("div", "arcloud-row");
	ui.testBtn = el("button", "arcloud-btn", "测试连通");
	ui.testBtn.addEventListener("click", () => testAccount(ui));
	rowOps.append(ui.testBtn);
	const keyHint = el("div", "arcloud-field");
	keyHint.append(el("label", "", "网关管理密钥（首次绑定填一次）"));
	const adminInput = el("input");
	adminInput.type = "password";
	adminInput.placeholder = "ds2api admin key";
	adminInput.value = localStorage.getItem("arReverseAdminKey") || "";
	adminInput.addEventListener("change", () => localStorage.setItem("arReverseAdminKey", adminInput.value.trim()));
	keyHint.append(adminInput);
	rowOps.append(keyHint);
	accountCard.append(rowOps);
	ui.status = el("div", "arcloud-status");
	accountCard.append(ui.status);
	pageDs.append(accountCard);
	root.append(pageDs);

	// 页面：自定义端点
	const pageCustom = el("div", "arcloud-page");
	pageCustom.append(el("div", "arcloud-status", "地址 / 密钥 / 模型名直接用节点本体上的 openai_base / openai_key / openai_model 三个 widget。"));
	root.append(pageCustom);

	// 结果预览（特色）
	const previewCard = el("div", "arcloud-card");
	previewCard.append(el("div", "arcloud-card-title", "最近一次反推结果"));
	ui.preview = el("div", "arcloud-preview");
	ui.preview.append(el("div", "empty", "跑一次反推后，布局框会叠在缩略图上显示在这里"));
	previewCard.append(ui.preview);
	ui.meta = el("div", "arcloud-meta");
	previewCard.append(ui.meta);
	root.append(previewCard);

	function applyProvider() {
		pageLocal.classList.toggle("hidden", st.provider !== "local");
		pageDs.classList.toggle("hidden", st.provider !== "ds");
		pageCustom.classList.toggle("hidden", st.provider !== "custom");
		// 同步 provider widget 值
		const w = (node.widgets || []).find((x) => x.name === "provider");
		if (w) {
			const map = { local: "本地模型（transformers）", ds: "DeepSeek 云端（ds2api 本地网关）", custom: "自定义 OpenAI 兼容端点" };
			if (w.value !== map[st.provider]) { w.value = map[st.provider]; w.callback?.(); }
		}
		requestAnimationFrame(() => {
			const h = 60 + (st.provider === "ds" ? 178 : 24) + 150;
			node.setSize([node.size[0], Math.max(node.size[0] * 0.45, h)]);
			app.graph.setDirtyCanvas(true, true);
		});
	}
	ui.applyProvider = applyProvider;
	applyProvider();
	renderPreview(ui, node);
	updateMeta(ui, st);
	return ui;
}

app.registerExtension({
	name: "AssembledRecipes.ReverseCloudPanel",

	beforeRegisterNodeDef(nodeType, nodeData) {
		if (nodeData?.name !== NODE_CLASS) return;
		const orig = nodeType.prototype.onNodeCreated;
		nodeType.prototype.onNodeCreated = function () {
			const r = orig?.apply(this, arguments);
			injectStyle();
			const ui = buildPanel(this);
			const widget = this.addDOMWidget("arcloud_panel", "custom", ui.root, { serialize: false });
			widget.computeSize = () => [0, 420];
			this.setSize([Math.max(this.size[0] || 320, 380), Math.max(this.size[1] || 300, 460)]);
			wireResultCapture(this);
			// 带缩略图：抓输入图的预览（LiteGraph 的 image 输入会在节点上挂 img）
			const origExec = this.onExecuted;
			this.onExecuted = function (msg) {
				origExec?.apply(this, arguments);
			};
			return r;
		};
	},

	nodeCreated(node) {
		if (node.comfyClass !== NODE_CLASS) return;
		// 输入图变化时把缩略图写进 lastResult.thumb（从节点的图像上传组件拿）
		const tryThumb = () => {
			const st = stateFor(node);
			const ui = ui_map.get(node);
			const imgWidget = (node.widgets || []).find((w) => w.name === "image" && w.img?.src) ||
				(node.images?.[0] ? { img: { src: "/view?" + new URLSearchParams(node.images[0]) } } : null);
			const src = imgWidget?.img?.src || (node.images?.[0] ? apiUrl(node.images[0]) : null);
			if (src && st.lastResult && st.lastResult.thumb !== src) {
				st.lastResult.thumb = src;
				if (ui) renderPreview(ui, node);
			}
		};
		function apiUrl(item) {
			const p = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder || "", type: item.type || "input" });
			return "/view?" + p.toString();
		}
		const iv = setInterval(() => {
			if (!document.contains(node?.domWidgetDataList?.[0]?.element || document.body)) { clearInterval(iv); return; }
			tryThumb();
		}, 3000);
	},
});
