#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动启动脚本
- 支持多账号轮流操作
- 优先支持 Cookie 登录 (RUSTIX_COOKIE)，失效或未配置时自动降级至账号密码登录
- 通过服务器ID直接跳转控制台页面
- 自动刷新保存 Cookie 到 GitHub Repository Secrets
- 仅发送汇总通知

改进记录 (v2):
- 固定 wait_for_timeout → wait_for_selector / wait_for_function 智能等待
- 关键操作加 retry 装饰器
- Cloudflare 验证页检测与等待
- 监控循环：刷新后真正等页面渲染完
- Cookie 过期时明确日志提示
- 登录失败时保存调试截图
"""

import json
import os
import sys
import time
import logging
import argparse
import traceback
from datetime import datetime
from functools import wraps

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
SMART_WAIT = 15000          # 智能等待默认超时 (ms)
LOGIN_PAGE_WAIT = 6000      # 登录页渲染等待 (ms)
CONSOLE_LOAD_WAIT = 15000
STEP_SHORT = 1500           # 短等待 (ms)
STEP_MEDIUM = 3000          # 中等待 (ms)


# ─── 重试工具 ───────────────────────────────────────────────

def retry(max_attempts=3, delay=2, backoff=2, exceptions=(Exception,)):
    """重试装饰器，指数退避"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            wait = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_attempts:
                        logger.warning(f"[重试] {func.__name__} 第{attempt}次失败: {e}，{wait}s后重试")
                        time.sleep(wait)
                        wait *= backoff
                    else:
                        logger.error(f"[重试] {func.__name__} 全部{max_attempts}次尝试失败")
            raise last_exc
        return wrapper
    return decorator


# ─── Cloudflare 检测 ────────────────────────────────────────

def is_cloudflare_challenge(page: Page) -> bool:
    """检测当前页面是否是 Cloudflare 验证页"""
    try:
        title = page.title().lower()
        url = page.url.lower()
        if "just a moment" in title or "checking" in title or "challenge" in title:
            return True
        if "challenges.cloudflare.com" in url:
            return True
        # 检查页面内容
        body_text = (page.inner_text("body") or "")[:500].lower()
        cf_keywords = ["verify you are human", "checking your browser",
                       "please wait", "cloudflare", "turnstile"]
        if sum(1 for kw in cf_keywords if kw in body_text) >= 2:
            return True
    except Exception:
        pass
    return False


