# -*- coding: utf-8 -*-
"""消息库：agent 的"原始对话记录"，整个项目唯一的事实来源（source of truth）。

对应洋葱链蓝图里的"消息库"角色：
  - 所有内容（系统提示、论文、模型回复）都追加在这里；
  - 后续的 L1压缩/L2注入/L3投影 三层 Preparer，
都只处理这里【发出去的副本】，绝不写回本库。

这是三条铁律里的第 1 条（铁律①：Preparer 只改发送副本），
原始论文一旦丢失，压缩就变成了信息损毁，所以本类 MessageStore 不提供"修改已有消息"的方法,只提供两个能力：
    - 追加消息；
    - 导出副本。
"""

import copy


class MessageStore:
    """保存完整对话历史，只进不改。"""

    def __init__(self) -> None:
        # 每条消息是 {"role": "system"/"user"/"assistant", "content": "..."}
        # 这个格式是 OpenAI 兼容接口的标准格式，方舟 GLM 也用同一格式。
        self._messages: list[dict] = []

    # 只追加，没有修改产出信息的方法
    def add(self, role: str, content: str, **extra) -> None:
        """追加一条消息。

        extra 用于 function calling 的消息字段：
          assistant 发起工具调用 → add("assistant", 正文或空串,
                                        tool_calls=[{id, type, function}])
          工具执行结果回传   → add("tool", 结果文本, tool_call_id=调用id)
        """
        msg = {"role": role, "content": content}
        msg.update(extra)    # 把extra 加到dict 里
        self._messages.append(msg)

    # 唯一出口
    def send_copy(self) -> list[dict]:
        """导出一份深拷贝。
        
        为什么是深拷贝？
            后续处理链会在这个副本上做有损操作，上一轮改坏了，下一轮可以从库里重新 copy 一份完整的重来

        执行顺序：先 send_copy() → Preparer 链处理副本 → 才发给模型。
        """
        '''
        copy.copy(x)        # —— 浅拷贝
        copy.deepcopy(x)    # —— 深拷贝
        '''
        return copy.deepcopy(self._messages)

    # ── 两个只读的观察方法，供日志/调试/界面展示用 ────────────────────

    def __len__(self) -> int:
        """消息条数，比如 len(store) == 3 表示 system+user+assistant 各一条。"""
        return len(self._messages)

    def total_chars(self) -> int:
        """消息库总字符数。
        
        粗略衡量"原始上下文有多大"，
        后面 L1 压缩层的"字符预算"判断就会用它做对比。"""
        return sum(len(m["content"]) for m in self._messages)
