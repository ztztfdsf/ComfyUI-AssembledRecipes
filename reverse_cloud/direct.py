"""DeepSeek 节点内直连（不走 ds2api 网关）：账密登录 + 传图 + 补全。

协议照 ds2api（Go）源码移植：
  登录  POST /api/v0/users/login   明文 JSON {email|mobile, password, device_id, os:"web"}
  上传  POST /api/v0/file/upload_file  multipart + x-model-type:default + PoW + x-file-size
  就绪  GET  /api/v0/file/fetch_files?file_ids=...  轮询到 status SUCCESS
  补全  POST /api/v0/chat/completion  SSE，ref_file_ids 带图片
所有请求带数美指纹头（x-device-id / x-device-model）与 PoW（sha3 wasm solver）。
"""

from __future__ import annotations

import io
import json
import threading
import time

import httpx

BASE = "https://chat.deepseek.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
DEVICE_MODEL = "WA9ESmLzLGE"  # Windows 档案展示型号（ds2api 默认档案同位）
SEC_CH_UA = '"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="24"'

_LOGIN_LOCK = threading.Lock()
_TOKEN_CACHE = {}  # (account, device_id) -> {"token":..., "ts":...}


class DirectError(RuntimeError):
    pass


def _headers(token=None, device_uuid=None, extra=None):
    """Android App 协议头（ds2api LoginHeaders/BaseHeadersFor 同款）。"""
    h = {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": "DeepSeek/2.7.5 Android/34",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-locale": "zh_CN",
        "x-client-platform": "android",
        "x-client-timezone-offset": "28800",
        "x-client-version": "2.7.5",
    }
    if token:
        h["authorization"] = "Bearer " + token
    if device_uuid:
        h["x-device-id"] = device_uuid
    if extra:
        h.update(extra)
    return h


def _client(proxy=None):
    return httpx.Client(base_url=BASE, timeout=30.0, proxy=proxy, follow_redirects=True)


def _unwrap(data):
    if data.get("code") not in (0, None):
        raise DirectError("DeepSeek: %s" % (data.get("msg") or data))
    inner = data.get("data") or {}
    if inner.get("biz_code") not in (0, None):
        raise DirectError("DeepSeek: %s" % (inner.get("biz_msg") or inner))
    biz = inner.get("biz_data")
    if biz is None:
        raise DirectError("DeepSeek 响应异常: " + json.dumps(data, ensure_ascii=False)[:200])
    return biz


def login(account, password, device_id, proxy=None):
    """账密登录（Android App 协议，明文 JSON），返回 bearer token。

    account 含 @ 走 email，否则手机号。device_id 为 B 前缀的 88 字符设备指纹
    （网页版数美 SDK 产物，登录专用）；登录后的 API 用 UUID v4 形式的 x-device-id。
    """
    account = (account or "").strip()
    is_email = "@" in account
    body = {
        "email": account if is_email else "",
        "mobile": "" if is_email else account,
        "password": password,
        "area_code": "" if is_email else "+86",
        "device_id": device_id,
        "os": "android",
    }
    with _client(proxy) as http:
        r = http.post("/api/v0/users/login", json=body,
                      headers=_headers(extra={"Host": "chat.deepseek.com"}))
        try:
            data = r.json()
        except ValueError:
            raise DirectError("登录响应非 JSON（HTTP %s）: %s" % (r.status_code, r.text[:150]))
        biz = _unwrap(data)
        token = ((biz.get("user") or {}).get("token")) or biz.get("token")
        if not token:
            raise DirectError("登录成功但未返回 token")
        return token


def get_token(account, password, device_id, proxy=None, max_age=3600 * 5):
    key = (account, device_id)
    with _LOGIN_LOCK:
        hit = _TOKEN_CACHE.get(key)
        if hit and time.time() - hit["ts"] < max_age:
            return hit["token"]
        token = login(account, password, device_id, proxy)
        _TOKEN_CACHE[key] = {"token": token, "ts": time.time()}
        return token


def _pow_header(pow_solver, http, token, device_uuid, target_path):
    r = http.post("/api/v0/chat/create_pow_challenge", json={"target_path": target_path},
                  headers=_headers(token, device_uuid))
    challenge = _unwrap(r.json())["challenge"]
    return pow_solver.make_header(challenge)


