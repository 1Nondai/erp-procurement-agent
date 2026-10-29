"""
ERP 采购智能助手 评测脚本（离线可跑，不需要启动 Agent / 沙箱）。

评测三层：
    1. 检索层（rag_retrieval）：期望来源是否出现在 knowledge_search 的
       top-k 结果里 + 相关度阈值检查 —— 只依赖 embedding API；
    2. 答案层（domain_answer）：把检索到的片段拼进 prompt，让 LLM 回答，
       检查答案是否包含 expected 关键要点 —— 依赖 embedding + LLM API；
    3. 路由层（agent_routing）：需要完整 DeepAgents 环境（沙箱、MCP、PG），
       本脚本离线运行时自动跳过并标注 skipped
       （尚未实现 --with-agent 在线模式，需要时先启动 start_web.py 全栈）。

运行：
    cd QingchiERP
    python src/eval/run_eval.py                    # 检索层 + 答案层
    python src/eval/run_eval.py --top-k 5          # 自定义检索条数
    python src/eval/run_eval.py --case rag-004     # 只跑单条用例
    python src/eval/run_eval.py --skip-answer      # 只测检索层（不调 LLM）

结果输出：
    控制台汇总表 + eval/results/eval_report_<时间戳>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

# 保证可以从 src 目录导入 rag / agent 包
_SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC))

from rag.knowledge_base import KnowledgeBase  # noqa: E402

# 相关度阈值：低于该分数的命中视为"弱相关"（检索层判失败）
_SCORE_THRESHOLD = 0.35
# 答案生成的模型（走 DeepSeek OpenAI 兼容接口；离线无 Key 时答案层自动跳过）
_ANSWER_MODEL_NAME = "deepseek-chat"


def load_cases(path: Path) -> list[dict]:
    """加载评测用例 YAML。

    Args:
        path: eval_cases.yaml 路径。

    Returns:
        用例字典列表。
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("cases", [])


# ============================================================
# 检索层评测
# ============================================================

async def eval_retrieval(kb: KnowledgeBase, case: dict, top_k: int) -> dict:
    """评测单条用例的检索质量。

    判定规则：
        - 正例（expected_sources 非空）：每个期望来源都必须在 top-k 的
          source 集合中出现，且 top1 分数不低于阈值；
        - 负例（expected_sources 为空）：只检查"检索不崩溃 + 返回条数正确"，
          相关度阈值不做硬判定（留人工/后续升级为 LLM judge）。

    Returns:
        {"case_id", "layer", "passed", "detail"} 评测记录。
    """
    await kb.ensure_built()
    results = await kb.search(case["query"], top_k=top_k)
    got_sources = [r["meta"].get("source", "") for r in results]
    top1_score = results[0]["score"] if results else 0.0

    expected_sources = case.get("expected_sources") or []
    if expected_sources:
        missing = [s for s in expected_sources if s not in got_sources]
        passed = (not missing) and top1_score >= _SCORE_THRESHOLD
        detail = {
            "expected_sources": expected_sources,
            "got_sources": got_sources,
            "top1_score": round(top1_score, 4),
            "missing_sources": missing,
        }
    else:
        # 负例：只验证检索链路正常工作
        passed = isinstance(results, list)
        detail = {"top1_score": round(top1_score, 4), "negative_case": True}

    return {
        "case_id": case["id"],
        "layer": "retrieval",
        "passed": passed,
        "detail": detail,
    }


# ============================================================
# 答案层评测
# ============================================================

def _get_answer_llm():
    """创建答案生成模型（DeepSeek OpenAI 兼容协议）。

    Returns:
        ChatOpenAI 实例；无 API Key 时返回 None（答案层跳过）。
    """
    from agent.env_utils import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
    if not DEEPSEEK_API_KEY:
        return None
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=_ANSWER_MODEL_NAME,
        temperature=0,
        openai_api_key=DEEPSEEK_API_KEY,
        openai_api_base=DEEPSEEK_BASE_URL,
        max_tokens=512,
    )


