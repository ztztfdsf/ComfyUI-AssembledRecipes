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
- 插件自带一个前端面板（`web/reverse_cloud.js`）与几个同源路由 `/assembled-recipes/ds/*`，
  只给反推节点的「账号池 / 连通测试 / 拉模型列表 / 内置网关启停」用，全部只监听本机
- 反推节点的面板只占节点内部：原生 widget 全部 `hidden`（仍在 `node.widgets` 里，
  保存/执行链路不变），节点尺寸完全由用户拖动决定（不会自己长高）
- 旧工作流若还留着 `配方保存` / `配方·画廊选择` 节点，重启后会显示为缺失节点，删掉即可：
  把 `model` 链直接接到下游，`image` 链直接连到出图节点

## 反推节点面板（极简风）

顶部只剩两个控件：**主界面** 文字页签 + **模式** 下拉（本地模型 / DeepSeek 云端 / 自定义端点）。
选模式就是选 `provider`，同时跳到那一模式的配置页；主界面页放天天要动的东西。

| 页 | 内容 |
|---|---|
| 主界面 | 运行（最大生成长度 / 多图取第几张 / 反推前清场 / 结束后卸载）· 画廊标签（喂多少 tag）· 提示词规则 rule（默认收起） |
| 本地模型 | 模型（`models/LLM`）+ 量化 |
| DeepSeek 云端 | 账号池列表（禁言/限流/可用徒章 + 单行试跑/移除）、添加账号表单、**思考强度（五档）**、内置网关服务与日志（默认收起） |
| 自定义端点 | 请求地址、API 密钥、**模型下拉 + 拉取模型列表**、手填模型名（收起）、**思考强度（五档）**、**流式开关（默认非流式）** |

视觉：一根主色 + 三级灰度 + 1px 发丝线，没有卡片盒；输入框只有下划线，开关细线条，
次要信息（服务日志、手填模型、规则）一律收进 `details`。

### 自定义 OpenAI 兼容端点

- **拉模型列表**：`GET {base}/models`（面板同源路由 `/assembled-recipes/ds/models`），
  结果按 base URL 缓在 localStorage；下拉里永远包含当前值（即使是手填的），
  拉到的列表若不认当前模型名会自动选第一个。
- **默认非流式**：反推要的是整块 JSON，流式只会多一层拼接。开了流式就逐帧拼 `delta.content`，
  `reasoning_content`（推理）不会混进正文；若端点声称流式却一帧正文都不给，直接报错提示改回非流式。
- **思考强度（五档）**：`不思考 / minimal / low / medium / high / max`，**两个云端通道共用一个参数**：
  - 自定义端点 → 原值发 `reasoning_effort`；选「不思考」则**不发这个字段**（很多端点对未知字段会 400）。
    端点不认该字段时自动去掉重打一次（控制台会说），不会误报成“端点挂了”。
  - DeepSeek 云端 → 官方只有 `thinking` 一个布尔开关，没有强度档，所以收敛：`medium/high/max` 算开、
    `minimal/low` 算关、「不思考」= 不覆盖，回落到「深度思考」那个开关。
  - 规则只住在 `providers.effort_to_thinking()` 一处，节点与内置网关都用它，不会两边飘。
- **代理铁律分级**：目标是本机地址（127.0.0.1 / localhost / ::1）时**不套代理**（它不出网卡，
  套机场反而会被 LM Studio / llama.cpp 拒接）；目标是外部地址时一律走代理，检不到代理直接报错。
- 地址形状容错：`http://host:port` / `…/` / `…/v1` / `…/v1/chat/completions` 四种写法都能拼对。

## 内置 DeepSeek 网关（云端反推，零外部依赖）

反推节点的 `provider = DeepSeek 云端（内置网关）` 用的是插件自带的纯 Python 客户端
（`reverse_cloud/direct.py` + `pow.py`），**不需要 ds2api.exe、不需要任何 Go/Rust 网关**，
依赖只有 `httpx / numpy / cryptography`（ComfyUI 环境里本来就有）。

