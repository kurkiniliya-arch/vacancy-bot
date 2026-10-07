from dataclasses import asdict
import json
import sqlite3
from .filtering import assess, render, refine, Rules
from .model import Post


class Store:
    def __init__(self, path, review_enabled=False, packets_enabled=False, rules=None):
        self.rules = rules or Rules()
        self.review_enabled = review_enabled
        self.packets_enabled = packets_enabled
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS sources (
                name TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
                initialized INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS vacancies (
                key TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS posts (
                source TEXT NOT NULL, post_id INTEGER NOT NULL, key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                PRIMARY KEY(source, post_id));
            CREATE TABLE IF NOT EXISTS revisions (
                source TEXT, post_id INTEGER, fingerprint TEXT, payload TEXT, observed_at TEXT,
                UNIQUE(source, post_id, fingerprint));
            CREATE TABLE IF NOT EXISTS post_keys (
                key TEXT NOT NULL, notification_key TEXT NOT NULL,
                PRIMARY KEY(key, notification_key));
            CREATE TABLE IF NOT EXISTS outbox (
                key TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL,
                next_attempt REAL NOT NULL DEFAULT 0, message_id INTEGER,
                CHECK (state IN ('pending','inflight','sent','uncertain','failed')));
            CREATE TABLE IF NOT EXISTS reviews (
                key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                observed_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                result TEXT);
        ''')
        with self.db:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(posts)")}
            if "eligible" not in columns:
                # Existing snapshots remain historical on upgrade; never replay
                # a backlog just because the classifier or schema changed.
                self.db.execute("ALTER TABLE posts ADD COLUMN eligible INTEGER NOT NULL DEFAULT 0")
            self.db.execute("INSERT OR IGNORE INTO post_keys SELECT key,key FROM posts")
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(outbox)')}
            for name, kind in [('payload','TEXT'),('packet','TEXT'),('packet_state',"TEXT NOT NULL DEFAULT 'pending'"),
                               ('packet_attempts','INTEGER NOT NULL DEFAULT 0'),
                               ('packet_next_attempt','REAL NOT NULL DEFAULT 0'),('attachment_ids','TEXT')]:
                if name not in columns: self.db.execute(f'ALTER TABLE outbox ADD COLUMN {name} {kind}')
            # Existing unsent rows can be prepared, but never create historical outbox rows.
            self.db.execute('''UPDATE outbox SET payload=(SELECT payload FROM posts
                WHERE posts.key=outbox.key ORDER BY last_seen DESC LIMIT 1)
                WHERE state='pending' AND payload IS NULL''')

    def close(self):
        self.db.close()

    def due(self, source, now):
        row = self.db.execute("SELECT next_attempt FROM sources WHERE name=?", (source,)).fetchone()
        return row is None or row[0] <= now

    def source_failed(self, source, now, retry_after=0):
        # Does not initialize or advance the source; a failed first fetch is not a baseline.
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO sources(name) VALUES (?)", (source,))
            count = self.db.execute("SELECT failures FROM sources WHERE name=?", (source,)).fetchone()[0] + 1
            delay = max(retry_after, min(3600, 30 * 2 ** min(count - 1, 7)))
            self.db.execute("UPDATE sources SET failures=?, next_attempt=? WHERE name=?",
                            (count, now + delay, source))

    def ingest(self, source: str, posts: list[Post], observed_at: str):
        if any(p.source != source for p in posts):
            raise ValueError("Mixed source snapshot")
        counts = dict(baseline=0, queued=0, duplicate=0, changed=0, rejected=0, old=0)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT OR IGNORE INTO sources(name) VALUES (?)", (source,))
            src = self.db.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
            cursor = src["cursor"]
            for post in sorted(posts, key=lambda p: p.post_id):
                prior = self.db.execute("SELECT * FROM posts WHERE source=? AND post_id=?",
                                        (source, post.post_id)).fetchone()
                payload = json.dumps(asdict(post), ensure_ascii=False)
                if prior and prior["fingerprint"] == post.fingerprint:
                    counts["duplicate"] += 1
                    continue
                eligible = bool(prior["eligible"]) if prior else bool(src["initialized"] and post.post_id > cursor)
                notification_key = prior["key"] if prior else post.key
                if prior:
                    counts["changed"] += 1
                    self.db.execute("UPDATE posts SET fingerprint=?,payload=?,last_seen=? WHERE source=? AND post_id=?",
                                    (post.fingerprint, payload, observed_at, source, post.post_id))
                else:
                    self.db.execute("INSERT INTO posts VALUES (?,?,?,?,?,?,?,?)",
                                    (source, post.post_id, notification_key, post.fingerprint, payload,
                                     observed_at, observed_at, int(eligible)))
                self.db.execute("INSERT OR IGNORE INTO revisions VALUES (?,?,?,?,?)",
                                (source, post.post_id, post.fingerprint, payload, observed_at))
                self.db.execute("INSERT OR IGNORE INTO vacancies VALUES (?,?)", (post.key, observed_at))
                self.db.execute("INSERT OR IGNORE INTO post_keys VALUES (?,?)", (post.key, notification_key))
                assessment = assess(post,self.rules)
                if not eligible:
                    if not prior:
                        counts["baseline" if not src["initialized"] else "old"] += 1
                    continue
                # Follow all URL aliases, including edited links, so neither
                # cross-channel reposts nor later edits resend a delivered item.
                related = [row[0] for row in self.db.execute('''
                    WITH RECURSIVE linked(key) AS (
                        SELECT ? UNION SELECT ?
                        UNION SELECT p.notification_key FROM post_keys p JOIN linked l ON p.key=l.key
                        UNION SELECT p.key FROM post_keys p JOIN linked l ON p.notification_key=l.key
                    ) SELECT key FROM linked
                ''', (post.key, notification_key))]
                placeholders = ",".join("?" for _ in related)
                history = self.db.execute(f"SELECT 1 FROM posts WHERE eligible=0 AND key IN ({placeholders}) LIMIT 1",
                                          related).fetchone()
                queued = self.db.execute(f"SELECT key,state,message_id FROM outbox WHERE key IN ({placeholders})", related).fetchall()
                if history or any(row["state"] != "pending" or row['message_id'] is not None for row in queued):
                    counts["duplicate"] += 1
                    continue
                if assessment.category == "вне предварительного отбора":
                    # Only unsent work may be removed. A later qualifying edit
                    # can create a fresh pending row; delivery failures cannot.
                    if prior:
                        self.db.execute("DELETE FROM outbox WHERE key=? AND state='pending'", (notification_key,))
                        self.db.execute("UPDATE reviews SET state='ignored' WHERE key=?", (notification_key,))
                    counts["rejected"] += 1
                else:
                    if self.review_enabled:
                        key=queued[0]['key'] if queued else notification_key
                        self.db.execute('''INSERT INTO reviews(key,fingerprint,payload,observed_at)
                            VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                            fingerprint=excluded.fingerprint,payload=excluded.payload,observed_at=excluded.observed_at,
                            state='pending',attempts=0,next_attempt=0,result=NULL''',
                            (key,post.fingerprint,payload,observed_at))
                        for row in queued:
                            self.db.execute("DELETE FROM outbox WHERE key=? AND state='pending'",(row['key'],))
                        counts.setdefault('review',0)
                        counts['review']+=1
                        continue
                    body = render(post, assessment, observed_at)
                    if queued:
                        # Retain a rate-limit delay when refreshing an unsent card.
                        key = queued[0]["key"]
                        self.db.execute("UPDATE outbox SET body=?,payload=?,packet=NULL,packet_state='pending',packet_attempts=0,packet_next_attempt=0 WHERE key=? AND state='pending'", (body, payload, key))
                        for duplicate in queued[1:]:
                            self.db.execute("DELETE FROM outbox WHERE key=? AND state='pending'", (duplicate["key"],))
                        counts["duplicate"] += int(not prior)
                    else:
                        self.db.execute("INSERT INTO outbox(key,body,state,payload) VALUES (?,?,'pending',?)", (notification_key, body, payload))
                        counts["queued"] += 1
            self.db.execute("UPDATE sources SET cursor=?,initialized=1,failures=0,next_attempt=0 WHERE name=?",
                            (max([cursor] + [p.post_id for p in posts]), source))
        return counts

    def recover_reviews(self):
        # Model inference has no external side effects and is safe to retry.
        with self.db:
            self.db.execute("UPDATE reviews SET state='pending' WHERE state='working'")

    def claim_review(self, now):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute("SELECT * FROM reviews WHERE state='pending' AND next_attempt<=? ORDER BY rowid LIMIT 1",(now,)).fetchone()
            if row:
                self.db.execute("UPDATE reviews SET state='working' WHERE key=?",(row['key'],))
        return dict(row) if row else None

    def finish_review(self, item, result, now):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            current=self.db.execute('SELECT * FROM reviews WHERE key=?',(item['key'],)).fetchone()
            if not current or current['fingerprint']!=item['fingerprint'] or current['state']!='working':
                return 'stale'
            post=Post(**json.loads(current['payload']))
            assessment=assess(post,self.rules)
            if (result is None or result.get('verdict')=='unclear') and assessment.role_kind!='target':
                attempts=current['attempts']+1
                self.db.execute("UPDATE reviews SET state='pending',attempts=?,next_attempt=? WHERE key=?",
                                (attempts,now+min(1800,300*attempts),item['key']))
                return 'retry'
            if result is not None:
                assessment=refine(post,assessment,result)
            self.db.execute("UPDATE reviews SET state='done',result=? WHERE key=?",
                            (json.dumps(result or {'fallback':'rules'},ensure_ascii=False),item['key']))
            if assessment.category=='вне предварительного отбора': return 'rejected'
            existing=self.db.execute('''WITH RECURSIVE linked(key) AS (
                SELECT ? UNION SELECT ?
                UNION SELECT p.notification_key FROM post_keys p JOIN linked l ON p.key=l.key
                UNION SELECT p.key FROM post_keys p JOIN linked l ON p.notification_key=l.key)
                SELECT 1 FROM outbox WHERE key IN (SELECT key FROM linked) LIMIT 1''',
                (item['key'],post.key)).fetchone()
            if existing: return 'duplicate'
            self.db.execute("INSERT INTO outbox(key,body,state,payload) VALUES (?,?,'pending',?)",
                            (item['key'],render(post,assessment,current['observed_at']),current['payload']))
            return 'queued'

    def recover_packets(self):
        with self.db:
            self.db.execute("UPDATE outbox SET packet_state='pending' WHERE packet_state='working' AND state='pending'")

    def claim_packet(self, now):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute("""SELECT * FROM outbox WHERE state='pending' AND message_id IS NULL
                AND payload IS NOT NULL AND packet_state='pending' AND packet_next_attempt<=? ORDER BY rowid LIMIT 1""",(now,)).fetchone()
            if row: self.db.execute("UPDATE outbox SET packet_state='working' WHERE key=?",(row['key'],))
        return dict(row) if row else None

    def finish_packet(self, item, packet, now):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM outbox WHERE key=?',(item['key'],)).fetchone()
            if not row or row['payload']!=item['payload'] or row['packet_state']!='working' or row['state']!='pending':
                return 'stale'
            if packet is None:
                attempts=row['packet_attempts']+1
                self.db.execute("UPDATE outbox SET packet_state='pending',packet_attempts=?,packet_next_attempt=? WHERE key=?",
                                (attempts,now+min(1800,300*attempts),item['key']))
                return 'retry'
            self.db.execute("UPDATE outbox SET packet=?,packet_state='ready' WHERE key=?",
                            (json.dumps(packet,ensure_ascii=False),item['key']))
            return 'ready'

    def preview(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM outbox ORDER BY rowid")]

    def get_setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))

    def schedule_source(self, source, next_attempt):
        with self.db:
            self.db.execute("UPDATE sources SET next_attempt=? WHERE name=?", (next_attempt, source))

    def claim(self, now):
        # Claim is committed before network I/O; competing workers cannot claim this row.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            readiness = " AND packet_state='ready'" if self.packets_enabled else ''
            row = self.db.execute("SELECT * FROM outbox WHERE state='pending' AND next_attempt<=?" + readiness +
                                  " ORDER BY (message_id IS NOT NULL) DESC,rowid LIMIT 1",
                                  (now,)).fetchone()
            if row:
                self.db.execute("UPDATE outbox SET state='inflight' WHERE key=?", (row["key"],))
        return dict(row) if row else None

    def resolve(self, key, state, next_attempt=0, message_id=None, pause_until=None, halt=None, attachment_ids=None):
        if state not in {"sent", "pending", "uncertain", "failed"}:
            raise ValueError("Invalid delivery outcome")
        with self.db:
            changed = self.db.execute("UPDATE outbox SET state=?,next_attempt=?,message_id=COALESCE(?,message_id),attachment_ids=COALESCE(?,attachment_ids) WHERE key=? AND state='inflight'",
                                     (state, next_attempt, message_id, json.dumps(attachment_ids) if attachment_ids else None, key)).rowcount
            if changed != 1:
                raise ValueError("Delivery was not claimed")
            if pause_until is not None:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('send_after',?)", (str(pause_until),))
            if halt:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('delivery_halted',?)", (halt,))

    def recover_after_exclusive_restart(self):
        # Only after stopping the old process; never steal work from a live worker.
        with self.db:
            self.db.execute("UPDATE outbox SET state='uncertain' WHERE state='inflight'")
