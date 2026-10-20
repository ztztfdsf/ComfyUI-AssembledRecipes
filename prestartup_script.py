"""Windows 下把 stdout/stderr 包成 UTF-8（errors=replace）。

ComfyUI 无控制台/重定向启动时，文本流默认走系统 GBK 编码，任何插件 print
emoji（如 rgthree 的加载消息 🎉）都会 UnicodeEncodeError 直接炸掉整个启动。
此前是 prompt-assistant 在 import 期顺手 wrap 的（暗依赖副作用），本插件
把它变成明面能力——prestartup 早于一切插件加载，无论从启动器还是命令行
启动都受保护。  —— github.com/ztztfdsf/ComfyUI-AssembledRecipes
"""
import io
import sys

if sys.platform == "win32":
    try:
        enc = (getattr(sys.stdout, "encoding", None) or "").lower()
        if enc not in ("utf-8", "utf8"):
            if getattr(sys.stdout, "buffer", None) is not None:
                sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            if getattr(sys.stderr, "buffer", None) is not None:
                sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
