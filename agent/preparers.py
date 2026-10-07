# -*- coding: utf-8 -*-
"""Preparer 洋葱链：发送前对消息副本做逐层加工的流水线。

对应蓝图方案①（洋葱链上下文工程），完整形态是三层：

    build_for_task(state)                    ← ContextEngine 等价物（loop.py 里的装配）
     └─ PaperDigestPreparer                  ← L1 压缩：论文全文 → 预算内技术要点（三级策略）
         └─ PatentSpecPreparer               ← L2 注入：专利撰写规范/五书模板
             └─ SectionProjector             ← L3 投影：小节任务 JSON → 模型可读消息

三条铁律（交接文档里的设计原则）在本文件落地了前两条：
  铁律①  Preparer 只改发送副本 send_copy() —— 本模块永远不接触 MessageStore 本体，
  铁律②  inner= 嵌套装配（从内到外），执行顺序（从外到内）；
        嵌套装配是 静态结构（代码里谁包着谁），执行顺序是 动态行为（运行时谁先跑）
        装配时 L1 套在最外层 → 执行顺序自然是 L1（有损）压缩 → L2注入 → L3投影，
        "先压缩再注入"保证L2规范/模板不会被压缩吞掉。
  （铁律③：L1 是内容压缩可丢细节，L3 是格式翻译不丢信息——本轮只做 L1，等步骤4做 L3 时两种性质分开实现，不混。）
"""

import json
import re
from pathlib import Path

import config

# ── 论文段的显式标记 ──────────────────────────────────────────────────
# 为什么需要标记：Preparer 必须精确知道"哪些字符是论文、哪些是指令"，
# 否则压缩可能误伤任务指令。所以注入论文的地方（main.py / tools.py）
# 统一用这一对标记包住论文全文，L1 只压缩标记之间的内容。
PAPER_BEGIN = "━━━━━━━━ 论文原文开始 ━━━━━━━━"
PAPER_END = "━━━━━━━━ 论文原文结束 ━━━━━━━━"


class Preparer:
    """Preparer 基类：只负责"怎么包、怎么传"，不负责"加工什么"。

    【什么是洋葱链】三个子类互相包着（L1 包 L2，L2 包 L3），发送前
    消息依次被三层加工。包装动作 = 构造时传 inner= 参数；传递动作 =
    下面的 prepare() 递归。加工逻辑全部在子类的 _apply() 里。

    入参 messages 来自 MessageStore.send_copy()。
    """

    def __init__(self, inner: "Preparer | None" = None) -> None:
        # 参数逐段看：
        #   inner                参数名：内层 Preparer（本层包着的下一层）
        #   : "Preparer | None"  类型注解：要么是 Preparer 对象，要么 None
        #                        （链的最内层没有下一层）
        #   = None               默认值：不传 inner = "我是最内层"
        # 用法：PaperDigestPreparer(inner=L2实例)——创建 L1 的同时把 L2
        # 塞进它的 .inner 属性，"包"就是一次普通的构造传参。
        self.inner = inner  # 存到实例属性上，prepare() 靠它找到下一层

    def prepare(self, messages: list[dict]) -> list[dict]:
        """逐行执行流程（以 L1(inner=L2) 为例）：

        行①  messages = self._apply(messages)
             self 是 L1 → 执行 L1 的加工（压缩论文），返回的新列表重新
             赋给 messages——此后传的都是加工过的版本。

        行②  if self.inner is not None:
             L1.inner 是 L2 → 成立，继续递归；最内层的 inner 是 None →
             不成立，递归到此为止（这就是递归出口）。

        行③  messages = self.inner.prepare(messages)
             调内层的 prepare——内层重复完全相同的三步：先自己 _apply，
             再看自己的 inner……一层层往里剥（这就是"洋葱"）。

        行④  return messages
             最内层加工完的结果原样往回传，一层层退栈，最终交还给
             调用方 loop._execute_turn。加工都发生在"进去"的路上，
             返回值只是原样上浮。
        """
        messages = self._apply(messages)              # ① 本层先加工
        if self.inner is not None:                    # ② 有内层才递归
            messages = self.inner.prepare(messages)   # ③ 交给内层重复同样流程
        return messages                               # ④ 内层结果 = 最终结果

    def _apply(self, messages: list[dict]) -> list[dict]:
        """子类覆盖：本层的加工逻辑。基类默认原样通过。"""
        return messages


