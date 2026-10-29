# src/backends/sandbox_setup.py
"""
OpenSandbox 沙箱的初始化与文件播种模块。

职责:
1. 获取或创建 OpenSandbox 沙箱，包装为 OpenSandboxBackend。
2. 播种技能文件（技能包 SKILL.md）。

注意：AGENTS.md 已迁移到 StoreBackend（全局共享），不经过沙箱。
用户长期记忆（/memories/）由 CompositeBackend 路由到 StoreBackend 持久化。
运行时的增量技能同步由 SkillsSyncMiddleware 负责。
"""
from datetime import timedelta
import os
import shlex
from pathlib import Path
from typing import List, Tuple

from opensandbox import SandboxSync

from agent.backends.custom_opensandbox import OpenSandboxBackend
from agent.config import (
    LOCAL_SKILLS_DIR, SANDBOX_SKILLS_ROOT,
    SANDBOX_EGRESS_MODE, SANDBOX_EGRESS_ALLOWED_HOSTS, SUPPLIER_URL_MAPPING,
)

# 出网策略需要 NetworkPolicy / NetworkRule 两个类型，不同版本的 opensandbox
# 导出位置不一致（config 或 models）。这里做兼容导入：都导不到时由
# _network_policy_kwargs() 默认 fail-closed 抛错（仅 SANDBOX_EGRESS_FAIL_OPEN=1
# 才降级为不上策略），避免"悄悄放开出网"。
try:  # pragma: no cover - 依赖具体安装的 SDK 版本
    from opensandbox.config import NetworkPolicy, NetworkRule  # type: ignore
except Exception:  # noqa: BLE001
    try:
        from opensandbox.models import NetworkPolicy, NetworkRule  # type: ignore
    except Exception:  # noqa: BLE001
        NetworkPolicy = None  # type: ignore
        NetworkRule = None  # type: ignore


def _collect_egress_hosts() -> List[str]:
    """汇总沙箱允许出网的主机白名单。

    三段来源（去重、保序）：
      1. config.SANDBOX_EGRESS_ALLOWED_HOSTS：基础设施类（PyPI 镜像等）；
      2. supplier-price-urls/data/url_mapping.yaml 里出现的报价站点主机：
         技能要在沙箱内抓这些页面取实时报价，因此必须放行——白名单若漏了它们，
         表现是"比价分析拿不到外部数据"，而不是报错，很难排查，所以这里自动提取；
      3. 环境变量 SANDBOX_EGRESS_EXTRA_HOSTS（逗号分隔），用于临时追加。
    """
    hosts: List[str] = list(SANDBOX_EGRESS_ALLOWED_HOSTS)

    # 2. 从报价 URL 映射表自动提取主机
    try:
        import yaml  # 惰性导入：本模块被 import 时不额外拉起 yaml
        from urllib.parse import urlparse

        if SUPPLIER_URL_MAPPING.exists():
            mapping = yaml.safe_load(SUPPLIER_URL_MAPPING.read_text(encoding="utf-8")) or {}
            for item in (mapping.get("mappings") or []):
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                host = urlparse(url).hostname
                if host and host not in hosts:
                    hosts.append(host)
    except Exception as e:  # noqa: BLE001
        print(f"[WARNING] 读取报价 URL 映射表失败，出网白名单可能不完整: {e}")

    # 3. 运行期追加
    extra = os.environ.get("SANDBOX_EGRESS_EXTRA_HOSTS", "")
    for h in extra.split(","):
        h = h.strip()
        if h and h not in hosts:
            hosts.append(h)

    return hosts


