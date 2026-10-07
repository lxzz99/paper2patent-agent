# -*- coding: utf-8 -*-
"""交付管线（最后一公里）的单测：全部不联网、不烧 token。

覆盖四块：
  1. _strip_code_fence        模型偶尔包裹 JSON 的 ``` 围栏要能剥掉
  2. _validate_patent_json    校验器：合法通过 / 各类缺陷给出人话错误
  3. assemble_delivery_json   mock 剧本驱动：坏 JSON → 回喂错误修正；
                              三次都坏则放弃（尝试机制 + 错误汇总回喂）
  4. run_delivery_pipeline    用最小合法 JSON 真跑 skill 三脚本
                              （无 API：附图 SVG/PNG → DOCX → PDF 兜底）

用法：python test_delivery.py   （通过打 OK，断言失败抛 AssertionError）
"""

import json
import tempfile
from pathlib import Path

from loop import (_strip_code_fence, _validate_patent_json,
                  _PATENT_JSON_REQUIRED, _DESCRIPTION_REQUIRED)
from tools import (run_delivery_pipeline, create_session_dir, write_json,
                   write_file, OUTPUT_DIR, CHECKPOINT_NAME,
                   find_resumable_session)


# ── 最小合法交付 JSON（契约必填字段一个不少）────────────────────────────

def _minimal_json() -> dict:
    return {
        "invention_name": "一种测试方法",
        "source_title": "测试论文",
        "abstract": "本发明公开一种测试方法，属于测试领域，解决了测试问题。",
        "abstract_drawing": "建议选取说明书附图中的图1作为摘要附图。",
        "claims": ["1.一种测试方法，其特征在于：包括步骤S101。"],
        "description": {
            "technical_field": "本发明涉及测试领域。",
            "background": "现有技术存在测试问题。",
            "invention_content": "本发明提供一种测试方法。",
            "drawing_description": ["图1为本发明的流程图。"],
            "embodiments": "如图1所示，执行步骤S101。",
        },
        # 步骤条目写法与 skill 的 STEP_RE 对齐：S编号+名称，用分号分隔，
        # 至少 2 条（require_items 要求 >= 2），并含"流程"二字触发方法流程图
        "drawings": ["图1：测试方法流程图，包含步骤S101获取测试数据；"
                     "S102执行测试；S103输出测试结果。"],
    }


def test_strip_code_fence():
    raw = json.dumps(_minimal_json(), ensure_ascii=False)
    # 无围栏原样返回；带 ```json 围栏、带解释文字都剥干净
    assert _strip_code_fence(raw) == raw
    assert _strip_code_fence(f"```json\n{raw}\n```") == raw
    assert _strip_code_fence(f"```\n{raw}\n```") == raw
    assert json.loads(_strip_code_fence(f"```JSON\n{raw}\n```"))["invention_name"]
    print("OK  _strip_code_fence：围栏剥离 4 例")


