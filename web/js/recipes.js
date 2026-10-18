// AssembledRecipes 配方画廊：瀑布流浏览已拼装配方（点选即用、× 删除）
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const REPO_URL = "https://github.com/ztztfdsf/ComfyUI-AssembledRecipes";
const REPO_TEXT = "github.com/ztztfdsf/ComfyUI-AssembledRecipes";

const CARD_CSS = `
  .ar-wrap { width: 100%; height: 100%; overflow: auto; box-sizing: border-box; }
  .ar-masonry { column-count: 2; column-gap: 8px; padding: 4px; }
  .ar-card { position: relative; break-inside: avoid; border: 1px solid #444; border-radius: 8px;
             padding: 6px; margin: 0 0 8px 0; cursor: pointer; background: #222; overflow: hidden; }
  .ar-card:hover { border-color: #8af; }
  .ar-card.selected { border-color: #6c6; box-shadow: 0 0 8px #6c6; }
  .ar-card img { width: 100%; border-radius: 4px; display: block; background: #111; min-height: 40px; }
  .ar-card .nm { font-weight: 600; font-size: 12px; word-break: break-all; margin-top: 4px; color: #eee; }
  .ar-card .meta { opacity: .7; font-size: 10px; color: #ccc; }
  .ar-del { position: absolute; right: 8px; top: 8px; width: 22px; height: 22px; border-radius: 50%;
            background: rgba(200,40,40,.85); color: #fff; border: none; cursor: pointer; font-size: 14px;
            line-height: 20px; text-align: center; padding: 0; }
  .ar-del:hover { background: rgba(240,60,60,1); }
  .ar-empty { opacity: .65; font-size: 12px; padding: 12px; text-align: center; color: #ddd; }
`;

async function listRecipes() {
  const r = await api.fetchApi("/assembled-recipes/list");
  return (await r.json()).recipes || [];
}
async function deleteRecipe(name) {
  await api.fetchApi("/assembled-recipes/delete", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}
function fmtSize(b) {
  return b > 1 << 30 ? (b / (1 << 30)).toFixed(2) + " GiB" : (b / (1 << 20)).toFixed(1) + " MiB";
}

function buildGallery(container, { onSelect, getSelected, columns = 2 } = {}) {
  const style = document.createElement("style");
  style.textContent = CARD_CSS;
  container.appendChild(style);
  const wrap = document.createElement("div");
  wrap.className = "ar-wrap";
  const masonry = document.createElement("div");
  masonry.className = "ar-masonry";
  masonry.style.columnCount = String(columns);
  wrap.appendChild(masonry);
  container.appendChild(wrap);

  const render = async () => {
    masonry.innerHTML = "加载中…";
    let recipes = [];
    try { recipes = await listRecipes(); }
    catch (e) { masonry.innerHTML = `<div class="ar-empty">加载失败: ${e}</div>`; return; }
    if (!recipes.length) {
      masonry.innerHTML = '<div class="ar-empty">（暂无配方——跑一张带「配方快照·保存」的图）</div>';
      return;
    }
    masonry.innerHTML = "";
    const sel = getSelected ? getSelected() : null;
    for (const r of recipes) {
      const base = r.name.replace(/\.safetensors$/, "");
      const card = document.createElement("div");
      card.className = "ar-card" + (sel && (sel === base || sel === r.name) ? " selected" : "");
      card.innerHTML = `
        <img src="/assembled-recipes/preview/${encodeURIComponent(r.name)}" loading="lazy"
             onerror="this.style.opacity=.2;this.alt='(无预览)'">
        <button class="ar-del" title="删除缓存">×</button>
        <div class="nm">${base}</div>
        <div class="meta">${fmtSize(r.size)} · ${r.created || ""}</div>
        ${r.lora_info ? `<div class="meta">${r.lora_info}</div>` : ""}`;
      card.onclick = (ev) => {
        if (ev.target.classList?.contains("ar-del")) return;
        onSelect && onSelect(r.name);
        render();
      };
      card.querySelector(".ar-del").onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除配方 ${base} ？（移入 trash/）`)) return;
        await deleteRecipe(r.name);
        render();
      };
      masonry.appendChild(card);
    }
  };
  return render;
}

// ① 画布节点：配方画廊（DOM 控件必须声明 surfaces.canvas=shown，否则不显示）
app.registerExtension({
  name: "AssembledRecipes.GalleryNode",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== "RecipeGallery (AssembledRecipes)") return;
    const orig = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = orig ? orig.apply(this, arguments) : undefined;
      const node = this;
      const container = document.createElement("div");
      container.style.cssText = "width:100%;height:360px;min-height:220px;overflow:auto;background:#1b1b1b;border-radius:6px;";
      node.addDOMWidget("ar_gallery", "ar_gallery", container, {
        serialize: false,
        hideOnZoom: false,
        surfaces: { canvas: "shown", vueNode: "never", panel: "never" },
      });
      const render = buildGallery(container, {
        columns: 2,
        getSelected: () => node.widgets?.find((w) => w.name === "selected_recipe")?.value || "",
        onSelect: (name) => {
          const w = node.widgets?.find((w) => w.name === "selected_recipe");
          if (w) {
            w.value = name.replace(/\.safetensors$/, "");
            node.setDirtyCanvas?.(true, true);
            app.graph?.setDirtyCanvas?.(true, true);
          }
        },
      });
      node._arRender = render;
      try { node.setSize([360, 500]); } catch (e) {}
      setTimeout(render, 50);
      return r;
    };
  },
});

// ② 侧边栏面板
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
          box.style.cssText = "padding:8px;height:100%;overflow:auto;box-sizing:border-box;";
          const head = document.createElement("div");
          head.innerHTML = `<button id="ar-refresh" style="width:100%;margin-bottom:8px;">刷新</button>`;
          const body = document.createElement("div");
          box.appendChild(head);
          box.appendChild(body);
          el.appendChild(box);
          const render = buildGallery(body, { columns: 2 });
          head.querySelector("#ar-refresh").onclick = render;
          render();
        },
      });
    } catch (e) {
      console.warn("[AssembledRecipes] 侧边栏注册失败:", e);
    }
  },
});

// ③ 节点右下角显示 GitHub 地址（点击打开）
app.registerExtension({
  name: "AssembledRecipes.RepoCorner",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!nodeData?.name || !nodeData.name.includes("(AssembledRecipes)")) return;

    const origDraw = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function (ctx) {
      if (origDraw) origDraw.apply(this, arguments);
      if (this.flags?.collapsed) return;
      ctx.save();
      ctx.font = "10px sans-serif";
      const w = ctx.measureText(REPO_TEXT).width;
      const x = Math.max(4, this.size[0] - w - 8);
      const y = this.size[1] - 6;
      this._repoRect = { x: x - 2, y: y - 11, w: w + 4, h: 13 };
      ctx.fillStyle = "rgba(120,170,255,0.9)";
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
