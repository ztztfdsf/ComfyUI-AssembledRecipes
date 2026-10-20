"""Assembled model recipe cache for ComfyUI.

拼装一次，落盘成配方；下次直读配方，跳过 LoRA 拼装。
- RecipeSave (AssembledRecipes): 生成结束后把「已拼装权重」快照存盘（同名已存在则跳过）
- RecipeLoad (AssembledRecipes): 直读配方文件输出 MODEL（零拼装），可选逐层流水
- StreamToggle (AssembledRecipes): MODEL 直通，可选开启逐层流水（省显存、防换页）
删除 = 移入 trash/ 子目录（软删除，可手动找回）。
逐层流水原理：把 patcher 的 partially_load 的驻留预算钳到 ~0 字节，
权重逐 block 按需上卡算完即走——显存峰值 = 单块 + 激活，永不爆表。
"""

import base64
import json
import os
import re
import socket
import subprocess
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


def _load_any(path):
    """优先原生加载；失败回退 moss-image 插件加载器（拆分 qkv 量化配方）。"""
    try:
        return comfy.sd.load_diffusion_model(path)
    except Exception:
        moss = os.path.join(folder_paths.base_path, "custom_nodes", "moss-image", "loaders.py")
        if not os.path.exists(moss):
            raise
        import importlib.util
        spec = importlib.util.spec_from_file_location("moss_image_loaders", moss)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["moss_image_loaders"] = mod
        spec.loader.exec_module(mod)
        return mod.load_model(path, "default")


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


def _fmt_strength(s):
    try:
        return f"{float(s):g}"
    except Exception:
        return str(s).strip()


def _parse_lora_info(text):
    """支持 <lora:name:strength[:clip]> 与 "name:strength, name2:0.8" 两种写法。"""
    text = (text or "").replace(chr(10), ",").replace(chr(13), ",")
    out = [(m.group(1), m.group(2))
           for m in re.finditer(r"<lora:([^:>]+):([0-9.]+)(?::([0-9.]+))?>", text)]
    if out:
        return out
    for tok in re.split("[,;]+", text):
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"^(.+?)[\s]*:[\s]*([0-9.]+)$", tok)
        if m:
            out.append((m.group(1).strip(), m.group(2)))
        else:
            out.append((tok, ""))
    return out


def _find_lora_preview(lora_name):
    """按 LoRA 名找预览图：先精确路径，再模糊匹配（名字只是简称时也能命中）。"""
    try:
        roots = folder_paths.get_folder_paths("loras")
    except Exception:
        return None
    rel = (lora_name or "").replace(chr(92), "/").strip()
    if not rel:
        return None
    stem = os.path.splitext(rel)[0]

    def first_preview(dir_path, base_name):
        for suf in PREVIEW_SUFFIXES:
            p = os.path.join(dir_path, base_name + suf)
            if os.path.exists(p):
                return p
        return None

    # ① 精确
    for root in roots:
        base = os.path.join(root, stem)
        p = first_preview(os.path.dirname(base), os.path.basename(base))
        if p:
            return p

    # ② 模糊：沿目录树找名字互相包含的 safetensors
    needle = os.path.basename(stem).lower()
    hits = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                low = fn.lower()
                if any(low.endswith(s) for s in PREVIEW_SUFFIXES):
                    continue
                if needle and needle in os.path.splitext(low)[0]:
                    hits.append((dirpath, os.path.splitext(fn)[0]))
            if len(hits) > 40:
                break
    for dir_path, base_name in hits:
        p = first_preview(dir_path, base_name)
        if p:
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
    DESCRIPTION = "生成结束后把已拼装(含 LoRA)的权重存成配方。名字留空=按 LoRA 组合自动命名。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    def snapshot(self, model, image, recipe_name, note="", lora_info=None):
        # 单/无 LoRA 没有融合成本，存 5.4G 配方纯浪费——直接跳过（lora_info 未接线时维持旧行为）
        if lora_info is not None:
            _active = _parse_lora_info(lora_info or "")
            if len(_active) < 2:
                print(f"[AssembledRecipes] 激活 LoRA {len(_active)} 个（<2 无融合成本），不存配方")
                return (image,)
        if not (recipe_name or "").strip():
            entries = _parse_lora_info(lora_info or "")
            if entries:
                recipe_name = "auto_" + "+".join(
                    _sanitize(n.split("/")[-1].replace(".safetensors", ""))
                    + ("@" + _fmt_strength(s) if s not in (None, "") else "")
                    for n, s in entries)
                print(f"[AssembledRecipes] 自动命名: {recipe_name}")
            else:
                print("[AssembledRecipes] 名字为空且无 LoRA 信息，跳过快照")
                return (image,)
        name = _sanitize(recipe_name)
        path = os.path.join(RECIPES_DIR, name + ".safetensors")
        if os.path.exists(path):
            print(f"[AssembledRecipes] 同名同配置已存过，跳过快照: {name}")
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
    DESCRIPTION = "直读配方(零 LoRA 拼装)。streaming=逐层流水，显存峰值≈单层。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

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
    DESCRIPTION = "MODEL 直通 + 逐层流水开关(权重逐层上卡，显存峰值≈单层)。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    def apply(self, model, streaming=False):
        if streaming:
            model = enable_streaming(model)
        print(f"[AssembledRecipes] 逐层流水={streaming}")
        return (model,)





