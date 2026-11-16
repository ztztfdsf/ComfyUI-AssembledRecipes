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
from . import endpoints
from . import providers  # providers 只在函数内懒导入 gateway，不成环

PORT_DEFAULT = 5812
MODEL_MAIN = "deepseek-v4.1-flash"
MODELS = [MODEL_MAIN, MODEL_MAIN + "-nothinking", MODEL_MAIN + "-search"]

# 可重入：_persist_account 拿锁后还要调 save_config（再拿一次），普通 Lock 会当场死锁（实测卡死过）
_CONFIG_LOCK = threading.RLock()
_STATE = {
    "server": None,
    "thread": None,
    "port": None,
    "clients": {},
    "cursor": 0,
    "error": "",
    "started_at": 0.0,
    "requests": 0,
    "last_ms": 0.0,
    "last_account": "",
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


# ============================ 账号池（多账号轮巡）============================
#
# 一个 DeepSeek 账号的额度/风控是共享的（实测：单账号连打几小时会被 mute 3 天），
# 所以这里把账号当池子用：
#   · 平时按 cursor 往后转（round-robin），不拆死第一号——把风控压力摊开
#   · 哪个账号被禁言 → 按上游给的 mute_until 直接跳过（到期自动回来）
#   · 哪个账号被限流 → 短冷却（默认 120s）后跳过
#   · 全池都不可用 → 报错文案里列出每个账号的状态与最早恢复时间
# 账密只存本地 user/AssembledRecipes/ds_gateway.json，不进工作流、不进日志。

_COOLDOWN_S = 120.0        # 限流后该账号暂时不用的时长
_MUTE_FALLBACK_S = 6 * 3600.0   # 上游没给 mute_until 时的保守估计


def _blank_account() -> dict:
    return {"email": "", "password": "", "device_id": "",
            "added_at": time.time(), "muted_until": 0.0,
            "cooldown_until": 0.0, "fail_streak": 0, "served": 0, "last_ok_at": 0.0}


def accounts(cfg: dict | None = None) -> list:
    """读账号列表；旧单账号形状（顶层 email/password）在这里一次性折进去并落盘。"""
    cfg = load_config() if cfg is None else cfg
    lst = cfg.get("accounts")
    if not isinstance(lst, list):
        lst = []
        legacy = str(cfg.get("email") or cfg.get("account") or "").strip()
        if legacy and cfg.get("password"):
            a = _blank_account()
            a.update({"email": legacy, "password": str(cfg["password"]),
                      "device_id": str(cfg.get("device_id") or "")})
            lst.append(a)
            for k in ("email", "account", "password", "device_id"):
                cfg.pop(k, None)
            cfg["accounts"] = lst
            save_config(cfg)
            _log("旧单账号配置已折成账号池：%s" % legacy)
        return lst
    out = []
    for a in lst:
        if isinstance(a, dict) and str(a.get("email") or "").strip() and a.get("password"):
            a.setdefault("device_id", "")
            for k, dv in (("muted_until", 0.0), ("cooldown_until", 0.0), ("fail_streak", 0),
                          ("served", 0), ("last_ok_at", 0.0), ("added_at", time.time())):
                a.setdefault(k, dv)
            out.append(a)
    return out


def _persist_account(acct: dict):
    """把运行时状态（禁言/冷却/计数）写回配置，重启后还记得。"""
    with _CONFIG_LOCK:
        cfg = load_config()
        lst = cfg.get("accounts")
        if not isinstance(lst, list):
            lst = accounts(cfg)          # 已经落盘成列表的形态
            cfg["accounts"] = lst
        hit = False
        for row in lst:
            if isinstance(row, dict) and str(row.get("email", "")).strip() == acct["email"]:
                row.update({k: acct[k] for k in
                            ("password", "device_id", "muted_until", "cooldown_until",
                             "fail_streak", "served", "last_ok_at", "added_at") if k in acct})
                hit = True
                break
        if not hit:
            lst.append(acct)
        cfg["accounts"] = lst
        for k in ("email", "account", "password", "device_id"):
            cfg.pop(k, None)
        save_config(cfg)


def account_state(a: dict, now: float | None = None) -> str:
    now = time.time() if now is None else now
    if float(a.get("muted_until") or 0) > now:
        return "muted"
    if float(a.get("cooldown_until") or 0) > now:
        return "cooling"
    if not str(a.get("device_id") or "").strip():
        return "no_device_id"
    return "ready"


def _fmt_when(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")


def client_for(a: dict) -> direct.DeepSeekClient:
    """一个账号一个长驻 client（token 缓存复用，避免每枪都登录）。"""
    cfg = load_config()
    pool = _STATE.setdefault("clients", {})
    key = a["email"]
    c = pool.get(key)
    if c and c.password == a["password"] and c.device_id == a["device_id"]:
        return c
    if c:
        try:
            c.close()
        except Exception:
            pass
    c = direct.DeepSeekClient(a["email"], a["password"], a["device_id"],
                              proxy=cfg.get("proxy") or None)
    pool[key] = c
    return c


def with_account(fn, what: str):
    """在账号池里挑一个能用的跑 fn(client, account)；禁言/限流就自动换下一个。

    fn 的返回值原样传出；抛 DeepSeekError 时按类型记账：
      muted → 记下 mute_until（上游给了就用真值），长期跳过
      rate  → 短冷却，本轮跳过
      其余  → 直接报（不是换账号能解决的事）
    """
    pool = accounts()
    if not pool:
        raise direct.DeepSeekError("还没有绑定 DeepSeek 账号（在节点面板「账号池」里添加）")
    now = time.time()
    order = list(pool)
    cursor = int(_STATE.get("cursor") or 0) % len(order)
    order = order[cursor:] + order[:cursor]
    tried = []
    for a in order:
        st = account_state(a, now)
        if st != "ready":
            continue
        tried.append(a["email"])
        try:
            out = fn(client_for(a), a)
        except direct.DeepSeekError as e:
            if e.muted:
                until = float(getattr(e, "mute_until", 0) or 0) or (now + _MUTE_FALLBACK_S)
                a["muted_until"] = until
                a["fail_streak"] = int(a.get("fail_streak") or 0) + 1
                _persist_account(a)
                _log("%s 被禁言到 %s，转下一个账号" % (a["email"], _fmt_when(until)))
                continue
            if e.rate_limited:
                a["cooldown_until"] = now + _COOLDOWN_S
                a["fail_streak"] = int(a.get("fail_streak") or 0) + 1
                _persist_account(a)
                _log("%s 被限流，冷却 %ds 后转下一个" % (a["email"], int(_COOLDOWN_S)))
                continue
            raise
        a["served"] = int(a.get("served") or 0) + 1
        a["last_ok_at"] = time.time()
        a["fail_streak"] = 0
        a["cooldown_until"] = 0.0
        # cursor 存的是“池里的下标”，不是旋转后 order 的下标（之前算错了导致
        # 第二枪之后永远钉在同一个账号上，轮巡变成拆死一号）
        _STATE["cursor"] = (pool.index(a) + 1) % len(pool)
        _persist_account(a)
        _STATE["last_account"] = a["email"]
        return out
    # 整池都不可用：把每个账号的状态摊开报出去，不要只说“失败了”
    detail = []
    for a in pool:
        st = account_state(a, now)
        if st == "muted":
            detail.append("%s 禁言到 %s" % (a["email"], _fmt_when(a["muted_until"])))
        elif st == "cooling":
            detail.append("%s 限流冷却到 %s" % (a["email"], _fmt_when(a["cooldown_until"])))
        elif st == "no_device_id":
            detail.append("%s 缺 device_id" % a["email"])
        else:
            detail.append("%s 可用但本轮已试过" % a["email"])
    raise direct.DeepSeekError("DeepSeek 账号池暂时全部不可用（%d 个）：%s" % (len(pool), "；".join(detail)))


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

    def job(client, acct):
        t0 = time.time()
        file_id = client.upload_image(buf.getvalue(), filename="layout.jpg", content_type="image/jpeg")
        result = client.completion(prompt, file_ids=[file_id], thinking=bool(thinking),
                                   ensure=_json_array_complete)
        ms = round((time.time() - t0) * 1000.0, 1)
        _STATE["last_ms"] = ms
        _STATE["requests"] += 1
        _log("caption ok %dms 出字%d 思考%s 账号%s" %
              (ms, len(result["text"]), "on" if thinking else "off", acct["email"]))
        return result["text"]

    return with_account(job, "caption")


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


# reasoning_effort 的五档→DeepSeek 官方只有一个 thinking 开关（布尔），所以这里收敛；
# 规则只住在 providers.effort_to_thinking 一处，节点与网关都用它。


def _flags_from_body(body: dict):
    """模型名后缀 + reasoning_effort → (model, thinking, search)。

    effort 优先于模型名后缀：显式选了强度就按强度说话。
    """
    model, thinking, search = _model_flags(body.get("model"))
    eff = str(body.get("reasoning_effort") or "").strip().lower()
    if eff in ("", "auto"):
        return model, thinking, search
    if eff not in ("minimal", "low", "medium", "high", "max", "none", "off"):
        raise direct.DeepSeekError("reasoning_effort 不认的值：%r（可用：minimal/low/medium/high/max）" % eff)
    return model, providers.effort_to_thinking(eff, default=thinking), search


def run_completion(body: dict) -> dict:
    """非流式：一次补全，返回 OpenAI 形状。"""
    prompt, images = _extract_prompt_and_images(body)
    if not prompt:
        raise direct.DeepSeekError("messages 里没有文本内容")
    model, thinking, search = _flags_from_body(body)
    def job(client, acct):
        t0 = time.time()
        file_ids = []
        for blob in images[:5]:
            file_ids.append(client.upload_image(blob))
        result = client.completion(prompt, file_ids=file_ids, thinking=thinking, search=search)
        ms = round((time.time() - t0) * 1000.0, 1)
        _STATE["last_ms"] = ms
        _STATE["requests"] += 1
        _log("completion ok %dms 图%d张 出字%d 账号%s" %
              (ms, len(file_ids), len(result["text"]), acct["email"]))
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

    return with_account(job, "completion")


def stream_completion(body: dict):
    """流式：按 OpenAI SSE 逐块吐字（上游本身就是逐 token 推的）。"""
    prompt, images = _extract_prompt_and_images(body)
    model, thinking, search = _flags_from_body(body)
    box = {"done": False, "err": None, "text": []}
    holder = {}

    def job(client, acct):        # 传图阶段可以换账号；一旦开始吐字就钉死当前账号
        holder.update(client=client, acct=acct,
                      file_ids=[client.upload_image(b) for b in images[:5]])
        return True

    with_account(job, "stream-setup")
    client, acct = holder["client"], holder["acct"]
    file_ids = holder["file_ids"]
    cid = "chatcmpl-" + secrets.token_hex(12)

    def worker():
        try:
            client.completion(prompt, file_ids=file_ids, thinking=thinking, search=search,
                              on_delta=lambda s: box["text"].append(s))
            _STATE["requests"] += 1
            _log("stream ok 出字%d 账号%s" % (sum(len(x) for x in box["text"]), acct["email"]))
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
    pool = accounts()
    now = time.time()
    rows = []
    for a in pool:
        st = account_state(a, now)
        rows.append({
            "email": a["email"],
            "state": st,
            "has_device_id": bool(str(a.get("device_id") or "").strip()),
            "muted_until": float(a.get("muted_until") or 0),
            "muted_until_text": (_fmt_when(a["muted_until"]) if st == "muted" else ""),
            "cooldown_until": float(a.get("cooldown_until") or 0),
            "served": int(a.get("served") or 0),
            "fail_streak": int(a.get("fail_streak") or 0),
            "last_ok_at": float(a.get("last_ok_at") or 0),
        })
    ready = [r for r in rows if r["state"] == "ready"]
    recover = min([r["muted_until"] for r in rows if r["state"] == "muted"]
                  + [r["cooldown_until"] for r in rows if r["state"] == "cooling"]
                  or [0.0])
    out = {
        "running": _STATE["server"] is not None,
        "port": _STATE["port"] or cfg.get("port") or PORT_DEFAULT,
        "base": gateway_base(),
        "bound": bool(ready),
        "accounts": rows,
        "ready": len(ready),
        "recover_at": recover,
        "recover_text": (_fmt_when(recover) if recover > now else ""),
        "last_account": _STATE.get("last_account") or "",
        "email": (ready[0]["email"] if ready else (rows[0]["email"] if rows else "")),
        "has_device_id": bool(ready and ready[0]["has_device_id"]),
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
    """添加/更新一个账号（同邮箱就地更新密码与 device_id），加完立即试跑这一号。"""
    email = str(body.get("email") or body.get("account") or "").strip()
    password = str(body.get("password") or "").strip()
    device_id = str(body.get("device_id") or "").strip()
    if not email or not password:
        return {"ok": False, "error": "邮箱/密码不能为空"}
    pool = accounts()
    hit = next((a for a in pool if a["email"] == email), None)
    is_new = hit is None
    if is_new:
        hit = _blank_account()
        hit["email"] = email
        pool.append(hit)
    hit["password"] = password
    if device_id:
        hit["device_id"] = device_id
    hit["muted_until"] = 0.0
    hit["cooldown_until"] = 0.0
    hit["fail_streak"] = 0
    cfg = {k: v for k, v in load_config().items() if k != "accounts"}
    cfg["accounts"] = pool
    cfg.setdefault("api_key", api_key())
    cfg.setdefault("port", PORT_DEFAULT)
    save_config(cfg)
    _log("账号池 %s %s（池内 %d 个，密码只存本地）" % ("新增" if is_new else "更新", email, len(pool)))
    return test_connectivity(email)


def unbind(email: str = "") -> dict:
    """解绑：给了 email 只删那一个，没给就清空整池。"""
    pool = accounts()
    if email:
        left = [a for a in pool if a["email"] != email]
    else:
        left = []
    cfg = {k: v for k, v in load_config().items() if k != "accounts"}
    cfg["accounts"] = left
    save_config(cfg)
    for key in ([email] if email else [a["email"] for a in pool]):
        c = _STATE.get("clients", {}).pop(key, None)
        if c:
            try:
                c.close()
            except Exception:
                pass
    _log("已从账号池移除 %s（剩 %d 个）" % (email or "全部账号", len(left)))
    return {"ok": True, "accounts": len(left)}


def fetch_models(body: dict) -> dict:
    """面板「拉取模型列表」：给了端点名就从本地库取 base/key（密钥不出本机），
    没给名字才用直传的 base/key。"""
    body = body or {}
    name = str(body.get("endpoint") or "").strip()
    if name:
        try:
            row = endpoints.resolve(name)
        except Exception as e:
            return {"ok": False, "error": str(e)[:400]}
        base, key = row["base"], row.get("key") or ""
    else:
        base = str(body.get("base") or "").strip()
        key = str(body.get("key") or "").strip()
    if not base:
        return {"ok": False, "error": "请求地址是空的"}
    t0 = time.time()
    try:
        models = providers.list_models(base, key)
    except Exception as e:
        _log("拉模型列表失败 %s: %s" % (base, e))
        return {"ok": False, "error": str(e)[:400], "base": base}
    ms = round((time.time() - t0) * 1000.0, 1)
    if name:
        endpoints.cache_models(name, models)
    _log("拉模型列表 %s → %d 个（%dms）" % (base, len(models), ms))
    return {"ok": True, "models": models, "ms": ms, "base": base, "endpoint": name}


def endpoints_api(body: dict) -> dict:
    """端点库的增删改查（面板用）。返回体永远不回显密钥。"""
    body = body or {}
    act = str(body.get("action") or "list")
    name = str(body.get("name") or "").strip()
    if act == "list":
        return {"ok": True, **endpoints.public_view()}
    if act == "save":
        if not name:
            return {"ok": False, "error": "端点名字不能空"}
        try:
            row = endpoints.upsert(name, base=str(body.get("base") or ""),
                                   key=(None if body.get("key") in (None, "") else str(body.get("key"))),
                                   model=str(body.get("model") or ""))
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        _log("端点已保存 %s（base=%s，密钥%s）" %
              (name, row["base"],
               "已更新" if body.get("key") else ("沿用旧的" if row.get("key") else "没填")))
        return {"ok": True, **endpoints.public_view()}
    if act == "select":
        if not endpoints.set_current(name):
            return {"ok": False, "error": "找不到端点 %s" % name}
        _log("当前端点 → %s" % name)
        return {"ok": True, **endpoints.public_view()}
    if act == "delete":
        if not endpoints.remove(name):
            return {"ok": False, "error": "找不到端点 %s" % name}
        _log("已删除端点 %s" % name)
        return {"ok": True, **endpoints.public_view()}
    return {"ok": False, "error": "未知 action: %s" % act}


def test_connectivity(email: str = "") -> dict:
    """测一个账号（不填就用池里第一个可用的）。"""
    t0 = time.time()
    pool = accounts()
    if email:
        pool = [a for a in pool if a["email"] == email] or pool
    try:
        if not pool:
            raise direct.DeepSeekError("账号池是空的")
        acct = next((a for a in pool if account_state(a) == "ready"), pool[0])
        client = client_for(acct)
        client.login()
        result = client.completion("只回答两个字：收到")
        ms = round((time.time() - t0) * 1000.0, 1)
        acct["fail_streak"] = 0
        acct["cooldown_until"] = 0.0
        acct["muted_until"] = 0.0
        acct["last_ok_at"] = time.time()
        acct["served"] = int(acct.get("served") or 0) + 1
        _persist_account(acct)
        _STATE["error"] = ""
        _STATE["last_account"] = acct["email"]
        _log("连通正常 %dms（%s）" % (ms, acct["email"]))
        return {"ok": True, "ms": ms, "model": MODEL_MAIN, "email": acct["email"],
                "echo": result["text"][:20]}
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
    """停服务 + 清空整池账密（面板「解除绑定」按钮）。"""
    n = len(accounts())
    cfg = {k: v for k, v in load_config().items() if k != "accounts"}
    cfg["accounts"] = []
    save_config(cfg)
    for c in _STATE.get("clients", {}).values():
        try:
            c.close()
        except Exception:
            pass
    _STATE["clients"] = {}
    _STATE["cursor"] = 0
    _log("已清空账号池（%d 个账号的本地账密已抹掉）" % n)
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
        try:
            body = await request.json()
        except Exception:
            body = {}
        email = str((body or {}).get("email") or "").strip()
        out = await asyncio.get_event_loop().run_in_executor(None, test_connectivity, email)
        return aiohttp.web.json_response(out, dumps=dumps)

    @routes.post("/assembled-recipes/ds/unbind")
    async def _unbind(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        email = str((body or {}).get("email") or "").strip()
        fn = (lambda: unbind(email)) if email else stop_and_unbind
        out = await asyncio.get_event_loop().run_in_executor(None, fn)
        return aiohttp.web.json_response(out, dumps=dumps)

    @routes.post("/assembled-recipes/ds/models")
    async def _models(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        out = await asyncio.get_event_loop().run_in_executor(None, fetch_models, body)
        return aiohttp.web.json_response(out, dumps=dumps)

    @routes.post("/assembled-recipes/ds/endpoints")
    async def _endpoints(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        out = await asyncio.get_event_loop().run_in_executor(None, endpoints_api, body)
        return aiohttp.web.json_response(out, dumps=dumps)

    @routes.post("/assembled-recipes/ds/service")
    async def _service(request):
        body = await request.json()
        fn = start if str(body.get("action")) == "start" else stop
        out = await asyncio.get_event_loop().run_in_executor(None, fn)
        return aiohttp.web.json_response(out, dumps=dumps)
