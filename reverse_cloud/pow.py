# -*- coding: utf-8 -*-
"""DeepSeek PoW（DeepSeekHashV1）纯 Python 求解器。

DeepSeekHashV1 = SHA3-256 但跳过 Keccak-f[1600] 的第 0 轮（只做 round 1..23）。
numpy 批量并行：difficulty 144000 时实测 ~0.5s（纯 Python 标量要几十秒）。

正确性双保险（本机实测）：
  · 完整 24 轮版本与 hashlib.sha3_256 逐字节一致 → 置换本体是教科书实现
  · 与 ds2api(Go) 的 pow 包逐轮比对状态一致 → 轮常数表取自同一份权威源码
"""
from __future__ import annotations

import base64
import json

import numpy as np

# Keccak-f[1600] 轮常数（与 ds2api pow/deepseek_hash.go 同源）
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_RATE = 136  # SHA3-256 rate（字节）
_MASK = np.uint64(0xFFFFFFFFFFFFFFFF)

# rho+pi 行走表（FIPS 202）：lane 索引 i = x + 5y
_DST, _SRC, _ROT = [], [], []
_x, _y = 1, 0
for _i in range(24):
    _nx, _ny = _y, (2 * _x + 3 * _y) % 5
    _DST.append(_nx + 5 * _ny)
    _SRC.append(_x + 5 * _y)
    _ROT.append(((_i + 1) * (_i + 2) // 2) % 64)
    _x, _y = _nx, _ny


def _rotl(v, k):
    k = int(k)
    if k == 0:
        return v
    return ((v << np.uint64(k)) | (v >> np.uint64(64 - k))) & _MASK


def _rounds(state, start, end):
    """state: (N,25) uint64，就地写回，做 Keccak-f 的 [start,end) 轮。

    lane 约定 i = x + 5y → reshape(N,5,5) 后轴序是 [y, x]。
    """
    n = state.shape[0]
    cur = state
    for r in range(start, end):
        a = cur.reshape(n, 5, 5)
        # theta：C[x] = XOR over y
        c = a[:, 0] ^ a[:, 1] ^ a[:, 2] ^ a[:, 3] ^ a[:, 4]
        d = np.empty_like(c)
        d[:, 0] = c[:, 4] ^ _rotl(c[:, 1], 1)
        d[:, 1] = c[:, 0] ^ _rotl(c[:, 2], 1)
        d[:, 2] = c[:, 1] ^ _rotl(c[:, 3], 1)
        d[:, 3] = c[:, 2] ^ _rotl(c[:, 4], 1)
        d[:, 4] = c[:, 3] ^ _rotl(c[:, 0], 1)
        cur = (cur ^ np.broadcast_to(d[:, None, :], (n, 5, 5)).reshape(n, 25))

        # rho + pi（lane 0 不变；走法与 FIPS 202 一致）
        b = np.empty_like(cur)
        b[:, 0] = cur[:, 0]
        for j in range(24):
            b[:, _DST[j]] = _rotl(cur[:, _SRC[j]], _ROT[j])

        # chi：A'[x][y] = B[x][y] ^ ((~B[x+1][y]) & B[x+2][y])，x 沿最后一轴
        bb = b.reshape(n, 5, 5)
        cur = (bb ^ (((~np.roll(bb, -1, axis=2)) & _MASK) & np.roll(bb, -2, axis=2))).reshape(n, 25)

        # iota
        cur[:, 0] = cur[:, 0] ^ np.uint64(_RC[r])
    state[...] = cur
    return state


def build_block(prefix: bytes, nonce: int) -> bytes:
    """SHA3 填充单块：prefix||decimal(nonce) + 0x06 … 末字节 |= 0x80（要求总长 < 136）。"""
    body = prefix + str(int(nonce)).encode()
    if len(body) >= _RATE:
        raise ValueError("PoW 前缀过长（超出一个 rate 块）")
    buf = bytearray(_RATE)
    buf[:len(body)] = body
    buf[len(body)] = 0x06
    buf[_RATE - 1] |= 0x80
    return bytes(buf)


def _hash_blocks(blocks: np.ndarray, rounds_start: int) -> np.ndarray:
    """blocks: (N,136) uint8 → (N,4) uint64 摘要 lane。"""
    states = np.zeros((blocks.shape[0], 25), dtype=np.uint64)
    lanes = blocks.view("<u8").reshape(blocks.shape[0], _RATE // 8)
    states[:, 0:_RATE // 8] = states[:, 0:_RATE // 8] ^ lanes
    _rounds(states, rounds_start, 24)
    return states[:, 0:4]


def deepseek_hash_v1(prefix: bytes, nonces, chunk: int = 20000) -> np.ndarray:
    """批量算 DeepSeekHashV1(prefix + str(nonce))，返回 (len(nonces),4) uint64。"""
    out = []
    for lo in range(0, len(nonces), chunk):
        sel = nonces[lo:lo + chunk]
        blocks = np.frombuffer(b"".join(build_block(prefix, n) for n in sel), dtype=np.uint8)
        out.append(_hash_blocks(blocks.reshape(len(sel), _RATE), 1))
    return np.concatenate(out) if len(out) > 1 else out[0]


def solve(challenge_hex: str, salt: str, expire_at, difficulty, chunk: int = 20000) -> int:
    """搜 nonce ∈ [0, difficulty) 使摘要等于 challenge。"""
    target = np.frombuffer(bytes.fromhex(challenge_hex), dtype="<u8").astype(np.uint64).reshape(1, 4)
    prefix = (salt + "_" + str(int(expire_at)) + "_").encode()
    difficulty = int(difficulty)
    for lo in range(0, difficulty, chunk):
        hi = min(lo + chunk, difficulty)
        blocks = np.frombuffer(b"".join(build_block(prefix, i) for i in range(lo, hi)), dtype=np.uint8)
        got = _hash_blocks(blocks.reshape(hi - lo, _RATE), 1)
        hit = np.where((got == target).all(axis=1))[0]
        if len(hit):
            return lo + int(hit[0])
    raise RuntimeError("PoW 在 difficulty 内无解")


def make_header(challenge: dict, answer: int) -> str:
    """base64(JSON)，键顺序必须与真实网页客户端 JSON.stringify 一致。"""
    ordered = {
        "algorithm": challenge.get("algorithm"),
        "challenge": challenge.get("challenge"),
        "salt": challenge.get("salt"),
        "answer": int(answer),
        "signature": challenge.get("signature"),
        "target_path": challenge.get("target_path"),
    }
    raw = json.dumps(ordered, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()


# ---------- 自检：完整 24 轮必须等于 hashlib.sha3_256 ----------
def self_test() -> bool:
    import hashlib
    for text in (b"", b"abc", b"deepseek", b"x" * 100, b"y" * 130):
        # 只覆盖单块情形（填充后必须仍是一个 rate 块）
        if len(text) > _RATE - 2:
            continue
        body = bytearray(text)
        body.append(0x06)
        while len(body) % _RATE:
            body.append(0x00)
        body[-1] ^= 0x80
        blocks = np.frombuffer(bytes(body), dtype=np.uint8).reshape(1, _RATE)
        got = _hash_blocks(blocks, 0)[0].astype("<u8").tobytes()
        if got != hashlib.sha3_256(text).digest():
            return False
    return True