协议（逐条本机实弹验过）：

| 环节 | 做法 |
|---|---|
| 登录 | `POST /api/v0/users/login`，请求体 AES-128-CBC 加密，key‖iv 用 RSA-OAEP(SHA-256) 封在 `x-request-encryption-key`；头为 Android App 形状 |
| 会话 | `POST /api/v0/chat_session/create` |
| 防机器 | `POST /api/v0/chat/create_pow_challenge` → 解 `DeepSeekHashV1`（= SHA3-256 但跳过 Keccak 第 0 轮）→ `x-ds-pow-response`；numpy 批量搜 nonce，difficulty 144000 约 0.5s。**challenge 是一次性的**，每枪都重新取 |
| 看图 | `POST /api/v0/file/upload_file`（multipart + `x-model-type` + PoW）→ `file_id` → completion 的 `ref_file_ids`；所以云端是完整视觉反推 |
| 输出 | 原生 SSE。正文与推理在收尾的 `{"p":"response/fragments","v":[{id,type,content}]}` 里才分开；裸 `{"v":"…"}` 是打字机流（推理和正文混在一起），只当兵底 |

坑位实录（推翻上一版结论）：

- **风控关卡是 `device_id`，不是 TLS/H2 指纹**。编造的 device_id 一律 `RISK_DEVICE_DETECTED`；
  换成真实浏览器数美 SDK 抓到的那串后，**裸 `httpx`（不带任何指纹伪装）登录与补全全部通过**。
  所以本实现不需要 `curl_cffi`/uTLS 之类。device_id 只能自己抓（浏览器 F12 看 `deviceprofile/v4`），
  插件不能代劳。
- **出口铁律**：客户端启动时自动读环境变量 → Windows 系统代理；**找不到代理直接报错**，绝不裸走真实 IP。
- 短时间连打多枪会回 `TOO_MANY_REQUESTS`；单账号时自己退避重试，多账号时直接转下一个。
- 上游报错不走事件流而是回一个 `application/json` 信封（实测禁言就是这个形状），
  插件会当场抛出来并把 `mute_until` 翻成具体时间，不会静默交空正文。

凭据存放（重要）：账号池（邮箱/密码/device_id）存
`<ComfyUI>/user/AssembledRecipes/ds_gateway.json`（本地文件，插件自管），
**不写进工作流 JSON、不进日志、不进提示词**。日志里只会出现邮箱与耗时。

### 多账号轮巡

一个 DeepSeek 账号的额度与风控是共享的（实测单账号连续用几小时会被 `user is muted`
禁言 3 天），所以网关把账号当**池子**用，面板「DeepSeek 云端」页里可以加任意多个：

| 情况 | 行为 |
|---|---|
| 平时 | 按顺序轮巡，每成功一枪就把游标推到下一个——不拆死一号，风控压力摊开 |
| 某号被禁言 | 按上游给的 `mute_until` 记账并跳过，到期自动回来（面板行上直接写“禁言 · 到 11-19 09:54”） |
| 某号被限流 | 进 120s 短冷却后跳过（协议层不再自己睡 15-60s，有下一个号就用下一个） |
| 整池不可用 | 报错文案把**每个账号**的状态与最早恢复时间列出来，不会只说“失败了” |
| 缺 device_id 的号 | 不参与轮巡（上去也必被 RISK_DEVICE_DETECTED 打回来） |

状态（禁言/冷却/成功次数）持久化在同一个配置文件里，重启 ComfyUI 不会忘。
`POST /assembled-recipes/ds/unbind` 带 `email` 只删那一个，不带就清空整池。

同时它还开了一个 OpenAI 兼容服务（`http://127.0.0.1:5812/v1`，仅监听本机，`Authorization: Bearer <配置里的 key>`），
带 `stream` 与 base64 图片；反推节点本体走进程内直调，不依赖这个端口。

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