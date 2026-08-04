# Real XDP Validation

验证日期：2026-08-03

## 验证范围

本次验证使用一台经授权的真实 CP-02S 设备，通过公司现有 XDP MCP
服务的 Streamable HTTP 接口完成。凭证只保存在本机 `.env`，本文不记录
PSN、Token、完整 URL、SSID、BSSID 或 MAC。

## 已完成

1. MCP 会话建立成功，并完成工具发现。
2. 以下五个只读工具均调用成功：
   - `get_device_info`
   - `get_machine_facts`
   - `get_port_details`
   - `get_charging_status`
   - `get_temperature_mode`
3. 设备事实返回 CP-02S、5 个端口和 160 W 总功率预算。
4. DeviceOps API 已切换到 `xdp + rules` 运行配置。
5. “查询设备整体状态”完成以下链路：
   `Planner -> validate -> retrieve -> query -> MCP -> summarize`。
6. “查询 2 号端口状态”成功返回归一化后的端口、连接、协议、电压、
   电流和功率字段。
7. “打开 2 号端口”曾停在 `confirmation_required`，随后以
   `approved=false` 取消，验证了拒绝分支不会执行真实控制。
8. 控制工作流增加操作前读取和操作后复查：
   `prepare_control -> interrupt -> control -> verify_control`。
9. “2 号端口为什么无法充电”完成四类证据采集：
   - 端口实时状态
   - 充电状态位图
   - PD 协商状态
   - 温度策略
10. 诊断结论为端口未检测到连接设备，Trace 完整经过：
    `collect_port_status -> collect_charging_status ->`
    `collect_pd_status -> collect_temperature_mode -> analyze_diagnosis`。
11. 经本人授权，真实执行了 2 号端口关闭命令；XDP MCP 返回
    `ports [2] turned off`。操作前端口已处于无负载状态，复查结果为
    `already_satisfied`。
12. 随后真实执行 2 号端口开启命令作为恢复操作；XDP MCP 返回
    `ports [2] turned on`。由于端口没有连接负载，端口详情仍为
    `disconnected / 0 W`，复查结果为 `unobservable`。
13. 恢复后的最终只读查询成功，设备端口保持无负载、无功率输出。
    这不能证明使能锁存状态，但控制工具成功返回且设备已恢复为开启命令。
14. 最新真实联调审计记录未包含设备凭证、网络标识和被过滤的原始
    PD 标识。
15. 增加设备运维对话控制台，复用同一 FastAPI 和 DeviceOpsService，
    支持查询、端口遥测、Trace、证据和控制确认。
16. 控制台已在桌面和 390 × 844 移动端完成浏览器验收；控制确认使用
    取消分支，没有在页面测试中再次改变真实设备。
17. 0.4.0 增加 `conversation_id` 短期上下文和精确状态摘要，真实完成
    以下多轮会话：
    - “现在几号端口在充电”明确返回 2 号端口及实时电气参数。
    - “所以是几号”再次明确返回 2 号端口。
    - “把它关掉”解析为 2 号端口关闭命令并停在确认阶段。
    - 取消后追问“所以它还在充电吗”，查询确认 2 号端口仍在充电。
18. 多轮控制验证全程使用取消分支，没有再次执行真实控制。
19. 增加 30 条人工标注的 Planner Eval，覆盖查询、诊断、控制、澄清、
    多轮指代和越权端口校验；规则 Planner 基线为 `30/30`。
20. 自动化测试共 65 项通过，源码与测试编译检查通过。

## 安全边界

- 真实设备信息进入 Agent 领域模型前使用字段白名单，PSN、SSID 和
  BSSID 不进入 API 响应或审计日志。
- 探针只调用固定的只读工具白名单。
- PD 诊断结果使用字段白名单，不保存原始 VID、PID 或 XID。
- 控制动作必须经过 LangGraph `interrupt` 和单独的 resume 请求。
- 控制前状态读取失败时直接阻止命令；命令成功后必须再次读取状态。
- 无负载时现有端口遥测无法展示开关使能位，因此项目区分“命令成功”
  和“状态变化已验证”，不会把 `unobservable` 写成已验证。

## 尚未完成

1. 在 2 号端口连接测试负载后再次执行控制，观察真实电压、电流或连接
   状态变化，把验证结果从 `unobservable` 提升为 `verified`。
2. 验证设备离线、MCP 超时、鉴权失败和响应字段缺失。
3. 保存经过人工复核的脱敏 fixture，补充离线回归测试。
4. 录制不包含设备凭证的演示视频。