class RecipeRouter:
    """按组合键路由：有配方→直读配方（不执行 model 分支）；没有→执行 model 分支（LoRA 拼装）并返回。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "recipe_key": ("STRING", {"default": "", "tooltip": "配方名/组合键；例：auto_Nyalia+QAQ。留空=强制走 model 分支"}),
                "streaming": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "model": ("MODEL", {"lazy": True, "tooltip": "兜底分支：只有配方不存在时才会执行（避免拼装费）"}),
            },
        }

    RETURN_TYPES = ("MODEL", "STRING")
    RETURN_NAMES = ("model", "used_recipe")
    FUNCTION = "route"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "填组合名=命中配方就直读、不执行 LoRA 分支(省拼装)；留空=走 LoRA 分支。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    @staticmethod
    def _resolve(recipe_key):
        key = (recipe_key or "").strip()
        if not key:
            return None
        fname = key if key.endswith(".safetensors") else key + ".safetensors"
        path = os.path.join(RECIPES_DIR, fname)
        return path if os.path.exists(path) else None

    def check_lazy_status(self, recipe_key="", streaming=False, model=None):
        if self._resolve(recipe_key):
            return []
        return ["model"]

    def route(self, recipe_key="", streaming=False, model=None):
        path = self._resolve(recipe_key)
        if path:
            patcher = _load_any(path)
            if streaming:
                patcher = enable_streaming(patcher)
            print(f"[AssembledRecipes] 路由命中配方（跳过拼装）: {os.path.basename(path)}")
            return (patcher, os.path.basename(path))
        if model is None:
            raise ValueError("【AssembledRecipes】配方不存在，且兜底 model 输入是空的——请把 LoRA 输出接到本节点的 model 输入")
        print("[AssembledRecipes] 路由未命中配方 → 走 LoRA 分支")
        return (model, "")

class RecipeGallery:
    """画廊点选配方；没选就走惰性兜底 model（不选=零额外开销）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "selected_recipe": ("STRING", {"default": "", "tooltip": "画布上点卡片自动填；留空=走兜底 model(正常 LoRA 链路)"}),
                "streaming": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "model": ("MODEL", {"lazy": True, "tooltip": "兜底分支：只有没选配方时才会执行"}),
            },
        }

    RETURN_TYPES = ("MODEL", "STRING")
    RETURN_NAMES = ("model", "used_recipe")
    FUNCTION = "load"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "画布瀑布流点选配方直读(跳过拼装)；不选则走兜底 model 分支。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    @staticmethod
    def _resolve(selected_recipe):
        key = (selected_recipe or "").strip()
        if not key:
            return None
        fname = key if key.endswith(".safetensors") else key + ".safetensors"
        path = os.path.join(RECIPES_DIR, fname)
        return path if os.path.exists(path) else None

    def check_lazy_status(self, selected_recipe="", streaming=False, model=None):
        return [] if self._resolve(selected_recipe) else ["model"]

    def load(self, selected_recipe="", streaming=False, model=None):
        path = self._resolve(selected_recipe)
        if path:
            patcher = _load_any(path)
            if streaming:
                patcher = enable_streaming(patcher)
            print(f"[AssembledRecipes] 画廊直读配方（跳过拼装）: {os.path.basename(path)}")
            return (patcher, os.path.basename(path))
        if model is None:
            raise ValueError("【AssembledRecipes】没选配方，且兜底 model 输入是空的——请把 LoRA 输出接到本节点的 model 输入")
        print("[AssembledRecipes] 画廊未选配方 → 走兜底 model 分支")
        return (model, "")



