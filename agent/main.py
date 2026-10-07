# -*- coding: utf-8 -*-
"""CLI 入口：也是整个项目的"全景地图"。

一条数据的完整旅程（分节模式 = 生产路径）：

    论文文件(.txt/.md/.pdf)
      → tools.read_file 读全文（PDF 提取文字层）
      → 消息库 messages.MessageStore（PAPER_BEGIN/END 标记包住，只进不改）
      → 分节生成 loop.generate_full_draft_sections：
          说明书摘要 → 权利要求书 → 说明书 → 说明书附图
          （每节一轮调用，后节可见前节草稿，逐节落盘检查点）
      → 交付组装 loop.assemble_delivery_json：
          模型把四节草稿组装成结构化 JSON，本地校验器把关（≤3 次回喂修正）
      → 交付管线 tools.run_delivery_pipeline：
          skill 脚本出附图(SVG/PNG) → DOCX → PDF，产物全落会话文件夹

九大设计点在代码里的落点（读码/面试索引）：
    ① 消息库 source of truth、深拷贝发送副本   messages.py（铁律①）
    ② Preparer 洋葱链 inner= 嵌套装配          preparers.py Preparer + loop.py 装配点（铁律②）
    ③ L1 三级压缩（删章节→白名单分配→兜底）   preparers.py PaperDigestPreparer
    ④ 分节生成 + 断点续跑判据                  loop.py DEFAULT_SECTIONS / tools.py find_resumable_session
    ⑤ 双保险：输入侧收紧预算 / 输出侧续写      loop.py _execute_turn（判别锚点对照表见 loop.py 模块文档）
    ⑥ function calling 工具循环                tools.py execute_tool / loop.py 工具轮循环
    ⑦ 交付校验分界：结构对错归代码、内容归模型 loop.py _validate_patent_json
    ⑧ 429 分流：额度类不重试 / 突发类递增等待  model_client.py chat + config.py
    ⑨ 保真红线：SystemPrompt→素材质量→gaps     loop.py SYSTEM_PROMPT / preparers.py / 交付校验器

三种用法（都在 agent/ 目录下执行）：

    python main.py 论文.pdf --sections   # 生产路径：分节生成 + 交付 docx/pdf
    python main.py 论文.txt --mock       # 假模型演示，不需要 API key
    python main.py --chat                # 交互模式：多轮对话，输入 q 退出

单轮模式（不带 --sections）的输入组装见 build_task_input()；它保留下来
用作对比基线：一次调用写全文，截断概率高——这正是分节模式要解决的问题。
"""

import argparse
import json
import sys

from loop import AgentLoop
from model_client import make_client
from preparers import PAPER_BEGIN, PAPER_END
from tools import (read_file, write_file, write_json, create_session_dir,
                   find_resumable_session, CHECKPOINT_NAME,
                   run_delivery_pipeline, resolve_local_files)


def load_paper(path: str) -> str:
    """读论文文件。统一走工具层 tools.read_file（支持 .txt/.md/.pdf）。"""
    try:
        return read_file(path)
    except ValueError as e:
        raise SystemExit(str(e))


def build_task_input(paper_text: str) -> str:
    """把"任务指令 + 论文"组装成一条 user 消息。

    指令部分来自 skills/paper2patent/references/input-requirements.md
    的推荐格式（【论文标题】【论文摘要】……）。论文段用统一的
    PAPER_BEGIN/PAPER_END 标记包住——L1 压缩层靠这对标记精确定位
    "哪些字符是论文"，压缩时绝不误伤任务指令。
    """
    return (
        "请将下面这篇论文转换为中国发明专利申请文件（五大部分，纯文本输出）。\n"
        f"{PAPER_BEGIN}\n{paper_text}\n{PAPER_END}"
    )


