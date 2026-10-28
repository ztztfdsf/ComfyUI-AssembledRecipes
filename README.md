# ComfyUI-AssembledRecipes

**拼装一次，处处直读** —— LoRA 拼装太慢？把拼好的权重快照成配方，下次直读，跳过拼装。
顺带解决量化模型在 16G / 8G 卡上的显存爆表问题。

## 痛点

量化模型 + LoRA 时，ComfyUI 每次加载都要「反量化 → 合并补丁 → 重新量化」，
一个组合几分钟；换组合再拼一次。本插件把拼好的权重落盘复用，**拼一次，用一辈子**。

## 节点（11 个）

| 节点 | 作用 |
|---|---|
| **配方·保存快照** | 放在生成链尾部。生成完成后把「已拼装权重」存成 `models/assembled_recipes/<名字>.safetensors`。**名字留空 = 按 LoRA 组合自动命名** `auto_<名1>+<名2>`。自动生成瀑布流缩略图（把各 LoRA 预览图横排合成） |
| **配方·画廊选择** | 画布自绘瀑布流画廊（缩略图 / 名字 / 体积 / 时间）。**点卡片 = 直读配方（跳过 LoRA 分支）；不选 = 走兜底 `model` 输入（正常拼装）**。卡片右上角 × 删除（进 `trash/`），🔄 刷新列表 |
| **配方·逐层流水开关** | MODEL 直通 + 逐层流水开关。逐层流水 = 权重逐 block 按需上卡，显存峰值 = 单层 + 激活，永不爆表，代价是稍慢 |
| **配方·深度清理** | 最彻底的清理：卸载全部模型 + `gc` + `synchronize` + `empty_cache ×3` + `ipc_collect`。用于真挤 / 跑前清残渣；可选丢弃 LoRA 合并留下的原权重备份（腾出≈模型体积，前提是下次从干净基座重打补丁） |
| **配方·踢编码器回内存** | 放在**编码之后、采样之前**：吃 model + 正负条件（保证时序），把除主模型外的已加载模型（文本编码器等 5G+）offload 回内存并清缓存。走官方 `free_memory(keep_loaded=…)`，安全 |
| **配方·显存策略纠正** | 运行时把 `--highvram` 纠回 NORMAL_VRAM + 重开 smart memory（等价摘掉启动参数，启动器零改动） |
| **配方·布局反推** | **进程内直载** Qwen3-VL（transformers，零外部进程零黑框）反推出 MOSS 布局 JSON。显存串行铁律：反推前自动清场（全部卸回内存）→ 模型上卡独占 → 推理完退卡回内存（常驻缓存）。**框数硬上限 10 个**（多主体强制分开建框，只框人物/大型物体/文字块，不给发丝衣褶建框），desc 强制英文 120~220 词连贯段落；水印/署名/AI声明/版权商用一律不建框（规则禁令 + 清洗节点确定性拦截双保险）；画廊标签自动裁剪到 100 字符（防几百个 tag 被逐条变框）。输出详细 desc 但可控 |
| **配方·反推JSON清洗** | 反推输出坏 JSON 的兜底（裸换行/字符串bbox/截断/纯废话都接得住），并把坐标从模型原生的 xyxy 确定性转成 CreateBoundingBoxes 要的 yxyx；水印/署名/声明类框在这里硬拦（关键词黑名单 + 角落人名启发 + 署名簇连坐），接 `CreateBoundingBoxes` 前垫一道 |
| **配方·空串转无** | 空/纯空白文本 → `None`。修「画廊空选择吃掉提示词」的坑：rgthree Any Switch 只跳过 `None` 不跳过 `""`，而画廊类节点没选内容时正好吐空字符串，会抢走开关优先位把下游提示词框整个吃掉。接在画廊输出与开关之间即可正确回落 |

## 用法

1. **第一次**：正常跑图（加载器 → 清理 → LoRA → 画廊[不选卡片] → 子图），末尾接「保存快照」
2. 跑完自动存成 `auto_<LoRA组合>` 配方
3. **以后**：在画廊里点那张卡片 → 直读配方，秒级出图
4. **管理**：画廊卡片右键删除，或侧边栏「配方缓存」面板（删除 = 移入 `trash/`，可找回）

推荐链路：

```
加载器 → 开局重清理 → 逐层流水开关 → LoRA → 画廊选择 → 拼装后清缓存 → 采样子图
                                                                      ↓
                              保存快照 → 卸载 → 清缓存 → 预览
```

### 节点更新记录
- `配方·踢编码器回内存` 增加 `image_pass` 排序口（收工位用：保证在出图之后执行）+ `image` 透传输出
- `配方·深度清理` 增加 aimdo 暂存镜像（ModelVBAR）驱逐，自动保护仍在加载的主模型

