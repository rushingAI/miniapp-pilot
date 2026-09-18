# 第三方依赖与素材

MiniApp Pilot 自有代码采用 Apache-2.0；第三方依赖保留各自许可证，通过包管理器安装，不随本源码包分发。

## 直接运行依赖

以下记录来自对应版本安装包的许可证元数据（2026-09-18）。

| 依赖 | 版本 | 许可证标识 |
| --- | --- | --- |
| anyio | 4.14.0 | MIT |
| claude-agent-sdk | 0.2.110 | MIT，另见下文运行条款 |
| fastapi | 0.138.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| mcp | 1.28.0 | MIT |
| openpyxl | 3.1.5 | MIT |
| pillow | 12.2.0 | MIT-CMU |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pyyaml | 6.0.3 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| uiautomator2 | 3.6.0 | MIT |
| uvicorn | 0.49.0 | BSD-3-Clause |
| websockets | 16.0 | BSD-3-Clause |

Claude Agent SDK 的 LICENSE 标为 MIT，但安装包还捆绑 Claude Code CLI，其元数据另列 [Anthropic 商业服务条款](https://www.anthropic.com/legal/commercial-terms)。SDK 的 MIT 标识不覆盖所有运行组件和服务条件；模型接入与商业使用仍需核对相应条款。

测试依赖 pytest，以及上述包的传递依赖，按各自发行包条款使用。本表不是完整依赖清单；若以后分发安装器或捆绑依赖，需按实际打包内容补齐许可证及 NOTICE。

## 示例与视频

[微信官方示例](https://github.com/wechat-miniprogram/miniprogram-demo) 是被测对象，本项目不分发其源码。演示中的第三方界面、标识和商标不因本项目使用 Apache-2.0 而转为该许可；公开使用仍需审阅。