class PaperDigestPreparer(Preparer):
    """L1 压缩层：论文全文 → 预算内的技术要点（三级策略）。

    工作方式（"L1 压缩：有字符预算，超了才压"）：
      1. 扫描每条消息里 PAPER_BEGIN/PAPER_END 标记之间的论文段；
      2. 论文段 ≤ config.PAPER_CHAR_BUDGET → 原样通过（不白压）；
      3. 超预算 → 进 _compress 的三级调度（见其注释）。

    压缩策略的三次进化（每一级都是真实运行暴露问题后补的）：
        v1（废弃） 无脑头尾截断（头70%+尾30%）：
            问题：论文的中间字符核心内容——整段蒸发，专利只能写出"形式合格、技术空心"的空壳；
        【考虑 结构化抽取】（_extract_by_sections）
        v2 黑名单删除：按章节标题整段删除参考，
            文献/致谢/附录，其余完整保留，
            问题：字符仍超预算；
        v3 白名单定权重 × 章节长度 = 章节预算（_truncate_by_sections）：
            按"权重×长度"给每章分配字符预算、章内头尾都保，
            问题：标题缺失
        v4 【完整保留论文开头】——标题/摘要区是交付阶段source_title 的唯一来源。

    last_stats 记录最近一次加工统计，供 main.py 打印压缩率（演示用）。
    """

    # 黑名单：这些章节对专利撰写没有价值（引文列表/致谢/附录代码），整段删除。
    # 刻意保持清单极短：误删一章技术内容的代价远大于少省几个字符。
    _DROP_SECTIONS = ("参考文献", "references", "致谢", "acknowledg", "附录", "appendix")

    # 白名单：命中标题的章节与专利技术方案直接相关（方法/系统/实验数据），
    # 截断时优先保障（权重 1.0）；未命中的章节权重 0.5——标题没认出
    # 不代表内容无关，保守半保，绝不整段丢弃（宁可多留不可误删）。
    _KEEP_SECTIONS = ("摘要", "abstract", "引言", "introduction",
                      "背景", "background", "方法", "method", "模型", "model",
                      "网络", "network", "架构", "architecture", "系统", "system",
                      "实验", "experiment", "结果", "results", "评估", "evaluation",
                      "性能", "performance", "消融", "ablation", "数据集", "dataset",
                      "结论", "conclusion", "公式", "formula", "训练", "training")

    # 章节标题的常见写法：独立的短行，或带编号的短行（"2. 方法"、"3.1 网络结构"）
    _NUMBERED_HEADING = re.compile(r"^\d+(\.\d+)*[\.、]?\s*\S{1,30}$")

    def __init__(self, inner: "Preparer | None" = None) -> None:
        super().__init__(inner)
        self.last_stats: dict | None = None

    def _apply(self, messages: list[dict]) -> list[dict]:
        """L1 层的加工逻辑：压缩每条消息里的论文段，并记录压缩统计。

        【先厘清"论文段"】指一对 PAPER_BEGIN/PAPER_END 标记之间的
        【整篇论文全文】（str 类型），不是章节也不是段落——按章节
        切分是压缩内部（_split_sections）的事，在这一层看不到。
        返回列表只是因为标记对可能出现多次：--chat 模式下
        resolve_local_files 会给用户输入里的每个文件路径都包一对
        标记，一句话贴两个文件就有两个论文段；常规分节流程里只有
        一个，列表长度为 1。

        整体流程："数账 → 压缩 → 再数账 → 存账本"：
          ① 遍历发送副本的每条消息，没有论文标记的直接跳过
             （系统提示/任务指令等一个字不碰，L1 只对论文段动手）；
          ② 压缩前先统计：论文段有几个、共多少字符、几个超预算；
          ③ 核心动作只有一行：_process_content 调 _compress 三级调度，
             把超预算的论文段压到预算内再塞回消息；
          ④ 压缩后再统计一次实际字符数（含省略说明，如实测量）；
          ⑤ 两笔账存进 last_stats，供 main.py 打印压缩率——
             所以打出来的是实测压缩率，不是估算值。
        """
        before = after = 0      # 论文段压缩前/后的总字符数
        sections = compressed = 0   # 论文段个数 / 其中超预算的个数
        #     ⚠ 这里的 sections 指"论文段"的个数，与 _split_sections
        #     的"章节"无关——last_stats 的键名沿用，main.py 按它打印
        budget = config.PAPER_CHAR_BUDGET
        for msg in messages:
            content = msg["content"]
            if PAPER_BEGIN not in content:
                continue  # ① 这条消息里没有论文段，原样通过
            # ② 压缩前：数一遍账（before 以原文为准，只统计不修改）。
            #    seg 是 str——一对标记之间的整篇论文全文
            for seg in self._paper_segments(content):
                sections += 1
                before += len(seg)
                if len(seg) > budget:
                    compressed += 1
            # ③ 全方法唯一改内容的语句：压缩并塞回原消息
            msg["content"] = self._process_content(content)
            # ④ 压缩后：再数一遍账（after 以实际发送内容为准，
            #    含省略说明的长度，如实统计）
            for seg in self._paper_segments(msg["content"]):
                after += len(seg)
        # ⑤ 存账本：main.py 的 print_compression_stats 读它打印压缩率
        self.last_stats = {
            "sections": sections, "compressed": compressed,
            "chars_before": before, "chars_after": after,
            "budget": budget,
        }
        return messages  # 返回加工完的副本，交给内层 Preparer（L2）

    # ── 内部实现 ─────────────────────────────────────────────────────

    def _paper_segments(self, content: str) -> list[str]:
        """取出所有标记之间的论文段（不含标记本身）。

        每个元素是一个 str = 一对 PAPER_BEGIN/PAPER_END 之间的
        完整论文全文。列表长度 = 这条消息里标记对的出现次数：
        通常为 1（整个流程只注入一篇论文）；大于 1 只发生在
        --chat 模式下一条消息注入了多个文件（每个路径各包一对标记）。
        """
        segments = []
        for block in content.split(PAPER_BEGIN)[1:]:  # [begin后的部分, ...]
            seg = block.split(PAPER_END)[0] if PAPER_END in block else block
            segments.append(seg)
        return segments

    def _process_content(self, content: str) -> str:
        """压缩一条消息里所有论文段（超预算的才压），其余原样保留。

        拆分逻辑：content.split(PAPER_BEGIN) 后，第 0 块是首标记前的
        指令，之后每块 = 论文段 + 结束标记 + 后续文字。每块加工完
        【拼成一个整体】再回填，最后用 PAPER_BEGIN 把各块连回去——
        这能保证"不压缩时拼回结果与原文逐字符相等"（无损往返）。
        """
        chunks = content.split(PAPER_BEGIN)
        out = [chunks[0]]
        for chunk in chunks[1:]:
            if PAPER_END in chunk:
                seg, _, rest = chunk.partition(PAPER_END)
                out.append(self._compress(seg) + PAPER_END + rest)
            else:
                out.append(chunk)  # 有开始标记无结束标记（异常输入），原样保留
        return PAPER_BEGIN.join(out)

    def _compress(self, text: str) -> str:
        """压缩调度器（三级）：丢无关章节 → 白名单按节分配 → 头尾截断兜底。"""
        budget = config.PAPER_CHAR_BUDGET
        if len(text) <= budget:
            return text
        digest = self._extract_by_sections(text)
        if digest is None:  # 识别不出章节结构 → 纯兜底截断
            return self._truncate_head_tail(text, budget, note_kind="截断")
        if len(digest) <= budget:
            return digest
        # 抽取后仍超预算 → 白名单按节分配截断（第二级，2026-09-17 升级：
        # 旧版在此处对全文做头尾截断，42204 字符真实论文的中间约 32581
        # 字符——方法核心章节——整段蒸发，权利要求只能写出空壳。见
        # _truncate_by_sections 注释）
        return self._truncate_by_sections(digest, budget)

    def _split_sections(self, text: str) -> list[tuple[str | None, list[str]]]:
        """按标题行把文本切成 (标题行|None, 正文行列表) 的章节序列。

        首个标题之前的内容算作 (None, 前导行)（摘要前的标题/作者等）。
        供白名单分配使用；与 _extract_by_sections 的逐行状态机不同，
        这里需要整章边界，所以先收集再处理。
        """
        sections: list[tuple[str | None, list[str]]] = []
        head: str | None = None
        body: list[str] = []
        for line in text.splitlines(keepends=True):
            if self._is_heading(line):
                if head is not None or body:
                    sections.append((head, body))
                head, body = line, []
            else:
                body.append(line)
        if head is not None or body:
            sections.append((head, body))
        return sections

    # 文档头保留长度：论文开头是标题/作者/摘要区，也是交付阶段 source_title
    # 等元信息的唯一来源。按权重分配会把前导章节压到几百字符，标题正好
    # 被腰斩（真实论文实证：模型只能抄到 "...Integrating Associat"，gaps
    # 自曝"标题被截断"）。先整段保留开头，剩余预算再按节分配。
    _HEAD_RESERVE = 400

    def _truncate_by_sections(self, text: str, budget: int) -> str:
        """第二级：白名单按节分配截断（替代旧版的全局头尾截断）。

        全局头尾截断的致命伤：一刀切掉论文中间——方法核心章节整段蒸发，
        而它恰恰是专利权利要求/说明书最需要的素材（真实论文实证：摘要
        和实验结论尾巴存活，中间 32581 字符方法细节全丢，模型只能写出
        "形式合格、技术空心"的专利）。按节分配后每章头尾都留，方法
        章节至少存活一部分。

        分配规则：
          - 论文开头 _HEAD_RESERVE 字符（标题/摘要区）整段保留，不参与
            分配——标题被腰斩会让 source_title 无法逐字填写；
          - 命中白名单的章节权重 1.0，未命中权重 0.5（见 _KEEP_SECTIONS）；
          - 各节按 权重×长度 占比分得【剩余】预算，节内沿用头 70% + 尾 30%；
          - 未超自身配额的小节整段保留（不多占，总量允许略超——预算是
            软约束，上下文完整性优先于精确卡线）。
        """
        sections = self._split_sections(text)
        head_txt = ""
        if sections:
            h0, b0 = sections[0]
            first = (h0 or "") + "".join(b0)
            head_txt = first[:self._HEAD_RESERVE]
            rest = first[len(head_txt):]
            sections[0] = (None, [rest] if rest else [])
        budget_rest = max(budget - len(head_txt), 200)
        weights = []
        for head, _body in sections:
            matched = head is not None and any(
                k in head.lower() for k in self._KEEP_SECTIONS)
            weights.append(1.0 if matched else 0.5)
        lens = [len(head or "") + sum(len(l) for l in body)
                for head, body in sections]
        weighted = [w * l for w, l in zip(weights, lens)]
        total = sum(weighted)
        out = []
        for (head, body), alloc in zip(sections,
                                       [int(budget_rest * wl / total)
                                        for wl in weighted]):
            seg = (head or "") + "".join(body)
            if len(seg) <= alloc:
                out.append(seg)
            else:
                out.append(self._truncate_head_tail(seg, alloc,
                                                    note_kind="本节截断"))
        return (head_txt
                + "……【L1白名单压缩：论文超预算，已按章节分配字符预算——"
                "方法/实验等专利相关章节优先保留，论文开头（标题/摘要区）"
                "与各章的开头结尾均完整保留，仅各章中间部分被省略；原始论文"
                "完整保存在消息库】……\n"
                + "".join(out))

    def _extract_by_sections(self, text: str) -> str | None:
        """第一级：按章节抽取。删掉参考文献/致谢/附录整段，其余完整保留。

        返回 None 表示"这篇文本没有可识别的章节结构"，调用方退回兜底。
        算法刻意保守：只有标题行命中丢弃清单才开删，一版普通论文的
        标题误判最多让"删"提前/延后开始，而丢弃清单本身极短（见类注释）。
        """
        lines = text.splitlines(keepends=True)
        kept: list[str] = []
        dropping = False
        for line in lines:
            if self._is_heading(line):
                dropping = any(k in line.lower() for k in self._DROP_SECTIONS)
            if not dropping:
                kept.append(line)
        if len(kept) == len(lines):  # 一行都没删掉 → 无章节结构
            return None
        return ("……【L1真压缩：已按章节抽取——参考文献/致谢/附录等"
                "非技术章节已整段删除，其余内容完整保留】……\n"
                + "".join(kept))

    def _is_heading(self, line: str) -> bool:
        """判断一行是不是章节标题（保守判定：短行 + 特征明显才认）。"""
        s = line.strip().lower()
        if not s or len(s) > 40:
            return False
        if s.startswith("#"):  # Markdown 标题
            return True
        if self._NUMBERED_HEADING.match(s):  # "2. 方法" / "3.1 网络结构"
            # 编号之后必须真的有文字（字母/汉字）：PDF 文字提取常把
            # 公式编号"21"、乱码"2)'(1)("单独断成短行，它们不是标题——
            # 误认会把方法章节切碎、碎片只拿到低权重（真实论文实证）
            num = re.match(r"^\d+(?:\.\d+)*[\.、]?\s*", s)
            rest = s[num.end():].strip()
            if re.search(r"[a-z一-鿿]", rest):
                return True
            return False
        keywords = ("摘要", "abstract", "引言", "introduction", "背景", "background",
                    "相关工作", "related work", "方法", "method", "实验", "experiment",
                    "结果", "results", "结论", "conclusion", "讨论", "discussion")
        if any(s == k or s.startswith(k) for k in keywords) and len(s) <= 25:
            return True
        # 丢弃清单里的章节名本身也是标题行（"参考文献"/"致谢"/"References"…），
        # 不认出它们，_extract_by_sections 的 dropping 永远不会开启。
        return any(s == k or s.startswith(k)
                   for k in self._DROP_SECTIONS) and len(s) <= 15

    def _truncate_head_tail(self, text: str, budget: int, note_kind: str) -> str:
        """兜底层（第三级）：无脑头尾截断（头70% + 中间省略说明 + 尾30%）。

        两个调用方：① _compress 在识别不出章节结构时整体兜底；
        ② _truncate_by_sections 把它当作"节内截断"原语（note_kind
        传"本节截断"，让省略说明能区分是全文截断还是章内截断）。
        头尾都留：开头常有方法总述，结尾常有实验结论/技术效果，
        两头都是专利最需要的部分。
        """
        head = int(budget * 0.7)
        tail = budget - head
        omitted = len(text) - head - tail
        note = (f"\n……【L1{note_kind}：中间省略约 {omitted} 字符；"
                f"原始论文仍完整保存在消息库，此处仅发送副本被截断】……\n")
        return text[:head] + note + text[-tail:]


