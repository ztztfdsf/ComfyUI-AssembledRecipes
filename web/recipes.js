// AssembledRecipes 配方画廊 v3：DOM 挂载（Nodes 2.0 下唯一可靠的显示方式，照 Aaalice 的挂法）
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";


// ── 诊断上报（浏览器 → 服务端日志）──
function diag(stage, extra) {
  try {
    api.fetchApi("/assembled-recipes/diag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage, ts: Date.now(), url: location.href, ...(extra || {}) }),
    });
  } catch (e) { /* ignore */ }
}
diag("module-loaded", { hasApp: !!app, hasApi: !!api, hasExtMgr: !!app?.extensionManager });
window.addEventListener("error", (e) => diag("window-error", { msg: String(e.message || e.error), src: e.filename, line: e.lineno }));
window.addEventListener("unhandledrejection", (e) => diag("promise-error", { msg: String(e.reason) }));

const REPO_URL = "https://github.com/ztztfdsf/ComfyUI-AssembledRecipes";
const REPO_TEXT = "github.com/ztztfdsf/ComfyUI-AssembledRecipes";

async function fetchRecipes() {
  const r = await api.fetchApi("/assembled-recipes/list");
  const d = await r.json();
  return d.recipes || [];
}
async function deleteRecipe(name) {
  await api.fetchApi("/assembled-recipes/delete", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}
function fmtSize(b) {
  return b > 1 << 30 ? (b / (1 << 30)).toFixed(2) + "G" : (b / (1 << 20)).toFixed(0) + "M";
}


// ───────────────────────── 节点内画廊（DOM 挂载） ─────────────────────────
let _galInst = 0;

function injectStyles() {
  if (document.getElementById("ar-gal-style")) return;
  const s = document.createElement("style");
  s.id = "ar-gal-style";
  s.textContent = `
.ar-gal{display:flex;flex-direction:column;height:100%;min-height:240px;background:#191919;border:1px solid #333;border-radius:8px;overflow:hidden;font:12px/1.4 sans-serif;color:#ddd;}
.ar-gal .ar-head{display:flex;align-items:center;gap:8px;padding:6px 8px;background:#222;flex:0 0 auto;}
.ar-gal .ar-head button{cursor:pointer;border:1px solid #555;background:#2c2c2c;color:#ddd;border-radius:6px;padding:3px 10px;}
.ar-gal .ar-head .ar-count{opacity:.7;font-size:11px;}
.ar-gal .ar-head .ar-repo{margin-left:auto;font-size:10px;color:#79b6ff;text-decoration:none;}
.ar-gal .ar-grid{flex:1 1 auto;overflow-y:auto;display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:8px;align-content:start;}
.ar-gal .ar-card{position:relative;border:1px solid #444;border-radius:8px;background:#242424;padding:6px;cursor:pointer;}
.ar-gal .ar-card:hover{border-color:#8af;}
.ar-gal .ar-card.sel{border-color:#6c6;background:#24391f;}
.ar-gal .ar-card img{width:100%;height:96px;object-fit:contain;background:#111;border-radius:4px;display:block;}
.ar-gal .ar-card .nm{font-weight:600;font-size:11px;word-break:break-all;margin-top:4px;}
.ar-gal .ar-card .mt{opacity:.7;font-size:10px;}
.ar-gal .ar-card .del{position:absolute;right:6px;top:6px;width:20px;height:20px;border-radius:50%;background:rgba(200,40,40,.92);color:#fff;border:none;cursor:pointer;font-weight:700;line-height:1;}
.ar-gal .ar-empty{grid-column:1/-1;opacity:.75;padding:24px 12px;text-align:center;}
`;
  document.head.appendChild(s);
}

function mountGalleryDOM(node) {
  injectStyles();
  const root = document.createElement("div");
  root.className = "ar-gal";
  root.innerHTML = `
    <div class="ar-head">
      <button class="ar-refresh" type="button">🔄 刷新</button>
      <span class="ar-count"></span>
      <a class="ar-repo" href="${REPO_URL}" target="_blank" rel="noreferrer">${REPO_TEXT}</a>
    </div>
    <div class="ar-grid"><div class="ar-empty">加载中…</div></div>`;
  const grid = root.querySelector(".ar-grid");
  const countEl = root.querySelector(".ar-count");

  const refresh = async () => {
    grid.innerHTML = `<div class="ar-empty">加载中…</div>`;
    let list = [];
    try {
      list = await fetchRecipes();
    } catch (e) {
      grid.innerHTML = `<div class="ar-empty">加载失败：${String(e).slice(0, 60)}</div>`;
      return;
    }
    countEl.textContent = `${list.length} 个配方`;
    if (!list.length) {
      grid.innerHTML = `<div class="ar-empty">（暂无配方）先跑一张带「配方·保存」的图</div>`;
      return;
    }
    const sel = (node.widgets?.find((w) => w.name === "selected_recipe")?.value || "").replace(/\.safetensors$/, "");
    grid.innerHTML = "";
    for (const it of list) {
      const base = it.name.replace(/\.safetensors$/, "");
      const card = document.createElement("div");
      card.className = "ar-card" + (base === sel ? " sel" : "");
      const img = document.createElement("img");
      img.src = `/assembled-recipes/preview/${encodeURIComponent(it.name)}`;
      img.onerror = () => { img.style.opacity = ".2"; };
      const nm = document.createElement("div");
      nm.className = "nm";
      nm.textContent = base;
      const mt = document.createElement("div");
      mt.className = "mt";
      mt.textContent = `${fmtSize(it.size)} · ${it.created || ""}`;
      const del = document.createElement("button");
      del.className = "del";
      del.type = "button";
      del.title = "删除（进 trash/ 可找回）";
      del.textContent = "×";
      card.append(img, nm, mt, del);

      card.addEventListener("click", () => {
        const w = node.widgets?.find((x) => x.name === "selected_recipe");
        if (w) w.value = base;
        for (const c of grid.querySelectorAll(".ar-card")) c.classList.remove("sel");
        card.classList.add("sel");
      });
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除配方 ${base} ？（移入 trash/ 可找回）`)) return;
        await deleteRecipe(it.name);
        refresh();
      });
      grid.appendChild(card);
    }
  };
  root.querySelector(".ar-refresh").addEventListener("click", refresh);

  // Nodes 2.0 按 (node id, widget name, widget type) 复用渲染行，所以 type 要带实例后缀
  const type = `ar_gallery__inst_${(_galInst++).toString(36)}`;
  const surfaces = { canvas: "shown", vueNode: "never", panel: "never" };
  try {
    const widget = node.addDOMWidget("ar_gallery", type, root, {
      serialize: false,
      hideOnZoom: true,
      margin: 0,
      surfaces,
      getValue: () => "",
      setValue: () => {},
    });
    if (widget) {
      widget.surfaces = surfaces;
      if (!widget.options) widget.options = {};
      Object.assign(widget.options, { serialize: false, surfaces, getValue: () => "", setValue: () => {} });
    }
    diag("gallery-dom-mounted", { widgetType: type, hasWidget: !!widget });
  } catch (e) {
    diag("gallery-dom-error", { msg: String(e) });
  }
  try { node.setSize([380, 540]); } catch (e) {}
  refresh();
}

app.registerExtension({
  name: "AssembledRecipes.Gallery",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== "RecipeGallery (AssembledRecipes)") return;
    diag("gallery-hook", {});
    const proto = nodeType.prototype;
    const origCreated = proto.onNodeCreated;
    proto.onNodeCreated = function () {
      const r = origCreated ? origCreated.apply(this, arguments) : undefined;
      try {
        mountGalleryDOM(this);
      } catch (e) {
        diag("gallery-mount-error", { msg: String(e) });
      }
      return r;
    };
  },
});


// ───────────────────────── 其他节点：右下角仓库地址 ─────────────────────────
app.registerExtension({
  name: "AssembledRecipes.RepoCorner",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!nodeData?.name || !nodeData.name.includes("(AssembledRecipes)")) return;
    if (nodeData.name === "RecipeGallery (AssembledRecipes)") return;

    const origDraw = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function (ctx) {
      if (origDraw) origDraw.apply(this, arguments);
      if (this.flags?.collapsed) return;
      ctx.save();
      ctx.font = "9px sans-serif";
      const w = ctx.measureText(REPO_TEXT).width;
      const x = Math.max(4, this.size[0] - w - 8);
      const y = this.size[1] - 4;
      this._repoRect = { x: x - 2, y: y - 10, w: w + 4, h: 12 };
      ctx.fillStyle = "rgba(120,170,255,0.85)";
      ctx.fillText(REPO_TEXT, x, y);
      ctx.restore();
    };

    const origDown = nodeType.prototype.onMouseDown;
    nodeType.prototype.onMouseDown = function (e, localPos) {
      const r = this._repoRect;
      if (r && localPos && localPos[0] >= r.x && localPos[0] <= r.x + r.w
          && localPos[1] >= r.y && localPos[1] <= r.y + r.h) {
        window.open(REPO_URL, "_blank");
        return true;
      }
      return origDown ? origDown.apply(this, arguments) : false;
    };
  },
});

// ───────────────────────── 侧边栏面板（DOM 悬浮层） ─────────────────────────
app.registerExtension({
  name: "AssembledRecipes.Panel",
  setup() {
    try {
      app.extensionManager.registerSidebarTab({
        id: "assembled-recipes",
        title: "配方画廊",
        icon: "pi pi-images",
        type: "custom",
        render: (el) => {
          const box = document.createElement("div");
          box.style.cssText = "padding:8px;height:100%;overflow:auto;box-sizing:border-box;color:#ddd;";
          box.innerHTML = `<button id="ar-refresh" style="width:100%;margin-bottom:8px;">刷新</button>
                           <div id="ar-grid" style="column-count:2;column-gap:8px;"></div>`;
          el.appendChild(box);
          const grid = box.querySelector("#ar-grid");
          const render = async () => {
            grid.innerHTML = "加载中…";
            let list = [];
            try { list = await fetchRecipes(); } catch (e) { grid.innerHTML = "加载失败"; return; }
            if (!list.length) { grid.innerHTML = "（暂无配方）"; return; }
            grid.innerHTML = "";
            for (const it of list) {
              const base = it.name.replace(/\.safetensors$/, "");
              const card = document.createElement("div");
              card.style.cssText = "position:relative;break-inside:avoid;border:1px solid #444;border-radius:8px;padding:6px;margin-bottom:8px;background:#222;";
              card.innerHTML = `
                <img src="/assembled-recipes/preview/${encodeURIComponent(it.name)}" style="width:100%;border-radius:4px;display:block;background:#111;" onerror="this.style.opacity=.2">
                <div style="font-weight:600;font-size:12px;word-break:break-all;margin-top:4px;"></div>
                <div style="opacity:.7;font-size:10px;">${fmtSize(it.size)} · ${it.created || ""}</div>
                <button style="position:absolute;right:6px;top:6px;width:20px;height:20px;border-radius:50%;background:rgba(200,40,40,.9);color:#fff;border:none;cursor:pointer;">×</button>`;
              card.querySelector("div").textContent = base;
              card.querySelector("button").onclick = async (ev) => {
                ev.stopPropagation();
                if (!confirm(`删除配方 ${base} ？（移入 trash/）`)) return;
                await deleteRecipe(it.name);
                render();
              };
              grid.appendChild(card);
            }
          };
          box.querySelector("#ar-refresh").onclick = render;
          render();
        },
      });
    } catch (e) {
      console.warn("[AssembledRecipes] 侧边栏注册失败:", e);
    }
  },
});


// ── 自动补线：模型链断了自动接上（只动我们自己的节点 + VRAM_Debug 清理节点）──
function autoRepair(reason) {
  const graph = app.graph;
  if (!graph || !graph._nodes) return;
  const nodes = graph._nodes;
  const has = (n, kw) => (n.type || "").includes(kw);
  const modelOut = (n) => {
    const idx = (n.outputs || []).findIndex((o) => o.type === "MODEL");
    return idx >= 0 ? idx : -1;
  };
  const modelIn = (n) => (n.inputs || []).findIndex((i) => i.name === "model" || i.name === "model_pass");
  const freeOut = (n, idx) => !n.outputs[idx].links || n.outputs[idx].links.length === 0;

  const galleries = nodes.filter((n) => has(n, "RecipeGallery") || has(n, "RecipeRouter"));
  const vds = nodes.filter((n) => has(n, "VRAM_Debug"));
  const sources = nodes.filter((n) => has(n, "Lora Loader") || has(n, "UNETLoader"));
  let fixed = 0;

  // ① 清理节点缺 model_pass 输入 → 从左侧的加载器/LoRA 接上
  for (const d of vds) {
    const ii = modelIn(d);
    if (ii < 0) continue;
    if (d.inputs[ii].link != null) continue;
    const up = sources.filter((s) => s.pos[0] < d.pos[0] && modelOut(s) >= 0);
    if (!up.length) continue;
    const src = up.sort((a, b) => b.pos[0] - a.pos[0])[0];
    try {
      src.connect(modelOut(src), d, ii);
      fixed++;
      diag("autorepair-vd", { from: src.type, to: d.title || d.type });
    } catch (e) { diag("autorepair-vd-fail", { err: String(e) }); }
  }

  // ② 画廊缺 model 输入 → 优先接左边空闲的 model_pass 输出，其次接 LoRA
  for (const g of galleries) {
    const ii = (g.inputs || []).findIndex((i) => i.name === "model");
    if (ii < 0 || g.inputs[ii].link != null) continue;
    let src = null;
    const cands = vds.filter((d) => d.pos[0] < g.pos[0] && modelOut(d) >= 0)
      .map((d) => ({ d, slot: d.outputs.findIndex((o) => o.name === "model_pass") }))
      .filter((x) => x.slot >= 0);
    const preferred = cands.find((x) => freeOut(x.d, x.slot)) || cands[0];
    if (preferred) src = { n: preferred.d, slot: preferred.slot };
    else {
      const ls = sources.filter((s) => s.pos[0] < g.pos[0] && modelOut(s) >= 0);
      if (ls.length) src = { n: ls.sort((a, b) => b.pos[0] - a.pos[0])[0], slot: modelOut(ls.sort((a, b) => b.pos[0] - a.pos[0])[0]) };
    }
    if (src) {
      try {
        src.n.connect(src.slot, g, ii);
        fixed++;
        diag("autorepair-gallery", { from: src.n.type, to: g.type });
      } catch (e) { diag("autorepair-gallery-fail", { err: String(e) }); }
    }
  }

  if (fixed) {
    graph.setDirtyCanvas(true, true);
    console.warn(`[AssembledRecipes] 自动补线 ${fixed} 根（${reason}）`);
  }
  return fixed;
}

app.registerExtension({
  name: "AssembledRecipes.AutoRepair",
  async afterConfigureGraph() { setTimeout(() => autoRepair("graph-loaded"), 300); },
  async setup() { setTimeout(() => autoRepair("page-load"), 2000); },
  async loadedGraphNode() { setTimeout(() => autoRepair("node-loaded"), 600); },
});
