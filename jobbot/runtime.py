"""Linux single-process service. Sending needs both --send and an enabled config."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import stat
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from .model import Post
from .filtering import Rules
from .config import load_config
from .localmodel import classify, ModelUnavailable
from .applications import prepare, load_profile
from .state import Store
from .telegram import BotAPI, TelegramError, deliver_one
from .websource import fetch, SourceError


def log(event, **details):
    print(json.dumps({"event": event, **details}, ensure_ascii=False), flush=True)


def credentials(path):
    p = Path(path)
    info = p.stat()
    if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
        raise ValueError("Credentials require a regular file with permissions 0600")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("exclusive_bot_confirmed") is not True or data.get("recipient_confirmed") is not True:
        raise ValueError("Bot exclusivity and personal recipient need confirmation")
    if type(data.get("chat_id")) is not int or data["chat_id"] <= 0:
        raise ValueError("Invalid personal chat ID")
    return data


def run(config, database, secret_path=None, send=False, once=False):
    import fcntl  # Linux service; offline tests and parser also work on Windows.
    sources = config.get("sources", [])
    if not sources or len(sources) != len(set(sources)):
        raise ValueError("Expected unique configured channels")
    interval = config.get("poll_seconds", 60)
    if type(interval) is not int or interval < 60:
        raise ValueError("Public preview interval must be at least 60 seconds")
    if send and config.get("delivery", {}).get("enabled") is not True:
        raise ValueError("Sending is disabled in configuration")
    rules=Rules.from_config(config)
    os.umask(0o077)
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(str(database) + ".lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError("Another instance holds this database") from None
    model_config = config.get('local_model',{})
    review_enabled = send and model_config.get('enabled',False)
    application_config = config.get('applications',{})
    packets_enabled = send and application_config.get('enabled',False)
    asset_dir = application_config.get('profile_dir','')
    if packets_enabled: load_profile(asset_dir)
    store = Store(database,review_enabled=review_enabled,packets_enabled=packets_enabled,rules=rules)
    pool = ThreadPoolExecutor(max_workers=1) if review_enabled or packets_enabled else None
    review_future = None
    review_item = None
    work_kind = None
    try:
        mode = "live" if send else "dry-run"
        previous = store.get_setting("mode")
        if previous and previous != mode:
            raise ValueError("Use a new live database; do not replay the dry-run queue")
        store.set_setting("mode", mode)
        abandoned = store.db.execute("SELECT COUNT(*) FROM outbox WHERE state='inflight'").fetchone()[0]
        store.recover_after_exclusive_restart()
        store.recover_reviews()
        store.recover_packets()
        if abandoned:
            store.set_setting("delivery_halted", "uncertain_after_restart")
        api = None
        if send:
            if store.get_setting("delivery_halted"):
                raise ValueError("Delivery is halted pending manual inspection")
            secret = credentials(secret_path)
            api = BotAPI(secret["token"])
            api.preflight(secret.get("bot_username"))
            recipient = api.call("getChat", {"chat_id": secret["chat_id"]})
            if recipient.get("type") != "private" or recipient.get("id") != secret["chat_id"]:
                raise ValueError("Recipient verification failed")
        running = True

        def stop(*_):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        log("started", mode=mode, sources=len(sources), poll_seconds=interval, local_model=bool(review_enabled), applications=bool(packets_enabled))
        # Stagger sources to avoid request bursts. Persistent source backoff wins.
        schedule = {name: time.time() + i * 2 for i, name in enumerate(sources)}
        visited = set()
        next_source_request = 0.0
        while running:
            now = time.time()
            if review_future is not None and review_future.done():
                try:
                    review_result = review_future.result()
                except Exception:
                    review_result = None
                if work_kind=='packet':
                    outcome = store.finish_packet(review_item,review_result,now)
                    log('application_packet',outcome=outcome,mode=(review_result or {}).get('mode','retry'))
                else:
                    outcome = store.finish_review(review_item,review_result,now)
                    log('model_review',outcome=outcome,fallback=review_result is None)
                review_future = None
            if pool is not None and review_future is None:
                review_item = store.claim_packet(now) if packets_enabled else None
                if review_item:
                    work_kind='packet'
                    review_future = pool.submit(prepare,Post(**json.loads(review_item['payload'])),asset_dir,
                                               model_config.get('model','qwen3:4b'),application_config.get('research',False),application_config.get('use_model',False))
                elif review_enabled:
                    review_item = store.claim_review(now)
                    if review_item:
                        work_kind='review'
                        review_future = pool.submit(classify,Post(**json.loads(review_item['payload'])),model_config.get('model','qwen3:4b'),rules)
            if now >= next_source_request:
                for source in sources:
                    if once and source in visited:
                        continue
                    if schedule[source] > now:
                        continue
                    if not store.due(source, now):
                        visited.add(source)
                        schedule[source] = now + interval
                        continue
                    visited.add(source)
                    try:
                        posts = fetch(source)
                        stats = store.ingest(source, posts, datetime.now(timezone.utc).isoformat())
                    except SourceError as error:
                        store.source_failed(source, time.time(), error.retry_after)
                        log("source_error", source=source, reason=str(error))
                    else:
                        store.schedule_source(source, time.time() + interval)
                        log("source_checked", source=source, **stats)
                    schedule[source] = time.time() + interval
                    next_source_request = time.time() + 2
                    break
            if api:
                outcome = deliver_one(store, api, secret["chat_id"], time.time(),asset_dir=asset_dir)
                if outcome not in {"empty", "waiting"}:
                    log("delivery", outcome=outcome)
                    if outcome in {"failed", "uncertain"}:
                        # Avoid burning through the entire queue on invalid credentials.
                        raise ValueError("Delivery needs manual inspection; no automatic retry of this item")
            if once and len(visited) == len(sources):
                break
            time.sleep(0.5)
        log("stopped")
    finally:
        if pool is not None:
            pool.shutdown(wait=False,cancel_futures=True)
        store.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--credentials")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.send and not args.credentials:
        parser.error("--send requires --credentials")
    try:
        config = load_config(args.config)
        run(config, args.database, args.credentials, args.send, args.once)
    except TelegramError as error:
        log("stopped_with_error", reason=error.kind)
        return 1
    except Exception as error:
        # No traceback or arbitrary exception message: those can contain request URLs.
        log("stopped_with_error", reason=type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