class DeepClean:
    """深度清理：卸载模型权重到内存 + 强制把显存缓存池还给驱动。

    比普通 empty_cache 更彻底：unload → gc → synchronize → empty_cache → ipc_collect → 再 empty。
    放在跑图链开头（模型加载之前或之后均可，之后=连刚加载的也一并腾出，采样时按需重载）。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "unload_models": ("BOOLEAN", {"default": True, "tooltip": "把已加载模型权重卸回内存(跑图时按需重载)"}),
                "force_empty_cache": ("BOOLEAN", {"default": True, "tooltip": "强制把显存缓存池还给驱动(不占内存,下次重新申请)"}),
            },
            "optional": {
                "model_pass": ("MODEL", {"lazy": True}),
                "image_pass": ("IMAGE", {"lazy": True}),
                "any_input": ("*", {"lazy": True}),
            },
        }

    RETURN_TYPES = ("MODEL", "IMAGE", "STRING")
    RETURN_NAMES = ("model", "image", "report")
    FUNCTION = "clean"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "深度清理：模型卸回内存 + 显存缓存池强制还给驱动。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    def check_lazy_status(self, **kwargs):
        return [k for k in ("model_pass", "image_pass", "any_input") if k in kwargs]

    @staticmethod
    def _evict_staged(keep_modules=()):
        """驱逐 aimdo 的暂存权重镜像（ModelVBAR 驻留页），不动保留模型。"""
        try:
            import gc
            import comfy_aimdo.model_vbar as mv
        except Exception as e:
            return 0, 0, f"无 aimdo，跳过 ({e})"
        keep = set()
        for m in keep_modules:
            for v in (getattr(m, "dynamic_vbars", None) or {}).values():
                keep.add(id(v))
        n = freed = 0
        seen = 0
        try:
            objs = gc.get_objects()
        except Exception:
            objs = []
        for obj in objs:
            try:
                if isinstance(obj, mv.ModelVBAR):
                    seen += 1
                    if id(obj) not in keep:
                        freed += int(obj.free_memory(2 ** 62) or 0)
                        obj.set_watermark(0)
                        n += 1
            except Exception:
                pass
        try:
            total = int(mv.vbars_analyze() or 0)
        except Exception:
            total = -1
        return n, freed, f"vbar 总数 {seen} 保留 {len(keep)} 剩余驻留 {total // 2**20}MB"

    def clean(self, unload_models=True, force_empty_cache=True, model_pass=None, image_pass=None, any_input=None):
        def free_mb():
            try:
                return model_management.get_free_memory() / (1024 ** 2)
            except Exception:
                return -1.0

        before = free_mb()
        if unload_models:
            try:
                model_management.unload_all_models()
            except Exception as e:
                print(f"[AssembledRecipes] unload 失败: {e}")
        import gc
        gc.collect()
        if force_empty_cache:
            try:
                torch.cuda.synchronize()
            except Exception:
                pass
            for _ in range(3):
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    break
                try:
                    torch.cuda.ipc_collect()
                except Exception:
                    pass
                gc.collect()
        # 驱逐 aimdo 暂存镜像（只保护仍加载的主模型，如 DiT）
        keep_mods = []
        try:
            for lm in list(model_management.current_loaded_models):
                mod = getattr(lm, 'real_model', None) or getattr(getattr(lm, 'model', None), 'model', None)
                if mod is not None:
                    keep_mods.append(mod)
        except Exception:
            pass
        n_v, freed_v, note_v = self._evict_staged(keep_mods)
        print(f"[AssembledRecipes] {note_v}", flush=True)
        after = free_mb()
        report = f"free {before:.0f}MB -> {after:.0f}MB (freed {after - before:.0f}MB)"
        print(f"[AssembledRecipes] 深度清理: {report}", flush=True)
        return (model_pass, image_pass, report)


class FreeVram:
    """把非主模型（文本编码器等）卸回内存，只留要采样的模型。

    放在「编码完成后、采样之前」：会等所有 conditioning 就绪再动手，
    然后把除 model 输入之外的已加载模型全部 offload 到内存，并清显存缓存。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"model": ("MODEL",)},
            "optional": {
                "conditioning_1": ("CONDITIONING", {"tooltip": "接正/负条件（用于保证本节点在编码之后执行）"}),
                "conditioning_2": ("CONDITIONING",),
                "conditioning_3": ("CONDITIONING",),
                "image_pass": ("IMAGE", {"lazy": True, "tooltip": "可选：只用来保证本节点在出图之后执行（收工位用）"}),
            },
        }

    RETURN_TYPES = ("MODEL", "STRING", "IMAGE")
    RETURN_NAMES = ("model", "report", "image")
    FUNCTION = "free"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "采样前把文本编码器等非主模型卸回内存（省 5G+ 显存），只留要采样的模型。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    def check_lazy_status(self, model=None, **kwargs):
        return [k for k in ("conditioning_1", "conditioning_2", "conditioning_3", "image_pass") if k in kwargs]

    def free(self, model, conditioning_1=None, conditioning_2=None, conditioning_3=None, image_pass=None):
        # MODEL 输入本身就是 ModelPatcher；current_loaded_models 里存的也是 patcher
        keep_patcher = model
        before = model_management.get_free_memory()
        keep = []
        for lm in list(model_management.current_loaded_models):
            m = lm.model
            if m is None:
                continue
            same = m is keep_patcher
            if not same and hasattr(m, "is_clone"):
                try:
                    same = m.is_clone(keep_patcher)
                except Exception:
                    same = False
            if same:
                keep.append(lm)
        names = [type(getattr(lm.model, "model", lm.model)).__name__
                 for lm in model_management.current_loaded_models if lm not in keep]
        print(f"[AssembledRecipes] 已加载: {[type(getattr(lm.model,'model',lm.model)).__name__ for lm in model_management.current_loaded_models]} | 保留 {len(keep)} 个", flush=True)
        try:
            model_management.free_memory(1e30, model_management.get_torch_device(), keep_loaded=keep)
        except Exception as e:
            print(f"[AssembledRecipes] free_memory 失败，退回卸载全部: {e}", flush=True)
            model_management.unload_all_models()
        model_management.soft_empty_cache()
        after = model_management.get_free_memory()
        report = (f"卸回内存: {', '.join(names) if names else '无'} | "
                  f"空闲 {before / 2**20:.0f}MB -> {after / 2**20:.0f}MB")
        print(f"[AssembledRecipes] {report}", flush=True)
        return (model, report, image_pass)


