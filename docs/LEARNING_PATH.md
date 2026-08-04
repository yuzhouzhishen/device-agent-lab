# 学习路径：examples/01-07

`examples/` 目录保留了构建正式项目前的概念拆解材料。每个文件只演示一个
概念，可以独立运行。正式项目入口是 `device-agent` 和
`device_agent_lab.api:app`，不再从这些 Demo 继续堆功能。

建议顺序：01 → 02 → 03 → 04 → 05 → 06A-D → 06 → 07。

## 01 最小 Agent

模型、工具、提示词三件套。

```bash
python examples/01_basic_agent.py
```

没有配置 `GEMINI_API_KEY` 时会跳过真实模型调用。

## 02 上下文与结构化输出

把"聊天回复"变成"业务代码可以校验和执行的动作"。

```bash
python examples/02_context_and_schema.py
```

Agent 输出结构化动作而不是自然语言：

```json
{
  "intent": "control_device",
  "action": "set_port_power",
  "device_id": "DEMO-CP02-001",
  "port": 2,
  "enabled": true,
  "need_confirmation": true,
  "reason": "用户请求打开设备的2号端口。"
}
```

相关文件：`src/device_agent_lab/agent_contracts.py`。

## 03 结构化动作执行

把 `DeviceCommand` 交给 mock 设备执行。

```bash
python examples/03_execute_device_command.py
```

```text
用户自然语言 -> DeviceCommand -> 校验 -> 控制类等待确认 -> 调用 mock 设备
```

相关文件：`command_executor.py`、`mock_device.py`。

## 04 短期记忆

同一个 `thread_id` 下，Agent 能看到上一轮对话，理解"把它打开"中的"它"。

```bash
python examples/04_short_term_memory.py
```

依赖 `langgraph.checkpoint.memory.InMemorySaver`。

## 05 MCP 工具调用

Agent 不再直接调用 `mock_device.py`，改为通过本地 MCP server 发现并调用工具。

```bash
python examples/05_mcp_tools_agent.py
```

```text
启动本地 MCP server 子进程
-> MultiServerMCPClient 发现 tools
-> Agent 接收 MCP tools 并选择 device_set_port_power
-> MCP server 调用 mock 设备
```

相关文件：`mcp_client_config.py`、`mcp_server.py`。

## 06A-D LangGraph 基础

第一次学 LangGraph 不要直接进完整工作流。这四个示例每个只增加一个概念，
都不需要 API Key。

| 文件 | 新增概念 | 结构 |
| --- | --- | --- |
| `06a_minimal_graph.py` | State / Node / Edge / START / END | `START -> add_one -> END` |
| `06b_conditional_routing.py` | `add_conditional_edges()` | 按 State 进入查询或控制分支 |
| `06c_shared_state.py` | 局部更新、`Annotated[..., operator.add]` 累加 trace | `normalize -> query -> summarize` |
| `06d_interrupt_resume.py` | `InMemorySaver`、`thread_id`、`interrupt()`、`Command(resume=...)` | 暂停与恢复 |

学习要求：先能解释当前文件的 State、每个节点的输入输出和连线，再进入下一个。

## 06 设备控制综合 Demo

把条件分支、共享状态、人工确认和 `DeviceCommand`、mock 设备组合起来。

```bash
python examples/06_langgraph_workflow.py
```

三条分支：

```text
查询：validate -> query -> summarize
控制：validate -> control -> interrupt -> summarize
澄清：validate -> clarify -> summarize
```

相关文件：`device_workflow.py`、`tests/test_device_workflow.py`。

## 07 Agent + LangGraph 整合

Gemini 把自然语言转成 `DeviceCommand`，LangGraph 负责校验、路由、确认和执行。

```bash
python examples/07_agent_to_langgraph.py
DEVICE_REQUEST="查询2号端口状态" python examples/07_agent_to_langgraph.py
```

默认请求是"打开 2 号端口"，运行到控制节点会在终端询问确认，输入 `y` 才会
修改 mock 设备。

07 仍是教学版整合示例，只使用本地 mock。正式项目在 `ops_workflow.py`、
`device_gateway.py` 和 `device_ops_service.py` 中完成了异步 MCP、知识检索、
审计和 CLI/API 接入。

## 从 Demo 进入正式项目

1. 用 `device-agent --planner rules --backend mock` 跑查询和控制。
2. 读 `device_ops_service.py`，理解 `start()` 和 `resume()`。
3. 读 `ops_workflow.py`，画出节点、状态和条件路由。
4. 读 `device_gateway.py`，比较 Mock、本地 MCP 和 XDP 的映射差异。
5. 把一个故障知识条目改成自己的内容并验证检索结果。
6. 新增一个只读设备动作，并补对应测试。
