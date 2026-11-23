"""本地 WD14 ONNX 打标 + 「一份文件三种标签」拼装。

为什么自己写而不调外部服务：这是训练数据打标，模型与词表都在本机
（ComfyUI/models/wd14/<model_id>/{model.onnx, selected_tags.csv}），
走 ONNX Runtime 直接推理不占显存、不吃网络、也不经过任何在线审查层。

预处理 / 后处理照 AnimaLoraStudio 的 WD14 实现（G:/蝾螈炉子/AnimaLoraStudio/
studio/services/tagging/wd14.py）：等比缩到长边=输入尺寸 → 白底居中 pad → BGR →
按 category 分开阈值（4=character 用角色阈值，9=rating 丢弃，其余走通用阈值）。

文件格式（每张图一个 .txt，三行，顺序固定）：
  第 1 行  短提示词 Tags（触发词在最前，逗号分隔）
  第 2 行  长提示词——一句话自然语言描述
  第 3 行  反推布局 JSON（{"summary":…, "items":[{type,bbox,desc}…]}）
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading

_LOCK = threading.Lock()
_CACHE: dict = {}


def models_root() -> str:
    try:
        import folder_paths
        return os.path.join(folder_paths.models_dir, "wd14")
    except Exception:
        pass
    # 不在 ComfyUI 进程里（例如写训练集打标脚本）时按插件位置回推：
    # <ComfyUI>/custom_nodes/<插件>/reverse_cloud/tagger.py → <ComfyUI>/models/wd14
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        here = os.path.dirname(here)
        cand = os.path.join(here, "models", "wd14")
        if os.path.isdir(cand):
            return cand
    return ""


def list_taggers() -> list:
    """扫 models/wd14 下带 model.onnx + selected_tags.csv 的子目录。"""
    root = models_root()
    out = []
    try:
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if (os.path.isfile(os.path.join(d, "model.onnx"))
                    and os.path.isfile(os.path.join(d, "selected_tags.csv"))):
                out.append(name)
    except Exception:
        pass
    return out or ["（models/wd14 下没有可用的打标模型）"]


def _load(model_id: str):
    """载入（并缓存）ONNX 会话 + 词表。返回 dict。"""
    key = (model_id, models_root())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    import onnxruntime as ort
    d = os.path.join(models_root(), model_id)
    mp = os.path.join(d, "model.onnx")
    tp = os.path.join(d, "selected_tags.csv")
    if not (os.path.isfile(mp) and os.path.isfile(tp)):
        raise RuntimeError("打标模型不在：%s\nmodels/wd14 下现有：%s"
                           % (d, "、".join(list_taggers())))
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    provs = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    try:
        sess = ort.InferenceSession(mp, sess_options=so, providers=provs)
    except Exception:
        sess = ort.InferenceSession(mp, sess_options=so,
                                    providers=["CPUExecutionProvider"])
    size = 448
    for dim in sess.get_inputs()[0].shape[1:]:
        if isinstance(dim, int) and dim > 0:
            size = dim
            break
    tags, cats = [], []
    with open(tp, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tags.append(row["name"].replace("_", " "))
            try:
                cats.append(int(row.get("category") or 0))
            except ValueError:
                cats.append(0)
    info = {"sess": sess, "size": size, "tags": tags, "cats": cats,
            "input": sess.get_inputs()[0].name, "model_id": model_id}
    with _LOCK:
        _CACHE[key] = info
    return info


def _preprocess(pil, size: int):
    import numpy as np
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(pil) or pil
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = img.copy()
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    canvas.paste(img, ((size - img.size[0]) // 2, (size - img.size[1]) // 2))
    arr = np.asarray(canvas, dtype=np.float32)[..., ::-1]   # WD14 训练用 BGR
    return arr[None, ...]


BLACKLIST_DEFAULT = (
    "text, watermark, signature, username, artist name, web address, logo, "
    "english text, chinese text, japanese text, speech bubble, comic, "
    "letterboxed, twitter username, dated, artist logo, web address, "
    "multiple views, reference sheet, sketch, monochrome, greyscale"
)


def tag_image(pil, model_id: str, th_general: float = 0.35, th_character: float = 0.85,
              blacklist: str = BLACKLIST_DEFAULT, max_tags: int = 0) -> list:
    """返回按置信度排序的 tag 列表（空格形式；不含 @ 触发词，触发词由上层加）。"""
    import numpy as np
    info = _load(model_id)
    x = _preprocess(pil, info["size"])
    logits = info["sess"].run(None, {info["input"]: x})[0]
    scores = np.asarray(logits)[0]
    # 有些导出的模型直接给概率，有些给 logits：值域不在 0~1 就走 sigmoid
    if scores.min() < 0 or scores.max() > 1:
        scores = 1.0 / (1.0 + np.exp(-scores))
    bl = {t.replace("_", " ").strip().lower() for t in
          (s for s in re.split(r"[,\n]", blacklist or "") if s.strip())}
    picked = []
    for i, p in enumerate(scores):
        if i >= len(info["tags"]):
            break
        tag, cat = info["tags"][i], info["cats"][i]
        if cat == 9:                      # rating（safe/questionable/explicit）不进正文
            continue
        if tag.strip().lower() in bl:
            continue
        thr = th_character if cat == 4 else th_general
        if float(p) >= thr:
            picked.append((tag, float(p)))
    picked.sort(key=lambda x: -x[1])
    out = [t for t, _ in picked]
    if max_tags and max_tags > 0:
        out = out[:max_tags]
    return out


def compose_tags(tags: list, trigger_word: str = "@YUAN") -> str:
    """触发词在最前（Anima 约定），其余逗号分隔；去重但不改动 case。"""
    head = (trigger_word or "").strip()
    seen, out = set(), []
    if head:
        for t in [x.strip() for x in re.split(r"[,\n]", head) if x.strip()]:
            k = t.lower()
            if k not in seen:
                seen.add(k)
                out.append(t)
    for t in tags or []:
        k = t.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(t.strip())
    return ", ".join(out)


_SENT_END = re.compile(r"(?<=[.!?。！？])\s+")


def one_sentence(text: str, max_chars: int = 300) -> str:
    """把长描述压成一句自然语言（取第一句；第一句太短就把前两句拼上）。"""
    t = " ".join((text or "").split())
    if not t:
        return ""
    parts = [p for p in _SENT_END.split(t) if p]
    out = parts[0] if parts else t
    if len(out) < 40 and len(parts) > 1:
        out = out + " " + parts[1]
    return out[:max_chars].strip()


def caption_from_layout(layout_text: str) -> str:
    """从反推布局 JSON 里取 summary（官方形态是 {"caption": …}）当长描述来源。"""
    # 插件被当作包加载时（from .reverse_cloud import tagger）用相对导入，
    # 被当成顶层模块加载时用绝对导入——两条都得能跑，否则节点里直接 ImportError。
    try:
        from . import providers as PV
    except ImportError:
        from reverse_cloud import providers as PV
    parsed = PV.parse_layout(layout_text or "")
    if not parsed:
        return ""
    return parsed[0] or ""


def write_caption_file(path: str, tags_line: str, nl_line: str, layout_line: str) -> str:
    """三行落盘：短 tags / 一句话长描述 / 布局 JSON。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tags_line.strip() + "\n")
        f.write((nl_line or "").strip().replace("\n", " ") + "\n")
        f.write((layout_line or "").strip().replace("\n", " ") + "\n")
    return path


def read_caption_file(path: str) -> dict:
    """读回三行（自检 / 面板预览用）。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    lines += [""] * (3 - len(lines))
    return {"tags": lines[0], "nl": lines[1], "layout": lines[2]}