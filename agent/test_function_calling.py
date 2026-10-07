# -*- coding: utf-8 -*-
"""任务2 function calling 工具循环的单元测试（不需要 API key）。

运行（在 agent/ 目录下）：python test_function_calling.py

五个用例：
  1. 完整工具循环：模型调 read_file → harness 执行 → 结果回传 → 模型出正文
  2. write_file 安全边界：目录穿越 / 绝对路径 / 盘符全被拒绝
  3. execute_tool 容错：未知工具、缺参数、读不存在的文件 → 返回错误文本不抛异常
  4. 轮数上限：模型连续要工具超过 MAX_TOOL_ROUNDS → 强制收尾
  5. 开关：ENABLE_TOOLS=0 时不把工具 schema 发给模型
"""

import json

import config
from loop import AgentLoop
from messages import MessageStore
from model_client import MockClient, ToolCall
from tools import TOOL_SCHEMAS, execute_tool, write_tool_file

PASSED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    assert cond, f"{name} 失败 {detail}"
    PASSED.append(name)
    print(f"  OK {name}")


class RecordingMock(MockClient):
    """在 MockClient 基础上记录每次 chat 收到的 messages，供断言检查。"""

    def chat(self, messages, max_tokens=None, tools=None):
        self.last_messages = messages
        self.last_tools = tools
        return super().chat(messages, max_tokens=max_tokens, tools=tools)


print("用例1 完整工具循环（read_file）")
demo = "../test/demo_paper.md"
client = RecordingMock(script=[
    {"tool": "read_file", "args": {"path": demo}},   # 第1次：模型要读文件
    {"text": "已读完文件，摘要如下……"},                # 第2次：拿到结果后出正文
])
agent = AgentLoop(client)
final = agent.run_turn(f"请阅读 {demo} 并总结")
# ① 第2次调用时，模型收到的消息里必须有 role=tool 的结果
tool_msgs = [m for m in client.last_messages if m["role"] == "tool"]
check("结果以 role=tool 回传", len(tool_msgs) == 1)
check("结果内容是文件全文", "演示论文" in tool_msgs[0]["content"]
      and len(tool_msgs[0]["content"]) > 200)
check("tool_call_id 对应", tool_msgs[0]["tool_call_id"] == "call_1")
check("调用前有 assistant.tool_calls 消息",
      any(m["role"] == "assistant" and "tool_calls" in m
          for m in client.last_messages))
# ② 消息库里照实记了工具往返（下一轮上下文可见，模型不会重复调）
store_msgs = agent.store._messages
check("消息库记录工具往返",
      sum(1 for m in store_msgs if m["role"] == "tool") == 1
      and sum(1 for m in store_msgs if m["role"] == "assistant" and "tool_calls" in m) == 1)
check("最终回复正确", final == "已读完文件，摘要如下……")

print("用例2 write_file 安全边界")
check("拒绝目录穿越", "拒绝" in execute_tool("write_file",
      {"content": "x", "filename": "../evil.txt"}))
check("拒绝绝对路径", "拒绝" in execute_tool("write_file",
      {"content": "x", "filename": "D:/evil.txt"}))
check("拒绝盘符相对路径", "拒绝" in execute_tool("write_file",
      {"content": "x", "filename": "C:evil.txt"}))
out = write_tool_file("安全写入测试", "test_safe.md")
check("正常写入成功", out.exists() and out.read_text(encoding="utf-8") == "安全写入测试")
out.unlink()

print("用例3 execute_tool 容错（错误回传文本，不抛异常）")
r = execute_tool("no_such_tool", {})
check("未知工具 → 错误文本", "未知工具" in r)
r = execute_tool("read_file", {})
check("缺参数 → 错误文本", "缺少必填参数" in r)
r = execute_tool("read_file", {"path": "不存在的文件.txt"})
check("文件不存在 → 错误文本", "工具执行失败" in r and "不存在" in r)

r = execute_tool("write_file", {"_raw": "{\"content\": \"坏JSON"})
check("坏JSON参数 → 给出修正指引", "不是合法 JSON" in r and "不要用相同参数重试" in r)

print("用例4 轮数上限（MAX_TOOL_ROUNDS=2）")
saved_rounds = config.MAX_TOOL_ROUNDS
config.MAX_TOOL_ROUNDS = 2
client = RecordingMock(script=[{"tool": "read_file", "args": {"path": demo}}] * 5)
agent = AgentLoop(client)
final = agent.run_turn("请一直读文件")
config.MAX_TOOL_ROUNDS = saved_rounds
check("执行了 2 轮后强制收尾", client.call_count == 3  # 首次 + 2 轮工具后各重调 1 次
      and "工具调用轮数上限" in final)

print("用例5 ENABLE_TOOLS=0 开关")
saved = config.ENABLE_TOOLS
config.ENABLE_TOOLS = False
client = RecordingMock()
AgentLoop(client).run_turn("你好")
check("不发送工具 schema", client.last_tools is None)
config.ENABLE_TOOLS = saved
client = RecordingMock()
AgentLoop(client).run_turn("你好")
check("默认发送工具 schema", client.last_tools == TOOL_SCHEMAS)

print(f"\nfunction calling 单测 {len(PASSED)}/17 全部通过")
