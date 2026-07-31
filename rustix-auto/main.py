#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动启动脚本 (seleniumbase UC 版)
=============================================
基于 Auto-Renew-Bothosting 验证过的反自动化方案：
  - seleniumbase uc 模式（undetected Chrome，真实浏览器指纹）
  - 有头模式运行（CI 中由 workflow 通过 xvfb-run 提供虚拟显示）
  - uc_open_with_reconnect 绕过 Mitelis 多层 JS 挑战
    （mit_ck_p1 cookie -> JS 设 mit_ck_p2 -> 74KB 混淆脚本执行证明，
     无头/带自动化标记的浏览器会被拖死，表现为 domcontentloaded 永不触发）

流程：
  加载账号 -> 站点可达性预检 -> UC 浏览器登录（密码优先，Cookie 降级）
  -> Manage Server -> 判断 start 按钮状态 -> 点击启动 -> 确认 Running Done!

环境变量：
  ACCOUNTS / ACCOUNTS_FILE   账号（email:password 逗号分隔；或 JSON 文件）
  RUSTIX_COOKIE              Cookie 降级登录（可选）
  IS_PROXY / PROXY_SERVER    代理（workflow 由 setup_proxy.sh 写入 GITHUB_ENV）
  TG_BOT_TOKEN / TG_CHAT_ID  Telegram 通知（可选）
  HEADLESS                   uc 模式默认有头；设 "true" 强制无头（仅调试用）

站点语言：俄语 / 英语（不支持中文）
"""

import argparse
import json
import logging
import os
import sys
import time

import requests
from seleniumbase import SB

import notify

# ---------------- 日志配置 ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("run.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("rustix-auto")

BASE_URL = "https://my.rustix.me"
LOGIN_URL = f"{BASE_URL}/auth/login"
STEP_WAIT = 2  # 秒

IS_PROXY = os.environ.get("IS_PROXY", "false").strip().lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"


# ---------------- 账号加载 ----------------
def parse_accounts_string(raw: str) -> list:
    accounts = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        email, password = item.split(":", 1)
        email, password = email.strip(), password.strip()
        if email and password:
            accounts.append({"email": email, "password": password})
    return accounts


def load_accounts() -> list:
    accounts_env = os.environ.get("ACCOUNTS", "").strip()
    if accounts_env:
        accounts = parse_accounts_string(accounts_env)
        if accounts:
            logger.info(f"从环境变量 ACCOUNTS 加载到 {len(accounts)} 个账号")
            return accounts

    accounts_file = os.environ.get("ACCOUNTS_FILE", "accounts.json")
    if os.path.exists(accounts_file):
        with open(accounts_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        logger.info(f"从文件 {accounts_file} 加载到 {len(data)} 个账号")
        return data

    raise RuntimeError("未配置账号：请设置环境变量 ACCOUNTS（格式 email:password,...）或创建 accounts.json")


# ---------------- 元素查找（俄语占位符注意大小写） ----------------
def find_input_by_placeholder(sb, keywords, name_fallbacks=()):
    """通过 placeholder 关键词查找输入框（CSS 属性子串匹配，区分大小写）。

    Rustix 登录框占位符为俄语：「Имя пользователя или эл. почта」与「Пароль」
    （П 大写），关键词必须覆盖实际大小写；name 属性兜底由调用方按字段语义传入
    （邮箱 -> username/email，密码 -> password），避免串找。
    """
    for kw in keywords:
        try:
            sel = f'input[placeholder*="{kw}"]'
            if sb.is_element_visible(sel):
                return sel
        except Exception:
            continue
    for name in name_fallbacks:
        try:
            sel = f'input[name="{name}"]'
            if sb.is_element_visible(sel):
                return sel
        except Exception:
            continue
    return None


def find_button_by_text(sb, texts):
    """查找可见按钮/链接（seleniumbase 支持 :contains 伪类）。"""
    for text in texts:
        for sel in (f'button:contains("{text}")', f'a:contains("{text}")'):
            try:
                if sb.is_element_visible(sel):
                    return sel
            except Exception:
                continue
    return None


# ---------------- 网络预检 ----------------
def get_current_ip(proxy_server: str = "") -> str:
    proxies = {"http": proxy_server, "https": proxy_server} if proxy_server else None
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()


def check_site_reachable(timeout: int = 20) -> bool:
    """站点可达性预检：20 秒内能拿到 HTTP 响应即视为可达（403/200 都算）。"""
    proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None
    try:
        resp = requests.get(
            LOGIN_URL,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
        )
        logger.info(f"站点预检: HTTP {resp.status_code} | 耗时 {resp.elapsed.total_seconds():.1f}s | {resp.url}")
        return True
    except Exception as e:
        logger.error(f"站点预检失败（{timeout}s 内未完成连接）: {e}")
        return False


# ---------------- Mitelis 挑战处理 ----------------
def wait_login_form(sb, timeout: int = 60) -> bool:
    """轮询等待登录表单出现。

    Mitelis 挑战链通过后页面才会出现真实登录表单（email/password 输入框），
    挑战页特征：极小 HTML + 内嵌 script 设 mit_ck_p2 + 解析阻塞混淆脚本，无 input。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            email_sel = find_input_by_placeholder(
                sb, ["почта", "username", "email", "user"], name_fallbacks=("username", "user", "email", "login")
            )
            pwd_sel = find_input_by_placeholder(
                sb, ["Пароль", "пароль", "password"], name_fallbacks=("password",)
            )
            if email_sel and pwd_sel:
                return True
        except Exception:
            pass
        sb.sleep(1)
    return False


