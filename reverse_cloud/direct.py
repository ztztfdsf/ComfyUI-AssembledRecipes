# -*- coding: utf-8 -*-
"""DeepSeek 网页/App 协议直连客户端（纯 Python，无外部网关）。

协议逐条实弹验证过（本机，走代理）：
  登录   POST /api/v0/users/login        body 混合加密（AES-128-CBC + RSA-OAEP-SHA256），
                                        密文为请求体，key||iv 用公钥加密后放 x-request-encryption-key
  会话   POST /api/v0/chat_session/create
  PoW    POST /api/v0/chat/create_pow_challenge → DeepSeekHashV1 搜 nonce → x-ds-pow-response
  传图   POST /api/v0/file/upload_file   multipart + x-model-type + PoW，轮询 fetch_files 到就绪
  补全   POST /api/v0/chat/completion    SSE，fragments 按 id 覆盖累计

风控要点（推翻早前结论）：真正的关卡是 **device_id 必须是真实数美指纹**，
不是 TLS/H2 指纹 —— 裸 httpx + 真实 device_id 实测可登录可补全；编造的 device_id
一律 RISK_DEVICE_DETECTED。所以本客户端不引入 curl_cffi/uTLS 之类指纹伪装。

出口铁律：所有请求必须走代理，检测不到代理就直接报错，绝不裸奔暴露本机 IP。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import time

import httpx

from . import pow as _pow

BASE = "https://chat.deepseek.com"
APP_VERSION = "2.7.5"
ANDROID_API_LEVEL = "34"
TARGET_COMPLETION = "/api/v0/chat/completion"
TARGET_CONTINUE = "/api/v0/chat/continue"
TARGET_UPLOAD = "/api/v0/file/upload_file"

# DeepSeek 生产认证接口公钥（逆向自 Android v2.7.5 / Web v2.7.1，与 ds2api 同源）
_AUTH_RSA_PUB_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0RLAhx1TOJhAQBHS1LHXlnkfOg86O"
    "+uhsmKWNNkmugVrRqO6cl13SnjBs2otRYZGJd50xpBG4We2i6/oCJCwxA/49YZfIwaeDmTx+WB"
    "IN7qrwhwP7vGCi6Osesmn/Ddgdye6mifYkRpRMonQjGY6VPaUMTw1jjOAIKph5YZn0bDVmGLvs"
    "5eH6CoQYHM5/Z1Nfhj36VL28eZi3elJHoz/L6Tzi2wY1rCYqYHfepq3Ysy8y5Y5BeFJXN60RYL"
    "lbs+MRC+2kyJqcfApUnn1O/W2om9dhxc6Rc5dqas1YB4X4CWCttrKk/suhpIx9zpQY9kqtotbe"
    "5zweYaKv0rCVeqx8wIDAQAB"
)

class _Accumulator:
    """SSE 结果累加器。

    实测内容会**分两路**来：开头几块走带 fragment id 的 patch，后面的走裸 {"v":"…"} 打字机——
    两边不重复，是一前一后同一条流的两段（typed=3 + raw=482 刚好拼成完整 JSON）。
    所以正文 = 按到达顺序把两路拼起来；收尾的 {"p":"response/fragments","v":[…]} 分片表
    只用来拆 THINK/RESPONSE（开思考时它是唯一能分清推理与正文的地方）。
    """

    def __init__(self):
        self.parts = []        # 正文，按到达顺序（patch 非推理 + 裸 v）
        self.table_answer = {}  # 分片表里的 RESPONSE 分片
        self.think = {}         # 分片表 / patch 里的推理分片
        self.message_id = None
        self.auto_continue = False
        self.finished = False

    def text(self) -> str:
        joined = "".join(self.parts)
        table = "".join(self.table_answer[k] for k in sorted(self.table_answer))
        # 实测开头会被分片表先拿走一块（例如 “ [” 两字符），而后面全在打字机流里；
        # 只要流开头不是这段，就把它接回头部，否则永远缺开头那个 [。
        if table and len(table) < len(joined) and not joined.startswith(table[:min(len(table), 8)]):
            joined = table + joined
        chosen = table if len(table) >= len(joined) else joined
        if _DEBUG:
            print("[ds-sse] 正文选型 stream=%d table=%d -> %s" %
                  (len(joined), len(table), "table" if chosen is table else "stream"), flush=True)
            print("[ds-sse]   最终头:", repr(chosen[:44]), flush=True)
        return chosen

    def thinking_text(self) -> str:
        return "".join(self.think[k] for k in sorted(self.think))


def _mute_readable(inner: dict) -> str:
    """上游回 biz_msg="user is muted" 时，把 mute_until 翻成人话（所有端点共用）。"""
    if not isinstance(inner, dict) or str(inner.get("biz_msg") or "").strip() != "user is muted":
        return ""
    biz = inner.get("biz_data") if isinstance(inner.get("biz_data"), dict) else {}
    until = biz.get("mute_until")
    if not until:
        return "DeepSeek: 账号被禁言（user is muted），上游没给了时间"
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(float(until))
    left = (float(until) - time.time()) / 3600.0
    return ("DeepSeek 把这个账号禁言了，%s 才解（上游原文 user is muted，还剩 %.0f 小时）"
            % (when.strftime("%m-%d %H:%M"), max(0.0, left)))


class DeepSeekError(RuntimeError):
    """带错误码的直连异常（biz_msg / code 原样带出，便于面板显示真因）。"""

    def __init__(self, message, code=None, mute_until=None):
        super().__init__(message)
        self.code = code
        self.mute_until = mute_until      # 上游给的解禁时间戳（有就能众账号轮巡时直接跳过）

    @property
    def muted(self) -> bool:
        return bool(self.mute_until) or "user is muted" in str(self).lower()

    @property
    def rate_limited(self) -> bool:
        text = str(self).upper()
        return "TOO_MANY_REQUESTS" in text or "RATE" in text

    @property
    def auth_failed(self) -> bool:
        """token 失效（上游会直接回 Authorization Failed / invalid token，实测登录一次后
        旧 token 就作废，而客户端只按 5h 龄期缓存 → 隔一阵子第一枪必挂）。"""
        text = str(self).upper()
        return ("AUTHORIZATION FAILED" in text or "INVALID TOKEN" in text
                or "UNAUTHORIZED" in text or "TOKEN_EXPIRED" in text
                or "HTTP 401" in text or "HTTP 403" in text
                or "NOT LOGGED IN" in text)


# 上游限流时的退避重试（实测短时间连打多枪会回 TOO_MANY_REQUESTS）
_RETRY_WAITS = (15.0, 30.0, 60.0)
_DEBUG = bool(os.environ.get("ASSEMBLED_DS_DEBUG"))


def _with_retry(client, fn, what: str):
    """协议层只处理“同一个账号能自己恢复”的那一类：token 失效。

    token 失效 → 丢缓存、重新登录、重试一次（只给一次，避免登录风暴）。
    限流/禁言不在这里睡——那是“政策”，交给上层账号池决定（换下一个账号，
    没得换才退避），否则单账号一被限流就要白等 15-60s。
    """
    relogged = False
    while True:
        try:
            return fn()
        except DeepSeekError as e:
            if e.auth_failed and not relogged:
                relogged = True
                client.drop_token()
                print("[DS网关] %s token 失效，已重新登录重试：%s" % (what, e), flush=True)
                continue
            raise


# ============================ 出口代理 ============================

def detect_proxy() -> str:
    """按 环境变量 → Windows 系统代理 的顺序找出口；找不到就抛错（禁止裸奔）。"""
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        val = (os.environ.get(key) or "").strip()
        if val and not val.startswith("socks"):
            return val if "://" in val else "http://" + val
    try:  # Windows Internet Settings（FlClash 等客户端改的就是这里）
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            if enable:
                server, _ = winreg.QueryValueEx(k, "ProxyServer")
                server = str(server).strip()
                host = server.split(";")[0].split("=")[-1].strip()
                if host:
                    return "http://" + host
    except Exception:
        pass
    raise DeepSeekError("没有检测到可用代理出口，已拒绝直连（防止暴露本机真实 IP）")


# ============================ 请求体加密 ============================

def _encrypt_auth_body(plain: bytes):
    """AES-128-CBC(PKCS7) 加密请求体；key||iv 用 RSA-OAEP(SHA-256) 加密后 base64。

    返回 (密文, x-request-encryption-key 头值)；加密库缺失时返回 (None, None) 由调用方走平文。
    """
    try:
        import secrets
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding as aspad
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.serialization import load_der_public_key
    except Exception:
        return None, None
    key, iv = secrets.token_bytes(16), secrets.token_bytes(16)
    pad = 16 - (len(plain) % 16)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(plain + bytes([pad]) * pad) + enc.finalize()
    pub = load_der_public_key(base64.b64decode(_AUTH_RSA_PUB_B64))
    wrapped = pub.encrypt(key + iv, aspad.OAEP(mgf=aspad.MGF1(algorithm=hashes.SHA256()),
                                               algorithm=hashes.SHA256(), label=None))
    return ct, base64.b64encode(wrapped).decode()


# ============================ 客户端 ============================

class DeepSeekClient:
    """一个账号一个实例；token 自动登录与续期，PoW 按 target_path 缓存。"""

    def __init__(self, account: str, password: str, device_id: str, proxy: str | None = None):
        self.account = (account or "").strip()
        self.password = (password or "").strip()
        self.device_id = (device_id or "").strip()
        self.proxy = proxy or detect_proxy()
        self._lock = threading.RLock()   # 本账号的 token 锁（不全局）
        self._token = ""
        self._token_ts = 0.0
        self._http = httpx.Client(timeout=httpx.Timeout(30.0, read=600.0),
                                  proxy=self.proxy, follow_redirects=True)

    # ---- 基础 ----
    def close(self):
        try:
            self._http.close()
        except Exception:
            pass

    def _headers(self, token: bool = False, extra: dict | None = None) -> dict:
        h = {
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": "DeepSeek/%s Android/%s" % (APP_VERSION, ANDROID_API_LEVEL),
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-platform": "android",
            "x-client-version": APP_VERSION,
            "x-client-locale": "zh_CN",
            "x-client-timezone-offset": str(-time.timezone) if time.timezone else "-28800",
        }
        if token:
            h["authorization"] = "Bearer " + self._token
            h["x-device-id"] = self.device_id
        if extra:
            h.update(extra)
        return h

    @staticmethod
    def _unwrap(payload: dict) -> dict:
        """逐层剥 code / biz_code / biz_data，把上游错误码原样抛出来。"""
        if not isinstance(payload, dict):
            raise DeepSeekError("响应不是 JSON 对象")
        code = payload.get("code")
        inner = payload.get("data") or {}
        biz_code = inner.get("biz_code") if isinstance(inner, dict) else None
        biz = inner.get("biz_data") if isinstance(inner, dict) else None
        if code not in (0, None):
            raise DeepSeekError("DeepSeek: %s" % (payload.get("msg") or code), code=code)
        if biz_code not in (0, None):
            err = DeepSeekError(_mute_readable(inner) or "DeepSeek: %s" % (inner.get("biz_msg") or biz_code),
                                code=biz_code)
            biz = inner.get("biz_data") if isinstance(inner.get("biz_data"), dict) else {}
            if str(inner.get("biz_msg") or "").strip() == "user is muted" and biz.get("mute_until"):
                err.mute_until = float(biz["mute_until"])
            raise err
        if biz is None:
            raise DeepSeekError("DeepSeek 响应缺 biz_data: " + json.dumps(payload, ensure_ascii=False)[:180])
        return biz

    # ---- 登录 ----
    def login(self) -> str:
        is_email = "@" in self.account
        body = {"password": self.password, "device_id": self.device_id, "os": "android"}
        if is_email:
            body["email"] = self.account
        else:
            body["mobile"] = self.account
            body["area_code"] = "+86"
        plain = json.dumps(body, separators=(",", ":")).encode()
        ct, key_hdr = _encrypt_auth_body(plain)
        headers = self._headers(extra={"x-request-encryption-key": key_hdr} if key_hdr else None)
        r = self._http.post(BASE + "/api/v0/users/login",
                            content=(ct if ct else plain), headers=headers)
        try:
            payload = r.json()
        except ValueError:
            raise DeepSeekError("登录响应非 JSON（HTTP %s）: %s" % (r.status_code, r.text[:150]))
        biz = self._unwrap(payload)
        token = (biz.get("user") or {}).get("token") or biz.get("token")
        if not token:
            raise DeepSeekError("登录成功但没有 token")
        self._token, self._token_ts = token, time.time()
        return token

    def ensure_token(self, max_age: float = 5 * 3600.0) -> str:
        # 锁是每账号一把（不是全局）：多账号轮巡时，一个账号登录慢不能拖死其他账号
        with self._lock:
            if not self._token or time.time() - self._token_ts > max_age:
                return self.login()
            return self._token

    def drop_token(self) -> None:
        """丢掉缓存的 token（上游说它失效了就用这个，下一枪会重新登录）。"""
        with self._lock:
            self._token = None
            self._token_ts = 0.0

    # ---- PoW ----
    def pow_header(self, target_path: str, force: bool = True) -> str:
        """取一份新 challenge 并求解。

        实测：challenge 是一次性的——同一个 x-ds-pow-response 打第二枪，上游回 200 但流是空的。
        所以这里不缓存，每次请求都重新算（取 challenge ~0.15s + 求解 ~0.5-1.4s）。
        """
        self.ensure_token()
        r = self._http.post(BASE + "/api/v0/chat/create_pow_challenge",
                            content=json.dumps({"target_path": target_path}).encode(),
                            headers=self._headers(token=True))
        challenge = self._unwrap(r.json())["challenge"]
        answer = _pow.solve(challenge["challenge"], challenge["salt"],
                            int(challenge["expire_at"]), int(challenge["difficulty"]))
        return _pow.make_header(challenge, answer)

    # ---- 会话 ----
    def create_session(self) -> str:
        self.ensure_token()
        r = self._http.post(BASE + "/api/v0/chat_session/create", content=b"{}",
                            headers=self._headers(token=True))
        return self._unwrap(r.json())["chat_session"]["id"]

    # ---- 传图 ----
    def upload_image(self, data: bytes, filename: str = "layout.jpg",
                     content_type: str = "image/jpeg", wait_ready_s: float = 90.0) -> str:
        """上传 PIL 图，返回 file_id（内部轮询到解析 SUCCESS）。"""
        return _with_retry(
            self, lambda: self._upload_once(data, filename, content_type, wait_ready_s), "upload_file")

    def _upload_once(self, data, filename, content_type, wait_ready_s) -> str:
        self.ensure_token()
        boundary = "----arboundary%016x" % int.from_bytes(os.urandom(8), "big")
        head = ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                "Content-Type: %s\r\n\r\n" % (boundary, filename.replace('"', "_"), content_type)).encode()
        body = head + data + ("\r\n--%s--\r\n" % boundary).encode()
        headers = self._headers(token=True, extra={
            "content-type": "multipart/form-data; boundary=%s" % boundary,
            "x-model-type": "default",
            "x-file-size": str(len(data)),
            "x-ds-pow-response": self.pow_header(TARGET_UPLOAD),
        })
        r = self._http.post(BASE + "/api/v0/file/upload_file", content=body, headers=headers)
        biz = self._unwrap(r.json())
        node = biz.get("node") or biz
        file_id = node.get("id") or node.get("file_id") or ""
        if not file_id:
            raise DeepSeekError("上传成功但没有 file id: " + json.dumps(biz, ensure_ascii=False)[:180])
        status = str(node.get("status") or node.get("file_status") or "").upper()
        ready = ("SUCCESS", "COMPLETED", "PROCESSED", "READY", "DONE", "AVAILABLE")
        deadline = time.time() + wait_ready_s
        while status not in ready and time.time() < deadline:
            time.sleep(1.0)
            rr = self._http.get(BASE + "/api/v0/file/fetch_files",
                                 params={"file_ids": file_id}, headers=self._headers(token=True))
            biz2 = self._unwrap(rr.json())
            files = biz2.get("files") if isinstance(biz2, dict) else None
            node = (files[0] if isinstance(files, list) and files else biz2)
            status = str(node.get("status") or node.get("file_status") or "").upper()
        if status not in ready:
            raise DeepSeekError("图片解析未就绪: status=%s" % status)
        return file_id

    # ---- 补全 ----
    def completion(self, prompt: str, session_id: str | None = None,
                   parent_message_id=None, file_ids=None, thinking: bool = False,
                   search: bool = False, on_delta=None, ensure=None,
                   max_continue: int = 3) -> dict:
        """跑一轮补全，返回 {text, session_id, message_id, thinking_text}。

        被限流时整轮重做；ensure(text) 返回 False（例如 JSON 被拦腰截断）时
        自动走 /api/v0/chat/continue 续写，最多 max_continue 轮。
        """
        result = _with_retry(
            self,
            lambda: self._completion_once(prompt, session_id, parent_message_id,
                                          file_ids, thinking, search, on_delta),
            "completion")
        rounds = 0
        while rounds < max_continue and result["message_id"] and not self._ensure_ok(ensure, result["text"]):
            rounds += 1
            more = self._continue_round(result["session_id"], result["message_id"])
            if not more:
                break
            result["text"] += more
        return result

    @staticmethod
    def _ensure_ok(ensure, text) -> bool:
        if ensure is None:
            return True
        try:
            return bool(ensure(text))
        except Exception:
            return True

    def _continue_round(self, session_id: str, message_id) -> str:
        """/api/v0/chat/continue 续写一轮（官方客户端在回答被截断时就是这么做的）。

        注意：PoW 的 target_path **没有** /api/v0/chat/continue 这一档（实测回
        INVALID_TARGET_PATH），续写枪用的是 completion 的 challenge（ds2api 也是这么做的）。
        """
        self.ensure_token()
        body = {"chat_session_id": session_id, "message_id": int(message_id),
                "fallback_to_resume": True, "shumei_device_id": self.device_id}
        headers = self._headers(token=True, extra={
            "Referer": BASE + "/a/chat/s/" + session_id,
            "x-ds-pow-response": self.pow_header(TARGET_COMPLETION),
        })
        acc = _Accumulator()
        with self._http.stream("POST", BASE + TARGET_CONTINUE,
                               content=json.dumps(body).encode(), headers=headers) as resp:
            if resp.status_code != 200:
                raw = resp.read()
                raise DeepSeekError("continue HTTP %s: %s" % (resp.status_code, raw[:200]))
            self._guard_error_envelope(resp, "continue")
            self._consume_sse(resp, acc)
        return acc.text()

    @staticmethod
    def _guard_error_envelope(resp, what: str) -> None:
        """上游出错时不回事件流，而回一个 application/json 错误信封。

        实测样本（账号被禁言）：
          {"code":0,"data":{"biz_code":5,"biz_msg":"user is muted",
           "biz_data":{"is_muted":1,"mute_until":1795053265.0,"msg":null}}}
        不拦的话流里一个字节都没有，节点会静默交出空正文 → 下游 0 框，没人知道为什么。
        """
        ctype = (resp.headers.get("content-type") or "").lower()
        if "application/json" not in ctype:
            return
        raw = resp.read()
        try:
            payload = json.loads((raw or b"").decode("utf-8", "ignore") or "{}")
        except ValueError:
            raise DeepSeekError("%s 返回非流内容(%s): %s" % (what, ctype, raw[:180]))
        inner = (payload.get("data") or {}) if isinstance(payload, dict) else {}
        hint = _mute_readable(inner)
        if hint:
            biz = inner.get("biz_data") if isinstance(inner.get("biz_data"), dict) else {}
            raise DeepSeekError(hint, code=inner.get("biz_code"),
                                mute_until=float(biz["mute_until"]) if biz.get("mute_until") else None)
        DeepSeekClient._unwrap(payload)   # biz_code 非 0 会在这里抛（带出上游原文）
        raise DeepSeekError("%s 没有返回事件流（content-type=%s）: %s"
                            % (what, ctype, json.dumps(payload, ensure_ascii=False)[:200]))

    def _completion_once(self, prompt, session_id, parent_message_id, file_ids,
                         thinking, search, on_delta) -> dict:
        """跑一轮补全，返回 {text, session_id, message_id, thinking_text}。"""
        self.ensure_token()
        session_id = session_id or self.create_session()
        body = {
            "chat_session_id": session_id,
            "parent_message_id": parent_message_id,
            "model_type": "default",
            "prompt": prompt,
            "ref_file_ids": list(file_ids or []),
            "thinking_enabled": bool(thinking),
            "search_enabled": bool(search),
            "action": None,
            "preempt": False,
            "shumei_device_id": self.device_id,
        }
        headers = self._headers(token=True, extra={
            "Referer": BASE + "/a/chat/s/" + session_id,
            "x-ds-pow-response": self.pow_header(TARGET_COMPLETION),
        })
        acc = _Accumulator()
        with self._http.stream("POST", BASE + TARGET_COMPLETION,
                               content=json.dumps(body).encode(), headers=headers) as resp:
            if resp.status_code != 200:
                raw = resp.read()
                raise DeepSeekError("completion HTTP %s: %s" % (resp.status_code, raw[:200]))
            self._guard_error_envelope(resp, "completion")
            self._consume_sse(resp, acc, on_delta)
        answer = acc.text()
        if acc.auto_continue and acc.message_id:
            answer = answer + self._continue_round(session_id, acc.message_id)
        return {
            "text": answer,
            "thinking_text": acc.thinking_text(),
            "session_id": session_id,
            "message_id": acc.message_id,
        }

    def _consume_sse(self, resp, acc, on_delta=None):
        """DeepSeek 原生 SSE 三种形态（实测）：
          · {"p":"response/fragments/<id>/[thinking/]content","o":"APPEND|SET","v":"…"} → 带类型增量
          · {"v":{"response":{...,"fragments":[{id,type,content}]}}} → 快照（该 fragment 全量）
          · {"v":"文本"} → 打字机重复流（会混推理，只当兵底 + 没开思考时驱动 on_delta）
        """
        debug = bool(os.environ.get("ASSEMBLED_DS_DEBUG"))
        for raw in resp.iter_lines():
            if not raw or not raw.startswith("data:"):
                continue
            payload = raw[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if debug and isinstance(event.get("p"), str) and "fragments" in event["p"] \
                    and isinstance(event.get("v"), list):
                print("[ds-sse] 分片表", json.dumps(
                    [(f.get("id"), f.get("type"), len(f.get("content") or ""))
                     for f in event["v"] if isinstance(f, dict)], ensure_ascii=False), flush=True)
            if "request_message_id" in event:
                acc.message_id = event.get("response_message_id") or acc.message_id
                continue
            path = event.get("p")
            val = event.get("v")
            if isinstance(path, str):
                # 收尾的权威分片表：{"p":"response/fragments","v":[{id,type,content},…]}
                # 开思考时只有这里能分开推理与正文（THINK/RESPONSE）
                if path == "response/fragments" and isinstance(val, list):
                    for f in val:
                        if not isinstance(f, dict):
                            continue
                        fid = int(f.get("id") or 0)
                        content = f.get("content") or ""
                        if not content:
                            continue
                        if str(f.get("type") or "RESPONSE").upper() == "THINK":
                            acc.think[fid] = content
                        else:
                            acc.table_answer[fid] = content
                    continue
                if path in ("response/status", "response/quasi_status", "quasi_status"):
                    acc.finished = val == "FINISHED"
                    continue
                m = re.match(r"response/fragments/(-?\d+)/(thinking/)?content", path)
                if m and isinstance(val, str):
                    if m.group(2):
                        acc.think[int(m.group(1))] = val  # 推理单独存，不污染正文
                    else:
                        acc.parts.append(val)
                        if on_delta:
                            on_delta(val)
                continue
            if isinstance(val, str):
                acc.parts.append(val)
                if on_delta:
                    on_delta(val)
            elif isinstance(val, dict) and "response" in val:
                r = val.get("response") or {}
                acc.auto_continue = bool(r.get("auto_continue"))
                if r.get("message_id"):
                    acc.message_id = r["message_id"]
                for f in (r.get("fragments") or []):
                    if not isinstance(f, dict):
                        continue
                    fid = int(f.get("id") or 0)
                    content = f.get("content") or ""
                    if not content:
                        continue
                    if str(f.get("type") or "RESPONSE").upper() == "THINK":
                        acc.think[fid] = content
                    else:
                        acc.table_answer[fid] = content
