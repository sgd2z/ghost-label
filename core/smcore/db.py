"""The library: one SQLite file (WAL). Rows are plain dicts; JSON columns end in `_json`."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

SCHEMA = [
    # v1
    """
    CREATE TABLE artists (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_name TEXT, bio TEXT, soul TEXT, voice TEXT,
        origin TEXT, formed TEXT, genres_json TEXT DEFAULT '[]', members_json TEXT DEFAULT '[]',
        visual TEXT, portrait_path TEXT, portrait_prompt TEXT, relation TEXT,
        voice_track_id TEXT, source_set_id TEXT, brief_id TEXT, seed_artist_id TEXT,
        batch_id TEXT, data_json TEXT DEFAULT '{}', created REAL NOT NULL);
    CREATE TABLE albums (
        id TEXT PRIMARY KEY, artist_id TEXT NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
        title TEXT NOT NULL, year INTEGER, kind TEXT DEFAULT 'album', concept TEXT, liner_notes TEXT,
        cover_path TEXT, cover_prompt TEXT, cover_brief TEXT, position INTEGER DEFAULT 0,
        status TEXT DEFAULT 'planned', brief_id TEXT, data_json TEXT DEFAULT '{}', created REAL NOT NULL);
    CREATE TABLE tracks (
        id TEXT PRIMARY KEY, album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
        artist_id TEXT NOT NULL, number INTEGER NOT NULL, title TEXT NOT NULL, theme TEXT,
        style TEXT, lyrics TEXT, is_instrumental INTEGER DEFAULT 0, language TEXT, bpm INTEGER,
        keyscale TEXT, explicit INTEGER DEFAULT 0, seed INTEGER, target_seconds REAL,
        reference_track_ids_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'planned', audio_path TEXT, duration REAL, loudness REAL, error TEXT,
        recorded_at REAL, record_seconds REAL, engine TEXT, data_json TEXT DEFAULT '{}', created REAL NOT NULL);
    CREATE INDEX tracks_album ON tracks(album_id, number);
    CREATE INDEX albums_artist ON albums(artist_id, position);
    CREATE TABLE favorites (kind TEXT NOT NULL, item_id TEXT NOT NULL, created REAL NOT NULL,
        PRIMARY KEY (kind, item_id));
    CREATE TABLE play_events (id INTEGER PRIMARY KEY AUTOINCREMENT, track_id TEXT NOT NULL,
        kind TEXT NOT NULL, position REAL, at REAL NOT NULL);
    CREATE TABLE taste (id INTEGER PRIMARY KEY CHECK (id = 1), profile_json TEXT DEFAULT '{}',
        reactions_json TEXT DEFAULT '[]', answers_json TEXT DEFAULT '[]', notes TEXT DEFAULT '',
        signals_since_refresh INTEGER DEFAULT 0, updated REAL);
    CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, status TEXT NOT NULL,
        progress REAL DEFAULT 0, message TEXT, error TEXT, data_json TEXT DEFAULT '{}',
        result_json TEXT DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL);
    CREATE TABLE source_sets (id TEXT PRIMARY KEY, title TEXT, note TEXT, mode TEXT,
        status TEXT DEFAULT 'new', created REAL NOT NULL);
    CREATE TABLE source_tracks (id TEXT PRIMARY KEY, set_id TEXT NOT NULL REFERENCES source_sets(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, url TEXT NOT NULL, resolver TEXT, title TEXT, channel TEXT,
        duration REAL, thumbnail TEXT, metadata_json TEXT DEFAULT '{}', catalog_json TEXT DEFAULT '[]',
        analysis_json TEXT DEFAULT '{}', clip_path TEXT, status TEXT DEFAULT 'new', error TEXT,
        included INTEGER DEFAULT 1);
    CREATE TABLE style_briefs (id TEXT PRIMARY KEY, set_id TEXT NOT NULL REFERENCES source_sets(id) ON DELETE CASCADE,
        version INTEGER NOT NULL, parent_id TEXT, instruction TEXT, brief_json TEXT NOT NULL,
        model TEXT, created REAL NOT NULL);
    CREATE TABLE kv (key TEXT PRIMARY KEY, value_json TEXT, updated REAL);
    """,
    # v2: a link set's options; the real lyrics of its source songs (fetched only when asked for)
    """
    ALTER TABLE source_sets ADD COLUMN options_json TEXT DEFAULT '{}';
    CREATE TABLE source_lyrics (track_id TEXT PRIMARY KEY REFERENCES source_tracks(id) ON DELETE CASCADE,
        status TEXT NOT NULL, source TEXT, synced INTEGER DEFAULT 0, language TEXT, text TEXT, synced_text TEXT,
        edited INTEGER DEFAULT 0, error TEXT, detail_json TEXT DEFAULT '{}', updated REAL NOT NULL);
    """,
]

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(kind UNINDEXED, item_id UNINDEXED, title, subtitle, body,
    tokenize = 'unicode61 remove_diacritics 2');
"""

JSON_SUFFIX = "_json"


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:16]