def open_login_page(sb) -> bool:
    """打开登录页并等待 Mitelis 挑战链结束，返回是否出现登录表单。"""
    logger.info(f"打开登录页: {LOGIN_URL}")
    try:
        # uc 核心：检测到挑战时断开并重连 CDP 会话，让挑战以为浏览器重启
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=4)
    except Exception as e:
        logger.warning(f"uc_open_with_reconnect 异常，降级 sb.open: {e}")
        try:
            sb.open(LOGIN_URL)
        except Exception as e2:
            logger.warning(f"sb.open 也失败: {e2}")
    logger.info("等待 Mitelis 挑战通过（最长 60s）...")
    return wait_login_form(sb, timeout=60)


# ---------------- 登录 ----------------
def login_with_password(sb, email: str, password: str) -> bool:
    if not open_login_page(sb):
        logger.error("60s 内未出现登录表单（挑战未通过或站点不可达）")
        # 诊断：记录页面片段，区分「卡在挑战页」与「完全空白/拖死」
        try:
            src = sb.get_page_source()
            url = sb.get_current_url()
            logger.info(f"诊断 | URL: {url} | 页面大小: {len(src)} | 片段: {src[:200]!r}")
            logger.info(f"诊断 | 挑战标记: {[m for m in ('mit_ck', 'FsGtA7wj4k6YkizM', 'just a moment') if m in src]}")
        except Exception:
            pass
        try:
            sb.save_screenshot("debug_login.png")
        except Exception:
            pass
        return False

    email_sel = find_input_by_placeholder(
        sb, ["почта", "username", "email", "user"], name_fallbacks=("username", "user", "email", "login")
    )
    pwd_sel = find_input_by_placeholder(
        sb, ["Пароль", "пароль", "password"], name_fallbacks=("password",)
    )
    logger.info("填写账号密码...")
    sb.type(email_sel, email)
    sb.type(pwd_sel, password)

    login_btn = find_button_by_text(sb, ["Войти", "Login", "Sign in"]) or 'input[type="submit"]'
    logger.info(f"点击登录按钮 ({login_btn})")
    sb.click(login_btn)

    deadline = time.time() + 30
    while time.time() < deadline:
        url = sb.get_current_url()
        if "/auth/login" not in url:
            logger.info(f"✅ 登录成功，跳转至: {url}")
            return True
        sb.sleep(1)
    logger.error("登录后未跳转（密码错误或页面异常）")
    try:
        sb.save_screenshot("debug_login_failed.png")
    except Exception:
        pass
    return False


