# -*- coding: utf-8 -*-
"""内置 DeepSeek 网关：把「网页/App 协议直连」包成 OpenAI 兼容的本地服务。

节点选「DeepSeek 云端」时打的就是这里（http://127.0.0.1:<port>/v1），
不再需要外部 ds2api.exe。依赖只有 httpx + numpy + cryptography（ComfyUI 环境里都有）。

对外接口（仅监听 127.0.0.1）：
  GET  /v1/models              模型列表
  POST /v1/chat/completions    OpenAI 格式；支持 stream 与 base64 图片（image_url.data URL）
  GET  /admin/status           绑定状态 / 出口代理 / 最近日志
  POST /admin/bind             {email,password,device_id} 绑定账号并试登录
  POST /admin/test             跑一枪极小补全，报连通与耗时
  POST /admin/unbind           解除绑定

账号与 api key 存在 user 目录的 ds_gateway.json（密码只落这个本地文件，绝不进日志/提示词）。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import direct

PORT_DEFAULT = 5812
MODEL_MAIN = "deepseek-v4.1-flash"
MODELS = [MODEL_MAIN, MODEL_MAIN + "-nothinking", MODEL_MAIN + "-search"]

_CONFIG_LOCK = threading.Lock()
_STATE = {
    "server": None,
    "thread": None,
    "port": None,
    "client": None,
    "error": "",
    "started_at": 0.0,
    "requests": 0,
    "last_ms": 0.0,
}
_LOG = deque(maxlen=200)


def _log(msg: str):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    _LOG.append(line)
    try:
        print("[DS网关] " + line)
    except Exception:
        pass


# ============================ 配置 ============================

def config_path() -> str:
    import folder_paths
    root = os.path.join(folder_paths.get_user_directory(), "AssembledRecipes")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "ds_gateway.json")


def load_config() -> dict:
    path = config_path()
    try:
        with io.open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict):
    cfg.pop("password_echo", None)
    with _CONFIG_LOCK:
        with io.open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


def api_key() -> str:
    cfg = load_config()
    key = str(cfg.get("api_key") or "").strip()
    if not key:
        key = "sk-ar-" + secrets.token_hex(16)
        cfg["api_key"] = key
        save_config(cfg)
    return key


def gateway_base() -> str:
    return "http://127.0.0.1:%d/v1" % (load_config().get("port") or PORT_DEFAULT)


# ============================ 账号客户端 ============================

def get_client() -> direct.DeepSeekClient:
    cfg = load_config()
    account = str(cfg.get("email") or cfg.get("account") or "").strip()
    password = str(cfg.get("password") or "").strip()
    device_id = str(cfg.get("device_id") or "").strip()
    if not account or not password:
        raise direct.DeepSeekError("还没有绑定 DeepSeek 账号（在节点面板的「账号绑定」里填一次）")
    if not device_id:
        raise direct.DeepSeekError("缺少 device_id：必须是真实浏览器抓到的数美指纹，编造的会被上游判 RISK_DEVICE_DETECTED")
    client = _STATE.get("client")
    if client and client.account == account and client.device_id == device_id:
        return client
    if client:
        try:
            client.close()
        except Exception:
            pass
    client = direct.DeepSeekClient(account, password, device_id, proxy=cfg.get("proxy") or None)
    _STATE["client"] = client
    return client


# ============================ 给反推节点的直接入口 ============================

def _json_array_complete(text: str) -> bool:
    """反推要的是闭合的 JSON 数组；被截断时走 continue 续写。"""
    s = (text or "").strip()
    start = s.find("[")
    if start < 0:
        return len(s) > 40  # 没看到数组开头：正文够长就先当完成（避免死循环续写）
    try:
        json.loads(s[start:])
        return True
    except ValueError:
        # 尾巴可能有重复前缀（推理+正文混流），找最后一个 ]
        end = s.rfind("]")
        if end <= start:
            return False
        try:
            json.loads(s[start:end + 1])
            return True
        except ValueError:
            return False


def caption(pil, system_rule: str = "", user_text: str = "", thinking: bool = True,
            max_side: int = 1280, timeout_note: str = "") -> str:
    """PIL 图 + 规则 → 反推原文（不走 HTTP，进程内直调）。

    DeepSeek 原生聊天只有一个 prompt 字段（没有独立 system 位），所以把
    布局规则当 system、把画廊标签当参考资料，拼成一份提示词。
    图片先本地降边转 JPEG 再走官方 file 通道上传。
    """
    from PIL import Image
    im = pil
    if max(im.size) > max_side:
        r = max_side / max(im.size)
        im = im.resize((max(1, round(im.width * r)), max(1, round(im.height * r))), Image.LANCZOS)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)

    parts = []
    if (system_rule or "").strip():
        parts.append(system_rule.strip())
    if (user_text or "").strip():
        parts.append("【参考资料 gallery tags（只用来识别角色/系列，不是要逐个建框）】\n"
                     + user_text.strip())
    parts.append("现在看上传的这张图，严格按上面规则输出布局 JSON（只输出 JSON，不要任何解释）。")
    prompt = "\n\n".join(parts)

    client = get_client()
    t0 = time.time()
    file_id = client.upload_image(buf.getvalue(), filename="layout.jpg", content_type="image/jpeg")
    result = client.completion(prompt, file_ids=[file_id], thinking=bool(thinking),
                              ensure=_json_array_complete)
    ms = round((time.time() - t0) * 1000.0, 1)
    _STATE["last_ms"] = ms
    _STATE["requests"] += 1
    _log("caption ok %dms 出字%d 思考%s" % (ms, len(result["text"]), "on" if thinking else "off"))
    return result["text"]


# ============================ OpenAI 兼容层 ============================

def _extract_prompt_and_images(body: dict):
    """OpenAI messages → (prompt, [image_bytes])；图片支持 data URL 与 http(s) 由节点侧避免。"""
    messages = body.get("messages") or []
    texts, images = [], []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            if content.strip():
                texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    texts.append(str(part["text"]))
                elif part.get("type") == "image_url":
                    url = str((part.get("image_url") or {}).get("url") or "")
                    data = _decode_data_url(url)
                    if data:
                        images.append(data)
    return "\n".join(texts).strip(), images


def _decode_data_url(url: str) -> bytes | None:
    m = re.match(r"data:(image/[a-zA-Z0-9.+-]+);base64,(.+)", url, re.S)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(2))
    except Exception:
        return None


def _model_flags(model: str):
    model = (model or MODEL_MAIN).strip()
    if model.endswith("-nothinking"):
        return MODEL_MAIN, False, False
    if model.endswith("-search"):
        return MODEL_MAIN, False, True
    return MODEL_MAIN, True, False


def run_completion(body: dict) -> dict:
    """非流式：一次补全，返回 OpenAI 形状。"""
    prompt, images = _extract_prompt_and_images(body)
    if not prompt:
        raise direct.DeepSeekError("messages 里没有文本内容")
    model, thinking, search = _model_flags(body.get("model"))
    client = get_client()
    t0 = time.time()
    file_ids = []
    for blob in images[:5]:
        file_ids.append(client.upload_image(blob))
    result = client.completion(prompt, file_ids=file_ids, thinking=thinking, search=search)
    ms = round((time.time() - t0) * 1000.0, 1)
    _STATE["last_ms"] = ms
    _STATE["requests"] += 1
    _log("completion ok %dms 图%d张 出字%d" % (ms, len(file_ids), len(result["text"])))
    return {
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result["text"]},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def stream_completion(body: dict):
    """流式：按 OpenAI SSE 逐块吐字（上游本身就是逐 token 推的）。"""
    prompt, images = _extract_prompt_and_images(body)
    model, thinking, search = _model_flags(body.get("model"))
    client = get_client()
    file_ids = [client.upload_image(b) for b in images[:5]]
    cid = "chatcmpl-" + secrets.token_hex(12)
    box = {"done": False, "err": None, "text": []}

    def worker():
        try:
            client.completion(prompt, file_ids=file_ids, thinking=thinking, search=search,
                              on_delta=lambda s: box["text"].append(s))
        except Exception as e:
            box["err"] = e
        finally:
            box["done"] = True

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    sent = 0
    while True:
        n = len(box["text"])
        if n > sent:
            for piece in box["text"][sent:]:
                yield _sse_chunk(cid, model, piece)
            sent = n
        if box["done"]:
            break
        time.sleep(0.05)
    if box["err"]:
        yield "data: " + json.dumps({"error": {"message": str(box["err"])}}, ensure_ascii=False) + "\n\n"
    yield _sse_chunk(cid, model, "", finish="stop")
    yield "data: [DONE]\n\n"


def _sse_chunk(cid: str, model: str, content: str, finish=None) -> str:
    payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
               "model": model,
               "choices": [{"index": 0, "delta": ({"content": content} if content else {}),
                            "finish_reason": finish}]}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


# ============================ HTTP 服务 ============================

class _Handler(BaseHTTPRequestHandler):
    server_version = "AssembledRecipesDS/1.0"

    def log_message(self, fmt, *args):  # 走自己的日志环，不刷屏控制台
        _log("http %s" % (fmt % args))

    def _send(self, code: int, payload, content_type="application/json"):
        raw = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", content_type + "; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        got = (self.headers.get("authorization") or "").strip()
        token = got[7:].strip() if got.lower().startswith("bearer ") else got
        return not token or token == api_key()  # 只监听 127.0.0.1，空 key 也放行

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "deepseek"} for m in MODELS]})
        elif self.path.startswith("/admin/status"):
            self._send(200, status_payload())
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/chat/completions":
            body = self._body()
            if not self._authorized():
                return self._send(401, {"error": {"message": "bad api key"}})
            if body.get("stream"):
                try:
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream; charset=utf-8")
                    self.send_header("cache-control", "no-cache")
                    self.end_headers()
                    for chunk in stream_completion(body):
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    _log("stream 失败: %s" % e)
                return
            try:
                self._send(200, run_completion(body))
            except Exception as e:
                _log("completion 失败: %s" % (e,))
                self._send(502, {"error": {"message": str(e)[:300]}})
            return
        if path == "/admin/bind":
            self._send(200, bind(self._body()))
            return
        if path == "/admin/test":
            self._send(200, test_connectivity())
            return
        if path == "/admin/unbind":
            self._send(200, stop_and_unbind())
            return
        self._send(404, {"error": {"message": "not found"}})


def status_payload() -> dict:
    cfg = load_config()
    out = {
        "running": _STATE["server"] is not None,
        "port": _STATE["port"] or cfg.get("port") or PORT_DEFAULT,
        "base": gateway_base(),
        "bound": bool(cfg.get("email") and cfg.get("password")),
        "email": str(cfg.get("email") or ""),
        "has_device_id": bool(cfg.get("device_id")),
        "models": MODELS,
        "requests": _STATE["requests"],
        "last_ms": _STATE["last_ms"],
        "error": _STATE["error"],
        "proxy": "",
        "logs": list(_LOG)[-12:],
    }
    try:
        out["proxy"] = direct.detect_proxy()
    except Exception as e:
        out["proxy"] = ""
        out["error"] = str(e)
    return out


def bind(body: dict) -> dict:
    email = str(body.get("email") or body.get("account") or "").strip()
    password = str(body.get("password") or "").strip()
    device_id = str(body.get("device_id") or "").strip()
    cfg = load_config()
    if not email or not password:
        return {"ok": False, "error": "邮箱/密码不能为空"}
    cfg.update({"email": email, "password": password})
    if device_id:
        cfg["device_id"] = device_id
    cfg.setdefault("api_key", api_key())
    cfg.setdefault("port", PORT_DEFAULT)
    save_config(cfg)
    _STATE["client"] = None
    _log("绑定账号 %s（密码已存本地配置，不进日志）" % email)
    return test_connectivity()


def test_connectivity() -> dict:
    t0 = time.time()
    try:
        client = get_client()
        client.login()
        result = client.completion("只回答两个字：收到")
        ms = round((time.time() - t0) * 1000.0, 1)
        _STATE["error"] = ""
        _log("连通正常 %dms" % ms)
        return {"ok": True, "ms": ms, "model": MODEL_MAIN, "echo": result["text"][:20]}
    except Exception as e:
        _STATE["error"] = str(e)[:300]
        _log("连通失败: %s" % (e,))
        return {"ok": False, "error": str(e)[:300],
                "detail": traceback.format_exc(limit=2).strip()[-200:]}


# ============================ 启停 ============================

def start(port: int | None = None) -> dict:
    if _STATE["server"] is not None:
        return {"ok": True, "already": True, "port": _STATE["port"]}
    cfg = load_config()
    port = int(port or cfg.get("port") or PORT_DEFAULT)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        _STATE["error"] = "端口 %d 起不来: %s" % (port, e)
        _log(_STATE["error"])
        return {"ok": False, "error": _STATE["error"]}
    httpd.daemon_threads = True
    th = threading.Thread(target=httpd.serve_forever, name="ds-gateway", daemon=True)
    th.start()
    _STATE.update({"server": httpd, "thread": th, "port": port, "started_at": time.time()})
    if cfg.get("port") != port:
        cfg["port"] = port
        save_config(cfg)
    _log("网关已启动 http://127.0.0.1:%d/v1（key 见本地配置）" % port)
    return {"ok": True, "port": port, "base": gateway_base()}


def stop():
    httpd = _STATE.get("server")
    if httpd is None:
        return {"ok": True, "already": True}
    httpd.shutdown()
    httpd.server_close()
    _STATE.update({"server": None, "thread": None, "port": None})
    _log("网关已停止")
    return {"ok": True}


def stop_and_unbind():
    """解除绑定同时把账号从本地配置抹掉（面板「解除绑定」按钮）。"""
    cfg = load_config()
    for k in ("email", "password", "device_id"):
        cfg.pop(k, None)
    save_config(cfg)
    _STATE["client"] = None
    _log("已解除账号绑定（本地配置里的账密已抹掉）")
    return {"ok": True}


# ============================ ComfyUI 路由（给节点面板） ============================

def register_routes():
    """把面板要用的接口挂在 ComfyUI 自己的端口上（同源，免 CORS）。"""
    import asyncio

    import aiohttp
    from server import PromptServer

    routes = PromptServer.instance.routes
    dumps = lambda obj: json.dumps(obj, ensure_ascii=False)  # noqa: E731

    @routes.get("/assembled-recipes/ds/status")
    async def _status(request):
        return aiohttp.web.json_response(status_payload(), dumps=dumps)

    @routes.post("/assembled-recipes/ds/bind")
    async def _bind(request):
        body = await request.json()
        out = await asyncio.get_event_loop().run_in_executor(None, bind, body)
        return aiohttp.web.json_response(out, dumps=dumps)

    @routes.post("/assembled-recipes/ds/test")
    async def _test(request):
        out = await asyncio.get_event_loop().run_in_executor(None, test_connectivity)
        return aiohttp.web.json_response(out, dumps=dumps)

    @routes.post("/assembled-recipes/ds/unbind")
    async def _unbind(request):
        out = await asyncio.get_event_loop().run_in_executor(None, stop_and_unbind)
        return aiohttp.web.json_response(out, dumps=dumps)

    @routes.post("/assembled-recipes/ds/service")
    async def _service(request):
        body = await request.json()
        fn = start if str(body.get("action")) == "start" else stop
        out = await asyncio.get_event_loop().run_in_executor(None, fn)
        return aiohttp.web.json_response(out, dumps=dumps)
