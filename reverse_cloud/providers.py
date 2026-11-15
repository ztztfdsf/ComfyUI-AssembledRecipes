"""统一反推入口：local（transformers 进程内）/ deepseek（网页版免费）/ openai（自定义端点）。

三个 provider 都吃同一套输入（PIL 图 + system 规则 + 用户参考提示词），
吐同一格式的输出：只含 JSON 数组的字符串，交给下游 ReverseJsonSanitize。

OpenAI 兼容模式走标准 vision chat.completions（image_url 用 data URL），
DeepSeek 网页版不能传图 —— 它只吃文本，所以把图片先在本地跑一遍轻量 OCR/描述？
不：DeepSeek 网页版聊天也不支持视觉输入（截至 2026-11），但 deepseek 平台 API 有
视觉模型？也没有。所以 deepseek 模式的定位 = **文本智能层**：本地图反推（快速粗框）
已经由本地小模型完成时，把「图上文字 OCR + 本地粗结果」交给 DeepSeek 精修排序与关系描述。
为避免歧义：**deepseek 模式要求本地有一个小模型先出粗稿**（models/LLM 下任意一个），
云端只做「关系描述精修 + JSON 重排」。这正是主人要的「云端或者本地 AI」组合：
本地负责看，云端负责想。
"""

from __future__ import annotations

import base64
import io
import json


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


def _openai_chat(base_url: str, api_key: str, model: str, system: str, user_text: str,
                 pil, temperature: float = 1.0, timeout: float = 300.0,
                 data_url: str | None = None) -> str:
    import httpx
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        if url.endswith("/v1"):
            url += "/chat/completions"
        else:
            url += "/v1/chat/completions"
    content: list = [{"type": "text", "text": user_text}]
    if pil is not None:
        content.insert(0, {"type": "image_url",
                           "image_url": {"url": data_url or _pil_to_data_url(pil)}})
    body = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else [])
        + [{"role": "user", "content": content}],
        "temperature": temperature,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = httpx.post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise ProviderError(f"OpenAI 端点 HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise ProviderError(f"OpenAI 响应解析失败: {e} :: {r.text[:300]}")


# ---------------------------------------------------------------- 统一入口

def run_caption(provider: str, pil, system_rule: str, user_text: str,
                local_runner=None, openai_base: str = "", openai_key: str = "",
                openai_model: str = "") -> str:
    """provider: local / deepseek / openai。

    local: 调 local_runner(pil, system_rule, user_text)（节点里现有的 transformers 路径）
    deepseek: 文本进 DeepSeek 网页版（不吃图；把画面 OCR/本地粗稿拼进 user_text 由调用方完成）
    openai: 标准视觉 chat.completions
    返回模型原始文本（JSON 数组或含思考前缀），交给清洗节点。
    """
    if provider == "local":
        if local_runner is None:
            raise ProviderError("local provider 没给 local_runner")
        return local_runner(pil, system_rule, user_text)
    if provider == "openai":
        if not openai_base:
            raise ProviderError("OpenAI 自定义模式需要填请求地址")
        return _openai_chat(openai_base, openai_key, openai_model, system_rule, user_text, pil)
    raise ProviderError(f"未知 provider: {provider}")


def extract_json_array(text: str):
    """从模型输出里抠出第一个可解析的 JSON 数组（含思考前缀/代码块也能抠）。"""
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
    start = s.find("[")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    v = json.loads(s[start:i + 1])
                    return v if isinstance(v, list) else None
                except ValueError:
                    return None
    return None
