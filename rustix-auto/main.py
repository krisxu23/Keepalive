#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动启动脚本
- 支持多账号轮流操作
- 优先支持 Cookie 登录 (RUSTIX_COOKIE)，失效或未配置时自动降级至账号密码登录
- 自动登录 https://my.rustix.me/auth/login
- 点击 Manage Server -> 判断 start 按钮状态 -> 启动服务器
- 监听浏览器控制台 "Running Done!" 确认上线
- 通过 stop 按钮可点击状态验证（不点击 stop）

站点语言：俄语 / 英语（不支持中文）
"""

import json
import os
import sys
import time
import logging
import argparse
from datetime import datetime

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

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

LOGIN_URL = "https://my.rustix.me/auth/login"
HOME_URL = "https://my.rustix.me"
# 启动后等待 "Running Done!" 的最长时间（秒）
START_WAIT_TIMEOUT = 120
# 各步骤通用等待（ms）
STEP_WAIT = 3000
# 登录页 SPA 渐进渲染等待（ms）
LOGIN_PAGE_WAIT = 6000


# ---------------- 账号与 Cookie 加载 ----------------
def parse_accounts_string(raw: str):
    """解析 'email1:password1,email2:password2' 格式为账号列表。"""
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


def load_accounts():
    """读取账号配置。优先级：环境变量 ACCOUNTS > accounts.json 文件。"""
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

    raise RuntimeError(
        "未配置账号：请设置环境变量 ACCOUNTS（格式 email:password,...）或创建 accounts.json"
    )


def load_cookies_for_account(email: str) -> list:
    """从环境变量 RUSTIX_COOKIE 中解析当前账号的 Cookie 列表。
    
    支持格式：
    1. 单个账号的 JSON 数组: [{"name": "...", "value": "..."}, ...]
    2. 多账号映射字典: {"user1@email.com": [{"name": "..."}, ...], "user2@email.com": [...]}
    """
    cookie_env = os.environ.get("RUSTIX_COOKIE", "").strip()
    if not cookie_env:
        return []
    try:
        data = json.loads(cookie_env)
        # 格式 1：多账号字典映射
        if isinstance(data, dict) and email in data:
            logger.info(f"成功匹配到账号 {email} 的专属 Cookie 配置")
            return data[email]
        # 格式 2：单个账号的 Cookie 数组
        if isinstance(data, list):
            logger.info(f"载入通用/单账号 Cookie 配置")
            return data
        # 格式 3：单个 Cookie 字典字典
        if isinstance(data, dict) and "name" in data:
            logger.info(f"载入单条 Cookie 配置")
            return [data]
    except Exception as e:
        logger.warning(f"解析 RUSTIX_COOKIE 失败 (请确保其为合法的 JSON 格式): {e}")
    return []


# ---------------- 通用辅助 ----------------
def is_clickable(locator) -> bool:
    """判断元素是否可点击：可见 + 可用 + 非禁用 + 可接收指针事件。"""
    try:
        if locator.count() == 0:
            return False
        el = locator.first
        if not el.is_visible() or not el.is_enabled():
            return False
        if el.get_attribute("disabled") is not None:
            return False
        aria_disabled = el.get_attribute("aria-disabled")
        if aria_disabled and aria_disabled.lower() == "true":
            return False
        if el.evaluate("el => getComputedStyle(el).pointerEvents") == "none":
            return False
        return True
    except Exception:
        return False


def find_first_visible(page: Page, selectors):
    """按顺序在 selectors 中寻找第一个存在且可见的元素，返回 (locator, selector)。"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc, sel
        except Exception:
            continue
    return None, None


