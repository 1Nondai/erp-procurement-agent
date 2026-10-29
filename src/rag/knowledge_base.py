"""
RAG 知识库模块（检索增强生成）。

职责：
    1. Embedding：调用智谱 embedding-3 把文本转成向量；
    2. 向量存储：本地 JSON 文件持久化（文本 + 向量 + 元数据）；
    3. 检索：余弦相似度 top-k，返回最相关的知识片段。

为什么不用 Chroma/Milvus（当前阶段）：
    - 评测和演示场景的知识库规模在百条量级，numpy 暴力扫描的延迟
      不到 1 毫秒，引入向量数据库纯属杀鸡用牛刀；
    - JSON 落盘的存储格式肉眼可读、可手工编辑、可进 Git，
      对于"理解 RAG 工作原理"这个目标更友好；
    - 接口按"向量库"抽象（build/add/search 三方法），
      将来要换 Chroma 只需替换 _VectorStore 实现，上层代码零改动。

使用方式：
    from rag.knowledge_base import KnowledgeBase
    kb = KnowledgeBase()               # 加载/创建知识库
    await kb.ensure_built()            # 首次构建默认知识语料
    results = await kb.search("理化板台面")  # 向量检索
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from agent.env_utils import (
    RAG_CHROMA_DIR,
    RAG_EMBEDDING_MODEL,
    RAG_TOP_K,
    ZHIPU_API_KEY,
)

logger = logging.getLogger(__name__)

# 知识库持久化文件（JSON Lines 格式：一行一条 {"text", "vector", "meta"}）
_STORE_FILE = Path(RAG_CHROMA_DIR) / "knowledge.jsonl"


# ============================================================
# Embedding 客户端（智谱 embedding-3，OpenAI 兼容协议）
# ============================================================

class ZhipuEmbedding:
    """智谱 embedding API 的轻量封装。

    为什么单独封装而不是用 langchain 的 Embeddings 抽象：
        本项目调用智谱的方式统一走 zai-sdk / OpenAI 兼容 HTTP，
        直接封装一个 embed(texts) 方法可以让依赖关系最小化，
        也方便在评测脚本里独立测试嵌入质量。

    维度说明：
        embedding-3 默认输出 2560 维；同一次构建必须使用同一个模型，
        否则向量维度不一致会导致检索时形状错误（代码里有显式校验）。
    """

    def __init__(self, api_key: str, model: str = RAG_EMBEDDING_MODEL) -> None:
        if not api_key:
            raise ValueError("ZHIPU_API_KEY 未配置，RAG embedding 不可用")
        self.api_key = api_key
        self.model = model
        # 惰性导入：只有真正要嵌入时才拉起 SDK（避免 import 本模块就报错）
        from zai import ZhipuAiClient
        self._client = ZhipuAiClient(api_key=api_key)

    def embed(self, texts: List[str]) -> List[List[float]]:
        """把一批文本转成向量（同步调用，批量上限以 API 文档为准）。

        Args:
            texts: 文本列表（每条建议 < 500 字，超长会被 API 截断）。

        Returns:
            与输入等长的向量列表，每个向量是 float 列表。
        """
        if not texts:
            return []
        resp = self._client.embeddings.create(
            model=self.model,
            input=texts,
        )
        # 按 index 排序，防御 API 乱序返回
        items = sorted(resp.data, key=lambda d: d.index)
        return [list(item.embedding) for item in items]


# ============================================================
# 向量存储（JSONL 落盘 + numpy 余弦检索）
# ============================================================

class _VectorStore:
    """本地 JSONL 向量存储。

    数据形态（一行一条记录）：
        {"text": "知识片段原文", "vector": [...], "meta": {"source": "..."}}

    读写策略：
        - 全量加载进内存（列表 + numpy 矩阵）；
        - 检索 = 归一化后矩阵点积（数学上等价于余弦相似度）；
        - 追加写入用文件锁 + 重建内存索引，保证多线程/多进程追加的一致性。

    规模边界：
        万条以内完全够用（一次全量 scan < 10ms）；
        超过万条请换成 Chroma / pgvector。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._texts: List[str] = []
        self._vectors: Optional[np.ndarray] = None  # shape: (n, dim)
        self._metas: List[Dict[str, Any]] = []
        # 预先归一化好的向量矩阵（shape 同 _vectors）。search() 只用它做点积，
        # 避免每次检索都对全库重算范数——这是随库规模线性增长的固定开销。
        self._norm_vectors: Optional[np.ndarray] = None
        self._load()

    # ------------------------- 持久化 -------------------------

    def _load(self) -> None:
        """从 JSONL 文件加载全部记录到内存。文件不存在则视为空库。"""
        self._texts, self._metas, vecs = [], [], []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self._texts.append(rec["text"])
                    vecs.append(rec["vector"])
                    self._metas.append(rec.get("meta", {}))
                except (json.JSONDecodeError, KeyError):
                    # 单条脏数据跳过，不能让整库加载失败
                    logger.warning("RAG 知识库存在损坏行，已跳过")
        self._vectors = (
            np.asarray(vecs, dtype=np.float32) if vecs else None
        )
        # 加载即预算归一化矩阵（与 search 里原来的表达式完全一致，
        # 保证数值等价；1e-10 epsilon 防零向量除零）
        self._norm_vectors = self._build_norm_vectors(self._vectors)

    @staticmethod
    def _build_norm_vectors(vectors: Optional[np.ndarray]) -> Optional[np.ndarray]:
        """按行做 L2 归一化，返回与输入同形状的新矩阵（None 直接透传）。"""
        if vectors is None or vectors.size == 0:
            return None
        return vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-10)

    def _append_file(self, records: List[Dict[str, Any]]) -> None:
        """把新记录追加写入 JSONL 文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ------------------------- 增 / 查 -------------------------

    def add(self, texts: List[str], vectors: List[List[float]], metas: Optional[List[Dict]] = None) -> None:
        """追加一批知识片段（写文件 + 重建内存索引）。

        Args:
            texts: 片段原文列表。
            vectors: 与 texts 等长的向量列表。
            metas: 可选元数据列表（如 {"source": "采购手册"}）。
        """
        if len(texts) != len(vectors):
            raise ValueError("texts 与 vectors 长度不一致")
        metas = metas or [{} for _ in texts]
        records = [
            {"text": t, "vector": v, "meta": m}
            for t, v, m in zip(texts, vectors, metas)
        ]
        with self._lock:
            self._append_file(records)
            # 追加后重建内存结构（全量重读最简单且不会出错）
            self._load()

    def search(self, query_vector: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
        """余弦相似度检索 top-k。

        数学说明：
            余弦相似度 = A·B / (|A||B|)。把所有向量预先归一化后，
            分母恒为 1，点积即余弦相似度——这就是下面 normalize 的目的。

        Args:
            query_vector: 查询向量（未归一化也可以，内部会处理）。
            top_k: 返回条数上限。

        Returns:
            [{"text", "meta", "score"}] 按 score 降序。
        """
        if self._norm_vectors is None or not self._texts:
            return []
        q = np.asarray(query_vector, dtype=np.float32)
        if q.shape != self._vectors.shape[1:]:
            raise ValueError(
                f"查询向量维度 {q.shape} 与知识库维度 {self._vectors.shape[1:]} 不一致"
            )

        # 库向量已在 _load()/add() 时归一化完毕（缓存），这里只归一化查询向量
        # （加 epsilon 防零向量除零）。点积 = 余弦相似度，数值与每次重算全库范数等价。
        norm_q = q / (np.linalg.norm(q) + 1e-10)

        scores = self._norm_vectors @ norm_q  # 点积 = 余弦相似度
        top_k = min(top_k, len(scores))
        # argpartition 比 full sort 快：只找前 k 大的索引
        best = np.argpartition(-scores, top_k - 1)[:top_k]
        best = best[np.argsort(-scores[best])]
        return [
            {"text": self._texts[i], "meta": self._metas[i], "score": float(scores[i])}
            for i in best
        ]

    def count(self) -> int:
        """返回库中片段总数。"""
        return len(self._texts)

    def clear(self) -> None:
        """清空知识库（删除存储文件并重置内存索引）。"""
        with self._lock:
            if self.path.exists():
                self.path.unlink()
            self._texts, self._metas, self._vectors = [], [], None
            self._norm_vectors = None


# ============================================================
# KnowledgeBase：面向上层的外观类（构建 / 检索一站式）
# ============================================================

class KnowledgeBase:
    """RAG 知识库外观类：组合 embedding 与向量存储。

    生命周期：
        kb = KnowledgeBase()
        await kb.ensure_built()     # 幂等：空库时用默认语料构建一次
        results = await kb.search("台面选型")

    线程模型：
        embed() 走网络 IO（在 asyncio 的 executor 里跑同步 SDK），
        search() 是纯内存计算（微秒级），因此只有嵌入需要 await。
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store = _VectorStore(store_path or _STORE_FILE)
        self._embedding: Optional[ZhipuEmbedding] = None

    def _get_embedding(self) -> ZhipuEmbedding:
        """惰性创建 embedding 客户端（缺 Key 时给出清晰报错）。"""
        if self._embedding is None:
            self._embedding = ZhipuEmbedding(ZHIPU_API_KEY or "", RAG_EMBEDDING_MODEL)
        return self._embedding

    async def ensure_built(self) -> None:
        """确保知识库已构建：空库时用默认语料构建一次（幂等）。

        默认语料来自 rag.knowledge_docs.DEFAULT_KNOWLEDGE_DOCS，
        后续可用 add_documents() 追加业务文档（产品手册、采购制度等）。
        """
        if self.store.count() > 0:
            return
        from rag.knowledge_docs import DEFAULT_KNOWLEDGE_DOCS
        await self.add_documents(DEFAULT_KNOWLEDGE_DOCS)
        logger.info("RAG 知识库已完成初始构建: %s 条", self.store.count())

    async def add_documents(self, docs: List[Dict[str, str]]) -> None:
        """把文档列表嵌入并写入向量库。

        Args:
            docs: [{"content": "片段文本", "source": "来源标识"}]
                  （source 是元数据，检索结果会原样带回，方便溯源）
        """
        if not docs:
            return
        texts = [d["content"] for d in docs]
        metas = [{"source": d.get("source", "未标注")} for d in docs]

        # 同步 SDK 调用放进默认线程池，避免阻塞事件循环
        # （asyncio.get_event_loop() 在 3.12+ 已弃用且无运行 loop 时会报错，
        #   to_thread 内部使用 get_running_loop，语义更明确）
        import asyncio
        vectors = await asyncio.to_thread(self._get_embedding().embed, texts)
        self.store.add(texts, vectors, metas)

    async def search(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """检索与查询最相关的知识片段。

        Args:
            query: 自然语言查询（如"理化板和环氧板怎么选"）。
            top_k: 返回条数，默认取 env RAG_TOP_K。

        Returns:
            [{"text", "meta", "score"}] 按相关性降序。
        """
        import asyncio
        q_vec = await asyncio.to_thread(self._get_embedding().embed, [query])
        return self.store.search(q_vec[0], top_k or RAG_TOP_K)