def test_validate_patent_json():
    good = json.dumps(_minimal_json(), ensure_ascii=False)
    data, err = _validate_patent_json(good)
    assert data is not None and err == ""

    # 顶层不是 JSON 对象
    data, err = _validate_patent_json("[]")
    assert data is None and "对象" in err
    # 语法错误
    data, err = _validate_patent_json("{bad json")
    assert data is None and "JSON" in err
    # 缺必填字段
    broken = _minimal_json(); broken.pop("claims")
    data, err = _validate_patent_json(json.dumps(broken, ensure_ascii=False))
    assert data is None and "claims" in err
    # claims 空数组
    broken = _minimal_json(); broken["claims"] = []
    data, err = _validate_patent_json(json.dumps(broken, ensure_ascii=False))
    assert data is None and "非空" in err
    # description 缺字段
    broken = _minimal_json(); broken["description"].pop("embodiments")
    data, err = _validate_patent_json(json.dumps(broken, ensure_ascii=False))
    assert data is None and "embodiments" in err
    # drawings 叙事体画面描述 → 判为不可解析（对齐 skill 脚本的解析口径）
    broken = _minimal_json()
    broken["drawings"] = ["图1为方法的流程示意图，画面中有三个矩形流程框依次排列。"]
    data, err = _validate_patent_json(json.dumps(broken, ensure_ascii=False))
    assert data is None and "S101" in err and "模块" in err, err
    # drawings 用紧凑步骤条目 → 通过
    broken["drawings"] = ["图1：测试方法流程图，包含步骤S101获取数据；S102处理；S103输出。"]
    data, err = _validate_patent_json(json.dumps(broken, ensure_ascii=False))
    assert data is not None, err
    # 模型偷带脚本拥有的字段（drawing_assets 等）→ 拒收（脚本的
    # infer_assets 发现它存在会优先采用并忽略 drawings，必然炸）
    broken = _minimal_json()
    broken["drawing_assets"] = [{"figure_no": 1, "title": "图1"}]
    data, err = _validate_patent_json(json.dumps(broken, ensure_ascii=False))
    assert data is None and "drawing_assets" in err
    # 图号一致性：drawings 与 drawing_description 图号集合不一致 → 拦下
    # （真实运行实证：模型改写 drawings 格式后漏改附图说明，交付物打架）
    broken = _minimal_json()
    broken["drawings"] = [
        "图1：测试方法流程图，包含步骤S101获取数据；S102处理；S103输出。",
        "图2：测试系统结构框图，包含数据模块101、处理模块102。",
    ]
    data, err = _validate_patent_json(json.dumps(broken, ensure_ascii=False))
    assert data is None and "图2" in err and "一一对应" in err, err
    # 错误汇总：claims 与 drawings 同时坏 → 一条报错里两个问题都在
    broken = _minimal_json()
    broken.pop("claims")
    broken["drawings"] = ["图1为方法的画面描述，有几个矩形框。"]
    data, err = _validate_patent_json(json.dumps(broken, ensure_ascii=False))
    assert data is None and "claims" in err and "S101" in err, err
    print("OK  _validate_patent_json：合法 1 例 + 缺陷 9 例（含图号一致性/错误汇总）")


def test_assemble_delivery_json_retry():
    """剧本：第一次输出坏 JSON（缺 claims），第二次输出合法 JSON。

    断言：① 模型被调了 2 次（2 次尝试机制）；② 第二次的任务要求里
    带上了第一次的错误原因（回喂修正）；③ 返回值是合法 JSON。
    """
    from loop import AgentLoop, SectionProjector
    from model_client import MockClient

    bad = json.dumps({k: v for k, v in _minimal_json().items()
                      if k != "claims"}, ensure_ascii=False)
    good = json.dumps(_minimal_json(), ensure_ascii=False)
    client = MockClient(script=[
        {"text": bad},   # 第一次：缺 claims → 校验失败
        {"text": good},  # 第二次：修正
    ])
    agent = AgentLoop(client)
    result = agent.assemble_delivery_json({"说明书摘要": "草稿"})
    assert json.loads(result)["claims"], "应返回合法 JSON"
    assert client.call_count == 2, f"期望 2 次尝试，实际 {client.call_count}"

    # 检查第二次任务确实回喂了错误原因（在消息库的 user 消息里找）
    second_task = next(m["content"] for m in agent.store._messages
                       if m["role"] == "user" and m["content"].startswith(
                           SectionProjector.TASK_MARK)
                       and "未通过校验" in m["content"])
    assert "claims" in second_task, "回喂内容应包含第一次的错误字段名"
    print("OK  assemble_delivery_json：坏→回喂→好，2 次尝试机制生效")


def test_assemble_delivery_json_gives_up():
    """三次都输出坏 JSON → 抛 ValueError，绝不把坏货交给下游。"""
    from loop import AgentLoop
    from model_client import MockClient

    bad = json.dumps({"invention_name": "不完整"}, ensure_ascii=False)
    client = MockClient(script=[{"text": bad}] * 3)
    agent = AgentLoop(client)
    try:
        agent.assemble_delivery_json({"说明书摘要": "草稿"})
    except ValueError as e:
        assert "三次校验均失败" in str(e)
    else:
        raise AssertionError("三次都坏却没抛 ValueError")
    assert client.call_count == 3
    print("OK  assemble_delivery_json：三次失败正确放弃（ValueError）")