def print_compression_stats(agent: AgentLoop) -> None:
    """打印 L1 压缩统计：原始上下文 vs 实际发送，这是压缩率的实测依据。"""
    stats = getattr(agent.preparer, "last_stats", None)
    print(f"[上下文] 原始 {agent.store.total_chars()} 字符 | "
          f"实际发送 {agent.last_send_chars} 字符")
    if stats and stats["compressed"]:
        ratio = 1 - stats["chars_after"] / stats["chars_before"]
        print(f"[L1压缩] 论文段 {stats['sections']} 个，其中 {stats['compressed']} 个超预算"
              f"（预算 {stats['budget']} 字符），"
              f"{stats['chars_before']} → {stats['chars_after']} 字符，"
              f"压缩率 {ratio:.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description="论文转专利智能 agent（骨架版）")
    parser.add_argument("paper", nargs="?", help="论文文本文件路径（.txt / .md）")
    parser.add_argument("--mock", action="store_true",
                        help="使用假模型演示，不需要 API key")
    parser.add_argument("--chat", action="store_true",
                        help="进入交互多轮对话模式")
    parser.add_argument("--sections", action="store_true",
                        help="分节生成（摘要→权利要求书→说明书→附图各一轮，抗截断）")
    args = parser.parse_args()

    client = make_client(mock=args.mock)
    agent = AgentLoop(client)

    # ── 交互模式：一个真正的 while 主循环 ────────────────────────────
    # 每轮：读用户输入 → run_turn（写库→调模型→写回）→ 打印。
    # "写回消息库"的价值在这里能直观看到：第二轮提问时，模型能看到
    # 自己上一轮的回复（上下文连续）。
    if args.chat:
        print("交互模式已启动（输入 q 退出）。系统提示已加载。")
        # 带论文文件启动时，自动把"任务指令+论文全文"作为第一轮注入。
        # 论文一旦进入消息库，后续每一轮的发送副本都会带上它，
        # 用户就可以在同一会话里连续追问（比如核对发明名称）。
        if args.paper:
            paper_text = load_paper(args.paper)
            print(f"已载入论文 {args.paper}（{len(paper_text)} 字符），"
                  f"正在生成专利文本，请稍候……\n")
            reply = agent.run_turn(build_task_input(paper_text))
            print("agent>", reply)
            out = write_file(reply)  # 生成结果落盘，会话关掉也不丢
            print(f"\n[第1轮统计] finish_reason={agent.last_finish_reason} | "
                  f"消息库 {len(agent.store)} 条")
            print_compression_stats(agent)
            print(f"[工具层] 生成结果已落盘：{out}")
            print("\n论文已注入会话，可继续追问（输入 q 退出）。")
        while True:
            try:
                user_input = input("\n你> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input or user_input.lower() == "q":
                break
            # 发送前先做"本地文件解析"：聊天里的真实路径 → 文件全文。
            # 模型永远读不了磁盘，读盘这一步必须在主循环代码里完成。
            resolved = resolve_local_files(user_input)
            if resolved != user_input:
                print("[主循环] 已检测到本地文件并注入全文（模型本身无读盘能力）")
            print("\nagent>", agent.run_turn(resolved))
        print(f"\n会话结束。消息库共 {len(agent.store)} 条消息，"
              f"原始上下文 {agent.store.total_chars()} 字符。")
        return

    # ── 单轮模式：必须给论文文件 ────────────────────────────────────
    if not args.paper:
        raise SystemExit("用法：python main.py 论文.txt [--mock|--sections]  或  python main.py --chat")

    paper_text = load_paper(args.paper)
    print(f"已读取论文 {len(paper_text)} 字符，正在请求模型……")

    # ── 会话文件夹 + 断点续跑 ──────────────────────────────────────
    # 有未完成的会话（patent.pdf 还没生成出来）就复用该文件夹继续：
    # 节没写完的接着写，写完了的直接重试交付，绝不重复烧已付成本的
    # token；没有才开新文件夹。
    resumable = find_resumable_session(args.paper)
    if resumable is not None:
        session_dir, _ = resumable
        print(f"[断点] 发现未完成的会话，本次续跑：{session_dir}")
    else:
        session_dir = create_session_dir(args.paper)
        print(f"[会话] 本次产物目录：{session_dir}")
    checkpoint_path = session_dir / CHECKPOINT_NAME

    if args.sections:
        # 分节生成（生产路径）：4 次调用代替 1 次。
        # 是什么：程序拆成 4 轮——说明书摘要 → 权利要求书 → 说明书 →
        # 说明书附图，每轮一个任务，后一轮能看到前几轮的草稿。
        # 为什么需要：这是对抗输出截断的生成策略，和双保险是配合关系——
        # 一次写完 1 万多字的专利，很容易撞上 max_tokens 上限被拦腰截断；
        # 每次只写一节，输出短，截断概率大幅下降（长论文实测 4 轮全部
        # stop，零截断）。附带好处：后一节复用前一节的名称和术语，
        # 全文一致性更好。
        drafts = agent.generate_full_draft_sections(paper_text,
                                                    checkpoint_path)
        reply = "\n\n".join(f"━━━━━━━━ {name} ━━━━━━━━\n{text}"
                            for name, text in drafts.items())
        # ── 交付管线：结构化 JSON → 附图 → DOCX → PDF ─────────────────
        # 第 5 节任务让模型把前四节组装成 skill 的结构化契约，校验通过后
        # 落盘 JSON，再编排 skill 脚本出正式交付文件。整段用 try/except
        # 保护：文本交付（上面的 reply + .md 落盘）永远不因交付失败而报废。
        try:
            json_reply = agent.assemble_delivery_json(drafts)
            json_path = write_json(json.loads(json_reply), session_dir)
            print(f"[交付] 结构化 JSON 已落盘：{json_path}")
            produced = run_delivery_pipeline(json_path)
            print("[交付] 正式交付文件已生成：")
            for item in produced:
                print(f"  - {item}")
        except (ValueError, RuntimeError) as e:
            print(f"[交付] 交付管线失败（不影响以上文本结果）：{e}")
    else:
        reply = agent.run_turn(build_task_input(paper_text))


    print("\n━━━━━━━━━━ 模型输出 ━━━━━━━━━━\n")
    print(reply)
    out = write_file(reply, session_dir)  # 落盘到会话文件夹：patent.md
    print(f"\n[工具层] 生成结果已落盘：{out}")
    # 运行结果摘要：证明"写回"确实发生了，也是后续 L1 压缩效果的对照基线
    # （压缩后 send 副本会比这里的"原始上下文"小，差距就是压缩率）。
    print(f"\n━━━━━━━━━━ 会话统计 ━━━━━━━━━━")
    print(f"结束原因 finish_reason : {agent.last_finish_reason}"
          f"{'（被截断，双保险在步骤3处理）' if agent.last_finish_reason == 'length' else ''}")
    print(f"消息库消息条数         : {len(agent.store)}（system + user + assistant）")
    print_compression_stats(agent)


if __name__ == "__main__":
    # Windows 终端强制 UTF-8 输出，避免中文乱码
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
