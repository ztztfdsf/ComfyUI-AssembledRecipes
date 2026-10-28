"""Assembled model recipe cache for ComfyUI.

拼装一次，落盘成配方；下次直读配方，跳过 LoRA 拼装。
- RecipeSave: 生成结束后把「已拼装权重」快照存盘（同名已存在则跳过）
- RecipeGallery: 画布点选配方直读（不选=走兜底 LoRA 链）；配方删除=彻底硬删
- StreamToggle: MODEL 直通，可选逐层流水（省显存、防换页）
- DeepClean/FreeVram/VramModeFix: 显存治理（卸载/清池/策略纠正）
- ReverseJsonSanitize/ReverseLayoutCaption/BlankToNone: 布局反推链容错

【防鬼图】aimdo 动态加载下 LoRA 会烤进常驻权重，还原靠 patcher.backup；
DeepClean 的 drop_patch_backups 已强制禁用（丢备份=烤入永久化）。
所有节点带 [AR⏱] 计时插桩。
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
TRASH_DIR = None  # 删除已改为硬删除，不再使用回收目录
os.makedirs(RECIPES_DIR, exist_ok=True)

STREAM_RESIDENT_BYTES = 0.1  # 驻留预算≈0 → 全流水


MAX_NEW_TOKENS_CAP = 6144  # 反推输出硬顶：规则要求最多 10 框×220 词≈3000 token，再高纯浪费
GEN_TIMEOUT_S = 300       # 单次推理超时（到点带上已有输出返回，尾部交清洗节点兜底）


def _gen_stopper(deadline_s):
    """generate 的停止条件：超时 / 用户点了取消 / 每 256 token 报进度。

    以前 generate 是一次性阻塞调用：主人设 16384 token 上限时模型万一不吐 EOS，
    就要白算 20 分钟，而且 ComfyUI 的“取消”按钮完全叫不停它（实测）。
    """
    try:
        from transformers import StoppingCriteria
    except Exception:
        return None
    import time as _t

    class _Stop(StoppingCriteria):
        def __init__(self):
            self.t0 = _t.time()
            self.n = 0
            self.reason = None

        def __call__(self, input_ids, scores, **kwargs):
            self.n += 1
            if self.n % 256 == 0:
                print(f"[AssembledRecipes] 反推生成中: {self.n} tok / {_t.time() - self.t0:.0f}s", flush=True)
            if _t.time() - self.t0 > deadline_s:
                self.reason = f"超时 {deadline_s}s"
                return True
            try:
                from comfy import model_management as _mm
                if _mm.processing_interrupted():
                    self.reason = "用户取消"
                    return True
            except Exception:
                pass
            return False

    return _Stop()


def _model_type_of(path):
    """读 config.json 的 model_type（小写）；读不到返回空串。"""
    try:
        with open(os.path.join(path, "config.json"), encoding="utf-8") as f:
            return (json.load(f).get("model_type") or "").lower()
    except Exception:
        return ""


def _model_size_gb(path):
    """模型目录里 safetensors 总占用（GB），用来估算各量化档位要多少显存。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                if f.endswith((".safetensors", ".bin", ".pt")):
                    total += os.path.getsize(os.path.join(root, f))
    except Exception:
        return 0.0
    return total / 2**30


def _estimate_need_gb(path, quant):
    """估算反推峰值显存需求（GB）。权重 / 量化倍数 + 激活与 KV 余量。"""
    size = _model_size_gb(path)
    if size <= 0:
        return 0.0
    if quant.startswith("4bit"):
        return size / 4.0 * 1.10 + 3.0
    if quant.startswith("8bit"):
        return size / 2.0 * 1.05 + 3.0
    return size * 1.02 + 3.0


def _list_llm_models():
    root = os.path.join(folder_paths.models_dir, "LLM")
    try:
        names = [d for d in sorted(os.listdir(root))
                 if not d.startswith(".")
                 and os.path.isdir(os.path.join(root, d))
                 and os.path.isfile(os.path.join(root, d, "config.json"))]
        return names if names else ["（LLM 目录里没有可用模型）"]
    except Exception:
        return ["（models/LLM 目录不存在）"]


def _gc_instances(cls):
    """返回当前存活的某类实例（ComfyUI 的执行缓存对象取不到句柄时用）。"""
    import gc
    out = []
    for o in gc.get_objects():
        try:
            if isinstance(o, cls):
                out.append(o)
        except Exception:
            pass
    return out


