#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动启动脚本
- 支持多账号轮流操作
- 优先支持 Cookie 登录 (RUSTIX_COOKIE)，失效或未配置时自动降级至账号密码登录
- 通过 Manage Server -> 判断 start 按钮状态 -> 启动服务器
- 监听浏览器控制台 "Running Done!" 确认上线
- 通过 stop 按钮可点击状态验证（不点击 stop）
- 脚本末尾自动清理旧 workflow 运行记录

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
START_WAIT_TIMEOUT = 180
STEP_WAIT = 3000
LOGIN_PAGE_WAIT = 6000


# ---------------- GitHub Secret 更新 ----------------
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
        if resp.status_code != 200:
            logger.warning(f"获取 GitHub Public Key 失败: {resp.status_code}")
            return False
        public_key_data = resp.json()
        public_key = public_key_data.get("key", "")
        key_id = public_key_data.get("key_id", "")
        if not public_key or not key_id:
            logger.warning("获取到的 Public Key 不完整")
            return False
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


# ---------------- 账号加载 ----------------
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

    raise RuntimeError("未配置账号：请设置环境变量 ACCOUNTS（格式 email:password,...）或创建 accounts.json")


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
    except Exception as e:
        logger.warning(f"解析 RUSTIX_COOKIE 异常: {e}")
    return []


# ---------------- 通用辅助 ----------------
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
    # 降级：用 Playwright 的 getByPlaceholder / getByRole 直接查找
    placeholders = {
        "почта": ("placeholder", "почта"),
        "username": ("placeholder", "username"),
        "email": ("placeholder", "email"),
        "password": ("placeholder", "password"),
        "пароль": ("placeholder", "пароль"),
    }
    return None, None


def find_input_by_placeholder(page: Page, keywords: list):
    """通过 placeholder 关键词查找输入框，支持俄语/英语。"""
    for kw in keywords:
        try:
            loc = page.get_by_placeholder(kw, exact=False).first
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def find_button_by_text(page: Page, texts):
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


def find_start_button(page: Page):
    return find_button_by_text(page, ["Start", "Запустить", "Power On", "Boot"])


def find_stop_button(page: Page):
    return find_button_by_text(page, ["Stop", "Остановить", "Power Off", "Shut down", "Shutdown"])


def check_server_online(page: Page) -> bool:
    try:
        status_spans = page.locator("span.text-success-50, span[class*='text-success']")
        count = status_spans.count()
        for i in range(count):
            text = (status_spans.nth(i).text_content() or "").strip().lower()
            if text in ("online", "запущен"):
                return True

        card_spans = page.locator("span[class*='ServerCardGradient']")
        count = card_spans.count()
        for i in range(count):
            text = (card_spans.nth(i).text_content() or "").strip().lower()
            if text in ("online", "запущен"):
                return True

        start_btn, _, _ = find_start_button(page)
        stop_btn, _, _ = find_stop_button(page)
        if start_btn and stop_btn:
            if not is_clickable(start_btn) and is_clickable(stop_btn):
                return True
    except Exception:
        pass
    return False


