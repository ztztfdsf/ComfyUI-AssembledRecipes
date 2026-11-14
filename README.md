# ComfyUI-AssembledRecipes

**显存治理 + 布局反推** 的节点集。面向「量化大模型 + LoRA 在 16G / 8G 卡上跑不动」这条链，
提供逐层流水、编码器回内存、清池纠正，以及一条进程内的 MOSS 布局反推链。

> **v2 破坏性变更（2026-11-14）**：配方缓存（`配方·保存快照` / `配方·画廊选择` /
> `/assembled-recipes` 路由 / 画廊 UI）**已整体移除**。原因见文末「为什么移除配方缓存」——
> 它存出来的快照必然是裸基座，且它想省的合并开销实测不到 1 秒。
> 想固化固定 LoRA 组合，直接用 ComfyUI 自带的 `UNETLoader → LoRA Loader ×N → ModelSave`
> 烤成成品底模即可（实测与实时链路逐像素一致）。

## 节点（7 个）

| 节点 | 作用 |
|---|---|
| **逐层流水开关** | MODEL 直通 + 逐层流水开关。逐层流水 = 权重逐 block 按需上卡，显存峰值 = 单层 + 激活，永不爆表，代价是稍慢 |
| **深度清理** | 最彻底的清理：卸载全部模型 + `gc` + `synchronize` + `empty_cache ×3` + `ipc_collect`。用于真挤 / 跑前清残渣。含 aimdo 池 trim（`cudaMemPoolTrimTo`），能把 `--cuda-malloc` 下清不干净的池真正还给驱动 |
| **踢编码器回内存** | 放在**编码之后、采样之前**：吃 model + image_pass（保证时序），把除主模型外的已加载模型（文本编码器等 5G+）offload 回内存并清缓存。走官方 `free_memory(keep_loaded=…)`，安全 |
| **显存策略纠正** | 运行时把 `--highvram` 纠回 NORMAL_VRAM + 重开 smart memory（等价摘掉启动参数，启动器零改动） |
| **布局反推** | **进程内直载 LFM3-VL-2B**（transformers，零外部进程零黑框）反推出 MOSS 布局 JSON，**默认跑 CUDA Graph 加速（实测 2.1 倍：55.6s → 26.2s，31.3 → 87.8 tok/s）**。显存串行铁律：反推前自动清场（全部卸回内存）→ 模型上卡独占 → 推理完退卡回内存（常驻缓存）。**框数硬上限 10 个**，desc 强制英文 120~220 词连贯段落；水印/署名/AI声明/版权商用一律不建框；画廊标签自动裁剪到 100 字符。输出详细 desc 但可控 |
| **反推JSON清洗** | 反推输出坏 JSON 的兜底（裸换行/字符串bbox/截断/纯废话都接得住），并把坐标从模型原生的 xyxy 确定性转成 `CreateBoundingBoxes` 要的 yxyx；水印/署名/声明类框硬拦（关键词黑名单 + 角落人名启发 + 署名簇连坐）；**同坐标重复框去重**（模型凑 10 框上限时会把同一背景/同一主体局部写两遍，同坐标只留 desc 最长的那个） |
| **空串转无** | 空/纯空白文本 → `None`。rgthree 的 Any Switch 只跳过 `None` 不跳过 `""`，而画廊/选择器类节点没选内容时正好吐空字符串，会抢走开关优先位把下游提示词框整个吃掉。接在画廊输出与开关之间即可正确回落 |

## 推荐链路

```
加载器 → 开局清缓存+GC → 逐层流水开关 → LoRA → 采样子图 → 拼装后清缓存 → 收工清池 → 预览
                                              ↓
                                      踢编码器回内存（编码后、采样前）
```

反推链路（用时开，用完关）：

```
参考图 / 画廊选中图 → 布局反推 → 反推JSON清洗 → CreateBoundingBoxes → MOSSImageLayoutPrompt
```

## 显存优化三件套

- **逐层流水**：显存紧张/爆表/换页卡死时打开，峰值 = 单层 + 激活
- **踢编码器回内存**：量化文本编码器动辄 5G+，编码完就搬回内存，采样期只留 DiT
- **深度清理**：跑前清残渣、跑后归还显存

> 注意：`model_management.free_memory()` 是官方唯一安全的卸载入口（会维护
> `current_loaded_models` 列表）。直接对 `LoadedModel` 调 `model_unload()`
> 而不摘出列表，会让后续 `cleanup_models_gc()` 在 `is_dead()` 处崩 —— 本插件已踩过这个坑。

## 模型下载与放置（一键直达）

