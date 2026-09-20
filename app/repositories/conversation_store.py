"""会话持久化：独立于单轮 LangGraph State，SQLite 事务保证同一会话串行执行。

每次操作在工作线程中打开/关闭连接，不阻塞事件循环，也不跨线程共享连接。
租约用于回收服务异常退出留下的 running 状态；不是图节点断点续跑。
"""

import asyncio
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from uuid import uuid4


class ConversationNotFound(Exception):
    pass


class ConversationBusy(Exception):
    pass


class ConversationStore:
    LEASE_SECONDS = 180  # 大于查询总超时 120 秒

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    query TEXT NOT NULL, status TEXT NOT NULL,
                    resolved_query TEXT, context_json TEXT, outcome_json TEXT,
                    created_at REAL NOT NULL, finished_at REAL, lease_expires REAL
                );
                CREATE INDEX IF NOT EXISTS turns_by_conversation
                    ON conversation_turns(conversation_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_turn
                    ON conversation_turns(conversation_id) WHERE status = 'running';
            """)
            db.execute("BEGIN IMMEDIATE")
            # 进程重启或客户端在开始消费流之前断开时，最多等待一个租约周期。
            db.execute(
                "UPDATE conversation_turns SET status='cancelled', finished_at=?, "
                "outcome_json=? WHERE status='running' AND lease_expires <= ?",
                (
                    time.time(),
                    json.dumps(
                        {"type": "error", "message": "上次查询已中断，请重新提问。"}
                    ),
                    time.time(),
                ),
            )
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    async def create(self) -> dict:
        def operation():
            now = time.time()
            row = {
                "id": str(uuid4()),
                "title": "新会话",
                "created_at": now,
                "updated_at": now,
            }
            with self._db() as db:
                db.execute(
                    "INSERT INTO conversations VALUES (:id,:title,:created_at,:updated_at)",
                    row,
                )
            return row

        return await asyncio.to_thread(operation)

    async def list(self) -> list[dict]:
        def operation():
            with self._db() as db:
                return [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 50"
                    )
                ]

        return await asyncio.to_thread(operation)

    @staticmethod
    def _require(db, conversation_id):
        row = db.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise ConversationNotFound("会话不存在，请新建会话。")
        return dict(row)

    @staticmethod
    def _turn(row):
        result = dict(row)
        result["context"] = json.loads(result.pop("context_json") or "null")
        result["outcome"] = json.loads(result.pop("outcome_json") or "null")
        return result

    async def get(self, conversation_id: str) -> dict:
        def operation():
            with self._db() as db:
                conversation = self._require(db, conversation_id)
                rows = db.execute(
                    "SELECT * FROM conversation_turns WHERE conversation_id=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 100",
                    (conversation_id,),
                ).fetchall()
                conversation["turns"] = [self._turn(row) for row in reversed(rows)]
                return conversation

        return await asyncio.to_thread(operation)

    async def begin_turn(self, conversation_id: str, query: str) -> dict:
        def operation():
            with self._db() as db:
                self._require(db, conversation_id)
                if db.execute(
                    "SELECT 1 FROM conversation_turns WHERE conversation_id=? AND status='running'",
                    (conversation_id,),
                ).fetchone():
                    raise ConversationBusy(
                        "这个会话还有查询在执行，请等待完成后再提问。"
                    )
                now, turn_id = time.time(), str(uuid4())
                db.execute(
                    "INSERT INTO conversation_turns (id,conversation_id,query,status,created_at,lease_expires) "
                    "VALUES (?,?,?,'running',?,?)",
                    (turn_id, conversation_id, query, now, now + self.LEASE_SECONDS),
                )
                db.execute(
                    "UPDATE conversations SET updated_at=?, title=CASE WHEN title='新会话' THEN ? ELSE title END WHERE id=?",
                    (now, query[:40], conversation_id),
                )
                # 最近三次成功查询 + 尚未完成的澄清。结果行、SQL 和失败记录不传给模型。
                success_rows = db.execute(
                    "SELECT * FROM conversation_turns WHERE conversation_id=? AND status='completed' "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 3",
                    (conversation_id,),
                ).fetchall()
                cutoff = success_rows[0]["created_at"] if success_rows else 0
                pending_rows = db.execute(
                    "SELECT * FROM conversation_turns WHERE conversation_id=? AND status='clarification' "
                    "AND created_at>? ORDER BY created_at DESC, rowid DESC LIMIT 2",
                    (conversation_id, cutoff),
                ).fetchall()
                history = []
                for row in sorted(
                    [*success_rows, *pending_rows], key=lambda row: row["created_at"]
                ):
                    turn = self._turn(row)
                    history.append(
                        {
                            "query": turn["query"],
                            "resolved_query": turn["resolved_query"],
                            "context": turn["context"],
                            "status": turn["status"],
                            "clarification": (turn["outcome"] or {}).get("message"),
                        }
                    )
                return {
                    "id": turn_id,
                    "conversation_id": conversation_id,
                    "history": history,
                }

        return await asyncio.to_thread(operation)

    async def finish_turn(
        self,
        turn_id: str,
        status: str,
        outcome: dict,
        resolved_query: str | None = None,
        context: dict | None = None,
    ) -> bool:
        if status not in {
            "completed",
            "clarification",
            "unsupported",
            "error",
            "cancelled",
        }:
            raise ValueError("Invalid terminal status")

        def operation():
            with self._db() as db:
                now = time.time()
                updated = db.execute(
                    "UPDATE conversation_turns SET status=?,outcome_json=?,resolved_query=?,context_json=?,finished_at=? "
                    "WHERE id=? AND status='running' AND lease_expires>?",
                    (
                        status,
                        json.dumps(outcome, ensure_ascii=False, default=str),
                        resolved_query,
                        json.dumps(context, ensure_ascii=False),
                        now,
                        turn_id,
                        now,
                    ),
                ).rowcount
                if updated:
                    db.execute(
                        "UPDATE conversations SET updated_at=? WHERE id=(SELECT conversation_id FROM conversation_turns WHERE id=?)",
                        (now, turn_id),
                    )
                return bool(updated)

        return await asyncio.to_thread(operation)


@lru_cache
def get_conversation_store() -> ConversationStore:
    default = Path(__file__).resolve().parents[2] / "data" / "conversations.sqlite3"
    return ConversationStore(os.environ.get("CONVERSATION_DB_PATH", str(default)))
