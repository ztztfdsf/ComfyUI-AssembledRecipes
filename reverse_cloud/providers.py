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
                openai_model: str = "", thinking: bool = True) -> str:
    """provider: local / deepseek / openai。

    local:    调 local_runner(pil, system_rule, user_text)（节点里现有的 transformers 路径）
    deepseek: 内置网关——真实图片上传 + DeepSeek 补全（进程内直调，不开端口也能用）
    openai:   标准视觉 chat.completions（任意 OpenAI 兼容端点）
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
        if not openai_base:
            raise ProviderError("OpenAI 自定义模式需要填请求地址")
        return _openai_chat(openai_base, openai_key, openai_model, system_rule, user_text, pil)
    raise ProviderError(f"未知 provider: {provider}")


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