## 显存优化三件套

- **逐层流水**：显存紧张/爆表/换页卡死时打开，峰值 = 单层 + 激活
- **踢编码器回内存**：量化文本编码器动辄 5G+，编码完就搬回内存，采样期只留 DiT
- **深度清理**：跑前清残渣、跑后归还显存

> 注意：`model_management.free_memory()` 是官方唯一安全的卸载入口（会维护
> `current_loaded_models` 列表）。直接对 `LoadedModel` 调 `model_unload()`
> 而不摘出列表，会让后续 `cleanup_models_gc()` 在 `is_dead()` 处崩 —— 本插件已踩过这个坑。

## 自动补线

前端带自动修复：加载工作流后若发现「模型链断了」（画廊/清理节点的 MODEL 输入空着），
会自动从上游接上，并在浏览器控制台打印 `[AssembledRecipes] 自动补线 N 根`。

## 模型下载与放置（一键直达）

| 文件 | 放哪 | 下载 |
|---|---|---|
| DiT·SFT 量化版 int8_convrot（5.4G） | `models/diffusion_models/` | [下载](https://huggingface.co/Comfy-Org/MOSS-Image/blob/main/diffusion_models/moss_image_sft_int8_convrot.safetensors) |
| DiT·Base 量化版 int8_convrot（5.4G） | `models/diffusion_models/` | [下载](https://huggingface.co/Comfy-Org/MOSS-Image/blob/main/diffusion_models/moss_image_base_int8_convrot.safetensors) |
| 文本编码器 qwen3.5_9b_w4a8_gptq（7.3G） | `models/text_encoders/` | [下载](https://huggingface.co/Comfy-Org/MOSS-Image/blob/main/text_encoders/qwen3.5_9b_w4a8_gptq.safetensors) |
| VAE flux2-vae（336M） | `models/vae/` | [下载](https://huggingface.co/Comfy-Org/MOSS-Image/blob/main/vae/flux2-vae.safetensors) |
| 反推模型 Qwen3-VL-4B 去审查版（8.9G，HF transformers 布局整个目录）：`Qwen3-VL-4b-Heretic`（推荐，新法去审查）或 `Huihui-Qwen3-VL-4B-Instruct-abliterated` | `models/LLM/<模型目录>/` | [Heretic](https://huggingface.co/DreamFast/Qwen3-VL-4b-Heretic) · [Huihui](https://huggingface.co/huihui-ai/Huihui-Qwen3-VL-4B-Instruct-abliterated) |

- **下载即用，无需拆分**：2026-10-16 起 ComfyUI（≥ v0.42）原生支持 MOSS，直接用核心简易节点 `UNETLoader` / `CLIPLoader(type=moss_image)` 加载官方融合版文件。低于 v0.42 的旧版本没有原生 MOSS 支持。
- 社区 LoRA 多按融合 qkv 训练：原生融合链路下注意力权重可 **100%** 打进（旧拆分方案只能打进约六成）。
- 本插件配方直读优先走 `comfy.sd.load_diffusion_model`（融合/原生格式），旧拆分格式配方自动回退 moss-image 插件加载器。
- 反推依赖（按模型分档）：
  - `transformers>=4.57` —— Qwen3-VL / Gemma4 系
  - `transformers>=5.19` —— LFM3-VL 系（模型仓库自带建模代码，节点会以 `trust_remote_code=True` 执行模型目录内的 `.py`，请只使用可信来源）
  - `bitsandbytes` + `accelerate` —— 4bit / 8bit 量化档
  - 模型走进程内直载，无外部服务、无黑框；首次反推时从盘加载入内存（~30s），之后常驻内存免盘读。
  - 反推模型若是新架构（自带 `auto_map`），加载时会在日志里明确提示"将以 trust_remote_code 执行该目录内的 .py"。

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

- 配方 = 完整模型权重（含量化格式与 `comfy_quant` 元数据），体积 ≈ 模型本身
- 工作流加载器建议直接用 ComfyUI **核心简易节点**（`UNETLoader`/`CLIPLoader`），无需 moss-image 插件
- 快照采用「源文件全量镜像 + 合并权重覆盖」，所以键集与源模型一字不差
- 不修改 ComfyUI 核心；所有功能都是节点；逐层流水通过自有 patcher 实例绑定实现


## 行为约定（省磁盘）

- **单/无激活 LoRA 不存配方**（没有融合成本，存 5.4G 纯浪费；≥2 个激活 LoRA 才快照）
- **画廊再点一次已选中的卡片 = 取消选择**（或点顶部「✕ 取消选择」），回到兜底 model 分支
