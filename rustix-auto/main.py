#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动启动脚本
- 支持多账号轮流操作
- 优先支持 Cookie 登录 (RUSTIX_COOKIE)，失效或未配置时自动降级至账号密码登录
- 通过服务器ID直接跳转控制台页面
- 自动刷新保存 Cookie 到 GitHub Repository Secrets
- 仅发送汇总通知
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
START_WAIT_TIMEOUT = 300
STEP_WAIT = 3000
LOGIN_PAGE_WAIT = 6000
DASHBOARD_LOAD_WAIT = 15000
CONSOLE_LOAD_WAIT = 15000


def get_server_console_url() -> str:
    server_id = os.environ.get("RUSTIX_SERVERID", "").strip()
    if not server_id:
        raise RuntimeError("未配置 RUSTIX_SERVERID 环境变量")
    return f"https://my.rustix.me/server/{server_id}/console"


def update_github_secret(secret_name: str, secret_value: str) -> bool:
    gh_token = os.environ.get("GH_TOKEN", "").strip()
    if not gh_token:
        logger.info("未配置 GH_TOKEN，跳过更新 GitHub Secret")
        return False

    repo_full_name = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo_full_name:
        logger.warning("未获取到 GITHUB_REPOSITORY 环境变量")
        return False

    public_key_url = f"https://api.github.com/repos/{repo_full_name}/actions/secrets/public-key"
    secret_url = f"https://api.github.com/repos/{repo_full_name}/actions/secrets/{secret_name}"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        resp = requests.get(public_key_url, headers=headers, timeout=10)
        public_key_data = resp.json()
        if resp.status_code != 200:
            logger.warning(f"获取 GitHub Public Key 失败: {resp.status_code}")
            return False

        public_key = public_key_data.get("key", "")
        key_id = public_key_data.get("key_id", "")
        if not public_key or not key_id:
            logger.warning(f"获取到的 Public Key 不完整")
            return False
        logger.info(f"成功获取 GitHub Public Key (key_id={key_id})")
    except Exception as e:
        logger.warning(f"获取 GitHub Public Key 异常: {e}")
        return False

    try:
        import base64
        from nacl import public, encoding

        public_key_obj = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
        sealed_box = public.SealedBox(public_key_obj)
        encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
        encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

        payload = {"encrypted_value": encrypted_b64, "key_id": key_id}
        resp = requests.put(secret_url, headers=headers, json=payload, timeout=10)
        if resp.status_code in (201, 204):
            logger.info(f"成功更新 GitHub Secret: {secret_name}")
            return True
        else:
            logger.warning(f"更新 GitHub Secret 失败: {resp.status_code}")
            return False
    except Exception as e:
        logger.warning(f"加密更新异常: {e}")
        return False


def save_cookies(context) -> bool:
    try:
        cookies = context.cookies()
        if not cookies:
            logger.info("未获取到任何 Cookie")
            return False
        cookie_json = json.dumps(cookies, indent=2)
        logger.info(f"获取到 {len(cookies)} 个 Cookie")
        return update_github_secret("RUSTIX_COOKIE", cookie_json)
    except Exception as e:
        logger.warning(f"保存 Cookie 异常: {e}")
        return False


def parse_accounts_string(raw: str):
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

    raise RuntimeError("未配置账号：请设置环境变量 ACCOUNTS")


def load_cookies_for_account(email: str) -> list:
    cookie_env = os.environ.get("RUSTIX_COOKIE", "").strip()
    if not cookie_env:
        return []
    try:
        data = json.loads(cookie_env)
        if isinstance(data, dict) and email in data:
            logger.info(f"成功匹配到账号 {email} 的专属 Cookie")
            return data[email]
        if isinstance(data, list):
            logger.info(f"载入通用 Cookie 配置")
            return data
        if isinstance(data, dict) and "name" in data:
            logger.info(f"载入单条 Cookie")
            return [data]
    except Exception as e:
        logger.warning(f"解析 RUSTIX_COOKIE 失败: {e}")
    return []


def is_clickable(locator) -> bool:
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
        return True
    except Exception:
        return False


