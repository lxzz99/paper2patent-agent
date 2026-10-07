# -*- coding: utf-8 -*-
"""最小主循环：整个 agent 的心脏。

一句话概括它做的事（对应交接文档步骤1的验收标准）：

    while 循环：取消息库发送副本 → 调模型 → 把回复写回消息库

这就是 Claude Code / OneCode 这类 agent 的骨架形态。循环体是
"加工副本 → 调模型 → 双保险恢复 → 写回"，按蓝图逐步加装完成：

    ✅ 步骤2  L1 PaperDigestPreparer：发副本前先压缩论文（有字符预算）
    ✅ 步骤3  双保险：输入侧超限→收紧预算重试；输出侧截断→续写恢复
    ✅ 步骤4  L2 PatentSpecPreparer + L3 SectionProjector：注入规范、投影小节
    ✅ 步骤5  交付管线：assemble_delivery_json（第 5 轮组装结构化 JSON
              + 本地校验回喂）→ tools.run_delivery_pipeline 出 docx/pdf

双保险（方案②）的设计要点，来自 OneCode core/loop.py:28-33 / 594-596：

    |        | 输入侧保险              | 输出侧保险                  |
    | 触发   | 请求被拒(HTTP413/超限)  | finish_reason == "length"   |
    | 锚点   | 模型还没开始生成        | 模型开始了但没写完          |
    | 恢复   | 收紧L1预算→重发本轮     | 升级max_tokens→续写提示≤3次 |
    | 要点   | retryable=False重试无用 | 截断不是错误，是"未完成"    |
"""

# 续写提示词。中文版改写自 OneCode CONTINUATION_PROMPT（core/loop.py:29-33）：
# "Resume directly; no apology, no recap ... Pick up mid-thought"。
# 三个关键词缺一不可：直接续写（不道歉）、不复述（no recap）、
# 从断点接续（mid-thought）——少一句模型就会把全文重写一遍。
CONTINUATION_PROMPT = (
    "你的上一条回复因输出长度限制被截断，没有写完。"
    "请直接从中断处继续输出剩余内容：不要道歉、不要重复或总结已输出的部分、"
    "不要加任何开场白，就当作没有被截断过，从被打断的那个字接着写，直到写完。"
)

import json
import re
import time
from pathlib import Path

import config
from model_client import is_context_limit_error
from preparers import PAPER_BEGIN, PAPER_END, PatentSpecPreparer, SectionProjector, _RULES_DIR
from tools import TOOL_SCHEMAS, execute_tool

# 截断续写时 max_tokens 的升级阶梯（每次续写用上一档）：8000 装不下就让
# 32000 接手；上限对应 config.ESCALATED_MAX_OUTPUT_TOKENS（思考 token
# 计入 max_tokens 的实测依据见 config 注释）
_OUTPUT_TOKEN_LADDER = (8000, config.ESCALATED_MAX_OUTPUT_TOKENS)

# ── 分节生成的任务清单（完整专利的生成顺序与各节要求）─────────────────
# 摘要附图并入"说明书摘要"节（它只是一行选取建议，不值得单独一轮调用）。
# 所以四节覆盖五大部分。要求措辞与 SYSTEM_PROMPT/注入规范保持同一口径。
DEFAULT_SECTIONS = [
    ("说明书摘要",
     "不超过300字，且全文只有结尾一个句号（中途不得断句），"
     "开头为'本发明公开一种[发明名称]，属于[技术领域]'，"
     "含方案核心与技术效果；"
     "最后单独一行给出摘要附图建议（注：建议选取说明书附图中的图X作为摘要附图）。"),
    ("权利要求书",
     "10项以内：权1为方法独权（步骤化表述），如论文支持系统/装置再增加装置独权，"
     "其余为从属权利要求；用'其特征在于'引出技术特征；每项只有结尾一个句号；"
     "禁止使用'等、大约、优选、可以、比如、不限于'等不确定词汇。"),
    ("说明书",
     "五段式：技术领域；技术背景（技术定义/现有技术/存在问题及后果，三层次）；"
     "发明内容（简要概括/方案细化/效果说明，三层次）；附图说明；"
     "具体实施方式（每个关键步骤回答：是什么/解决什么问题/怎么解决/达到什么效果）。"),
    ("说明书附图",
     "逐图给出黑白线条图（流程图/框图）的文字描述，每图编号（图1、图2……），"
     "图中模块与步骤名称必须与已完成的权利要求书、说明书完全一致。"),
]