def test_run_delivery_pipeline_smoke():
    """最小合法 JSON 真跑三步 skill 脚本：附图 → DOCX → PDF（不联网）。"""
    with tempfile.TemporaryDirectory() as td:
        json_path = Path(td) / "patent_test.json"
        json_path.write_text(json.dumps(_minimal_json(), ensure_ascii=False),
                             encoding="utf-8")
        produced = run_delivery_pipeline(json_path)
        drawings_dir, docx_path, pdf_path = produced
        assert drawings_dir.is_dir() and any(drawings_dir.iterdir()), "附图目录为空"
        assert docx_path.exists() and docx_path.stat().st_size > 0, "DOCX 未生成"
        assert pdf_path.exists() and pdf_path.stat().st_size > 0, "PDF 未生成"
        # --update-json 应把 drawing_assets 写回 JSON
        updated = json.loads(json_path.read_text(encoding="utf-8"))
        assert updated.get("drawing_assets"), "drawing_assets 未写回 JSON"
        for name in ("patent_test.json", "patent_test.docx", "patent_test.pdf"):
            print(f"    产物已生成：{name}")


def test_session_dir():
    """会话文件夹：非法字符清洗 + patent.* 固定命名 + 产物集中落盘。"""
    import shutil
    # 名称清洗：Windows 非法字符替换为下划线
    session = create_session_dir('a<b>c:d"e.pdf')
    name = session.name
    assert all(ch not in name for ch in ':*?"<>|'), name
    assert session.exists() and session.parent == OUTPUT_DIR
    # 产物集中：md/json 固定命名落在会话文件夹内
    assert write_file("草稿文本", session) == session / "patent.md"
    assert write_json(_minimal_json(), session) == session / "patent.json"
    assert (session / "patent.md").exists() and (session / "patent.json").exists()
    shutil.rmtree(session)
    print("OK  create_session_dir：非法字符清洗 + patent.* 固定命名")


def test_checkpoint_resume():
    """断点续跑：检查点恢复已完成节、只补缺的节、每节落盘检查点。"""
    from loop import DEFAULT_SECTIONS, AgentLoop
    from model_client import MockClient

    client = MockClient(script=[{"text": f"补写的第{k}节"} for k in range(3)])
    agent = AgentLoop(client)
    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / CHECKPOINT_NAME
        # 预置检查点：摘要与权利要求书已完成（模拟限流中断后的现场）
        cp.write_text(json.dumps({"说明书摘要": "已有摘要",
                                  "权利要求书": "已有权项"},
                                 ensure_ascii=False), encoding="utf-8")
        drafts = agent.generate_full_draft_sections("测试论文", cp)
        assert drafts["说明书摘要"] == "已有摘要", "已完成节应从检查点恢复"
        assert drafts["说明书附图"] == "补写的第1节"
        assert client.call_count == 2, f"只应补 2 节，实际调用 {client.call_count} 次"
        assert len(json.loads(cp.read_text(encoding="utf-8"))) == 4
    print("OK  断点续跑：恢复 2 节 + 只补 2 节 + 检查点写满 4 节")


def test_find_resumable_session():
    """断点查找：patent.pdf 缺失即未完成（命中复用），已交付/无检查点不命中。"""
    import shutil
    # 未完成（2 节，无 patent.pdf）→ 命中
    session = create_session_dir('a<b>c:d"e.pdf')
    (session / CHECKPOINT_NAME).write_text(
        json.dumps({"说明书摘要": "x"}, ensure_ascii=False), encoding="utf-8")
    hit = find_resumable_session('a<b>c:d"e.pdf')
    assert hit is not None and hit[0] == session
    # 检查点写满 4 节但没有 patent.pdf → 仍命中（交付中断场景，只续交付）
    (session / CHECKPOINT_NAME).write_text(
        json.dumps({f"节{k}": "x" for k in range(4)}, ensure_ascii=False),
        encoding="utf-8")
    hit = find_resumable_session('a<b>c:d"e.pdf')
    assert hit is not None and hit[0] == session
    # 已交付（patent.pdf 存在）→ 不命中
    (session / "patent.pdf").write_bytes(b"%PDF-1.4 fake")
    assert find_resumable_session('a<b>c:d"e.pdf') is None
    shutil.rmtree(session)
    print("OK  断点查找：未交付命中复用（含满 4 节交付中断场景），已交付不误命中")


if __name__ == "__main__":
    test_strip_code_fence()
    test_validate_patent_json()
    test_assemble_delivery_json_retry()
    test_assemble_delivery_json_gives_up()
    test_run_delivery_pipeline_smoke()
    test_session_dir()
    test_checkpoint_resume()
    test_find_resumable_session()
    print("\n交付管线单测全部通过")
