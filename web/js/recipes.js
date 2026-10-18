// AssembledRecipes 配方管理面板：浏览已拼装配方 + 删除（软删除到 trash/）
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

async function listRecipes() {
  const r = await api.fetchApi("/assembled-recipes/list");
  return (await r.json()).recipes || [];
}

async function deleteRecipe(name) {
  const r = await api.fetchApi("/assembled-recipes/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return r.ok;
}

function fmtSize(b) {
  return b > 1 << 30 ? (b / (1 << 30)).toFixed(2) + " GiB" : (b / (1 << 20)).toFixed(1) + " MiB";
}

app.registerExtension({
  name: "AssembledRecipes.Panel",
  setup() {
    app.extensionManager.registerSidebarTab({
      id: "assembled-recipes",
      title: "配方缓存",
      icon: "pi pi-database",
      type: "custom",
      render: (el) => {
        const box = document.createElement("div");
        box.style.cssText = "padding:10px;display:flex;flex-direction:column;gap:8px;height:100%;overflow:auto;";
        box.innerHTML = `
          <div style="display:flex;gap:6px;">
            <button id="ar-refresh" style="flex:1;">刷新</button>
          </div>
          <div id="ar-list" style="display:flex;flex-direction:column;gap:6px;"></div>
          <div style="opacity:.6;font-size:11px;">删除 = 移入 models/assembled_recipes/trash/（可手动找回）</div>`;
        el.appendChild(box);

        const render = async () => {
          const list = box.querySelector("#ar-list");
          list.innerHTML = "加载中…";
          try {
            const recipes = await listRecipes();
            if (!recipes.length) {
              list.innerHTML = "（暂无配方）";
              return;
            }
            list.innerHTML = "";
            for (const r of recipes) {
              const card = document.createElement("div");
              card.style.cssText = "border:1px solid #444;border-radius:6px;padding:8px;";
              card.innerHTML = `
                <div style="font-weight:600;word-break:break-all;">${r.name}</div>
                <div style="opacity:.8;font-size:11px;">${fmtSize(r.size)} · ${r.created || ""}</div>
                ${r.lora_info ? `<div style="opacity:.7;font-size:11px;">LoRA: ${r.lora_info}</div>` : ""}
                ${r.note ? `<div style="opacity:.7;font-size:11px;">${r.note}</div>` : ""}
                <button data-name="${r.name}" style="margin-top:6px;color:#f66;">删除</button>`;
              card.querySelector("button").onclick = async (ev) => {
                if (!confirm(`删除配方 ${r.name} ？（移入 trash/）`)) return;
                ev.target.disabled = true;
                await deleteRecipe(r.name);
                render();
              };
              list.appendChild(card);
            }
          } catch (e) {
            list.innerHTML = "加载失败: " + e;
          }
        };
        box.querySelector("#ar-refresh").onclick = render;
        render();
      },
    });
  },
});
