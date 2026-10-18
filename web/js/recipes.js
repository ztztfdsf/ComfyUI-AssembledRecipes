// AssembledRecipes 配方画廊：瀑布流浏览已拼装配方（点选即用、可删除）
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const CARD_CSS = `
  .ar-masonry { column-count: 2; column-gap: 8px; }
  .ar-card { break-inside: avoid; border: 1px solid #444; border-radius: 8px; padding: 6px; margin-bottom: 8px; cursor: pointer; background: #222; }
  .ar-card:hover { border-color: #8af; }
  .ar-card.selected { border-color: #6c6; box-shadow: 0 0 6px #6c6; }
  .ar-card { position: relative; }
  .ar-card img { width: 100%; border-radius: 4px; display: block; background: #111; min-height: 60px; }
  .ar-del { position: absolute; right: 8px; top: 8px; width: 22px; height: 22px; border-radius: 50%;
            background: rgba(200,40,40,.85); color: #fff; border: none; cursor: pointer; font-size: 14px;
            line-height: 20px; text-align: center; padding: 0; }
  .ar-del:hover { background: rgba(240,60,60,1); }
  .ar-card .nm { font-weight: 600; font-size: 12px; word-break: break-all; margin-top: 4px; }
  .ar-card .meta { opacity: .7; font-size: 10px; }
  .ar-card button { margin-top: 4px; font-size: 10px; color: #f66; }
  .ar-empty { opacity: .6; font-size: 12px; padding: 12px; text-align: center; }
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

function buildGallery(container, { onSelect, getSelected, compact }) {
  const style = document.createElement("style");
  style.textContent = CARD_CSS;
  container.appendChild(style);
  const masonry = document.createElement("div");
  masonry.className = "ar-masonry";
  if (compact) masonry.style.columnCount = "1";
  container.appendChild(masonry);

  const render = async () => {
    masonry.innerHTML = "加载中…";
    let recipes = [];
    try { recipes = await listRecipes(); } catch (e) { masonry.innerHTML = "加载失败: " + e; return; }
    if (!recipes.length) { masonry.innerHTML = '<div class="ar-empty">（暂无配方——跑一张带「配方快照·保存」的图）</div>'; return; }
    masonry.innerHTML = "";
    const sel = getSelected ? getSelected() : null;
    for (const r of recipes) {
      const base = r.name.replace(/\.safetensors$/, "");
      const card = document.createElement("div");
      card.className = "ar-card" + (sel && (sel === base || sel === r.name) ? " selected" : "");
      card.innerHTML = `
        <img src="/assembled-recipes/preview/${encodeURIComponent(r.name)}" onerror="this.style.opacity=.25" loading="lazy">
        <button class="ar-del" title="删除缓存">×</button>
        <div class="nm">${base}</div>
        <div class="meta">${fmtSize(r.size)} · ${r.created || ""}</div>
        ${r.lora_info ? `<div class="meta">LoRA: ${r.lora_info}</div>` : ""}`;
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

app.registerExtension({
  name: "AssembledRecipes.Panel",
  setup() {
    app.extensionManager.registerSidebarTab({
      id: "assembled-recipes",
      title: "配方画廊",
      icon: "pi pi-images",
      type: "custom",
      render: (el) => {
        const box = document.createElement("div");
        box.style.cssText = "padding:8px;height:100%;overflow:auto;";
        const head = document.createElement("div");
        head.innerHTML = `<button id="ar-refresh" style="width:100%;margin-bottom:8px;">刷新</button>`;
        const body = document.createElement("div");
        box.appendChild(head); box.appendChild(body);
        el.appendChild(box);
        const render = buildGallery(body, {});
        head.querySelector("#ar-refresh").onclick = render;
        render();
      },
    });
  },

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "RecipeGallery (AssembledRecipes)") return;
    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
      const node = this;
      const container = document.createElement("div");
      container.style.cssText = "max-height:360px;overflow:auto;padding:2px;";
      node.addDOMWidget("ar_gallery", "ar_gallery", container, { serialize: false });
      const render = buildGallery(container, {
        getSelected: () => (node.widgets?.find(w => w.name === "selected_recipe")?.value) || "",
        onSelect: (name) => {
          const w = node.widgets?.find(w => w.name === "selected_recipe");
          if (w) { w.value = name.replace(/\.safetensors$/, ""); node.setDirtyCanvas?.(true, true); }
        },
      });
      node._arRender = render;
      render();
      return r;
    };
  },
});
