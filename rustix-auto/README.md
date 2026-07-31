# Rustix 服务器自动启动

自动登录 [my.rustix.me](https://my.rustix.me)，进入 Manage Server，检测并启动服务器，通过浏览器控制台 `Running Done!` 与 stop 按钮状态确认上线。支持多账号轮流操作、Telegram 通知、GitHub Actions 自动运行、代理节点出口。

## 功能

- 多账号轮流登录与操作（每个账号独立浏览器上下文）
- 登录策略：账号密码登录（第一选择）→ Cookie 登录（降级，成功后自动回写 `RUSTIX_COOKIE` Secret）
- 自动登录 → 点击 `Manage Server` → 判断 `start` 按钮状态
  - `start` 可点击 → 服务器离线，点击启动
  - `start` 不可点击 → 服务器已在线，跳过
- 监听浏览器控制台 `Running Done!` 确认上线，并通过 `stop` 按钮可点击状态验证（**不点击 stop**）
- **代理节点支持**：配置 `NODE_LINK` 后浏览器流量走节点出口，避开目标站对 GitHub 机房 IP 的拦截
- 启动时打印 `📍 当前出口 IP`，直观确认节点是否生效
- 站点可达性预检：启动前 20 秒探测 `my.rustix.me`，不可达时快速失败并提示原因（不再白等 90 秒后崩溃）
- 完整日志输出 + 文件日志 `run.log`，调试截图 `debug_*.png`
- Telegram 批量汇总通知（邮箱脱敏）
- 自动清理旧 workflow 运行记录（保留最近 1 条）

## 目录结构

```
.github/workflows/rustix-checkin.yml  # GitHub Actions 工作流
rustix-auto/
├── main.py                     # 主脚本
├── notify.py                   # Telegram 通知组件
├── requirements.txt            # Python 依赖
├── .env.example                # 环境变量示例
└── .gitignore
```

## 配置 Secrets

在仓库 **Settings → Secrets and variables → Actions → New repository secret** 添加：

| Secret | 必填 | 说明 |
|--------|------|------|
| `RUSTIX_ACCOUNTS` | ✅ | 账号列表，简单格式 `邮箱:密码`，多账号用英文逗号分隔。密码不能包含英文逗号，可以包含冒号（按第一个冒号分割） |
| `NODE_LINK` | 建议 | 代理节点链接，目标站拦截机房 IP 时必需（见下方「代理节点」） |
| `TG_BOT_TOKEN` | 可选 | Telegram Bot Token（@BotFather 获取） |
| `TG_CHAT_ID` | 可选 | 接收通知的 chat id（@userinfobot 获取；群组为负数） |
| `RUSTIX_COOKIE` | 可选 | 登录 Cookie（JSON）。密码登录成功后自动回写更新，无需手动维护；密码登录不可用时可降级使用 |
| `RUSTIX_SERVERID` | 可选 | 服务器 ID |
| `GH_TOKEN` | 可选 | GitHub PAT，用于自动更新 `RUSTIX_COOKIE` Secret 与清理旧 workflow 运行记录 |

## 代理节点（绕过 IP 拦截）

**为什么需要**：`my.rustix.me` 挂在 Mitelis DDoS 防护（响应头 `Server: Mitelis DDoS-Mitigation`）后面，会拦截或拖死 GitHub Actions 机房（数据中心）IP 的请求。典型表现：登录页 60 秒加载超时，随后连调试截图都超时崩溃（`Page.screenshot: Timeout 30000ms exceeded`）。

**怎么配**：只需要添加一个 Secret：

- `NODE_LINK`：你的节点分享链接，支持 `vless://`、`vmess://`、`trojan://`、`ss://` 等格式（从机场客户端的「复制节点链接」获取；订阅链接需先提取出单节点）

**工作原理**：

1. workflow 的 `⚙ 设置代理 (sing-box)` 步骤运行 `setup_proxy.sh`（第三方脚本，该步骤仅注入 `NODE_LINK` 一个环境变量）
2. `NODE_LINK` 非空 → 下载 sing-box → 解析节点 → 本地启动代理（SOCKS5 `127.0.0.1:1080` / HTTP `127.0.0.1:1081`），并把 `IS_PROXY=true`、`PROXY_SERVER=socks5://127.0.0.1:1080` 写入 `GITHUB_ENV`
3. 同一步骤里还会用 curl 分别以「直连」和「代理」两种路径访问 `my.rustix.me`，输出 HTTP 状态码与耗时对比，一眼判断当前出口是否被拦截
4. `main.py` 检测到 `IS_PROXY=true` → 浏览器上下文挂载代理，先请求 `api.ip.sb/ip` 打印当前出口 IP；随后对 `my.rustix.me` 做 20 秒可达性预检，不通则快速失败并给出明确原因（不再白等 60s+30s）
5. 之后所有页面访问都经 sing-box → 你的节点 → `my.rustix.me`，目标站看到的出口 IP 为节点 IP

**如何确认生效**：运行日志出现：

```
🔗 已启用代理: socks5://127.0.0.1:1080
📍 当前出口 IP: 1.2.3.4   ← 应为节点 IP，而不是 GitHub 机房 IP
站点预检: HTTP 200/302 | 耗时 x.xs
```

**注意：节点出口 IP 必须不是机房 IP**。机场（订阅）节点的出口绝大多数是数据中心 IP，Mitelis 对机房 IP 一视同仁地拦截——即使挂了代理也会同样超时。此时 workflow 会输出 `代理: 超时/失败（节点出口 IP 可能也被 Mitelis 拦截）`，请换用**住宅/家宽 IP 的节点**（如自建在家里的代理，或提供家宽出口的服务）。

**未配置 `NODE_LINK` 时**：`setup_proxy.sh` 写入 `IS_PROXY=false`，脚本直连，行为与原来完全一致。

**本地调试**：本地运行需自己先起好代理，再设置环境变量：

```bash
IS_PROXY=true PROXY_SERVER=socks5://127.0.0.1:1080 python main.py
```

## 通知示例

```
📊 Rustix 批量执行汇总

🚩 总体: 🎉 全部成功
⏰ 时间: 2026-06-27 13:46:00
📈 统计: 共 2 个
✅ 成功 2 | ❌ 失败 0
━━━━━━━━━━━━━━━━━━
账号明细
1️⃣ *********@example.com
    ✅ 成功启动
2️⃣ **********@example.com
    🟢 已在线
━━━━━━━━━━━━━━━━━━
🔗 前往控制台
```

> 未配置 `TG_BOT_TOKEN` / `TG_CHAT_ID` 时自动跳过通知，不影响主流程。网络异常时仅记录日志，不中断运行。

## GitHub Actions 自动运行

工作流文件：`.github/workflows/rustix-checkin.yml`，触发方式：

| 触发方式 | 说明 | 配置 |
|----------|------|------|
| 手动触发 | Actions 页面点 Run workflow | 默认支持，无需配置 |
| API 触发 | `repository_dispatch`，`event_type` 为 `cloudflare_cron_trigger`（需与 Worker 中配置严格一致） | 由 Cloudflare Worker 定时/事件触发，见下方 |

当前 workflow 为**无条件执行**（每次触发都完整运行）。如需按事件内容过滤（例如只在 Down 时执行），可在 job 上添加 `if` 条件，workflow 内已留有注释示例。

### Cloudflare Worker 触发（推荐）

仓库 `webhook-action/` 下有现成的 Worker 示例（Uptime Kuma → Cloudflare Worker → GitHub `repository_dispatch`）。部署时注意：

1. `event_type` 必须与 workflow 的 `types` 一致（当前为 `cloudflare_cron_trigger`），否则触发无效
2. Worker 需要 GitHub PAT（`repo` 权限 + Actions read/write），参考 `webhook-action/uptime-webhook.js` 头部的部署说明

### Uptime Kuma 故障触发（可选）

若用 Uptime Kuma 直接触发（不经 Worker）：

#### 1. 创建 GitHub PAT

1. GitHub → Settings → Developer settings → **Personal access tokens** → Fine-grained tokens → Generate new token
2. 设置：
   - **Token name**：`uptime-kuma-rustix`
   - **Repository access**：Only select repositories → 勾选 `Keepalive`
   - **Permissions**：Repository permissions → Actions → Read and write
3. 复制生成的 Token（只显示一次）

#### 2. Uptime Kuma 配置 Webhook 通知

在 Uptime Kuma 新建通知，类型选 **Webhook**：

| 字段 | 填写内容 |
|------|---------|
| 显示名称 | `Rustix 触发`（自定义） |
| Post URL | `https://api.github.com/repos/<用户名>/Keepalive/dispatches` |
| HTTP 方法 | `POST` |

勾选「额外 Header」，填：

```json
{
  "Authorization": "Bearer <你的PAT>",
  "Accept": "application/vnd.github+json"
}
```

请求体选「自定义」，填：

```json
{
  "event_type": "cloudflare_cron_trigger",
  "client_payload": {
    "status": "{{ status }}"
  }
}
```

> 关键：`event_type` 必须为 `cloudflare_cron_trigger`，与 workflow 里 `types: [cloudflare_cron_trigger]` 对应。`{{ status }}` 是 Uptime Kuma 内置模板变量，DOWN 时为 `Down`，UP 时为 `Up`。

#### 3. 关联到监控项

在 Rustix 服务器对应的监控项设置页底部「通知」处，勾选刚创建的 Webhook 通知。

#### 4. 测试

- 手动 Pause 再 Resume 监控项，触发一次 DOWN
- 去仓库 **Actions** 页面，应看到 `rustix-auto-alive` 产生新 run，触发事件显示 `repository_dispatch`
- 若 workflow 加了 `if: status == 'Down'` 过滤，UP 恢复时 run 会 skipped（不消耗 Actions 分钟数）

## 说明

- 账号密码等敏感信息仅通过 Secrets 传入，**不会**硬编码到脚本或提交到仓库。
- 调试截图 `debug_*.png` 仅在找不到关键元素时生成，随运行日志一起上传 artifact（保留 7 天）。
- 若站点页面结构更新导致选择器失效，可在 `find_button_by_text` / `find_first_visible` 中补充选择器。
- `setup_proxy.sh` 来自第三方域名，该步骤仅接收 `NODE_LINK` 环境变量，其他 Secrets 不会暴露给它；sing-box 本体从 GitHub 官方 releases 下载。
- 若配置节点后仍失败：先看 workflow 里 `⚙ 设置代理` 步骤的「直连 / 代理」对比测试输出——若代理路径超时/失败，说明节点出口 IP 被 Mitelis 拦截（机场节点多为机房 IP），需换**住宅/家宽 IP** 的节点；若代理测试正常但脚本仍失败，再检查日志中 `📍 当前出口 IP` 与 `站点预检` 输出。
