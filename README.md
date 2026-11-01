# WorkBuddy Switch Python 版（wbspy）— 多账号切换 · 扫码登录 · 自动签到/猫猫旅行赚积分 · 统一 OpenAI API 网关

**wbspy**（WorkBuddy Spy / WorkBuddy Switch Python Edition）是一个用纯 Python 独立重写的
[WorkBuddy 桌面助手](https://github.com/koi128bit/WorkBuddy-Switch)平替工具：把你散落在各处的
**AI 账号、免费模型、签到积分**统一管理起来 —— 一个命令行 + 一个网页控制台 + 一个本地
OpenAI 兼容 API 网关，全部搞定。

> 🇨🇳 中文项目 · Windows / macOS / Linux 全平台 · 仅依赖 Python 3.9+，安装后占用极小
>
> Based on [WorkBuddy Switch](https://github.com/koi128bit/WorkBuddy-Switch) by koi128bit ·
> 厂商注册表来自 [dsh-xdpool](https://www.npmjs.com/package/dsh-xdpool)（MIT）

---

## ✨ 它能干什么（五大功能）

### 1️⃣ 多账号一键切换（多号党刚需）

如果你有多个 WorkBuddy 账号（工作号 / 个人号），桌面客户端每次重新登录很麻烦。
wbspy 把当前登录存成**快照**，之后一键切换：

```bash
wbspy switch save            # 保存当前登录为快照（如 "工作号"）
wbspy switch list            # 列出所有快照
wbspy switch switch 工作号    # 切换到指定账号
wbspy switch restore         # 一键还原到切换前的登录状态
```

切换语义与原版一致：**只替换 `account` / `auth` 字段**，机器本地元数据（设置、插件、
对话历史、机器标识）全部保留；账号列表按 uid 去重重建；检测到切换后又产生新登录时
`restore` 会拒绝执行以保护当前状态。

### 2️⃣ 扫码登录 13 家 AI 渠道（Windows 也能扫码）

`wbspy login <渠道>` 按各渠道支持的方式**自动回退**（也可 `--method` 强制指定）：

| 方式 | 说明 | 已实现渠道 |
| --- | --- | --- |
| **qr 扫码** | 协议级扫码，二维码直接显示，手机扫一扫拿 token | Raccoon（商汤小浣熊）、Loomy（微信）、MiniMax、ZCode |
| **browser 网页授权** | 授权链接渲染成二维码 + 自动开浏览器，轮询拿凭据 | WorkBuddy 国内版 / 国际版（state → token → 档案） |
| **device 设备码** | 设备码 + PKCE，跨设备登录 | MiniMax、Cline（WorkOS）、Qoder 国内 / 国际版、CodeArts（华为云 ticket） |
| **callback 授权回调** | 授权页登录后 302 回本服务 /authorize 换凭据 | TRAE（refreshToken / PKCE 双路）、LobsterAI（authCode → exchange） |
| **oauth 本地回调** | Google OAuth，本地 8199 端口收 code | Gemini Code Assist |
| **local 读本机** | 读桌面客户端登录文件，零交互兜底 | 其余渠道 |

13 家渠道登录协议**全部真实现**（无「打开官网手动登录」降级）。TRAE / LobsterAI 的
回调在公网部署时自动指向 `WBSPY_PUBLIC_ORIGIN`（配合 `WBSPY_PUBLIC_SHARECODE`），
手机扫码登录后 302 跳回公网服务完成换票。

登录入口有两套：**命令行**（`wbspy login`）和**网页**（Web 控制台里点卡片，SVG 二维码弹窗，
全程不用终端 —— Windows 没法在终端扫码的问题不存在了）。MiniMax 还可以在网页里
「在新窗口打开官方登录页」直接从提供商页面扫码。

```bash
wbspy channels               # 查看全部 13 家渠道各自支持什么方式
wbspy login buddy            # WorkBuddy 国内版（网页授权）
wbspy login raccoon          # Raccoon 扫码直登
wbspy login buddy --method local   # 直接读本机客户端凭据
```

凭据落盘 `~/.wbspy/`，账号文件权限 0600；本机加密凭据（`$wbEncrypted`）自动识别。

### 3️⃣ 积分全自动：签到 · 成长任务 · 连登 · 抽奖 · 猫猫养成/旅行 · 补签

对接 WorkBuddy 国内版活动后端，**七步全流程一个不落**（这是目前覆盖最全的开源实现）：

| 步骤 | 做什么 | 对应积分项 |
| --- | --- | --- |
| ① 猫猫领养 | `buddy/info → agreement → first` 自动领养 | 养成玩法入口 |
| ② 每日签到 | daily-checkin | 基础积分 |
| ③ 成长任务 | 任务 accept + claim 两段式领取 | 任务积分 |
| ④ 连登兑换 | 读取连登天数（兼容嵌套/扁平两种响应）→ 自动兑换 | 连续登录奖励 |
| ⑤ 幸运抽奖 | `lottery/summary` 查剩余次数 → `lottery/draw` 全抽完（client_token 幂等） | 抽奖积分 |
| ⑥ 猫猫旅行 | 上一程领取（**带 record_id**）→ 出发新一程 | 旅行活跃保障 |
| ⑦ 补签卡 | `heatmap` 找昨日缺口 → `makeup-cards/use` 自动补签 | 活跃保障度 |

```bash
wbspy quota            # 查各账号当前积分
wbspy checkin          # 立即跑一轮（签到→任务→连登→抽奖→旅行→补签）
wbspy auto --once     # 单轮执行，适合挂 crontab
wbspy auto --daily 08:30   # 托管模式，每天 08:30 自动跑，可配 webhook 通知
```

仅国内版执行活动接口（国际版后端无签到，自动跳过，不会报错）。

### 4️⃣ 统一 OpenAI API 网关：一个端点穿透 76 家厂商 470+ 模型

启动 `wbspy web`（或 `wbspy gateway`）后，你得到一个本地 OpenAI 兼容端点。
**任何支持自定义 OpenAI base URL 的客户端**（Cherry Studio、LobeChat、沉浸式翻译、
openai sdk、curl……）都能直接接入：

| 你写的 model | 实际路由 | 容错能力 |
| --- | --- | --- |
| `cn:deepseek-v3` / `global:xxx` / 纯模型名 | WorkBuddy 账号池 | **多账号自动轮换**：429 限流按 Retry-After 冷却、凭据失效自动换号，响应头 `X-Wbspy-Account` 告知实际服务账号 |
| `deepseek/deepseek-chat` 等 `<厂商>/<模型>` | 76 家聚合渠道 | 按 Key 路由，OpenAI 兼容透传（71 家），同模型跨厂商 failover |
| Anthropic 协议模型 | 自动协议转换 | `/v1/messages` 端点，请求/响应/SSE 流式全转换 |

特性：

* ✅ **模型原始名称零改动** —— 对外暴露 `<厂商>/<原名>` 做路由命名空间，发到厂商上游的
  `model` 字段还原为厂商原始名称，版本号、free 标识等一个字符不动
* ✅ **470+ 模型目录**：`GET /v1/models` 一次拿全，`configured` 标注哪些已配好 Key
* ✅ **上下文预算裁剪**：超长对话按 token 预算自动裁剪（保 system + 最近两轮），不被上游拒
* ✅ **用量账本**：按天分桶统计调用次数与 token，网页看报表，CSV 导出
* ✅ **Token 自动续期**：账号临期前 10 分钟后台静默刷新
* ✅ **安全**：网关统一鉴权（Bearer / `X-Wbspy-Key`），默认只绑 127.0.0.1，
  域名白名单校验（未知域名拒绝外发凭据），凭据文件 0600

```bash
curl http://127.0.0.1:8178/v1/models
curl http://127.0.0.1:8178/v1/chat/completions \
  -d '{"model":"deepseek/deepseek-chat","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

### 5️⃣ Web 控制台（推荐，全部功能可视化）

```bash
wbspy web --port 8178     # 界面 + 网关同端口
```

浏览器打开 `http://127.0.0.1:8178/`，Raycast Dark 风格单页应用，六个板块：

* **登录** — 13 家渠道卡片，网页内 SVG 二维码扫码（Windows 友好），前端轮询状态
* **账号池** — 可用/加密/过期状态一目了然，一键移除
* **免费可调用 API** — 76 家厂商表（分类标签 + 搜索框），Key 输入即存即生效
* **积分自动化** — 一键跑一轮，实时日志
* **桌面切换** — 快照保存/切换/恢复
* **网关** — 池状态、模型目录、用量统计与 CSV 导出

---

## 🚀 快速开始

```bash
pip install .            # 本仓库根目录执行
wbspy web --port 8178    # 打开网页控制台
```

依赖极小：`requests` + `fastapi` + `uvicorn`（扫码另装 `qrcode`）。无 Node、无数据库、
无后台服务，全部本地运行。

## 🖥️ 部署要求

| 项目 | 要求 |
| --- | --- |
| Python | **≥ 3.9**（3.11+ 亦支持；无 3.10+ 专属语法，老系统无需编译新 Python） |
| 依赖 | `requests` / `fastapi` / `uvicorn`（纯 Python，无编译依赖，`pip install .` 自动装）；网页扫码渲染另需 `qrcode` |
| 操作系统 | Linux / macOS / Windows 均可（无平台专属代码）；**32 位 ARM（armv7）低配设备（玩客云 / 树莓派 32 位）可跑**，已验证依赖链全有 wheel，实测内存 ~60 MB |
| 资源 | 磁盘 < 10 MB，运行内存 ~50 MB |
| 网络 | 能访问各渠道上游域名即可；国内网络直连，无需代理 |
| 数据 | 凭据、配置、用量账本全部存于 `~/.wbspy/`；删除该目录即完全卸载，绝不散落别处 |

## 📦 部署方式

### 方式一：裸机 / VPS 直接跑

```bash
git clone https://github.com/yingfing/workbuddy-switch-python
cd workbuddy-switch-python && pip install .
wbspy web --host 0.0.0.0 --port 8178
```

systemd 常驻（`/etc/systemd/system/wbspy.service`）：

```ini
[Unit]
Description=wbspy - WorkBuddy Switch (Python)
After=network-online.target

[Service]
ExecStart=/usr/local/bin/wbspy web --host 0.0.0.0 --port 8178
Restart=on-failure
# 公网部署且用到 TRAE / LobsterAI 回调登录时取消注释：
# Environment=WBSPY_PUBLIC_ORIGIN=https://wbspy.example.com

[Install]
WantedBy=multi-user.target
```

### 方式二：Docker

```bash
docker compose up -d --build          # 凭据持久化在 ./data/wbspy
# 或纯 docker：
docker build -t wbspy . && docker run -d -p 8178:8178 -v wbspy-data:/root/.wbspy wbspy
```

### 迁移账号池

凭据就是 `~/.wbspy/` 目录下的文件，整机搬迁 = 拷目录：

```bash
rsync -av ~/.wbspy/ new-server:~/.wbspy/
```

不迁移也行——新机器上重新扫码，一分钟的事。

## 🌐 公网部署清单

暴露到公网前建议按顺序过一遍：

1. **设置鉴权 key**（三选一，优先级从高到低）：环境变量 `WBSPY_API_KEY=xxx`、启动参数 `wbspy web --api-key xxx`、或写入 `~/.wbspy/config.json` 的 `"api_key"` 字段。设置后 `/v1/*`（OpenAI 网关）与 `/api/*`（管理接口）全部要求 `Authorization: Bearer <key>` 或 `X-Wbspy-Key`，控制台页面与 `/healthz` 不受影响
2. **上 HTTPS**：前面挂 Nginx / Caddy 反代即可（Caddy 两行配置自动签证书）
3. **回调域名**：用到 TRAE / LobsterAI 扫码登录时，设置 `WBSPY_PUBLIC_ORIGIN=https://你的域名`，授权回调会自动指向公网（否则回调打在 127.0.0.1 上，扫码走不完）
4. **防火墙**：只放行 80/443，8178 端口不必对外

## 🆚 与原版的关系

| | WorkBuddy-Switch（原版） | wbspy（本项目） |
| --- | --- | --- |
| 技术栈 | Electron + TypeScript | **纯 Python**，无 Node 依赖 |
| 运行占用 | 打包后数百 MB | **安装后 < 5 MB** |
| 登录 | 桌面 GUI | 命令行 + **网页扫码**（跨平台） |
| 积分自动化 | 签到/任务/旅行 | **七步全流程**（含连登兑换、抽奖、补签卡） |
| 模型网关 | 账号池 | 账号池 + **76 家厂商统一穿透** + Anthropic 转换 |

本项目为**独立实现**：未复制原版任何 TypeScript/Electron 源码，借鉴的是产品形态与协议研究
成果。原版 Attribution License 明确「独立实现相同功能不因功能相似而受此许可约束」。

## 📦 工程结构

```
wbspy/
  document.py       凭据文档解析 / 国内国际版区域判定
  channels.py       13 家渠道注册表（qr / browser / device / local 分层）
  login.py          统一登录入口（扫码 → 网页 → 设备码 → 本机 多级回退）
  login_session.py  网页两段式登录状态机（SVG 二维码、授权链接、轮询）
  upstream.py       上游协议客户端（对话/积分七项/刷新/向量，错误分类驱动容错）
  switch.py         快照保存/切换/恢复（mergeAuth 语义）
  store.py          账号库 ~/.wbspy/（0600）
  pool.py           账号池（轮换 + 冷却 + 容错 + 自动续期）
  aggregate.py      76 家厂商统一穿透与跨厂商 failover
  convert.py        OpenAI ↔ Anthropic 协议转换（含 SSE 流式）
  context_budget.py 上下文预算裁剪
  gateway.py        FastAPI OpenAI 兼容网关（/v1/*，统一鉴权）
  webui.py          Web 控制台路由（/api/*、用量报表）
  automation.py     积分自动化七步全流程 + 定时调度 + webhook 通知
  usage.py          用量账本（按天分桶、CSV 导出）
  static/index.html 网页控制台（单页应用）
tests/test_wbspy.py  18 项协议核心测试（全过）
```

## ⚠️ 免责声明

本项目与腾讯 WorkBuddy / 商汤小浣熊 / MiniMax 及文中提到的任何厂商均无关联或背书关系。
上游协议随时可能变动；多账号轮换属灰色用法，**仅供个人学习研究，请自担风险**。

## 📄 License

[MIT](LICENSE)。第三方组件致谢见 [NOTICE.md](NOTICE.md)。
