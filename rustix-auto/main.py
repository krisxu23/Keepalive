#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动启动脚本
- 支持多账号轮流操作
- 优先支持 Cookie 登录 (RUSTIX_COOKIE)，失效或未配置时自动降级至账号密码登录
- 自动登录 https://my.rustix.me/auth/login
- 点击 Manage Server -> 判断 start 按钮状态 -> 启动服务器
- 支持 Telegram 多节点实事网页截图发送，方便排查假登录与状态异常
- 强力校验控制台前端路由跳转

站点语言：俄语 / 英语（不支持中文）
"""

import json
import os
import sys
import time
import logging
import argparse
from datetime import datetime

import requests
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
# 服务器列表卡片渲染等待（ms）
DASHBOARD_LOAD_WAIT = 15000
# 控制台页面加载等待（ms）
CONSOLE_LOAD_WAIT = 10000


# ---------------- Telegram 截图发送接口 ----------------
def send_telegram_photo(photo_path: str, caption: str = "") -> bool:
    """直接调用 Telegram Bot API 发送实时截图，方便远程排查。"""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过发送 TG 截图")
        return False
    if not os.path.exists(photo_path):
        logger.warning(f"未找到截图文件: {photo_path}")
        return False
        
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, 'rb') as photo:
            files = {'photo': photo}
            data = {'chat_id': chat_id, 'caption': caption}
            response = requests.post(url, data=data, files=files, timeout=15)
            if response.status_code == 200:
                logger.info(f"成功发送 Telegram 截图验证: {photo_path}")
                return True
            else:
                logger.warning(f"发送 TG 截图失败，状态码: {response.status_code}, 响应: {response.text}")
    except Exception as e:
        logger.warning(f"发送 TG 截图时出现网络异常: {e}")
    return False


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
    """从环境变量 RUSTIX_COOKIE 中解析当前账号的 Cookie 列表。"""
    cookie_env = os.environ.get("RUSTIX_COOKIE", "").strip()
    if not cookie_env:
        return []
    try:
        data = json.loads(cookie_env)
        if isinstance(data, dict) and email in data:
            logger.info(f"成功匹配到账号 {email} 的专属 Cookie 配置")
            return data[email]
        if isinstance(data, list):
            logger.info(f"载入通用/单账号 Cookie 配置")
            return data
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


def find_button_by_text_robust(page: Page, target_texts: list):
    """
    鲁棒地遍历页面按钮和链接元素。
    通过提取 innerText/textContent，净化空白字符后进行不区分大小写的子串匹配，
    从而解决 SVG 嵌套、类名混淆或多余空格导致的定位失败问题。
    """
    for text in target_texts:
        try:
            loc = page.get_by_role("button").filter(has_text=text).first
            if loc.count() > 0 and loc.is_visible():
                return loc, f"button_role_{text}", text
            loc_a = page.get_by_role("link").filter(has_text=text).first
            if loc_a.count() > 0 and loc_a.is_visible():
                return loc_a, f"link_role_{text}", text
        except Exception:
            continue

    try:
        elements = page.locator('button, a, [role="button"], input[type="button"], input[type="submit"]')
        count = elements.count()
        for i in range(count):
            el = elements.nth(i)
            text_content = el.text_content() or ""
            text_content_clean = " ".join(text_content.split()).lower()
            for target in target_texts:
                if target.lower() in text_content_clean:
                    if el.is_visible():
                        return el, f"custom_locator_{i}", target
    except Exception as e:
        logger.warning(f"遍历页面寻找按钮时出错: {e}")

    return None, None, None


# ---------------- 登录流程 ----------------
def do_login(page: Page, email: str, password: str) -> bool:
    logger.info(f"打开登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        logger.warning("页面加载超时，继续尝试")

    page.wait_for_timeout(LOGIN_PAGE_WAIT)

    # 用户名/邮箱输入框
    email_loc, email_sel = find_first_visible(page, [
        'input[name="username"]',
        'input[type="email"]',
        'input[name="email"]',
        'input[autocomplete="username"]',
    ])
    # 密码输入框
    pwd_loc, pwd_sel = find_first_visible(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
    ])

    if not email_loc or not pwd_loc:
        page.screenshot(path="debug_login_form_error.png")
        send_telegram_photo("debug_login_form_error.png", f"❌ 账号 {email} 未能加载到登录表单")
        logger.error("未找到登录表单")
        return False

    logger.info(f"填写账号: {email}")
    email_loc.fill(email)
    pwd_loc.fill(password)
    page.wait_for_timeout(500)

    # 登录按钮
    login_btn, login_sel, txt = find_button_by_text_robust(page, [
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
        page.screenshot(path="debug_login_btn_error.png")
        send_telegram_photo("debug_login_btn_error.png", f"❌ 账号 {email} 未找到登录按钮")
        logger.error("未找到登录按钮")
        return False

    logger.info(f"点击登录按钮 (text={txt})")
    try:
        login_btn.click()
    except Exception:
        login_btn.first.click(force=True)

    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(STEP_WAIT)

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
    logger.info("等待服务器列表卡片渲染完成...")

    try:
        page.wait_for_selector(
            'a[href*="/server/"][href*="/console"]',
            timeout=DASHBOARD_LOAD_WAIT,
        )
        logger.info("检测到 Manage Server 链接已渲染")
    except Exception as e:
        logger.warning(f"等待服务器卡片超时: {e}，继续尝试寻找按钮")

    page.wait_for_timeout(STEP_WAIT)

    logger.info("寻找 Manage Server 按钮")

    manage, sel, txt = find_button_by_text_robust(page, [
        "Manage Server",
        "Manage",
        "Управление",
        "Управлять сервером",
    ])
    if not manage:
        manage, sel = find_first_visible(page, [
            'a[href*="/server/"][href*="/console"]',
            'a:has-text("Manage")',
            'a:has-text("Управление")',
            '[href*="manage" i]',
        ])
        txt = "Manage(fallback)"

    if not manage:
        page.screenshot(path="debug_no_manage_btn.png")
        send_telegram_photo("debug_no_manage_btn.png", "❌ 登录成功后，但在控制面板主页未找到 'Manage Server' 按钮")
        logger.error("未找到 Manage Server 按钮")
        return False

    logger.info(f"点击 Manage Server 按钮 (text={txt})")
    try:
        manage.click()
    except Exception:
        manage.first.click(force=True)

    logger.info("等待控制台页面加载...")
    try:
        page.wait_for_url(
            lambda url: "/server/" in url and "/console" in url,
            timeout=CONSOLE_LOAD_WAIT,
        )
        logger.info(f"路由跳转成功，当前真实 URL: {page.url}")
    except Exception as e:
        logger.warning(f"等待 URL 路由重定向超时，当前 URL: {page.url}。将继续流程...")

    page.wait_for_timeout(STEP_WAIT)
    return True


# ---------------- 启动服务器流程 ----------------
def start_server(page: Page, console_lines: list, email: str) -> str:
    """
    返回状态字符串：
      - "started"  成功启动并验证
      - "online"   服务器已在线（无需操作）
      - "offline"  服务器离线且启动失败
      - "no_start" 未找到 start 按钮
    """
    logger.info("等待控制台页面状态元素渲染...")

    try:
        page.wait_for_function(
            """() => {
                const hasStart = document.querySelector('button') && 
                    Array.from(document.querySelectorAll('button')).some(b => 
                        b.textContent.trim().toLowerCase().includes('start') ||
                        b.textContent.trim().toLowerCase().includes('запустить')
                    );
                const hasStop = document.querySelector('button') &&
                    Array.from(document.querySelectorAll('button')).some(b =>
                        b.textContent.trim().toLowerCase().includes('stop') ||
                        b.textContent.trim().toLowerCase().includes('остановить')
                    );
                const hasOnline = document.body.innerText.toLowerCase().includes('online') ||
                                  document.body.innerText.toLowerCase().includes('запущен');
                const hasOffline = document.body.innerText.toLowerCase().includes('offline') ||
                                   document.body.innerText.toLowerCase().includes('выключен');
                return hasStart || hasStop || hasOnline || hasOffline;
            }""",
            timeout=CONSOLE_LOAD_WAIT,
        )
        logger.info("控制台状态元素渲染成功")
    except Exception as e:
        logger.warning(f"等待控制台状态渲染超时: {e}，继续尝试匹配")

    page.wait_for_timeout(STEP_WAIT)

    logger.info("寻找 start 按钮")
    start_btn, sel, txt = find_button_by_text_robust(page, [
        "Start",
        "Запустить",
        "Power On",
        "Boot",
    ])
    if not start_btn:
        page.screenshot(path="debug_no_start_btn.png")
        send_telegram_photo("debug_no_start_btn.png", f"⚠️ 账号 {email} 在控制台页面未找到 Start 按钮 (已拍照，请排查)")
        logger.error("未找到 start 按钮")
        return "no_start"

    # 3. 检查 start 按钮是否可点击
    clickable = is_clickable(start_btn)
    logger.info(f"start 按钮可点击状态: {clickable}")

    # 4. 如果 start 按钮不可点击 (disabled)
    if not clickable:
        logger.info("start 按钮不可点击 -> 检查服务器是否已经是在线状态...")
        page_text = page.locator("body").text_content() or ""
        is_online_text = "online" in page_text.lower() or "запущен" in page_text.lower()
        stop_status = check_stop_button(page)

        if is_online_text or stop_status == "clickable":
            logger.info("确认：服务器已在线，无需启动。拍摄在线截图...")
            page.screenshot(path="server_status_online.png")
            send_telegram_photo("server_status_online.png", f"ℹ️ 账号 {email} 的服务器已经处于 Online (在线状态)，无需再启动。")
            return "online"
        else:
            logger.warning("虽然 start 按钮不可点击，但没有检测到明确的 Online 状态。")
            page.screenshot(path="server_status_unknown.png")
            send_telegram_photo("server_status_unknown.png", f"❓ 账号 {email} 的 Start 按钮不可点击，但页面未显示在线状态，请核实。")
            return "online"

    # 5. 如果 start 按钮可点击，代表离线，点击启动
    logger.info("服务器目前处于离线状态，点击 start 启动")
    try:
        start_btn.click()
    except Exception:
        start_btn.first.click(force=True)

    # 6. 循环等待直到上线
    logger.info(f"等待服务器上线中（最长 {START_WAIT_TIMEOUT}s）")
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
        try:
            current_text = page.locator("body").text_content() or ""
            if "online" in current_text.lower() or "запущен" in current_text.lower():
                logger.info("检测到页面状态已成功变更为 Online")
                detected = True
                break
        except Exception:
            pass

        page.wait_for_timeout(2000)

    if detected:
        logger.info("服务器已成功上线")
    else:
        logger.warning("等待超时，未能捕获上线特征，进行最终 stop 按钮状态验证")

    # 7. 最终状态验证
    page.wait_for_timeout(STEP_WAIT)
    if check_stop_button(page) == "clickable":
        logger.info("验证成功：stop 按钮可点击，服务器已在线运行。发送喜报截图...")
        page.screenshot(path="server_start_success.png")
        send_telegram_photo("server_start_success.png", f"🚀 账号 {email} 的服务器已成功激活启动并验证上线！")
        return "started"
    
    logger.warning("验证未通过：stop 按钮不可点击")
    page.screenshot(path="server_start_failed.png")
    send_telegram_photo("server_start_failed.png", f"❌ 账号 {email} 的服务器离线，且点击 Start 启动后验证失败，仍处于 Offline。")
    return "offline"


def check_stop_button(page: Page) -> str:
    """返回 'clickable' / 'exists_not_clickable' / 'not_found'。"""
    stop_btn, sel, txt = find_button_by_text_robust(page, [
        "Stop",            # 英语
        "Остановить",      # 俄语
        "Power Off",
        "Shut down",
        "Shutdown",
    ])
    if not stop_btn:
        logger.info("未找到 stop 按钮")
        return "not_found"

    clickable = is_clickable(stop_btn)
    logger.info(f"stop 按钮可点击状态: {clickable} (不进行点击)")
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
                for c in cookies:
                    if "domain" not in c:
                        c["domain"] = "my.rustix.me"
                context.add_cookies(cookies)

                # 访问主页
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(STEP_WAIT)

                # 验证登录状态：是否被重定向到 /auth/login
                if "/auth/login" not in page.url:
                    logger.info("Cookie 已注入，等待服务器列表卡片渲染以验证登录状态...")
                    try:
                        page.wait_for_selector(
                            'a[href*="/server/"][href*="/console"]',
                            timeout=DASHBOARD_LOAD_WAIT,
                        )
                        logger.info("Cookie 验证成功！服务器列表已加载。")
                        cookie_login_success = True
                    except Exception as e:
                        logger.warning(f"等待服务器卡片超时: {e}")
                        manage, _, _ = find_button_by_text_robust(page, ["Manage Server", "Manage", "Управление"])
                        if manage:
                            logger.info("Cookie 验证成功！找到 Manage Server 按钮。")
                            cookie_login_success = True

                if not cookie_login_success:
                    logger.warning("Cookie 登录验证未通过。")
            except Exception as e:
                logger.warning(f"使用 Cookie 登录时出现异常，将切换密码登录: {e}")

        # 2. 账号密码登录兜底
        if not cookie_login_success:
            logger.info("尝试使用传统账号密码方式登录...")
            if not do_login(page, email, password):
                page.screenshot(path="login_failed.png")
                send_telegram_photo("login_failed.png", f"❌ 账号 {email} 登录失败（Cookie 和密码均失效）")
                result["error"] = "登录失败（Cookie 和 密码均尝试完毕）"
                return result
            logger.info("密码登录成功，跳转到服务器总览页面并等待加载...")
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector(
                    'a[href*="/server/"][href*="/console"]',
                    timeout=DASHBOARD_LOAD_WAIT,
                )
                logger.info("服务器列表卡片已加载")
            except Exception as e:
                logger.warning(f"等待服务器卡片加载超时: {e}")
            page.wait_for_timeout(STEP_WAIT)

        # 拍摄登录成功后的控制台主页，并推送到 Telegram
        logger.info("已成功登录主面板！正在截取主页面验证...")
        page.screenshot(path="dashboard_success.png")
        send_telegram_photo("dashboard_success.png", f"🔓 账号 {email} 登录主面板验证成功！正在切换到控制台...")

        # 3. 点击 Manage Server
        if not click_manage_server(page):
            result["error"] = "未找到 Manage Server"
            return result

        # 4. 启动服务器并验证
        status = start_server(page, console_lines, email)
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

    if notify.tg_enabled():
        notify.notify_summary(results)

    sys.exit(0 if ok == len(results) and ok > 0 else 1)


if __name__ == "__main__":
    main()
