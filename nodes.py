"""Assembled model recipe cache for ComfyUI.

拼装一次，落盘成配方；下次直读配方，跳过 LoRA 拼装。
- RecipeSave (AssembledRecipes): 生成结束后把「已拼装权重」快照存盘（同名已存在则跳过）
- RecipeLoad (AssembledRecipes): 直读配方文件输出 MODEL（零拼装），可选逐层流水
- StreamToggle (AssembledRecipes): MODEL 直通，可选开启逐层流水（省显存、防换页）
删除 = 移入 trash/ 子目录（软删除，可手动找回）。
逐层流水原理：把 patcher 的 partially_load 的驻留预算钳到 ~0 字节，
权重逐 block 按需上卡算完即走——显存峰值 = 单块 + 激活，永不爆表。
"""

import json
import os
import re
import sys
import time
import types

import torch
import folder_paths
import comfy.sd
from comfy import model_management

RECIPES_DIR = os.path.join(folder_paths.models_dir, "assembled_recipes")
TRASH_DIR = os.path.join(RECIPES_DIR, "trash")
os.makedirs(RECIPES_DIR, exist_ok=True)
os.makedirs(TRASH_DIR, exist_ok=True)

STREAM_RESIDENT_BYTES = 0.1  # 驻留预算≈0 → 全流水


def recipe_files():
    return sorted(
        f for f in os.listdir(RECIPES_DIR)
        if f.endswith(".safetensors") and os.path.isfile(os.path.join(RECIPES_DIR, f))
    )


def _sanitize(name: str) -> str:
    name = (name or "").strip()
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name or f"recipe_{int(time.time())}"


def enable_streaming(patcher):
    """实例级绑定：强制逐层流水（不动核心类，只包我们自己的实例）。"""
    orig = patcher.partially_load

    def partially_load(self_or_none=None, device=None, extra_memory=None, force_patch_weights=False, **kw):
        # 兼容绑定/未绑定调用
        dev = device if device is not None else getattr(patcher, "load_device", None)
        return orig(dev, STREAM_RESIDENT_BYTES, force_patch_weights=force_patch_weights)

    patcher.partially_load = types.MethodType(partially_load, patcher)
    patcher._assembled_streaming = True
    return patcher



PREVIEW_SUFFIXES = [
    ".preview.webp", ".preview.png", ".preview.jpg", ".preview.jpeg",
    ".webp", ".png", ".jpg", ".jpeg", ".example.0.jpeg",
]


def _parse_lora_info(text):
    """<lora:name:strength[:clip]> → [(name, strength)]"""
    return [(m.group(1), m.group(2))
            for m in re.finditer(r"<lora:([^:>]+):([0-9.]+)(?::([0-9.]+))?>", text or "")]


def _find_lora_preview(lora_name):
    try:
        roots = folder_paths.get_folder_paths("loras")
    except Exception:
        return None
    rel = lora_name.replace(chr(92), "/")
    for root in roots:
        base = os.path.join(root, rel)
        d = os.path.dirname(base)
        b = os.path.basename(base)
        for suf in PREVIEW_SUFFIXES:
            p = os.path.join(d, b + suf)
            if os.path.exists(p):
                return p
    return None


