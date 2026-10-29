# src/middlewares/user_skills_restore.py
"""
技能恢复中间件

在每个 Agent 运行周期开始前，将 StoreBackend 中持久化的技能
恢复到沙箱 /skills/{scope}/{skill_name}/ 路径下，使子 Agent 可以通过
渐进式披露发现和使用。

与 SkillsSyncMiddleware 分工：
  - SkillsSyncMiddleware: 本地 src/skills/ → 沙箱（预置技能）
  - UserSkillsRestoreMiddleware: StoreBackend → 沙箱（持久化技能）
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

from langchain.agents.middleware import AgentMiddleware


class UserSkillsRestoreMiddleware(AgentMiddleware):
    """从 StoreBackend 恢复持久化技能到沙箱的中间件。"""

    def __init__(self, backend, skills_namespace) -> None:
        """
        Args:
            backend: OpenSandboxBackend 实例，负责文件上传。
            skills_namespace: StoreBackend 中技能的命名空间元组。
        """
        super().__init__()
        self.backend = backend
        self.namespace = skills_namespace
        # 内容哈希缓存：sandbox_path -> md5（与 backend 同生命周期）。
        # 持久化技能在多数轮次里没有变化，命中缓存即可跳过重复上传，
        # 避免每个 Agent 周期都把全部技能文件重新灌一遍沙箱。
        self._uploaded_hashes: Dict[str, str] = {}

    async def abefore_agent(
        self, state: Dict[str, Any], runtime: Any
    ) -> Optional[Dict[str, Any]]:
        """运行前：从 StoreBackend 读取持久化技能，上传到沙箱。"""
        store = runtime.store
        files = await self._collect_skills(store)
        if files:
            await self.backend.aupload_files(files)
            # 只有上传成功后才更新缓存；失败时保持旧值，下一轮会重试
            for path, content in files:
                self._uploaded_hashes[path] = hashlib.md5(content).hexdigest()
        return None

    def before_agent(
        self, state: Dict[str, Any], runtime: Any
    ) -> Optional[Dict[str, Any]]:
        """同步版本：不执行操作（技能恢复仅支持异步）。"""
        return None

    # --------------------- 内部方法 ---------------------
    async def _collect_skills(self, store) -> List[Tuple[str, bytes]]:
        """
        从 StoreBackend 收集所有持久化技能文件。

        StoreBackend key 格式: /{scope}/{skill_name}/...
        沙箱目标路径: /skills/{scope}/{skill_name}/...

        Returns:
            (沙箱路径, 文件内容字节) 的列表。
        """
        files: List[Tuple[str, bytes]] = []

        try:
            items = await store.asearch(self.namespace)
        except Exception:
            return files

        for item in items:
            key = str(item.key).lstrip("/")

            # key 格式: {scope}/{skill_name}/...
            # 映射到: /skills/{scope}/{skill_name}/...
            parts = key.split("/", 1)
            if len(parts) != 2:
                continue
            scope, rest = parts
            sandbox_path = f"/skills/{scope}/{rest}"

            content = item.value
            if isinstance(content, dict):
                content = content.get("content", "")
            # Store 里 content 字段是"行列表"（写入端统一存 [整篇文本] 或 [行1, 行2, ...]）。
            # 直接 encode(list) 会把 Python 列表的 repr（带引号、逗号、\n 转义）
            # 写进沙箱，技能文件就彻底坏了。这里兼容 str 与 list 两种形态。
            if isinstance(content, (list, tuple)):
                content = "\n".join(str(x) for x in content)
            if isinstance(content, str):
                content = content.encode("utf-8")
            if not content:
                continue

            # 内容没变就不用再传（md5 命中即跳过）
            if self._uploaded_hashes.get(sandbox_path) == hashlib.md5(content).hexdigest():
                continue

            files.append((sandbox_path, content))

        return files
