"""统一反推入口：local（transformers 进程内）/ deepseek（内置网关直连 DeepSeek）/ openai（自定义端点）。

三个 provider 都吐同一格式：模型原始文本（JSON 数组，可能带思考前缀），
由下游 extract_json_array / ReverseJsonSanitize 清洗。

DeepSeek 云端可以直接看图：官方客户端的图片通道是
  POST /api/v0/file/upload_file → file_id → completion 的 ref_file_ids
（本机实弹验证过，实现见 reverse_cloud/direct.py），所以云端模式是完整的视觉反推。
网关由本包的 gateway.py 提供（纯 Python），不再依赖外部 ds2api.exe。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from urllib.parse import urlsplit


class ProviderError(RuntimeError):
    pass


# ---------------------------------------------------------------- OpenAI 兼容

def _pil_to_data_url(pil, max_side=1024, fmt="JPEG", quality=90) -> str:
    from PIL import Image
    im = pil
    if max(im.size) > max_side:
        r = max_side / max(im.size)
        im = im.resize((max(1, round(im.width * r)), max(1, round(im.height * r))), Image.LANCZOS)
    buf = io.BytesIO()
    if fmt == "JPEG" and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(buf, fmt, quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/{fmt.lower()};base64,{b64}"


def _endpoint(base_url: str, tail: str) -> str:
    """把用户填的 base 拼成具体端点（允许已经写了完整路径）。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ProviderError("OpenAI 自定义模式需要填请求地址")
    if url.endswith(tail):
        return url
    if url.endswith("/v1"):
        return url + "/" + tail
    return url + "/v1/" + tail


def _client(url: str, timeout: float):
    """建 httpx 客户端。

    对外端点：一律走代理，检不到直接报错（绝不裸走真实出口 IP）。
    本地端点（127.0.0.1 / localhost / ::1）：不走代理——它根本不出网卡，
    套上机场反而会被它拒接（很多人把自定义端点填成本机 LM Studio / llama.cpp）。
    """
    import httpx
    try:
        from . import direct
    except ImportError:
        import direct  # type: ignore
    host = (urlsplit(url).hostname or "").lower()
    local = host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")
    proxy = None if local else direct.detect_proxy()
    return httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True)


def list_models(base_url: str, api_key: str = "", timeout: float = 30.0) -> list:
    """GET {base}/models → 模型 id 列表（面板的「拉取模型列表」用）。"""
    url = _endpoint(base_url, "models")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with _client(url, timeout) as cli:
        r = cli.get(url, headers=headers)
    if r.status_code != 200:
        raise ProviderError(f"拉模型列表 HTTP {r.status_code}: {r.text[:240]}")
    try:
        data = r.json()
    except ValueError:
        raise ProviderError(f"模型列表不是 JSON: {r.text[:200]}")
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ProviderError(f"模型列表形状不认识: {json.dumps(data, ensure_ascii=False)[:200]}")
    out = []
    for row in rows:
        if isinstance(row, str):
            out.append(row)
        elif isinstance(row, dict):
            mid = row.get("id") or row.get("name") or row.get("model")
            if mid:
                out.append(str(mid))
    return sorted(set(out))


_PARAM_WORDS = ("reasoning_effort", "reasoning", "thinking", "enable_thinking",
                "thinking_budget", "chat_template_kwargs", "budget_tokens")
_REJECT_WORDS = ("invalid", "unknown", "unsupported", "not supported", "extra inputs",
                 "extra fields", "unexpected", "unrecognized", "not permitted", "not allowed",
                 "invalid value", "is not allowed", "不支持", "无法识别", "参数错误")


def _looks_like_rejected_param(text: str) -> bool:
    """端点拒接某个「思考参数」的常见回词（各家 400 文案五花八门，只认关键词）。"""
    t = (text or "").lower()
    return any(k in t for k in _PARAM_WORDS) and any(k in t for k in _REJECT_WORDS)