class VramModeFix:
    """纠正启动参数造成的显存策略（纯节点，不改启动器/命令行）。

    `--highvram` 会把 vram_state 钉成 HIGH_VRAM 并关掉动态显存，导致模型全部驻留、
    一采样就 OOM；本节点在运行时把策略纠正回 NORMAL_VRAM + 智能内存，效果等价于
    去掉 `--highvram`，但完全靠图内节点完成。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {"model": ("MODEL", {"tooltip": "接在加载器后面；输出原样透传"})},
        }

    RETURN_TYPES = ("MODEL", "STRING")
    RETURN_NAMES = ("model", "report")
    FUNCTION = "fix"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "运行时纠正 --highvram 造成的全驻留策略（不改启动参数）。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    def fix(self, model=None):
        mm = model_management
        changes = []
        try:
            if mm.vram_state == mm.VRAMState.HIGH_VRAM:
                mm.vram_state = mm.VRAMState.NORMAL_VRAM
                changes.append("HIGH_VRAM → NORMAL_VRAM")
        except Exception as e:
            print(f"[AssembledRecipes] 切换 vram_state 失败: {e}", flush=True)
        try:
            if mm.DISABLE_SMART_MEMORY:
                mm.DISABLE_SMART_MEMORY = False
                changes.append("智能内存: 关 → 开")
        except Exception:
            pass
        mm.soft_empty_cache()
        free = mm.get_free_memory() / 2**20
        report = ("显存策略: " + ("; ".join(changes) if changes else "本来就是动态模式，无需纠正")
                  + f" | 空闲 {free:.0f}MB")
        print(f"[AssembledRecipes] {report}", flush=True)
        return (model, report)

class ReleasePhantomVram:
    '''释放「虚空显存」：torch 缓存池预留未用部分 + 可丢弃的补丁备份。

    收工位用。drop_patch_backups=True 时把 LoRA 合并留下的原权重备份丢掉（约等于模型体积）；
    前提：下次换组合/重跑会从干净基座重新打补丁（本工作流满足）。若同一 patcher 要回滚补丁，不要开。
    '''

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "drop_patch_backups": ("BOOLEAN", {"default": True, "tooltip": "丢掉 LoRA 合并的原权重备份（可腾出接近模型体积的显存）；会失去对当前补丁的回滚能力"}),
            },
            "optional": {
                "image_pass": ("IMAGE", {"lazy": True}),
                "any_input": ("*", {"lazy": True}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "report")
    FUNCTION = "release"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "释放虚空显存：清 torch 池预留 + 可选丢弃补丁备份。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    @staticmethod
    def _torch_stats():
        try:
            import torch
            st = torch.cuda.memory_stats()
            return st.get('reserved_bytes.all.current', 0), st.get('active_bytes.all.current', 0)
        except Exception:
            return 0, 0

    def check_lazy_status(self, **kwargs):
        return [k for k in ("image_pass", "any_input") if k in kwargs]

    def release(self, drop_patch_backups=True, image_pass=None, any_input=None):
        import gc
        import torch
        res0, act0 = self._torch_stats()
        free0 = model_management.get_free_memory() / 2 ** 20

        dropped = drop_bytes = 0
        if drop_patch_backups:
            seen = set()
            candidates = []
            try:
                import comfy.model_patcher as mp
                for obj in gc.get_objects():
                    try:
                        if isinstance(obj, mp.ModelPatcher) and id(obj) not in seen:
                            seen.add(id(obj))
                            candidates.append(obj)
                    except Exception:
                        pass
            except Exception:
                pass
            for p in candidates:
                try:
                    for attr in ('backup', 'backup_buffers', 'hook_backup', 'object_patches_backup'):
                        d = getattr(p, attr, None)
                        if isinstance(d, dict) and d:
                            for v in list(d.values()):
                                try:
                                    w = getattr(v, 'weight', v)
                                    if torch.is_tensor(w): drop_bytes += w.nelement() * w.element_size()
                                except Exception:
                                    pass
                            d.clear()
                            dropped += 1
                except Exception:
                    pass

        for _ in range(3):
            try:
                model_management.soft_empty_cache()
            except Exception:
                pass
            try:
                torch.cuda.synchronize()
            except Exception:
                pass
        try:
            torch.cuda.ipc_collect()
        except Exception:
            pass
        gc.collect()

        res1, act1 = self._torch_stats()
        free1 = model_management.get_free_memory() / 2 ** 20
        report = (f"池预留 {res0/2**30:.2f}G->{res1/2**30:.2f}G | 活跃 {act0/2**30:.2f}G->{act1/2**30:.2f}G | "
                  f"丢弃备份 {dropped} 份≈{drop_bytes//2**20}MB | 驱动空闲 {free0:.0f}->{free1:.0f}MB")
        print(f"[AssembledRecipes] 释放虚空: {report}", flush=True)
        return (image_pass, report)


def _ollama_host_port(service_url):
    from urllib.parse import urlparse
    raw = service_url if "://" in service_url else "http://" + service_url
    p = urlparse(raw)
    return p.hostname or "127.0.0.1", p.port or 11434


def _port_alive(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _ensure_ollama_service(service_url, wait_seconds=45):
    """确保 Ollama 服务在线；不在则从 ComfyUI/ollama/ 自动拉起。返回 (host, port) 或 None。"""
    host, port = _ollama_host_port(service_url)
    if _port_alive(host, port, 1.5):
        return host, port
    base = folder_paths.base_path
    exe = os.path.join(base, "ollama", "ollama.exe")
    if not os.path.isfile(exe):
        print(f"[AssembledRecipes] 反推服务未启动，且找不到 {exe}——请把 ollama.exe+lib 放进 ComfyUI/ollama/", flush=True)
        return None
    env = os.environ.copy()
    env["OLLAMA_HOST"] = f"{host}:{port}"
    env["OLLAMA_MODELS"] = os.path.join(base, "models", "LLM")
    try:
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen([exe, "serve"], env=env, creationflags=flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, close_fds=True)
        print(f"[AssembledRecipes] 反推服务自启: 已拉起 ollama (pid {proc.pid}) | 模型库 {env['OLLAMA_MODELS']}，等待就绪...", flush=True)
    except Exception as e:
        print(f"[AssembledRecipes] 拉起 ollama 失败: {e}", flush=True)
        return None
    t0 = time.time()
    while time.time() - t0 < wait_seconds:
        if _port_alive(host, port, 1.0):
            print(f"[AssembledRecipes] 反推服务就绪: {host}:{port} ({time.time() - t0:.1f}s)", flush=True)
            return host, port
        time.sleep(0.5)
    print(f"[AssembledRecipes] 反推服务 {wait_seconds}s 内未就绪", flush=True)
    return None


class ReverseServiceAutostart:
    """反推服务（Ollama）自动启动。

    机器重启后 Ollama 服务会消失（症状：反推节点报 "All connection attempts failed"）。
    本节点接在反推图源与反推节点之间，image 原样透传，仅负责保证反推执行前服务在线：
    - 先探测服务地址，活着就直接放行（零开销）；
    - 不活着则从 ComfyUI/ollama/ollama.exe 启动（模型库自动指向 ComfyUI/models/LLM），
      轮询等待就绪，上限 wait_seconds 秒。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "透传；接在反推图源切换与反推节点之间"}),
                "service_url": ("STRING", {"default": "http://127.0.0.1:11434", "tooltip": "Ollama 服务地址"}),
                "wait_seconds": ("INT", {"default": 45, "min": 5, "max": 300, "tooltip": "等待服务就绪的上限秒数"}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "ensure"
    CATEGORY = "AssembledRecipes"

    @classmethod
    def IS_CHANGED(cls, image, service_url, wait_seconds):
        # 服务状态是外部世界，不随图内输入变化——返回 NaN 禁用执行缓存，每次都真检
        return float("NaN")

    DESCRIPTION = "反推前自动拉起 Ollama 服务（exe 放 ComfyUI/ollama/，模型库 ComfyUI/models/LLM）。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    @staticmethod
    def _alive(host, port, timeout=1.0):
        return _port_alive(host, port, timeout)

    def ensure(self, image, service_url, wait_seconds):
        host, port = _ollama_host_port(service_url)
        if _port_alive(host, port, 1.5):
            print(f"[AssembledRecipes] 反推服务在线: {host}:{port}", flush=True)
            return (image,)
        hp = _ensure_ollama_service(service_url, wait_seconds)
        if hp is None and not _port_alive(host, port, 1.0):
            print("[AssembledRecipes] 反推服务不可用，放行（反推节点会自行报错）", flush=True)
        return (image,)


class ReverseJsonSanitize:
    """清洗反推节点吐出的布局 JSON，喂给 CreateBoundingBoxes 的 bboxes 口。

    反推模型偶尔产出病 JSON：字符串里夹裸换行、bbox 写成字符串、输出被上下文截断等，
    核心的 CreateBoundingBoxes 会直接抛 "not valid JSON" 炸整张图。本节点容错修复：
    1) 剖掉 markdown 围栏与前后废话，定位 JSON 数组主体；
    2) 修复字符串字面量里的裸控制字符（换行/回车/制表转义）；
    3) 截断则回退到最后一个完整元素（字符串感知扫描）再闭合；
    4) 逐元素校验：bbox 统一转 4 个 0~1000 整数（字符串形式自动拆），非法元素丢弃；
    5) 全军覆没时输出 []（退回普通文生图，不炸链路）。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {"tooltip": "接反推节点的描述输出"})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("json",)
    FUNCTION = "sanitize"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "修复反推输出的病 JSON（裸换行/字符串 bbox/截断），喂给边界框画布。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    @staticmethod
    def _fix_raw_control(s):
        out = []
        in_str = esc = False
        for ch in s:
            if in_str:
                if esc:
                    out.append(ch)
                    esc = False
                elif ch == "\\":
                    out.append(ch)
                    esc = True
                elif ch == '"':
                    out.append(ch)
                    in_str = False
                elif ch == "\n":
                    out.append("\\n")
                elif ch == "\r":
                    out.append("\\r")
                elif ch == "\t":
                    out.append("\\t")
                elif ord(ch) < 0x20:
                    out.append("\\u%04x" % ord(ch))
                else:
                    out.append(ch)
            else:
                if ch == '"':
                    in_str = True
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _truncate_to_complete(s):
        """字符串感知地找最后一个完整元素的闭括号，截到那里再闭合数组。"""
        cut = -1
        in_str = esc = False
        depth = 0
        for idx, ch in enumerate(s):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "[{":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 1 and ch == "}":
                    cut = idx
        if cut < 0:
            return None
        return s[:cut + 1] + "]"

    @staticmethod
    def _clean_bbox(b):
        if isinstance(b, str):
            b = [b]
        if not isinstance(b, (list, tuple)):
            return None, False
        if len(b) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in b):
            nums = [float(v) for v in b]
            fixed = False
        else:
            joined = " ".join(str(v) for v in b)
            nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", joined)][:4]
            fixed = True
        if len(nums) != 4:
            return None, fixed
        return [int(round(max(0.0, min(1000.0, n)))) for n in nums], fixed

    def sanitize(self, text):
        report = []
        raw = text or ""
        s = raw.strip()
        if "```" in s:
            for part in s.split("```"):
                p = part.strip()
                if p[:4].lower() == "json":
                    p = p[4:].strip()
                if p.startswith("["):
                    s = p
                    break
        i = s.find("[")
        data = None
        if i >= 0:
            j = s.rfind("]")
            candidates = []
            if j > i:
                candidates.append(s[i:j + 1])
            if not s[i:].rstrip().endswith("]"):
                candidates.append(s[i:])  # 截断场景：rfind 切到内括号时用全文尾重试
            for arr in candidates:
                if data is not None:
                    break
                try:
                    data = json.loads(arr)
                    break
                except Exception:
                    pass
                fixed = self._fix_raw_control(arr)
                try:
                    data = json.loads(fixed)
                    report.append("修复字符串内裸控制字符")
                    break
                except Exception:
                    pass
                trunc = self._truncate_to_complete(fixed)
                if trunc:
                    try:
                        data = json.loads(trunc)
                        report.append("截断回退到最后完整元素")
                        break
                    except Exception:
                        pass
        out, dropped, bbox_fixed = [], 0, 0
        if isinstance(data, list):
            for e in data:
                if not isinstance(e, dict):
                    dropped += 1
                    continue
                bbox, fixed = self._clean_bbox(e.get("bbox"))
                if bbox is None:
                    dropped += 1
                    continue
                if fixed:
                    bbox_fixed += 1
                etype = "text" if str(e.get("type", "")).strip().lower() == "text" else "obj"
                desc = str(e.get("desc", "")).strip() or "unknown"
                item = {"type": etype, "bbox": bbox, "desc": desc}
                if etype == "text":
                    txt = str(e.get("text", "")).strip()
                    if not txt:
                        dropped += 1
                        continue
                    item["text"] = txt
                out.append(item)
        elif data is None:
            report.append("未解析出布局，输出空布局")
        msg = f"保留 {len(out)} 个框"
        if dropped:
            msg += f"，丢弃 {dropped} 个坏元素"
        if bbox_fixed:
            msg += f"，修复 {bbox_fixed} 个字符串 bbox"
        report.append(msg)
        result = json.dumps(out, ensure_ascii=False)
        print(f"[AssembledRecipes] 反推JSON清洗: {'; '.join(report)} | 输出 {len(result)} 字符", flush=True)
        return (result,)


_LAYOUT_RULE_DEFAULT = (
    "你是图像布局反推器。识别图中所有主要对象、人物和文字块——每个独立可见的实体（人物、动物、物体、背景中的显著主体、文字块）都必须有自己的框，不要合并，也不要遗漏次要对象。"
    "只输出一个 JSON 数组，不要输出任何其他文字或 markdown 标记。每个元素格式：{\"type\":\"obj\",\"bbox\":[ymin,xmin,ymax,xmax],\"desc\":\"英文自然语言提示词\"}；"
    "画面中的文字用 {\"type\":\"text\",\"bbox\":[ymin,xmin,ymax,xmax],\"text\":\"文字原文\",\"desc\":\"英文自然语言说明\"}。"
    "desc 一律使用英文自然语言提示词，写得详细完整：像写给画师的一段英文描述句，包含主体外观、发型发色、服装与材质、颜色、姿势动作、表情、朝向、"
    "与镜头的距离（close-up / upper body / half body / full body）等所有可见信息，每条 desc 至少 15~40 个英文单词，宁可详细不可简略；"
    "不要用中文、不要用标签堆砌；唯独 text 字段保留画面文字的原文原样（含大小写与语言）。"
    "若用户消息中附带了画廊提示词（角色名/风格标签），遇到不认识的角色或概念时，用它来补全 desc 中的角色名与细节。"
    "坐标是 0~1000 的归一化值，顺序为 [上,左,下,右]（yxyx）。bbox 必须是 4 个纯数字组成的数组，如 [12,34,56,78]，禁止加引号或合并成字符串。所有框按从背景到前景排列。")


class ReverseLayoutCaption:
    """MOSS 布局反推（自研 Ollama 直连，上下文拉满）。

    甩开第三方反推节点的小窗口（它给单图只预留 512 token 输出，复杂图必截断）：
    - 直连 Ollama /api/chat，num_ctx 按 /api/show 查询的模型上限（qwen3-vl 为 262144）拉满，
    - 由 max_ctx 封顶（默认 32768，显存不够自动对半降级重试：32768→16384→8192→4096）；
    - 反推完 keep_alive=0 立即释放显存；
    - 输出坏 JSON 由下游「配方·反推JSON清洗」兑底。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要反推的图"}),
                "rule": ("STRING", {"multiline": True, "default": _LAYOUT_RULE_DEFAULT, "tooltip": "反推规则（system 提示词）"}),
                "user_prompt": ("STRING", {"multiline": True, "default": "", "tooltip": "画廊提示词等角色参考资料，遇到不认识的角色用它补全"}),
                "model": ("STRING", {"default": "huihui_ai/qwen3-vl-abliterated:4b-instruct-q4_K_M", "tooltip": "Ollama 模型名"}),
                "service_url": ("STRING", {"default": "http://127.0.0.1:11434", "tooltip": "Ollama 服务地址"}),
                "max_ctx": ("INT", {"default": 32768, "min": 4096, "max": 262144, "tooltip": "上下文封顶；实际取 min(模型上限, 此值)，失败自动对半降级"}),
                "auto_unload": ("BOOLEAN", {"default": True, "tooltip": "反推完立即释放 Ollama 占用的显存"}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "caption"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "自研布局反推：Ollama 直连、上下文拉满、显存自动降级。下游接「配方·反推JSON清洗」。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # VLM 是随机的：同一张图重跑也要重新反推，禁用执行缓存
        return float("NaN")

    @staticmethod
    def _image_to_b64(image):
        import io
        from PIL import Image
        t = image[0].clamp(0, 1).cpu().numpy()
        arr = (t * 255.0).round().astype("uint8")
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode(), arr.shape[1], arr.shape[0]

    @staticmethod
    def _looks_like_resource_error(msg, body):
        blob = (msg + " " + body).lower()
        return any(k in blob for k in ("memory", "out of", "oom", "alloc", "not enough", "kv", "llama-server", "cuda"))

    def caption(self, image, rule, user_prompt, model, service_url, max_ctx, auto_unload):
        import urllib.error as _ue
        import urllib.request as _ur
        hp = _ensure_ollama_service(service_url, 45)
        if hp is None:
            raise RuntimeError(f"Ollama 服务不可用（{service_url}）且无法自动拉起——请把 ollama.exe+lib 放进 ComfyUI/ollama/")
        base = f"http://{hp[0]}:{hp[1]}"

        num_ctx = int(max_ctx)
        try:
            rq = _ur.Request(base + "/api/show", data=json.dumps({"model": model}).encode(),
                             headers={"Content-Type": "application/json"})
            info = json.load(_ur.urlopen(rq, timeout=15)).get("model_info", {})
            lens = [int(v) for k, v in info.items() if "context_length" in k.lower() and isinstance(v, (int, float))]
            if lens:
                num_ctx = min(num_ctx, max(lens))
        except Exception as e:
            print(f"[AssembledRecipes] 布局反推: 查询模型上下文上限失败（按 {num_ctx} 用）: {e}", flush=True)

        b64, iw, ih = self._image_to_b64(image)
        if image.shape[0] > 1:
            print(f"[AssembledRecipes] 布局反推: 批量 {image.shape[0]} 张，只取第一张", flush=True)
        user_content = ((user_prompt or "").strip() + "\n\n" if (user_prompt or "").strip() else "") \
            + "Analyze the image and output ONLY the JSON array as instructed."

        t0 = time.time()
        last_err = None
        for attempt in range(4):
            payload = {
                "model": model, "stream": False,
                "keep_alive": "0" if auto_unload else "5m",
                "messages": [
                    {"role": "system", "content": rule},
                    {"role": "user", "content": user_content, "images": [b64]},
                ],
                "options": {"num_ctx": num_ctx, "temperature": 0.2},
            }
            try:
                rq = _ur.Request(base + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
                resp = json.load(_ur.urlopen(rq, timeout=300))
                if isinstance(resp, dict) and resp.get("error"):
                    raise RuntimeError(str(resp["error"]))
                text_out = (resp.get("message") or {}).get("content", "")
                warn = " | ⚠ 输出触顶(done=length)，尾部交给清洗节点兜底" if resp.get("done_reason") == "length" else ""
                print(f"[AssembledRecipes] 布局反推: {iw}x{ih} | num_ctx={num_ctx} | "
                      f"提示词 {resp.get('prompt_eval_count', '?')} tok + 输出 {resp.get('eval_count', '?')} tok | "
                      f"文本 {len(text_out)} 字符 | 耗时 {time.time() - t0:.1f}s{warn}", flush=True)
                return (text_out,)
            except Exception as e:
                last_err = e
                body = ""
                try:
                    body = e.read().decode()[:300]
                except Exception:
                    pass
                print(f"[AssembledRecipes] 布局反推: num_ctx={num_ctx} 失败: {e} {body}", flush=True)
                if num_ctx > 4096 and self._looks_like_resource_error(str(e), body):
                    num_ctx = max(4096, num_ctx // 2)
                    print(f"[AssembledRecipes] 布局反推: 疑似显存不够，降级重试 num_ctx={num_ctx}", flush=True)
                    continue
                break
        raise RuntimeError(f"布局反推失败（最后一次 num_ctx={num_ctx}）: {last_err}")


NODE_CLASS_MAPPINGS = {
    "RecipeSave (AssembledRecipes)": RecipeSaveAssembled,
    "RecipeLoad (AssembledRecipes)": RecipeLoadAssembled,
    "StreamToggle (AssembledRecipes)": StreamToggle,
    "RecipeGallery (AssembledRecipes)": RecipeGallery,
    "RecipeRouter (AssembledRecipes)": RecipeRouter,
    "DeepClean (AssembledRecipes)": DeepClean,
    "FreeVram (AssembledRecipes)": FreeVram,
    "VramModeFix (AssembledRecipes)": VramModeFix,
    "ReleasePhantomVram (AssembledRecipes)": ReleasePhantomVram,
    "ReverseServiceAutostart (AssembledRecipes)": ReverseServiceAutostart,
    "ReverseJsonSanitize (AssembledRecipes)": ReverseJsonSanitize,
    "ReverseLayoutCaption (AssembledRecipes)": ReverseLayoutCaption,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "RecipeSave (AssembledRecipes)": "配方保存",
    "RecipeLoad (AssembledRecipes)": "配方·直读加载",
    "RecipeGallery (AssembledRecipes)": "配方·画廊选择",
    "RecipeRouter (AssembledRecipes)": "配方·惰性路由",
    "StreamToggle (AssembledRecipes)": "配方·逐层流水开关",
    "DeepClean (AssembledRecipes)": "配方·深度清理",
    "FreeVram (AssembledRecipes)": "配方·踢编码器回内存",
    "VramModeFix (AssembledRecipes)": "配方·显存策略纠正",
    "ReleasePhantomVram (AssembledRecipes)": "配方·释放虚空显存",
    "ReverseServiceAutostart (AssembledRecipes)": "配方·反推服务自启",
    "ReverseJsonSanitize (AssembledRecipes)": "配方·反推JSON清洗",
    "ReverseLayoutCaption (AssembledRecipes)": "配方·布局反推",
}
