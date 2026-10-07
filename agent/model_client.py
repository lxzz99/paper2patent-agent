# -*- coding: utf-8 -*-
"""模型客户端：全项目唯一负责"调模型"的地方。

设计要点：真实 API（ArkClient）和演示用 mock（MockClient）实现同一个
chat() 接口。主循环只认 chat()，根本不知道背后是真模型还是假模型——
这样"无 key 演示"和"真实联调"共用一套代码，切换只在 main.py 一行。

另外把模型的 finish_reason（结束原因）原样带回来：
  - "stop"    ：模型正常写完了
  - "length"  ：模型【开始了没写完】被 max_tokens 截断
这个字段就是双保险方案②里"输出侧保险"的判别锚点（步骤3会用到），
骨架阶段先把它带出来存好，避免到时候改接口。
"""

import json
import time
from dataclasses import dataclass, field

from openai import OpenAI  # 方舟是 OpenAI 兼容接口，直接用官方 openai 包

import config

# 网络类异常的重试次数/间隔（默认 1 次 / 2 秒，config 里可改）：
# 重试是为了扛"偶发抖动"，不是为了硬扛故障——次数刻意少，快速失败。
MAX_RETRIES = config.ARK_MAX_RETRIES
RETRY_WAIT_SECONDS = config.ARK_RETRY_WAIT_SECONDS
# 429 突发限流的独立重试预算与基础等待（等待按次数递增：
# 10s/20s/30s——服务端保护窗口比网络抖动长得多，一次 10s 不够）
MAX_429_RETRIES = config.ARK_429_MAX_RETRIES
RATE_LIMIT_WAIT_SECONDS = config.ARK_429_RETRY_WAIT_SECONDS


@dataclass
class ToolCall:
    """模型发起的一次工具调用请求（function calling）。

    id        : 调用标识，回传工具结果时用 tool_call_id 对应上
    name      : 要调的工具名（如 "read_file"）
    arguments : 调用参数（API 返回的是 JSON 字符串，这里已解析成 dict；
                解析失败时保留 {"_raw": 原始串}，让上层能拿到原文）
    """
    id: str
    name: str
    arguments: dict


