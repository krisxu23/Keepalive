"""Telegram 通知模块。

读取环境变量 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。
任意一个未设置时，发送函数静默跳过。
任何网络异常都被吃掉，不影响主流程。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from .env import load_local_env


def _is_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(
        os.environ.get("TELEGRAM_CHAT_ID")
    )


def send(text: str) -> bool:
    load_local_env()

    if not _is_configured():
        print("[notifier] TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置，跳过推送")
        return False

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        resp = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(
                "[notifier] Telegram 返回 "
                f"{resp.status_code}: {_sanitize_text(resp.text[:200])}"
            )
            return False
        return True
    except Exception as exc:
        print(f"[notifier] Telegram 推送异常: {_sanitize_exception(exc)}")
        return False


def send_failure(error_summary: str, attempts: int) -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_url = os.environ.get("GITHUB_RUN_URL", "(本地运行)")

    lines = [
        "VPS8 签到失败",
        f"时间: {now}",
        f"重试次数: {attempts}",
        f"最后错误: {error_summary}",
        f"日志: {run_url}",
    ]
    return send("\n".join(lines))


def send_photo(photo_path: Path | str, caption: str) -> bool:
    load_local_env()

    if not _is_configured():
        print("[notifier] TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置，跳过图片推送")
        return False

    path = Path(photo_path)
    if not path.exists():
        print(f"[notifier] 图片不存在，跳过推送: {path}")
        return False

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendPhoto"

    try:
        with path.open("rb") as photo:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"photo": (path.name, photo, "image/png")},
                timeout=20,
            )
        if resp.status_code != 200:
            print(
                "[notifier] Telegram 图片返回 "
                f"{resp.status_code}: {_sanitize_text(resp.text[:200])}"
            )
            return False
        print(f"[notifier] Telegram 图片已发送: {path}")
        return True
    except Exception as exc:
        print(f"[notifier] Telegram 图片推送异常: {_sanitize_exception(exc)}")
        return False


def send_result_photo(status: str, photo_path: Path | str) -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    caption = f"VPS8 签到结果\n状态: {status}\n时间: {now}"
    return send_photo(photo_path, caption)


def _sanitize_exception(exc: Exception) -> str:
    return f"{type(exc).__name__}: {_sanitize_text(str(exc))}"


def _sanitize_text(text: str) -> str:
    msg = text
    for secret_name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        secret = os.environ.get(secret_name)
        if secret:
            msg = msg.replace(secret, "<redacted>")
    return msg


if __name__ == "__main__":
    ok = send("VPS8 checkin notifier 测试消息")
    print("发送结果:", ok)