def _compose_lora_preview(lora_entries, height=300):
    """把组合里每个 LoRA 的预览图横排合成一张，图上写 LoRA 名。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    tiles = []
    for name, strength in lora_entries:
        p = _find_lora_preview(name)
        if not p:
            continue
        try:
            im = Image.open(p).convert("RGB")
        except Exception:
            continue
        w = max(1, int(im.width * height / max(1, im.height)))
        tiles.append((im.resize((w, height)), f"{os.path.basename(name)} :{strength}"))
    if not tiles:
        return None
    total_w = min(sum(t[0].width for t in tiles), 2400)
    canvas = Image.new("RGB", (max(total_w, 64), height + 26), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    font = None
    for fp in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf"):
        try:
            font = ImageFont.truetype(fp, 18)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    x = 0
    for im, label in tiles:
        if x + im.width > canvas.width:
            im = im.crop((0, 0, max(1, canvas.width - x), im.height))
        canvas.paste(im, (x, 0))
        draw.text((x + 6, height + 3), label, fill=(255, 255, 255), font=font)
        x += im.width
        if x >= canvas.width:
            break
    return canvas


def _save_thumb(img_obj, path, size=(320, 320)):
    try:
        im = img_obj.copy()
        im.thumbnail(size)
        im.save(path, quality=85)
        return True
    except Exception as e:
        print(f"[AssembledRecipes] 缩略图保存失败: {e}")
        return False

class RecipeSaveAssembled:
    """生成链尾部快照：把当前已拼装（含 LoRA 补丁）的权重存成配方。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "image": ("IMAGE",),
                "recipe_name": ("STRING", {"default": "my_recipe", "multiline": False}),
                "note": ("STRING", {"default": "", "multiline": True}),
            },
            "optional": {
                "lora_info": ("STRING", {"forceInput": True}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "snapshot"
    CATEGORY = "AssembledRecipes"

    def snapshot(self, model, image, recipe_name, note="", lora_info=None):
        name = _sanitize(recipe_name)
        path = os.path.join(RECIPES_DIR, name + ".safetensors")
        if os.path.exists(path):
            print(f"[AssembledRecipes] 配方已存在，跳过快照: {name}")
            return (image,)

        base = model.model if hasattr(model, "model") else model
        merged = {k.replace("diffusion_model.", "", 1): v.detach().to("cpu").contiguous()
                  for k, v in base.state_dict().items()}

        import safetensors.torch
        from safetensors import safe_open

        # 源文件路径（loaders 会把来源记在 cached_patcher_init）
        src_path = None
        ci = getattr(model, "cached_patcher_init", None)
        if ci and len(ci) > 1 and ci[1]:
            cand = ci[1][0]
            if isinstance(cand, str) and os.path.exists(cand):
                src_path = cand

        if src_path:
            # 全量镜像源文件（保留 comfy_quant / comfy_attention.config 等元数据张量），
            # 被 LoRA 改过的权重用合并值覆盖
            with safe_open(src_path, framework="pt") as f:
                out = {}
                for k in f.keys():
                    out[k] = merged[k] if k in merged else f.get_tensor(k)
                safetensors.torch.save_file(out, path, metadata=f.metadata())
            print(f"[AssembledRecipes] 配方(全量镜像)已保存: {name} "
                  f"({os.path.getsize(path):,} B, 源={os.path.basename(src_path)})")
        else:
            safetensors.torch.save_file(merged, path)
            print(f"[AssembledRecipes] 配方(纯权重,无源文件)已保存: {name} ({os.path.getsize(path):,} B)")

        meta = {
            "name": name,
            "note": note or "",
            "lora_info": lora_info or "",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tensors": len(merged),
            "bytes": os.path.getsize(path),
            "source": os.path.basename(src_path) if src_path else "",
        }
        with open(os.path.join(RECIPES_DIR, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)

        # 封面：优先 LoRA 预览合成图（图上写 LoRA 名），否则退回生成图缩略
        preview_path = os.path.join(RECIPES_DIR, name + ".preview.jpg")
        entries = _parse_lora_info(lora_info or "")
        composed = _compose_lora_preview(entries) if entries else None
        if composed is not None:
            _save_thumb(composed, preview_path, size=(720, 720))
            print(f"[AssembledRecipes] 封面=LoRA 合成图 ({len(entries)} 个)")
        else:
            try:
                from PIL import Image
                import numpy as np
                arr = image[0].detach().to("cpu").numpy()
                arr = (np.clip(arr, 0, 1) * 255).astype("uint8")
                _save_thumb(Image.fromarray(arr), preview_path)
                print("[AssembledRecipes] 封面=生成图缩略")
            except Exception as e:
                print(f"[AssembledRecipes] 封面生成失败(不影响配方): {e}")
        return (image,)


class RecipeLoadAssembled:
    """直读已拼装配方输出 MODEL（零 LoRA 拼装），可选逐层流水。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "recipe": (recipe_files(),),
                "streaming": ("BOOLEAN", {"default": False, "tooltip": "逐层流水：显存峰值极小，稍慢；显存紧张/爆表时开"}),
            },
            "optional": {
                "recipe_name": ("STRING", {"forceInput": True, "tooltip": "画廊节点点选的名字会覆盖上面的下拉"}),
            },
        }

    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "load"
    CATEGORY = "AssembledRecipes"

    @classmethod
    def IS_CHANGED(cls, recipe, streaming):
        p = os.path.join(RECIPES_DIR, recipe)
        return f"{os.path.getmtime(p) if os.path.exists(p) else 0}:{streaming}"

    def load(self, recipe, streaming=False, recipe_name=None):
        if recipe_name:
            recipe = recipe_name if recipe_name.endswith(".safetensors") else recipe_name + ".safetensors"
        path = os.path.join(RECIPES_DIR, recipe)
        try:
            patcher = comfy.sd.load_diffusion_model(path)
        except Exception:
            # 拆分 qkv / 量化配方走 moss-image 插件加载器（与来源模型同款）
            import importlib.util
            moss = os.path.join(folder_paths.base_path, "custom_nodes", "moss-image", "loaders.py")
            if not os.path.exists(moss):
                raise
            spec = importlib.util.spec_from_file_location("moss_image_loaders", moss)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["moss_image_loaders"] = mod
            spec.loader.exec_module(mod)
            patcher = mod.load_model(path, "default")
        if streaming:
            patcher = enable_streaming(patcher)
        print(f"[AssembledRecipes] 配方直读: {recipe} | 逐层流水={streaming}")
        return (patcher,)


class StreamToggle:
    """MODEL 直通 + 逐层流水开关（给正常 LoRA 链路用）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "streaming": ("BOOLEAN", {"default": False, "tooltip": "逐层流水：显存峰值极小，稍慢；显存紧张/爆表时开"}),
            },
        }

    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "apply"
    CATEGORY = "AssembledRecipes"

    def apply(self, model, streaming=False):
        if streaming:
            model = enable_streaming(model)
        print(f"[AssembledRecipes] 逐层流水={streaming}")
        return (model,)




class RecipeGallery:
    """画廊瀑布流选配方：点卡片直出 MODEL（跳过合并风暴）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "selected_recipe": ("STRING", {"default": ""}),
                "streaming": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("MODEL", "STRING")
    RETURN_NAMES = ("model", "recipe_name")
    FUNCTION = "load"
    CATEGORY = "AssembledRecipes"

    @classmethod
    def IS_CHANGED(cls, selected_recipe, streaming):
        return f"{selected_recipe}:{streaming}"

    def load(self, selected_recipe, streaming=False):
        recipe = selected_recipe if selected_recipe.endswith(".safetensors") else selected_recipe + ".safetensors"
        path = os.path.join(RECIPES_DIR, recipe)
        if not os.path.exists(path):
            raise FileNotFoundError(f"配方不存在: {recipe}（先跑一次带保存的图）")
        try:
            patcher = comfy.sd.load_diffusion_model(path)
        except Exception:
            import importlib.util
            moss = os.path.join(folder_paths.base_path, "custom_nodes", "moss-image", "loaders.py")
            if not os.path.exists(moss):
                raise
            spec = importlib.util.spec_from_file_location("moss_image_loaders", moss)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["moss_image_loaders"] = mod
            spec.loader.exec_module(mod)
            patcher = mod.load_model(path, "default")
        if streaming:
            patcher = enable_streaming(patcher)
        print(f"[AssembledRecipes] 画廊直读: {recipe} | 逐层流水={streaming}")
        return (patcher, recipe)

NODE_CLASS_MAPPINGS = {
    "RecipeSave (AssembledRecipes)": RecipeSaveAssembled,
    "RecipeLoad (AssembledRecipes)": RecipeLoadAssembled,
    "StreamToggle (AssembledRecipes)": StreamToggle,
    "RecipeGallery (AssembledRecipes)": RecipeGallery,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "RecipeSave (AssembledRecipes)": "配方快照·保存 (AssembledRecipes)",
    "RecipeLoad (AssembledRecipes)": "配方快照·直读 (AssembledRecipes)",
    "StreamToggle (AssembledRecipes)": "逐层流水开关 (AssembledRecipes)",
    "RecipeGallery (AssembledRecipes)": "配方画廊 (AssembledRecipes)",
}