def _looks_like_rejected_effort(text: str) -> bool:
    """向后兼容的旧名字（只针对 reasoning_effort）。"""
    return _looks_like_rejected_param(text) and "reasoning_effort" in (text or "").lower()


# ---------------------------------------------------------------- 拒答识别
_REFUSAL_STRONG = (
    "我无法协助", "无法协助", "我不能处理", "不能处理", "我不会为", "不会为这类", "无法提供",
    "不会生成", "不能生成", "抱歉，我", "很抱歉", "拒绝", "无法满足", "不合适处理", "涉及敏感",
    "i can't", "i cannot", "i can\'t assist", "cannot assist", "can\'t assist", "i won\'t",
    "i am unable", "i\'m unable", "not able to help", "unable to help", "cannot help with",
    "i must decline", "against my guidelines", "violates", "content policy", "safety policy",
)


def looks_like_refusal(text: str) -> bool:
    """模型拒答 vs 格式不合格：拒答是「一段人话、没有 JSON 结构」。"""
    t = (text or "").strip()
    if not t or len(t) > 1500:
        return False
    if "{" in t or "[" in t:
        return False
    low = t.lower()
    return any(k.lower() in low for k in _REFUSAL_STRONG)


def classify_failure(text: str) -> tuple:
    """给上层一个确定分类：(kind, 人话说明)。kind: refusal / empty / format。"""
    t = (text or "").strip()
    if not t:
        return ("empty", "模型返回空正文")
    if looks_like_refusal(t):
        return ("refusal", "模型拒答（内容安全过滤），不是格式问题")
    return ("format", "正文里抠不出 JSON 数组（模型没按格式回答）")


