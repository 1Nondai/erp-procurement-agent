"""
环境变量统一读取模块（所有密钥/连接串的唯一入口）。

设计目标：
    1. 单一职责：整个项目只在这一处调用 os.getenv 读取敏感配置，
       避免 API Key、数据库密码散落在多个文件里（此前 .env 的 key 被硬编码
       在两份配置中、PG 密码出现在 config.py / web_config.py 两处，容易漂移）。
    2. 环境优先级：进程环境变量 > 项目根 .env 文件（load_dotenv(override=False)
       表示已存在的环境变量不覆盖，方便 CI/容器注入）。
    3. 失败要"看得见"：缺少关键 Key 时不静默返回 None，而是记录 WARNING，
       让调用方在使用时能从日志快速定位问题（fail-loud，但不阻断 import）。

为什么用 dotenv 而不是手工解析：
    python-dotenv 是事实标准库，支持注释、引号、多行值，
    并能把 .env 内容注入 os.environ，供子进程（沙箱脚本）继承。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录 = src/agent/env_utils.py 往上三级（src/agent -> src -> 项目根）
# 这样无论从哪个工作目录启动（uvicorn / pytest / 脚本），都能找到同一个 .env。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 加载项目根目录的 .env（存在才加载，不存在不报错）；
# override=False：真实环境变量优先于 .env 文件——容器/CI 注入的密钥不被本地文件覆盖。
load_dotenv(_PROJECT_ROOT / ".env", override=False)

# ============================================================
# LLM API Keys（模型服务密钥）
# ============================================================
# DeepSeek：主 Agent / 摘要模型 / RAG 问答生成均使用它
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.cn/v1')

# 智谱：web_search 工具与 RAG embedding 模型使用它
ZHIPU_API_KEY = os.getenv('ZHIPU_API_KEY')
ZHIPU_BASE_URL = os.getenv('ZHIPU_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4')

# 以下为可选的备用模型服务（当前主链路未使用，保留配置位）
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL')
MINIMAX_API_KEY = os.getenv('MINIMAX_API_KEY')
MINIMAX_BASE_URL = os.getenv('MINIMAX_BASE_URL')
ALIBABA_API_KEY = os.getenv('ALIBABA_API_KEY')
ALIBABA_BASE_URL = os.getenv('ALIBABA_BASE_URL')
K2_API_KEY = os.getenv('K2_API_KEY')
K2_BASE_URL = os.getenv('K2_BASE_URL')
LOCAL_BASE_URL = os.getenv('LOCAL_BASE_URL')

# ============================================================
# 基础设施连接串
# ============================================================
# PostgreSQL：LangGraph checkpoint（对话状态）+ 展示消息表 + RAG 元数据
# 默认值仅供本地开发；生产环境务必通过环境变量注入（不要把密码写进代码）。
POSTGRES_URI = os.getenv(
    'POSTGRES_URI',
    'postgresql://postgres:***@localhost:5432/langgraph_db',
)

# RAG 知识库持久化目录（Chroma 向量库落盘位置）
RAG_CHROMA_DIR = os.getenv('RAG_CHROMA_DIR', str(_PROJECT_ROOT / 'src' / 'rag' / 'chroma_store'))
# RAG 向量维度：智谱 embedding-3 默认输出 2560 维
RAG_EMBEDDING_MODEL = os.getenv('RAG_EMBEDDING_MODEL', 'embedding-3')
# 检索返回的 top-k 条数
RAG_TOP_K = int(os.getenv('RAG_TOP_K', '5'))

# ============================================================
# 可观测性：Langfuse 链路追踪
# ============================================================
# 说明：agent/observability/langfuse_tracing.py 优先从本模块取这些值，
# 取不到才回落到 os.getenv —— 这样既遵守「密钥只在 env_utils 读」的约定，
# 又保证该模块能脱离项目单独导入（脚本 / 测试 / 其他服务复用）。
#
# 留空时的行为：只要 PUBLIC_KEY 与 SECRET_KEY 都有值就自动开启追踪；
# 想强制关闭（如本地调试不想上报）把 LANGFUSE_ENABLED 设为 'false'。
LANGFUSE_ENABLED = os.getenv('LANGFUSE_ENABLED')
LANGFUSE_PUBLIC_KEY = os.getenv('LANGFUSE_PUBLIC_KEY')
LANGFUSE_SECRET_KEY = os.getenv('LANGFUSE_SECRET_KEY')
# 服务地址：云版按区域选择；自托管填内网地址
LANGFUSE_BASE_URL = os.getenv('LANGFUSE_BASE_URL', 'https://cloud.langfuse.com')
# 采样率 0~1（生产流量大时用 0.1 之类，只上报十分之一）
LANGFUSE_SAMPLE_RATE = os.getenv('LANGFUSE_SAMPLE_RATE')
# 环境 / 版本标签，用于在控制台区分 dev 与 prod 数据
LANGFUSE_ENVIRONMENT = os.getenv('LANGFUSE_ENVIRONMENT')
LANGFUSE_RELEASE = os.getenv('LANGFUSE_RELEASE')
# 批量上报：攒够 N 条或每 M 秒推一次
LANGFUSE_FLUSH_AT = os.getenv('LANGFUSE_FLUSH_AT')
LANGFUSE_FLUSH_INTERVAL = os.getenv('LANGFUSE_FLUSH_INTERVAL')
LANGFUSE_TIMEOUT = os.getenv('LANGFUSE_TIMEOUT')
# 敏感信息脱敏开关（默认开）
LANGFUSE_MASK_ENABLED = os.getenv('LANGFUSE_MASK_ENABLED')
# 启动时校验密钥（会发一次 HTTP 请求，默认关）
LANGFUSE_AUTH_CHECK = os.getenv('LANGFUSE_AUTH_CHECK')
LANGFUSE_DEBUG = os.getenv('LANGFUSE_DEBUG')

# ============================================================
# 缺失关键配置的告警（import 时提示一次，运行期不再重复刷屏）
# ============================================================
# 只在真正影响功能的 Key 缺失时提醒；LOCAL_BASE_URL 这类可选项不在此列。
_MISSING_HINTS = {
    'DEEPSEEK_API_KEY': '主模型/摘要模型/RAG 问答将不可用',
    'ZHIPU_API_KEY': 'web_search 与 RAG embedding 将不可用',
}
_missing = [f'{k}（{v}）' for k, v in _MISSING_HINTS.items() if not os.getenv(k)]
if _missing:
    # 用 print 而非 logging：本模块在 logging.basicConfig 之前被 import，
    # 此时 logging 尚未配置 handler，print 到 stdout 是唯一可靠途径。
    print(f"[env_utils][WARNING] 以下环境变量未配置: {'; '.join(_missing)}")
