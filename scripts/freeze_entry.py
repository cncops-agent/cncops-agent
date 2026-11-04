"""PyInstaller 冻结后的启动入口：起 Web 服务 + 自动打开浏览器（Demo 用）。

开发态也能直接 `python scripts/freeze_entry.py` 运行，效果等同 run_web.py，
但额外做了一步"等服务健康后自动打开默认浏览器"。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

if getattr(sys, "frozen", False):
    # 冻结态：cncops 已由 PyInstaller 打包进可执行文件，无需把 src 加进路径
    pass
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _open_browser(page: str):
    """尽力打开默认浏览器，返回 (是否成功, 使用的方式或失败原因)。

    冻结 exe 里 webbrowser 在子线程调用 os.startfile 可能因 COM 未初始化而静默失败，
    因此按顺序多重兜底，并把结果明确打印到控制台，不再吞异常。
    """
    import ctypes
    import subprocess

    errors = []
    if sys.platform == "win32":
        # ShellExecute 系列 API 要求调用线程初始化 COM；子线程默认没有，先补上
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        except Exception as exc:
            errors.append(f"CoInitializeEx={exc!r}")

    # 1) 系统默认 http 关联（最贴近用户习惯）
    if hasattr(os, "startfile"):
        try:
            os.startfile(page)
            return True, "系统默认浏览器关联（os.startfile）"
        except Exception as exc:
            errors.append(f"startfile={exc!r}")

    # 2) Python 标准库
    try:
        if webbrowser.open(page):
            return True, "webbrowser 标准库"
        errors.append("webbrowser 返回 False")
    except Exception as exc:
        errors.append(f"webbrowser={exc!r}")

    # 3) 交给独立 cmd 进程执行 start，避开本线程的 COM / 权限上下文
    if sys.platform == "win32":
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", page],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True, "cmd /c start（独立进程）"
        except Exception as exc:
            errors.append(f"cmd start={exc!r}")

    return False, " | ".join(errors) if errors else "未知原因"


def _wait_and_open(url: str) -> None:
    """轮询健康检查，就绪后打开浏览器；失败时把地址与原因明确打印，不静默吞掉。"""
    page = url.replace("/api/health", "")
    deadline = time.time() + 30.0
    ready = False
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.4)
    print(f"\n  服务{'已就绪' if ready else '启动较慢'}，正在打开浏览器：{page}", flush=True)
    ok, how = _open_browser(page)
    if ok:
        print(f"  已通过「{how}」发起打开；若没看到窗口，请手动访问：{page}", flush=True)
    else:
        print(f"  [注意] 自动打开浏览器失败：{how}", flush=True)
        print(f"  请手动复制下面地址到浏览器打开：\n  {page}", flush=True)


def main() -> int:
    # 先设环境变量，再读取配置（settings 有 lru_cache，必须在其首次调用前设定）
    os.environ.setdefault("CNCOPS_DEMO_ALLOW", "1")   # 演示注入默认开（数据源本身为仿真器）
    os.environ.setdefault("CNCOPS_LOG_LEVEL", "WARNING")

    from cncops import paths
    from cncops.config import settings

    paths.ensure_dirs()  # 确保 data/ logs/ 存在（冻结态指向可写目录）
    cfg = settings()

    from cncops.api import app
    import uvicorn

    url = f"http://{cfg.api_host}:{cfg.api_port}"
    print("=" * 58, flush=True)
    print("  cncops-agent · 中小加工厂设备运维 AI 诊断助手（演示版）", flush=True)
    print("=" * 58, flush=True)
    print(f"  本机访问地址： {url}", flush=True)
    print("  正在启动服务，浏览器将自动打开……", flush=True)
    print("  若浏览器没弹出，直接复制上面的地址到浏览器打开即可。", flush=True)
    print("  关闭本窗口即可停止服务。", flush=True)
    print("=" * 58, flush=True)

    threading.Thread(
        target=_wait_and_open, args=(url + "/api/health",), daemon=True
    ).start()
    try:
        uvicorn.run(app, host=cfg.api_host, port=cfg.api_port,
                    reload=False, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
