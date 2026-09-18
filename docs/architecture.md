# 执行架构

Web UI → Run Coordinator → Native Harness → device-operator → Device MCP / TestExcel

只有一条执行链。Coordinator 与 Suite Registry 管理 Run、attempt、Suite 身份和游标；模型理解任务、判断页面，程序负责动作、等待、证据和结果保存。SDK 使用隔离的 Run 工作目录，不加载外部设置。

## 测试生命周期

- `open_test_suite` 检查工作表并返回首步；`record_test_step` 保存结果、返回下一步，末步原子生成回填文件。
- Stop 中断当前 turn，保留游标。Continue 重验原 Suite 游标、观察设备，再继续执行。
- 新测试创建独立的 Suite、attempt 和证据目录。执行期间保持设备绑定。
- `needs_user_input` 等待人工处理，不计为步骤完成。
- operator 失败时，Harness 可在同一前台调用内接棒一次，仍使用原 Registry 和结果写入口。

## 动作与证据

`execute_ui_actions` 负责批量参数检查、执行、等待及后置条件校验。固定脚本 `run_ui_steps` / `run_ui_flow` 共用底层执行和 Stop 屏障。

画面变化只说明需要继续观察，不能单独判定用例通过。完成条件与证据都需核实，证据必须存在且属于当前 Suite。

## 数据准备模块

`preparation` 负责配置、参数校验、单次 HTTP 请求和安全回执，不管理 Suite 游标或测试 verdict。移除后，通用 UI、设备操作和表格导入仍可运行。

实际验证范围见 [测试记录](validation.md)。
