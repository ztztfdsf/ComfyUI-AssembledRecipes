# -*- coding: utf-8 -*-
"""自定义 OpenAI 兼容端点的本地端点库。

为什么要这个：base / key / model 原本是节点的普通 widget，于是它们会——
  ① 跟着工作流存进 user/default/workflows/*.json；
  ② 被 ComfyUI 写进每张出图 PNG 的 workflow 元数据（发给别人图就等于发了 key）；
  ③ 进 /history 与队列存档。
本模块把这三项挪到本机一个文件里，工作流只带一个**端点名字**。

文件：<ComfyUI user>/AssembledRecipes/openai_endpoints.json
  {"endpoints": [{"name": "...", "base": "...", "key": "...", "model": "...",
                  "models": [...拉过的列表...], "added_at": ..., "last_ok_at": ...}],
   "current": "..."}

铁律：key 只在本机文件里；日志/报错/状态接口一律不回显 key（只说有没有）。
"""

from __future__ import annotations

import json
import os
import threading
import time

_LOCK = threading.RLock()
CURRENT_FALLBACK = "（本地当前端点）"


def config_path() -> str:
    try:
        import folder_paths
        base = folder_paths.get_user_directory()
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".config", "comfy")
    return os.path.join(base, "AssembledRecipes", "openai_endpoints.json")


def _read_raw() -> dict:
    p = config_path()
    if not os.path.isfile(p):
        return {"endpoints": [], "current": ""}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"endpoints": [], "current": ""}
    if not isinstance(data, dict):
        data = {"endpoints": [], "current": ""}
    data.setdefault("endpoints", [])
    data.setdefault("current", "")
    return data


def _write_raw(cfg: dict) -> None:
    p = config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def endpoints() -> list:
    with _LOCK:
        rows = [e for e in _read_raw()["endpoints"] if isinstance(e, dict) and e.get("name")]
    for e in rows:
        e.setdefault("base", "")
        e.setdefault("key", "")
        e.setdefault("model", "")
        e.setdefault("models", [])
    return rows


def current_name() -> str:
    with _LOCK:
        return str(_read_raw().get("current") or "")


def names() -> list:
    return [e["name"] for e in endpoints()]


def _find(rows: list, name: str):
    name = (name or "").strip()
    for e in rows:
        if e["name"] == name:
            return e
    return None


def resolve(name: str = "") -> dict:
    """按名字取端点；名字空/等于兜底项 → 用 current。取不到抛错并列出可用名字。"""
    rows = endpoints()
    name = (name or "").strip()
    if not name or name == CURRENT_FALLBACK:
        name = current_name()
    hit = _find(rows, name) if name else None
    if hit is None and rows and not name:
        hit = rows[0]
    if hit is None:
        avail = "、".join(e["name"] for e in rows) or "（端点库是空的）"
        raise KeyError("找不到端点 %r；本机可用：%s" % (name, avail))
    if not (hit.get("base") or "").strip():
        raise KeyError("端点 %r 没填请求地址" % hit["name"])
    return hit


def upsert(name: str, base: str = "", key: str | None = None, model: str = "",
           set_current: bool = True) -> dict:
    """新增或更新一个端点。key 传 None = 保留原来那把（面板不回显密钥）。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("端点名字不能空")
    with _LOCK:
        cfg = _read_raw()
        hit = _find(cfg["endpoints"], name)
        if hit is None:
            hit = {"name": name, "added_at": time.time(), "last_ok_at": 0.0}
            cfg["endpoints"].append(hit)
        if base.strip():
            hit["base"] = base.strip()
        if key is not None:
            hit["key"] = key
        if model.strip():
            hit["model"] = model.strip()
        hit.setdefault("models", [])
        if set_current:
            cfg["current"] = name
        _write_raw(cfg)
        return dict(hit)


def remove(name: str) -> bool:
    with _LOCK:
        cfg = _read_raw()
        left = [e for e in cfg["endpoints"] if e.get("name") != name]
        if len(left) == len(cfg["endpoints"]):
            return False
        cfg["endpoints"] = left
        if cfg.get("current") == name:
            cfg["current"] = left[0]["name"] if left else ""
        _write_raw(cfg)
        return True


def set_current(name: str) -> bool:
    with _LOCK:
        cfg = _read_raw()
        if not _find(cfg["endpoints"], name):
            return False
        cfg["current"] = name
        _write_raw(cfg)
        return True


def cache_models(name: str, models: list) -> None:
    with _LOCK:
        cfg = _read_raw()
        hit = _find(cfg["endpoints"], name)
        if hit is None:
            return
        hit["models"] = list(models)[:400]
        _write_raw(cfg)


def public_view() -> dict:
    """给面板/状态接口用的形状：**绝不含密钥**，只说有没有。"""
    rows = endpoints()
    cur = current_name()
    return {
        "current": cur,
        "endpoints": [{
            "name": e["name"],
            "base": e.get("base") or "",
            "model": e.get("model") or "",
            "has_key": bool((e.get("key") or "").strip()),
            "key_hint": (e.get("key") or "")[:4] + "…" if e.get("key") else "",
            "models": e.get("models") or [],
            "added_at": e.get("added_at") or 0.0,
            "last_ok_at": e.get("last_ok_at") or 0.0,
        } for e in rows],
        "path": config_path(),
    }
