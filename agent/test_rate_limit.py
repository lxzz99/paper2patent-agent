# -*- coding: utf-8 -*-
"""429 分流处理的单测：SetLimitExceeded 不重试，突发限流等待后重试。

背景（2026-09-17 真实踩坑）：分节生成连发请求触发 RequestBurstTooFast
（可恢复的突发限流），但旧代码把所有 429 当额度问题直接抛出，整轮生成
崩掉。修复后 429 分两类，本文件用假客户端逐字验证这个分流。

用法：python test_rate_limit.py
"""

import model_client
from model_client import ArkClient


class _ApiError(Exception):
    """带 status_code 的假 API 异常（模拟 openai.RateLimitError）。"""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _Message:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class _Completions:
    """按剧本抛异常或返回回复的假 completions 接口。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _Resp(step)


class _StubClient:
    """替换 ArkClient._client 的假 openai 客户端。"""

    def __init__(self, script):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(script)


def _make_client(script) -> ArkClient:
    client = ArkClient("k", "https://stub", "stub-model")
    client._client = _StubClient(script)
    return client


def test_burst_429_retries_then_succeeds():
    """RequestBurstTooFast（无 SetLimitExceeded）→ 等待后重试成功。"""
    model_client.RATE_LIMIT_WAIT_SECONDS = 0  # 测试不等 10 秒
    burst = _ApiError("Error code: 429 - {'error': {'code': 'RequestBurstTooFast', "
                      "'message': 'System protection triggered by request burst.'}}",
                      429)
    client = _make_client([burst, "恢复后的正常回复"])
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "恢复后的正常回复"
    assert client._client.chat.completions.calls == 2, "应重试一次"
    print("OK  突发限流 429：等待重试后成功恢复（共 2 次调用）")


def test_set_limit_exceeded_no_retry():
    """SetLimitExceeded（额度暂停）→ 不重试直接抛（重试纯属烧时间）。"""
    model_client.RATE_LIMIT_WAIT_SECONDS = 0
    quota = _ApiError("Error code: 429 - {'error': {'code': "
                      "'SetLimitExceeded', 'message': 'quota paused'}}", 429)
    client = _make_client([quota, "不应到达的回复"])
    try:
        client.chat([{"role": "user", "content": "hi"}])
    except _ApiError as e:
        assert "SetLimitExceeded" in str(e)
    else:
        raise AssertionError("额度类 429 应直接抛出")
    assert client._client.chat.completions.calls == 1, "不应重试"
    print("OK  额度类 429（SetLimitExceeded）：不重试直接报错（共 1 次调用）")


def test_quota_429_exhausted_retries():
    """重试次数用尽仍 429 → 原样上抛（不吞错误）。"""
    model_client.RATE_LIMIT_WAIT_SECONDS = 0
    model_client.MAX_429_RETRIES_BACKUP = model_client.MAX_429_RETRIES
    model_client.MAX_429_RETRIES = 1  # 收紧预算便于测试
    burst = _ApiError("429 RequestBurstTooFast", 429)
    client = _make_client([burst, burst])
    try:
        client.chat([{"role": "user", "content": "hi"}])
    except _ApiError:
        pass
    else:
        raise AssertionError("重试耗尽仍 429 应上抛")
    finally:
        model_client.MAX_429_RETRIES = model_client.MAX_429_RETRIES_BACKUP
    assert client._client.chat.completions.calls == 2  # 首次 + 1 次重试
    print("OK  突发限流重试耗尽：如实上抛（独立预算，共 2 次调用）")


if __name__ == "__main__":
    test_burst_429_retries_then_succeeds()
    test_set_limit_exceeded_no_retry()
    test_quota_429_exhausted_retries()
    print("\n429 分流单测全部通过")
