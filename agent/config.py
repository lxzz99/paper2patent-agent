# -*- coding: utf-8 -*-
"""配置文件：模型连接信息 + 全部可调参数，集中一处。

连接三要素（API key / 模型名 / 接口地址）只从 ./.env 读取：
  - .env 已被 .gitignore 排除，绝不提交 git；
  - 格式每行一条 KEY=VALUE，# 开头为注释：
      ARK_API_KEY=ark-xxxx
      ARK_MODEL=glm-xxx
      ARK_BASE_URL=https://xxx
"""

import os
from pathlib import Path


def _load_env(path: Path) -> None:
    """把 .env 里的 KEY=VALUE 逐行写入 os.environ（连接信息的唯一来源，
    覆盖同名环境变量，避免"改了 .env 不生效"的排查陷阱）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue  # 跳过空行、注释行、格式不完整的行
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip()


# import 时立刻加载，下面的常量读到的就是 .env 的最终值
_load_env(Path(__file__).resolve().parent / ".env")

# ── 连接三要素（来自 .env）────────────────────────────────────────────
ARK_BASE_URL = os.environ.get(
    "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
)
ARK_API_KEY = os.environ.get("ARK_API_KEY", "")
ARK_MODEL = os.environ.get("ARK_MODEL", "")  # 模型名或接入点 ID（ep-xxx）

# ── 上下文与输出预算 ──────────────────────────────────────────────────
# L1 压缩层：论文超过此字符数才压缩
PAPER_CHAR_BUDGET = int(os.environ.get("PAPER_CHAR_BUDGET", "10000"))
# 首次请求的 max_tokens；0 = 不设（⚠ 强制思考模型必须设上限，否则无限思考）
INITIAL_MAX_OUTPUT_TOKENS = int(os.environ.get("INITIAL_MAX_OUTPUT_TOKENS", "16000"))
# 分节模式每节的输出预算（截断由输出侧续写保险兜底，不怕给小）
SECTION_MAX_OUTPUT_TOKENS = int(os.environ.get("SECTION_MAX_OUTPUT_TOKENS", "8000"))
# 截断续写时 max_tokens 升到的上限（思考 token 计入输出预算，所以要大）
ESCALATED_MAX_OUTPUT_TOKENS = int(os.environ.get("ESCALATED_MAX_OUTPUT_TOKENS", "32000"))
# 输出侧保险：截断后最多续写几次
MAX_OUTPUT_RECOVERY_RETRIES = 3
# 输入侧保险：上下文被拒后最多"收紧预算→重试"几次
MAX_CONTEXT_RECOVERY_RETRIES = 3

# ── 工具循环 ──────────────────────────────────────────────────────────
# 环境变量"ENABLE_TOOLS"使能，是否把工具 schema 发给模型自选（设 0 退回纯提示词模式）
ENABLE_TOOLS = os.environ.get("ENABLE_TOOLS", "1") not in ("0", "false", "False")
# 单轮最多执行几批工具调用，防死循环
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "8"))

# ── 重试与限速 ────────────────────────────────────────────────────────
# 网络抖动（超时/断连）重试次数与间隔
ARK_MAX_RETRIES = int(os.environ.get("ARK_MAX_RETRIES", "1"))
ARK_RETRY_WAIT_SECONDS = int(os.environ.get("ARK_RETRY_WAIT_SECONDS", "2"))
# 可恢复 429（请求过快）的独立重试预算与基础等待（10s/20s/30s 递增）；
# 额度类 429（SetLimitExceeded）不重试
ARK_429_MAX_RETRIES = int(os.environ.get("ARK_429_MAX_RETRIES", "3"))
ARK_429_RETRY_WAIT_SECONDS = int(os.environ.get("ARK_429_RETRY_WAIT_SECONDS", "10"))
# 分节轮次间的主动停顿秒数，防连发触发突发保护；0 = 关闭
ARK_TURN_INTERVAL_SECONDS = int(os.environ.get("ARK_TURN_INTERVAL_SECONDS", "5"))

# 思考模式开关：设 1 才向 API 发 thinking=disabled（仅支持关闭思考的模型可用）
DISABLE_THINKING = os.environ.get("DISABLE_THINKING", "0") not in ("0", "false", "False")
