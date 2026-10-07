import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from .console import configure_output
from .model import Post
from .state import Store
from .filtering import Rules
from .config import load_config


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Offline dry run only; never calls Telegram")
    parser.add_argument("--config", required=True)
    parser.add_argument("--snapshot", required=True, help="Local JSON source snapshot")
    parser.add_argument("--database", required=True, help="Separate dry-run SQLite database")
    args = parser.parse_args()
    config = load_config(args.config)
    if config.get("delivery", {}).get("enabled") is not False:
        parser.error("Offline preview requires delivery.enabled=false")
    data = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    allowed = config.get("sources", [])
    if data["source"] not in allowed:
        parser.error("Source is not in the approved local configuration")
    if data.get("complete") is not True:
        parser.error("Source snapshot failed or incomplete; state unchanged")
    posts = [Post(**post) for post in data["posts"]]
    now = datetime.now(timezone.utc).isoformat()
    store = Store(args.database,rules=Rules.from_config(config))
    previous=store.get_setting("mode")
    if previous and previous!="dry-run":
        store.close()
        raise ValueError("Use a separate dry-run database")
    store.set_setting("mode","dry-run")
    try:
        print(json.dumps(store.ingest(data["source"], posts, now), ensure_ascii=False))
        for entry in store.preview():
            print(f"\n[{entry['state']}; НЕ ОТПРАВЛЕНО]\n{entry['body']}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
