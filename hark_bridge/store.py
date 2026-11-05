"""账号库：SQLite + Fernet 加密会话。

凭据从不以明文落盘。密钥从 HARK_BRIDGE_KEY 读取，缺失直接抛错，
不会退化成明文存储。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .state import AccountState

KEY_ENV = "HARK_BRIDGE_KEY"
DB_ENV = "HARK_BRIDGE_DB"
DEFAULT_DB = "./data/accounts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account            TEXT PRIMARY KEY,
    label              TEXT NOT NULL DEFAULT '',
    state              TEXT NOT NULL,
    cookies_enc        BLOB,
    session_id         TEXT,
    user_id            TEXT,
    session_expires_at INTEGER,
    last_probe_ok_at   INTEGER,
    last_probe_status  INTEGER,
    last_error         TEXT,
    probe_attempts     INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_state ON accounts(state);
"""


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def load_cipher() -> Fernet:
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        raise ConfigError(
            f"缺少 {KEY_ENV}。生成方式：\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{KEY_ENV} 不是合法的 Fernet 密钥") from exc


@dataclass(frozen=True)
class Account:
    account: str
    label: str
    state: AccountState
    session_id: str | None
    user_id: str | None
    session_expires_at: int | None
    last_probe_ok_at: int | None
    last_probe_status: int | None
    last_error: str | None
    probe_attempts: int
    created_at: int
    updated_at: int


class AccountStore:
    """账号状态与加密会话的唯一来源。"""

    def __init__(self, path: str | Path | None = None) -> None:
        resolved = str(path or os.environ.get(DB_ENV, DEFAULT_DB))
        self.path = Path(resolved)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = load_cipher()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- 加密 ----------------------------------------------------------

    def _encrypt(self, data: dict[str, str]) -> bytes:
        return self._cipher.encrypt(json.dumps(data, ensure_ascii=False).encode())

    def _decrypt(self, blob: bytes | None) -> dict[str, str]:
        if not blob:
            return {}
        try:
            return json.loads(self._cipher.decrypt(blob).decode())
        except (InvalidToken, ValueError):
            # 密钥换了但库没重建：把账号当作未验证，不要抛错打断整个池
            return {}

    # ---- 写入 ----------------------------------------------------------

    def save_session(
        self,
        account: str,
        *,
        cookies: dict[str, str],
        session_id: str | None,
        user_id: str | None,
        session_expires_at: int | None,
        state: AccountState,
        probe_status: int | None = None,
        label: str = "",
    ) -> None:
        now = int(time.time())
        payload = self._encrypt(cookies)
        self._conn.execute(
            """
            INSERT INTO accounts (
                account, label, state, cookies_enc, session_id, user_id,
                session_expires_at, last_probe_ok_at, last_probe_status,
                last_error, probe_attempts, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)
            ON CONFLICT(account) DO UPDATE SET
                label              = excluded.label,
                state              = excluded.state,
                cookies_enc        = excluded.cookies_enc,
                session_id         = excluded.session_id,
                user_id            = excluded.user_id,
                session_expires_at = excluded.session_expires_at,
                last_probe_ok_at   = excluded.last_probe_ok_at,
                last_probe_status  = excluded.last_probe_status,
                last_error         = NULL,
                updated_at         = excluded.updated_at
            """,
            (
                account,
                label,
                state.value,
                payload,
                session_id,
                user_id,
                session_expires_at,
                now if state in (AccountState.SESSION_VALID, AccountState.SESSION_EXPIRING) else None,
                probe_status,
                None,  # last_error
                now,   # created_at
                now,   # updated_at
            ),
        )
        self._conn.commit()

    def mark_failure(
        self,
        account: str,
        *,
        state: AccountState,
        error: str,
        probe_status: int | None = None,
        clear_credentials: bool = False,
    ) -> None:
        now = int(time.time())
        if clear_credentials:
            self._conn.execute(
                """
                UPDATE accounts SET state=?, last_error=?, last_probe_status=?,
                    cookies_enc=NULL, session_id=NULL, session_expires_at=NULL,
                    probe_attempts=probe_attempts+1, updated_at=?
                WHERE account=?
                """,
                (state.value, error, probe_status, now, account),
            )
        else:
            self._conn.execute(
                """
                UPDATE accounts SET state=?, last_error=?, last_probe_status=?,
                    probe_attempts=probe_attempts+1, updated_at=?
                WHERE account=?
                """,
                (state.value, error, probe_status, now, account),
            )
        self._conn.commit()

    def delete(self, account: str) -> bool:
        cursor = self._conn.execute("DELETE FROM accounts WHERE account=?", (account,))
        self._conn.commit()
        return cursor.rowcount > 0

    # ---- 读取 ----------------------------------------------------------

    def get(self, account: str) -> Account | None:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE account=?", (account,)
        ).fetchone()
        return _row_to_account(row) if row else None

    def cookies(self, account: str) -> dict[str, str]:
        row = self._conn.execute(
            "SELECT cookies_enc FROM accounts WHERE account=?", (account,)
        ).fetchone()
        return self._decrypt(row["cookies_enc"]) if row else {}

    def list_all(self) -> list[Account]:
        rows = self._conn.execute(
            "SELECT * FROM accounts ORDER BY account"
        ).fetchall()
        return [_row_to_account(row) for row in rows]

    def dispatchable(self, *, now: int | None = None) -> list[Account]:
        """返回当前可派单的账号。"""
        from .state import is_dispatchable

        moment = now if now is not None else int(time.time())
        result: list[Account] = []
        for record in self.list_all():
            if is_dispatchable(
                record.state,
                now=moment,
                last_probe_ok_at=record.last_probe_ok_at,
                session_expires_at=record.session_expires_at,
            ):
                result.append(record)
        return result

    def redacted(self) -> list[dict[str, object]]:
        """给 /admin/accounts 用的脱敏快照，不含任何凭据。"""
        return [
            {
                "account": record.account,
                "label": record.label,
                "state": record.state.value,
                "session_expires_at": record.session_expires_at,
                "last_probe_ok_at": record.last_probe_ok_at,
                "last_probe_status": record.last_probe_status,
                "last_error": record.last_error,
                "probe_attempts": record.probe_attempts,
                "updated_at": record.updated_at,
            }
            for record in self.list_all()
        ]

    def close(self) -> None:
        self._conn.close()


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        account=row["account"],
        label=row["label"] or "",
        state=AccountState(row["state"]),
        session_id=row["session_id"],
        user_id=row["user_id"],
        session_expires_at=row["session_expires_at"],
        last_probe_ok_at=row["last_probe_ok_at"],
        last_probe_status=row["last_probe_status"],
        last_error=row["last_error"],
        probe_attempts=row["probe_attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
