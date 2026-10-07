# -*- coding: utf-8 -*-
"""Flask 网页界面：给骨架加一张"脸"，方便演示和录屏。

刻意保持最小：一个页面、一个表单、一个按钮。核心逻辑（消息库、主循环、
模型客户端）一行不重复——网页只是把"读文件 + 打印"换成了"填表单 + 渲染"，
真正的转换仍然调用和 CLI 完全相同的 AgentLoop.run_turn()。

启动方式（agent/ 目录下）：

    python webapp.py            # 真实模型（需要 ARK_API_KEY / ARK_MODEL）
    python webapp.py --mock     # 假模型演示（无 key，浏览器打开 http://127.0.0.1:5000）

每个请求新建一个 AgentLoop 实例（一次请求 = 一次完整会话），
骨架阶段不做跨请求的会话保持——多轮能力在 CLI 的 --chat 里已经验证，
网页侧等 L1/L2 上来之后再考虑加"继续追问"。
"""

import argparse
import sys

from flask import Flask, request, render_template_string

from loop import AgentLoop
from model_client import make_client

app = Flask(__name__)

# 页面模板用 render_template_string 内联渲染，省掉 templates/ 目录，
# 让"界面层"物理上只有这一个文件。样式只保留最基础的可读性。
PAGE = """
<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <title>paper2patent · 论文转专利</title>
  <style>
    body   { font-family: "Microsoft YaHei", sans-serif; max-width: 900px;
             margin: 2rem auto; padding: 0 1rem; color: #222; }
    h1     { font-size: 1.4rem; }
    textarea { width: 100%; height: 260px; font-size: 14px;
               padding: .6rem; box-sizing: border-box; }
    button { margin-top: .8rem; padding: .5rem 1.6rem; font-size: 15px; }
    .result { white-space: pre-wrap; background: #f7f7f7; border: 1px solid #ddd;
              padding: 1rem; margin-top: 1rem; line-height: 1.7; }
    .meta  { color: #777; font-size: 13px; margin-top: .6rem; }
  </style>
</head>
<body>
  <h1>paper2patent · 论文转专利（骨架版）</h1>
  <p>把论文全文粘贴到下面的输入框，点"生成专利文本"。</p>
  <form method="post">
    <textarea name="paper" placeholder="在此粘贴论文全文（或论文的核心方法章节）…">{{ paper }}</textarea>
    <button type="submit">生成专利文本</button>
  </form>
  {% if reply %}
    <div class="result">{{ reply }}</div>
    <div class="meta">finish_reason={{ finish_reason }} · 消息库 {{ msg_count }} 条 ·
         原始上下文 {{ total_chars }} 字符</div>
  {% endif %}
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    """唯一路由：GET 显示空白表单，POST 取论文 → 跑一轮 → 带结果重新渲染。

    注意 mock 客户端在应用启动时就定死（见下面的 main），
    所以每次请求只是新建 AgentLoop，客户端复用。
    """
    paper = request.form.get("paper", "").strip()
    reply = finish = None

    if request.method == "POST" and paper:
        agent = AgentLoop(app.config["CLIENT"])
        reply = agent.run_turn(paper)          # 和 CLI 完全同一个主循环
        finish = agent.last_finish_reason
        return render_template_string(
            PAGE, paper=paper, reply=reply, finish_reason=finish,
            msg_count=len(agent.store), total_chars=agent.store.total_chars(),
        )

    return render_template_string(PAGE, paper=paper, reply=None,
                                  finish_reason=None, msg_count=0, total_chars=0)


def main() -> None:
    parser = argparse.ArgumentParser(description="paper2patent 网页版")
    parser.add_argument("--mock", action="store_true", help="使用假模型（无 key 演示）")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    app.config["CLIENT"] = make_client(mock=args.mock)  # 客户端全局唯一
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