def login_with_cookie(sb) -> bool:
    cookie_env = os.environ.get("RUSTIX_COOKIE", "").strip()
    if not cookie_env:
        logger.info("未配置 RUSTIX_COOKIE 环境变量")
        return False
    try:
        data = json.loads(cookie_env)
        cookies = data if isinstance(data, list) else [data]
    except json.JSONDecodeError as e:
        logger.warning(f"RUSTIX_COOKIE 解析失败: {e}")
        return False

    logger.info("载入 Cookie 登录...")
    try:
        sb.open(BASE_URL + "/")
        sb.sleep(2)
        for c in cookies:
            try:
                sb.add_cookie({"name": c["name"], "value": c["value"], "domain": "my.rustix.me"})
            except Exception as e:
                logger.warning(f"cookie 注入失败 {c.get('name')}: {e}")
        sb.open(LOGIN_URL)
        sb.sleep(4)
    except Exception as e:
        logger.warning(f"Cookie 登录流程异常: {e}")
        return False

    url = sb.get_current_url()
    logger.info(f"Cookie 登录后 URL: {url}")
    return "/auth/login" not in url


# ---------------- Manage Server 流程 ----------------
def click_manage_server(sb) -> bool:
    logger.info("寻找 Manage Server 按钮")
    sb.sleep(STEP_WAIT)
    sel = find_button_by_text(sb, ["Manage Server", "Manage", "Управление", "Управлять сервером"])
    if not sel:
        logger.error("未找到 Manage Server 按钮")
        try:
            sb.save_screenshot("debug_dashboard.png")
        except Exception:
            pass
        return False
    logger.info(f"点击 Manage Server ({sel})")
    try:
        sb.click(sel)
    except Exception as e:
        logger.warning(f"点击异常: {e}")
        try:
            sb.js_click(sel)
        except Exception:
            return False
    sb.sleep(6)
    return True


def start_server(sb) -> str:
    """启动服务器，返回状态（与 notify.STATUS_MAP 对齐）：
    started=点击后确认上线 / online=本就在线 / no_start / offline=超时
    """
    logger.info("寻找 start 按钮")
    sb.sleep(STEP_WAIT)

    start_sel = find_button_by_text(sb, ["Start", "Запустить", "Power On", "Boot"])
    if not start_sel:
        logger.error("未找到 start 按钮")
        try:
            sb.save_screenshot("debug_start.png")
        except Exception:
            pass
        return "no_start"

    clickable = False
    try:
        clickable = sb.is_element_clickable(start_sel)
    except Exception:
        pass
    logger.info(f"start 按钮可点击状态: {clickable}")

    if not clickable:
        logger.info("start 不可点击 -> 服务器已在线")
        return "online"

    logger.info("点击 start 启动服务器...")
    sb.click(start_sel)

    # 等待确认上线：页面出现 "Running Done!" 或 stop 按钮变为可点击（最多 120s）
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            if "Running Done!" in sb.get_page_source():
                logger.info("✅ 检测到 'Running Done!'")
                return "started"
            stop_sel = find_button_by_text(sb, ["Stop", "Остановить", "Power Off", "Shut down", "Shutdown"])
            if stop_sel:
                try:
                    if sb.is_element_clickable(stop_sel):
                        logger.info("✅ stop 按钮已可点击（服务器已启动）")
                        return "started"
                except Exception:
                    pass
        except Exception:
            pass
        sb.sleep(3)
    logger.warning("等待启动确认超时")
    return "offline"