def _load_vlm_model(path, quant):
    """按量化方式从磁盘构建反推模型（device_map 分块上卡，显存峰值比 .to() 低得多）。"""
    import torch
    from transformers import AutoProcessor, AutoModelForImageTextToText

    # trust_remote_code：LFM3 这类新架构把建模代码随仓库分发（auto_map），不带这个加载不了
    processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
    kw = {}
    if quant.startswith("4bit"):
        from transformers import BitsAndBytesConfig
        # Gemma4 系在 bf16 compute 下推理时会报
        # "expected scalar type Float but found BFloat16"（加载不报、生成时才炸），
        # 所以按模型类型选 compute dtype：gemma4 用 fp32，其余保持 bf16。
        mt = ""
        try:
            with open(os.path.join(path, "config.json"), encoding="utf-8") as f:
                mt = (json.load(f).get("model_type") or "").lower()
        except Exception:
            pass
        ct = torch.float32 if "gemma4" in mt else torch.bfloat16
        bnb_kw = {"load_in_4bit": True, "bnb_4bit_quant_type": "nf4",
                  "bnb_4bit_compute_dtype": ct, "bnb_4bit_use_double_quant": True}
        if "gemma4" in mt:
            # Gemma4 的 lm_head 与 embed 共享权重（tie_word_embeddings），
            # 4bit 下每次 generate 都要把 262144×3840 的词表反量化回 fp32，
            # 一次性要吃 7.2GB，16GB 卡直接爆。跳过 lm_head 量化后
            # 峰值从 13.5GB 降到 9.5GB（实测），质量无影响。
            bnb_kw["llm_int8_skip_modules"] = ["lm_head"]
        kw = {"quantization_config": BitsAndBytesConfig(**bnb_kw),
              "device_map": {"": "cuda:0" if torch.cuda.is_available() else "cpu"},
              "dtype": ct}
        model = AutoModelForImageTextToText.from_pretrained(path, trust_remote_code=True, **kw)
        if ct is torch.float32:
            print("[AssembledRecipes] 反推: gemma4 系 → 4bit 用 fp32 compute（跳过 lm_head 量化省 4GB）", flush=True)
    elif quant.startswith("8bit"):
        from transformers import BitsAndBytesConfig
        kw["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
        kw["device_map"] = {"": "cuda:0" if torch.cuda.is_available() else "cpu"}
        kw["dtype"] = torch.bfloat16
        model = AutoModelForImageTextToText.from_pretrained(path, trust_remote_code=True, **kw)
    else:
        kw["dtype"] = torch.bfloat16
        model = AutoModelForImageTextToText.from_pretrained(path, trust_remote_code=True, **kw)
    return processor, model


def _lfm3_prompt():
    """LFM3-VL 专用提示。

    两个实测得到的硬结论（同一张图重复跑）：
    · 关掉 thinking 直接答→框退化成全图；开 thinking 能把框收紫（头/帽单独框）。
    · thinking 长度跟提示词的长短成正比：“逐元素详细推理”会烧到 3.4k tok / 100s，
      改成直接给任务 + JSON 格式只要 1.7k tok / 45s，答案一字不差。
    所以这里刻意写得极短：只列 5 个目标、坐标口径、长度要求，不让它磨叽。
    """
    return (
        "Output a JSON array with exactly 5 entries, largest region first: "
        "background scenery, whole character, upper clothing, lower clothing, "
        "head details (hat/hair/face).\n"
        "box coordinates 0-1000, each box tight around that element only. "
        "Each desc: 60-120 English words describing only what is inside that box. JSON only:\n"
        '[{"type":"obj","bbox":[x1,y1,x2,y2],"desc":"..."}, ...]'
    )


def _parse_json_array(txt):
    """从模型输出里取 JSON 数组。thinking 里也会出现 [..]，所以从后往前找能解析的。"""
    starts = [m.start() for m in re.finditer(r"\[\s*\{", txt)]
    for s in reversed(starts):
        for e in range(len(txt), s + 1, -1):
            if txt[e - 1] != "]":
                continue
            try:
                v = json.loads(txt[s:e])
            except Exception:
                continue
            if isinstance(v, list) and v and all(isinstance(x, dict) and "bbox" in x for x in v):
                return v, s
    return None, -1


def _trim_cuda_pool():
    """把 cudaMallocAsync 的池强制 trim 回 0（还给驱动）。

    `--cuda-malloc` 模式下 torch 的 empty_cache 不一定把空闲块全还回去，
    池会自己留着 → 工作流里 nobody trim 的话，多单之后显存“看着不够用”，
    反推上卡时只能吃碎片池，推理速度直接腰斩（实测 24.9s → 51.8s）。
    这里直接调运行时 API cudaMemPoolTrimTo(pool, 0)。
    """
    try:
        import ctypes
        import glob
        import os
        import torch
        libdir = os.path.join(os.path.dirname(torch.__file__), "lib")
        cands = sorted(glob.glob(os.path.join(libdir, "cudart64_*.dll"))) \
            + sorted(glob.glob(os.path.join(libdir, "libcudart*.so*")))
        if not cands:
            return False
        lib = ctypes.CDLL(cands[-1])
        pool = ctypes.c_void_p()
        if lib.cudaDeviceGetDefaultMemPool(ctypes.byref(pool), ctypes.c_int(0)) != 0:
            return False
        return lib.cudaMemPoolTrimTo(pool, ctypes.c_size_t(0)) == 0
    except Exception:
        return False


def _timed(tag):
    """计时上下文：with _timed("DeepClean"): ... → 打印 [AR⏱] DeepClean 123ms"""
    class _T:
        def __enter__(self):
            self.t0 = time.perf_counter()
            return self
        def __exit__(self, *a):
            print(f"[AR⏱] {tag} {(time.perf_counter() - self.t0) * 1000:.0f}ms", flush=True)
    return _T()


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
        with _timed("RecipeSave.state_dict→CPU"):
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
                with _timed("RecipeSave.写盘5.4GB"):
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
        with _timed("StreamToggle"):
            if streaming:
                model = enable_streaming(model)
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
        with _timed("RecipeGallery"):
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
                "drop_patch_backups": ("BOOLEAN", {"default": False, "tooltip": "【已禁用，勾了也不执行】原功能：丢掉 LoRA 合并留下的原权重备份。但 aimdo 动态加载下丢备份会把 LoRA 烤入永久化（切/关 LoRA 出鬼图），故强制保留，由下次加载自动还原"}),
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
        """驱逐 aimdo 的暂存权重镜像（ModelVBAR 驻留页），不动保留模型。

        先用 C 层 vbars_analyze() 预检：驻留 0MB 时直接跳过 gc 全堆扫描
        （实测每单扫描都是 0 命中，白扫几百毫秒）。"""
        try:
            import gc
            import comfy_aimdo.model_vbar as mv
        except Exception as e:
            return 0, 0, f"无 aimdo，跳过 ({e})"
        try:
            total = int(mv.vbars_analyze() or 0)
        except Exception:
            total = -1
        if total <= 0:
            return 0, 0, "vbar 驻留 0MB，跳过扫描"
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
        return n, freed, f"vbar 总数 {seen} 保留 {len(keep)} 剩余驻留 {total // 2**20}MB"

    def clean(self, unload_models=True, force_empty_cache=True, drop_patch_backups=False, model_pass=None, image_pass=None, any_input=None):
        with _timed("DeepClean"):
            return self._clean_impl(unload_models, force_empty_cache, drop_patch_backups, model_pass, image_pass, any_input)

    def _clean_impl(self, unload_models, force_empty_cache, drop_patch_backups, model_pass, image_pass, any_input):
        def free_mb():
            try:
                return model_management.get_free_memory() / (1024 ** 2)
            except Exception:
                return -1.0

        before = free_mb()
        dropped = drop_bytes = 0
        if drop_patch_backups:
            # 【已禁用】aimdo 动态加载下，丢备份会把 LoRA 补丁永久烤进常驻基模
            # （切/关 LoRA 后出鬼图）；备份留在 CPU RAM，下次加载时 load() 开头的
            # restore_loaded_backups() 会自动还原原权重。实测不丢备份不影响出图
            # （备份不占显存，只占 CPU RAM）。
            print("[AssembledRecipes] drop_patch_backups 已禁用（防 LoRA 烤入残留）：备份保留，由下次加载自动还原", flush=True)
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
            # 自适应清池：第一轮没清出新东西就停（固定三轮实测纯浪费 ~600ms）
            for _ in range(3):
                try:
                    before_empty = model_management.get_free_memory()
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
                except Exception:
                    break
                if model_management.get_free_memory() - before_empty < 8 * 2**20:
                    break
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
        _trim_cuda_pool()
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
        with _timed("FreeVram"):
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
        with _timed("VramModeFix"):
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
            # 每单开局彻底把 cudaMallocAsync 池还给驱动（上一单的合并临时块/碎片）
            try:
                import torch as _t
                _t.cuda.synchronize()
                _t.cuda.empty_cache()
                _t.cuda.ipc_collect()
            except Exception:
                pass
            trimmed = _trim_cuda_pool()
            free = mm.get_free_memory() / 2**20
            report = ("显存策略: " + ("; ".join(changes) if changes else "本来就是动态模式，无需纠正")
                      + f" | 空闲 {free:.0f}MB" + (" | 池trim OK" if trimmed else ""))
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
    DESCRIPTION = "修复反推输出的病 JSON（裸换行/字符串 bbox/截断），并把坐标从 xyxy 转成 yxyx，喂给边界框画布。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes"

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

    @staticmethod
    def _looks_like_corner_signature(e):
        """角落里的 Title-Case 单词 -> 疑似作者签名（如 Qianliu）。

        署名常单独出现、不带任何关键词，但位置有特征：挤在画面边缘（签名习惯），
        且是首字母大写的纯字母单词/短名。招牌与 UI 常用全大写（CAFE / START）或不在边缘。
        """
        if str(e.get("type", "")).strip().lower() != "text":
            return False
        txt = str(e.get("text", "")).strip()
        if not txt or len(txt) > 24 or " " in txt.strip():
            return False
        if not re.fullmatch(r"[A-Z][A-Za-z]{2,23}", txt):
            return False
        b = e.get("bbox")
        if not (isinstance(b, (list, tuple)) and len(b) == 4):
            return False
        y0, x0, y1, x1 = [float(v) for v in b]
        edge = 150.0
        return min(x0, y0, 1000.0 - x1, 1000.0 - y1) <= edge

    @staticmethod
    def _area(b):
        y0, x0, y1, x1 = [float(v) for v in b]
        return max(0.0, x1 - x0) * max(0.0, y1 - y0)

    @classmethod
    def _is_signature_cluster(cls, b, meta_bboxes):
        """署名簇连坐：只对「小区域」生效。

        署名一组挤在角落（FANBOX + Qianliu + 邮箱），互相重叠。但全图主体 obj
        的 bbox 也天然包含角标——那种大框绝不能连坐（实测误杀过主体框）。
        因此要求双方都是局部小块（<= 图 25%）且重叠明显。
        """
        if cls._area(b) > 250000.0:  # 1000x1000 的 25%
            return False
        for mb in meta_bboxes:
            if cls._area(mb) > 250000.0:
                continue
            if cls._overlap_ratio(b, mb) >= 0.3:
                return True
        return False

    @staticmethod
    def _overlap_ratio(a, b):
        ay0, ax0, ay1, ax1 = [float(v) for v in a]
        by0, bx0, by1, bx1 = [float(v) for v in b]
        iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        ih = max(0.0, min(ay1, by1) - max(ay0, by0))
        inter = iw * ih
        if inter <= 0:
            return 0.0
        amin = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
        return inter / amin if amin > 0 else 0.0

    @staticmethod
    def _is_meta_element(e):
        """水印 / 画师署名 / AI 声明 / 版权商用声明 / 链接 / 签名 → 一律丢弃。

        这类文字不是画面内容，是图的「外部元信息」，反推进布局会在重绘时把它
        烙进新图（还会把画师名洗成自己的签名）。规则里已经禁止模型建框，但纯
        规则约束是概率性的（实测带水印图仍被框出 5 个烂框），这里做确定性兜底。
        """
        if not isinstance(e, dict):
            return False
        etype = str(e.get("type", "")).strip().lower()
        blob = " ".join([
            str(e.get("text", "")),
            str(e.get("desc", "")),
        ]).lower()
        if not blob.strip():
            return False
        # 链接 / 邮箱 / 社交 handle
        if re.search(r"https?://|www\.|\b\S+@\S+\.(?:com|cc|net|org|jp|me|io|dev|to|gg)\b|@\w{2,}", blob):
            return True
        # 常见平台与作者名指向
        if re.search(r"fanbox|pixiv|fantia|patreon|booth\.pm|skeb|gumroad|artstation|deviantart|twitter|x\.com|telegram|discord", blob):
            return True
        # AI 生成声明 / 禁止 AI
        if re.search(r"ai[\s-]?generated|aigc|#ai\b|\bai\s*art\b|no\s*ai|not\s*for\s*ai|do\s*not\s*train|noai|禁止\s*ai|严禁\s*ai|ai\s*(禁止|训练|学习)", blob):
            return True
        # 版权 / 商用 / 转载声明
        if re.search(r"copyright|all\s*rights\s*reserved|©|™|严禁商用|禁止商用|商用|禁止转载|转载禁止|无断|二禁|do\s*not\s*(repost|use|share)|unauthori[sz]ed", blob):
            return True
        # 署名 / 签名 / 水印：text 框直接丢；obj 框只在 desc 明确说它是水印/签名时才丢
        if etype == "text" and re.search(r"signature|watermark|credit|illustration\s*by|drawn\s*by|art\s*by|illust\s*by|签名|水印|画师|作者|绘制", blob):
            return True
        if etype == "obj" and re.search(r"watermark|signature|水印|签名|署名", blob):
            return True
        return False

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
        out, dropped, bbox_fixed, meta_dropped = [], 0, 0, 0
        if isinstance(data, list):
            # 第一遍：定性谁是 meta（水印/署名/声明/链接/角落人名）
            items = [e for e in data if isinstance(e, dict)]
            meta_flags = [self._is_meta_element(e) or self._looks_like_corner_signature(e) for e in items]
            meta_bboxes = []
            for e, is_meta in zip(items, meta_flags):
                if is_meta:
                    b, _ = self._clean_bbox(e.get("bbox"))
                    if b:
                        meta_bboxes.append(b)
            # 第二遍：署名常一组挤在一起（FANBOX + Qianliu + 邮箱），与 meta 区域重叠的同驱
            for e, is_meta in zip(items, meta_flags):
                if is_meta:
                    meta_dropped += 1
                    continue
                b, _ = self._clean_bbox(e.get("bbox"))
                if b and self._is_signature_cluster(b, meta_bboxes):
                    meta_dropped += 1
                    continue
                bbox, fixed = self._clean_bbox(e.get("bbox"))
                if bbox is None:
                    dropped += 1
                    continue
                if fixed:
                    bbox_fixed += 1
                etype = "text" if str(e.get("type", "")).strip().lower() == "text" else "obj"
                desc = str(e.get("desc", "")).strip() or "unknown"
                # 坐标序交换：模型按原生习惯输出 xyxy [x0,y0,x1,y1]，
                # CreateBoundingBoxes 要 yxyx [y0,x0,y1,x1]。规则层拗不过模型（实测），
                # 在这里确定性交换，保证下游永远吃到 yxyx。
                yxyx = [bbox[1], bbox[0], bbox[3], bbox[2]]
                item = {"type": etype, "bbox": yxyx, "desc": desc}
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
        if meta_dropped:
            msg += f"，过滤水印/署名/声明 {meta_dropped} 个"
        if bbox_fixed:
            msg += f"，修复 {bbox_fixed} 个字符串 bbox"
        report.append(msg)
        result = json.dumps(out, ensure_ascii=False)
        print(f"[AssembledRecipes] 反推JSON清洗: {'; '.join(report)} | 输出 {len(result)} 字符", flush=True)
        return (result,)


_VLM_CACHE = {}  # 模型目录 -> (processor, model) 内存常驻缓存


# 规则要点：限死框数（防过度分割）、禁止把画廊 tag 逐条变框、desc 120~220 词、坐标用模型原生的 xyxy（清洗节点统一转 yxyx）
_LAYOUT_RULE_DEFAULT = (
    '# 角色\n'
    '你是图像版面解析引擎，把画面拆成「布局元素」，供文生图按区域重绘。\n'
    '\n'
    '# 绝对禁框（最高优先级，无例外）\n'
    '凡是图的「外部元信息」，不管长在画面哪个位置、不管多显眼，都视为不存在：不建框、不进 text、不进 desc。\n'
    '禁止的类别（命中任一即丢）：\n'
    '1. 水印：watermark、半透明角标、重复铺满的底纹文字。\n'
    '2. 作者/画师署名与社交 handle：@xxxx、Qianliu、qianliu@fanbox.cc、FANBOX、PIXIV、Patreon、推特/X 用户名。\n'
    '3. AI 生成声明：AI-generated、AIGC、#ai、no AI、not for AI、do not train、禁止 AI、严禁 AI。\n'
    '4. 版权与商用声明：©、™、copyright、all rights reserved、严禁商用、禁止转载、do not repost、unauthorized。\n'
    '5. 链接与签名：URL、邮箱、signature、签名。\n'
    '对比：黑板上写的「欢迎」、招牌上的「CAFE」、对话框里的台词、UI 里的按钮文字 —— 这些是画面内容，正常建框。\n'
    '判断标准：那行字是「画里」的还是「图上」的？图上的（作者盖上去的）一律不框。\n'
    '\n'
    '# 内部流程（只在心里做，不要输出）\n'
    '1. 先数画面里有几个主要主体：人物/动物各算一个主体，成段的文字算一个元素，占据显眼位置的大物体算一个元素。\n'
    '2. 同一主体的头发、眼睛、衣服、手、配饰全部属于它，只给它一个框，不拆。\n'
    '\n'
    '# 框选规则\n'
    '- 多主体必须分开建框：画面里有几个人物/动物就建几个框，各自写各自的 desc，禁止把两个及以上的人合并进同一个框。只有画面确实只有一个主体时才允许整幅大框。\n'
    '- 框数上限 10 个，宁少勿多；超过 10 个说明你在把画面拆碎。\n'
    '- 框要紧贴该元素在图中真实可见的范围：人物包含头发、四肢、手持物和衣摆；不要把阴影、背景算进去。\n'
    '- 人物 / 动物：框住整个轮廓（全身图含头顶到脚下，半身图到画面下沿或腰线）。\n'
    '- 文字块：整段文字一个框，不要一行一个框、不要一个字一个框。\n'
    '- 大物体：只框占据画面显著位置的（家具、大型道具、明显前景物）。\n'
    '- 对画面重要的整块背景环境（天空、街道、房间、荒野）可以给一个覆盖它的 obj 框，放最底层；但背景光斑、装饰星星、纹理不算。\n'
    '- 只框图里真实看得见的东西，不要编造图上不存在的元素。\n'
    '- 禁止建框：发丝、眼睛、单件饰品、衣褶、背景光斑、装饰星星、纹理、细小图案、logo 细节。\n'
    '- 禁止建框（任何情况下，包括 type=text）：水印、画师/作者署名、AI 生成声明（如 AI-generated、禁止AI使用）、版权与商用声明、网址链接、logo、签名等非画面内容文字——一律当作不存在：不建框、不输出、不描述。\n'
    '\n'
    '# 坐标\n'
    '- 归一化 0~1000 的整数，顺序是 [左, 上, 右, 下]，也就是 xmin, ymin, xmax, ymax。\n'
    '- 例：[0, 0, 1000, 1000] 是整张图；[0, 0, 1000, 500] 是画面上半部分整宽；[570, 266, 797, 500] 是画面中间偏右下的一块。\n'
    '- 必须 xmin < xmax 且 ymin < ymax；四个值都是纯数字，不加引号、不写成字符串。\n'
    '\n'
    '# 输出（直接喂给「创建边界框」节点，字段必须严格照它的规则）\n'
    '只输出一个 JSON 数组。不要 markdown 代码块，不要解释，不要任何前后缀文字。\n'
    '数组元素两种形态：\n'
    '{"type":"obj","bbox":[xmin,ymin,xmax,ymax],"desc":"英文描述"}\n'
    '{"type":"text","bbox":[xmin,ymin,xmax,ymax],"text":"画面文字原文","desc":"英文说明"}\n'
    '- type 只有 "text" 是特殊值，其余元素一律写 "obj"，不要自创别的类型。\n'
    '- text 字段只在 type 为 "text" 时出现；obj 元素不要带 text。\n'
    '- 可选字段 "color_palette"：能明确判断该区域主色调时，写最多 5 个十六进制色，如 ["#1b2a44","#d8c7a1"]；拿不准就整个字段省略，不要猜。\n'
    '- 排序：从背景到前景（先背景元素，主体在后），下游按顺序理解遮挡。\n'
    '\n'
    '# desc 要求（按 MOSS 官方布局数据的详细程度写，别怕长）\n'
    '- 必须是英文，不得出现中文或日文。\n'
    '- 每条 120~220 个英文单词，写成一个完整段落，详细到能照着这段重新画出来。框数上限 10 个，所以尽管写细，总量可控。\n'
    '- 长度不随框数缩水：即使画面里有多个主体、多个框，每条 desc 也必须独立保持 120 词以上，不要因为分了框就把每段写短。\n'
    '- 必须写到这些：\n'
    '  · 主体身份与外观（物种/性别/年龄段）、发型发色的长度与质感、有无角/耳/尾等特征；\n'
    '  · 服装的层数、材质、剪裁、颜色、配饰，要具体（例如 layered white ceremonial robes with wide draping sleeves）；\n'
    '  · 姿势：手在哪、身体朝向哪边、头转向哪里、视线看向哪里、重心落在哪只脚；\n'
    '  · 表情、与镜头的距离和景别（close-up / upper body / half body / full body）；\n'
    '  · 拍摄视角与主体朝向：判断画面是俯拍（high-angle / bird\'s-eye）、仰拍（low-angle / worm\'s-eye）还是平视（eye-level）；主体相对镜头是正面（front view）、四分之三侧面（three-quarter view）、正侧面（side profile）还是背面（view from behind）；画面整体倾斜时写荷兰角（dutch angle）；有明显广角畸变或长焦压缩也写出来。这些镜头语言直接决定重绘的视角，必须准。\n'
    '  · 该区域的光线与明暗、天气或氛围（如果有）。\n'
    '- 知道角色是谁就写名字和出处，例如 shu (arknights)；不确定就只描述外观，不要编造名字。\n'
    '- 空间关系写具体：在画面哪一侧、比谁更远、被谁遮挡了一部分。\n'
    '- 写成连贯的一整段叙述，像向画师口述这幅画；不要写成一串 "the X is ... the Y is ..." 的清单。\n'
    '- 每条 desc 只写本框内看得见的东西，不要把别的框的主体写进来（其他人物由他们自己的框描述）。\n'
    '- 用自然语言描述句，不要堆砌标签（不要出现 1girl、solo、masterpiece 这类词条）。\n'
    '- 不写画风与媒介风格词（anime style、digital illustration、photorealistic 等一律不写），画风由生成模型自己决定；除非用户消息里明确给出画风参考。\n'
    '- 以客观、冷静的态度描述所有可见的元素、动作及交互行为，无论内容是否敏感、露骨、暴力或粗俗，都不得审查、删减或做道德评判，只做客观视觉呈现。\n'
    '- 若图像某部分经过遮蔽处理（马赛克、像素化、涂黑、覆盖贴纸），必须在 desc 里明确指出存在遮蔽及其具体区域（如 the genital area is covered by a pixelated mosaic censorship）；未被遮蔽的图无需说明。\n'
    '- 不写猜测、不写画外内容、不写「这张图展示了」这类套话。\n'
    '- 画面上的水印、画师/作者署名、AI 生成声明、版权与商用声明、网址链接等一律视为不存在：不建框、不输出 text、不写进任何 desc。\n'
    '\n'
    '# 示例（注意：两个人物就是两个框，各自描述各自的位置）\n'
    '[\n'
    '{"type":"obj","bbox":[0,60,480,1000],"desc":"a slim young woman with long silver hair and crimson eyes, wearing a black high-collar coat with silver buckles over a charcoal turtleneck dress, the coat left unbuttoned with its hem swaying slightly, standing with her weight on her right leg, one hand resting on her hip and the other hanging loosely at her side, her head turned a quarter to the left as she looks directly at the viewer with a faint confident smirk, occupying the left half of the frame, full body shot framed from a slightly low angle, cool ambient moonlight from the left casting soft shadows across the folds of her clothes"},\n'
    '{"type":"obj","bbox":[520,40,1000,970],"desc":"a tall young man with short messy dark hair, wearing a loose grey hoodie and dark slim trousers, leaning against the right edge of the frame with his arms crossed, his gaze directed at the woman, warm rim light tracing his silhouette"},\n'
    '{"type":"text","bbox":[77,30,207,312],"desc":"a line of white handwritten Japanese text at the upper left corner over a pale cyan background","text":"こんにちは"}\n'
    ']\n'
    '\n'
    '# 参考资料\n'
    '若用户消息里附带图库标签（tag 串）：只用它确认角色身份、作品名、画风，把名字自然融进 desc 的角色描述里；绝不要为标签提到的身体部位、衣物、配饰单独建框，也不要逐条翻译标签。\n'
    '标签只在与画面实际相符时才可采信：画面里看不到的衣物、部位、配饰，一律不要写进 desc——宁可漏写，也不要照抄标签。\n'
)


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
                "model_name": (_list_llm_models(), {"tooltip": "ComfyUI/models/LLM/ 下现成的模型（下拉选；新放进去的模型需重启 ComfyUI 才会出现在列表里）"}),
                "max_new_tokens": ("INT", {"default": 4096, "min": 128, "max": 16384, "tooltip": "输出 token 上限（desc 详细版建议 >=2048）。实际硬顶 6144：规则最多 10 框×220 词≈3000 tok，再高只会让模型不吐 EOS 时白算很久"}),
                "pre_clear": ("BOOLEAN", {"default": True, "tooltip": "反推前把 ComfyUI 全部模型卸回内存，显存同一时间只住一个模型"}),
                "offload_after": ("BOOLEAN", {"default": True, "tooltip": "反推完本模型退卡回内存（内存常驻，下次上卡免盘读）"}),
            },
            "optional": {
                # 放在最后：旧工作流 widgets_values 前 6 位保持对齐，不会错位；缺省=4bit
                "quant": (["4bit (NF4，显存最省)", "8bit", "none (bf16，原精度)"], {"default": "4bit (NF4，显存最省)", "tooltip": "量化方式。4bit 显存最省（12B 在 16GB 卡上只有它能跑）；8bit / bf16 只有小模型或大显存卡才装得下，不够时节点会直接报错不等加载"}),
                # 大模型（如 Gemma4-12B）有能力自己从一堆 tag 里筛出画面真实内容，
                # 小模型容易看到多少 tag 就框多少个，所以做成开关。
                "tag_mode": (["全部（推荐 12B 级大模型）", "精简（只给前几个 tag）"], {"default": "全部（推荐 12B 级大模型）", "tooltip": "画廊 tag 喂多少给模型。全部=画质/装束细节更准，但小模型容易“看到多少 tag 就框多少个”；精简=只给开头的作品/角色名，防暴框"}),
            },
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
    def _settle_and_evict(settle_seconds=20, need_mb=14000):
        """模型管理：等加载平息 → 全部卸回内存 → 只留反推模型上卡。

        串行铁律：显存同一时间只住一个模型。反推前把 ComfyUI 侧的模型
        （上张图的 DiT 热机房、预取器抢跑的编码器等）全部 free_memory 卸回内存。

        坑（实测）：free_memory 有时只卸掉一部分（ComfyUI 认为某些模型
        "还在用"，或 vbar 的 staged weights 占着），清场后只剩 9789MB，
        12B 反推（峰值 12.6GB）直接 OOM。所以清完要验收，不够就逐个强制卸。
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
        # cudaMallocAsync 池 trim：把池里的空闲块还给驱动，反推上卡才有连续大块显存
        try:
            import torch as _t
            _t.cuda.synchronize()
            _t.cuda.empty_cache()
            _t.cuda.ipc_collect()
        except Exception:
            pass
        _trim_cuda_pool()

        # 验收：不够就逐个强制卸（ComfyUI 有时会把模型当成“还在用”而跳过）
        forced = 0
        for _round in range(3):
            mid = mm.get_free_memory() / 2**20
            if mid >= need_mb:
                break
            left = list(mm.current_loaded_models)
            if not left:
                break
            for lm in left:
                try:
                    lm.model_unload()
                    forced += 1
                except Exception:
                    pass
            try:
                import torch as _t
                _t.cuda.empty_cache()
            except Exception:
                pass
            _trim_cuda_pool()
            time.sleep(1)

        # aimdo 的 vbar GPU 驻留页不归 ComfyUI 的 model_management 管，
        # 必须单独驱逐，否则反推大模型时白白被吃掉几 GB（实测就是这样卡的）。
        vbar_note = ""
        try:
            n_v, freed_v, note_v = DeepClean._evict_staged()
            if n_v:
                vbar_note = f" | vbar 驱逐 {n_v} 个/{freed_v // 2**20}MB"
        except Exception as e:
            vbar_note = f" | vbar 驱逐失败 {e}"

        # ComfyUI 的节点输出缓存会抱着上一单的模型对象（实测 DiT 5.2GB 卸不掉），
        # 导致再清也腾不出显存。这里直接清掉执行缓存（代价：下一单少一点缓存加速）。
        try:
            import comfy_execution.caching as _cc
            cleared = []
            for cls_name in ("CacheSet", "HierarchicalCache", "LRUCache", "RAMPressureCache", "NullCache"):
                cls = getattr(_cc, cls_name, None)
                if cls is None:
                    continue
                for inst in list(_gc_instances(cls)):
                    for m_name in ("clean_unused", "ram_release", "clear", "reset"):
                        fn = getattr(inst, m_name, None)
                        if not callable(fn):
                            continue
                        try:
                            fn()
                            cleared.append(f"{cls_name}.{m_name}")
                        except Exception:
                            pass
                        break
            if cleared:
                vbar_note += f" | 清缓存 {'/'.join(cleared)}"
        except Exception:
            pass

        # 最后一手：moss-image 的 Qwen 编码器走 aimdo 的 vbar 分页，ComfyUI 的
        # unload/detach/清缓存都不会把 GPU 页退回去（实测恒定残留 5.2GB）。
        # 这里直接扫所有 nn.Module，把还扚在 GPU 上的手动搬回 CPU。
        # 代价：下一单出图要重载编码器（+~60s），所以只在显存不够时才做。
        moved = 0
        if mm.get_free_memory() / 2**20 < need_mb:
            try:
                import gc as _g3
                import torch as _t3
                for obj in _g3.get_objects():
                    if not isinstance(obj, _t3.nn.Module):
                        continue
                    try:
                        ps = list(obj.parameters(recurse=False))
                        if not ps:
                            continue
                        if any(p.device.type == "cuda" for p in ps):
                            obj.to("cpu")
                            moved += 1
                    except Exception:
                        pass
                if moved:
                    _t3.cuda.empty_cache()
                    _trim_cuda_pool()
                    vbar_note += f" | 强制搬回 CPU {moved} 个模块"
            except Exception as e:
                vbar_note += f" | 搬 CPU 失败 {e}"

        after = mm.get_free_memory() / 2**20
        # 残留量改成并入主日志（曾用来排查 moss 编码器卡 5.2GB：残留不为 0 就该看
        # _settle_and_evict 里的「强制搬回 CPU」是不是被跳过了）
        residual = -1
        try:
            import torch as _t
            residual = _t.cuda.memory_allocated() / 2**20
        except Exception:
            pass
        warn = "" if after >= need_mb else f" ⚠ 仍不足（目标 {need_mb}MB）"
        print(f"[AssembledRecipes] 反推前清场: 等待 {wait_s:.0f}s | 卸载 {names or '无'}"
              f"{f' +强制{forced}个' if forced else ''} | "
              f"空闲 {before:.0f}→{after:.0f}MB | 池trim {'OK' if _trim_cuda_pool() else '跳过'}"
              f"{f' | 残留 {residual:.0f}MB' if residual > 100 else ''}{vbar_note}{warn}", flush=True)

    def caption(self, image, rule, user_prompt, model_name, max_new_tokens, pre_clear, offload_after, quant="4bit (NF4，显存最省)", tag_mode="全部（推荐 12B 级大模型）"):
        import torch
        path = os.path.join(folder_paths.models_dir, "LLM", model_name)
        if not os.path.isdir(path):
            avail = []
            try:
                llm_root = os.path.join(folder_paths.models_dir, "LLM")
                avail = sorted(d for d in os.listdir(llm_root)
                               if os.path.isdir(os.path.join(llm_root, d)))
            except Exception:
                pass
            raise RuntimeError(
                f"找不到反推模型目录: {path}\n"
                f"LLM 目录下现有: {avail or '（空）'}\n"
                f"把 HF transformers 格式的模型整个放进去，或在节点里把 model_name 改成上面某个名字。")

        is_lfm3 = "lfm3" in _model_type_of(path)

        if pre_clear:
            # 按模型实际需求定清场目标：12B 在 16GB 卡上要 14GB，4B 类 8GB，
            # LFM3 只要 4GB（1.9B 全精度 3.6GB）——目标越低越不会触发“强制搬 CPU”，
            # 小模型就不用每单重载那个 5.2GB 文本编码器。
            need = 14000 if "gemma4" in _model_type_of(path) else (4000 if is_lfm3 else 8000)
            self._settle_and_evict(need_mb=need)

        # LFM3 强制全精度：它才 1.9B，bf16 只占 3.6GB，没必要量化；
        # 实测 4bit 量化会把它“先想再答”的能力搞坏（思考停不下来、JSON 崩），
        # 而 bf16 全精度代价只有 3.6GB —— 省这点不值得。
        if is_lfm3 and not quant.startswith("none"):
            print(f"[AssembledRecipes] LFM3 强制全精度（忽略 quant={quant}），bf16 仅占 ~3.6GB", flush=True)
            quant = "none (bf16，原精度)"

        # 量化档位预检：8bit / bf16 的大模型在 16GB 卡上必然 OOM，
        # 不预检就要白等一分钟加载才报错（实测 12B：8bit 63s、bf16 107s）。
        need_gb = _estimate_need_gb(path, quant)
        if need_gb > 0:
            from comfy import model_management as _mm2
            avail_gb = _mm2.get_free_memory() / 2**30
            if need_gb > avail_gb:
                hint = "，这模型请改用 4bit" if not quant.startswith("4bit") else ""
                raise RuntimeError(
                    f"显存不够：{quant} 估算需 {need_gb:.1f}GB，当前只有 {avail_gb:.1f}GB{hint}。\n"
                    f"（模型目录 {_model_size_gb(path):.1f}GB；清场开销太大时先关掉其他占显存的程序，或换更小的模型）")

        t0 = time.time()
        cache_key = (path, quant)
        cached = _VLM_CACHE.get(cache_key)
        if cached is None:
            processor, model = _load_vlm_model(path, quant)
            _VLM_CACHE[cache_key] = (processor, model)
            qtag = {"4": "4bit", "8": "8bit"}.get(quant[0], "bf16")
            first = f"首次加载[{qtag}] {time.time() - t0:.1f}s（内存常驻，以后免盘读）"
        else:
            processor, model = cached
            first = "内存命中"
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

        def _oom():
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

        def _is_oom(e):
            # torch/ComfyUI 的 OOM 文案多种多样："Allocation on device"、
            # "CUDA out of memory"、torch.OutOfMemoryError…都要认，
            # 否则降级链根本不触发（实测杬在这里）。
            s = f"{type(e).__name__} {e}".lower()
            return (isinstance(e, torch.OutOfMemoryError)
                    or "out of memory" in s
                    or "allocation on device" in s)

        # tag 处理只做一次（降级链里每档都算一遍会把日志刷满）
        all_mode = str(tag_mode).startswith("全部")
        up = self._trim_user_prompt(user_prompt, max_chars=100000 if all_mode else 100)

        def _prepare(max_side, tokens):
            pil = self._image_to_pil(image, max_side=max_side)
            if is_lfm3:
                # LFM3：内置精简提示（长规则它跟不上），开 thinking 走“从大到小逐元素推理”。
                # 不拼画廊 tag：实测塞 tag 会让 2B 的思考量从 ~2.3k 涨到满 4k 还出不来答案；
                # 它自己看图完全够（标签反而让它纠结）。
                content = _lfm3_prompt()
                messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": content}]}]
                chat = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                                    enable_thinking=True)
            else:
                content = (("Gallery tags (auto-generated; may list features NOT visible in this image — "
                            "use them only to confirm character/work identity, never as a placement checklist):\n"
                            + up + "\n\n" if up else "")
                           + "Analyze the image and output ONLY the JSON array as instructed.")
                messages = [
                    {"role": "system", "content": rule},
                    {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": content}]},
                ]
                chat = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inp = processor(text=[chat], images=[pil], return_tensors="pt").to(device)
            return pil, inp, int(tokens)

        t1 = time.time()
        up_s = 0.0
        gen_s = off_s = 0.0
        n_prompt = n_new = 0
        pil = None
        text_out = ""
        free0 = free1 = -1
        retry_note = ""
        try:
            # 上卡前再验收一次：不够就重跑清场（上卡阶段的 OOM 没有降级链兜底）
            from comfy import model_management as _mm
            free_mb = _mm.get_free_memory() / 2**20
            if free_mb < 13000:
                print(f"[AssembledRecipes] 反推上卡前显存仅 {free_mb:.0f}MB，再清一次", flush=True)
                self._settle_and_evict(settle_seconds=4)
                up_s = 0.0
            t1 = time.time()
            try:
                model.to(device)
            except Exception as e:
                if "out of memory" not in str(e).lower():
                    raise
                print(f"[AssembledRecipes] 反推上卡 OOM，强制清场后重试", flush=True)
                self._settle_and_evict(settle_seconds=2, need_mb=14000)
                try:
                    model.to(device)
                except Exception as e2:
                    if "out of memory" not in str(e2).lower():
                        raise
                    # .to('cuda') 要一次性搬整个 CPU 副本，峰值比 from_pretrained 的
                    # device_map 分块上卡高得多。大模型在 16GB 卡上只能走重建路径。
                    print("[AssembledRecipes] 二次 OOM，销毁 CPU 副本改走分块加载（较慢但稳）", flush=True)
                    _VLM_CACHE.pop(cache_key, None)
                    try:
                        del model
                    except Exception:
                        pass
                    import gc as _gc
                    _gc.collect()
                    self._settle_and_evict(settle_seconds=2, need_mb=14000)
                    model = _load_vlm_model(path, quant)[1]
                    _VLM_CACHE[cache_key] = (processor, model)
                    first += "（重建）"
            up_s = time.time() - t1

            if image.shape[0] > 1:
                print(f"[AssembledRecipes] 布局反推: 批量 {image.shape[0]} 张，只取第一张", flush=True)

            # OOM 降级链：原尺寸 → 448 → 320（图越小视觉 token 越少，显存峰值越低）
            # token 上限先夹到硬顶：主人拉到 16384 只是保险丝，不该真的算到 16384
            want_tokens = min(int(max_new_tokens), MAX_NEW_TOKENS_CAP)
            # Gemma4-12B 在 16GB 卡上很挤（工作流里编码器常驻 5.2GB 卸不掉，
            # 12B 峰值 9.5GB 就顶线）。给它降一档长边，峰值降 ~0.4GB。
            big_model = "gemma4" in _model_type_of(path)
            first_side = 512 if big_model else 640
            attempts = [(first_side, want_tokens), (448, min(want_tokens, 2048)),
                        (320, min(want_tokens, 1024))]
            out_ids = None
            for i, (side, toks) in enumerate(attempts):
                pil, inputs, toks = _prepare(side, toks)
                try:
                    t2 = time.time()
                    stopper = _gen_stopper(GEN_TIMEOUT_S)
                    gen_kwargs = {"max_new_tokens": toks, "do_sample": False}
                    if stopper is not None:
                        gen_kwargs["stopping_criteria"] = [stopper]
                    with torch.inference_mode():
                        out_ids = model.generate(**inputs, **gen_kwargs)
                    gen_s = time.time() - t2
                    max_new_tokens = toks
                    if stopper is not None and stopper.reason:
                        retry_note = f" | ⚠ 生成提前结束（{stopper.reason}，{stopper.n} tok）"
                        if "取消" in stopper.reason:
                            retry_note += "：输出不完整，本单作废重跑即可"
                        elif "超时" in stopper.reason:
                            retry_note += "：输出不完整，尾部交清洗节点兜底"
                    if i:
                        retry_note += f" | ⚠ 显存不足已降档重试（长边 {side}）"
                    break
                except Exception as e:
                    if not _is_oom(e) or i == len(attempts) - 1:
                        raise
                    print(f"[AssembledRecipes] 反推显存不足（长边 {side}），降档重试…", flush=True)
                    del inputs
                    _oom()

            n_new = out_ids.shape[1] - inputs["input_ids"].shape[1]
            n_prompt = inputs["input_ids"].shape[1]
            text_out = processor.batch_decode(out_ids[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]
            if is_lfm3:
                # LFM3 先在 thinking 里推理再出 JSON，取最后一个能解析的数组
                arr, pos = _parse_json_array(text_out)
                if arr:
                    text_out = json.dumps(arr, ensure_ascii=False)
                    n_items = len(arr)
                    print(f"[AssembledRecipes] LFM3 顺序推理: 捕获 {n_items} 个元素", flush=True)
                else:
                    print("[AssembledRecipes] LFM3 输出里没找到可解析的 JSON 数组，原文交给清洗节点", flush=True)
            free0 = torch.cuda.mem_get_info()[0] / 2**20 if torch.cuda.is_available() else -1
            del inputs, out_ids
        finally:
            # 无论成功、OOM 还是被中断，VLM 必须退卡——否则 8GB 会永久死在显存里，
            # 而且它不归 ComfyUI 的 model_management 管，/free 洗不掉（实测踩过）。
            if offload_after:
                t3 = time.time()
                residual = 0
                try:
                    model.to("cpu")
                except Exception as e:
                    print(f"[AssembledRecipes] 反推模型退卡失败: {e}", flush=True)
                _oom()
                _trim_cuda_pool()
                # bnb 量化 + device_map 加载的模型，.to('cpu') 经常只搬一部分，
                # 残留几 GB 卡在显存里（实测 Gemma4-12B 残留 5.1GB，
                # 下一单反推直接 OOM）。残了就整只销毁，下次重建（+30s）。
                try:
                    residual = torch.cuda.memory_allocated()
                except Exception:
                    residual = 0
                if residual > 800 * 2**20:
                    _VLM_CACHE.pop(cache_key, None)
                    try:
                        del model
                    except Exception:
                        pass
                    import gc as _gc
                    _gc.collect()
                    _oom()
                    _trim_cuda_pool()
                    try:
                        residual = torch.cuda.memory_allocated()
                    except Exception:
                        residual = 0
                    print(f"[AssembledRecipes] 退卡不彻底（残留 {residual / 2**20:.0f}MB），"
                          f"已销毁缓存，下次重建", flush=True)
                off_s = time.time() - t3
                free1 = torch.cuda.mem_get_info()[0] / 2**20 if torch.cuda.is_available() else -1

        trunc = " | ⚠ 触顶截断，尾部交给清洗节点兜底" if n_new >= int(max_new_tokens) else ""
        img_size = f"{pil.width}x{pil.height}" if pil is not None else "?"
        print(f"[AssembledRecipes] 布局反推: {first} | 图 {img_size} | 提示 {n_prompt} tok + 输出 {n_new} tok ({n_new / max(gen_s, 0.1):.1f} tok/s) | 上卡 {up_s:.1f}s | 推理 {gen_s:.1f}s "
              f"| 退卡 {off_s:.1f}s | 空闲 {free0:.0f}→{free1:.0f}MB | 文本 {len(text_out)} 字符{trunc}{retry_note}", flush=True)
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
