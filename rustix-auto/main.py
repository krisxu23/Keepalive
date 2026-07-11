#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动启动脚本
- 支持多账号轮流操作
- 优先支持 Cookie 登录 (RUSTIX_COOKIE)，失效或未配置时自动降级至账号密码登录
- 自动登录 https://my.rustix.me/auth/login
- 通过服务器ID直接跳转控制台页面
- 自动刷新保存 Cookie 到 GitHub Repository Secrets
- 仅发送汇总通知

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
START_WAIT_TIMEOUT = 120
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
        logger.warning("未获取到 GITHUB_REPOSITORY 环境变量，跳过更新 GitHub Secret")
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
            logger.warning(f"获取 GitHub Public Key 失败，状态码: {resp.status_code}, 响应: {public_key_data}")
            return False

        public_key = public_key_data.get("key", "")
        key_id = public_key_data.get("key_id", "")
        if not public_key or not key_id:
            logger.warning(f"获取到的 Public Key 不完整: {public_key_data}")
            return False
        logger.info(f"成功获取 GitHub Public Key (key_id={key_id})")
    except Exception as e:
        logger.warning(f"获取 GitHub Public Key 时出现异常: {e}")
        return False

    try:
        import base64
        from nacl import public, encoding, utils

        public_key_obj = public.PublicKey(
            public_key.encode("utf-8"),
            encoding.Base64Encoder()
        )
        sealed_box = public.SealedBox(public_key_obj)
        encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
        encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

        payload = {
            "encrypted_value": encrypted_b64,
            "key_id": key_id,
        }
        resp = requests.put(secret_url, headers=headers, json=payload, timeout=10)
        if resp.status_code in (201, 204):
            logger.info(f"成功更新 GitHub Secret: {secret_name}")
            return True
        else:
            logger.warning(f"更新 GitHub Secret 失败，状态码: {resp.status_code}, 响应: {resp.text}")
            return False
    except Exception as e:
        logger.warning(f"加密并更新 GitHub Secret 时出现异常: {e}")
        return False


def save_cookies(context) -> bool:
    try:
        cookies = context.cookies()
        if not cookies:
            logger.info("未获取到任何 Cookie")
            return False

        cookie_json = json.dumps(cookies, indent=2)
        logger.info(f"获取到 {len(cookies)} 个 Cookie，准备更新到 GitHub Secret")

        return update_github_secret("RUSTIX_COOKIE", cookie_json)
    except Exception as e:
        logger.warning(f"获取并保存 Cookie 时出现异常: {e}")
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

    raise RuntimeError(
        "未配置账号：请设置环境变量 ACCOUNTS（格式 email:password,...）或创建 accounts.json"
    )


def load_cookies_for_account(email: str) -> list:
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
        if el.evaluate("el => getComputedStyle(el).pointerEvents") == "none":
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


def find_button_by_text_robust(page: Page, target_texts: list):
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


def do_login(page: Page, email: str, password: str) -> bool:
    logger.info(f"打开登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        logger.warning("页面加载超时，继续尝试")

    page.wait_for_timeout(LOGIN_PAGE_WAIT)

    email_loc, email_sel = find_first_visible(page, [
        'input[name="username"]',
        'input[type="email"]',
        'input[name="email"]',
        'input[autocomplete="username"]',
    ])
    pwd_loc, pwd_sel = find_first_visible(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
    ])

    if not email_loc or not pwd_loc:
        logger.error("未找到登录表单")
        return False

    logger.info(f"填写账号: {email}")
    email_loc.fill(email)
    pwd_loc.fill(password)
    page.wait_for_timeout(500)

    login_btn, login_sel, txt = find_button_by_text_robust(page, [
        "Войти",
        "Login",
        "Sign in",
    ])
    if not login_btn:
        login_btn, login_sel = find_first_visible(page, [
            'button[type="submit"]',
            'input[type="submit"]',
        ])
        txt = "submit(fallback)"

    if not login_btn:
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


def navigate_to_console(page: Page) -> bool:
    console_url = get_server_console_url()
    logger.info(f"直接跳转到控制台页面: {console_url}")

    try:
        page.goto(console_url, wait_until="domcontentloaded", timeout=60000)
        logger.info(f"控制台页面加载完成，当前 URL: {page.url}")
    except Exception as e:
        logger.warning(f"跳转控制台页面时出现异常: {e}")

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


def start_server(page: Page, console_lines: list, email: str) -> str:
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
        logger.error("未找到 start 按钮")
        return "no_start"

    clickable = is_clickable(start_btn)
    logger.info(f"start 按钮可点击状态: {clickable}")

    if not clickable:
        logger.info("start 按钮不可点击 -> 检查服务器是否已经是在线状态...")
        page_text = page.locator("body").text_content() or ""
        is_online_text = "online" in page_text.lower() or "запущен" in page_text.lower()
        stop_status = check_stop_button(page)

        if is_online_text or stop_status == "clickable":
            logger.info("确认：服务器已在线，无需启动。")
            return "online"
        else:
            logger.warning("虽然 start 按钮不可点击，但没有检测到明确的 Online 状态。")
            return "online"

    logger.info("服务器目前处于离线状态，点击 start 启动")
    try:
        start_btn.click()
    except Exception:
        start_btn.first.click(force=True)

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

    page.wait_for_timeout(STEP_WAIT)
    if check_stop_button(page) == "clickable":
        logger.info("验证成功：stop 按钮可点击，服务器已在线运行。")
        return "started"

    logger.warning("验证未通过：stop 按钮不可点击")
    return "offline"


def check_stop_button(page: Page) -> str:
    stop_btn, sel, txt = find_button_by_text_robust(page, [
        "Stop",
        "Остановить",
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
            if any(k in low for k in ["app is running", "error", "started", "running"]):
                logger.info(f"[console] {text}")

        page.on("console", on_console)
        page.on("pageerror", lambda err: logger.warning(f"[pageerror] {err}"))

        cookies = load_cookies_for_account(email)
        cookie_login_success = False

        if cookies:
            logger.info("检测到 RUSTIX_COOKIE 配置，尝试通过 Cookie 导入登录...")
            try:
                for c in cookies:
                    if "domain" not in c:
                        c["domain"] = "my.rustix.me"
                context.add_cookies(cookies)

                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(STEP_WAIT)

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

        if not cookie_login_success:
            logger.info("尝试使用传统账号密码方式登录...")
            if not do_login(page, email, password):
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

        logger.info("已成功登录主面板！")
        save_cookies(context)

        if not navigate_to_console(page):
            result["error"] = "跳转到控制台页面失败"
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