def wait_for_cloudflare_clear(page: Page, max_wait: int = 30) -> bool:
    """等待 Cloudflare 验证通过，返回是否成功通过"""
    if not is_cloudflare_challenge(page):
        return True
    logger.info("检测到 Cloudflare 验证页，等待自动通过...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(2)
        if not is_cloudflare_challenge(page):
            logger.info("Cloudflare 验证已通过")
            return True
    logger.warning(f"Cloudflare 验证等待超时 ({max_wait}s)")
    return False


# ─── 智能等待辅助 ───────────────────────────────────────────

def smart_wait_for_elements(page: Page, selectors: list, timeout: int = SMART_WAIT) -> bool:
    """智能等待：等待任意一个选择器对应的元素出现并可见"""
    selector_str = ", ".join(selectors)
    try:
        page.wait_for_selector(selector_str, state="visible", timeout=timeout)
        return True
    except PWTimeout:
        return False


def safe_goto(page: Page, url: str, timeout: int = 60000) -> bool:
    """安全跳转：带 Cloudflare 检测"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except PWTimeout:
        logger.warning(f"页面加载超时: {url}")
        return False
    except Exception as e:
        logger.warning(f"页面跳转异常: {e}")
        return False
    return wait_for_cloudflare_clear(page)


def wait_for_page_ready(page: Page, extra_ms: int = 0):
    """等待页面就绪：domcontentloaded + Cloudflare 清除 + 可选额外等待"""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PWTimeout:
        pass
    wait_for_cloudflare_clear(page)
    if extra_ms > 0:
        page.wait_for_timeout(extra_ms)


# ─── 截图调试 ───────────────────────────────────────────────

def save_debug_screenshot(page: Page, name: str):
    """保存调试截图（失败时用）"""
    try:
        page.screenshot(path=f"debug_{name}.png", full_page=True)
        logger.info(f"已保存调试截图: debug_{name}.png")
    except Exception:
        pass


# ─── 原有函数（保持不变） ───────────────────────────────────

def get_server_console_url() -> str:
    server_id = os.environ.get("RUSTIX_SERVERID", "").strip()
    if not server_id:
        raise RuntimeError("未配置 RUSTIX_SERVERID 环境变量")
    return f"https://my.rustix.me/server/{server_id}/console"


@retry(max_attempts=3, delay=2, exceptions=(Exception,))
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

    resp = requests.get(public_key_url, headers=headers, timeout=10)
    public_key_data = resp.json()
    if resp.status_code != 200:
        logger.warning(f"获取 GitHub Public Key 失败: {resp.status_code}")
        return False

    public_key = public_key_data.get("key", "")
    key_id = public_key_data.get("key_id", "")
    if not public_key or not key_id:
        logger.warning("获取到的 Public Key 不完整")
        return False
    logger.info(f"成功获取 GitHub Public Key (key_id={key_id})")

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
        logger.info("未配置 RUSTIX_COOKIE 环境变量")
        return []
    try:
        data = json.loads(cookie_env)
        if isinstance(data, dict) and email in data:
            logger.info(f"成功匹配到账号 {email} 的专属 Cookie")
            return data[email]
        if isinstance(data, list):
            logger.info("载入通用 Cookie 配置")
            return data
        if isinstance(data, dict) and "name" in data:
            logger.info("载入单条 Cookie")
            return [data]
    except json.JSONDecodeError as e:
        logger.warning(f"解析 RUSTIX_COOKIE 失败 (JSON 格式错误): {e}")
        logger.warning("建议重新获取 Cookie 并更新 Secret")
    except Exception as e:
        logger.warning(f"解析 RUSTIX_COOKIE 异常: {e}")
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
    """检测服务器是否在线"""
    try:
        # 方式1：精确匹配绿色 Online 状态标签
        status_spans = page.locator("span.text-success-50, span[class*='text-success']")
        count = status_spans.count()
        for i in range(count):
            text = (status_spans.nth(i).text_content() or "").strip().lower()
            if text == "online" or text == "запущен":
                logger.info("检测到精确状态标签: Online (绿色)")
                return True

        # 方式2：ServerCardGradient 绿色状态
        card_spans = page.locator("span[class*='ServerCardGradient']")
        count = card_spans.count()
        for i in range(count):
            text = (card_spans.nth(i).text_content() or "").strip().lower()
            if text == "online" or text == "запущен":
                logger.info("检测到 ServerCardGradient 状态: Online")
                return True

        # 方式3：InformationBar 绿色状态标签
        info_spans = page.locator('[class*="InformationBar"]')
        count = info_spans.count()
        for i in range(count):
            text = (info_spans.nth(i).text_content() or "").strip().lower()
            if text == "online" or text == "запущен":
                logger.info("检测到 InformationBar 状态: Online")
                return True

        # 方式4：按钮组合判断
        start_btn, _, _ = find_start_button(page)
        stop_btn, _, _ = find_stop_button(page)

        if start_btn and stop_btn:
            start_clickable = is_clickable(start_btn)
            stop_clickable = is_clickable(stop_btn)
            if start_clickable and not stop_clickable:
                return False

    except Exception as e:
        logger.warning(f"check_server_online 异常: {e}")
    return False


def check_dashboard_online(page: Page) -> bool:
    """跳转到总览页面检查服务器卡片状态"""
    try:
        if not safe_goto(page, HOME_URL):
            return False
        # 智能等待：等服务器卡片或状态标签出现
        smart_wait_for_elements(page, [
            'a[href*="/server/"][href*="/console"]',
            'span[class*="text-success"]',
            'span[class*="ServerCardGradient"]',
        ], timeout=SMART_WAIT)

        return check_server_online(page)
    except Exception as e:
        logger.warning(f"检查总览页面状态异常: {e}")
        return False


def check_server_offline(page: Page) -> bool:
    """检测服务器是否离线"""
    try:
        status_spans = page.locator("span.text-danger-50, span[class*='text-danger']")
        count = status_spans.count()
        for i in range(count):
            text = (status_spans.nth(i).text_content() or "").strip().lower()
            if text == "offline" or text == "выключен":
                logger.info("检测到精确状态标签: Offline")
                return True

        card_spans = page.locator("span[class*='ServerCardGradient']")
        count = card_spans.count()
        for i in range(count):
            text = (card_spans.nth(i).text_content() or "").strip().lower()
            if text == "offline" or text == "выключен":
                logger.info("检测到 ServerCardGradient 状态: Offline")
                return True

        start_btn, _, _ = find_start_button(page)
        stop_btn, _, _ = find_stop_button(page)

        if start_btn and stop_btn:
            start_clickable = is_clickable(start_btn)
            stop_clickable = is_clickable(stop_btn)
            if start_clickable and not stop_clickable:
                return True
            if not start_clickable and stop_clickable:
                return False
    except Exception:
        pass
    return False


def do_login(page: Page, email: str, password: str) -> bool:
    logger.info(f"打开登录页: {LOGIN_URL}")
    if not safe_goto(page, LOGIN_URL, timeout=60000):
        logger.warning("登录页加载失败，重试一次")
        time.sleep(3)
        if not safe_goto(page, LOGIN_URL, timeout=60000):
            logger.error("登录页重试仍失败")
            return False

    # 智能等待：等表单元素出现
    if not smart_wait_for_elements(page, [
        'input[name="username"]', 'input[type="email"]', 'input[name="email"]',
        'input[type="password"]', 'input[name="password"]',
    ], timeout=SMART_WAIT):
        save_debug_screenshot(page, "login_no_form")
        logger.error("登录页未找到表单元素")
        return False

    email_loc, _ = find_first_visible(page, [
        'input[name="username"]', 'input[type="email"]', 'input[name="email"]'
    ])
    pwd_loc, _ = find_first_visible(page, [
        'input[type="password"]', 'input[name="password"]'
    ])

    if not email_loc or not pwd_loc:
        save_debug_screenshot(page, "login_form_missing")
        logger.error("未找到登录表单（元素不可见）")
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
        save_debug_screenshot(page, "login_no_btn")
        logger.error("未找到登录按钮")
        return False

    logger.info(f"点击登录按钮: {txt}")
    try:
        login_btn.click()
    except Exception:
        try:
            login_btn.first.click(force=True)
        except Exception:
            login_btn.first.evaluate("el => el.click()")

    # 智能等待：等 URL 变化（离开登录页）或错误提示出现
    logger.info("等待登录结果...")
    try:
        page.wait_for_function(
            """() => {
                const url = window.location.href;
                if (!url.includes('/auth/login')) return true;
                const text = document.body.innerText || '';
                const keywords = ['incorrect', 'invalid', 'неверн', 'ошибк', 'error', 'failed'];
                return keywords.some(k => text.toLowerCase().includes(k));
            }""",
            timeout=20000,
        )
    except PWTimeout:
        logger.warning("等待登录结果超时")

    page.wait_for_timeout(STEP_SHORT)

    if "/auth/login" in page.url:
        body = (page.inner_text("body") or "")[:500].lower()
        if any(k in body for k in ["incorrect", "invalid", "неверн", "ошибк"]):
            logger.error("登录失败：账号或密码错误")
            return False
        # 可能是 Cloudflare 还没过
        if is_cloudflare_challenge(page):
            logger.info("登录后遇到 Cloudflare，等待验证通过...")
            if wait_for_cloudflare_clear(page, max_wait=30):
                if "/auth/login" not in page.url:
                    logger.info("登录成功（Cloudflare 后）")
                    return True
        save_debug_screenshot(page, "login_still_on_login")
        logger.error("登录后仍在登录页")
        return False

    logger.info("登录成功")
    return True


def navigate_to_console(page: Page) -> bool:
    console_url = get_server_console_url()
    logger.info(f"直接跳转到控制台页面: {console_url}")

    if not safe_goto(page, console_url, timeout=60000):
        logger.warning("控制台页面加载失败，重试一次")
        time.sleep(3)
        if not safe_goto(page, console_url, timeout=60000):
            logger.error("控制台页面重试仍失败")
            return False

    # 智能等待：等路由跳转完成或控制台内容出现
    try:
        page.wait_for_function(
            """() => {
                const url = window.location.href;
                if (url.includes('/server/') && url.includes('/console')) return true;
                const text = document.body.innerText || '';
                return text.includes('Start') || text.includes('Stop') ||
                       text.includes('Online') || text.includes('Offline') ||
                       text.includes('Запустить') || text.includes('Остановить');
            }""",
            timeout=CONSOLE_LOAD_WAIT,
        )
        logger.info(f"路由跳转成功: {page.url}")
    except PWTimeout:
        logger.warning(f"等待路由超时，当前 URL: {page.url}")

    page.wait_for_timeout(STEP_MEDIUM)
    return True


def wait_for_status_change(page: Page, expected_status: str, timeout: int = SMART_WAIT) -> bool:
    """智能等待页面状态变化到指定状态"""
    js_checks = {
        "online": """() => {
            const text = document.body.innerText || '';
            return text.includes('Online') || text.includes('Запущен');
        }""",
        "start_button_ready": """() => {
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const t = (btn.textContent || '').toLowerCase();
                if (t.includes('start') || t.includes('запустить')) {
                    if (!btn.disabled && btn.getAttribute('aria-disabled') !== 'true') return true;
                }
            }
            return false;
        }""",
    }
    js = js_checks.get(expected_status)
    if not js:
        return True

    try:
        page.wait_for_function(js, timeout=timeout)
        return True
    except PWTimeout:
        return False


def start_server(page: Page, console_lines: list, email: str) -> str:
    """
    返回状态字符串：
      - "started"  成功启动并验证
      - "online"   服务器已在线（无需操作）
      - "offline"  服务器离线且启动失败
      - "no_start" 未找到 start 按钮
    """
    logger.info("等待控制台页面渲染...")
    # 智能等待：等控制台内容出现
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
    except PWTimeout:
        logger.warning("等待渲染超时，继续尝试")
        page.wait_for_timeout(STEP_MEDIUM)

    # 初始状态检测
    start_btn_initial, _, _ = find_start_button(page)
    stop_btn_initial, _, _ = find_stop_button(page)
    if start_btn_initial and stop_btn_initial:
        s_click = is_clickable(start_btn_initial)
        st_click = is_clickable(stop_btn_initial)
        logger.info(f"初始状态: Start可点击={s_click}, Stop可点击={st_click}")
        if not s_click and st_click:
            logger.info("服务器已处于 Online 状态，无需启动")
            return "online"
        if s_click and not st_click:
            logger.info("检测到服务器处于 Offline 状态")

    if check_server_online(page):
        logger.info("服务器已处于 Online 状态，无需启动")
        return "online"

    if check_server_offline(page):
        logger.info("检测到服务器处于 Offline 状态")

    logger.info("寻找 start 按钮")
    # 智能等待按钮出现
    if not wait_for_status_change(page, "start_button_ready", timeout=SMART_WAIT):
        logger.warning("等待 Start 按钮超时，尝试直接查找")

    start_btn, sel, txt = find_start_button(page)
    if not start_btn:
        save_debug_screenshot(page, "no_start_button")
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
        start_btn.click()
        start_clicked = True
        logger.info("Start 按钮已点击（方式1）")
    except Exception as e:
        logger.warning(f"普通点击失败: {e}")
        try:
            start_btn.first.click(force=True)
            start_clicked = True
            logger.info("Start 按钮已点击（方式2: force）")
        except Exception as e2:
            logger.warning(f"force 点击也失败: {e2}")
            try:
                start_btn.first.evaluate("el => el.click()")
                start_clicked = True
                logger.info("Start 按钮已点击（方式3: JS）")
            except Exception as e3:
                logger.error(f"所有点击方式都失败: {e3}")

    if not start_clicked:
        save_debug_screenshot(page, "click_start_failed")
        logger.error("无法点击 Start 按钮")
        return "offline"

    # 点击后等待启动指令发出
    page.wait_for_timeout(STEP_MEDIUM)
    logger.info("启动指令已发出，跳转到总览页面监控服务器状态...")

    # 跳转到总览页面
    if not safe_goto(page, HOME_URL):
        logger.warning("跳转总览页失败，重试")
        page.wait_for_timeout(STEP_MEDIUM)
        safe_goto(page, HOME_URL)

    # 智能等待：等服务器卡片出现
    smart_wait_for_elements(page, [
        'a[href*="/server/"][href*="/console"]',
        'span[class*="text-success"]',
        'span[class*="text-danger"]',
    ], timeout=SMART_WAIT)

    logger.info(f"等待服务器上线中（最长 {START_WAIT_TIMEOUT}s）")
    deadline = time.time() + START_WAIT_TIMEOUT
    detected = False
    last_refresh = time.time()
    starting_detected = False
    refresh_count = 0

    while time.time() < deadline:
        # 方式1：检测绿色 Online 状态
        try:
            status_spans = page.locator("span.text-success-50, span[class*='text-success']")
            count = status_spans.count()
            for i in range(count):
                text = (status_spans.nth(i).text_content() or "").strip().lower()
                if text == "online" or text == "запущен":
                    logger.info("总览页面检测到绿色 Online 状态，服务器启动成功！")
                    detected = True
                    break
        except Exception:
            pass
        if detected:
            break

        # 方式2：检测黄色 Starting 状态
        if not starting_detected:
            try:
                yellow_spans = page.locator("span.text-yellow-50, span[class*='text-yellow']")
                count = yellow_spans.count()
                for i in range(count):
                    text = (yellow_spans.nth(i).text_content() or "").strip().lower()
                    if "start" in text or "запуск" in text:
                        logger.info(f"总览页面检测到启动中状态: {text}")
                        starting_detected = True
                        break
            except Exception:
                pass

        # 方式3：ServerCardGradient 状态标签
        try:
            card_spans = page.locator("span[class*='ServerCardGradient']")
            count = card_spans.count()
            for i in range(count):
                text = (card_spans.nth(i).text_content() or "").strip().lower()
                if text == "online" or text == "запущен":
                    logger.info("ServerCardGradient 检测到 Online，服务器启动成功！")
                    detected = True
                    break
        except Exception:
            pass
        if detected:
            break

        # 定期刷新页面（每15秒）
        if time.time() - last_refresh >= 15:
            refresh_count += 1
            elapsed = int(time.time() - (deadline - START_WAIT_TIMEOUT))
            logger.info(f"[第{refresh_count}次刷新] 已等待 {elapsed}s，刷新总览页面检查状态...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                # 智能等待页面渲染完
                wait_for_cloudflare_clear(page)
                smart_wait_for_elements(page, [
                    'span[class*="text-success"]',
                    'span[class*="text-danger"]',
                    'span[class*="ServerCardGradient"]',
                    'a[href*="/server/"]',
                ], timeout=SMART_WAIT)
            except Exception as e:
                logger.warning(f"刷新异常: {e}")
            last_refresh = time.time()

        page.wait_for_timeout(STEP_MEDIUM)

    if detected:
        logger.info("服务器已成功上线（总览页面确认）")
        console_url = get_server_console_url()
        safe_goto(page, console_url)
        page.wait_for_timeout(STEP_MEDIUM)
        return "started"
    else:
        logger.warning(f"等待超时（{START_WAIT_TIMEOUT}s），服务器未能上线")

    # 最终验证
    console_url = get_server_console_url()
    safe_goto(page, console_url)
    page.wait_for_timeout(STEP_MEDIUM)

    info_spans = page.locator('[class*="InformationBar"]')
    count = info_spans.count()
    for i in range(count):
        text = (info_spans.nth(i).text_content() or "").strip().lower()
        if text == "online" or text == "запущен":
            logger.info("控制台页面确认服务器已在线")
            return "started"

    save_debug_screenshot(page, "final_check_failed")
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
                if not safe_goto(page, HOME_URL):
                    logger.warning("Cookie 登录：首页加载失败")
                else:
                    # 智能等待：等服务器卡片出现或被重定向到登录页
                    try:
                        page.wait_for_function(
                            """() => {
                                const url = window.location.href;
                                if (url.includes('/auth/login')) return 'login';
                                const links = document.querySelectorAll('a[href*="/server/"][href*="/console"]');
                                if (links.length > 0) return 'cards';
                                const btns = document.querySelectorAll('button');
                                for (const b of btns) {
                                    const t = (b.textContent || '').toLowerCase();
                                    if (t.includes('manage')) return 'manage';
                                }
                                return null;
                            }""",
                            timeout=SMART_WAIT,
                        )
                    except PWTimeout:
                        logger.warning("Cookie 登录：等待页面元素超时")

                    page.wait_for_timeout(STEP_SHORT)

                    if "/auth/login" not in page.url:
                        # 再次检查
                        manage, _, _ = find_button_by_text(page, ["Manage Server", "Manage", "Управление"])
                        server_link, _, _ = find_first_visible(page, ['a[href*="/server/"][href*="/console"]'])
                        if server_link or manage:
                            logger.info("Cookie 验证成功！")
                            cookie_login_success = True
                        else:
                            logger.warning("Cookie 登录验证未通过（无服务器元素）")
                    else:
                        logger.warning("Cookie 已过期，被重定向到登录页")
                        # Cookie 过期是常见问题，给出明确提示
                        logger.info("💡 提示: Cookie 过期，请更新 RUSTIX_COOKIE Secret")

            except Exception as e:
                logger.warning(f"Cookie 登录异常，切换密码登录: {e}")

        if not cookie_login_success:
            logger.info("尝试账号密码登录...")
            if not do_login(page, email, password):
                result["error"] = "登录失败"
                save_debug_screenshot(page, "login_failed")
                return result
            logger.info("密码登录成功，跳转服务器总览页面...")
            if not safe_goto(page, HOME_URL):
                logger.warning("跳转首页失败")
            # 智能等待服务器列表
            smart_wait_for_elements(page, [
                'a[href*="/server/"][href*="/console"]',
            ], timeout=SMART_WAIT)
            page.wait_for_timeout(STEP_SHORT)

        logger.info("已成功登录主面板！")
        save_cookies(context)

        if not navigate_to_console(page):
            result["error"] = "跳转到控制台失败"
            save_debug_screenshot(page, "navigate_failed")
            return result

        status = start_server(page, console_lines, email)
        result["status"] = status
        result["ok"] = status in ("started", "online")
        if status == "offline":
            save_debug_screenshot(page, "start_failed_offline")
        return result

    except Exception as e:
        result["error"] = f"异常: {e}"
        logger.exception("处理账号时发生异常")
        try:
            save_debug_screenshot(page, "exception")
        except Exception:
            pass
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
                time.sleep(8)  # 多账号间隔加大到8秒，给浏览器更多释放时间

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
