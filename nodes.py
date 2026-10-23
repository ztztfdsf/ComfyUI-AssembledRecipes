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
                "drop_patch_backups": ("BOOLEAN", {"default": False, "tooltip": "丢掉 LoRA 合并留下的原权重备份(可腾出接近模型体积的显存)；前提是下次从干净基座重打补丁"}),
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

    def clean(self, unload_models=True, force_empty_cache=True, drop_patch_backups=False, model_pass=None, image_pass=None, any_input=None):
        def free_mb():
            try:
                return model_management.get_free_memory() / (1024 ** 2)
            except Exception:
                return -1.0

        before = free_mb()
        dropped = drop_bytes = 0
        if drop_patch_backups:
            import gc as _gc
            try:
                import comfy.model_patcher as _mp
                seen = set()
                for obj in _gc.get_objects():
                    try:
                        if not isinstance(obj, _mp.ModelPatcher) or id(obj) in seen:
                            continue
                        seen.add(id(obj))
                        for attr in ('backup', 'backup_buffers', 'hook_backup', 'object_patches_backup'):
                            d = getattr(obj, attr, None)
                            if isinstance(d, dict) and d:
                                for v in list(d.values()):
                                    try:
                                        w = getattr(v, 'weight', v)
                                        if torch.is_tensor(w):
                                            drop_bytes += w.nelement() * w.element_size()
                                    except Exception:
                                        pass
                                d.clear()
                                dropped += 1
                    except Exception:
                        pass
            except Exception as e:
                print(f"[AssembledRecipes] 丢补丁备份跳过: {e}")
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
        if dropped:
            report += f" | 丢弃备份 {dropped} 份≈{drop_bytes // 2**20}MB"
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


_VLM_CACHE = {}  # 模型目录 -> (processor, model) 内存常驻缓存


