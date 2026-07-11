#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 通知组件

通过 Telegram Bot API 推送运行结果通知，使用标准库 urllib，无需额外依赖。

配置环境变量：
- TG_BOT_TOKEN: Bot Token（从 @BotFather 获取）
- TG_CHAT_ID:   接收通知的 chat id（从 @userinfobot 获取，或群组 id，负数）

未配置时自动跳过，不影响主流程。
"""

import html
import json
import os
import logging
import urllib.request
from datetime import datetime

logger = logging.getLogger("rustix-auto.tg")

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# 运行状态 -> (emoji, 中文描述)
STATUS_MAP = {
    "started":  ("✅", "成功启动"),
    "online":   ("🟢", "已在线"),
    "offline":  ("❌", "启动失败"),
    "no_start": ("⚠️", "未找到Start按钮"),
    "unknown":  ("❓", "未知"),
}


def tg_enabled() -> bool:
    """是否已配置 Telegram 通知。"""
    return bool(
        os.environ.get("TG_BOT_TOKEN", "").strip()
        and os.environ.get("TG_CHAT_ID", "").strip()
    )


def _send(text: str) -> bool:
    """发送一条 HTML 格式的 Telegram 消息。失败时仅记录日志，不抛异常。"""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    url = API_BASE.format(token=token, method="sendMessage")
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            if data.get("ok"):
                logger.info("Telegram 通知发送成功")
                return True
            logger.warning(f"Telegram 返回错误: {data.get('description')}")
            return False
    except Exception as e:
        logger.warning(f"Telegram 通知发送失败: {e}")
        return False


def _status_text(status: str) -> str:
    emoji, desc = STATUS_MAP.get(status, ("❓", status))
    return f"{emoji} {desc}"


def _esc(text: str) -> str:
    """转义 HTML 特殊字符，防止破坏消息格式。"""
    return html.escape(str(text), quote=False)


def _mask_email(email: str) -> str:
    """脱敏邮箱：将 @ 前的本地名替换为等量 *，@ 后域名保留。"""
    if "@" in str(email):
        local, domain = str(email).split("@", 1)
        return "*" * len(local) + "@" + domain
    return str(email)


def notify_account_result(result: dict) -> bool:
    """单账号处理结果通知。"""
    email = result.get("email", "?")
    status = result.get("status", "unknown")
    ok = result.get("ok", False)
    err = (result.get("error") or "").strip()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    flag = "✅ 成功" if ok else "❌ 失败"
    lines = [
        "🤖 <b>Rustix 自动启动通知</b>",
        "",
        f"👤 <b>账号</b>: <code>{_esc(_mask_email(email))}</code>",
        f"📊 <b>状态</b>: {_status_text(status)}",
        f"🎯 <b>结果</b>: {flag}",
    ]
    if err:
        lines.append(f"⚠️ <b>错误</b>: <code>{_esc(err)}</code>")
    lines += [
        f"⏰ <b>时间</b>: {now}",
        "",
        "━" * 18,
        '🔗 <a href="https://my.rustix.me">前往控制台</a>',
    ]
    return _send("\n".join(lines))


def notify_summary(results: list) -> bool:
    """批量执行汇总通知。
    
    只在以下情况发送通知：
    - 有账号成功启动（之前离线，脚本唤醒成功）
    - 有账号失败（登录失败、启动失败等）
    - 所有账号都在线时不发送通知
    """
    total = len(results)
    ok = sum(1 for r in results if r.get("ok"))
    fail = total - ok
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    online_count = sum(1 for r in results if r.get("status") == "online")
    started_count = sum(1 for r in results if r.get("status") == "started")
    has_failures = fail > 0

    if online_count == total and not has_failures:
        logger.info("所有账号均已在线，无需发送通知")
        return False

    if fail == 0:
        overall = "🎉 全部成功"
    elif ok > 0:
        overall = "⚠️ 部分失败"
    else:
        overall = "❌ 全部失败"

    lines = [
        "📊 <b>Rustix 批量执行汇总</b>",
        "",
        f"🚩 <b>总体</b>: {overall}",
        f"⏰ <b>时间</b>: {now}",
        f"📈 <b>统计</b>: 共 {total} 个",
        f"✅ <b>成功</b> {ok} | ❌ <b>失败</b> {fail}",
    ]
    
    if started_count > 0:
        lines.append(f"🔄 <b>已唤醒</b> {started_count} 台")

    lines += [
        "",
        "━" * 13,
        "<b>账号明细</b>",
    ]
    num_emoji = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
                 "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, r in enumerate(results):
        idx = num_emoji[i] if i < len(num_emoji) else f"<b>{i + 1}.</b>"
        email = r.get("email", "?")
        status = r.get("status", "unknown")
        ok_r = r.get("ok", False)
        line = f"{idx} <code>{_esc(_mask_email(email))}</code>\n    {_status_text(status)}"
        err = (r.get("error") or "").strip()
        if err and not ok_r:
            line += f" ｜ <code>{_esc(err[:40])}</code>"
        lines.append(line)
    lines += [
        "━" * 13,
        '🔗 <a href="https://my.rustix.me">前往控制台</a>',
    ]
    return _send("\n".join(lines))