# ══════════════════════════════════════════════════════════════════════
# L2 规范注入层 + L3 小节投影层（步骤4）
# ══════════════════════════════════════════════════════════════════════

# 规范来源：skill 的 references/ 目录（normative source，见 SKILL.md 第18行）。
# L2 运行时直接读原文件注入——比手抄进代码更忠实：skill 规则更新后
# agent 无需改代码，注入内容自动跟进。找不到文件时退回内置精简版。
# _AGENT_DIR：本文件所在目录（agent/），从左往右四步：
#   __file__    str，Python 自动注入的特殊变量 = 当前文件自身路径
#               （在 preparers.py 里就是 ...\agent\preparers.py）
#   Path(...)   把字符串包装成 Path 对象（pathlib 库的类）
#   .resolve()  解析成规范化绝对路径（去掉 .. 、符号链接等）
#   .parent     取父目录（上一级文件夹）→ 得到 agent 目录
_AGENT_DIR = Path(__file__).resolve().parent

# _RULES_DIR：规则文件目录。整行是一个三元表达式（条件表达式）：
#   getattr(config, "RULES_DIR", "")  从 config 模块取 RULES_DIR 属性；
#                                     没有该属性时返回 ""（getattr 第三参数
#                                     是默认值，不写会直接 AttributeError）
#   if getattr(...)                   属性存在且非空 → 用配置指定的目录
#   else _AGENT_DIR.parent / ...      否则用默认：agent 的上一级（项目根）
#                                     / 后接目录名——注意 / 在这里不是除法，
#                                     是 Path 重载的路径拼接运算符
#                                     （__truediv__），等价于 os.path.join
_RULES_DIR = Path(config.RULES_DIR) if getattr(config, "RULES_DIR", "") else \
    _AGENT_DIR.parent / "skills" / "paper2patent" / "references"