# ---------------- 登录流程 ----------------
def do_login(page: Page, email: str, password: str) -> bool:
    logger.info(f"打开登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        logger.warning("页面加载超时，继续尝试")

    page.wait_for_timeout(LOGIN_PAGE_WAIT)

    # 优先用 placeholder 查找（Rustix 登录框是俄语 placeholder）
    email_loc = find_input_by_placeholder(page, ["почта", "username", "email", "user"])
    pwd_loc = find_input_by_placeholder(page, ["пароль", "password"])

    # 降级用 CSS 选择器
    if not email_loc:
        email_loc, email_sel = find_first_visible(page, [
            'input[name="username"]',
            'input[type="email"]',
            'input[name="email"]',
            'input[autocomplete="username"]',
        ])
    if not pwd_loc:
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

    login_btn, login_sel, txt = find_button_by_text(page, ["Войти", "Login", "Sign in"])
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

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        logger.warning("登录后 networkidle 超时，继续流程")
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
    logger.info("寻找 Manage Server 按钮")
    page.wait_for_timeout(STEP_WAIT)

    manage, sel, txt = find_button_by_text(page, [
        "Manage Server",
        "Manage",
        "Управление",
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
    page.wait_for_timeout(6000)
    return True


# ---------------- 启动服务器流程 ----------------
def start_server(page: Page, console_lines: list) -> str:
    logger.info("寻找 start 按钮")
    page.wait_for_timeout(STEP_WAIT)

    try:
        page.wait_for_selector('button:has-text("Start")', timeout=15000)
    except PWTimeout:
        pass

    start_btn, sel, txt = find_start_button(page)
    if not start_btn:
        page.screenshot(path=f"debug_start_{int(time.time())}.png")
        logger.error("未找到 start 按钮")
        return "no_start"

    clickable = is_clickable(start_btn)
    logger.info(f"start 按钮可点击状态: {clickable}")

    if not clickable:
        logger.info("start 按钮不可点击 -> 服务器可能已在线，跳过启动")
        if check_server_online(page):
            logger.info("确认服务器已在线")
        return "online"

    logger.info("服务器离线，点击 start 启动")
    try:
        start_btn.click()
    except Exception:
        start_btn.first.click(force=True)

    # 策略：先等 Running Done!，同时轮询页面状态，两者任一成功即可
    logger.info(f"等待服务器启动（最长 {START_WAIT_TIMEOUT}s）")
    deadline = time.time() + START_WAIT_TIMEOUT
    detected = False
    while time.time() < deadline:
        # 方法1：检查控制台日志
        if any("Running Done!" in line for line in console_lines):
            detected = True
            logger.info("检测到控制台 'Running Done!'")
            break

        # 方法2：检查页面文本
        try:
            if page.locator(":text('Running Done!')").count() > 0:
                detected = True
                logger.info("检测到页面 'Running Done!' 文本")
                break
        except Exception:
            pass

        # 方法3：检查 start 按钮变回不可点击 + stop 按钮可点击
        try:
            s_btn, _, _ = find_start_button(page)
            st_btn, _, _ = find_stop_button(page)
            if s_btn and st_btn:
                if not is_clickable(s_btn) and is_clickable(st_btn):
                    detected = True
                    logger.info("检测到状态变化: start 不可点击 + stop 可点击")
                    break
        except Exception:
            pass

        # 方法4：检查页面上是否出现 Online 状态文本
        try:
            body_text = (page.inner_text("body") or "").lower()
            if "online" in body_text and ("status" in body_text or "состояние" in body_text):
                # 进一步确认不是残留文本
                if check_server_online(page):
                    detected = True
                    logger.info("检测到 Online 状态文本")
                    break
        except Exception:
            pass

        page.wait_for_timeout(3000)

    if detected:
        logger.info("服务器启动成功")
    else:
        logger.warning(f"等待 {START_WAIT_TIMEOUT}s 超时，尝试最终验证")

    # 最终验证：等几秒后再次检查 stop 按钮
    page.wait_for_timeout(STEP_WAIT)
    if check_stop_button(page) == "clickable":
        logger.info("验证成功：stop 按钮可点击，服务器已上线")
        return "started"

    # 再等一次，给服务器更多时间
    logger.info("再次等待 30 秒后验证...")
    page.wait_for_timeout(30000)
    if check_stop_button(page) == "clickable":
        logger.info("延迟验证成功：服务器已上线")
        return "started"

    logger.warning("验证未通过：服务器可能仍在启动中")
    return "offline"


def check_stop_button(page: Page) -> str:
    stop_btn, sel, txt = find_stop_button(page)

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


# ---------------- 跳转到控制台（Cookie 登录用） ----------------
def navigate_to_console(page: Page) -> bool:
    server_id = os.environ.get("RUSTIX_SERVERID", "").strip()
    if not server_id:
        logger.info("未配置 RUSTIX_SERVERID，跳过直接跳转")
        return False

    console_url = f"https://my.rustix.me/server/{server_id}/console"
    logger.info(f"直接跳转到控制台页面: {console_url}")
    try:
        page.goto(console_url, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        logger.warning("控制台页面加载超时")
    except Exception as e:
        logger.warning(f"跳转异常: {e}")

    try:
        page.wait_for_url(lambda url: "/server/" in url and "/console" in url, timeout=15000)
        logger.info(f"路由跳转成功: {page.url}")
    except Exception:
        logger.warning(f"等待路由超时，当前 URL: {page.url}")

    page.wait_for_timeout(STEP_WAIT)
    return True


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

        console_lines = []

        def on_console(msg):
            text = msg.text or ""
            console_lines.append(text)
            low = text.lower()
            if any(k in low for k in ["running done", "app is running", "error", "started"]):
                logger.info(f"[console] {text[:200]}")

        page.on("console", on_console)
        page.on("pageerror", lambda err: logger.warning(f"[pageerror] {err}"))

        # === 第一选择：账号密码登录 ===
        password_login_success = False
        logger.info("尝试账号密码登录（第一选择）...")
        if do_login(page, email, password):
            password_login_success = True
            # 登录成功后立即更新 Cookie 到 GitHub Secrets
            logger.info("登录成功，正在更新 RUSTIX_COOKIE...")
            save_cookies(context)
        else:
            logger.warning("密码登录失败")

        # === 第二选择：Cookie 登录（密码失败时降级） ===
        cookie_login_success = False
        if not password_login_success:
            cookies = load_cookies_for_account(email)
            if cookies:
                logger.info("密码登录失败，降级尝试 Cookie 登录...")
                try:
                    for c in cookies:
                        if "domain" not in c:
                            c["domain"] = "my.rustix.me"
                    context.add_cookies(cookies)
                    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(6000)

                    if "/auth/login" not in page.url:
                        server_link, _ = find_first_visible(page, ['a[href*="/server/"][href*="/console"]'])
                        manage_btn, _, _ = find_button_by_text(page, ["Manage Server", "Manage", "Управление"])
                        if server_link or manage_btn:
                            logger.info("Cookie 验证成功！")
                            cookie_login_success = True
                        else:
                            logger.warning("Cookie 登录验证未通过（无服务器元素）")
                    else:
                        logger.warning("Cookie 已过期，被重定向到登录页")
                except Exception as e:
                    logger.warning(f"Cookie 登录异常: {e}")

        if not password_login_success and not cookie_login_success:
            result["error"] = "密码登录和 Cookie 登录均失败"
            return result

        # === 导航到服务器管理页面 ===
        if cookie_login_success and navigate_to_console(page):
            # Cookie 降级登录 + 直接跳转控制台
            status = start_server(page, console_lines)
        else:
            # 标准流程：Manage Server -> 控制台
            if not click_manage_server(page):
                result["error"] = "未找到 Manage Server"
                return result
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


# ---------------- Workflow 清理 ----------------
def cleanup_old_workflow_runs(keep_runs: int = 1):
    gh_token = os.environ.get("GH_TOKEN", "").strip()
    repo_full_name = os.environ.get("GITHUB_REPOSITORY", "").strip()

    if not gh_token or not repo_full_name:
        logger.info("未检测到 GH_TOKEN 或 GITHUB_REPOSITORY，跳过 workflow 清理")
        return

    api_base = f"https://api.github.com/repos/{repo_full_name}"
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    logger.info("========== 开始清理旧 Workflow 运行记录 ==========")

    try:
        resp = requests.get(f"{api_base}/actions/workflows", headers=headers, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"获取 workflows 失败: {resp.status_code}")
            return
        workflows = resp.json().get("workflows", [])
        logger.info(f"仓库共 {len(workflows)} 个 workflow")
    except Exception as e:
        logger.warning(f"获取 workflows 异常: {e}")
        return

    total_deleted = 0
    total_failed = 0

    for wf in workflows:
        wf_id = wf.get("id")
        wf_name = wf.get("name", "unknown")
        wf_state = wf.get("state", "unknown")

        if not wf_id:
            continue
        if wf_state != "active":
            logger.info(f"  跳过未启用的 workflow: {wf_name} ({wf_state})")
            continue

        try:
            page = 1
            all_run_ids = []
            while True:
                runs_resp = requests.get(
                    f"{api_base}/actions/workflows/{wf_id}/runs",
                    headers=headers,
                    params={"per_page": 100, "page": page, "status": "completed"},
                    timeout=15,
                )
                if runs_resp.status_code != 200:
                    break
                runs = runs_resp.json().get("workflow_runs", [])
                if not runs:
                    break
                for r in runs:
                    all_run_ids.append(r.get("id"))
                if len(runs) < 100:
                    break
                page += 1

            if len(all_run_ids) <= keep_runs:
                logger.info(f"  ✓ {wf_name}: {len(all_run_ids)} 条记录，无需清理")
                continue

            to_delete = all_run_ids[keep_runs:]
            logger.info(f"  🗑 {wf_name}: 共 {len(all_run_ids)} 条，保留 {keep_runs} 条，删除 {len(to_delete)} 条")

            deleted = 0
            for run_id in to_delete:
                try:
                    del_resp = requests.delete(
                        f"{api_base}/actions/runs/{run_id}",
                        headers=headers,
                        timeout=10,
                    )
                    if del_resp.status_code in (204, 200):
                        deleted += 1
                    else:
                        total_failed += 1
                except Exception:
                    total_failed += 1
                time.sleep(0.3)

            total_deleted += deleted
            logger.info(f"    已删除 {deleted}/{len(to_delete)} 条")

        except Exception as e:
            logger.warning(f"  清理 {wf_name} 时异常: {e}")
            total_failed += 1

    logger.info(f"========== Workflow 清理完成: 删除 {total_deleted} 条, 失败 {total_failed} 条 ==========")


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

    try:
        cleanup_old_workflow_runs(keep_runs=1)
    except Exception as e:
        logger.warning(f"workflow 清理异常: {e}")

    sys.exit(0 if ok == len(results) and ok > 0 else 1)


if __name__ == "__main__":
    main()
