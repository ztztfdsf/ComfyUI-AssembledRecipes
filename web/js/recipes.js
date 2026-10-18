// AssembledRecipes 配方画廊 v2：画布自绘瀑布流（不依赖 DOM 控件，兼容性最强）
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const REPO_URL = "https://github.com/ztztfdsf/ComfyUI-AssembledRecipes";
const REPO_TEXT = "github.com/ztztfdsf/ComfyUI-AssembledRecipes";

const IMG_CACHE = new Map();     // name -> HTMLImageElement
const META_CACHE = new Map();    // name -> {size, created, lora_info}

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
function getImg(name, onload) {
  if (IMG_CACHE.has(name)) return IMG_CACHE.get(name);
  const im = new Image();
  im.onload = () => { IMG_CACHE.set(name, im); onload && onload(); };
  im.onerror = () => { IMG_CACHE.set(name, null); };
  im.src = `/assembled-recipes/preview/${encodeURIComponent(name)}`;
  IMG_CACHE.set(name, null);
  return null;
}

// ───────────────────────── 画布节点：配方画廊 ─────────────────────────
app.registerExtension({
  name: "AssembledRecipes.Gallery",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== "RecipeGallery (AssembledRecipes)") return;

    const COLS = 2, PAD = 8, GAP = 8, CARD_H = 130, IMG_H = 96;
    const HEADER = 18;

    const proto = nodeType.prototype;
    const origCreated = proto.onNodeCreated;
    proto.onNodeCreated = function () {
      const r = origCreated ? origCreated.apply(this, arguments) : undefined;
      const node = this;
      node._ar = { recipes: [], scroll: 0, hover: -1, selected: "", loading: true };
      node.addWidget("button", "🔄 刷新配方", null, () => node._arRefresh());
      node._arRefresh = async () => {
        node._ar.loading = true;
        node.setDirtyCanvas(true, true);
        try {
          node._ar.recipes = await fetchRecipes();
          for (const it of node._ar.recipes) META_CACHE.set(it.name, it);
        } catch (e) {
          node._ar.recipes = [];
        }
        node._ar.loading = false;
        node.setDirtyCanvas(true, true);
      };
      node._arRefresh();
      try { node.setSize([380, 520]); } catch (e) {}
      return r;
    };

    // 判定网格区起点（widgets 之后）
    function gridTop(node) {
      const ws = node.widgets || [];
      const last = ws.length ? ws[ws.length - 1] : null;
      const y = last && last.last_y ? last.last_y + 34 : (node.widgets_start_y || 30) + ws.length * 24 + 30;
      return y;
    }

    proto.onDrawForeground = function (ctx) {
      const ar = this._ar;
      if (!ar || this.flags?.collapsed) {
        ctx.save();
        ctx.font = "9px sans-serif";
        ctx.fillStyle = "rgba(120,170,255,0.9)";
        ctx.fillText(REPO_TEXT, this.size[0] - 200, 10);
        ctx.restore();
        return;
      }
      const W = this.size[0], H = this.size[1];
      const top = gridTop(this);
      const viewH = H - top - 6;

      ctx.save();
      // 背景
      ctx.fillStyle = "#191919";
      ctx.fillRect(2, top, W - 4, viewH);
      ctx.beginPath();
      ctx.rect(2, top, W - 4, viewH);
      ctx.clip();

      if (ar.loading) {
        ctx.fillStyle = "#bbb";
        ctx.font = "12px sans-serif";
        ctx.fillText("加载中…", 10, top + 20);
        ctx.restore();
        return;
      }
      if (!ar.recipes.length) {
        ctx.fillStyle = "#aaa";
        ctx.font = "12px sans-serif";
        ctx.fillText("（暂无配方）先跑一张带「配方·保存」的图", 10, top + 20);
        ctx.restore();
        return;
      }

      const cardW = (W - PAD * 2 - GAP * (COLS - 1)) / COLS;
      const selKey = (this.widgets?.find((w) => w.name === "selected_recipe")?.value || "").replace(/\.safetensors$/, "");
      let idx = 0;
      for (let i = 0; i < ar.recipes.length; i++) {
        const it = ar.recipes[i];
        const base = it.name.replace(/\.safetensors$/, "");
        const col = i % COLS, row = Math.floor(i / COLS);
        const x = 2 + PAD + col * (cardW + GAP);
        const y = top + 6 + row * (CARD_H + GAP) - ar.scroll;
        if (y + CARD_H < top || y > H) continue;

        // 卡片底
        ctx.fillStyle = selKey === base ? "#24391f" : "#242424";
        ctx.fillRect(x, y, cardW, CARD_H);
        ctx.strokeStyle = selKey === base ? "#6c6" : "#444";
        ctx.lineWidth = selKey === base ? 2 : 1;
        ctx.strokeRect(x, y, cardW, CARD_H);
        if (ar.hover === i) {
          ctx.strokeStyle = "#8af";
          ctx.strokeRect(x, y, cardW, CARD_H);
        }

        // 缩略图
        const im = getImg(it.name, () => this.setDirtyCanvas(true, true));
        if (im && im.width) {
          const s = Math.min((cardW - 8) / im.width, IMG_H / im.height);
          const dw = im.width * s, dh = im.height * s;
          ctx.drawImage(im, x + (cardW - dw) / 2, y + 4 + (IMG_H - dh) / 2, dw, dh);
        } else {
          ctx.fillStyle = "#2f2f2f";
          ctx.fillRect(x + 4, y + 4, cardW - 8, IMG_H);
          ctx.fillStyle = "#777";
          ctx.font = "10px sans-serif";
          ctx.fillText("无预览", x + 10, y + IMG_H / 2 + 4);
        }

        // 名字 + 体积
        ctx.fillStyle = "#eee";
        ctx.font = "bold 11px sans-serif";
        let label = base;
        while (ctx.measureText(label).width > cardW - 30 && label.length > 6) label = label.slice(0, -2);
        ctx.fillText(label, x + 4, y + IMG_H + 16);
        ctx.fillStyle = "#9a9a9a";
        ctx.font = "9px sans-serif";
        ctx.fillText(fmtSize(it.size), x + 4, y + IMG_H + 28);

        // × 删除
        const bx = x + cardW - 20, by = y + 6;
        ctx.fillStyle = "#c62828";
        ctx.beginPath();
        ctx.arc(bx + 7, by + 7, 8, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "#fff";
        ctx.font = "bold 11px sans-serif";
        ctx.fillText("×", bx + 3.5, by + 11);
        ar[`box_${i}`] = { x, y, w: cardW, h: CARD_H, del: { x: bx, y: by, w: 16, h: 16 }, name: it.name, base };
        idx++;
      }
      ctx.restore();

      // 右下角仓库地址
      ctx.save();
      ctx.font = "9px sans-serif";
      ctx.fillStyle = "rgba(120,170,255,0.85)";
      ctx.fillText(REPO_TEXT, W - 200, H - 3);
      ctx.restore();
    };

    function hit(node, pos) {
      const ar = node._ar;
      if (!ar) return null;
      for (let i = 0; i < ar.recipes.length; i++) {
        const b = ar[`box_${i}`];
        if (!b) continue;
        if (pos[0] >= b.x && pos[0] <= b.x + b.w && pos[1] >= b.y && pos[1] <= b.y + b.h) return b;
      }
      return null;
    }

    proto.onMouseDown = function (e, pos) {
      const b = hit(this, pos);
      if (b) {
        if (pos[0] >= b.del.x && pos[0] <= b.del.x + b.del.w && pos[1] >= b.del.y && pos[1] <= b.del.y + b.del.h) {
          if (confirm(`删除配方 ${b.base} ？（移入 trash/）`)) {
            deleteRecipe(b.name).then(() => this._arRefresh && this._arRefresh());
          }
          return true;
        }
        const w = this.widgets?.find((x) => x.name === "selected_recipe");
        if (w) w.value = b.base;
        this._ar.selected = b.base;
        this.setDirtyCanvas(true, true);
        app.graph?.setDirtyCanvas(true, true);
        return true;
      }
      return false;
    };

    proto.onMouseMove = function (e, pos) {
      const b = hit(this, pos);
      const ar = this._ar || {};
      const i = b ? ar.recipes.findIndex((r) => r.name === b.name) : -1;
      if (i !== ar.hover) { ar.hover = i; this.setDirtyCanvas(true, true); }
      return b ? true : false;
    };

    proto.onMouseWheel = function (e, pos, delta) {
      if (!this._ar) return false;
      const top = gridTop(this);
      if (pos[1] < top) return false;
      const rows = Math.ceil(this._ar.recipes.length / COLS);
      const maxScroll = Math.max(0, rows * (CARD_H + GAP) - (this.size[1] - top));
      this._ar.scroll = Math.max(0, Math.min(maxScroll, this._ar.scroll + (delta > 0 ? 1 : -1) * 60));
      this.setDirtyCanvas(true, true);
      return true;
    };

    // 兼容旧仓角逻辑：整型节点也显示地址（已在上方绘制）
    const origDown = proto.onMouseDown;
    proto.onMouseDown = function (e, pos) {
      const r = this._repoRect;
      if (r && pos && pos[0] >= r.x && pos[0] <= r.x + r.w && pos[1] >= r.y && pos[1] <= r.y + r.h) {
        window.open(REPO_URL, "_blank");
        return true;
      }
      return origDown ? origDown.apply(this, arguments) : false;
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
                <div style="font-weight:600;font-size:12px;word-break:break-all;margin-top:4px;">${base}</div>
                <div style="opacity:.7;font-size:10px;">${fmtSize(it.size)} · ${it.created || ""}</div>
                <button style="position:absolute;right:6px;top:6px;width:20px;height:20px;border-radius:50%;background:rgba(200,40,40,.9);color:#fff;border:none;cursor:pointer;">×</button>`;
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