@dataclass
class ModelReply:
    """对模型一次回复的极简包装。

    content       : 模型输出的正文
    finish_reason : 结束原因，"stop"=写完了，"length"=被截断（没写完），
                    "tool_calls"=模型想调工具（正文为空或说明文字）
    tool_calls    : 非空表示模型请求调用工具，由 loop 执行后把结果
                    以 role="tool" 消息回传再重新询问模型
    usage         : 本次调用的 token 用量（completion_tokens /
                    reasoning_tokens），供费用诊断——思考型模型的
                    reasoning_tokens 也计入 max_tokens 输出上限
    """
    content: str
    finish_reason: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class ArkClient:
    """调用火山方舟 GLM 的真实客户端。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        # openai.OpenAI 客户端只要换 base_url 就能指向方舟，接口格式不变。
        # max_retries=0：关闭 SDK 内置的 429 自动重试（默认还会偷偷重发 2 次），
        # 重试全部由本类 chat() 的循环接管——否则每次重试背后隐藏 2 次额外
        # 请求，反而加重突发限流、拉长保护窗口。
        self._client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._model = model

    def chat(self, messages: list[dict], max_tokens: int | None = None,
             tools: list[dict] | None = None) -> ModelReply:
        """发送消息列表（必须是 send_copy() 的副本！），拿回一条回复。

        tools：function calling 的工具 schema 列表（OpenAI 格式），
        None 表示本轮不提供工具（模型只能生成文本）。

        带网络异常重试：偶发的超时/断网/限流等待后重试最多 MAX_RETRIES
        次。注意这里和双保险（步骤3）的重试是两回事——那里是"上下文太长
        被拒后压缩重试"，属于策略性恢复；这里是"网络抖动"，纯粹重发。
        """
        last_err: Exception | None = None
        attempt = 0          # 网络抖动重试计数（预算 MAX_RETRIES）
        rate_attempts = 0    # 429 突发限流重试计数（独立预算 MAX_429_RETRIES）
        while True:
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,   # 方舟接入点 ID（ep-xxx）或模型名
                    messages=messages,   # 标准格式：[{role, content}, ...]
                    **({"max_tokens": max_tokens} if max_tokens else {}),
                    **({"tools": tools} if tools else {}),
                    # 思考开关（config.DISABLE_THINKING）：思考型模型的
                    # 推理 token 计入 max_tokens，关掉它长文才写得完。
                    # 注意 thinking 不是 openai SDK 原生参数，必须放进
                    # extra_body 原样透传给方舟（放错位置会 TypeError）
                    **({"extra_body": {"thinking": {"type": "disabled"}}}
                       if config.DISABLE_THINKING else {}),
                )
                choice = resp.choices[0]  # 只发了一条消息，取第一个候选即可
                usage_info = {}
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    usage_info["completion_tokens"] = getattr(
                        usage, "completion_tokens", None)
                    details = getattr(usage, "completion_tokens_details", None)
                    reasoning = getattr(details, "reasoning_tokens", None) \
                        if details else None
                    if reasoning is not None:
                        usage_info["reasoning_tokens"] = reasoning
                    print(f"[用量] 输出 {usage_info.get('completion_tokens', '?')} tokens"
                          + (f"（其中思考 {reasoning}）" if reasoning is not None else ""))
                return ModelReply(
                    content=choice.message.content or "",
                    finish_reason=choice.finish_reason or "stop",
                    tool_calls=_parse_tool_calls(choice.message.tool_calls),
                    usage=usage_info,
                )
            except Exception as e:  # noqa: BLE001
                # 上下文超限不在这里重试：模型还没生成就被打回，重发多少次
                # 都没用（retryable=False），必须交给 loop 的输入侧保险压缩。
                if is_context_limit_error(e):
                    raise
                # 429 分两类（实测 RequestBurstTooFast 踩坑）：
                #   SetLimitExceeded = 额度暂停/用尽，重试纯属烧时间，
                #     直接给人话指引后抛出；
                #   其余（如 RequestBurstTooFast 请求过快）= 可恢复的突发
                #     限流，等服务端保护窗口过去后重试即可恢复。
                if getattr(e, "status_code", None) == 429:
                    if "SetLimitExceeded" in str(e):
                        print("[限流] HTTP 429：账号触达模型调用上限。若错误信息含 "
                              "SetLimitExceeded，请到方舟控制台的模型激活页调整或"
                              "关闭\"安全体验模式\"，或等待限额重置后再测。")
                        raise
                    rate_attempts += 1
                    if rate_attempts <= MAX_429_RETRIES:
                        wait = RATE_LIMIT_WAIT_SECONDS * rate_attempts  # 10s/20s/30s 递增
                        print(f"[限流] HTTP 429 请求过快（突发限流，可恢复），"
                              f"第 {rate_attempts}/{MAX_429_RETRIES} 次重试，"
                              f"等待 {wait}s……")
                        time.sleep(wait)
                        continue
                    raise  # 突发限流重试用尽，原样上抛
                last_err = e
                if attempt < MAX_RETRIES:
                    attempt += 1
                    print(f"[重试] 网络异常（{type(e).__name__}），"
                          f"{RETRY_WAIT_SECONDS}s 后重试……")
                    time.sleep(RETRY_WAIT_SECONDS)
                    continue
                raise  # 重试耗尽，把最后一次的异常抛给上层


def _parse_tool_calls(raw_tool_calls) -> list[ToolCall]:
    """把 API 返回的 tool_calls 对象列表解析成 ToolCall 列表。

    OpenAI 格式里 arguments 是 JSON 字符串；模型偶尔会生成不合法的
    JSON——此时不抛错（抛错=浪费一次已付费的请求），保留原文让
    工具层返回可读的错误信息给模型，模型下一轮自己修正参数。
    """
    parsed: list[ToolCall] = []
    for tc in (raw_tool_calls or []):
        fn = tc.function
        try:
            args = json.loads(fn.arguments) if fn.arguments else {}
        except (json.JSONDecodeError, TypeError):
            args = {"_raw": fn.arguments}
        parsed.append(ToolCall(id=tc.id, name=fn.name, arguments=args))
    return parsed


def is_context_limit_error(exc: Exception) -> bool:
    """判别"上下文超限"类错误——输入侧保险的触发条件。

    判别锚点：模型【还没开始生成】就被打回。表现为：
      - HTTP 413（Payload Too Large）；或
      - HTTP 400 + 错误文案里明确说上下文/输入太长。
    只认"明确说超限"的错误，避免把参数写错之类的 400 误判成超限。
    """
    status = getattr(exc, "status_code", None)
    if status == 413:
        return True
    if status != 400:
        return False
    text = str(exc).lower()
    keywords = ("context length", "context_length", "too long",
                "maximum context", "输入超长", "上下文长度")
    return any(k in text for k in keywords)


class MockClient:
    """假模型：不联网、不要 key，返回固定的演示回复。

    truncate_times 参数用于【稳定复现输出截断】：前 N 次 chat 返回
    finish_reason="length" 的半截回复，之后恢复 "stop"——这让双保险的
    续写恢复可以在无 key 环境下确定性验证（真模型的截断时机没法控制）。

    script 参数用于【稳定复现工具调用】（function calling 单测）：
    一个"剧本"列表，按次序取用。每一步是 dict：
      {"tool": "read_file", "args": {...}}  → 模型发起一次工具调用
      {"text": "……"}                        → 模型输出正文
    剧本演完后回落到默认行为（回显最后一条 user 输入的开头），
    这让"模型调工具 → 拿到结果 → 再生成正文"的完整循环可以
    在无 key 环境下逐字验证。
    """

    def __init__(self, finish_reason: str = "stop", truncate_times: int = 0,
                 script: list[dict] | None = None) -> None:
        self._finish_reason = finish_reason
        self._truncate_left = truncate_times
        self._script = list(script or [])
        self.call_count = 0

    def chat(self, messages: list[dict], max_tokens: int | None = None,
             tools: list[dict] | None = None) -> ModelReply:
        self.call_count += 1
        if self._truncate_left > 0:  # 模拟截断：先给半截，等续写提示
            self._truncate_left -= 1
            return ModelReply(
                content=f"【Mock 前半·第{self.call_count}次调用·被截断】",
                finish_reason="length",
            )
        if self._script:  # 按剧本演出（工具调用 / 指定正文）
            step = self._script.pop(0)
            if "tool" in step:
                return ModelReply(
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[ToolCall(id=f"call_{self.call_count}",
                                         name=step["tool"],
                                         arguments=step.get("args", {}))],
                )
            return ModelReply(content=step["text"], finish_reason="stop")
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        preview = last_user[:40].replace("\n", " ")
        return ModelReply(
            content=f"【Mock 续写/完整回复·第{self.call_count}次调用】"
                    f"最后一条输入开头为：{preview}……",
            finish_reason=self._finish_reason,
        )


def make_client(mock: bool = False):
    """客户端工厂：main.py / webapp.py 都从这里拿客户端。

    mock=True  → 直接给假模型，永不报"缺 key"。
    mock=False → 校验 key 和模型名都配置了，缺了就给出明确的设置提示。
    """
    if mock:
        return MockClient()

    if not config.ARK_API_KEY or not config.ARK_MODEL:
        raise SystemExit(
            "未配置真实模型。三种解决办法：\n"
            "  1) 加 --mock 参数，用假模型先跑通骨架（不需要 key）；\n"
            "  2) 在 agent/.env 文件里写两行（推荐，文件已被 gitignore）：\n"
            "       ARK_API_KEY=ark-xxxx\n"
            "       ARK_MODEL=模型名或ep-xxx\n"
            "  3) 设置环境变量（优先级高于 .env）：\n"
            "       PowerShell: $env:ARK_API_KEY='你的key'; $env:ARK_MODEL='ep-xxx'\n"
            "       cmd:        set ARK_API_KEY=你的key && set ARK_MODEL=ep-xxx"
        )
    return ArkClient(config.ARK_API_KEY, config.ARK_BASE_URL, config.ARK_MODEL)