# ── 交付 JSON 组装（"最后一公里"：模型文本 → skill 的结构化契约）─────────
# skill 的正式文档生成器（generate_patent_docx.py 等）吃的是结构化 JSON，
# 契约定义在 references/document-generation.md 的 "Structured Content
# Contract" 一节。与 L2 同一原则：skill 文件是 normative source，运行时
# 读原文件注入——skill 契约更新后 agent 不用改代码。读不到时用内置兜底。
_JSON_CONTRACT_CUT = "## Structured Content Contract"

_JSON_CONTRACT_FALLBACK = """字段契约（必需）：
- invention_name: 发明名称字符串
- source_title: 来源论文标题
- abstract: 说明书摘要正文（一个字符串）
- abstract_drawing: 摘要附图建议（如"建议选取图1作为摘要附图"）
- claims: 权利要求字符串数组，每条形如 "1.一种……，其特征在于……。"
- description: 对象，含 technical_field/background/invention_content/
  drawing_description/embodiments 五个键（附图说明可为字符串数组）
- drawings: 附图文字描述数组，如 "图1：……方法流程图，包含步骤S101～S103。"
只输出 JSON 对象本体，不要解释，不要用代码块包裹。"""

_PATENT_JSON_REQUIRED = ("invention_name", "abstract", "claims", "description", "drawings")
_DESCRIPTION_REQUIRED = ("technical_field", "background", "invention_content",
                         "drawing_description", "embodiments")
# 脚本拥有的字段：generate_patent_drawings.py 会生成 drawing_assets /
# image_model_prompts / drawing_validation 并用 --update-json 写回。
# 关键陷阱：脚本发现 JSON 里已有 drawing_assets 时会【优先采用它、完全
# 忽略 drawings】（infer_assets 的分支逻辑）——而模型按契约示例脑补的
# drawing_assets 没有 spec 条目，脚本必然解析失败。所以必须在校验层
# 禁止模型输出这些字段，把它们留给脚本。
_MODEL_FORBIDDEN = ("drawing_assets", "image_model_prompts", "drawing_validation")


def _load_json_contract() -> str:
    """从 skill 的 document-generation.md 读交付 JSON 契约，读不到用兜底。"""
    try:
        text = (_RULES_DIR / "document-generation.md").read_text(encoding="utf-8")
    except OSError:
        return _JSON_CONTRACT_FALLBACK
    if _JSON_CONTRACT_CUT not in text:
        return _JSON_CONTRACT_FALLBACK
    section = text.split(_JSON_CONTRACT_CUT, 1)[1]
    end = section.find("\n## ")  # 契约节到下一个二级标题为止
    if end != -1:
        section = section[:end]
    return section.strip()


