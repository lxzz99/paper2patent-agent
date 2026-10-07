# -*- coding: utf-8 -*-
"""工具层：harness 提供给 agent 的"手脚"。

为什么需要这一层（对应 Claude Code / OneCode 的核心概念）：
  模型只有"生成文本"一种能力，读盘、写盘、跑脚本全是 harness 的活。
  skill（paper2patent）假设宿主 harness 能读 PDF、能写文件——
  Claude Code 满足，我们自己的骨架 harness 不满足，所以在本文件补齐。
  命名与结构参考 OneCode 的 tools/read_file 等。

本文件的职责分四块：
  1. 基础工具（主循环按固定流程调用，模型不参与决策——方案A）：
       read_file(path)          读本地文件，支持 .txt/.md 和 PDF（pypdf 提取）
       write_file(content)      把生成的专利文本落盘到 output/ 目录
       resolve_local_files(...) 扫描聊天输入里的真实路径并注入全文
  2. function calling（方案B）：TOOL_SCHEMAS 把 read_file/write_file 的
       schema 随请求发给模型自选；execute_tool 统一执行，错误回传不抛
       （错误也是工具结果，模型下一轮自己修正）；write_tool_file 是
       面向模型的受限写盘（防目录穿越，模型参数不可信）。
  3. 会话管理：create_session_dir（一次生成一个文件夹）、
       find_resumable_session（断点续跑判据：patent.pdf 不存在=未完成，
       而不是节数不满——4 节写完后交付被打断也要能只续交付）。
  4. 交付管线编排：run_delivery_pipeline 依次跑 skill 的三个生成器
       脚本（附图 SVG/PNG → DOCX → PDF）——skill 是 normative source，
       agent 只做编排，不重复实现文档生成逻辑。
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from preparers import PAPER_BEGIN, PAPER_END  # 论文段标记，供 L1 压缩层定位

# ── 支持的文件类型 ────────────────────────────────────────────────────
# 论文常见载体就是这三类。PDF 用 pypdf 提取文字层（扫描版 PDF 没有
# 文字层，提取出来是空的——届时报错提示用户换文字版，绝不猜内容）。
TEXT_EXTS = {".txt", ".md", ".markdown"}
PDF_EXTS = {".pdf"}

# 输出目录：生成的专利落盘在这里（已被 .gitignore 排除，不入 git）。
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def read_file(path: str) -> str:
    """读一个本地文件，返回纯文本。读不了就抛 ValueError（带人话原因）。

    统一按 UTF-8 读文本文件（Windows 下必须显式指定编码）；
    PDF 逐页提取文字后拼接，页与页之间换行分隔。
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"文件不存在：{p.resolve()}")
    if not p.is_file():
        raise ValueError(f"不是文件（可能是目录）：{p.resolve()}")

    ext = p.suffix.lower()
    if ext in TEXT_EXTS:
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(f"文本文件不是 UTF-8 编码，请先转码：{p}（{e}）") from e

    if ext in PDF_EXTS:
        return _read_pdf(p)

    raise ValueError(
        f"暂不支持的文件类型 {ext}（当前支持 .txt/.md/.pdf）：{p}"
    )


