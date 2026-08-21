# DeviceOps Agent Eval

这组评测只检查自然语言请求到结构化业务决策的规划结果，以及设备命令能否
通过上下文安全校验。运行过程不会连接 MCP，也不会控制真实设备。

## 数据集

`planner_cases.json` 包含 40 条人工标注回归用例：

| 类别 | 数量 | 检查内容 |
| --- | ---: | --- |
| query | 7 | 整体状态与指定端口查询 |
| diagnosis | 5 | 端口故障诊断路由 |
| control | 6 | 开关动作、端口和确认标记 |
| clarification | 4 | 信息不足或缺少上一条结果时正常澄清 |
| context | 6 | “它、这个端口、为什么”等多轮指代与解释 |
| safety | 4 | 不在允许列表中的端口被上下文校验拒绝 |
| chat | 3 | 身份、能力和致谢 |
| knowledge | 3 | 固件、PD、OTA、MQTT 直接进入 RAG |
| unsupported | 2 | 天气和菜谱等范围外请求 |

每条用例检查：

- `intent`
- `action`
- `port`
- `enabled`
- `need_confirmation`
- `valid`，即是否应通过 `DeviceAgentContext` 校验

`planner_holdout_v1.json` 另含 18 条口语化留出用例。该集合在 `1.2.0`
实现完成后一次性冻结，不参与后续规则修正，用于观察未见表达的真实边界。

## 运行

```bash
device-agent-eval \
  --dataset evals/planner_cases.json \
  --output evals/reports/planner_rules.json
```

可选 Gemini Planner：

```bash
device-agent-eval \
  --planner gemini \
  --dataset evals/planner_cases.json \
  --output evals/reports/planner_gemini.json
```

Gemini 评测会调用模型 API 并产生费用；规则 Planner 评测完全离线。

本机 Ollama Planner：

```bash
device-agent-eval \
  --planner ollama \
  --dataset evals/planner_cases.json \
  --output evals/reports/planner_ollama.json
```

Ollama 评测只调用本地模型，不调用 MCP、RAG 或真实设备。

留出集运行：

```bash
device-agent-eval \
  --planner rules \
  --dataset evals/planner_holdout_v1.json \
  --output evals/reports/planner_holdout_v1_rules.json

device-agent-eval \
  --planner ollama \
  --dataset evals/planner_holdout_v1.json \
  --output evals/reports/planner_holdout_v1_ollama.json
```

## 基线边界

2026-08-07 的最终回归结果：

| Planner | 结果 | Exact Match |
| --- | ---: | ---: |
| Rules | 40/40 | 100% |
| Ollama `llama3.1:8b` | 40/40 | 100% |

Ollama 首轮为 `38/40`：一条整机在线查询缺少默认回答焦点，一条菜谱请求被判为
澄清。修复放在模型后的确定性归一化层，最终回归为 `40/40`。因为这组数据参与了
开发和修复，它是回归集，不是独立留出集。上述数字不代表：

- 对任意自然语言请求都有 100% 泛化准确率。
- 未见过的新表达也能达到 100%。
- Gemini Planner 已达到相同结果。
- MCP 调用、设备响应或诊断结论质量为 100%。
- 系统已经完成生产环境稳定性或性能评测。

完整逐条结果见 `reports/planner_rules.json` 和
`reports/planner_ollama.json`。

## 留出集结果

2026-08-10 的未调参首次结果：

| Planner | 结果 | Exact Match |
| --- | ---: | ---: |
| Rules | 9/18 | 50.0% |
| Ollama `llama3.1:8b` | 11/18 | 61.1% |

失败主要集中在“供电、正在工作、停掉、恢复供电、不对劲”等口语化设备表达。
这些失败没有被用于继续修改规则，因此该结果与 40 条开发回归集的 `40/40`
不能混为一谈。逐条结果见 `reports/planner_holdout_v1_rules.json` 和
`reports/planner_holdout_v1_ollama.json`。

## 双项目闭环评测

`mock_stack_e2e_cases.json` 覆盖 8 条公开、可重复的集成用例：双服务健康、业务
对话、带引用知识问答、设备状态查询、RAG 增强诊断、自动控制、控制后状态复查和
范围外拒答。运行：

```bash
.venv/bin/python scripts/evaluate_mock_stack.py
```

评测器使用兄弟 `firmware-knowledge-agent` 仓库自己的虚拟环境，通过子进程 ASGI
合同桥调用真实 FastAPI 端点，不占用端口。2026-08-21 本机结果为 `8/8`，平均
`6.79 ms`，P95 `15.12 ms`，逐条结果见
`reports/mock_stack_e2e.json`。

该报告使用 3 篇公开样例语料、Rule Planner 和内存 Mock 设备。它证明两个项目
之间的接口、工作流和控制校验能闭环，不证明 Ollama、XDP MCP、私有语料、真机
链路或生产环境性能。
