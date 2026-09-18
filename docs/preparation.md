# 数据准备与环境检查

两个工具共用用户导入的 JSON 配置：

- `prepare_test_data(action, parameters)`：执行已配置动作，支持 POST、PUT、PATCH、DELETE。
- `check_environment(check)`：执行已配置检查，只用 GET，不接收运行期参数。

模型只能选择已有名称，不能临时指定地址。名称不存在时返回可用标识，缺参数时返回字段名和类型。暂不支持异步轮询或脚本执行。

## 配置

每项需要 `url`、`method`、`success.path` 和 `success.equals`。`path` 是 JSON 键或数组索引列表，例如 `["ready"]`。HTTP 200 仍需通过结果校验。

参数支持 string、boolean、integer、number、object、array，可设 `required` 和 `default`。未知字段、NaN、Infinity 会被拒绝，JSON 对象不会转换为字符串。`timeout_s` 默认 15 秒，上限 60 秒。

完整示例见 [preparation.json](../examples/preparation.json)，运行方法见 [示例指南](../examples/README.md#可选的-http-假数据)。

## 返回结果

回执包含 `status`（success / failure / unknown）、`request_sent` 和安全错误代码，不包含凭据、请求内容或远端原文。

`request_sent=true` 只表示请求可能已发出。超时、响应无法判断或 HTTP 状态非成功时返回 unknown，写请求不自动重试。准备成功不会将用例记为 pass。

## 凭据与存储

`authorization_env` 引用以 `MINIAPP_PILOT_PREP_` 开头的环境变量，其值作为 Bearer Token 发送。不要把密钥写入配置或 URL。客户端不继承代理变量，也不跟随重定向。

配置保存在运行数据目录的 `preparation.json`。导入时完整校验后原子替换，失败保留旧文件；活跃 Suite 期间禁止替换。
