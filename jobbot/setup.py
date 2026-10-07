"""Run personally in an interactive SSH terminal. Never send the token to chat."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys
import time
from .telegram import BotAPI, TelegramError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--username", help="Expected BotFather username, without token")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        print("Откройте интерактивный SSH-терминал: ввод секретов через pipe запрещён.")
        return 1
    dest = Path(args.credentials).expanduser()
    if dest.exists():
        print("Файл уже существует; существующие настройки не перезаписаны.")
        return 1
    if input("Бот нигде больше не работает и getUpdates можно использовать для привязки? Введите ДА: ").strip() != "ДА":
        return 1
    token = getpass.getpass("Токен BotFather (ввод скрыт, не присылайте в чат): ").strip()
    api = BotAPI(token)
    me = api.preflight(args.username)
    nonce = secrets.token_hex(8)
    print("Откройте лично @" + me["username"] + " и отправьте:")
    print("/start " + nonce)
    print("Ожидание 5 минут. Сообщения вам сейчас не отправляются.")
    deadline = time.time() + 300
    offset = None
    match = None
    while time.time() < deadline and match is None:
        payload = {"timeout": 0, "limit": 100}
        if offset is not None:
            payload["offset"] = offset
        updates = api.call("getUpdates", payload)
        for update in updates:
            offset = max(offset or 0, update["update_id"] + 1)
            message = update.get("message", {})
            chat = message.get("chat", {})
            if (message.get("text") == "/start " + nonce and chat.get("type") == "private"
                    and chat.get("id") == message.get("from", {}).get("id")
                    and not message.get("from", {}).get("is_bot")):
                match = chat
                break
        if match is None:
            time.sleep(3)
    if match is None:
        print("Подтверждение не получено. Файл секретов не создан.")
        return 1
    print("Получатель:", match.get("first_name", ""), "@" + match.get("username", ""), "chat_id:", match["id"])
    if input("Это ваш личный чат для вакансий? Введите ДА: ").strip() != "ДА":
        return 1
    os.umask(0o077)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with dest.open("x", encoding="utf-8") as handle:
        json.dump({"token": token, "bot_username": me["username"], "chat_id": match["id"], "recipient_confirmed": True,
                   "exclusive_bot_confirmed": True}, handle)
    if os.name == "posix":
        dest.chmod(0o600)
    print("Настройки сохранены локально. Отправка и служба не запускались.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TelegramError as error:
        print("Настройка остановлена:", error.kind)
        raise SystemExit(1)
    except (KeyboardInterrupt, EOFError):
        print("Настройка отменена.")
        raise SystemExit(1)