def _egress_fail_open() -> bool:
    """出网策略构造失败时是否允许"降级放行"。

    默认 fail-closed：NetworkPolicy 导不进来 / 构造失败时，宁可让沙箱启动失败，
    也不能静默返回空参数——那样 SandboxSync.create 收不到 network_policy，
    等价于沙箱可以访问任意公网（数据外泄）与内网地址（SSRF），
    而调用方只看到一行 WARNING，很容易被忽略。
    只有在明确接受这一风险时（本地排障），设置 SANDBOX_EGRESS_FAIL_OPEN=1 才降级。
    """
    return os.environ.get("SANDBOX_EGRESS_FAIL_OPEN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _network_policy_kwargs() -> dict:
    """构造 SandboxSync.create 的出网策略参数。

    Returns:
        {"network_policy": NetworkPolicy(...)}；仅在显式关闭时返回 {}。

    Raises:
        RuntimeError: 出网策略不可用且未显式设置 SANDBOX_EGRESS_FAIL_OPEN=1。

    为什么默认"拒绝 + 白名单"，而不是沿用原来的注释掉（等价于全放开）：
    沙箱里跑的是技能脚本与模型临时代码，全放开意味着它可以访问任意公网（数据外泄）
    以及内网地址（SSRF）。白名单把"能出到哪"变成一条显式配置。
    """
    if SANDBOX_EGRESS_MODE == "open":
        print("[WARNING] 沙箱出网策略为 open（等价于不限制），仅建议本地排障时使用")
        return {}

    if NetworkPolicy is None or NetworkRule is None:
        msg = (
            "未能导入 NetworkPolicy/NetworkRule，出网限制无法生效，"
            "拒绝以保证安全（fail-closed）。请确认已安装的 opensandbox 版本中这两个类型的导出位置。"
        )
        if _egress_fail_open():
            print(f"[WARNING] {msg} 已因 SANDBOX_EGRESS_FAIL_OPEN=1 降级为不限制出网（风险自负）。")
            return {}
        raise RuntimeError(msg + " 如需临时放开，请设置 SANDBOX_EGRESS_FAIL_OPEN=1。")

    hosts = _collect_egress_hosts()
    if not hosts:
        print("[WARNING] 出网白名单为空，将拒绝对外网络（技能内依赖安装与网页抓取会失败）")

    try:
        policy = NetworkPolicy(
            defaultAction="deny",
            egress=[NetworkRule(action="allow", target=h) for h in hosts],
        )
        print(f"[INFO] 沙箱出网白名单（{len(hosts)}）: {', '.join(hosts)}")
        return {"network_policy": policy}
    except Exception as e:  # noqa: BLE001
        # 参数名/结构与当前 SDK 不匹配时默认阻断启动：没有策略就是全放开，
        # 这是安全问题而不是"少一层防护"，不能只打 WARNING 混过去。
        msg = f"构造 NetworkPolicy 失败，出网限制无法生效: {e}"
        if _egress_fail_open():
            print(f"[WARNING] {msg} 已因 SANDBOX_EGRESS_FAIL_OPEN=1 降级为不限制出网（风险自负）。")
            return {}
        raise RuntimeError(msg + " 如需临时放开，请设置 SANDBOX_EGRESS_FAIL_OPEN=1。") from e


def setup_sandbox(config, sandbox_id=None, image=None) -> OpenSandboxBackend:
    """
    获取或创建沙箱，播种基础文件。

    Args:
        config: ConnectionConfigSync 配置。
        sandbox_id: 可选，要连接的现有沙箱 ID。
        image: 可选，创建新沙箱时使用的镜像。

    Returns:
        OpenSandboxBackend 实例。
    """
    if sandbox_id:
        print(f"[INFO] 正在连接到现有沙箱: {sandbox_id}")
        try:
            sandbox = SandboxSync.connect(sandbox_id, connection_config=config)
            print(f"[INFO] 成功连接到沙箱: {sandbox_id}")
        except Exception as e:
            print(f"[WARNING] 连接沙箱失败: {e}，将创建新沙箱")
            sandbox_id = None

    if not sandbox_id:
        if not image:
            image = "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.0.2"

        print(f"[INFO] 正在创建新沙箱，使用镜像: {image}")
        sandbox = SandboxSync.create(
            image,
            entrypoint=["/opt/opensandbox/code-interpreter.sh"],
            env={"PYTHON_VERSION": "3.11"},
            resource={"cpu": "2", "memory": "4Gi"},
            timeout=timedelta(hours=2),
            connection_config=config,
            # 出网白名单：默认 deny + 放行必要主机（见 _network_policy_kwargs）
            **_network_policy_kwargs(),
        )

    backend = OpenSandboxBackend(sandbox=sandbox)
    print(f"[INFO] 沙箱就绪，ID: {sandbox.id}")

    # 预创建 skills 需要的目录，避免 Agent 运行时遇到 FileNotFoundError
    _ensure_dirs(backend)

    # 播种基础文件（AGENTS.md、Skills）
    _seed_files(backend)

    # 创建 Python venv + 预装第三方依赖
    _create_venv(backend)

    return backend


# skills 运行时依赖的目录（需在沙箱中预创建）
_SKILL_DIRS = ["/analysis/temp"]

# 所有 Python 依赖统一安装到此 venv，避开系统 Python 的 externally managed 限制
_VENV_PATH = "/opt/skills-venv"
_VENV_PIP = f"{_VENV_PATH}/bin/pip"
# skills 运行时需要的 Python 第三方包
_PREINSTALL_PACKAGES = ["numpy", "pandas", "matplotlib", "requests", "beautifulsoup4"]


def _ensure_dirs(backend: OpenSandboxBackend) -> None:
    """预创建 skills 运行所需的目录，避免 FileNotFoundError。"""
    for d in _SKILL_DIRS:
        backend.execute(f"mkdir -p {d}")


# 阿里云 PyPI 镜像，沙箱内走内网加速
_PYPI_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
# pip install 通用参数
_PIP_INSTALL_ARGS = f"-i {_PYPI_INDEX} --default-timeout=60 --no-input -q"


def _create_venv(backend: OpenSandboxBackend) -> None:
    """创建沙箱级 Python venv，并预装 skills 所需的第三方包。

    系统 Python 设置了 externally managed 限制（PEP 668），--system 安装会被拒绝。
    因此创建一个统一的 venv，并将 /opt/skills-venv/bin 注入 SANDBOX_PATH 最前面，
    所有 skill 的 python/pip 命令自动路由到 venv，无需改任何脚本。
    """
    # 1. 创建 venv（幂等：已存在则跳过）
    result = backend.execute(f"python3 -m venv {_VENV_PATH}")
    if result.exit_code != 0:
        print(f"[WARNING] venv 创建失败: {result.output[:200]}")
        return
    print(f"[INFO] Python venv 就绪: {_VENV_PATH}")

    # 2. 升级 pip（镜像加速，60s 超时）
    backend.execute(f"{_VENV_PIP} install --upgrade pip {_PIP_INSTALL_ARGS}", timeout=60)

    # 3. 预装依赖（sentinel 避免重复安装）
    for pkg in _PREINSTALL_PACKAGES:
        sentinel = f"/tmp/.venv_installed_{pkg}"
        check = backend.execute(f"test -f {sentinel}")
        if check.exit_code == 0:
            continue
        print(f"[INFO] 正在安装 Python 依赖: {pkg}...")
        result = backend.execute(
            f"{_VENV_PIP} install {pkg} {_PIP_INSTALL_ARGS}",
            timeout=120,
        )
        if result.exit_code == 0:
            backend.execute(f"touch {sentinel}")
            print(f"[INFO]   {pkg} 安装成功")
        else:
            print(f"[WARNING] {pkg} 安装失败: {result.output[:200]}")


def _seed_files(backend: OpenSandboxBackend) -> None:
    """
    将本地技能文件上传到沙箱。

    AGENTS.md 已迁移到 StoreBackend（全局共享，不经过沙箱）。
    仅上传在沙箱中尚不存在的文件，避免覆盖已更新的内容。
    """
    file_mapping: List[Tuple[Path, str]] = []

    # 遍历 skills 目录，添加所有技能文件
    skills_base = Path(LOCAL_SKILLS_DIR)
    if skills_base.exists():
        for skill_dir in skills_base.iterdir():
            if not skill_dir.is_dir():
                continue
            for local_file in skill_dir.rglob("*"):
                if local_file.is_file():
                    rel = local_file.relative_to(skills_base).as_posix()
                    sandbox_path = f"{SANDBOX_SKILLS_ROOT}/{rel}"
                    file_mapping.append((local_file, sandbox_path))

    # 收集需要上传的文件
    to_upload: List[Tuple[str, bytes]] = []
    for local_path, sandbox_path in file_mapping:
        if not local_path.exists():
            continue
        #read_bytes() 读取文件内容，返回 bytes 类型
        local_content = local_path.read_bytes()
        # 用 test -f 检测文件是否存在（无 ERROR 日志），避免 download_files 对 404 打 ERROR
        # 路径可能含空格/元字符，拼命令前必须 quote
        check = backend.execute(f"test -f {shlex.quote(sandbox_path)}")
        if check.exit_code == 0:
            try:
                results = backend.download_files([sandbox_path])
                if results and results[0].content and not results[0].error:
                    remote_content = results[0].content
                    if isinstance(remote_content, str):
                        remote_content = remote_content.encode("utf-8")
                    if remote_content == local_content:
                        continue
            except Exception:
                pass
        to_upload.append((sandbox_path, local_content))

    if to_upload:
        print(f"[INFO] 正在上传 {len(to_upload)} 个基础文件...")
        backend.upload_files(to_upload)
        print("[INFO] 基础文件上传完成。")
    else:
        print("[INFO] 所有基础文件已就绪，无需上传。")