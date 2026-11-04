"""启动 Web 服务的便捷脚本（等价于 uvicorn 命令，端口自动取自配置）。

用法：
    python scripts/run_web.py            # 前台启动，打开 http://127.0.0.1:8000
    $env:CNCOPS_API_PORT=8080; python scripts/run_web.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from cncops.config import settings

    cfg = settings()
    try:
        import uvicorn
    except ImportError:
        print("缺少依赖 uvicorn，请执行：pip install -r requirements.txt")
        return 1

    print(f"启动 Web 服务： http://{cfg.api_host}:{cfg.api_port}   （数据源：{cfg.data_source}）")
    print("演示模式：界面右上角开关；服务端总开关 CNCOPS_DEMO_ALLOW="
          f"{'开' if cfg.demo_allowed else '关'}")
    uvicorn.run("cncops.api:app", host=cfg.api_host, port=cfg.api_port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