def find_first_visible(page: Page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return loc, sel
        except Exception:
            continue
    return None, None


def find_button_by_text(page: Page, target_texts: list):
    """通用按钮查找：遍历所有按钮元素，检查文本内容"""
    all_buttons = page.locator('button, a, [role="button"]')
    count = all_buttons.count()

    for i in range(count):
        try:
            el = all_buttons.nth(i)
            if not el.is_visible():
                continue
            text_content = el.text_content() or ""
            text_clean = " ".join(text_content.split()).strip()
            for target in target_texts:
                if target.lower() in text_clean.lower():
                    return el, f"button_{i}", text_clean
        except Exception:
            continue

    return None, None, None


def find_start_button(page: Page):
    """精确查找 Start 按钮：按钮元素 + 文本包含 Start/Запустить + 没有 disabled"""
    all_buttons = page.locator('button')
    count = all_buttons.count()
    
    for i in range(count):
        try:
            el = all_buttons.nth(i)
            if not el.is_visible():
                continue
            text_content = el.text_content() or ""
            text_clean = " ".join(text_content.split()).strip().lower()
            
            if "start" in text_clean or "запустить" in text_clean or "power on" in text_clean:
                logger.info(f"找到 Start 按钮: '{text_clean}' (索引{i})")
                return el, f"start_button_{i}", text_clean
        except Exception:
            continue
    
    # 兜底：查找所有可点击元素
    all_elements = page.locator('[role="button"], a')
    count = all_elements.count()
    for i in range(count):
        try:
            el = all_elements.nth(i)
            if not el.is_visible():
                continue
            text_content = el.text_content() or ""
            text_clean = " ".join(text_content.split()).strip().lower()
            if "start" in text_clean or "запустить" in text_clean:
                logger.info(f"找到 Start 元素(兜底): '{text_clean}'")
                return el, f"start_el_{i}", text_clean
        except Exception:
            continue
    
    return None, None, None


def find_stop_button(page: Page):
    """精确查找 Stop 按钮"""
    all_buttons = page.locator('button')
    count = all_buttons.count()
    
    for i in range(count):
        try:
            el = all_buttons.nth(i)
            if not el.is_visible():
                continue
            text_content = el.text_content() or ""
            text_clean = " ".join(text_content.split()).strip().lower()
            
            if "stop" in text_clean or "остановить" in text_clean or "power off" in text_clean:
                logger.info(f"找到 Stop 按钮: '{text_clean}' (索引{i})")
                return el, f"stop_button_{i}", text_clean
        except Exception:
            continue
    
    return None, None, None


def check_server_online(page: Page) -> bool:
    """检测服务器是否在线：
    1. 精确匹配服务器卡片上的 Online 状态标签（text-success-50）
    2. Start 按钮不可点击 + Stop 按钮可点击
    """
    try:
        # 方式1：精确匹配服务器卡片上的 Online 状态标签
        status_spans = page.locator("span.text-success-50, span[class*='text-success']")
        count = status_spans.count()
        for i in range(count):
            text = (status_spans.nth(i).text_content() or "").strip().lower()
            if text == "online" or text == "запущен":
                logger.info(f"检测到精确状态标签: Online")
                return True

        # 方式2：精确匹配 ServerCardGradient 状态标签
        card_spans = page.locator("span[class*='ServerCardGradient']")
        count = card_spans.count()
        for i in range(count):
            text = (card_spans.nth(i).text_content() or "").strip().lower()
            if text == "online" or text == "запущен":
                logger.info(f"检测到 ServerCardGradient 状态: Online")
                return True

        # 方式3：Start/Stop 按钮组合判断
        start_btn, _, _ = find_start_button(page)
        stop_btn, _, _ = find_stop_button(page)

        if start_btn and stop_btn:
            start_clickable = is_clickable(start_btn)
            stop_clickable = is_clickable(stop_btn)
            logger.info(f"状态判定: Start可点击={start_clickable}, Stop可点击={stop_clickable}")

            if not start_clickable and stop_clickable:
                return True
            if start_clickable and not stop_clickable:
                return False

        # 方式4：兜底 - 检查 InformationBar 状态标签
        info_spans = page.locator('[class*="InformationBar"]')
        count = info_spans.count()
        for i in range(count):
            text = (info_spans.nth(i).text_content() or "").strip().lower()
            if text == "online" or text == "запущен":
                return True
            if text == "offline" or text == "выключен":
                return False
    except Exception as e:
        logger.warning(f"check_server_online 异常: {e}")
    return False


def check_dashboard_online(page: Page) -> bool:
    """跳转到总览页面检查服务器卡片状态"""
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        status_spans = page.locator("span.text-success-50, span[class*='text-success']")
        count = status_spans.count()
        for i in range(count):
            text = (status_spans.nth(i).text_content() or "").strip().lower()
            if text == "online" or text == "запущен":
                logger.info(f"总览页面检测到状态标签: Online")
                return True

        card_spans = page.locator("span[class*='ServerCardGradient']")
        count = card_spans.count()
        for i in range(count):
            text = (card_spans.nth(i).text_content() or "").strip().lower()
            if text == "online" or text == "запущен":
                logger.info(f"总览页面 ServerCardGradient 状态: Online")
                return True

        logger.info("总览页面未检测到 Online 状态")
        return False
    except Exception as e:
        logger.warning(f"检查总览页面状态异常: {e}")
        return False


def check_server_offline(page: Page) -> bool:
    """检测服务器是否离线：
    1. 精确匹配服务器卡片上的 Offline 状态标签（text-danger-50）
    2. Start 按钮可点击 + Stop 按钮不可点击
    """
    try:
        # 方式1：精确匹配服务器卡片上的 Offline 状态标签
        status_spans = page.locator("span.text-danger-50, span[class*='text-danger']")
        count = status_spans.count()
        for i in range(count):
            text = (status_spans.nth(i).text_content() or "").strip().lower()
            if text == "offline" or text == "выключен":
                logger.info(f"检测到精确状态标签: Offline")
                return True

        # 方式2：精确匹配 ServerCardGradient 状态标签
        card_spans = page.locator("span[class*='ServerCardGradient']")
        count = card_spans.count()
        for i in range(count):
            text = (card_spans.nth(i).text_content() or "").strip().lower()
            if text == "offline" or text == "выключен":
                logger.info(f"检测到 ServerCardGradient 状态: Offline")
                return True

        # 方式3：Start/Stop 按钮组合判断
        start_btn, _, _ = find_start_button(page)
        stop_btn, _, _ = find_stop_button(page)

        if start_btn and stop_btn:
            start_clickable = is_clickable(start_btn)
            stop_clickable = is_clickable(stop_btn)
            # 离线：Start可点击 + Stop不可点击
            if start_clickable and not stop_clickable:
                return True
            # 在线：Start不可点击 + Stop可点击
            if not start_clickable and stop_clickable:
                return False
    except Exception:
        pass
    return False


def do_login(page: Page, email: str, password: str) -> bool:
    logger.info(f"打开登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        logger.warning("页面加载超时")

    page.wait_for_timeout(LOGIN_PAGE_WAIT)

    email_loc, _ = find_first_visible(page, [
        'input[name="username"]', 'input[type="email"]', 'input[name="email"]'
    ])
    pwd_loc, _ = find_first_visible(page, [
        'input[type="password"]', 'input[name="password"]'
    ])

    if not email_loc or not pwd_loc:
        logger.error("未找到登录表单")
        return False

    logger.info(f"填写账号: {email}")
    email_loc.fill(email)
    pwd_loc.fill(password)
    page.wait_for_timeout(500)

    login_btn, _, txt = find_button_by_text(page, ["Войти", "Login", "Sign in"])
    if not login_btn:
        login_btn, _ = find_first_visible(page, ['button[type="submit"]', 'input[type="submit"]'])
        txt = "submit"

    if not login_btn:
        logger.error("未找到登录按钮")
        return False

    logger.info(f"点击登录按钮: {txt}")
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


def navigate_to_console(page: Page) -> bool:
    console_url = get_server_console_url()
    logger.info(f"直接跳转到控制台页面: {console_url}")
    try:
        page.goto(console_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        logger.warning(f"跳转异常: {e}")

    try:
        page.wait_for_url(lambda url: "/server/" in url and "/console" in url, timeout=CONSOLE_LOAD_WAIT)
        logger.info(f"路由跳转成功: {page.url}")
    except Exception:
        logger.warning(f"等待路由超时，当前 URL: {page.url}")

    page.wait_for_timeout(STEP_WAIT)
    return True


def start_server(page: Page, console_lines: list, email: str) -> str:
    """
    返回状态字符串：
      - "started"  成功启动并验证
      - "online"   服务器已在线（无需操作）
      - "offline"  服务器离线且启动失败
      - "no_start" 未找到 start 按钮
    """
    logger.info("等待控制台页面渲染...")
    try:
        page.wait_for_function(
            """() => {
                const text = document.body.innerText || "";
                return text.includes("Start") || text.includes("Stop") || 
                       text.includes("Online") || text.includes("Offline") ||
                       text.includes("Запустить") || text.includes("Остановить");
            }""",
            timeout=CONSOLE_LOAD_WAIT,
        )
        logger.info("控制台状态渲染成功")
    except Exception:
        logger.warning("等待渲染超时，继续尝试")

    page.wait_for_timeout(STEP_WAIT)

    if check_server_online(page):
        logger.info("服务器已处于 Online 状态，无需启动")
        return "online"

    if check_server_offline(page):
        logger.info("检测到服务器处于 Offline 状态")

    logger.info("寻找 start 按钮")
    start_btn, sel, txt = find_start_button(page)
    if not start_btn:
        logger.error("未找到 start 按钮")
        return "no_start"

    clickable = is_clickable(start_btn)
    logger.info(f"start 按钮可点击状态: {clickable}")

    if not clickable:
        if check_server_online(page):
            logger.info("确认：服务器已在线")
            return "online"
        else:
            logger.warning("start 按钮不可点击，但未检测到 Online 状态")
            return "online"

    logger.info("服务器离线，点击 start 启动")
    
    start_clicked = False
    try:
        # 方式1：普通点击
        start_btn.click()
        start_clicked = True
        logger.info("Start 按钮已点击（方式1）")
    except Exception as e:
        logger.warning(f"普通点击失败: {e}")
        try:
            # 方式2：force 点击
            start_btn.first.click(force=True)
            start_clicked = True
            logger.info("Start 按钮已点击（方式2: force）")
        except Exception as e2:
            logger.warning(f"force 点击也失败: {e2}")
            try:
                # 方式3：JS 点击
                start_btn.first.evaluate("el => el.click()")
                start_clicked = True
                logger.info("Start 按钮已点击（方式3: JS）")
            except Exception as e3:
                logger.error(f"所有点击方式都失败: {e3}")

    if not start_clicked:
        logger.error("无法点击 Start 按钮")
        return "offline"

    # 点击后等待一下，确认按钮状态变化
    page.wait_for_timeout(2000)
    try:
        start_btn2, _, _ = find_start_button(page)
        if start_btn2 and not is_clickable(start_btn2):
            logger.info("Start 按钮已变为不可点击，说明启动指令已发出")
        else:
            logger.warning("Start 按钮仍然可点击，可能需要确认弹窗")
            # 尝试点击确认按钮
            confirm_btn = page.get_by_role("button").filter(has_text="Confirm")
            if confirm_btn.count() > 0 and confirm_btn.first.is_visible():
                logger.info("检测到确认弹窗，点击确认")
                try:
                    confirm_btn.first.click()
                except Exception:
                    pass
    except Exception:
        pass

    logger.info(f"等待服务器上线中（最长 {START_WAIT_TIMEOUT}s）")
    deadline = time.time() + START_WAIT_TIMEOUT
    detected = False
    last_refresh = time.time()

    while time.time() < deadline:
        if any("Server marked as running" in line for line in console_lines):
            logger.info("控制台检测到 'Server marked as running'")
            detected = True
            break

        if any("Done (" in line and "For help" in line for line in console_lines):
            logger.info("控制台检测到服务器启动完成")
            detected = True
            break

        if check_server_online(page):
            logger.info("检测到页面状态已变更为 Online")
            detected = True
            break

        if time.time() - last_refresh >= 10:
            elapsed = int(time.time() - (deadline - START_WAIT_TIMEOUT))
            logger.info(f"已等待 {elapsed}s，刷新页面检查状态...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
            except Exception as e:
                logger.warning(f"刷新异常: {e}")
            last_refresh = time.time()

            if check_server_online(page):
                logger.info("刷新后检测到服务器已上线")
                detected = True
                break

            # 额外检查：跳转到总览页面验证服务器卡片状态
            if elapsed > 60:
                logger.info("额外验证：跳转到总览页面检查服务器卡片状态...")
                if check_dashboard_online(page):
                    logger.info("总览页面确认服务器已在线")
                    detected = True
                    break
                # 回到控制台页面继续等待
                console_url = get_server_console_url()
                page.goto(console_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

        page.wait_for_timeout(3000)

    if detected:
        logger.info("服务器已成功上线")
        return "started"
    else:
        logger.warning(f"等待超时（{START_WAIT_TIMEOUT}s）")

    # 最终验证：刷新页面再检查一次
    page.wait_for_timeout(STEP_WAIT)
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(STEP_WAIT)
    except Exception:
        pass

    if check_server_online(page):
        logger.info("最终验证：服务器已在线")
        return "started"

    logger.warning("验证未通过：服务器仍未上线")
    return "offline"


def check_stop_button(page: Page) -> str:
    stop_btn, sel, txt = find_stop_button(page)
    if not stop_btn:
        logger.info("未找到 stop 按钮")
        return "not_found"

    clickable = is_clickable(stop_btn)
    logger.info(f"stop 按钮可点击状态: {clickable}")
    return "clickable" if clickable else "exists_not_clickable"


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

        console_lines = []

        def on_console(msg):
            text = msg.text or ""
            console_lines.append(text)
            low = text.lower()
            if any(k in low for k in ["server marked as running", "done (", "running delayed init", "preparing spawn area"]):
                logger.info(f"[console] {text[:200]}")

        page.on("console", on_console)
        page.on("pageerror", lambda err: logger.warning(f"[pageerror] {err}"))

        cookies = load_cookies_for_account(email)
        cookie_login_success = False

        if cookies:
            logger.info("检测到 RUSTIX_COOKIE，尝试 Cookie 登录...")
            try:
                for c in cookies:
                    if "domain" not in c:
                        c["domain"] = "my.rustix.me"
                context.add_cookies(cookies)
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(STEP_WAIT)

                if "/auth/login" not in page.url:
                    logger.info("Cookie 已注入，等待服务器列表渲染...")
                    try:
                        page.wait_for_selector('a[href*="/server/"][href*="/console"]', timeout=DASHBOARD_LOAD_WAIT)
                        logger.info("Cookie 验证成功！服务器列表已加载")
                        cookie_login_success = True
                    except Exception:
                        logger.warning("等待服务器卡片超时")
                        manage, _, _ = find_button_by_text(page, ["Manage Server", "Manage", "Управление"])
                        if manage:
                            logger.info("Cookie 验证成功！找到 Manage 按钮")
                            cookie_login_success = True

                if not cookie_login_success:
                    logger.warning("Cookie 登录验证未通过")
            except Exception as e:
                logger.warning(f"Cookie 登录异常，切换密码登录: {e}")

        if not cookie_login_success:
            logger.info("尝试账号密码登录...")
            if not do_login(page, email, password):
                result["error"] = "登录失败"
                return result
            logger.info("密码登录成功，跳转服务器总览页面...")
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector('a[href*="/server/"][href*="/console"]', timeout=DASHBOARD_LOAD_WAIT)
                logger.info("服务器列表已加载")
            except Exception:
                logger.warning("等待服务器卡片超时")
            page.wait_for_timeout(STEP_WAIT)

        logger.info("已成功登录主面板！")
        save_cookies(context)

        if not navigate_to_console(page):
            result["error"] = "跳转到控制台失败"
            return result

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


def main():
    parser = argparse.ArgumentParser(description="Rustix 服务器自动启动")
    parser.add_argument("--headed", action="store_true", help="非无头模式")
    parser.add_argument("--only", help="只处理指定邮箱")
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