def upload_image(token, device_uuid, pil, proxy=None, max_side=1280, timeout_s=90.0):
    """上传 PIL 图，返回 file_id（内部轮询到解析 SUCCESS）。"""
    from .pow import DeepSeekPow
    buf = io.BytesIO()
    im = pil
    if max(im.size) > max_side:
        ratio = max_side / max(im.size)
        im = im.resize((max(1, round(im.width * ratio)), max(1, round(im.height * ratio))))
    if im.mode != "RGB":
        im = im.convert("RGB")
    im.save(buf, "JPEG", quality=92)
    data = buf.getvalue()
    pow_solver = DeepSeekPow()
    with _client(proxy) as http:
        pow_h = _pow_header(pow_solver, http, token, device_uuid, "/api/v0/file/upload_file")
        files = {"file": ("layout.jpg", data, "image/jpeg")}
        headers = _headers(token, device_uuid, {
            "x-model-type": "default",
            "x-ds-pow-response": pow_h,
            "x-file-size": str(len(data)),
            "x-thinking-enabled": "1",
        })
        r = http.post("/api/v0/file/upload_file", files=files, headers=headers, timeout=120.0)
        biz = _unwrap(r.json())
        node = biz.get("node") or biz
        file_id = node.get("id") or ""
        if not file_id:
            raise DirectError("上传成功但无 file id: " + json.dumps(biz, ensure_ascii=False)[:200])
        status = (node.get("status") or node.get("file_status") or "").upper()
        deadline = time.time() + timeout_s
        while status not in ("SUCCESS", "COMPLETED") and time.time() < deadline:
            time.sleep(1.0)
            r2 = http.get("/api/v0/file/fetch_files", params={"file_ids": file_id},
                          headers=_headers(token, device_uuid))
            biz2 = _unwrap(r2.json())
            flist = biz2.get("files")
            if isinstance(flist, list) and flist:
                node = flist[0]
                status = (node.get("status") or node.get("file_status") or "").upper()
            elif isinstance(biz2, dict) and biz2.get("id"):
                status = (biz2.get("status") or biz2.get("file_status") or "").upper()
        if status not in ("SUCCESS", "COMPLETED"):
            raise DirectError("图片解析未就绪: status=%s" % status)
        return file_id


def completion(token, device_uuid, prompt, file_ids=None, thinking=True,
               proxy=None, timeout_s=600.0):
    """新建会话 → SSE 补全 → 返回 RESPONSE 全文（THINK 除外）。"""
    from .pow import DeepSeekPow
    pow_solver = DeepSeekPow()
    with _client(proxy) as http:
        r = http.post("/api/v0/chat_session/create", json={},
                      headers=_headers(token, device_uuid))
        session_id = _unwrap(r.json())["chat_session"]["id"]
        body = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "prompt": prompt,
            "ref_file_ids": file_ids or [],
            "thinking_enabled": bool(thinking),
            "search_enabled": False,
            "action": None,
            "preempt": False,
            "model_type": "default",
        }
        pow_h = _pow_header(pow_solver, http, token, device_uuid, "/api/v0/chat/completion")
        out = []
        with http.stream("POST", "/api/v0/chat/completion", json=body,
                         headers=_headers(token, device_uuid, {
                             "content-type": "application/json",
                             "x-ds-pow-response": pow_h,
                             "x-thinking-enabled": "1" if thinking else "0",
                             "x-model-type": "default",
                         }),
                         timeout=httpx.Timeout(30.0, read=timeout_s)) as resp:
            resp.raise_for_status()
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
                data = event.get("data")
                if not isinstance(data, dict):
                    continue
                biz = data.get("biz_data")
                if not isinstance(biz, dict):
                    continue
                resp_obj = biz.get("response") or biz
                frags = resp_obj.get("fragments")
                if isinstance(frags, list):
                    for f in frags:
                        if isinstance(f, dict) and (f.get("type") or "RESPONSE") == "RESPONSE":
                            out.append(f.get("content") or "")
                patch = data.get("patch") or biz.get("patch")
                if isinstance(patch, list):
                    for p in patch:
                        if isinstance(p, dict) and (p.get("p") or "").endswith("/content") \
                                and p.get("o") == "APPEND" and isinstance(p.get("v"), str):
                            out.append(p["v"])
        return "".join(out)