# 注入哪三份：起草规则（权利要求/说明书怎么写）+ 质量清单（自查标准）
# + 附图规范（附图类型/视觉约束/标号一致性——步骤2机检发现"附图无部件
# 标号"后补入的第三来源）。每份可指定"裁剪标记"：标记之后的内容是
# 文件生成/SVG 脚本等与文本生成无关的部分，注入时切掉省上下文。
_RULES_FILES = (
    (_RULES_DIR / "claims-and-specification-rules.md", None),
    (_RULES_DIR / "quality-checklist.md", "## Document Files"),
    (_RULES_DIR / "drawing-generation.md", "## Required Output for Full Applications"),
)

# drawing-generation.md 只讲"标号要与说明书一致"，没讲"必须有标号"。
# 这条要求来自 README 说明书附图规则（"符号规范：附图中各部件应当使用
# 标号标注，标号通常为阿拉伯数字"），单独补一行，注明出处。
_NUMERAL_SUPPLEMENT = (
    "【附图标号补充规范（来源：README 说明书附图·符号规范）】\n"
    "说明书附图与附图说明中的各部件必须使用阿拉伯数字标号（如：卷积层101、"
    "处理器501），同一部件在所有附图中使用相同标号，且标号与说明书正文的"
    "引用一一对应；方法步骤使用 S101、S102、S103 编号，与具体实施方式的"
    "步骤描述一一对应。"
)

