"""一键启动脚本：后端 FastAPI(8090) + 前端 Vite(3000)。

设计取舍（为什么不用 docker compose / 进程管理器）：
    - 本地开发只要"一条命令起来"，subprocess 足够，多引入一层反而增加排障成本；
    - 后端必须等 /health 通过再起前端，否则前端首屏全是 502，会把人带到错误的方向；
    - 子进程输出实时转发并加来源前缀，前后端日志在同一终端里可读；
    - Ctrl+C / SIGTERM 统一清理，避免端口被僵尸进程占住。

用法：
    python start_web.py                 # 后端 + 前端
    python start_web.py --backend-only  # 只起后端（调 API 时够用）
    python start_web.py --frontend-only # 只起前端（后端已在别处跑）

环境变量：BACKEND_PORT（默认 8090）、FRONTEND_PORT（默认 3000）、READY_TIMEOUT（默认 180s）
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8090"))
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", "3000"))
READY_TIMEOUT = int(os.environ.get("READY_TIMEOUT", "180"))

_processes: List[subprocess.Popen] = []


def _pump(stream, prefix: str) -> None:
    """把子进程输出逐行转发到当前终端（带来源前缀）。"""
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            print(f"[{prefix}] {line}", flush=True)


def _spawn(cmd, cwd: Path, env: dict, prefix: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    threading.Thread(target=_pump, args=(proc.stdout, prefix), daemon=True).start()
    _processes.append(proc)
    return proc


def run_backend(reload: bool = False) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_DIR), env.get("PYTHONPATH", "")]))
    print(f"[System] 启动后端 uvicorn → http://localhost:{BACKEND_PORT}", flush=True)
    cmd = [
        sys.executable, "-m", "uvicorn", "api_view.web_main:app",
        "--host", "0.0.0.0", "--port", str(BACKEND_PORT),
    ]
    if reload:
        # 热重载会多开一个 watcher 进程；默认关掉，避免改一个文件触发整条 Agent 冷启动
        cmd.append("--reload")
    return _spawn(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        prefix="Backend",
    )


def wait_backend_ready(timeout: int = READY_TIMEOUT) -> bool:
    """轮询 /health，直到后端返回 healthy 或超时。

    Agent 首次初始化要创建沙箱、加载 MCP 工具，30-60 秒属正常，
    所以这里等的是"服务真能干活"，而不是"进程还活着"。
    """
    url = f"http://localhost:{BACKEND_PORT}/health"
    started = time.time()
    deadline = started + timeout
    print(f"[System] 等待后端就绪（最长 {timeout}s，首次含 Agent 冷启动）...", flush=True)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200 and b"healthy" in resp.read():
                    print("[System] 后端已就绪", flush=True)
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass  # 还没起来，继续等
        time.sleep(2)
        print(f"[System] 等待中... ({int(time.time() - started)}s / {timeout}s)", flush=True)
    return False


def run_frontend() -> Optional[subprocess.Popen]:
    if not FRONTEND_DIR.exists():
        print(f"[Frontend] 目录不存在：{FRONTEND_DIR}", flush=True)
        return None

    if not (FRONTEND_DIR / "node_modules").exists():
        print("[Frontend] 未安装依赖，执行 npm install ...", flush=True)
        result = subprocess.run("npm install", cwd=str(FRONTEND_DIR), shell=True)
        if result.returncode != 0:
            print("[Frontend] npm install 失败，已跳过前端启动", flush=True)
            return None

    print(f"[System] 启动前端 vite → http://localhost:{FRONTEND_PORT}", flush=True)
    return _spawn("npm run dev", cwd=FRONTEND_DIR, env=os.environ.copy(), prefix="Frontend")


def shutdown(*_args) -> None:
    print("\n[System] 正在停止服务...", flush=True)
    for proc in reversed(_processes):
        if proc.poll() is not None:
            continue
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("[System] 已全部停止", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="QingchiERP 采购智能助手 — 本地启动器")
    parser.add_argument("--backend-only", action="store_true", help="只启动后端")
    parser.add_argument("--frontend-only", action="store_true", help="只启动前端")
    parser.add_argument("--reload", action="store_true", help="后端启用热重载（会重复冷启动 Agent）")
    args = parser.parse_args()

    def _handle_signal(*_a):
        shutdown()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass  # 某些平台/线程上下文不支持，忽略即可

    print("=" * 62)
    print("  QingchiERP 采购智能助手 — 本地开发启动器")
    print("=" * 62)

    try:
        if not args.frontend_only:
            run_backend(reload=args.reload)
            if not wait_backend_ready():
                print("[System] 后端在限定时间内未就绪，请查看上方 Backend 日志", flush=True)
                print("[System] 常见原因：.env 缺密钥 / PostgreSQL 未启动 / 沙箱服务未启动", flush=True)
                shutdown()
                return 1

        if not args.backend_only:
            run_frontend()

        print()
        print("=" * 62)
        print(f"  前端      http://localhost:{FRONTEND_PORT}")
        print(f"  API 文档  http://localhost:{BACKEND_PORT}/docs")
        print("  Ctrl+C 停止全部服务")
        print("=" * 62)

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()
        return 0
    except Exception as exc:  # 兜底：异常也要清理子进程，不留僵尸
        print(f"[System] 启动失败：{exc}", flush=True)
        shutdown()
        return 1


if __name__ == "__main__":
    sys.exit(main())
