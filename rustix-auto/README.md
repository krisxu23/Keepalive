# Rustix 自动启动保活 (rustix-auto)

Rustix.me（Pterodactyl 面板）服务器自动启动脚本。**Playwright 方案因无法通过 Mitelis 反爬挑战已被淘汰**，现基于 [Auto-Renew-Bothosting](https://github.com/eooce/Auto-Renew-Bothosting) 验证过的 **seleniumbase UC（undetected Chrome）** 方案重写。

## 功能

- 多账号轮流操作，密码登录优先、Cookie 登录降级
- 通过 Manage Server → 判断 start 按钮状态 → 点击启动 → 确认上线
- 上线确认：页面出现 `Running Done!` 或 stop 按钮变为可点击（**不点击 stop**）
- Telegram 通知（单账号明细 + 汇总，失败才推）
- 可选代理：配置 `NODE_LINK` 后浏览器流量走节点出口
- 完整日志 `run.log` + 失败调试截图 `debug_*.png`（workflow artifact 保留 7 天）
- 自动清理旧 workflow 运行记录（保留最近 1 条）

## 为什么需要 UC 浏览器（背景）

`my.rustix.me` 由 **Mitelis DDoS-Mitigation** 防护，其挑战链是：

```
第1层: Set-Cookie mit_ck_p1 -> 挑战页（内嵌 token，JS 设 mit_ck_p2 后自动刷新）
第2层: 带 p1+p2 -> 极简 HTML，内嵌解析阻塞脚本 /FsGtA7wj4k6YkizM?<加密串>
第3层: 该脚本返回 74KB 混淆 JS（执行证明），检测到自动化痕迹即拖死连接
```

实测行为：**对非浏览器客户端（curl/requests）秒回 403/挑战页；对带自动化标记的浏览器（无头 Chromium、`navigator.webdriver=true`、UA 与真实版本不符）拖死连接**——`domcontentloaded` 永不触发，页面永远加载中。换代理出口 IP 无效（问题在浏览器指纹，不在 IP）。

seleniumbase UC 的解法：

- 真实 Chrome + 有头模式（CI 中由 `xvfb-run` 提供虚拟显示），无自动化构建标记
- 启动时打反检测补丁，隐藏 `navigator.webdriver` 等特征
- `uc_open_with_reconnect`：检测到挑战时断开并重连 CDP 会话，让挑战误以为浏览器重启，拿到放行 cookie

**本地实测**：UC 浏览器 7 秒通过挑战链，真实登录表单正常渲染，表单提交链路（服务器返回真实凭据校验结果）验证通过。

## 部署

### 1. Secrets

| Secret | 必填 | 说明 |
|---|---|---|
| `RUSTIX_ACCOUNTS` | ✅ | 账号，格式 `email:password,email:password`（密码不能含逗号；按第一个冒号分割） |
| `RUSTIX_COOKIE` | ❌ | JSON 数组/对象的浏览器 Cookie，密码登录失败时降级使用 |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token（@BotFather），与下项同时配置才推送 |
| `TG_CHAT_ID` | ❌ | Telegram chat id（@userinfobot 获取；群组 id 为负数） |
| `NODE_LINK` | ❌ | 节点链接（`vmess:// trojan:// ss:// vless://` 或订阅），配置后走代理出口 |

### 2. 触发方式（任选）

- **GitHub 定时**：workflow 已内置 `0 */6 * * *` 每 6 小时一次
- **Uptime Kuma 通知**（推荐，DOWN 时立即触发）：
  1. Uptime Kuma → 通知 → 新建「Webhook」，URL 填 `https://api.github.com/repos/<用户名>/Keepalive/dispatches`
  2. 勾选「额外 Header」：`Authorization: Bearer <PAT>`、`Accept: application/vnd.github+json`
  3. 请求体选「自定义」：`{"event_type": "cloudflare_cron_trigger", "client_payload": {"status": "{{ status }}"}}`
  4. 在 Rustix 服务器监控项的「通知」处勾选该 Webhook
  5. 手动 Pause/Resume 一次监控项测试，Actions 页应出现 `rustix-auto-alive` 新 run

> `event_type` 必须为 `cloudflare_cron_trigger`，与 workflow 的 `types: [cloudflare_cron_trigger]` 对应。

## 工作原理

1. workflow 的 `⚙ 设置代理 (sing-box)` 步骤运行 `setup_proxy.sh`（第三方脚本，该步骤仅注入 `NODE_LINK` 一个环境变量）：`NODE_LINK` 非空 → 下载 sing-box → 解析节点 → 本地启动 SOCKS5 代理 → 写入 `IS_PROXY=true` / `PROXY_SERVER=socks5://127.0.0.1:1080` 到 `GITHUB_ENV`
2. `main.py` 先做**站点可达性预检**（20 秒内拿到 HTTP 响应即视为可达，403/200 都算），不可达快速失败
3. UC 浏览器（真实 Chrome + 有头 + 反检测补丁）打开登录页，`uc_open_with_reconnect` 绕过 Mitelis 挑战，轮询等待真实登录表单（最长 60s）
4. 密码登录优先；失败自动降级 Cookie 登录
5. 登录后点击 Manage Server → 检查 start 按钮状态：可点击 → 点击并等待 `Running Done!` / stop 按钮可点击（120s）；不可点击 → 已在线上
6. 结果汇总 + Telegram 通知，最后清理旧 workflow 运行记录

**如何确认生效**：运行日志出现：

```
📍 当前出口 IP: ...          （配置了 NODE_LINK 时）
站点预检: HTTP 200 | 耗时 x.xs
打开登录页: https://my.rustix.me/auth/login
等待 Mitelis 挑战通过（最长 60s）...
填写账号密码... / 点击登录按钮
✅ 登录成功，跳转至: ...
✅ 检测到 'Running Done!'    （或 start 不可点击 -> 服务器已在线）
```

## 故障排查

- **`60s 内未出现登录表单`**：挑战未通过。确认 workflow 运行步骤使用了 `xvfb-run` + `python3 main.py`（有头模式）；本地调试勿设 `HEADLESS=true`。
- **`站点预检失败`**：站点不可达或网络问题；若配置了 `NODE_LINK`，检查日志中 `📍 当前出口 IP` 是否为节点 IP。
- **`登录后未跳转`**：密码错误（服务器会提示 «Введены неверные данные для входа»），或账号风控（可尝试配置 `NODE_LINK` 换出口）。
- **`未找到 start 按钮` / `未找到 Manage Server 按钮`**：站点改版，在 `find_button_by_text` 中补充按钮文案（俄语/英语）。
- **`等待启动确认超时`**：已点击 start 但 120s 内未见上线信号，查看 artifact 的 `run.log` 和 `debug_*.png`。

## 本地运行

```bash
pip install -r requirements.txt
seleniumbase install chromedriver
# Windows/Linux 桌面环境直接运行（有头）
ACCOUNTS="email:password" python main.py
# Linux 无桌面环境
ACCOUNTS="email:password" xvfb-run -a python main.py
# 代理（可选）
IS_PROXY=true PROXY_SERVER=socks5://127.0.0.1:1080 xvfb-run -a python main.py
```

## 说明

- 账号密码等敏感信息仅通过 Secrets 传入，不会硬编码到脚本或提交到仓库
- 调试截图仅在失败时生成，随 `run.log` 上传 artifact（保留 7 天）
- `setup_proxy.sh` 来自第三方域名，该步骤仅接收 `NODE_LINK` 环境变量，其他 Secrets 不会暴露给它；sing-box 本体从 GitHub 官方 releases 下载
- 站点语言为俄语/英语，选择器见 `find_button_by_text` / `find_input_by_placeholder`（注意俄语占位符大小写：`Пароль` 为大写 П）