# 规则要点：限死框数（防过度分割）、禁止把画廊 tag 逐条变框、desc 15~30 词（防 token 爆炸）
_LAYOUT_RULE_DEFAULT = (
    "# 角色\n"
    "你是图像版面解析引擎，把画面拆成「布局元素」，供文生图按区域重绘。\n"
    "\n"
    "# 内部流程（只在心里做，不要输出）\n"
    "1. 先数画面里有几个主要主体：人物/动物各算一个主体，成段的文字算一个元素，占据显眼位置的大物体算一个元素。\n"
    "2. 同一主体的头发、眼睛、衣服、手、配饰全部属于它，只给它一个框，不拆。\n"
    "\n"
    "# 框选规则\n"
    "- 框数上限 10 个，宁少勿多；超过 10 个说明你在把画面拆碎。\n"
    "- 人物 / 动物：框住整个轮廓（全身图含头顶到脚下，半身图到画面下沿或腰线）。\n"
    "- 文字块：整段文字一个框，不要一行一个框、不要一个字一个框。\n"
    "- 大物体：只框占据画面显著位置的（家具、大型道具、明显前景物）。\n"
    "- 禁止建框：发丝、眼睛、单件饰品、衣褶、背景光斑、装饰星星、纹理、细小图案、logo 细节。\n"
    "\n"
    "# 坐标\n"
    "- 归一化 0~1000 的整数，顺序是 [上, 左, 下, 右]，也就是 ymin, xmin, ymax, xmax。\n"
    "- 例：[0, 0, 1000, 1000] 是整张图；[0, 0, 500, 1000] 是画面上半部分整宽；[266, 570, 500, 797] 是画面中间偏右下的一块。\n"
    "- 必须 ymin < ymax 且 xmin < xmax；四个值都是纯数字，不加引号、不写成字符串。\n"
    "\n"
    "# 输出（直接喂给「创建边界框」节点，字段必须严格照它的规则）\n"
    "只输出一个 JSON 数组。不要 markdown 代码块，不要解释，不要任何前后缀文字。\n"
    "数组元素两种形态：\n"
    "{\"type\":\"obj\",\"bbox\":[ymin,xmin,ymax,xmax],\"desc\":\"英文描述\"}\n"
    "{\"type\":\"text\",\"bbox\":[ymin,xmin,ymax,xmax],\"text\":\"画面文字原文\",\"desc\":\"英文说明\"}\n"
    "- type 只有 \"text\" 是特殊值，其余元素一律写 \"obj\"，不要自创别的类型。\n"
    "- text 字段只在 type 为 \"text\" 时出现；obj 元素不要带 text。\n"
    "- 可选字段 \"color_palette\"：能明确判断该区域主色调时，写最多 5 个十六进制色，如 [\"#1b2a44\",\"#d8c7a1\"]；拿不准就整个字段省略，不要猜。\n"
    "- 排序：从背景到前景（先背景元素，主体在后），下游按顺序理解遮挡。\n"
    "\n"
    "# desc 要求（按 MOSS 官方布局数据的详细程度写，别怕长）\n"
    "- 必须是英文，不得出现中文或日文。\n"
    "- 每条 40~80 个英文单词，写成一个完整段落，详细到能照着这段重新画出来。框数上限 10 个，所以尽管写细，总量可控。\n"
    "- 必须写到这些：\n"
    "  · 主体身份与外观（物种/性别/年龄段）、发型发色的长度与质感、有无角/耳/尾等特征；\n"
    "  · 服装的层数、材质、剪裁、颜色、配饰，要具体（例如 layered white ceremonial robes with wide draping sleeves）；\n"
    "  · 姿势：手在哪、身体朝向哪边、头转向哪里、视线看向哪里、重心落在哪只脚；\n"
    "  · 表情、与镜头的距离和景别（close-up / upper body / half body / full body）；\n"
    "  · 该区域的光线与明暗、天气或氛围（如果有）。\n"
    "- 知道角色是谁就写名字和出处，例如 shu (arknights)；不确定就只描述外观，不要编造名字。\n"
    "- 空间关系写具体：在画面哪一侧、比谁更远、被谁遮挡了一部分。\n"
    "- 写成连贯的一整段叙述，像向画师口述这幅画；不要写成一串 \"the X is ... the Y is ...\" 的清单。\n"
    "- 用自然语言描述句，不要堆砌标签（不要出现 1girl、solo、masterpiece 这类词条）。\n"
    "- 不写猜测、不写画外内容、不写「这张图展示了」这类套话。\n"
    "\n"
    "# 示例\n"
    "[\n"
    "{\"type\":\"obj\",\"bbox\":[0,0,1000,1000],\"desc\":\"a slim young woman with long silver hair and crimson eyes, wearing a black high-collar coat with silver buckles, standing with one hand on her hip and a faint smirk, facing the viewer, full body shot\"},\n"
    "{\"type\":\"text\",\"bbox\":[30,77,207,312],\"desc\":\"a line of white handwritten Japanese text at the upper left corner over a pale cyan background\",\"text\":\"こんにちは\"}\n"
    "]\n"
    "\n"
    "# 参考资料\n"
    "若用户消息里附带图库标签（tag 串）：只用它确认角色身份、作品名、画风，把名字自然融进 desc 的角色描述里；绝不要为标签提到的身体部位、衣物、配饰单独建框，也不要逐条翻译标签。\n"
    "标签只在与画面实际相符时才可采信：画面里看不到的衣物、部位、配饰，一律不要写进 desc——宁可漏写，也不要照抄标签。\n"
    "")