def coerce_text(v) -> str:
    """把各家返回的「正文」统一成 str。

    为什么需要它：标准 OpenAI 的 message.content 是字符串，但不少兼容端点（含视觉多段
    输出、部分网关）会返回 parts 数组：[{"type": "text", "text": "..."}]。
    直接把这个 list 往下传，会在 check_layout 里炸成
    AttributeError: 'list' object has no attribute 'strip'（2026-11-16 实测）。
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        out = []
        for part in v:
            if isinstance(part, str):
                out.append(part)
                continue
            if isinstance(part, dict):
                # 思考段必须跳过：有的网关把 reasoning_effort 的产物作为独立 part 返回
                # （{"type": "thinking", "thinking": "...", "closed": true}），
                # 混进正文会把 JSON 泡成"思考过程 + 正文"，check_layout 直接判不合格。
                ptype = str(part.get("type") or "").lower()
                if ptype in ("thinking", "reasoning", "reasoning_content", "analysis"):
                    continue
                t = part.get("text")
                if t is None:
                    t = part.get("content")
                if isinstance(t, dict):
                    t = t.get("text")
                out.append(coerce_text(t))
                continue
            out.append(coerce_text(part))
        return "".join(out)
    if isinstance(v, dict):
        for k in ("text", "content", "output_text", "caption", "message"):
            if k in v:
                return coerce_text(v[k])
        return ""
    return str(v)


# ---------------------------------------------------------------- 思考强度方言
# 各家「让模型想多久」的请求格式完全不同：同一个 reasoning_effort 在别家就是 400。
# 下表形状照各家官方文档抄（2026-11 核对），不是猜的：
#   reasoning_effort      OpenAI GPT-5 系 Chat Completions：low/medium/high（新模型另有 none/minimal/xhigh）
#   reasoning_obj         OpenAI Responses 风格 / OpenRouter 统一参数：{"reasoning": {"effort": ...}}
#   openrouter_budget     OpenRouter 另一条路：{"reasoning": {"max_tokens": N}}
#   thinking_budget       Anthropic：{"thinking": {"type": "enabled", "budget_tokens": N}}（最小 1024）
#   thinking_type         DeepSeek 官方 / GLM / Kimi / MiniMax / 豆包：{"thinking": {"type": "enabled"}}
#   enable_thinking       Qwen-DashScope：{"enable_thinking": true, "thinking_budget": N}
#   chat_template_kwargs  vLLM / SGLang 本地部署：{"chat_template_kwargs": {"enable_thinking": true}}
# Gemini 原生只有 generationConfig.thinkingConfig.thinkingBudget，它的 OpenAI 兼容层不开放该字段，
# 所以兼容模式按 reasoning_effort / 无参数处理，不做假支持。
EFFORT_UNSET = "不思考"
EFFORT_LEVELS = ("不思考", "minimal", "low", "medium", "high", "max")
EFFORT_BUDGET = {"minimal": 1024, "low": 2048, "medium": 4096, "high": 8192, "max": 16384}
EFFORT_OPENAI = {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "max": "high"}
DIALECTS = ("reasoning_effort", "reasoning_obj", "thinking_budget", "thinking_type",
            "enable_thinking", "chat_template_kwargs", "openrouter_budget")
DIALECT_LABELS = {
    "reasoning_effort": 'reasoning_effort="high"',
    "reasoning_obj": 'reasoning={"effort":"high"}',
    "openrouter_budget": 'reasoning={"max_tokens":8192}',
    "thinking_budget": 'thinking={"type":"enabled","budget_tokens":8192}',
    "thinking_type": 'thinking={"type":"enabled"}',
    "enable_thinking": 'enable_thinking=true + thinking_budget=8192',
    "chat_template_kwargs": 'chat_template_kwargs={"enable_thinking":true}',
}


def thinking_fields(dialect: str, effort: str) -> dict:
    """按方言生成要 merge 进请求体的思考参数。"""
    if not effort or effort == EFFORT_UNSET:
        return {}
    if effort not in EFFORT_BUDGET:
        raise ProviderError("思考强度不认识：%r（可选 %s）" % (effort, " / ".join(EFFORT_LEVELS)))
    budget = EFFORT_BUDGET[effort]
    if dialect == "reasoning_effort":
        return {"reasoning_effort": EFFORT_OPENAI[effort]}
    if dialect == "reasoning_obj":
        return {"reasoning": {"effort": EFFORT_OPENAI[effort]}}
    if dialect == "openrouter_budget":
        return {"reasoning": {"max_tokens": budget}}
    if dialect == "thinking_budget":
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    if dialect == "thinking_type":
        return {"thinking": {"type": "enabled"}}
    if dialect == "enable_thinking":
        return {"enable_thinking": True, "thinking_budget": budget}
    if dialect == "chat_template_kwargs":
        return {"chat_template_kwargs": {"enable_thinking": True}}
    return {}


def _dialect_cache_file() -> str:
    try:
        from . import endpoints
    except ImportError:
        import endpoints  # type: ignore
    return os.path.join(os.path.dirname(endpoints.config_path()), "thinking_dialects.json")


def _cache_read() -> dict:
    p = _dialect_cache_file()
    try:
        with io.open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cache_write(d: dict) -> None:
    p = _dialect_cache_file()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:  # 缓存写不进去不影响出图
        print("[AssembledRecipes] 思考方言缓存写入失败：%s" % e, flush=True)


def _cache_key(base_url: str, model: str) -> str:
    return "%s|%s" % ((base_url or "").strip().rstrip("/").lower(), (model or "").strip())


def remember_dialect(base_url: str, model: str, dialect: str) -> None:
    d = _cache_read()
    d[_cache_key(base_url, model)] = {"dialect": dialect, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _cache_write(d)


def recall_dialect(base_url: str, model: str) -> str:
    row = _cache_read().get(_cache_key(base_url, model)) or {}
    return str(row.get("dialect") or "")


def record_refusal(base_url: str, model: str, text: str) -> None:
    """面板要能看见「上次为什么没出框」：是模型拒答，不是格式问题。"""
    d = _cache_read()
    k = _cache_key(base_url, model)
    row = d.get(k) or {}
    row["refusal"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "text": (text or "")[:300]}
    d[k] = row
    _cache_write(d)


def thinking_state() -> dict:
    """给面板：每个端点上次命中的方言 + 最近一次拒答。"""
    out = {}
    for k, row in (_cache_read() or {}).items():
        if not isinstance(row, dict):
            continue
        dia = row.get("dialect") or ""
        out[k] = {"dialect": dia,
                  "label": DIALECT_LABELS.get(dia, dia or "未探测"),
                  "at": row.get("at") or "",
                  "refusal": row.get("refusal") or None}
    return out


def dialect_candidates(base_url: str, model: str) -> list:
    """尝试顺序：缓存命中优先，其次按域名/模型名启发，最后一律兜到 reasoning_effort。"""
    hit = recall_dialect(base_url, model)
    host = (urlsplit(base_url or "").hostname or "").lower()
    m = (model or "").lower()
    if "openrouter" in host:
        base = ["reasoning_obj", "openrouter_budget", "reasoning_effort"]
    elif "anthropic" in host or m.startswith("claude"):
        base = ["thinking_budget", "reasoning_effort"]
    elif any(k in host for k in ("dashscope", "aliyuncs", "qwen")) or "qwen" in m:
        base = ["enable_thinking", "thinking_type", "reasoning_effort"]
    elif any(k in host for k in ("bigmodel", "z.ai", "moonshot", "minimax", "volces", "volcengine")):
        base = ["thinking_type", "reasoning_effort", "enable_thinking", "thinking_budget"]
    else:
        # 未知域名（第三方网关，如本机在用的 chenxing.pro）：reasoning_effort 优先——
        # 这类网关绝大多数就是 OpenAI 兼容层，认它；发 thinking{type} 反而会被它们
        # 转成上游请求然后回一个不带参数名的 422/400 泛错（实测）。
        # 模型名只当兜底顺序的提示，不当首选。
        base = ["reasoning_effort", "reasoning_obj", "thinking_type", "thinking_budget",
                "enable_thinking", "chat_template_kwargs"]
        if "glm" in m or "kimi" in m or "minimax" in m or "doubao" in m:
            base = ["thinking_type"] + [d for d in base if d != "thinking_type"]
    order = ([hit] if hit else []) + [d for d in base if d != hit]
    return [d for d in order if d in DIALECTS]


def _openai_chat(base_url: str, api_key: str, model: str, system: str, user_text: str,
                 pil, temperature: float = 1.0, timeout: float = 300.0,
                 data_url: str | None = None, stream: bool = False,
                 effort: str = "") -> str:
    """标准 OpenAI 视觉补全。默认非流式（反推要整块 JSON，流式只会多一层拼接）。

    思考强度按方言逐个试：命中就记进本机缓存，下次直接用它，不再多打一枪。
    全部方言被拒时**报错**而不是静默去掉参数——思考开没开是用户看得见的功能，
    悄悄降级等于骗人。"""
    url = _endpoint(base_url, "chat/completions")
    content: list = [{"type": "text", "text": user_text}]
    if pil is not None:
        content.insert(0, {"type": "image_url",
                           "image_url": {"url": data_url or _pil_to_data_url(pil)}})
    body = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else [])
        + [{"role": "user", "content": content}],
        "temperature": temperature,
        "stream": bool(stream),
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if effort and effort != EFFORT_UNSET:
        trials = [(d, thinking_fields(d, effort)) for d in dialect_candidates(base_url, model)]
    else:
        trials = [("", {})]

    rejected = []
    with _client(url, timeout) as cli:
        for dialect, fields in trials:
            trial = dict(body)
            trial.update(fields)
            if not stream:
                r = cli.post(url, json=trial, headers=headers)
                if r.status_code == 200:
                    if dialect:
                        remember_dialect(base_url, model, dialect)
                        print("[AssembledRecipes] 思考强度 %s → 方言 %s（%s）"
                              % (effort, dialect, DIALECT_LABELS.get(dialect, dialect)), flush=True)
                    try:
                        raw = r.json()["choices"][0]["message"]["content"]
                    except Exception as e:
                        raise ProviderError(f"OpenAI 响应解析失败: {e} :: {r.text[:300]}")
                    if isinstance(raw, (list, tuple)):
                        print("[AssembledRecipes] 端点把 content 返回成 %d 段数组，已拼接成一段正文"
                              % len(raw), flush=True)
                    return coerce_text(raw)
                # 400/422 = 参数层面被拒；有的网关只回 {"error":"openai_error"} 不点名，
                # 所以不能只靠关键词判断，状态码也要算进去，否则会误报「端点挂了」。
                if fields and (r.status_code in (400, 422) or _looks_like_rejected_param(r.text)):
                    rejected.append((dialect, r.status_code, r.text[:160]))
                    print("[AssembledRecipes] 方言 %s 被拒（HTTP %s），换下一种"
                          % (dialect, r.status_code), flush=True)
                    continue
                raise ProviderError(f"OpenAI 端点 HTTP {r.status_code}: {r.text[:300]}")
            # 流式
            with cli.stream("POST", url, json=trial, headers=headers) as r:
                if r.status_code != 200:
                    raw = r.read()
                    txt = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                    if fields and (r.status_code in (400, 422) or _looks_like_rejected_param(txt)):
                        rejected.append((dialect, r.status_code, txt[:160]))
                        print("[AssembledRecipes] 方言 %s 被拒（HTTP %s），换下一种"
                              % (dialect, r.status_code), flush=True)
                        continue
                    raise ProviderError(f"OpenAI 端点 HTTP {r.status_code}: {txt[:300]}")
                if dialect:
                    remember_dialect(base_url, model, dialect)
                chunks, bad = [], 0
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        bad += 1
                        continue
                    for ch in obj.get("choices") or []:
                        delta = ch.get("delta") or {}
                        piece = coerce_text(delta.get("content")) or coerce_text(ch.get("text"))
                        if piece:
                            chunks.append(piece)
            text = "".join(chunks)
            if not text:
                raise ProviderError(
                    f"流式没拿到正文（{bad} 行解不进去）——这个端点可能不支持 stream，改回非流式")
            return text

    lines = "; ".join("%s → HTTP %s %s" % (d, c, t.replace(chr(10), " ")) for d, c, t in rejected)
    raise ProviderError(
        "思考强度 %r 送不出去：试过的方言全被端点拒接（%s）。"
        "把「思考强度」改成「不思考」再跑，或者告诉我这个端点认哪种参数，我加进方言表。"
        % (effort, lines or "无"))

# ---------------------------------------------------------------- 统一入口

def run_caption(provider: str, pil, system_rule: str, user_text: str,
                local_runner=None, openai_base: str = "", openai_key: str = "",
                openai_model: str = "", thinking: bool = True,
                stream: bool = False, effort: str = "", endpoint: str = "") -> str:
    """provider: local / deepseek / openai。

    local:    调 local_runner(pil, system_rule, user_text)（节点里现有的 transformers 路径）
    deepseek: 内置网关——真实图片上传 + DeepSeek 补全（进程内直调，不开端口也能用）
    openai:   标准视觉 chat.completions（任意 OpenAI 兼容端点）；
              给了 endpoint 就从本地端点库取 base/key/model（密钥不进工作流也不进日志），
              直传的 base/key 只当覆盖项用。
    """
    if provider == "local":
        if local_runner is None:
            raise ProviderError("local provider 没给 local_runner")
        return local_runner(pil, system_rule, user_text)
    if provider == "deepseek":
        try:
            from . import gateway
        except ImportError:  # 包名导入失败时（前端直接加载模块）兜底
            import gateway  # type: ignore
        return gateway.caption(pil, system_rule, user_text, thinking=thinking)
    if provider == "openai":
        if endpoint and endpoint != "（工作流内联）":
            try:
                from . import endpoints
            except ImportError:
                import endpoints  # type: ignore
            row = endpoints.resolve(endpoint)   # 取不到会报错并列出可用名字
            openai_base = openai_base or row["base"]
            openai_key = openai_key or (row.get("key") or "")
            openai_model = openai_model or (row.get("model") or "")
        return _openai_chat(openai_base, openai_key, openai_model, system_rule, user_text, pil,
                            stream=stream, effort=effort)
    raise ProviderError(f"未知 provider: {provider}")


def effort_to_thinking(effort: str, default: bool = True) -> bool:
    """五档思考强度 → DeepSeek 的布尔 thinking。

    DeepSeek 官方只有 thinking_enabled 一个开关，没有强度档位，所以这里收敛：
    medium/high/max 算开，minimal/low 算关；「不思考」/空 = 不覆盖，回落到调用方的默认值。
    """
    eff = str(effort or "").strip().lower()
    if eff in ("", "不思考", "none", "auto"):
        return bool(default)
    return eff in ("medium", "high", "max")


def extract_json_array(text: str):
    """从模型输出里抠出第一个可解析的 JSON 数组（思考前缀/代码块/字符串里带括号也能抠）。"""
    s = (text or "").strip()
    # 去代码块
    if "```" in s:
        parts = s.split("```")
        for p in parts:
            p2 = p.strip()
            if p2.startswith("json"):
                p2 = p2[4:].strip()
            if p2.startswith("["):
                s = p2
                break
    # 逐个 [ 开试（云端带推理前缀时，第一个 [ 可能不是真数组的开头）。
    # 只认「非空对象数组」：布局 JSON 的元素必为 dict。
    # 找不到就返回 None——宁可不改原文、让下游清洗节点拿到完整文本，
    # 也不能退而求其次拿 bbox（那会把四个数字当成品交下去）。
    for start in range(len(s)):
        if s[start] != "[":
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(s[start:i + 1])
                    except ValueError:
                        break  # 这个 [ 不行，换下一个
                    if isinstance(v, list) and v and all(isinstance(o, dict) for o in v):
                        return v
                    break
        # 内层走完没闭合，继续找下一个 [
    return None


def salvage_objects(text: str):
    """模型把框一个个吐出来、但整份没套数组外壳（实测 gemma-4-E2B 就这样）。

    这不是「没按格式回答」到不可用：顶层 {…} 对象逐个扫出来再包一层 [ ] 就能用。
    只认顶层对象（字符串感知扫描，不会被 desc 里的括号带偏），且必须至少一个。
    """
    s = (text or "").strip()
    objs, depth, start = [], 0, -1
    in_str = esc = False
    for i, c in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    v = json.loads(s[start:i + 1])
                except ValueError:
                    v = None
                if isinstance(v, dict):
                    objs.append(v)
                start = -1
    return objs if objs and all(("bbox" in o) for o in objs) else None


def parse_layout(text: str):
    """把反推正文解析成 (summary, items)。两种形状都收：
       · 新版（现行默认规则要求的）：{"summary": "...", "items": [...]}
       · 旧版/模型偷懒：裸数组 [...]，此时 summary 为空串
    解析不出来返回 None。不做字段校验（校验在 check_layout）。
    """
    raw = coerce_text(text).strip()
    if not raw:
        return None
    # 剥 markdown 围栏
    if "```" in raw:
        for part in raw.split("```"):
            q = part.strip()
            if q.lower().startswith("json"):
                q = q[4:].strip()
            if q[:1] in ("[", "{"):
                raw = q
                break
    # 先试对象（新版）
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        try:
            v = json.loads(raw[i:j + 1])
        except ValueError:
            v = None
        if isinstance(v, dict) and ("items" in v or "summary" in v):
            items = v.get("items")
            if isinstance(items, str):
                try:
                    items = json.loads(items)
                except ValueError:
                    items = None
            summ = v.get("summary")
            # 官方 schema 里 summary 是对象 {"caption": ..., "canvas": ..., "meta": [...]}；
            # 直接 str() 会把整坨 dict 塞进 summary 输出口，下游拼提示词就会看到 {'caption': '...'}，
            # 所以对象形态先取 caption，取不到再退化成用户给的 summary 字符串
            if isinstance(summ, dict):
                summ = (summ.get("caption") or summ.get("summary")
                        or summ.get("description") or "")
            return str(summ or "").strip(), (items if isinstance(items, list) else [])
    arr = extract_json_array(raw)
    if arr is not None:
        return "", arr
    salv = salvage_objects(raw) if raw.count("[") <= raw.count("]") else None
    if salv:
        return "", salv
    return None


def check_layout(text: str):
    """云端返回的正文到底是不是一份能用的布局 JSON。

    返回 (boxes, problem, note)：problem 非 None = 不可用，调用方必须直接停链路
    （而不是把一堆废话交给下游清洗节点静默变空布局）；note 是「修好了什么」的说明，
    要打印出来——修过的东西不能悄悄修。
    """
    raw = coerce_text(text).strip()
    if not raw:
        return None, "模型返回空正文", ""
    parsed = parse_layout(raw)
    note = ""
    if parsed is None:
        arr = None
    else:
        arr = parsed[1]
    if arr is None:
        # 截断优先判：少掉的是内容不是噪声，宁可停也不要拿半份布局去出图
        if raw.count("[") > raw.count("]"):
            got = len(salvage_objects(raw) or [])
            return None, ("JSON 数组没闭合（输出被 token 上限截断）" +
                          ("，前 %d 个框是完整的但后面的框已经丢了" % got if got else "")), ""
        # 没截断、只是整份没套数组外壳（gemma-4-E2B 实测这样）：纯机械问题，救回来
        salv = salvage_objects(raw)
        if salv:
            return salv, None, "模型没套数组外壳，已按顶层对象补齐（%d 个框）" % len(salv)
        return None, "正文里抠不出 JSON 数组（模型没按格式回答）", ""
    bad = []
    for i, o in enumerate(arr):
        b = o.get("bbox")
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            bad.append("第%d个框缺 bbox" % (i + 1))
        elif not str(o.get("desc", "")).strip():
            bad.append("第%d个框缺 desc" % (i + 1))
    if bad:
        return None, "框字段不完整：" + "、".join(bad[:4]), ""
    return arr, None, note


def prompt_provenance(rule: str, user_text: str, **params) -> str:
    """把「这次到底喂了什么提示词」写成一段可贴进报错/日志的说明。

    user_prompt 是上游 Any Switch 合并进来的一个字符串，节点本身不知道它来自画廊还是手写框，
    所以这里按形状判：逗号分隔的小写 tag 串 → 画廊标签；否则 → 手写/选择器词。
    """
    import hashlib

    def _h(v):
        return hashlib.sha1(str(v).encode("utf-8", "ignore")).hexdigest()[:8]

    r = str(rule or "")
    u = str(user_text or "")
    tags = [x.strip() for x in u.replace("\n", ",").split(",") if x.strip()]
    is_tags = len(tags) >= 3 and all(
        (not t.startswith("<")) and (t == t.lower() or " " not in t) for t in tags[:6])
    src = "画廊标签（%d 个 tag）" % len(tags) if is_tags else "手写/选择器提示词"
    lines = ["提示词来源与形状："]
    lines.append("  rule 规则：%d 字（sha1 %s），首行「%s」" %
                 (len(r), _h(r), (r.splitlines() or [""])[0][:40]))
    # 额外要求是整段 rule 里唯一「本次现写、下次不会一样」的东西，必须单独摊出来；
    # 否则出错时只能看到 rule 总字数变了，看不出到底追加了什么约束
    _mk = "# 额外要求（本次"
    if _mk in r:
        _extra = r.split(_mk, 1)[1]
        _extra = _extra.split("\n", 1)[1] if "\n" in _extra else _extra
        lines.append("  额外要求（本次）：%s" % (_extra.strip().replace("\n", " ") or "（空）"))
    lines.append("  user_prompt：%s，%d 字（sha1 %s）" % (src, len(u), _h(u)))
    lines.append("  内容：%s" % (u[:200].replace("\n", " ") + ("…" if len(u) > 200 else "")))
    for k, v in params.items():
        if v is not None and v != "":
            lines.append("  %s：%s" % (k, v))
    return "\n".join(lines)
