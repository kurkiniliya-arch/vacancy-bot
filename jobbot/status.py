"""Read-only queue summary. Does not print tokens, recipient IDs, CV facts or letters."""
import argparse
import json
from pathlib import Path
import sqlite3
from .console import configure_output


def summary(path):
    path=Path(path).expanduser().resolve()
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        return {
            'integrity':db.execute('PRAGMA quick_check').fetchone()[0],
            'mode':dict(db.execute("SELECT key,value FROM settings WHERE key IN ('mode','delivery_halted')")),
            'outbox':dict(db.execute('SELECT state,count(*) FROM outbox GROUP BY state')),
            'reviews':dict(db.execute('SELECT state,count(*) FROM reviews GROUP BY state')),
            'packets':dict(db.execute("SELECT packet_state,count(*) FROM outbox WHERE state!='sent' GROUP BY packet_state")),
            'source_failures':dict(db.execute('SELECT name,failures FROM sources WHERE failures>0')),
        }
    finally:
        db.close()


def main():
    configure_output()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(summary(args.database),ensure_ascii=False,indent=2))
    except (OSError,sqlite3.Error):
        parser.error('Cannot read an existing vacancy-bot database')


if __name__=='__main__': main()
