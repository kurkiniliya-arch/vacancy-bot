"""One public-web scan; records baseline or previews alerts, never sends messages."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from .console import configure_output
from .state import Store
from .filtering import Rules
from .config import load_config
from .websource import fetch, SourceError


def main():
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    if config.get("delivery", {}).get("enabled") is not False:
        parser.error("Only dry run is available; delivery.enabled must be false")
    sources = config.get("sources", [])
    if not sources or len(sources) != len(set(sources)):
        parser.error("Expected a non-empty list of unique channels")
    interval=config.get('poll_seconds',60)
    if type(interval) is not int or interval<60:
        parser.error('poll_seconds must be an integer >= 60')
    store = Store(args.database,rules=Rules.from_config(config))
    previous=store.get_setting("mode")
    if previous and previous!="dry-run":
        store.close()
        raise ValueError("Use a separate dry-run database")
    store.set_setting("mode","dry-run")
    failed = False
    try:
        for source in sources:
            if not store.due(source, time.time()):
                print(json.dumps({"source": source, "status": "backoff"}))
                continue
            try:
                posts = fetch(source)
            except SourceError as error:
                store.source_failed(source, time.time(), error.retry_after)
                print(json.dumps({"source": source, "status": str(error)}))
                failed = True
            else:
                result = store.ingest(source, posts, datetime.now(timezone.utc).isoformat())
                store.schedule_source(source,time.time()+interval)
                print(json.dumps({"source": source, **result}, ensure_ascii=False))
            time.sleep(2)
        for entry in store.preview():
            print(f"\n[{entry['state']}; НЕ ОТПРАВЛЕНО]\n{entry['body']}")
    finally:
        store.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