def _read_pdf(p: Path) -> str:
    """用 pypdf 提取 PDF 的文字层。"""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ValueError(
            "读取 PDF 需要安装 pypdf：pip install pypdf"
        ) from e

    pages = PdfReader(str(p)).pages
    texts = []
    for i, page in enumerate(pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            texts.append(text)
    if not texts:
        raise ValueError(
            f"该 PDF 提取不出文字（多半是扫描图片版，需要 OCR 或换文字版）：{p}"
        )
    return "\n\n".join(texts)


# 断点检查点文件名（会话文件夹内）
CHECKPOINT_NAME = "drafts_checkpoint.json"


def _sanitize_stem(paper_name: str) -> str:
    """论文名 → 合法的文件夹名片段（去路径、替换 Windows 非法字符）。"""
    return re.sub(r'[\\/:*?"<>|]', "_", Path(paper_name).stem).strip() or "paper"


def create_session_dir(paper_name: str = "paper") -> Path:
    """为一次专利生成会话创建独立文件夹：output/{论文名}_{时间戳}/。

    一篇论文的全部产物（md/json/附图/docx/pdf）都收进同一个文件夹，
    多篇论文互不混淆；时间戳只出现在文件夹名上，内部文件名固定
    （patent.md / patent.json / patent.docx / patent.pdf），不用再猜
    哪几个文件是同一批生成的。
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session = OUTPUT_DIR / f"{_sanitize_stem(paper_name)}_{ts}"
    session.mkdir(parents=True, exist_ok=True)
    return session


def find_resumable_session(paper_name: str) -> tuple[Path, dict] | None:
    """找最近一个未完成的会话文件夹，支持断点续跑。

    "未完成"的判据是**最终交付物 patent.pdf 还不存在**——而不是节数
    不满：生成全部 4 节后交付管线仍可能被限流打断，此时重跑应该
    跳过全部生成、直接从交付续起，绝不能开新会话重新烧 4 节。
    命中返回 (会话文件夹, 已完成的 drafts)；全不命中返回 None。
    """
    candidates = sorted(
        OUTPUT_DIR.glob(f"{_sanitize_stem(paper_name)}_*/{CHECKPOINT_NAME}"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    for cp in candidates:
        session = cp.parent
        if (session / "patent.pdf").exists():
            continue  # 已交付完成，不算未完成
        try:
            drafts = json.loads(cp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(drafts, dict) and drafts:
            return session, drafts
    return None


def write_file(content: str, out_dir: Path | None = None) -> Path:
    """把生成结果落盘，返回文件路径。

    out_dir 给定 → 会话文件夹模式，写 out_dir/patent.md；
    out_dir 缺省 → 旧行为，写 output/patent_{时间戳}.md（--chat 等单文件场景）。
    """
    if out_dir is not None:
        out = out_dir / "patent.md"
    else:
        OUTPUT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = OUTPUT_DIR / f"patent_{ts}.md"
    out.write_text(content, encoding="utf-8")
    return out


# ── 方案B：function calling 的工具 schema 与统一执行器 ────────────────
# schema 用 OpenAI 的标准 JSON 格式随请求发给模型；"description" 是模型
# 决定"要不要调、怎么调"的唯一依据，必须把适用场景和约束写清楚。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取本地文件并返回全文。支持 .txt/.md/.markdown 纯文本"
                           "和 .pdf（提取文字层）。当用户提到本地文件路径且你还没有"
                           "看到该文件内容时调用它。路径必须是真实存在的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "文件的相对或绝对路径，如 ../test/demo_paper.md"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把文本内容保存为新文件，只允许写入 output 目录。"
                           "【仅在用户明确要求把内容保存成文件时才调用；撰写正文"
                           "（如专利全文）时直接在回复中输出文本即可，不要调用"
                           "本工具。】返回实际写入的文件路径。filename 只能是"
                           "文件名（可含一层子目录名），不接受绝对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                    "filename": {"type": "string",
                                 "description": "保存的文件名（如 patent_draft.md），"
                                                "省略时自动按时间戳命名"},
                },
                "required": ["content"],
            },
        },
    },
]


def write_tool_file(content: str, filename: str = "") -> Path:
    """execute_tool 专用的受限写盘：只允许落在 OUTPUT_DIR 目录内。

    安全边界（模型生成的参数不可信，必须校验）：
      - filename 含 ".."、盘符或绝对路径 → 拒绝；
      - 解析后用 relative_to 二次确认确实在 OUTPUT_DIR 里（双保险）。
    这与 main.py 直接调用 write_file 不同——那条路是 harness 自己写的
    固定文件名，不需要防；这条路的文件名来自模型输出，必须防目录穿越。
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    if not filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = OUTPUT_DIR / f"patent_{ts}.md"
    else:
        candidate = Path(filename)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise ValueError(f"filename 只能是 output 目录内的相对文件名，拒绝：{filename}")
        target = (OUTPUT_DIR / candidate).resolve()
        try:
            target.relative_to(OUTPUT_DIR.resolve())
        except ValueError as e:
            raise ValueError(f"写入位置越出 output 目录，拒绝：{filename}") from e
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def execute_tool(name: str, arguments: dict) -> str:
    """执行模型请求的工具调用，把结果作为字符串返回（永远不向上抛异常）。

    为什么吞异常：工具失败（文件不存在、参数缺失、路径非法）是模型的
    可恢复错误——把"错误原因"作为文本回传，模型下一轮会自己修正参数
    重试；如果把异常抛到主循环，整轮对话就断了。这对应 OneCode
    executor 的"错误也是工具结果"设计。
    """
    try:
        if "_raw" in arguments:
            # 参数不是合法 JSON（model_client 解析失败时保留原文）。
            # 必须给出"下一步怎么办"，只说"参数错了"模型只会原样重试。
            return ("错误：工具参数不是合法 JSON（常见原因：内容太长或引号转义出错）。"
                    "不要用相同参数重试：长正文请直接在回复中输出文本；"
                    "确需保存时请缩短内容或分多次调用。")
        if name == "read_file":
            path = arguments.get("path")
            if not path:
                return "错误：缺少必填参数 path"
            return read_file(path)
        if name == "write_file":
            content = arguments.get("content")
            if content is None:
                return "错误：缺少必填参数 content"
            return f"已写入文件：{write_tool_file(content, arguments.get('filename', ''))}"
        return f"错误：未知工具 {name}（可用工具：read_file、write_file）"
    except Exception as e:  # noqa: BLE001 —— 错误信息回传给模型，让它自行恢复
        return f"工具执行失败：{type(e).__name__}: {e}"


# ── 交付管线（结构化 JSON → 附图 → DOCX → PDF）────────────────────────
# 调用的是 skills/paper2patent/scripts/ 下的正式生成器——skill 是现成的
# normative source，agent 只做"编排"，不重复实现文档生成逻辑。
# 三个脚本已核对过 argparse 签名：
#   generate_patent_drawings.py  input -o output-dir --prefix --update-json
#   generate_patent_docx.py      input -o output
#   export_patent_pdf.py         docx  -o output --content-json
# PDF 走 Route A（无 LibreOffice 时用 Pillow 渲染图片版 PDF 兜底）。
SKILL_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "paper2patent" / "scripts"

_REQUIRED_SCRIPTS = ("generate_patent_drawings.py", "generate_patent_docx.py",
                     "export_patent_pdf.py")


def write_json(data: dict, out_dir: Path) -> Path:
    """把交付 JSON 落盘到会话文件夹 out_dir/patent.json，返回文件路径。

    ensure_ascii=False：中文原样写入（skill 的生成器按 UTF-8 读）；
    indent=2：落盘的是交付物，人要能直接打开检查。
    下游 run_delivery_pipeline 以 JSON 所在目录为基准解析附图与产物，
    所以 JSON 必须与会话文件夹里的其他产物同目录。
    """
    out = out_dir / "patent.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _run_script(script: str, *args: str) -> None:
    """跑一个 skill 脚本（同进程的 Python），非零退出抛 RuntimeError。"""
    cmd = [sys.executable, str(SKILL_SCRIPTS_DIR / script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise RuntimeError(f"{script} 退出码 {proc.returncode}：\n{tail}")


def run_delivery_pipeline(json_path: Path) -> list[Path]:
    """编排三步交付管线，返回生成的文件路径列表 [附图目录, DOCX, PDF]。

    步骤① 附图：从 JSON 的 drawings 生成 SVG/PNG，--update-json 把
       drawing_assets（相对 JSON 父目录的路径）写回 JSON——
    步骤② DOCX：generate_patent_docx.py 按同样的 base_dir（JSON 父目录）
       解析这些相对路径并嵌入图片，两步必须共用同一基准目录；
    步骤③ PDF：优先找 LibreOffice/soffice 转换排版版；找不到就用
       --content-json 走 Pillow 图片版兜底（Route A）。

    JSON 必须先通过 _validate_patent_json（loop 层负责），这里只编排。
    """
    json_path = json_path.resolve()
    for script in _REQUIRED_SCRIPTS:
        if not (SKILL_SCRIPTS_DIR / script).exists():
            raise ValueError(
                f"找不到 skill 脚本：{SKILL_SCRIPTS_DIR / script}。"
                "交付管线依赖 skills/paper2patent/，请确认目录完整。")
    stem = json_path.stem

    drawings_dir = json_path.parent / f"{stem}_drawings"
    _run_script("generate_patent_drawings.py", str(json_path),
                "-o", str(drawings_dir), "--prefix", stem, "--update-json")
    docx_path = json_path.parent / f"{stem}.docx"
    _run_script("generate_patent_docx.py", str(json_path), "-o", str(docx_path))
    pdf_path = json_path.parent / f"{stem}.pdf"
    _run_script("export_patent_pdf.py", str(docx_path), "-o", str(pdf_path),
                "--content-json", str(json_path))
    return [drawings_dir, docx_path, pdf_path]


# ── 聊天输入里的路径解析（原 main.py 的 resolve_local_files 升级版）────
# 从输入文本里切"词"的正则：路径、文件名都不会包含空白和中英文标点，
# 所以把空白和常见标点全部排除后剩下的连续片段，就是候选 token。
# 注意：故意不包含冒号——"论文路径如下："的冒号不会粘进路径里。
_TOKEN_RE = re.compile(r"[^\s\"'，。；、！？（）()：:【】\[\]]+")


def resolve_local_files(user_input: str) -> str:
    """把输入里出现的【真实存在且可读】的本地文件路径，替换为文件全文。

    策略（刻意保守，避免误伤普通句子）：
      - 只认 token 化后确实存在、且 read_file 能读出内容的路径；
      - 读不了（类型不支持/扫描版PDF/编码异常）就保持原样，
        让模型自己像往常一样回复"请粘贴内容"；
      - 路径在原句中的位置不变，替换后上下文依然通顺。
    """
    parts: list[str] = []
    last = 0
    replaced = False
    for m in _TOKEN_RE.finditer(user_input):
        token = m.group(0)
        p = Path(token)
        if not (p.exists() and p.is_file()):
            continue
        try:
            content = read_file(token)
        except ValueError:
            continue  # 读不出内容的不替换，保持原样
        parts.append(user_input[last:m.start()])
        parts.append(f"\n{PAPER_BEGIN}\n以下为文件 {token} 的全文：\n{content}\n{PAPER_END}\n")
        last = m.end()
        replaced = True
    if not replaced:
        return user_input
    parts.append(user_input[last:])
    return "".join(parts)
