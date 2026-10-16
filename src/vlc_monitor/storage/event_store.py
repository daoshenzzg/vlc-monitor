from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time


class EventStore:
    def __init__(self, path, *, timeout=0.1):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=timeout)
        try:
            self.db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, triggered REAL NOT NULL, payload TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS alarm_attention (event_id TEXT PRIMARY KEY, acknowledged_at REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS notification_delivery (id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, channel TEXT NOT NULL, phase TEXT NOT NULL, sequence INTEGER NOT NULL, attempts INTEGER NOT NULL, result TEXT NOT NULL, created REAL NOT NULL)")
            self.cleanup()
        except Exception:
            self.db.close()
            raise

    @staticmethod
    def event_record(event):
        return ("upsert", (event.id, event.triggered_at, json.dumps(asdict(event), ensure_ascii=False)))

    @staticmethod
    def validate_delivery(channel, phase, result):
        if channel not in ("local", "wechat") or phase not in ("first", "repeat", "end", "test", "start", "recovery"):
            raise ValueError("无效通知通道或阶段")
        allowed = {"requested", "sound_disabled", "muted", "sound_failed", "accepted", "failed", "uncertain", "queue_full", "expired", "cancelled"}
        if result not in allowed:
            raise ValueError("无效通知结果")

    def write_batch(self, records):
        """Ordered immutable records share one transaction, including barriers."""
        with self.db:
            for kind, values in records:
                if kind == "upsert":
                    self.db.execute("INSERT INTO events VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload WHERE json_extract(events.payload, '$.ended_at') IS NULL OR json_extract(excluded.payload, '$.ended_at') IS NOT NULL", values)
                elif kind == "acknowledge":
                    self.db.execute("INSERT OR IGNORE INTO alarm_attention VALUES (?,?)", values)
                elif kind == "delivery":
                    self.db.execute("INSERT INTO notification_delivery(event_id,channel,phase,sequence,attempts,result,created) VALUES (?,?,?,?,?,?,?)", values)
                elif kind == "cleanup":
                    self._cleanup()
                else:
                    raise ValueError("Unknown storage record")

    def upsert(self, events):
        self.write_batch(self.event_record(event) for event in events)

    def recent(self, limit=200):
        rows = []
        for event_id, payload, acknowledged_at in self.db.execute("SELECT e.id, e.payload, a.acknowledged_at FROM events e LEFT JOIN alarm_attention a ON e.id=a.event_id ORDER BY triggered DESC LIMIT ?", (max(0, min(1000, limit)),)):
            row = json.loads(payload)
            row["acknowledged_at"] = acknowledged_at
            delivery = self.db.execute("SELECT channel, result, created FROM notification_delivery WHERE event_id=? ORDER BY id DESC LIMIT 1", (event_id,)).fetchone()
            if delivery:
                row["delivery"] = {"channel": delivery[0], "result": delivery[1], "created_at": delivery[2]}
            rows.append(row)
        return rows

    def acknowledge(self, event_ids, acknowledged_at):
        self.write_batch(("acknowledge", (event_id, acknowledged_at)) for event_id in event_ids)

    def record_delivery(self, event_ids, channel, phase, result, sequence=0, attempts=1, created_at=None):
        self.validate_delivery(channel, phase, result)
        stamp = time.time() if created_at is None else created_at
        self.write_batch(("delivery", (event_id, channel, phase, sequence, attempts, result, stamp)) for event_id in event_ids)

    def _cleanup(self):
        self.db.execute("DELETE FROM events WHERE triggered < ?", (time.time() - 30 * 86400,))
        self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY triggered DESC LIMIT 10000)")
        self.db.execute("DELETE FROM alarm_attention WHERE event_id NOT IN (SELECT id FROM events)")
        self.db.execute("DELETE FROM notification_delivery WHERE created < ? OR (event_id NOT IN (SELECT id FROM events) AND phase != 'test')", (time.time() - 30 * 86400,))
        self.db.execute("DELETE FROM notification_delivery WHERE id NOT IN (SELECT id FROM notification_delivery ORDER BY id DESC LIMIT 30000)")

    def cleanup(self):
        self.write_batch((("cleanup", ()),))

    def close(self):
        self.db.close()
