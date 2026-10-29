"""RAG 向量检索纯逻辑测试（不依赖网络与 API Key）。

运行：python test_rag_logic.py

测试策略说明：
    embedding 调用走网络（智谱 API），不适合放在无 Key 环境跑的单元测试里；
    因此本文件只测"向量已就绪之后"的纯计算逻辑：检索排序、top_k 边界、
    维度校验、语料结构。真实 embedding 的端到端验证放在评测脚本
    （eval/run_eval.py，检索质量评测）里进行。
"""
import sys
import threading
import pathlib
import shutil

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from rag.knowledge_base import _VectorStore  # noqa: E402
from rag.knowledge_docs import DEFAULT_KNOWLEDGE_DOCS  # noqa: E402

# 测试产物目录（项目内，测试结束手动清理）
_TMP = pathlib.Path(__file__).parent / ".tmp"


def fresh_dir() -> pathlib.Path:
    """创建一个一次性测试目录（递增命名避免冲突）。"""
    _TMP.mkdir(parents=True, exist_ok=True)
    for i in range(100):
        d = _TMP / f"case_{i}"
        if not d.exists():
            d.mkdir()
            return d
    raise RuntimeError("测试目录用尽")


def make_store(tmpdir: pathlib.Path) -> _VectorStore:
    """构造一个内存态向量库（绕过文件加载，直接注入测试数据）。

    用 __new__ 跳过 __init__ 的文件加载，因为这里测的是检索算法本身，
    与持久化无关（持久化由 test_search_add_and_persist 覆盖）。
    """
    store = _VectorStore.__new__(_VectorStore)
    store.path = tmpdir / "kb.jsonl"
    store._lock = threading.Lock()
    store._texts = ["采购订单状态流转", "火花塞选型要点", "供应商QSDC评估"]
    store._metas = [{"source": "采购制度"}, {"source": "零部件知识"}, {"source": "供应商管理"}]
    # 三条互相正交的基向量：保证相似度只由方向决定
    store._vectors = np.asarray(
        [[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32
    )
    return store


def cleanup(d: pathlib.Path) -> None:
    """删除测试目录（失败不中断测试）。"""
    try:
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def test_search_ranking():
    """查询向量接近第 1 条 → top1 应为第 1 条且 score 接近 1。"""
    d = fresh_dir()
    try:
        store = make_store(d)
        r = store.search([1, 0.05, 0], top_k=3)
        assert r[0]["text"] == "采购订单状态流转", f"排序错误: {r[0]}"
        assert r[0]["score"] > 0.99, f"分数异常: {r[0]['score']}"
        assert r[0]["meta"]["source"] == "采购制度"
        print("PASS: 检索排序与元数据回传正确")
    finally:
        cleanup(d)


def test_search_top_k_limit():
    """top_k 大于库存条数时不应越界。"""
    d = fresh_dir()
    try:
        store = make_store(d)
        r = store.search([1, 1, 1], top_k=99)
        assert len(r) == 3
        print("PASS: top_k 越界保护正确")
    finally:
        cleanup(d)


def test_search_dim_mismatch():
    """查询维度不一致时必须显式报错（而不是悄悄返回错误结果）。"""
    d = fresh_dir()
    try:
        store = make_store(d)
        try:
            store.search([1, 2], top_k=1)
            raise AssertionError("应抛出 ValueError")
        except ValueError:
            print("PASS: 维度校验生效")
    finally:
        cleanup(d)


def test_search_add_and_persist():
    """add() 写文件后重建的 store 能读到相同数据（验证持久化往返）。"""
    d = fresh_dir()
    try:
        store = _VectorStore(d / "kb.jsonl")
        store.add(
            texts=["知识A", "知识B"],
            vectors=[[1.0, 0.0], [0.0, 1.0]],
            metas=[{"source": "测试"}, {"source": "测试"}],
        )
        assert store.count() == 2
        # 模拟进程重启：从同一个文件重新加载
        store2 = _VectorStore(d / "kb.jsonl")
        assert store2.count() == 2
        r = store2.search([1.0, 0.0], top_k=1)
        assert r[0]["text"] == "知识A"
        print("PASS: 追加写入与持久化往返正确")
    finally:
        cleanup(d)


def test_docs_shape():
    """默认语料每条必须含 content 与 source 字段。"""
    for doc in DEFAULT_KNOWLEDGE_DOCS:
        assert "content" in doc and doc["content"].strip(), f"缺 content: {doc}"
        assert "source" in doc, f"缺 source: {doc}"
    print(f"PASS: 默认语料结构合法（{len(DEFAULT_KNOWLEDGE_DOCS)} 条）")


if __name__ == "__main__":
    test_search_ranking()
    test_search_top_k_limit()
    test_search_dim_mismatch()
    test_search_add_and_persist()
    test_docs_shape()
    # 清理空的 .tmp 目录（若已无内容）
    shutil.rmtree(_TMP, ignore_errors=True)
    print("\n=== 全部 RAG 逻辑测试通过 ===")
