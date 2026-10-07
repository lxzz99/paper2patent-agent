# -*- coding: utf-8 -*-
"""L1 白名单压缩（2026-09-17 升级）的单测：全部不联网、不烧 token。

背景：旧版两级压缩（丢参考文献 → 全局头70%+尾30%截断）在 42204 字符
真实论文上把中间约 32581 字符的方法核心章节整段切掉，模型只看到摘要
和实验尾巴，专利只能写出"形式合格、技术空心"的空壳（gaps 三条缺口
全是这么来的）。升级为三级：丢无关章节 → 白名单按节分配 → 头尾兜底。

覆盖四块：
  1. _split_sections          章节切分（标题行识别、前导段落归属）
  2. _truncate_by_sections    白名单分配：相关章节存活率高于无关章节、
                              每章头尾都保留、总量卡住预算
  3. _compress 调度           超预算才压 / 无章节结构退回头尾兜底
  4. drop_paper 接线          交付轮发送副本不含论文原文，
                              消息库本体不受影响（铁律①）

用法：python test_preparers.py   （通过打 OK，断言失败抛 AssertionError）
"""

import config
from preparers import PaperDigestPreparer, PAPER_BEGIN, PAPER_END


def _make_paper() -> str:
    """四章节假论文：摘要(白名单短节) / 杂项(非白名单) / 方法(白名单长节)
    / 实验结果(白名单)。各章内容用不同字符填充，便于断言头尾存活。"""
    return (
        "## 摘要\n" + "摘" * 200 + "\n"
        "## 第一章 杂项内容\n" + "杂" * 2000 + "\n"
        "## 2 方法\n" + "方" * 4000 + "\n"
        "## 3 实验结果\n" + "实" * 2000 + "\n"
    )


def test_split_sections():
    p = PaperDigestPreparer()
    sections = p._split_sections(_make_paper())
    # 4 个标题 → 4 个章节；每章标题行正确、正文是填充字符
    assert len(sections) == 4, [h for h, _ in sections]
    assert sections[0][0].strip() == "## 摘要"
    assert sections[2][0].strip() == "## 2 方法"
    assert all("方" in "".join(b) for _, b in [sections[2]])
    # 前导内容（首个标题之前）归属 (None, 前导行)
    sections2 = p._split_sections("前言部分\n## 标题\n正文")
    assert sections2[0] == (None, ["前言部分\n"])
    print("OK  _split_sections：章节切分与前导归属正确")


def test_truncate_by_sections():
    p = PaperDigestPreparer()
    paper = _make_paper()
    budget = 3000
    old = config.PAPER_CHAR_BUDGET
    config.PAPER_CHAR_BUDGET = budget
    try:
        out = p._truncate_by_sections(paper, budget)
    finally:
        config.PAPER_CHAR_BUDGET = old

    # ① 显式声明策略，模型知道哪些章节被省略过
    assert "白名单压缩" in out and "本节截断" in out
    # ② 总量卡住预算（各节截断说明允许少量超出）
    assert len(out) <= budget + 300, len(out)
    # ③ 每章头尾都存活（旧版全局头尾截断做不到这一点——中间章节整段蒸发）
    for ch in ("摘", "杂", "方", "实"):
        seg = next(s for s in out.splitlines() if s.startswith(ch * 10))
        assert ch * 10 in seg[:200], f"{ch} 章开头丢失"
        assert ch * 10 in seg[-200:], f"{ch} 章结尾丢失"
    # ④ 白名单章节存活率高于非白名单章节（方法 1.0 权重 vs 杂项 0.5）
    def _kept_ratio(ch, total):
        seg = next(s for s in out.splitlines() if s.startswith(ch * 10))
        return len(seg) / total
    assert _kept_ratio("方", 4008) > _kept_ratio("杂", 2011), \
        "白名单章节（方法）的存活率应高于非白名单章节（杂项）"
    print("OK  _truncate_by_sections：白名单优先 + 每章头尾保留 + 总量卡线")


def test_compress_dispatch():
    p = PaperDigestPreparer()
    old = config.PAPER_CHAR_BUDGET
    config.PAPER_CHAR_BUDGET = 3000
    try:
        # 未超预算 → 原样返回（不白压）
        short = _make_paper()[:2000]
        assert p._compress(short) == short
        # 无章节结构的超长文本 → 退回头尾截断兜底
        blob = "无标题纯文本段落。" * 500
        out = p._compress(blob)
        assert "截断" in out and len(out) <= 3200
        # 有章节结构且含参考文献 → 先丢参考文献，再白名单分配
        paper = _make_paper() + "## 参考文献\n" + "文" * 3000 + "\n"
        out2 = p._compress(paper)
        # 参考文献的标题行与正文都不得出现（说明注记里提到"已删除"不算）
        assert "## 参考文献" not in out2 and "文" * 50 not in out2, \
            "参考文献应被整段删除"
        assert "白名单压缩" in out2
    finally:
        config.PAPER_CHAR_BUDGET = old
    print("OK  _compress 三级调度：不白压 / 无结构兜底 / 先丢参考文献再分配")


def test_drop_paper_delivery_turn():
    """交付轮 drop_paper：发送副本只留论文开头（标题区），消息库本体不受影响。"""
    from loop import AgentLoop
    from model_client import MockClient

    class RecordingMock(MockClient):
        """记录最近一次收到的 messages，供断言发送副本内容。"""
        def __init__(self):
            super().__init__(script=[{"text": "好"}])
            self.seen_messages = None

        def chat(self, messages, max_tokens=None, tools=None):
            self.seen_messages = messages
            return super().chat(messages, max_tokens, tools)

    mock = RecordingMock()
    agent = AgentLoop(mock)
    paper = ("论文标题：某测试论文\n\n" + "这是论文正文。" * 200
             + "\n论文结尾标记XYZ" * 50)  # 远超 600 字符
    agent.store.add("user",
                    f"以下是需要转换为中国发明专利的论文全文：\n"
                    f"{PAPER_BEGIN}\n{paper}\n{PAPER_END}")
    agent.run_section_turn(
        {"section": "交付JSON", "requirement": "组装各节草稿",
         "prior_sections": ["说明书摘要"]},
        max_tokens=100, drop_paper=True)
    sent = "".join(m["content"] for m in mock.seen_messages)
    # 发送副本：论文开头（标题区）保留供填 source_title，正文被截掉
    assert mock.seen_messages is not None
    assert "论文标题：某测试论文" in sent, "论文开头应保留（source_title 来源）"
    assert "无需重发" in sent, "应有显式省略说明"
    assert "论文结尾标记XYZ" not in sent, "论文尾部正文不应重发"
    # 消息库本体：论文原文原封不动（铁律①——Preparer/轮次只改发送副本）
    assert any("论文结尾标记XYZ" in m["content"]
               for m in agent.store._messages), \
        "消息库里的论文原文必须完整保留"
    print("OK  drop_paper：交付轮只留论文开头，正文不重发，消息库不动")


if __name__ == "__main__":
    test_split_sections()
    test_truncate_by_sections()
    test_compress_dispatch()
    test_drop_paper_delivery_turn()
    print("\nL1 白名单压缩单测全部通过")