def find_button_by_text(page: Page, texts):
    """通过按钮文本查找按钮，texts 为候选文本列表（不区分大小写）。"""
    for text in texts:
        for sel in [
            f'button:has-text("{text}")',
            f'a:has-text("{text}")',
            f'[role="button"]:has-text("{text}")',
            f'input[type="submit"][value*="{text}" i]',
            f'input[type="button"][value*="{text}" i]',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc, sel, text
            except Exception:
                continue
    return None, None, None


# ---------------- 登录流程 ----------------
def do_login(page: Page, email: str, password: str) -> bool:
    logger.info(f"打开登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        logger.warning("页面加载超时，继续尝试")

    page.wait_for_timeout(LOGIN_PAGE_WAIT)

    # 用户名/邮箱输入框（实测：type="text" name="username"）
    email_loc, email_sel = find_first_visible(page, [
        'input[name="username"]',
        'input[type="email"]',
        'input[name="email"]',
        'input[autocomplete="username"]',
    ])
    # 密码输入框（实测：type="password" name="password"）
    pwd_loc, pwd_sel = find_first_visible(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
    ])

    if not email_loc or not pwd_loc:
        page.screenshot(path=f"debug_login_{int(time.time())}.png")
        logger.error("未找到登录表单（邮箱/密码输入框）")
        return False

    logger.info(f"填写账号: {email}")
    email_loc.fill(email)
    pwd_loc.fill(password)
    page.wait_for_timeout(500)

    # 登录按钮（实测：button[type="submit"] 文本 "Войти"）
    login_btn, login_sel, txt = find_button_by_text(page, [
        "Войти",          # 俄语
        "Login",          # 英语
        "Sign in",
    ])
    if not login_btn:
        login_btn, login_sel = find_first_visible(page, [
            'button[type="submit"]',
            'input[type="submit"]',
        ])
        txt = "submit(fallback)"

    if not login_btn:
        page.screenshot(path=f"debug_login_{int(time.time())}.png")
        logger.error("未找到登录按钮")
        return False

    logger.info(f"点击登录按钮 (text={txt})")
    try:
        login_btn.click()
    except Exception:
        login_btn.first.click(force=True)

    # 等待跳转/加载
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        logger.warning("登录后 networkidle 超时，继续流程")
    page.wait_for_timeout(STEP_WAIT)

    # 检测是否登录成功（仍在 login 页则失败）
    if "/auth/login" in page.url:
        body = (page.inner_text("body") or "")[:500].lower()
        if any(k in body for k in ["incorrect", "invalid", "неверн", "ошибк"]):
            logger.error("登录失败：账号或密码错误")
            return False
        logger.error("登录后仍在登录页")
        return False

    logger.info("登录成功")
    return True


# ---------------- Manage Server 流程 ----------------
def click_manage_server(page: Page) -> bool:
    logger.info("寻找 Manage Server 按钮")
    page.wait_for_timeout(STEP_WAIT)

    manage, sel, txt = find_button_by_text(page, [
        "Manage Server",
        "Manage",
        "Управление",      # 俄语：管理
        "Управлять сервером",
    ])
    if not manage:
        manage, sel = find_first_visible(page, [
            'a:has-text("Manage")',
            'a:has-text("Управление")',
            '[href*="manage" i]',
        ])
        txt = "Manage(fallback)"

    if not manage:
        page.screenshot(path=f"debug_dashboard_{int(time.time())}.png")
        logger.error("未找到 Manage Server 按钮")
        return False

    logger.info(f"点击 Manage Server 按钮 (text={txt})")
    try:
        manage.click()
    except Exception:
        manage.first.click(force=True)

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    # SPA 渲染控制台页面需要更长时间
    page.wait_for_timeout(6000)
    return True


# ---------------- 启动服务器流程 ----------------
def start_server(page: Page, console_lines: list) -> str:
    """
    返回状态字符串：
      - "started"  成功启动并验证
      - "online"   服务器已在线（start 不可点击）
      - "offline"  服务器离线且启动失败
      - "no_start" 未找到 start 按钮
    """
    logger.info("寻找 start 按钮")
    page.wait_for_timeout(STEP_WAIT)

    # 显式等待 start 按钮出现（SPA 渲染）
    try:
        page.wait_for_selector('button:has-text("Start")', timeout=15000)
    except PWTimeout:
        pass

    # 重新查找
    start_btn, sel, txt = find_button_by_text(page, [
        "Start",
        "Запустить",
        "Power On",
        "Boot",
    ])
    if not start_btn:
        page.screenshot(path=f"debug_start_{int(time.time())}.png")
        logger.error("未找到 start 按钮")
        return "no_start"

    clickable = is_clickable(start_btn)
    logger.info(f"start 按钮可点击状态: {clickable}")

    if not clickable:
        logger.info("start 按钮不可点击 -> 服务器可能已在线，跳过启动")
        if check_stop_button(page) == "clickable":
            logger.info("stop 按钮可点击，服务器确实在线")
        return "online"

    # start 可点击 -> 服务器离线，点击启动
    logger.info("服务器离线，点击 start 启动")
    try:
        start_btn.click()
    except Exception:
        start_btn.first.click(force=True)

    # 等待控制台输出 "Running Done!"
    logger.info(f"等待控制台输出 'Running Done!'（最长 {START_WAIT_TIMEOUT}s）")
    deadline = time.time() + START_WAIT_TIMEOUT
    detected = False
    while time.time() < deadline:
        if any("Running Done!" in line for line in console_lines):
            detected = True
            break
        try:
            if page.locator(":text('Running Done!')").count() > 0:
                detected = True
                break
        except Exception:
            pass
        page.wait_for_timeout(2000)

    if detected:
        logger.info("已检测到 'Running Done!'，服务器上线中")
    else:
        logger.warning("等待超时，未检测到 'Running Done!'，继续验证 stop 状态")

    # 通过 stop 按钮状态验证（不点击 stop）
    page.wait_for_timeout(STEP_WAIT)
    if check_stop_button(page) == "clickable":
        logger.info("验证成功：stop 按钮可点击，服务器已上线")
        return "started"
    logger.warning("验证未通过：stop 按钮不可点击")
    return "offline"


def check_stop_button(page: Page) -> str:
    """返回 'clickable' / 'exists_not_clickable' / 'not_found'。"""
    stop_btn, sel, txt = find_button_by_text(page, [
        "Stop",            # 英语
        "Остановить",      # 俄语
        "Power Off",
        "Shut down",
        "Shutdown",
    ])
    if not stop_btn:
        stop_btn, sel = find_first_visible(page, [
            'button:has-text("Stop")',
            'button:has-text("Остановить")',
            '[role="button"]:has-text("Stop")',
            'input[value="Stop" i]',
        ])

    if not stop_btn:
        logger.info("未找到 stop 按钮")
        return "not_found"

    clickable = is_clickable(stop_btn)
    logger.info(f"stop 按钮可点击状态: {clickable} (不点击)")
    return "clickable" if clickable else "exists_not_clickable"


# ---------------- 单账号处理 ----------------
def process_account(account: dict, playwright, headless: bool = True) -> dict:
    email = account.get("email", "").strip()
    password = account.get("password", "").strip()
    result = {"email": email, "ok": False, "status": "unknown", "error": ""}

    if not email or not password:
        result["error"] = "账号或密码为空"
        logger.error(result["error"])
        return result

    logger.info(f"========== 开始处理账号: {email} ==========")
    browser = None
    try:
        browser = playwright.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = context.new_page()

        # 收集控制台消息
        console_lines = []

        def on_console(msg):
            text = msg.text or ""
            console_lines.append(text)
            low = text.lower()
            if any(k in low for k in ["app is running", "error", "started", "running"]):
                logger.info(f"[console] {text}")

        page.on("console", on_console)
        page.on("pageerror", lambda err: logger.warning(f"[pageerror] {err}"))

        # 1. 尝试使用 Cookie 登录（首选）
        cookies = load_cookies_for_account(email)
        cookie_login_success = False

        if cookies:
            logger.info("检测到 RUSTIX_COOKIE 配置，尝试通过 Cookie 导入登录...")
            try:
                # 规范化 Cookie 域名格式，确保 Playwright 正常注入
                for c in cookies:
                    if "domain" not in c:
                        c["domain"] = "my.rustix.me"
                context.add_cookies(cookies)

                # 访问主页
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(STEP_WAIT)

                # 验证登录是否成功：没有被反向重定向到登录页，且页面中能检测到管理按钮
                if "/auth/login" not in page.url:
                    manage, _, _ = find_button_by_text(page, ["Manage Server", "Manage", "Управление"])
                    if manage:
                        logger.info("Cookie 验证成功！已直接登录。")
                        cookie_login_success = True
                    else:
                        # 额外等待 SPA 渐进渲染，再重试一次按钮检测
                        page.wait_for_timeout(STEP_WAIT)
                        manage, _, _ = find_button_by_text(page, ["Manage Server", "Manage", "Управление"])
                        if manage:
                            logger.info("Cookie 验证成功！已直接登录。")
                            cookie_login_success = True

                if not cookie_login_success:
                    logger.warning("Cookie 登录验证未通过（未能发现管理选项或被重定向至登录页）。")
            except Exception as e:
                logger.warning(f"使用 Cookie 登录时出现异常，将切换密码登录: {e}")

        # 2. 如果 Cookie 登录失败或未配置 Cookie，降级使用密码登录
        if not cookie_login_success:
            logger.info("尝试使用传统账号密码方式登录...")
            if not do_login(page, email, password):
                result["error"] = "登录失败（Cookie 和 密码均尝试完毕）"
                return result

        # 3. 点击 Manage Server
        if not click_manage_server(page):
            result["error"] = "未找到 Manage Server"
            return result

        # 4. 启动服务器并验证
        status = start_server(page, console_lines)
        result["status"] = status
        result["ok"] = status in ("started", "online")
        return result

    except Exception as e:
        result["error"] = f"异常: {e}"
        logger.exception("处理账号时发生异常")
        return result
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        logger.info(f"========== 账号 {email} 处理结束: status={result['status']} ==========\n")


# ---------------- 主入口 ----------------
def main():
    parser = argparse.ArgumentParser(description="Rustix 服务器自动启动")
    parser.add_argument("--headed", action="store_true", help="非无头模式（调试用）")
    parser.add_argument("--only", help="只处理指定邮箱的账号")
    args = parser.parse_args()

    accounts = load_accounts()
    if args.only:
        accounts = [a for a in accounts if a.get("email") == args.only]
        if not accounts:
            logger.error(f"未找到账号: {args.only}")
            sys.exit(1)

    logger.info(f"共 {len(accounts)} 个账号待处理")
    results = []
    if notify.tg_enabled():
        logger.info("已启用 Telegram 通知")
    with sync_playwright() as pw:
        for idx, acc in enumerate(accounts, 1):
            logger.info(f"--- 第 {idx}/{len(accounts)} 个账号 ---")
            res = process_account(acc, pw, headless=not args.headed)
            results.append(res)
            if idx < len(accounts):
                time.sleep(5)

    # 汇总
    logger.info("================ 结果汇总 ================")
    ok = 0
    for r in results:
        flag = "OK" if r["ok"] else "FAIL"
        logger.info(f"[{flag}] {r['email']} | status={r['status']} | {r['error']}")
        if r["ok"]:
            ok += 1
    logger.info(f"成功 {ok}/{len(results)}")

    # 推送汇总通知
    if notify.tg_enabled():
        notify.notify_summary(results)

    sys.exit(0 if ok == len(results) and ok > 0 else 1)


if __name__ == "__main__":
    main()