def _strip_code_fence(text: str) -> str:
    """剥掉模型偶尔包裹 JSON 的 ``` 代码块围栏。"""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _validate_patent_json(reply: str) -> tuple[dict | None, str]:
    """校验模型输出的交付 JSON。返回 (数据, "") 或 (None, 全部错误汇总)。

    校验是确定性的本地代码：结构对不对机器说了算，内容好不好模型负责。
    错误【汇总】而不是报第一个就停：每次重试都要花真实 token，一次把
    所有问题回喂，模型一轮修正到位——真实运行实证：只报 drawings 格式
    一个错，模型第 2 次修了 drawings 却漏了 drawing_description 的图号
    同步，交付物里附图说明与实际附图打架。
    """
    try:
        data = json.loads(_strip_code_fence(reply))
    except json.JSONDecodeError as e:
        return None, f"不是合法 JSON（{e}）"
    if not isinstance(data, dict):
        return None, "顶层必须是 JSON 对象"
    errors: list[str] = []
    missing = [k for k in _PATENT_JSON_REQUIRED if k not in data]
    if missing:
        errors.append(f"缺少必填字段：{missing}")
    forbidden = [k for k in _MODEL_FORBIDDEN if k in data]
    if forbidden:
        errors.append(f"不要输出 {forbidden}——这些字段由附图生成脚本自动产生"
                      f"并写回，模型只输出 drawings（附图文字描述）即可")
    if not isinstance(data.get("claims"), list) or not data["claims"]:
        errors.append("claims 必须是非空数组")
    desc = data.get("description")
    desc_ok = isinstance(desc, dict)
    if not desc_ok:
        errors.append("description 必须是对象")
    else:
        missing_desc = [k for k in _DESCRIPTION_REQUIRED if k not in desc]
        if missing_desc:
            errors.append(f"description 缺少字段：{missing_desc}")
    # drawings 的"可解析性"检查：下游 generate_patent_drawings.py 按
    # "S101 步骤名；S102 步骤名；……"（流程图）或"包含……模块101、……"
    # （结构框图）的紧凑条目解析生成矢量图；叙事体画面描述解析不出
    # 条目，脚本会直接报错。这种内容格式问题机器能确定地判出来，
    # 就该在校验层拦下并回喂模型修正，而不是等脚本炸掉。
    drawings = data.get("drawings")
    drawings_ok = isinstance(drawings, list) and bool(drawings)
    if not drawings_ok and "drawings" not in missing:
        errors.append("drawings 必须是非空数组")
    if drawings_ok:
        for i, d in enumerate(drawings, 1):
            if not isinstance(d, str) or not d.strip():
                errors.append(f"drawings 第{i}条必须是附图文字描述字符串")
            elif len(re.findall(r"S\d{3}", d)) < 2 and not ("模块" in d and "包含" in d):
                errors.append(
                    f"drawings 第{i}条缺少可解析的步骤/模块条目。流程图请写"
                    f"'图X：……流程图，包含步骤S101 步骤名；S102 步骤名；……'"
                    f"（至少2条，编号名称与权利要求书一致）；结构框图请写"
                    f"'图X：……结构框图，包含……模块101、……模块102、……'。"
                    f"不要写叙事体画面描述。")
    # 图号一致性：附图说明与实际附图必须一一对应。真实运行实证：模型为
    # 通过格式校验把 drawings 从"趋势示意图"改写成流程图，却漏改
    # drawing_description，交付物里两处图号含义不同。
    if desc_ok and drawings_ok:
        fig_drawings = set(re.findall(r"图\d+", " ".join(drawings)))
        dd = desc.get("drawing_description")
        fig_desc = (set(re.findall(r"图\d+", " ".join(dd)))
                    if isinstance(dd, list) else set())
        if fig_drawings != fig_desc:
            sort = lambda s: sorted(s, key=lambda x: int(x[1:]))
            errors.append(
                f"附图说明与实际附图的图号必须一一对应：drawings 有 "
                f"{'、'.join(sort(fig_drawings))}，drawing_description 写了 "
                f"{'、'.join(sort(fig_desc)) or '（无图号）'}。改写 drawings "
                f"时必须同步更新 drawing_description 及正文中'如图X所示'的引用")
    if errors:
        return None, "；".join(errors)
    return data, ""

# ── 系统提示词（骨架精简版）────────────────────────────────────────────
# 来源：README.md"论文转专利Flash"Prompt（README.md 第 55~110 行）的核心
# 部分，只保留角色 + 任务 + 忠实性红线 + 五大部分骨架。
#
# 完整的撰写规范（权利要求禁词、句号规则、引用逻辑等）【故意不放进骨架】：
# 它们属于步骤4的 L2 PatentSpecPreparer（规范注入层），到时候以
# skills/paper2patent/references/ 下的规则文档为规范来源动态注入。
# 现在就全塞进来，正是"把上下文撑爆"的反面教材——分层才有意义。
SYSTEM_PROMPT = (
    "你是一位世界顶尖的专利代理师与专利工程师，专注于将学术论文的核心技术"
    "转化为严谨、可授权的高质量中国发明专利申请文件。\n"
    "请阅读我提供的论文原文，深度挖掘其核心机制、算法步骤与架构创新，"
    "然后转化为标准的中国专利申请文件。\n"
    "【绝对红线】深度挖掘不等于创造性扩展：你必须且只能从论文中提取信息，"
    "绝对不可编造、修改、歪曲原文内容。论文未提及的细节宁可不写，绝不脑补。\n"
    "输出按以下五大部分组织：\n"
    "一、说明书摘要（300字以内）；二、摘要附图（文字说明选取图X）；\n"
    "三、权利要求书（10项以内，每项只有结尾一个句号）；\n"
    "四、说明书（技术领域/背景技术/发明内容/附图说明/具体实施方式）；\n"
    "五、说明书附图（以文字形式描述黑白线条框图/流程图）。"
)


