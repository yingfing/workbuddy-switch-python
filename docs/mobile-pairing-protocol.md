# WorkBuddy「连接电脑端」协议逆向报告

> 侦察时间：2026-11-20 · 来源：桌面端 5.6.2（Linux deb）运行时日志 + edge-sync 扩展静态分析
> 结论先行：**手机 App 检测电脑 = 服务端看到同账号下有「允许移动端连接」且 WS presence 在线的 desktop 设备**。wbspy 可用 4 个 HTTP/WS 步骤伪装成桌面端，无需真机。

## 1. 手机「正在检测中…」的判定原理

手机 App 打开「连接电脑端」后，向服务端查询：同 uid 下是否存在满足以下条件的设备：

1. **remote-view 标志 = 1**（桌面端开了「允许移动端连接」，默认开启）
2. **presence online**（该设备在 `user:<uid>:devices` 通道有 `device.online` 心跳，clientType=desktop）

两条件满足 → 手机显示可连接的桌面端列表，之后所有操控走云端指令通道（见 §5）。

## 2. 完整协议链（wbspy 伪装桌面端所需的最小集）

### 第 1 步：注册「允许移动端连接」

```
PUT https://www.workbuddy.cn/console/as/conversation-sync/devices/remote-view
Authorization: Bearer <accessToken>      # wbspy 现有 token
X-User-Id: <uid>
{"deviceId": "<我们的设备uuid>", "flag": 1}   # 1=允许，2=拒绝
```

### 第 2 步：换 Channel 凭据

```
POST https://www.workbuddy.cn/edge-cloud/credentials
Authorization: Bearer <accessToken> · X-User-Id: <uid> · Content-Type: application/json
{
  "userId": "<uid>",
  "deviceId": "<uuid>",
  "clientType": "desktop",
  "deviceName": "<主机名>",
  "useDefaultScopes": true,
  "chSdkVersion": "2.3.7",
  "appVersion": "5.6.2",
  "capabilities": { "decompress": [] }
}
```

响应字段（mapToCredentials）：

| 字段 | 用途 |
|---|---|
| connectToken | WS 连接令牌（Centrifuge token） |
| publishToken | HTTP publish 令牌（可空 → 走无 Authorization 分支） |
| subscriptionTokens | 订阅令牌 |
| encryptionKeys / signingKeys | 加密/签名密钥（keyVersion 标识） |
| apisixPublishUrl | `https://www.workbuddy.cn/cf-publish/api/publish`（HTTP 发布通道） |
| cfWsUrl | `wss://www.workbuddy.cn/cf-connect/websocket`（WS 通道） |
| heartbeatIntervalMs | 心跳间隔（实测 30000ms） |
| keyVersion / serverTimeMs / expiresIn / renewBefore | 密钥版本 / 时钟校准 / 有效期 |

### 第 3 步：WS 连接（标准 Centrifuge JSON 协议，客户端 js-centrifuge）

```
WSS wss://www.workbuddy.cn/cf-connect/websocket?untrusted_uid=<uid>
→ {"id":1,"method":"connect","params":{"token":"<connectToken>","name":"js","version":""}}
→ subscribe "user:<uid>:devices"
```

`untrusted_uid` query 仅做限流/观测，**不作鉴权依据**。

### 第 4 步：presence 心跳（30s，走 WS publish 帧）

发布到 `user:<uid>:devices`，payload 极简：

```json
{"type": "device.online", "deviceId": "<uuid>", "clientType": "desktop", "ts": 1732100000000}
```

开关变化时另发 `{"type": "device.authorization_changed", ...}`（flag 1/2）。

## 3. 设备身份

- `deviceId`：客户端自生成 uuid，持久化即可（沙箱桌面端示例：`6e902e35-103a-413c-8514-f6c0be1aef22`）
- `deviceName`：主机名（服务端展示用）
- credentials 403 → refreshAccount 后用新 uid 重试；token 过期由 renewBefore 触发续期（重打第 2 步）

## 4. 手机端在场的信号（逆向观察）

日志中同账号 `deviceId=f73b83c3 clientType=web` 反复 join/leave + `DEVICE_ACTIVE source=presence`——手机 App「正在检测中」的轮询会以 web 客户端身份短暂上线并扫设备。

## 5. 手机 → 电脑指令协议（连接建立后）

指令从 `user:<uid>:devices` 云端通道下行，格式：

```json
{"type": "wb:connectors:list", "args": [{"bound": true}],
 "requestId": "<uuid>", "replyChannel": "reply:<requestId>#<uid>"}
```

桌面端执行后向 `replyChannel` 发布结果。观测到的指令全集：

| 类型 | 频次 | 语义 |
|---|---|---|
| wb:conversations:list/get/request/send | 高 | 会话列表/详情/发起/发送 |
| remote | 高 | 远程控制信令 |
| wb:connectors:list/states | 高 | 连接器状态 |
| wb:automations:list | 中 | 自动化任务 |
| wb:workspaces:list / wb:artifacts:list | 中 | 工作区/产物 |
| wb:models:list / wb:skills:installed / wb:experts:list | 低 | 能力清单 |
| remoteDesktop.capabilities | 探测 | 远程桌面能力（remote-desktop 扩展） |
| session.create / task | 低 | 建会话/派任务 |
| wb:config:preference / wb:config:set | 低 | 配置读写 |

**wbspy 伪装的最小可用集**：只需回 `wb:conversations:*`（转发本地会话）+ `wb:models:list`（报渠道能力）+ `session.create`/`task`（接任务跑 wbspy 自动化），即可让手机把 wbspy 当桌面端用。

## 6. 关键代码锚点（复现用）

全部位于 `edge-sync/server/index.cjs`（5.4MB，内嵌 ChSdk 2.3.7 + 业务层）：

- `fetchChannelCredentials` → POST /edge-cloud/credentials 请求体
- `mapToCredentials` → 响应字段表
- `createTransport` → cfWsUrl + connectToken + untrusted_uid
- `WSTransport` → `new Centrifuge(appendUntrustedUidQuery(...), {token})`
- `DevicePresence.buildPayload` → 心跳 payload
- `setRemoteViewFlag` → PUT /console/as/conversation-sync/devices/remote-view
- `setMobileConnectionEnabled` → 开关编排（flag → WS → authorization_changed）
- `CMD_IN / CMD_RAW_PAYLOAD` → 指令协议（§5）

运行时日志：`~/.workbuddy/logs/<date>/edge-sync.log`（`MOBILE_CONNECTION_*` / `CHANNEL-SDK` / `CMD_IN` 全链路可观测）。

## 7. wbspy 实施路径建议

1. `wbspy/edge_sync.py`：四步协议客户端（HTTP 2 个 + WS Centrifuge JSON + 心跳定时器）
2. 设备身份：`accounts/` 下持久化 `deviceId`（uuid4）
3. 指令面：先实现 `wb:conversations:list` + `wb:models:list` + `session.create`，reply 走 publish 通道
4. 自动化挂接：`session.create`/`task` → 转发给 wbspy 现有 automation/pool
5. 心跳故障按日志语义降级：consecutiveFailures → self-heal（重拉 credentials → 重连）

## 8. 风险与边界

- 手机端可见性是**全局的**：伪装上线后，手机 App 会看到两台桌面端（真机 + wbspy），注意设备命名区分
- presence 断线不续报会在 SERVER_ONLINE_WINDOW_MS 后被判离线；心跳异常需自愈逻辑
- credentials 含 encryptionKeys/signingKeys（keyVersion 撤销机制）——后续若上行报文要求签名，需按 signingKeys 实装（当前 presence/指令收发观测均未强制）
