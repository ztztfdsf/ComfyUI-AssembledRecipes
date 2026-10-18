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
        state = {k: v.detach().to("cpu").contiguous() for k, v in base.state_dict().items()}

        import safetensors.torch
        safetensors.torch.save_file(state, path)

        meta = {
            "name": name,
            "note": note or "",
            "lora_info": lora_info or "",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tensors": len(state),
            "bytes": os.path.getsize(path),
        }
        with open(os.path.join(RECIPES_DIR, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        print(f"[AssembledRecipes] 配方已保存: {name} ({meta['bytes']:,} B)")
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
        }

    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "load"
    CATEGORY = "AssembledRecipes"

    @classmethod
    def IS_CHANGED(cls, recipe, streaming):
        p = os.path.join(RECIPES_DIR, recipe)
        return f"{os.path.getmtime(p) if os.path.exists(p) else 0}:{streaming}"

    def load(self, recipe, streaming=False):
        path = os.path.join(RECIPES_DIR, recipe)
        patcher = comfy.sd.load_diffusion_model(path)
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


NODE_CLASS_MAPPINGS = {
    "RecipeSave (AssembledRecipes)": RecipeSaveAssembled,
    "RecipeLoad (AssembledRecipes)": RecipeLoadAssembled,
    "StreamToggle (AssembledRecipes)": StreamToggle,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "RecipeSave (AssembledRecipes)": "配方快照·保存 (AssembledRecipes)",
    "RecipeLoad (AssembledRecipes)": "配方快照·直读 (AssembledRecipes)",
    "StreamToggle (AssembledRecipes)": "逐层流水开关 (AssembledRecipes)",
}
