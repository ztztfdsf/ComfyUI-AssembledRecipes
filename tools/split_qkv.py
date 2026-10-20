# 将 Comfy-Org 的融合 qkv int8 convrot DiT 无损拆分为插件所需的分开 q/k/v
# 纯字节切片：新文件的每个张量数据都是官方文件数据段的连续切片，零数值改动
import json, mmap, os, re, struct, sys, shutil

SRC = "D:/ComfyUI-aki-v3（anima）/ComfyUI/models/diffusion_models/moss_image_sft_int8_convrot_attn.safetensors"
DST = "D:/ComfyUI-aki-v3（anima）/ComfyUI/models/diffusion_models/moss_image_sft_int8_convrot_split.safetensors.tmp"

with open(SRC, "rb") as f:
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    n = struct.unpack("<Q", mm[:8])[0]
    hdr = json.loads(mm[8:8 + n].decode("utf-8"))
    base = 8 + n  # 数据段起点

meta = hdr.pop("__metadata__", None)
print("原张量数:", len(hdr))

QKV_RE = re.compile(r"^layers\.(\d+)\.proj\.qkv\.(weight|weight_scale|comfy_quant)$")

new_tensors = []  # (name, dtype, shape, src_off, src_len)
order = sorted(hdr.keys())  # safetensors 不要求顺序，但保持确定性

for k in order:
    m = QKV_RE.match(k)
    info = hdr[k]
    s_off, s_end = info["data_offsets"]
    if not m:
        new_tensors.append((k, info["dtype"], info["shape"], base + s_off, s_end - s_off))
        continue
    N, suffix = m.group(1), m.group(2)
    if suffix == "comfy_quant":
        # JSON 元数据向量：整份复制给 q/k/v，不切分
        for name in ("q", "k", "v"):
            nk = f"layers.{N}.proj.{name}.{suffix}"
            new_tensors.append((nk, info["dtype"], list(info["shape"]),
                                base + s_off, s_end - s_off))
        continue
    rows = info["shape"][0]           # 9216
    row_bytes = (s_end - s_off) // rows
    assert (s_end - s_off) % rows == 0, f"{k} 不是整行字节"
    per = rows // 3                    # 3072
    for idx, name in enumerate(("q", "k", "v")):
        nk = f"layers.{N}.proj.{name}.{suffix}"
        new_shape = [per] + list(info["shape"][1:])
        new_tensors.append((nk, info["dtype"], new_shape,
                            base + s_off + idx * per * row_bytes, per * row_bytes))

print("拆分后张量数:", len(new_tensors))
qkv_count = sum(1 for t in new_tensors if t[0].endswith((".q.weight", ".k.weight", ".v.weight")))
print("q/k/v 权重条目:", qkv_count)

# 布局：顺序排列数据偏移
layout, off = [], 0
for name, dtype, shape, src, ln in new_tensors:
    layout.append((name, dtype, shape, src, ln, off))
    off += ln
total_data = off
print("数据段总字节:", total_data, "（应与原文件数据段一致）")

hdr_out = {}
for name, dtype, shape, src, ln, doff in layout:
    hdr_out[name] = {"dtype": dtype, "shape": shape, "data_offsets": [doff, doff + ln]}
if meta is not None:
    hdr_out["__metadata__"] = meta

hdr_bytes = json.dumps(hdr_out, separators=(",", ":")).encode("utf-8")
pad = (-len(hdr_bytes)) % 8
hdr_bytes += b" " * pad

with open(DST, "wb") as out:
    out.write(struct.pack("<Q", len(hdr_bytes)))
    out.write(hdr_bytes)
    # 顺序拷贝数据切片（写前校验对齐）
    H = len(hdr_bytes)
    for name, dtype, shape, src, ln, doff in layout:
        if out.tell() != 8 + H + doff:
            print("偏移漂移!", name, out.tell(), 8 + H + doff); sys.exit(1)
        out.write(mm[src:src + ln])
    if out.tell() != 8 + H + total_data:
        print("尾部校验失败"); sys.exit(1)

mm.close()
final = DST[:-4]
os.rename(DST, final)
print("完成:", final, os.path.getsize(final), "字节")
