# paper2patent 论文转专利智能 Agent

把一篇本地学术论文（PDF/Markdown/TXT）自动转换成一套完整的中国发明专利申请文件，并产出可直接交付的 **Word / PDF** 文件。核心运行时（消息库、上下文加工链、双保险容错、工具执行层、交付管线）全部自研，不依赖任何 Agent 框架。

---

## 一、安装

要求 Python 3.10+，在 `agent/` 目录下执行：

```cmd
pip install openai pypdf python-docx pillow
```

| 依赖 | 用途 |
|---|---|
| openai | 调用大模型（火山方舟 OpenAI 兼容接口） |
| pypdf | 读取 PDF 论文的文字层 |
| python-docx | 生成专利 Word 文书 |
| pillow | 附图 SVG 转 PNG、PDF 兜底渲染 |

## 二、配置（密钥只放 .env，绝不写进代码）

在 `agent/` 目录下新建 `.env` 文件（已被 .gitignore 排除，不会提交到 git）：

```ini
# 火山方舟的 API Key（控制台获取）
ARK_API_KEY=ark-xxxx
# 模型名或推理接入点（推荐非强制思考型模型）
ARK_MODEL=doubao-seed-2.1-turbo
# 1 = 请求时关闭思考模式（仅对支持的模型生效，可省 token 防截断）
DISABLE_THINKING=1
# 端点：智能体套餐 key 用 /api/plan/v3；普通按量付费用 /api/v3
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3
```

也可以不建文件，改用环境变量 `ARK_API_KEY` / `ARK_MODEL`（优先级高于 .env）。cmd 里临时设置（只对当前窗口生效，关窗即失效）：

```cmd
set ARK_API_KEY=ark-xxxx
set ARK_MODEL=模型名或ep-xxx
```

## 三、跑通一个专利（两步）

以本机论文 `C:\Users\lixuze\Desktop\temp\test_p2p\Paper_sample.pdf` 为例，**全部命令在 `agent/` 目录下执行**。

### 第 1 步：一条命令生成专利

```cmd
python main.py C:\Users\lixuze\Desktop\temp\test_p2p\Paper_sample.pdf --sections
```

`--sections` 是推荐模式：摘要 → 权利要求书 → 说明书 → 附图分四轮生成（短输出零截断、术语前后一致），随后自动追加第 5 轮"交付 JSON"组装并运行交付管线。过程约 3~4 分钟，终端会依次打印每轮 token 用量与产物路径。

论文的读取、校验、压缩都由 agent 自己完成，用户只需给路径：PDF 必须是文字版（扫描图片版提取不出文字时，agent 会明确报错提示，绝不猜测内容）；Markdown/TXT 论文同样直接可用。

不加 `--sections` 则单轮生成纯文本专利（快速预览用，长文易截断）。

### 第 2 步：到 output 目录取交付文件

生成的文件全部落在 **`agent/output/`**（该目录已被 gitignore，不入 git）：

```
agent/output/
└── Paper_sample_20260917_011500/     ← 一次运行 = 一个文件夹（论文名_时间戳）
    ├── patent.md                     ← 四节专利全文（纯文本草稿）
    ├── patent.json                   ← 结构化契约 JSON（下游生成器的输入）
    ├── patent_drawings/              ← 说明书附图
    │   ├── patent_图1.svg
    │   ├── patent_图1.png
    │   └── ...
    ├── patent.docx                   ← ★ 专利 Word 文书（附图已内嵌）
    └── patent.pdf                    ← ★ 专利 PDF 文书
```

文件夹名 = 论文名 + 生成时刻时间戳，一篇论文的全部产物集中一个文件夹，多批生成互不混淆；以终端打印的 `[会话] 本次产物目录` 和 `[交付] 正式交付文件已生成` 列表为准。

## 四、其他用法

```cmd
# 多轮对话模式：生成专利后可继续追问（如"发明名称是什么""第3条权利要求依据论文哪段"）
python main.py C:\论文.md --chat

# 无 key 演示：MockClient 假模型跑通全流程（不联网不花钱）
# 注意：假模型不会产出合法交付 JSON，--mock 下 --sections 只演示分节生成，
# 不含 DOCX/PDF 交付管线（交付管线需真实模型）
python main.py demo.md --mock
python main.py demo.md --sections --mock

# 单元测试（无需 key）
python test_function_calling.py
python test_delivery.py
```

## 五、可选升级：排版版 PDF

默认 PDF 用 Pillow 渲染图片版兜底（无 LibreOffice 也能出）。安装 LibreOffice 后**同一命令**自动升级为排版版 PDF（文字可选中、可检索），代码无需任何改动——安装后确认 `soffice` 在 PATH 里即可。

## 六、常见问题

| 现象 | 原因与处理 |
|---|---|
| PDF 报"提取不出文字" | 扫描版无文字层，先用 OCR 工具转成文字版 |
| 429 SetLimitExceeded | 额度暂停，到方舟控制台调整"安全体验模式"或等限额重置 |
| 429 RequestBurstTooFast | 突发限流：程序自动等待 10s/20s/30s 重试；若多次重试仍失败，等几分钟后**直接重跑同一命令**——已生成的节从断点恢复，不会重复消耗 |
| 生成被截断 | 确认加了 `--sections`；思考型模型请在 .env 设 `DISABLE_THINKING=1` |
| `[交付] 交付管线失败` | 文本结果不受影响照常落盘；多为附图描述格式问题，重跑一次即可 |
| 中文乱码 | 程序已强制 UTF-8 输出；个别情况下先执行 `chcp 65001` 切到 UTF-8 代码页再运行 |