def decode_row(row: sqlite3.Row | None):
    if row is None:
        return None
    out = {}
    for key in row.keys():
        value = row[key]
        if key.endswith(JSON_SUFFIX):
            try:
                out[key[: -len(JSON_SUFFIX)]] = json.loads(value) if value else None
            except ValueError:
                out[key[: -len(JSON_SUFFIX)]] = None
        else:
            out[key] = value
    return out


def encode(values: dict) -> dict:
    out = {}
    for key, value in values.items():
        if key.endswith(JSON_SUFFIX) and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        out[key] = value
    return out


class Database:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.has_fts = True
        self.migrate()

    def migrate(self):
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(SCHEMA[version:], start=version + 1):
                self.conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
            try:
                self.conn.executescript(FTS)
            except sqlite3.OperationalError:
                self.has_fts = False
            self.conn.execute("INSERT OR IGNORE INTO taste (id, updated) VALUES (1, ?)", (time.time(),))

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def all(self, sql: str, args=()) -> list:
        with self.lock:
            return [decode_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()):
        with self.lock:
            return decode_row(self.conn.execute(sql, args).fetchone())

    def scalar(self, sql: str, args=()):
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            return row[0] if row else None

    def run(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def insert(self, table: str, values: dict) -> dict:
        values = encode(values)
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
        return values

    def update(self, table: str, row_id: str, values: dict, key: str = "id"):
        if not values:
            return
        values = encode(values)
        sets = ", ".join(f"{k} = ?" for k in values)
        self.run(f"UPDATE {table} SET {sets} WHERE {key} = ?", tuple(values.values()) + (row_id,))

    # MARK: key/value

    def kv_get(self, key: str, default=None):
        row = self.one("SELECT value_json FROM kv WHERE key = ?", (key,))
        return default if row is None or row["value"] is None else row["value"]

    def kv_set(self, key: str, value) -> None:
        self.run("INSERT INTO kv (key, value_json, updated) VALUES (?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated = excluded.updated",
                 (key, json.dumps(value, ensure_ascii=False), time.time()))

    # MARK: search index

    def index(self, kind: str, item_id: str, title: str, subtitle: str = "", body: str = "") -> None:
        if not self.has_fts:
            return
        with self.lock:
            self.conn.execute("DELETE FROM search WHERE kind = ? AND item_id = ?", (kind, item_id))
            self.conn.execute("INSERT INTO search (kind, item_id, title, subtitle, body) VALUES (?, ?, ?, ?, ?)",
                              (kind, item_id, title or "", subtitle or "", body or ""))

    def unindex(self, kind: str, item_id: str) -> None:
        if self.has_fts:
            self.run("DELETE FROM search WHERE kind = ? AND item_id = ?", (kind, item_id))

    def search(self, query: str, limit: int = 40) -> list:
        words = [w for w in "".join(c if c.isalnum() else " " for c in query).split() if w]
        if not words:
            return []
        if self.has_fts:
            match = " ".join(f'"{w}"*' for w in words)
            return self.all("SELECT kind, item_id, title, subtitle FROM search WHERE search MATCH ? "
                            "ORDER BY rank LIMIT ?", (match, limit))
        like = f"%{query}%"
        rows = self.all("SELECT 'artist' AS kind, id AS item_id, name AS title, '' AS subtitle FROM artists "
                        "WHERE name LIKE ? LIMIT ?", (like, limit))
        rows += self.all("SELECT 'album' AS kind, id AS item_id, title, '' AS subtitle FROM albums "
                         "WHERE title LIKE ? LIMIT ?", (like, limit))
        rows += self.all("SELECT 'track' AS kind, id AS item_id, title, '' AS subtitle FROM tracks "
                         "WHERE title LIKE ? LIMIT ?", (like, limit))
        return rows[:limit]