# ---------------- 账号处理 ----------------
def process_account(account: dict, headless: bool = False) -> dict:
    email, password = account["email"], account["password"]
    result = {"email": email, "ok": False, "status": "unknown", "error": ""}
    logger.info(f"========== 开始处理账号: {email} ==========")

    sb_kwargs = {"uc": True, "headless": headless}  # uc 模式需真实浏览器（CI 用 xvfb 虚拟显示）
    if IS_PROXY:
        sb_kwargs["proxy"] = PROXY_SERVER
        logger.info(f"🔗 已启用代理: {PROXY_SERVER}")

    with SB(**sb_kwargs) as sb:
        try:
            # 诊断：代理模式下探测浏览器实际出口 IP，
            # 用于区分「代理未生效(直连机房IP)」和「代理生效但节点IP被拉黑」
            if IS_PROXY:
                try:
                    sb.open("https://api.ip.sb/ip")
                    ip = sb.get_text("body").strip()
                    logger.info(f"🌐 浏览器实际出口 IP: {ip}")
                except Exception as e:
                    logger.warning(f"浏览器出口 IP 探测失败: {e}")

            logger.info("尝试账号密码登录（第一选择）...")
            login_ok = login_with_password(sb, email, password)
            if not login_ok:
                logger.warning("密码登录失败，降级尝试 Cookie 登录...")
                login_ok = login_with_cookie(sb)

            if login_ok:
                result["status"] = "login_ok"
                if click_manage_server(sb):
                    status = start_server(sb)
                    result["status"] = status
                    result["ok"] = status in ("started", "online")
                    result["error"] = "" if result["ok"] else f"启动流程: {status}"
                else:
                    result["error"] = "未找到 Manage Server 按钮"
            else:
                result["status"] = "unknown"
                result["error"] = "密码登录和 Cookie 登录均失败"
        except Exception as e:
            result["error"] = str(e)[:300]
            logger.error(f"处理账号时发生异常: {e}")
            try:
                sb.save_screenshot("debug_exception.png")
            except Exception:
                pass

    logger.info(f"========== 账号 {email} 处理结束: status={result['status']} ==========")
    return result


# ---------------- 主入口 ----------------
def main():
    parser = argparse.ArgumentParser(description="Rustix 服务器自动启动 (seleniumbase UC)")
    parser.add_argument("--only", help="只处理指定邮箱的账号")
    args = parser.parse_args()

    accounts = load_accounts()
    if args.only:
        accounts = [a for a in accounts if a.get("email") == args.only]
        if not accounts:
            logger.error(f"未找到账号: {args.only}")
            sys.exit(1)

    logger.info(f"共 {len(accounts)} 个账号待处理")

    # uc 模式默认有头（无头会被 Mitelis 挑战识别），CI 由 xvfb-run 提供显示
    headless = os.environ.get("HEADLESS", "").strip().lower() in ("1", "true", "yes")
    if headless:
        logger.warning("HEADLESS=true：无头模式可能被 Mitelis 挑战识别，建议仅在调试时使用")

    if IS_PROXY:
        logger.info(f"🔗 已启用代理: {PROXY_SERVER}")
        try:
            logger.info(f"📍 当前出口 IP: {get_current_ip(PROXY_SERVER)}")
        except Exception as e:
            logger.warning(f"⚠️ 获取出口 IP 失败（检查节点是否可用）: {e}")
    else:
        logger.info("🍭 未启用代理，直连访问（若账号级风控拦截，请配置 NODE_LINK）")

    if not check_site_reachable():
        logger.error("站点不可达，跳过本次执行（快速失败）")
        sys.exit(1)

    results = []
    for idx, acc in enumerate(accounts, 1):
        logger.info(f"--- 第 {idx}/{len(accounts)} 个账号 ---")
        results.append(process_account(acc, headless=headless))
        if idx < len(accounts):
            time.sleep(5)

    logger.info("================ 结果汇总 ================")
    for r in results:
        flag = "OK" if r["ok"] else "FAIL"
        logger.info(f"[{flag}] {r['email']} | status={r['status']} | {r['error']}")

    if notify.tg_enabled():
        notify.notify_summary(results)

    failed = [r for r in results if not r["ok"]]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
