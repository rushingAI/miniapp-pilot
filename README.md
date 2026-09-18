# MiniApp Pilot

用自然语言和 Excel 测试微信小程序，自动操作 Android 真机，记录截图并回填结果。

本地 Web 界面，面向单人、单设备测试。目前提供源码，支持 Android。

[演示视频](docs/media/basic-interactions.mp4) · [示例 Excel](examples/basic-interactions.xlsx) · [在线阅读用例](examples/basic-interactions.md)

## 演示

https://github.com/user-attachments/assets/dd24b87f-e08e-4294-a30e-eca7f0361ab6

在微信官方示例中执行五个用例：输入替换、开关切换、单选、多选、返回目录。视频约 85 秒，裁剪画面、4 倍速播放。[实测记录](docs/validation.md#公开演示录像)

## 功能

- 输入自然语言要求，实时查看手机画面和操作步骤。
- 上传 Excel，执行用例，下载带结果和证据引用的表格。
- 停止、继续测试；需要登录、权限或补充信息时等待人工处理。
- 按需接入 HTTP 数据准备、环境检查和腾讯在线表格。

## 快速开始

需要 Python 3.12、ADB、Android 设备和模型服务凭证。模型接入使用 Claude Agent SDK，默认端点为 Kimi Coding；不支持任意模型协议。

安装依赖（低风险）：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

检查依赖并启动本地服务（低风险）：

```bash
.venv/bin/python run.py --check
.venv/bin/python run.py
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

Windows 使用 `py -3.12 -m venv .venv` 和 `.venv\Scripts\python.exe`，实机启动尚待验证。没有设备或模型凭证时，也可打开界面和运行离线测试。

### 跑一次示例测试

不需要 AppID、微信开发者工具或自建示例服务。

1. USB 连接测试手机，开启并批准 USB 调试，在网页中选择设备。
2. 在“设置 → 模型服务”填写 API Key、Base URL 和模型。
3. 手机登录微信，从 [官方示例仓库](https://github.com/wechat-miniprogram/miniprogram-demo) 扫码打开示例，进入“表单组件”。先核对 [页面与控件](examples/README.md#当前状态与预检)。
4. 上传 [basic-interactions.xlsx](examples/basic-interactions.xlsx)，发送“执行基础交互工作表”。
5. 查看结果和截图，下载回填 Excel。

示例仅操作本地组件。页面与用例不符时先暂停；测试自己的小程序须有相应权限。MiniApp Pilot 是独立项目，与微信官方无隶属关系。

更多用法见 [示例指南](examples/README.md)：自然语言操作、任务清单小程序和可选数据准备服务。

## 配置与隐私

- API Key 默认只保存在内存中；选择“记住此电脑”后存入系统凭据库。
- 运行记录、上传文件和截图保存在 `~/.miniapp-pilot/`，可用 `MINIAPP_PILOT_DATA_DIR` 指定源码目录之外的位置。
- 模型执行会向你配置的服务发送任务文本和设备截图。
- 服务仅监听本机，没有多用户认证，请勿暴露到公网。

也可将 `device-mcp/.env.example` 复制为同目录 `.env`。只读取该文件的 `MINIAPP_PILOT_*` 配置，不继承父目录配置或通用 `ANTHROPIC_*` 变量。详见 [安全说明](SECURITY.md)。

## 开发

离线回归需要 Node.js 20+。运行测试和文件检查（低风险，不操作手机）：

```bash
.venv/bin/python -m pytest -q
node --test device-mcp/agent/web/tests/*.mjs tests/*.mjs
.venv/bin/python scripts/release_check.py
```

[贡献指南](CONTRIBUTING.md) · [架构](docs/architecture.md) · [数据准备](docs/preparation.md) · [已验证与待验证项目](docs/validation.md)

## 许可证与作者

[Apache-2.0](LICENSE) · [rushingAI](https://github.com/rushingAI) · [第三方声明](THIRD_PARTY.md)