# 找不到规范文件时的兜底（保证 agent 脱离本仓库也能跑）。
# 注意：这只是"能跑"的下限，完整规范以 references/ 原文件为准。
_FALLBACK_SPEC = (
    "【撰写规范（精简兜底版）】\n"
    "1. 发明名称在摘要、权利要求书、说明书中必须完全一致；\n"
    "2. 每项权利要求只有结尾一个句号，内部用分号/逗号；\n"
    "3. 权利要求禁用：等、大约、可能、也许、例如、比如、优选、可以、"
    "不限于、部分、某些、若干、基本；\n"
    "4. 从权必须引用真实包含被引特征的前权；多项引用不得再引用多项；\n"
    "5. 说明书五段式；实施方式对每个关键步骤回答：是什么/解决什么问题/"
    "怎么解决/达到什么效果；\n"
    "6. 忠实性红线：不添加论文未记载的内容，不改写数据结论，"
    "论文语言转写为专利语言而非照抄。"
)


class PatentSpecPreparer(Preparer):
    """L2 注入层：把专利撰写规范 append 到发送消息的末尾。

    铁律②的受益者：本层在链上位于 L1 之后——L1 只压缩 PAPER 标记段，
    本层注入的规范永远轮不到被压缩（"先压再注"的全部意义）。

    注入方式：追加一条独立的 user 消息，而不是拼进论文消息。
    理由：① 规范与素材天然是两种上下文，分开便于将来按小节裁剪规范；
    ② 连续两条 user 消息是 OpenAI 兼容接口允许的写法（实测方舟 GLM 可用）。
    """

    def __init__(self, inner: "Preparer | None" = None) -> None:
        super().__init__(inner)
        self._spec_cache: str | None = None  # 规范文件进程内只读一次

    def _load_spec(self) -> str:
        if self._spec_cache is None:
            parts = []
            for path, cut in _RULES_FILES:
                text = path.read_text(encoding="utf-8")
                if cut:  # 裁掉与文本生成无关的章节（见 _RULES_FILES 注释）
                    text = text.split(cut)[0].rstrip()
                parts.append(text)
            self._spec_cache = _NUMERAL_SUPPLEMENT + "\n\n" + "\n\n".join(parts)
        return self._spec_cache

    def _apply(self, messages: list[dict]) -> list[dict]:
        try:
            spec = self._load_spec()
        except OSError:  # 脱离仓库运行（skills 目录不存在）时兜底
            spec = _FALLBACK_SPEC
        messages.append({
            "role": "user",
            "content": f"【以下撰写规范具有最高优先级，撰写时逐条遵守】\n{spec}",
        })
        return messages


