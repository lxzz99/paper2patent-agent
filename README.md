# paper2patent-agent

论文自动转专利智能 Agent：基于自研 Harness 框架与端到端交付管线，从论文原文自动生成中国发明专利申请文件（DOCX / PDF）。

## 项目结构

- `agent/` —— Agent Harness 核心代码
  - `loop.py`：主循环（消息库 → 发送副本加工 → 调模型 → 写回）
  - `messages.py`：MessageStore 消息库（append-only 原始记录 + 深拷贝发送副本）
  - `preparers.py`：Preparer 洋葱链（L1 内容压缩 / L2 规范注入 / L3 格式翻译）
  - `model_client.py`：模型客户端（OpenAI 兼容接口，火山方舟 Ark）
  - `tools.py`：Function Calling 工具层
- `skills/paper2patent/` —— 论文转专利业务 Skill（源自上游项目，见下方来源声明）

## 架构亮点

- **消息库双轨架构**：消息库为 append-only 原始记录，有损操作只作用于深拷贝发送副本，源数据零污染
- **Preparer 洋葱链**：`inner=` 嵌套装配，执行顺序 L1 压缩 → L2 注入 → L3 投影，先压再注
- **双保险恢复**：输入侧上下文超限 → 收紧压缩预算重试；输出侧截断 → max_tokens 阶梯升级续写
- **工具循环 + 断点续跑 + 确定性交付校验**：分节生成落盘检查点，交付 JSON 经本地校验后导出 DOCX/PDF

## 来源声明

本仓库 `skills/paper2patent/` 目录、LICENSE 及业务规则文件源自 [7toCR/paper2patent](https://github.com/7toCR/paper2patent)（MIT License）；`agent/` 目录为本仓库新增的 Agent Harness 实现。详见 [NOTICE.md](NOTICE.md)。

## 快速开始

1. 配置 `.env`：填写 `ARK_API_KEY` 等连接参数（格式见 `agent/config.py`）
2. 运行：`python agent/main.py --help` 查看入口参数
3. 或将 `skills/paper2patent/` 复制到支持 Skills 的 AI 工具中复用业务工作流
