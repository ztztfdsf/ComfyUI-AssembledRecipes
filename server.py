"""HTTP routes: 配方列表 / 删除（软删除到 trash/）。"""

import json
import os
import shutil

from aiohttp import web

from .nodes import RECIPES_DIR, TRASH_DIR, recipe_files

try:
    from server import PromptServer
except Exception:  # pragma: no cover
    PromptServer = None


def _info(name: str) -> dict:
    path = os.path.join(RECIPES_DIR, name)
    meta = {}
    meta_path = os.path.splitext(path)[0] + ".json"
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
    return {
        "name": name,
        "size": os.path.getsize(path),
        "mtime": os.path.getmtime(path),
        "note": meta.get("note", ""),
        "lora_info": meta.get("lora_info", ""),
        "created": meta.get("created", ""),
    }


async def list_recipes(request):
    return web.json_response({"recipes": [_info(f) for f in recipe_files()]})


async def delete_recipe(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    name = os.path.basename(data.get("name", ""))
    if not name.endswith(".safetensors"):
        return web.json_response({"ok": False, "error": "bad name"}, status=400)
    src = os.path.join(RECIPES_DIR, name)
    if not os.path.exists(src):
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    dst = os.path.join(TRASH_DIR, name)
    if os.path.exists(dst):
        os.remove(dst)
    shutil.move(src, dst)
    meta = os.path.splitext(src)[0] + ".json"
    if os.path.exists(meta):
        shutil.move(meta, os.path.join(TRASH_DIR, os.path.basename(meta)))
    print(f"[AssembledRecipes] 已移入 trash: {name}")
    return web.json_response({"ok": True})


if PromptServer is not None:
    PromptServer.instance.routes.get("/assembled-recipes/list")(list_recipes)
    PromptServer.instance.routes.post("/assembled-recipes/delete")(delete_recipe)
