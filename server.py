"""HTTP routes: 配方列表 / 删除（硬删除，文件彻底移除）。"""

import json
import os

from aiohttp import web

from .nodes import RECIPES_DIR, recipe_files

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
    stem = os.path.splitext(src)[0]
    os.remove(src)
    for p in (stem + ".json", stem + ".preview.jpg", stem + ".preview.png"):
        if os.path.exists(p):
            os.remove(p)
    print(f"[AssembledRecipes] 已彻底删除: {name}")
    return web.json_response({"ok": True})


async def preview(request):
    name = os.path.basename(request.match_info.get("name", ""))
    name = os.path.splitext(name)[0]
    for ext in (".preview.jpg", ".preview.png"):
        p = os.path.join(RECIPES_DIR, name + ext)
        if os.path.exists(p):
            return web.FileResponse(p)
    return web.Response(status=404)


async def diag(request):
    """前端诊断上报：浏览器把状态 POST 过来，落到服务端日志。"""
    try:
        data = await request.json()
    except Exception:
        data = {"raw": await request.text()}
    print(f"[AssembledRecipes][DIAG] {json.dumps(data, ensure_ascii=False)[:600]}", flush=True)
    return web.json_response({"ok": True})


def register_routes():
    if PromptServer is None or not hasattr(PromptServer, "instance") or PromptServer.instance is None:
        return False
    PromptServer.instance.routes.get("/assembled-recipes/list")(list_recipes)
    PromptServer.instance.routes.post("/assembled-recipes/delete")(delete_recipe)
    PromptServer.instance.routes.get("/assembled-recipes/preview/{name}")(preview)
    PromptServer.instance.routes.post("/assembled-recipes/diag")(diag)
    return True


if not register_routes():
    print("[AssembledRecipes] PromptServer 未就绪，路由稍后注册")