| 文件 | 放哪 | 下载 |
|---|---|---|
| DiT·Heal 量化版 int8_convrot（5.4G） | `models/diffusion_models/` | [下载](https://huggingface.co/KBlueLeaf/MOSS-Image-heal/blob/main/comfyui/moss_image_heal_int8_convrot.safetensors) |
| DiT·Base 量化版 int8_convrot（5.4G） | `models/diffusion_models/` | [下载](https://huggingface.co/Comfy-Org/MOSS-Image/blob/main/diffusion_models/moss-image-base-int8-convrot.safetensors) |
| 文本编码器 qwen3.5_9b_w4a8_gptq（7.3G） | `models/text_encoders/` | [下载](https://huggingface.co/Comfy-Org/MOSS-Image/blob/main/text_encoders/qwen3.5_9b_w4a8_gptq.safetensors) |
| VAE flux2-vae（336M） | `models/vae/` | [下载](https://huggingface.co/Comfy-Org/MOSS-Image/blob/main/vae/flux2-vae.safetensors) |
| 反推模型 **LFM3-VL-2B**（3.6G，HF transformers 布局整个目录）：定位能力在 2B~4B 档最强（RefCOCO-avg 90.5），bf16 全精度只占 3.6GB，是唯一支持 CUDA Graph 加速的档位 | `models/LLM/<模型目录>/` | [LiquidAI/LFM3-VL-2B](https://huggingface.co/LiquidAI/LFM3-VL-2B) |

- **下载即用，无需拆分**：ComfyUI ≥ v0.42 原生支持 MOSS，直接用核心简易节点 `UNETLoader` / `CLIPLoader(type=moss_image)` 加载官方融合版文件。
- 社区 LoRA 多按融合 qkv 训练：原生融合链路下注意力权重可 **100%** 打进。
- 反推依赖：
  - `transformers>=5.19` —— LFM3-VL（模型仓库自带建模代码，节点会以 `trust_remote_code=True`
    执行模型目录内的 `.py`，请只使用可信来源）。4.x 会直接 ImportError：模型代码用了 v5 才有的 `PreTrainedConfig`
  - 模型目录整个放进 `ComfyUI/models/LLM/<任意名字>/`，节点下拉自动列出（**LFM3 排最前**）；
    新放的模型需重启 ComfyUI 才出现在列表里
  - **LFM3 加速分两层，都是自动的、失败自动回退**：
    1. **fused RMSNorm**（载入即生效）：160 个手写 RMSNorm 换成 torch 的 fused 实现，
       纯生成 34 → 53-60 tok/s。fp32 下与原版 bit 等价；bf16 下 logits 差 <1%、top1/top5 相同。
    2. **CUDA Graph**（默认启用，先跑子进程探针）：把整步 decode 抓成一张图，
       完整反推 55.6s → 26.2s（31.3 → 87.8 tok/s）。探针真实 capture 一次并 replay 8 步，
       通过才启用；不通过自动回退 `model.generate`，不会让节点报错。
  - **重要：两种加速都会改变输出的具体文字**。bf16 下 1 ulp 的数值差会被多步 argmax 放大成
    完全不同的 token 序列（实测同图同 prompt：原版 3079 tok / 85-90s，加速后 1991 tok / 33s，
    5 个框都在、desc 长度 851 vs 839、bbox 偏差 <2%）。这不是 bug，是 LLM 自回归的本质。
  - 日志里会打 `⚙ CUDA Graph(maxlen 3072, 步进 N)` 或 `⚙ generate`，可据此判断走了哪条路。
  - `bitsandbytes` + `accelerate` —— 4bit / 8bit 量化档

## 交流与反馈

**Discord 服务器**：https://discord.gg/6JjXGccYG

有问题、Bug、需求都可以来提。

## 安装

ComfyUI Manager 搜 `AssembledRecipes`，或：

```bash
cd custom_nodes
git clone https://github.com/ztztfdsf/ComfyUI-AssembledRecipes
# 重启 ComfyUI
```

## 说明

- 不修改 ComfyUI 核心；所有功能都是节点；逐层流水通过自有 patcher 实例绑定实现
- 本插件**不再注册任何 HTTP 路由、不再有前端 `web/` 资源**
- 旧工作流若还留着 `配方保存` / `配方·画廊选择` 节点，重启后会显示为缺失节点，删掉即可：
  把 `model` 链直接接到下游，`image` 链直接连到出图节点

## 为什么移除配方缓存（2026-11-14 实证）

原设计：生成结束后把「已拼装（含 LoRA）的权重」快照成 `models/assembled_recipes/*.safetensors`，
下次在画廊点卡片直读，跳过 LoRA 拼装。实测结论是这条路从根上不成立：

1. **快照必然是裸基座**。ComfyUI 对量化模型的包是 `QuantizedTensor`，LoRA 补丁走
   `patch_weight_to_device()`：反量化 → 加差分 → 重新量化，结果写回 module 参数，
   原权重留在 `patcher.backup`。`model.state_dict()` 拿到的永远是没打补丁的原始权重。
   实测把「配方」与基座整文件比对：**去掉 header 后 SHA256 完全相同**，757 个张量逐字节一致。
   直读这种配方 = 把 LoRA 全部丢掉（画风直接掉回基座）。
   同一结论在 aimdo 动态加载路径下更彻底：`ModelPatcherDynamic.setup_param` 会给每个命中
   key 挂 `LowVramPatch`，也就是**每次 forward 现算**，从头到尾不写 module 参数。
2. **它想省的合并开销本来就不存在**。同种子交替实测（1024²、30 步、4 个 LoRA）：
   实时打补丁中位 **21.5s**，直读烤好的成品中位 **20.85s**，差 0.65s——而实时链路自身两单
   之间就能差 3.7s。显存稳态同样无差别（7.2~7.9GB 两边一样，因为动态加载下没有 backup）。
3. **真正该用的固化方式**：`UNETLoader → LoRA Loader ×N → ModelSave` 跑一单，
   就能把 LoRA 真烤进权重并落盘成可当底模选的成品（`comfy_extras/nodes_model_merging.py`
   → `LazyCastingQuantizedParam` → `patch_weight_to_device(return_weight=True)` →
   `set_weight(scale="recalculate")`，168 组 `weight_scale` 自动重算）。
   实测成品与实时链路**逐像素完全相同**，代价是 +5G 磁盘、改强度要重烤。
   所以：**需要固化就用 ModelSave，不需要就实时打补丁**——两者都不需要一个 5G 的配方缓存层。