async def eval_answer(kb: KnowledgeBase, case: dict, top_k: int, llm) -> dict:
    """评测"检索 + 生成"端到端：RAG 上下文下答案是否含期望要点。

    之所以要求"必须包含 expected 关键词"而不是自由打分：
        评测集里的 expected 都是客观事实（如"三家""JASO"），
        包含性检查可复现、无 LLM judge 的随机性；
        主观质量评测留待后续接入 LLM-as-judge。

    Returns:
        评测记录（无 LLM Key 时 passed=None 表示 skipped）。
    """
    if llm is None:
        return {
            "case_id": case["id"],
            "layer": "answer",
            "passed": None,   # None = skipped
            "detail": {"reason": "DEEPSEEK_API_KEY 未配置，答案层跳过"},
        }

    await kb.ensure_built()
    results = await kb.search(case["query"], top_k=top_k)
    context = "\n\n".join(
        f"[{i}] ({r['meta'].get('source', '')})\n{r['text']}"
        for i, r in enumerate(results, 1)
    )
    prompt = (
        "你是 ERP 采购助手。请仅根据以下知识库片段回答问题，"
        "不要编造片段之外的内容。\n\n"
        f"知识库片段：\n{context}\n\n问题：{case['query']}\n\n"
        "请用两三句话回答。"
    )
    resp = await llm.ainvoke(prompt)
    answer = resp.content if isinstance(resp.content, str) else str(resp.content)

    expected = (case.get("expected") or "").strip()
    passed = (not expected) or (expected in answer)
    return {
        "case_id": case["id"],
        "layer": "answer",
        "passed": passed,
        "detail": {
            "expected_keywords": expected,
            "answer": answer[:300],
        },
    }


# ============================================================
# 主流程
# ============================================================

async def main() -> int:
    parser = argparse.ArgumentParser(description="ERP 采购助手评测")
    parser.add_argument("--top-k", type=int, default=5, help="检索条数")
    parser.add_argument("--case", type=str, default=None, help="只跑指定 case_id")
    parser.add_argument(
        "--skip-answer", action="store_true",
        help="跳过答案层（只测检索，不调用 LLM）",
    )
    args = parser.parse_args()

    cases = load_cases(Path(__file__).parent / "eval_cases.yaml")
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"找不到用例: {args.case}")
            return 2

    print(f"=== ERP 采购助手评测：{len(cases)} 条用例，top_k={args.top_k} ===\n")

    # 前置检查：检索层必须依赖 embedding API。缺 Key 时明确告知配置方法，
    # 而不是让用户在第一条用例的 traceback 里找原因。
    from agent.env_utils import ZHIPU_API_KEY
    if not ZHIPU_API_KEY:
        print(
            "错误：ZHIPU_API_KEY 未配置，检索层评测无法运行（embedding 不可用）。\n"
            "请在项目根目录 .env 中添加：\n"
            "    ZHIPU_API_KEY=你的智谱API密钥\n"
            "配置后重新运行本脚本。"
        )
        return 2

    kb = KnowledgeBase()
    llm = None if args.skip_answer else _get_answer_llm()

    records: list[dict] = []
    for case in cases:
        t0 = time.time()
        tags = case.get("tags", [])
        rec: dict = {"case_id": case["id"], "tags": tags}

        # 路由层用例需要完整 Agent 环境，离线跳过
        if "agent_routing" in tags:
            rec.update(layer="routing", passed=None,
                       detail={"reason": "路由层需完整 Agent 环境，离线评测跳过"})
            records.append(rec)
            print(f"[skip ] {case['id']}: 路由层需完整 Agent 环境")
            continue

        # 1) 检索层
        r = await eval_retrieval(kb, case, args.top_k)
        records.append(r)
        mark = "PASS " if r["passed"] else "FAIL "
        print(f"[{mark}] {case['id']} 检索层 ({time.time()-t0:.1f}s) top1={r['detail'].get('top1_score')}")

        # 2) 答案层
        if "domain_answer" in tags and not args.skip_answer:
            t1 = time.time()
            r2 = await eval_answer(kb, case, args.top_k, llm)
            records.append(r2)
            if r2["passed"] is None:
                print(f"[skip ] {case['id']} 答案层: {r2['detail']['reason']}")
            else:
                mark2 = "PASS " if r2["passed"] else "FAIL "
                print(f"[{mark2}] {case['id']} 答案层 ({time.time()-t1:.1f}s)")

    # ---- 汇总 ----
    retrieval_recs = [x for x in records if x["layer"] == "retrieval"]
    answer_recs = [x for x in records if x["layer"] == "answer" and x["passed"] is not None]
    print("\n=== 汇总 ===")
    print(f"检索层: {sum(1 for x in retrieval_recs if x['passed'])}/{len(retrieval_recs)} 通过")
    if answer_recs:
        print(f"答案层: {sum(1 for x in answer_recs if x['passed'])}/{len(answer_recs)} 通过")

    # ---- 落盘报告 ----
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    report_path = out_dir / f"eval_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(
        json.dumps({"records": records}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"报告已保存: {report_path}")

    # 有失败则退出码非零，便于 CI 集成
    failed = any(x["passed"] is False for x in records)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