class SectionProjector(Preparer):
    """L3 投影层：把结构化的小节任务 JSON 翻译成模型可读的 user 消息。

    铁律③（L3 是格式翻译，不是压缩）：JSON 里的每个字段都必须出现在
    翻译结果里，一个信息都不丢——只换角色和格式，不删内容。
    这与 L1 的"内容压缩可丢细节"性质相反，所以两者绝不能混在一层。

    工作方式：分节生成时（AgentLoop.generate_full_draft_sections），
    小节任务以【小节任务JSON】开头的消息进入消息库（原始记录保留
    结构化形态，便于回放/断点续跑）；L3 在发送前把它翻译成自然语言。
    非 JSON 消息（论文、普通对话）原样通过。
    """

    TASK_MARK = "【小节任务JSON】"

    def _apply(self, messages: list[dict]) -> list[dict]:
        for msg in messages:
            if msg["role"] == "user" and msg["content"].startswith(self.TASK_MARK):
                msg["content"] = self._render(msg["content"][len(self.TASK_MARK):])
        return messages

    def _render(self, json_text: str) -> str:
        task = json.loads(json_text)
        lines = [f"请撰写专利申请文件的【{task['section']}】部分。"]
        if task.get("requirement"):
            lines.append(f"本部分撰写要求：{task['requirement']}")
        if task.get("prior_sections"):
            names = "、".join(task["prior_sections"])
            lines.append(f"此前已完成：{names}（见前文对话。发明名称、技术术语、"
                         "步骤/模块标号必须与已完成部分完全一致）。")
        if task.get("figures"):
            lines.append("图表引用：" + "；".join(task["figures"]))
        lines.append("只输出本部分内容，不要输出其他部分，不要复述论文原文。")
        return "\n".join(lines)