class ReverseLayoutCaption:
    """MOSS 布局反推（进程内直载 transformers，零外部进程、零黑框）。

    显存串行铁律：反推前清场（全部卸回内存）→ 本模型上卡独占 → 推理完退卡回内存
    （内存常驻缓存，下次上卡免盘读）→ 轮到编码器上卡。
    - model_name: ComfyUI/models/LLM/ 下的模型目录（HF transformers 布局）
    - 下载: https://huggingface.co/huihui-ai/Huihui-Qwen3-VL-4B-Instruct-abliterated
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "要反推的图"}),
                "rule": ("STRING", {"multiline": True, "default": _LAYOUT_RULE_DEFAULT, "tooltip": "反推规则（system 提示词）"}),
                "user_prompt": ("STRING", {"multiline": True, "default": "", "tooltip": "画廊提示词等角色参考资料，遇到不认识的角色用它补全"}),
                "model_name": ("STRING", {"default": "Huihui-Qwen3-VL-4B-Instruct-abliterated", "tooltip": "ComfyUI/models/LLM/ 下的模型目录（HF transformers 布局）"}),
                "max_new_tokens": ("INT", {"default": 4096, "min": 128, "max": 16384, "tooltip": "输出 token 上限（desc 详细版建议 >=2048）"}),
                "pre_clear": ("BOOLEAN", {"default": True, "tooltip": "反推前把 ComfyUI 全部模型卸回内存，显存同一时间只住一个模型"}),
                "offload_after": ("BOOLEAN", {"default": True, "tooltip": "反推完本模型退卡回内存（内存常驻，下次上卡免盘读）"}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "caption"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "自研布局反推：进程内直载 Qwen3-VL（transformers），无外部进程、黑框绝迹。下游接「配方·反推JSON清洗」。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # VLM 是随机的 + 清场是外部状态：同一张图重跑也要重新反推，禁用执行缓存
        return float("NaN")

    @staticmethod
    def _trim_user_prompt(text, max_chars=100):
        """画廊标签动辄几百个，全喂进去模型会把每个 tag 都框出来（实测 227 框）。

        只保留开头最关键的若干个（角色名/作品名通常排在最前），够模型认人即可。
        截到 100 字符：前几个总是「作品名 + 角色名」，再往后就是装束/身体部位 tag，
        那是角色的默认形象而非这张画面的实际内容，留着会让 VLM 把图上没有的衣物
        （灰裙、领带、过膝袜）硬写进 desc。
        """
        t = (text or "").strip()
        if not t:
            return ""
        parts = [x.strip() for x in t.replace("\n", ", ").split(",") if x.strip()]
        out, total = [], 0
        for p in parts:
            if total + len(p) + 2 > max_chars:
                break
            out.append(p)
            total += len(p) + 2
        trimmed = ", ".join(out)
        if trimmed and len(trimmed) < len(t):
            print(f"[AssembledRecipes] 画廊标签裁剪: {len(t)} → {len(trimmed)} 字符（{len(parts)} 个 tag 取 {len(out)} 个）", flush=True)
        return trimmed

    @staticmethod
    def _image_to_pil(image, max_side=640):
        from PIL import Image
        t = image[0].clamp(0, 1).cpu().numpy()
        arr = (t * 255.0).round().astype("uint8")
        im = Image.fromarray(arr)
        # 视觉塔是慢路径（实测 ~43ms/视觉token），长边 640 → 反推 ~13s；改大仅当图里小物件多
        if max(im.size) > max_side:
            r = max_side / max(im.size)
            im = im.resize((max(1, round(im.width * r)), max(1, round(im.height * r))), Image.LANCZOS)
        return im

    @staticmethod
    def _settle_and_evict(settle_seconds=20):
        """模型管理：等加载平息 → 全部卸回内存 → 只留反推模型上卡。

        串行铁律：显存同一时间只住一个模型。反推前把 ComfyUI 侧的模型
        （上张图的 DiT 热机房、预取器抢跑的编码器等）全部 free_memory 卸回内存。
        """
        from comfy import model_management as mm
        t0 = time.time()
        last = None
        while time.time() - t0 < settle_seconds:
            try:
                cur = mm.get_free_memory()
            except Exception:
                break
            if last is not None and abs(cur - last) < 128 * 2**20:
                break
            last = cur
            time.sleep(2)
        wait_s = time.time() - t0
        before = mm.get_free_memory() / 2**20
        names = []
        try:
            for lm in list(mm.current_loaded_models):
                names.append(type(getattr(lm.model, "model", lm.model)).__name__)
        except Exception:
            pass
        try:
            device = mm.get_torch_device()
            mm.free_memory(1e30, device, keep_loaded=[])
        except Exception as e:
            print(f"[AssembledRecipes] 反推前清场: free_memory 异常 {e}", flush=True)
        try:
            mm.soft_empty_cache()
        except Exception:
            pass
        after = mm.get_free_memory() / 2**20
        print(f"[AssembledRecipes] 反推前清场: 等待 {wait_s:.0f}s | 卸载 {names or '无'} | 空闲 {before:.0f}→{after:.0f}MB", flush=True)

    def caption(self, image, rule, user_prompt, model_name, max_new_tokens, pre_clear, offload_after):
        import torch
        if pre_clear:
            self._settle_and_evict()

        path = os.path.join(folder_paths.models_dir, "LLM", model_name)
        if not os.path.isdir(path):
            raise RuntimeError(
                f"找不到反推模型目录: {path}——把 "
                f"https://huggingface.co/huihui-ai/Huihui-Qwen3-VL-4B-Instruct-abliterated "
                f"整个仓库放到 ComfyUI/models/LLM/{model_name}/")

        t0 = time.time()
        cached = _VLM_CACHE.get(path)
        if cached is None:
            from transformers import AutoProcessor, AutoModelForImageTextToText
            processor = AutoProcessor.from_pretrained(path)
            model = AutoModelForImageTextToText.from_pretrained(path, torch_dtype=torch.bfloat16)
            _VLM_CACHE[path] = (processor, model)
            first = f"首次加载 {time.time() - t0:.1f}s（内存常驻，以后免盘读）"
        else:
            processor, model = cached
            first = "内存命中"
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        t1 = time.time()
        model.to(device)
        up_s = time.time() - t1

        pil = self._image_to_pil(image)
        if image.shape[0] > 1:
            print(f"[AssembledRecipes] 布局反推: 批量 {image.shape[0]} 张，只取第一张", flush=True)
        up = self._trim_user_prompt(user_prompt)
        user_content = (
            (up + "\n\n" if up else "")
            + "Analyze the image and output ONLY the JSON array as instructed."
        )
        messages = [
            {"role": "system", "content": rule},
            {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": user_content}]},
        ]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = processor(text=[text], images=[pil], return_tensors="pt").to(device)

        t2 = time.time()
        with torch.inference_mode():
            out_ids = model.generate(
                **inputs,
                max_new_tokens=int(max_new_tokens),
                do_sample=False,
            )
        gen_s = time.time() - t2
        n_new = out_ids.shape[1] - inputs["input_ids"].shape[1]
        n_prompt = inputs["input_ids"].shape[1]
        text_out = processor.batch_decode(out_ids[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]
        free0 = torch.cuda.mem_get_info()[0] / 2**20 if torch.cuda.is_available() else -1

        t3 = time.time()
        if offload_after:
            model.to("cpu")
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
        off_s = time.time() - t3
        free1 = torch.cuda.mem_get_info()[0] / 2**20 if torch.cuda.is_available() else -1
        trunc = " | ⚠ 触顶截断，尾部交给清洗节点兜底" if n_new >= int(max_new_tokens) else ""
        print(f"[AssembledRecipes] 布局反推: {first} | 图 {pil.width}x{pil.height} | 提示 {n_prompt} tok + 输出 {n_new} tok ({n_new / max(gen_s, 0.1):.1f} tok/s) | 上卡 {up_s:.1f}s | 推理 {gen_s:.1f}s "
              f"| 退卡 {off_s:.1f}s | 空闲 {free0:.0f}→{free1:.0f}MB | 文本 {len(text_out)} 字符{trunc}", flush=True)
        return (text_out,)



class BlankToNone:
    """空字符串 → None（让下游「Any Switch」等真正回落到备用输入）。

    背景：rgthree 的 Any Switch 只跳过 None，不跳过 ""。而 Aaalice 的
    PromptSelector / BooruGallery 在「没选任何条目」时输出的正是空字符串，
    于是它会抢走开关的优先位、把后面接的提示词输入框整个吃掉。
    本节点把空白文本换成 None，开关就能正确回落到下一个输入。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True, "tooltip": "待检查的文本"}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "convert"
    CATEGORY = "AssembledRecipes"
    DESCRIPTION = "空/纯空白的文本 → None，让 Any Switch 之类正确回落到下一个输入（修画廊空选择吃提示词的坑）。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")  # 永不缓存，每次按当前值判断

    def convert(self, text):
        s = (text or "")
        if s.strip():
            return (s,)
        return (None,)

