# DeviceOps Agent Eval

这组评测只检查自然语言请求到 `DeviceCommand` 的规划结果，以及命令能否
通过设备上下文安全校验。运行过程不会连接 MCP，也不会控制真实设备。

## 数据集

`planner_cases.json` 包含 30 条人工标注用例：

| 类别 | 数量 | 检查内容 |
| --- | ---: | --- |
| query | 7 | 整体状态与指定端口查询 |
| diagnosis | 5 | 端口故障诊断路由 |
| control | 6 | 开关动作、端口和确认标记 |
| clarification | 3 | 信息不足时请求补充端口 |
| context | 5 | “它、这个端口、刚才那个”等指代 |
| safety | 4 | 不在允许列表中的端口被上下文校验拒绝 |

每条用例检查：

- `action`
- `port`
- `enabled`
- `need_confirmation`
- `valid`，即是否应通过 `DeviceAgentContext` 校验

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

## 基线边界

2026-08-03 的规则 Planner 基线为 `30/30`，Exact Match `100%`。这个
数字只表示当前实现与这 30 条人工标注、范围内样本完全一致，不代表：

- 对任意自然语言请求都有 100% 泛化准确率。
- Gemini Planner 已达到相同结果。
- MCP 调用、设备响应或诊断结论质量为 100%。
- 系统已经完成生产环境稳定性或性能评测。

完整逐条结果见 `reports/planner_rules.json`。
