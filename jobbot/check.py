"""One explicitly requested setup message, with persistent delivery tracking."""
import argparse
from pathlib import Path
import time
from .runtime import credentials, log
from .state import Store
from .telegram import BotAPI, TelegramError, deliver_one


def check(secret_path, api_factory=BotAPI):
    secret = credentials(secret_path)
    api = api_factory(secret["token"])
    api.preflight(secret.get("bot_username"))
    chat = api.call("getChat", {"chat_id": secret["chat_id"]})
    if chat.get("type") != "private" or chat.get("id") != secret["chat_id"]:
        raise ValueError("recipient_mismatch")
    path = Path(secret_path).parent / "setup-check.sqlite"
    store = Store(path)
    try:
        # Run only while the live service is stopped. Outbox prevents automatic repeats.
        store.recover_after_exclusive_restart()
        key = "setup-check:v1"
        with store.db:
            store.db.execute("INSERT OR IGNORE INTO outbox(key,body,state) VALUES (?,?,'pending')",
                             (key, "Проверка связи с ботом прошла.\n"
                              "Этот личный чат выбран для вакансий.\n"
                              "Каналы и критерии отбора задаются в вашей конфигурации.\n"
                              "Старые посты при первом просмотре рассылаться не будут."))
        row = store.preview()[0]
        if row["state"] == "sent":
            return "already_sent"
        if row["state"] in {"uncertain", "failed"}:
            raise ValueError("setup_message_needs_manual_check")
        result = deliver_one(store, api, secret["chat_id"], time.time())
        if result != "sent":
            raise ValueError("setup_message_" + result)
        return result
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--send-test", action="store_true", required=True)
    args = parser.parse_args()
    try:
        log("setup_message", result=check(args.credentials))
    except TelegramError as error:
        log("setup_message_error", reason=error.kind)
        return 1
    except Exception as error:
        log("setup_message_error", reason=type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