NODE_CLASS_MAPPINGS = {
    "RecipeSave (AssembledRecipes)": RecipeSaveAssembled,
    "StreamToggle (AssembledRecipes)": StreamToggle,
    "RecipeGallery (AssembledRecipes)": RecipeGallery,
    "DeepClean (AssembledRecipes)": DeepClean,
    "FreeVram (AssembledRecipes)": FreeVram,
    "VramModeFix (AssembledRecipes)": VramModeFix,
    "ReverseJsonSanitize (AssembledRecipes)": ReverseJsonSanitize,
    "ReverseLayoutCaption (AssembledRecipes)": ReverseLayoutCaption,
    "BlankToNone (AssembledRecipes)": BlankToNone,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "RecipeSave (AssembledRecipes)": "配方保存",
    "RecipeLoad (AssembledRecipes)": "配方·直读加载",
    "RecipeGallery (AssembledRecipes)": "配方·画廊选择",
    "StreamToggle (AssembledRecipes)": "配方·逐层流水开关",
    "DeepClean (AssembledRecipes)": "配方·深度清理",
    "FreeVram (AssembledRecipes)": "配方·踢编码器回内存",
    "VramModeFix (AssembledRecipes)": "配方·显存策略纠正",
    "ReverseJsonSanitize (AssembledRecipes)": "配方·反推JSON清洗",
    "ReverseLayoutCaption (AssembledRecipes)": "配方·布局反推",
    "BlankToNone (AssembledRecipes)": "配方·空串转无",
}