class AgentLoop:
    """一个 agent 会话 = 一个 AgentLoop 实例。

    持有三样东西：
      store     —— 消息库（原始记录，只进不改）
      client    —— 模型客户端（真实 ArkClient 或 MockClient，接口相同）
      preparer  —— 发送前的 Preparer 洋葱链（默认挂 L1 压缩层）
    """

    def __init__(self, client, preparer=None) -> None:
        from messages import MessageStore  # 局部导入，避免模块循环依赖
        from preparers import PaperDigestPreparer
        self.client = client
        self.store = MessageStore()
        # 会话的第一条消息是系统提示。它进入消息库之后，
        # 每一轮 send_copy() 都会自动带上，不需要重复拼接。
        self.store.add("system", SYSTEM_PROMPT)
        # ── 洋葱链装配点（ContextEngine 的 build_for_task 等价物）──────
        # 下面的嵌套构造逐层拆开看：
        #
        #   PaperDigestPreparer(              ← L1 压缩层：论文全文→预算内要点
        #       inner=PatentSpecPreparer(         ← L2 注入层：追加撰写规范
        #           inner=SectionProjector()      ← L3 投影层：任务JSON→自然语言
        #       )                                 ← L3 不再传 inner，链到此为止
        #   )
        #
        #   inner= 是关键字参数：创建"外层"实例的同时，把"内层"实例塞进
        #   它的 .inner 属性——"谁包着谁"就是构造参数的嵌套关系。
        #   执行顺序由嵌套决定：prepare 先跑最外层 L1 → 递归到 L2 → L3，
        #   即 L1压缩 → L2注入 → L3投影，"先压再注"保证规范不被压缩吞掉
        # 参数 preparer 允许测试注入自定义链：preparer is not None 时
        # 直接用测试传进来的，否则才构建这条默认三层链。
        self.preparer = preparer if preparer is not None else PaperDigestPreparer(
            inner=PatentSpecPreparer(inner=SectionProjector())
        )
        # 上一轮实际发送的总字符数（压缩后），供 main.py 算压缩率
        self.last_send_chars = 0

    def run_turn(self, user_input: str) -> str:
        """跑完一轮普通对话：写入 → 双保险管道 → 写回 → 返回最终回复。"""
        self.store.add("user", user_input)  # ① 写入原始记录
        return self._execute_turn()

    def run_section_turn(self, task: dict, max_tokens: int | None = None,
                         drop_paper: bool = False) -> str:
        """跑一轮"小节任务"（分节生成模式专用）。

        任务以【小节任务JSON】形态写入消息库：原始记录保留结构化数据，
        便于回放与断点续跑；发送前由 L3 SectionProjector 翻译成
        模型可读的自然语言消息（格式翻译，不丢信息）。
        max_tokens：本节输出预算（分节模式按节给小预算，避免预留浪费）。
        drop_paper：发送副本里不再携带论文原文（消息库不受影响，铁律①）。
            仅供交付 JSON 组装轮使用——那一步只需各节草稿，论文是纯冗余，
            剔掉后全管线最大请求的输入显著变小（Agent Plan 单请求隐藏
            上限的实证对策）。
        """
        self.store.add("user", SectionProjector.TASK_MARK + json.dumps(task, ensure_ascii=False))
        return self._execute_turn(max_tokens=max_tokens, drop_paper=drop_paper)

    def generate_full_draft_sections(self, paper_text: str,
                                     checkpoint_path=None) -> dict:
        """分节生成完整专利五大部分（对抗长文截断的生成策略）。

        与一次性生成（run_turn 全文任务）相比：
          - 每节一次模型调用，单次输出短 → 截断概率大幅降低；
          - 后一节能看到前几节草稿（就在消息库的对话历史里），
            发明名称/术语/标号天然一致；
          - 代价是调用次数变多（4次），上下文随节数线性增长
            （后续可用 L1 压缩旧草稿，目前不做）。

        checkpoint_path 给定时做断点续跑：每完成一节就把 drafts 落盘；
        启动时检测到检查点则跳过已完成的节——限流/网络中断后重跑，
        只补缺的节，绝不重复烧已生成的 token。

        论文只入库一次（PAPER 标记包住），每轮发送副本里 L1 都会
        按预算压缩它。返回 {小节名: 草稿文本}。
        """
        self.store.add("user",
                       f"以下是需要转换为中国发明专利的论文全文：\n"
                       f"{PAPER_BEGIN}\n{paper_text}\n{PAPER_END}")
        drafts: dict[str, str] = {}
        # 断点恢复：检查点里有几节就跳过几节（内容以检查点为准）
        if checkpoint_path is not None and checkpoint_path.exists():
            try:
                drafts = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                print(f"[断点] 已从检查点恢复 {len(drafts)} 节：{list(drafts)}")
            except (json.JSONDecodeError, OSError):
                drafts = {}  # 检查点损坏按无断点处理，从头生成
        for i, (name, requirement) in enumerate(DEFAULT_SECTIONS, 1):
            if name in drafts:
                print(f"[断点] 跳过已完成小节：{name}")
                continue
            # 主动限速：轮次间停顿几秒，避免连发请求触发服务端突发保护（429）
            if config.ARK_TURN_INTERVAL_SECONDS > 0:
                time.sleep(config.ARK_TURN_INTERVAL_SECONDS)
            print(f"[分节] 第 {i}/{len(DEFAULT_SECTIONS)} 节：{name}")
            drafts[name] = self.run_section_turn({
                "section": name,
                "requirement": requirement,
                "prior_sections": list(drafts),  # 已完成小节名，供 L3 渲染
            }, max_tokens=config.SECTION_MAX_OUTPUT_TOKENS or None)
            # 每完成一节立即落盘检查点：中途任何原因退出都不丢已完成节
            if checkpoint_path is not None:
                checkpoint_path.write_text(
                    json.dumps(drafts, ensure_ascii=False, indent=2),
                    encoding="utf-8")
        return drafts

    def assemble_delivery_json(self, prior_sections: dict[str, str]) -> str:
        """第 5 节"交付 JSON"：让模型把前四节草稿组装成 skill 的结构化契约。

        这是"最后一公里"：前四节的产出是 markdown 文本，而 skill 的正式
        文档生成器（generate_patent_docx.py 等）吃的是结构化 JSON。翻译
        由模型做（内容搬运不丢信息，本质接近 L3 的格式翻译），但"对不对"
        由本地确定性校验器说了算（_validate_patent_json）。

        三次尝试：每次不过把【全部】校验错误回喂给模型修正；三次都失败
        才抛 ValueError——宁可报错也不要把坏 JSON 喂给下游生成器。
        返回 JSON 字符串（与消息库里的 assistant 回复一致）。
        """
        task = {
            "section": "交付JSON",
            "requirement": (
                "把以上各节已完成的内容组装成一个 JSON 对象，字段要求如下：\n"
                + _load_json_contract()
                + "\n\n【禁止输出的字段】不要输出 drawing_assets、"
                "image_model_prompts、drawing_validation、source_figures——"
                "这些由下游附图脚本自动生成并写回，你只负责 drawings。"
                "\n\n【drawings 写法硬性要求】下游脚本按紧凑条目解析附图："
                "方法流程图写'图1：……方法流程图，包含步骤S101 步骤名称；"
                "S102 步骤名称；S103 步骤名称。'（步骤编号与名称必须与权利要求"
                "书完全一致）；系统结构框图写'图2：……系统结构框图，包含"
                "模块名称101、模块名称102、模块名称103。'（模块名称与标号必须"
                "与权利要求书完全一致）。禁止写成叙事体画面描述。"
                "\n\n【全文一致性硬性要求】drawings 的图号与图名必须与 "
                "description.drawing_description（附图说明）一一对应；若为"
                "满足附图格式要求改写了某张图（如叙事体示意图改写为流程图），"
                "必须同步改写附图说明以及说明书正文中所有'如图X所示'的引用。"
                "\n\n【source_title 硬性要求】source_title 必须逐字抄写论文"
                "开头的原标题，不得转写、缩写或翻译。"
            ),
            "prior_sections": list(prior_sections),
        }
        last_error = ""
        for attempt in (1, 2, 3):
            if attempt == 2:  # 第二次尝试：把校验错误回喂，让模型对症修正
                # 与分节生成同样的主动限速，避免连发触发突发保护
                if config.ARK_TURN_INTERVAL_SECONDS > 0:
                    time.sleep(config.ARK_TURN_INTERVAL_SECONDS)
                task["requirement"] += (
                    f"\n\n注意：你上一次的输出未通过校验，问题：{last_error}。"
                    "请修正后重新输出完整 JSON。")
            print(f"[交付] 组装结构化 JSON（第 {attempt} 次尝试）")
            # 与分节同样的按节预算：交付请求带全部草稿，是全管线最大的
            # 请求，16000 预留会撞 Agent Plan 的单请求隐藏上限（429 实证）。
            # drop_paper：组装只需草稿，论文原文不重发——最大请求再瘦身
            reply = self.run_section_turn(
                task, max_tokens=config.SECTION_MAX_OUTPUT_TOKENS or None,
                drop_paper=True)
            data, err = _validate_patent_json(reply)
            if data is not None:
                return _strip_code_fence(reply)
            last_error = err
            print(f"[交付] 校验未通过：{err}")
        raise ValueError(f"交付 JSON 三次校验均失败，最后一次问题：{last_error}")

    def _execute_turn(self, max_tokens: int | None = None,
                      drop_paper: bool = False) -> str:
        """共享管道：加工副本 + 输入侧保险 → 调模型 → 输出侧保险 → 写回。

        run_turn 与 run_section_turn 的公共主体。调用前提：
        本轮的 user 消息已由调用方写入消息库。
        """
        # ② 输入侧保险 + 加工发送副本。
        #    send_copy() 深拷贝 → Preparer 洋葱链逐层加工（铁律①②）。
        #    若模型还没生成就以"上下文超限"打回（HTTP 413/400超限文案），
        #    盲目重发没有用（retryable=False），正确恢复方式是收紧 L1
        #    预算后【重新加工一份新副本】再发——所以每次尝试都从
        #    send_copy() 重新开始，而不是复用已压缩的旧副本。
        for attempt in range(1 + config.MAX_CONTEXT_RECOVERY_RETRIES):
            messages = self.store.send_copy()
            if self.preparer is not None:
                messages = self.preparer.prepare(messages)
            if drop_paper:
                # 只改发送副本（消息库里的论文原文不动，铁律①）。交付轮
                # 只需各节草稿，论文正文是纯冗余；但【开头 600 字符】保留
                # ——标题/摘要是 source_title 等元信息的唯一来源（首版
                # 全剔导致模型报"论文标题未提供"，实测教训）
                for msg in messages:
                    if PAPER_BEGIN not in msg["content"]:
                        continue
                    before, _, after = msg["content"].partition(PAPER_BEGIN)
                    seg, _, rest = after.partition(PAPER_END)
                    kept = seg[:600].rstrip()
                    note = ("……【论文正文已在分节撰写阶段使用，本步组装交付"
                            " JSON 无需重发；以上为论文开头（标题/摘要区），"
                            "供填写 source_title 等元信息】"
                            if len(seg) > 600 else "")
                    msg["content"] = (f"{before}{PAPER_BEGIN}\n{kept}\n{note}"
                                      f"{PAPER_END}{rest}")
            self.last_send_chars = sum(len(m["content"]) for m in messages)
            # 方案B 开关：ENABLE_TOOLS 时把工具 schema 随请求发给模型，
            # 由模型自己决定"要不要调、调哪个"（workflow 与工具循环的分工：
            # 写专利的主流程仍是写死的分节 workflow，工具是流程内的自由动作）
            tools = TOOL_SCHEMAS if config.ENABLE_TOOLS else None
            try:
                # 首次请求的 max_tokens：调用方指定（分节模式按节给小预算）
                # 优先；否则用全局默认（0 = 不设，服务商默认值）
                initial_max = max_tokens if max_tokens is not None \
                    else (config.INITIAL_MAX_OUTPUT_TOKENS or None)
                reply = self.client.chat(messages, max_tokens=initial_max,
                                         tools=tools)
                break
            except Exception as e:
                more_attempts = attempt < config.MAX_CONTEXT_RECOVERY_RETRIES
                if more_attempts and is_context_limit_error(e):
                    shrunk = max(500, config.PAPER_CHAR_BUDGET // 2)
                    print(f"[输入侧保险] 上下文超限被拒（模型未生成就打回），"
                          f"收紧 L1 预算 {config.PAPER_CHAR_BUDGET}→{shrunk}，重试本轮")
                    # 收紧后保持生效：同一会话里论文不会自己变短，下一次
                    # 本来就该用更紧的预算（可通过环境变量重置进程）
                    config.PAPER_CHAR_BUDGET = shrunk
                    continue
                raise  # 非超限错误（或重试次数用尽）原样上抛

        # ②.5 工具轮循环（方案B function calling）：
        #    模型回复带 tool_calls → harness 执行工具 → 结果以 role="tool"
        #    消息回传 → 重新询问模型，直到模型不再要工具（或轮数用尽）。
        #    全程发生在本轮的发送副本 messages 上；同时把每次往返收集进
        #    tool_exchanges，回合结束后写回消息库——工具执行结果是真实
        #    发生的对话事件，下一轮上下文必须能看到（否则模型会重复调工具）。
        tool_exchanges: list[dict] = []
        tool_rounds = 0
        while reply.tool_calls and tool_rounds < config.MAX_TOOL_ROUNDS:
            tool_rounds += 1
            # 先补记"assistant 发起调用"这条消息（OpenAI 格式要求：
            # 带 tool_calls 的 assistant 消息必须出现在 role=tool 消息之前）
            assistant_tool_msg = {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name,
                                  "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                    for tc in reply.tool_calls
                ],
            }
            messages.append(assistant_tool_msg)
            tool_exchanges.append(assistant_tool_msg)
            # 逐个执行本轮要的工具，结果逐条以 role="tool" 回传
            for tc in reply.tool_calls:
                result = execute_tool(tc.name, tc.arguments)
                print(f"[工具轮 {tool_rounds}/{config.MAX_TOOL_ROUNDS}] "
                      f"{tc.name}({json.dumps(tc.arguments, ensure_ascii=False)[:80]}) "
                      f"→ 返回 {len(result)} 字符")
                tool_msg = {"role": "tool", "tool_call_id": tc.id, "content": result}
                messages.append(tool_msg)
                tool_exchanges.append(tool_msg)
            # 带着工具结果重新询问模型（继续提供 tools，模型可能还要用）
            reply = self.client.chat(messages, tools=tools)

        # ③ 输出侧保险：截断不是错误，是"未完成"——发续写提示让模型接着写。
        #    与 OneCode 的差异（loop.py:594-596 是把截断消息+提示写进消息库）：
        #    我们只把"拼接完成的最终回复"写进消息库，续写往返只存在于
        #    发送副本里。取舍理由：消息库保存的是原始素材的最终形态，
        #    续写过程属于发送层细节，下一轮上下文只需要完整回复。
        pieces: list[str] = [reply.content]
        finish = reply.finish_reason
        self.continuation_count = 0
        while finish == "length" and self.continuation_count < config.MAX_OUTPUT_RECOVERY_RETRIES:
            self.continuation_count += 1
            max_out = _OUTPUT_TOKEN_LADDER[min(self.continuation_count, len(_OUTPUT_TOKEN_LADDER)) - 1]
            print(f"[输出侧保险] 检测到截断（模型开始了没写完），"
                  f"第 {self.continuation_count}/{config.MAX_OUTPUT_RECOVERY_RETRIES} 次续写，"
                  f"max_tokens 升至 {max_out}")
            cont_messages = messages + [
                {"role": "assistant", "content": "".join(pieces)},  # 已写的部分
                {"role": "user", "content": CONTINUATION_PROMPT},   # 续写指令
            ]
            more = self.client.chat(cont_messages, max_tokens=max_out, tools=tools)
            pieces.append(more.content)
            finish = more.finish_reason

        final = "".join(pieces)
        if finish == "length":  # 续写次数用尽仍是截断 → 放弃，如实保留已完成部分
            final += "\n\n【警告：已达续写次数上限仍未写完，以上为已完成部分】"
            print("[输出侧保险] 续写次数用尽，放弃续写")
        if reply.tool_calls:  # 工具轮数用尽模型还要调 → 如实收尾，不再执行
            final += "\n\n【警告：已达工具调用轮数上限，本轮不再执行工具】"
            print("[工具循环] 达到 MAX_TOOL_ROUNDS 上限，停止执行工具")

        # ④ 写回：工具往返 + 最终回复追加进消息库，成为下一轮的上下文。
        #    工具往返是原始对话事件（谁调了什么、结果是什么），照实入库；
        #    铁律①不受影响——这里写的是"新发生的事件"，不是改旧记录。
        for msg in tool_exchanges:
            extra = {k: v for k, v in msg.items() if k not in ("role", "content")}
            self.store.add(msg["role"], msg["content"], **extra)
        self.store.add("assistant", final)
        self.last_finish_reason = finish

        return final
