# 示例指南

## 官方示例：基础交互

从 [微信官方示例仓库](https://github.com/wechat-miniprogram/miniprogram-demo) 扫码打开小程序。使用授权 Android 设备、已登录微信和模型服务即可，不需要 AppID、微信开发者工具或示例服务。

[下载 Excel](basic-interactions.xlsx) · [阅读用例](basic-interactions.md) · [演示视频](../docs/media/basic-interactions.mp4)

五个用例分别测试输入替换、开关切换、单选、多选和返回目录。每例自行导航、准备状态，不依赖上一例的数据。表格的结果与证据列为空，执行后另行生成回填文件。

### 当前状态与预检

2026-09-17 在 Android 14、微信 8.0.78 上检查了以下控件，并完成两轮执行。线上示例版本未知，后续使用前仍需核对页面。

| 页面 | 测试对象 |
| --- | --- |
| input | “控制最大输入长度的input”下，占位文字为“最大输入长度为10”的输入框 |
| switch | 推荐展示样式中的“开启中”开关，判断开关状态而非行标题 |
| radio | 推荐展示样式中的“美国”“中国” |
| checkbox | 推荐展示样式中的“美国” |
| 返回目录 | input 详情左上角返回，回到含 input、radio 条目的表单组件目录 |

入口为“小程序官方组件展示 → 表单组件”。部分页面的无障碍树混有背景目录，需对照当前画面。

维护用例时记录检查日期、系统与微信版本，确认控件可定位且只影响本地组件。入口失效或页面变化时暂停并重新预检，不更换为其他小程序或绕过权限。

### 执行

1. 启动平台，连接并选择手机，配置模型。
2. 手机打开官方示例，进入表单组件目录。
3. 核对上述控件后，上传 Excel，发送“执行基础交互工作表”。
4. 下载回填结果，核对中间状态、最终状态和对应截图。

用例只输入“示例甲”“示例乙”等合成文本。无法核实的结果留待复核，不改写预期。表内“待真机验证”是执行前标记，后续记录见 [测试记录](../docs/validation.md)。

## 自然语言演示（不上传 Excel）

这条路径尚未单独实录。手机先停在表单组件目录，不附加 Excel，依次发送：

> 不使用或创建 Excel 测试。打开当前微信官方示例中的 input 页面；如果当前不在该示例内，停止并告诉我。

看到页面打开后，再发送：

> 在“控制最大输入长度的input”下输入“MiniApp”，确认显示内容一致。只操作这个输入框，不提交数据；简要报告结果，不输出本机路径或运行标识。

## 维护者可选任务清单样例

`task-list/` 是合成任务清单，供维护者做可控回归，不影响首次体验。其本地逻辑已有离线测试，小程序与 HTTP 服务的真机联动尚待验证。

### 本地模式

在微信开发者工具中导入 `task-list/`，填写你有开发权限的 AppID，编译后在测试设备预览。参考 [微信开发者文档](https://developers.weixin.qq.com/miniprogram/dev/framework/quickstart/)。

默认只使用小程序本地存储，不访问网络。“重置演示数据”只清空本示例任务；[task-list.xlsx](task-list.xlsx) 测试创建、编辑、完成、筛选和空输入校验。

### 可选的 HTTP 假数据

启动合成数据服务（低风险，仅监听本机）：

```bash
.venv/bin/python examples/mock_service.py
```

在“设置 → 测试数据与环境检查”导入 [preparation.json](preparation.json)。用 `demo_health` 检查服务，或调用 `seed_tasks`，参数为 `{"tasks":[{"title":"阅读示例"}]}`。数据只存于内存，服务重启后清空。

手机接入同一服务时，将 `task-list/config.js` 的 `fixtureBaseUrl` 改为授权测试地址。USB 端口映射示例（中风险，让指定设备访问本机服务；SERIAL 替换为测试设备序列号）：

```bash
adb -s SERIAL reverse tcp:8766 tcp:8766
```

地址使用 `http://127.0.0.1:8766`，然后点击“载入测试数据”。微信可能限制 HTTP 调试地址；此时使用合规 HTTPS 测试服务或本地模式。

测试后撤销映射（低风险，仅移除该端口映射）：

```bash
adb -s SERIAL reverse --remove tcp:8766
```

## 维护用例

先修改 Excel，再同步 Markdown 阅读版。只读输出 Markdown（低风险，不改工作簿）：

```bash
.venv/bin/python scripts/render_example.py
```

回归测试会检查两者一致。录像说明与待验项目统一见 [测试记录](../docs/validation.md)，数据处理见 [安全说明](../SECURITY.md)。
