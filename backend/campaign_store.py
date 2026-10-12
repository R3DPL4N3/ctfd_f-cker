"""Persistent, cross-swarm campaign state. Database and vault remain on the host."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def campaign_identity(ctfd_url: str, categories: list[str] | None = None, override: str = "") -> str:
    if override:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", override):
            raise ValueError("invalid campaign id")
        return override
    source = ctfd_url.rstrip("/").lower() + "|" + ",".join(sorted(c.lower() for c in categories or []))
    return "ctfd-" + hashlib.sha256(source.encode()).hexdigest()[:20]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class CampaignStore:
    """SQLite-backed, crash-safe intelligence store with encrypted credential values.

    The database is host-owned, not directly mounted in untrusted solver containers.
    Only explicitly authorized solver tools can read decrypted credential values.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts_dir = self.root / "artifacts"
        self.jobs_dir = self.root / "jobs"
        for path in (self.root, self.artifacts_dir, self.jobs_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                path.chmod(0o700)
        self.db_path = self.root / "campaign.db"
        self.key_path = self.root / "vault.key"
        try:
            fd = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
        if os.name == "posix":
            self.key_path.chmod(0o600)
        self._cipher = Fernet(self.key_path.read_bytes().strip())
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, value TEXT NOT NULL, challenge TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'reported', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL, domain TEXT NOT NULL,
                    password_cipher BLOB, hash_cipher BLOB,
                    source_challenge TEXT NOT NULL, status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, summary TEXT NOT NULL,
                    challenge TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stages (
                    challenge TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
                    size INTEGER NOT NULL, challenge TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                    engine TEXT NOT NULL, hash_type TEXT NOT NULL, artifact_id TEXT NOT NULL,
                    principal TEXT NOT NULL, status TEXT NOT NULL, pid INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
        if os.name == "posix":
            self.db_path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
        finally:
            db.close()

    def _encrypt(self, raw: str | None) -> bytes | None:
        return self._cipher.encrypt(raw.encode("utf-8")) if raw else None

    def _decrypt(self, raw: bytes | None) -> str | None:
        if not raw:
            return None
        try:
            return self._cipher.decrypt(raw).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("vault key mismatch or credential corruption") from exc

    def publish(self, payload: dict[str, Any], challenge: str) -> dict[str, int]:
        if not challenge or not isinstance(payload, dict):
            raise ValueError("challenge and payload required")
        counts = {"facts": 0, "credentials": 0}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for kind in ("targets", "sessions", "networks", "findings", "artifacts"):
                    items = payload.get(kind) or []
                    if not isinstance(items, list):
                        raise ValueError(kind + " must be a list")
                    for item in items[:200]:
                        if not isinstance(item, (str, dict)):
                            continue
                        if isinstance(item, dict) and any(
                            key.lower() in {"password", "hash", "secret", "token", "private_key"}
                            for key in item
                        ):
                            raise ValueError("put secrets into credentials, not facts")
                        value = _json(item)
                        if len(value) > 16384:
                            raise ValueError("fact too large")
                        digest = hashlib.sha256((kind + ":" + value.casefold()).encode()).hexdigest()
                        cursor = db.execute(
                            "INSERT OR IGNORE INTO facts(fingerprint,kind,value,challenge,created_at) VALUES(?,?,?,?,?)",
                            (digest, kind, value, challenge, _utc()),
                        )
                        counts["facts"] += max(cursor.rowcount, 0)
                credentials = payload.get("credentials") or []
                if not isinstance(credentials, list):
                    raise ValueError("credentials must be a list")
                for item in credentials[:100]:
                    if not isinstance(item, dict):
                        continue
                    username = str(item.get("username") or "").strip()
                    domain = str(item.get("domain") or "").strip()
                    if not username:
                        continue
                    password, password_hash = item.get("password"), item.get("hash")
                    if password is not None and not isinstance(password, str):
                        raise ValueError("password must be text")
                    if password_hash is not None and not isinstance(password_hash, str):
                        raise ValueError("hash must be text")
                    identity = (domain + "\\" + username).casefold()
                    old = db.execute("SELECT * FROM credentials WHERE identity=?", (identity,)).fetchone()
                    # A new unverified candidate must not overwrite an already verified secret.
                    if old and old["status"] == "verified" and item.get("verified") is not True:
                        continue
                    status = "verified" if item.get("verified") is True else "reported"
                    if old and old["status"] == "verified" and not password and not password_hash:
                        status = "verified"
                    if old and not password and not password_hash and status == old["status"]:
                        continue
                    if old:
                        db.execute("""UPDATE credentials SET username=?,domain=?,
                            password_cipher=COALESCE(?,password_cipher),
                            hash_cipher=COALESCE(?,hash_cipher),
                            source_challenge=?,status=?,updated_at=? WHERE identity=?""",
                            (username,domain,self._encrypt(password),self._encrypt(password_hash),
                             challenge,status,_utc(),identity))
                    else:
                        db.execute("""INSERT INTO credentials
                            (identity,username,domain,password_cipher,hash_cipher,
                             source_challenge,status,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                            (identity,username,domain,self._encrypt(password),self._encrypt(password_hash),
                             challenge,status,_utc()))
                    counts["credentials"] += 1
                if any(counts.values()):
                    db.execute("INSERT INTO events(kind,summary,challenge,created_at) VALUES(?,?,?,?)",
                               ("intelligence",
                                f"{counts['facts']} facts; {counts['credentials']} credential updates. Search campaign.",
                                challenge,_utc()))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return counts

    def search(self, query: str = "", limit: int = 30) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        term = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.connect() as db:
            facts = db.execute("""SELECT kind,value,challenge,status,created_at
                FROM facts WHERE kind LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\'
                ORDER BY id DESC LIMIT ?""", (term,term,limit)).fetchall()
            credentials = db.execute("""SELECT id,username,domain,source_challenge,status,
                password_cipher IS NOT NULL AS has_password,hash_cipher IS NOT NULL AS has_hash
                FROM credentials WHERE identity LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?""",
                (term,limit)).fetchall()
            stages = db.execute("SELECT challenge,status FROM stages ORDER BY challenge").fetchall()
        return {"facts": [{**dict(row), "value": json.loads(row["value"])} for row in facts],
                "credentials": [{
                    "ref": f"cred:{row['id']}", "username": row["username"], "domain": row["domain"],
                    "source_challenge": row["source_challenge"],"status": row["status"],
                    "has_password": bool(row["has_password"]), "has_hash": bool(row["has_hash"])
                } for row in credentials],
                "stages": [dict(row) for row in stages]}

    def credential(self, ref: str) -> dict[str, Any]:
        if not re.fullmatch(r"cred:[1-9][0-9]*", ref):
            raise ValueError("invalid credential reference")
        with self.connect() as db:
            row = db.execute("SELECT * FROM credentials WHERE id=?", (int(ref[5:]),)).fetchone()
        if row is None:
            raise KeyError("credential not found")
        return {"username": row["username"], "domain": row["domain"],
                "password": self._decrypt(row["password_cipher"]),
                "hash": self._decrypt(row["hash_cipher"]),
                "source_challenge": row["source_challenge"],"status": row["status"]}

    def mark_stage(self, challenge: str, status: str) -> None:
        if status not in {"active", "accepted", "failed"}:
            raise ValueError("invalid stage status")
        with self.connect() as db:
            db.execute("""INSERT INTO stages(challenge,status,updated_at) VALUES(?,?,?)
                ON CONFLICT(challenge) DO UPDATE SET
                status=CASE WHEN stages.status='accepted' THEN 'accepted' ELSE excluded.status END,
                updated_at=excluded.updated_at""", (challenge,status,_utc()))

    def latest_event_id(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])

    def events(self, after: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?",
                              (max(0,int(after)),max(1,min(int(limit),100)))).fetchall()
        return [dict(r) for r in rows]

    def stage_artifact(self, data: bytes, name: str, challenge: str) -> str:
        if not data or len(data) > 8 * 1024 * 1024:
            raise ValueError("artifact size must be 1 byte to 8 MiB")
        digest = hashlib.sha256(data).hexdigest()
        ref = "sha256:" + digest
        path = self.artifacts_dir / digest
        try:
            with path.open("xb") as f:
                f.write(data)
        except FileExistsError:
            pass
        if os.name == "posix":
            path.chmod(0o600)
        with self.connect() as db:
            db.execute("""INSERT OR IGNORE INTO artifacts(id,name,path,size,challenge,created_at)
                VALUES(?,?,?,?,?,?)""", (ref,Path(name).name,str(path),len(data),challenge,_utc()))
        return ref

    def artifact_path(self, ref: str) -> Path:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", ref):
            raise ValueError("invalid artifact reference")
        with self.connect() as db:
            row = db.execute("SELECT path FROM artifacts WHERE id=?", (ref,)).fetchone()
        if row is None:
            raise KeyError("artifact not found")
        path = Path(row["path"])
        if not path.is_relative_to(self.artifacts_dir):
            raise ValueError("artifact escaped campaign root")
        return path
