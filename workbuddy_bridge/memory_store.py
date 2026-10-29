"""Persist scoped QQ memories without depending on the framework or a model."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

RAW_TTL = 7 * 86400
RAW_LIMIT = 2000
MEMORY_LIMIT = 500
CATEGORIES = frozenset({"preference", "fact", "topic"})
GROUP_SUBJECT = "__group_memory__"
MESSAGE_KINDS = frozenset(
    {"chat", "question", "command", "attachment", "roleplay", "oversize"}
)
TOPIC_HINTS = frozenset({"preference", "plan", "fact", "other"})


class MemoryStoreError(RuntimeError):
    """Report an unavailable or incompatible local memory store."""


@dataclass(frozen=True)
class Scope:
    """Identify a conversation using trusted platform metadata."""

    platform: str
    bot_id: str
    kind: str
    conversation_id: str

    def __post_init__(self):
        if self.kind not in {"group", "private"} or any(
            not isinstance(value, str) or not value.strip() or len(value) > 256
            for value in (self.platform, self.bot_id, self.conversation_id)
        ):
            raise ValueError("Invalid memory scope")

    @property
    def key(self) -> str:
        return json.dumps(
            [self.platform, self.bot_id, self.kind, self.conversation_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class Memory:
    id: int
    subject: str
    name: str
    category: str
    key: str
    content: str
    source_kind: str
    updated_at: float
    expires_at: float | None
    source_at: float = 0.0
    group_name: str = ""
    context: tuple[Source, ...] = ()


@dataclass(frozen=True)
class GroupOverview:
    """Expose a group index without revealing individual memory contents."""

    intro: str
    member_count: int
    personal_count: int
    group_count: int
    last_message_at: float


@dataclass(frozen=True)
class MemoryGroup:
    """Describe a live topic without creating another factual memory."""

    title: str
    intro: str
    count: int


@dataclass(frozen=True)
class Pending:
    """Identify durable unprocessed input within a caller-approved scope."""

    scope: Scope
    subject: str
    last_message_at: float
    oldest_message_at: float


@dataclass(frozen=True)
class Source:
    ref: str
    text: str
    name: str
    created_at: float
    subject: str = ""


@dataclass(frozen=True)
class Batch:
    scope: Scope
    subject: str
    epoch: int
    proof: str
    messages: tuple[Source, ...]
    upper_id: int
    context: tuple[Source, ...] = ()
    epochs: tuple[tuple[str, int], ...] = ()


def _key(value: str) -> str:
    canonical = unicodedata.normalize("NFKC", value).casefold()
    canonical = "".join(char for char in canonical if char.isalnum())
    if not canonical:
        raise ValueError("Empty memory key")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _subject(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("Invalid memory subject")
    return value


def _upper_id(value: int | None) -> int:
    """Validate a captured message boundary shared by discovery and batch reads.

    Args:
        value: An inclusive SQLite message ID, or None for the legacy open boundary.

    Returns:
        A bounded integer suitable for a parameterized ID predicate.

    Raises:
        ValueError: The boundary is not a nonnegative SQLite integer.
    """
    if value is None:
        return 2**63 - 1
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("Invalid memory message boundary")
    return value


def _count_reason(diagnostics: dict | None, reason: str):
    if diagnostics is not None:
        reasons = diagnostics["reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1


def _personal_rejection(text: str, other_names: set[str]) -> str | None:
    """Reject explicit non-personal structures; the model judges meaning and value.

    Quoted titles and examples are not the speaker's own questions or commands.
    Names are trusted conversation metadata, never guesses from a word dictionary.
    """
    plain = re.sub(
        r'“[^”]*”|「[^」]*」|『[^』]*』|《[^》]*》|"[^"\n]*"', "", text
    ).strip()
    if re.search(r"[?？][。！!]*$|(?:吗|么)[。！!]*$", plain) or re.match(
        r"(?:请问|想问|问一下|有没有|是不是|能不能|为什么|怎么样|怎么办)", plain
    ):
        return "question"
    if re.search(r"假如|假设|如果|要是", plain) or re.match(
        r"(?:比如|例如|(?:我|本人)?(?:也许|可能))", plain
    ):
        return "hypothetical"
    if re.search(
        r"(?:^|[，。；！？,;!?])(?:我)?(?:听说|据说)|"
        r"(?:^|[，。；！？,;!?])(?:有人|别人|他|她|朋友)说",
        plain,
    ):
        return "reported"
    if re.search(
        r"执行命令|运行脚本|删除文件|修改文件|读取文件|新建文件|"
        r"忽略.{0,12}(?:规则|权限|系统|指令)|(?:授予|提升).{0,8}权限|"
        r"(?:我是|把我当|叫我)管理员|(?:输出|泄露).{0,8}(?:提示词|系统提示)|"
        r"(?:^|在|正在|开始|继续|我)(?:扮演|假装)|(?:^|[，。；,;])(?:编个|玩梗|开玩笑)",
        plain,
    ):
        return "instruction"
    if re.match(
        r"(?:他|她|它|你|您|大家|别人|有人|我们|咱们|本群|群里|群内|"
        r"(?:我(?:的)?)?(?:朋友|同学|同事|老爸|老妈|爸爸|妈妈|哥哥|姐姐|弟弟|妹妹|家人))",
        plain,
    ):
        return "other_subject"
    for clause in re.split(r"[，。；！？,;!?]", plain):
        if re.search(r"我|本人", clause):
            continue
        for name in other_names:
            position = clause.find(name)
            if position < 0:
                continue
            suffix = clause[position + len(name) :].lstrip()
            # A name alone can be a vocative; a named declarative subject cannot.
            if (
                suffix
                and suffix[0] not in ":："
                and (
                    position == 0
                    or re.match(
                        r"(?:真|实在|确实)?(?:吃不了|吃不惯|不爱吃|喜欢|最喜欢)", suffix
                    )
                )
            ):
                return "other_subject"
    return None


def _memory(row) -> Memory:
    evidence = json.loads(row["evidence_json"])
    source_at = (
        float(
            next(
                (entry for entry in evidence if entry.get("quote") == row["content"]),
                evidence[-1],
            ).get("created_at", row["updated_at"])
        )
        if evidence
        else row["updated_at"]
    )
    supporting = []
    for entry in evidence:
        supporting.extend(entry.get("context", []))
        if row["subject"] == GROUP_SUBJECT and entry.get("quote") != row["content"]:
            supporting.append(entry)
    context = tuple(
        Source(
            str(entry.get("source", "")),
            str(entry["quote"]),
            str(entry.get("name", "")),
            float(entry.get("created_at", source_at)),
            str(entry.get("subject", "")),
        )
        for entry in supporting
        if isinstance(entry, dict) and entry.get("quote")
    )
    return Memory(
        id=row["id"],
        subject=row["subject"],
        name=row["name"],
        category=row["category"],
        key=row["key_label"],
        content=row["content"],
        source_kind=row["source_kind"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
        source_at=source_at,
        group_name=row["group_name"],
        context=context,
    )


def _terms(value: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_]{2,}", value.casefold()))
    for span in re.findall(r"[\u3400-\u9fff]+", value):
        words.update(span[i : i + 2] for i in range(len(span) - 1))
    return words - {
        "什么",
        "记得",
        "记忆",
        "知道",
        "之前",
        "我的",
        "你的",
        "这个",
        "那个",
    }


GROUP_RULES = (
    (
        "饮食偏好",
        "饮食|口味|忌口|香菜|辣|甜食|米饭|面条|饺子|咖啡|奶茶|喝茶|普洱|红茶|龙井|爱吃|不吃|吃不了|吃不惯",
        "口味、忌口和饮品",
    ),
    (
        "称呼与交流",
        "称呼|叫我|昵称|名字|回复方式|说话方式|表达习惯|交流",
        "称呼和交流习惯",
    ),
    (
        "游戏娱乐",
        "游戏|在玩|开黑|星露谷|原神|崩铁|电影|音乐|动漫|看番|听歌|娱乐",
        "游戏、作品和娱乐喜好",
    ),
    (
        "工作学习",
        "工作|学习|上班|专业|考试|考研|编程|python|课程|学校|毕业|复习",
        "工作、学习和技能",
    ),
    (
        "日常习惯",
        "习惯|每天|作息|睡觉|起床|运动|跑步|健身|周末|早睡|晚睡",
        "作息、运动和日常习惯",
    ),
    (
        "近期计划",
        "打算|计划|准备|下周|明天|旅游|出差|搬家|装机",
        "近期安排和进行中的事情",
    ),
)


def _group_name(proposed: str, category: str, key: str, content: str) -> str:
    """Normalize an index label or infer one from current source text.

    Args:
        proposed: Optional short model-selected topic, never an access scope.
        category: Storage lifetime category.
        key: Existing fact topic used only when the source has no known topic.
        content: Current original statement, preferred over an old fact key.

    Returns:
        A bounded topic label suitable for indexing and a group selector.
    """
    if proposed:
        if not isinstance(proposed, str):
            raise ValueError("Invalid memory group")
        label = unicodedata.normalize("NFKC", proposed).strip()
        if not 2 <= len(label) <= 24 or re.search(
            r"[\r\n\t<>\[\]{}]|[\x00-\x1f]", label
        ):
            raise ValueError("Invalid memory group")
        return label
    for value in (content, key):
        for title, pattern, _intro in GROUP_RULES:
            if re.search(pattern, value, flags=re.IGNORECASE):
                return title
    return "近期计划" if category == "topic" else "其他自述"


def _groups(records: list[Memory]) -> list[MemoryGroup]:
    """Build descriptions exclusively from currently visible group members.

    Args:
        records: Already scoped and time-filtered memories in display order.

    Returns:
        Topic counts and short index descriptions, without copied old facts.
    """
    buckets: dict[str, list[Memory]] = {}
    for item in records:
        buckets.setdefault(item.group_name, []).append(item)
    descriptions = {title: intro for title, _pattern, intro in GROUP_RULES}
    groups = []
    for title, members in buckets.items():
        labels = []
        for item in members:
            label = " ".join(item.key.split())
            # A fact key may itself be an old whole statement after a correction.
            # Such labels must never become stale factual summaries.
            if (
                2 <= len(label) <= 16
                and _terms(label) & _terms(item.content)
                and not re.search(
                    r"^(?:我|本人)|喜欢|不吃|爱吃|以前|现在|不能|可以|不要|[。！？?]",
                    label,
                )
                and label != title
                and label not in labels
            ):
                labels.append(label)
        about = descriptions.get(title, f"{title}相关信息")
        detail = "；涉及" + "、".join(labels[:2]) if labels else ""
        intro = f"记录{about}{detail}，共{len(members)}条。"
        if len(intro) > 80:
            intro = f"记录{about}，共{len(members)}条。"
        groups.append(MemoryGroup(title, intro, len(members)))
    return groups


class Store:
    """Open short lived SQLite connections only when a memory operation runs.

    Args:
        path: Trusted plugin-owned database location, never supplied by QQ text.
    """

    def __init__(self, path, *, generation=None):
        self.path = Path(path)
        if generation is not None and (
            not isinstance(generation, str) or not 1 <= len(generation) <= 128
        ):
            raise ValueError("Invalid memory generation")
        self._generation = generation
        self._ready = False
        self._closed = threading.Event()
        self._init_lock = threading.Lock()
        # Serialize this instance's writes; SQLite still coordinates other instances.
        self._write_lock = threading.Lock()

    def close(self):
        """Reject queued work immediately; running transactions recheck before commit."""
        self._closed.set()

    @contextmanager
    def _db(self, *, write=False):
        if self._closed.is_set():
            raise MemoryStoreError("Memory store closed")
        acquired = False
        connection = None
        try:
            if write:
                deadline = time.monotonic() + 10
                while not acquired:
                    if self._closed.is_set():
                        raise MemoryStoreError("Memory store closed")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MemoryStoreError("Memory database busy")
                    acquired = self._write_lock.acquire(timeout=min(0.1, remaining))
            if self._closed.is_set():
                raise MemoryStoreError("Memory store closed")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Short write transactions can queue without being treated as corruption.
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            with self._init_lock:
                if self._closed.is_set():
                    raise MemoryStoreError("Memory store closed")
                if not self._ready:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version not in (0, 1, 2, 3, 4):
                        raise MemoryStoreError("Unsupported memory database version")
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("BEGIN IMMEDIATE")
                    if self._closed.is_set():
                        raise MemoryStoreError("Memory store closed")
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version not in (0, 1, 2, 3, 4):
                        raise MemoryStoreError("Unsupported memory database version")
                    if version == 0:
                        schema = """
                        CREATE TABLE IF NOT EXISTS state (
                          scope TEXT NOT NULL, subject TEXT NOT NULL,
                          epoch INTEGER NOT NULL DEFAULT 0,
                          processed_id INTEGER NOT NULL DEFAULT 0,
                          PRIMARY KEY(scope, subject));
                        CREATE TABLE IF NOT EXISTS messages (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          scope TEXT NOT NULL, subject TEXT NOT NULL,
                          message_id TEXT NOT NULL, name TEXT NOT NULL,
                          text TEXT NOT NULL, created_at REAL NOT NULL,
                          UNIQUE(scope, message_id));
                        CREATE INDEX IF NOT EXISTS messages_subject ON messages(scope, subject, id);
                        CREATE INDEX IF NOT EXISTS messages_time ON messages(scope, created_at);
                        CREATE TABLE IF NOT EXISTS memories (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          scope TEXT NOT NULL, subject TEXT NOT NULL,
                          name TEXT NOT NULL, category TEXT NOT NULL,
                          key_hash TEXT NOT NULL, key_label TEXT NOT NULL,
                          content TEXT NOT NULL, source_kind TEXT NOT NULL,
                          status TEXT NOT NULL DEFAULT 'active',
                          version INTEGER NOT NULL DEFAULT 1,
                          evidence_json TEXT NOT NULL DEFAULT '[]',
                          created_at REAL NOT NULL, updated_at REAL NOT NULL,
                          expires_at REAL,
                          UNIQUE(scope, subject, category, key_hash));
                        CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope, subject, status);
                        CREATE TABLE IF NOT EXISTS revisions (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          scope TEXT NOT NULL, subject TEXT NOT NULL,
                          memory_id INTEGER NOT NULL,
                          old_content TEXT NOT NULL, new_content TEXT NOT NULL,
                          reason TEXT NOT NULL, created_at REAL NOT NULL)
                        """
                        for statement in schema.split(";"):
                            if statement.strip():
                                connection.execute(statement)
                    if version < 2:
                        connection.execute(
                            "ALTER TABLE memories ADD COLUMN group_name TEXT NOT NULL DEFAULT ''"
                        )
                        connection.execute(
                            "ALTER TABLE state ADD COLUMN retry_after REAL NOT NULL DEFAULT 0"
                        )
                        for row in connection.execute(
                            "SELECT id,category,key_label,content FROM memories WHERE status!='deleted'"
                        ).fetchall():
                            connection.execute(
                                "UPDATE memories SET group_name=? WHERE id=?",
                                (
                                    _group_name(
                                        "",
                                        row["category"],
                                        row["key_label"],
                                        row["content"],
                                    ),
                                    row["id"],
                                ),
                            )
                        connection.execute(
                            "CREATE INDEX IF NOT EXISTS memories_group ON memories(scope,subject,group_name,status)"
                        )

                    if version < 3:
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN eligible INTEGER NOT NULL DEFAULT 1"
                        )
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN received_at REAL NOT NULL DEFAULT 0"
                        )
                        connection.execute("UPDATE messages SET received_at=created_at")
                        connection.execute(
                            "CREATE TABLE runtime_generation (id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL)"
                        )
                    if version < 4:
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN message_kind TEXT NOT NULL DEFAULT 'chat' "
                            "CHECK(message_kind IN ('chat','question','command','attachment','roleplay','oversize'))"
                        )
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN topic_hint TEXT NOT NULL DEFAULT 'other' "
                            "CHECK(topic_hint IN ('preference','plan','fact','other'))"
                        )
                        connection.execute("PRAGMA user_version=4")
                    if self._generation is not None:
                        connection.execute(
                            "INSERT INTO runtime_generation(id,token) VALUES(1,?) "
                            "ON CONFLICT(id) DO UPDATE SET token=excluded.token",
                            (self._generation,),
                        )
                    if self._closed.is_set():
                        raise MemoryStoreError("Memory store closed")
                    connection.commit()
                    self._ready = True
            if write:
                connection.execute("BEGIN IMMEDIATE")
                if self._closed.is_set():
                    raise MemoryStoreError("Memory store closed")
                if self._generation is not None:
                    current = connection.execute(
                        "SELECT token FROM runtime_generation WHERE id=1"
                    ).fetchone()
                    if current is None or current[0] != self._generation:
                        raise MemoryStoreError("Memory store retired")
            yield connection
            if write:
                if self._closed.is_set():
                    raise MemoryStoreError("Memory store closed")
                connection.commit()
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
            raise MemoryStoreError("Memory database unavailable") from None
        except MemoryStoreError:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
            if acquired:
                self._write_lock.release()

    def _barrier(self, db, scope: Scope, subject: str):
        """Invalidate captured extraction work and skip older input atomically."""
        upper = db.execute(
            "SELECT COALESCE(MAX(id),0) FROM messages WHERE scope=? AND subject=?",
            (scope.key, subject),
        ).fetchone()[0]
        db.execute(
            "UPDATE messages SET eligible=0 WHERE scope=? AND subject=? AND id<=?",
            (scope.key, subject, upper),
        )
        db.execute(
            "INSERT INTO state(scope,subject,epoch,processed_id) VALUES(?,?,1,?) "
            "ON CONFLICT(scope,subject) DO UPDATE SET epoch=epoch+1, "
            "processed_id=MAX(processed_id,excluded.processed_id)",
            (scope.key, subject, upper),
        )

    def _invalidate_dependencies(self, db, scope, subject):
        """Erase other records whose stored evidence depends on this member.

        Args:
            db: Current write transaction.
            scope: Trusted conversation containing the changed evidence.
            subject: Member whose previous statements must no longer be reused.
        """
        rows = db.execute(
            "SELECT id,evidence_json FROM memories WHERE scope=? AND subject!=?",
            (scope.key, subject),
        ).fetchall()
        for row in rows:
            evidence = json.loads(row["evidence_json"])
            sources = [*evidence]
            for item in evidence:
                sources.extend(item.get("context", []))
            if any(item.get("subject") == subject for item in sources):
                db.execute("DELETE FROM revisions WHERE memory_id=?", (row["id"],))
                db.execute("DELETE FROM memories WHERE id=?", (row["id"],))

    def _save(
        self,
        db,
        scope,
        subject,
        name,
        category,
        key,
        content,
        source_kind,
        evidence,
        now,
        group_name="",
        diagnostics=None,
    ):
        key_hash = _key(key)
        group_name = _group_name(group_name, category, key, content)
        if (
            source_kind == "extracted"
            and db.execute(
                "SELECT 1 FROM memories WHERE scope=? AND subject=? AND key_hash=? AND status='deleted'",
                (scope.key, subject, key_hash),
            ).fetchone()
        ):
            _count_reason(diagnostics, "forgotten_key")
            return None
        if (
            source_kind == "extracted"
            and db.execute(
                "SELECT 1 FROM memories WHERE scope=? AND subject=? AND content=? "
                "AND status='active' AND (expires_at IS NULL OR expires_at>?)",
                (scope.key, subject, content, now),
            ).fetchone()
        ):
            _count_reason(diagnostics, "duplicate")
            return None
        old = db.execute(
            "SELECT * FROM memories WHERE scope=? AND subject=? AND category=? AND key_hash=?",
            (scope.key, subject, category, key_hash),
        ).fetchone()
        if (
            source_kind == "extracted"
            and old is not None
            and (old["status"] == "deleted" or old["source_kind"] == "explicit")
        ):
            _count_reason(
                diagnostics,
                "forgotten_key" if old["status"] == "deleted" else "explicit_protected",
            )
            return None
        needs_slot = (
            old is None
            or old["status"] != "active"
            or (old["expires_at"] is not None and old["expires_at"] <= now)
        )
        if needs_slot:
            count = db.execute(
                "SELECT COUNT(*) FROM memories WHERE scope=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at>?)",
                (scope.key, now),
            ).fetchone()[0]
            if count >= MEMORY_LIMIT:
                if source_kind == "extracted":
                    _count_reason(diagnostics, "capacity")
                    return None
                raise MemoryStoreError("Memory capacity reached")
        anchor = now
        if source_kind == "extracted" and evidence:
            anchor = min(now, float(evidence[0]["created_at"]))
        expires = anchor + RAW_TTL if category == "topic" else None
        if old is not None:
            if old["content"] != content:
                db.execute(
                    "INSERT INTO revisions(scope,subject,memory_id,old_content,new_content,reason,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        scope.key,
                        subject,
                        old["id"],
                        old["content"],
                        content,
                        source_kind,
                        now,
                    ),
                )
            db.execute(
                "UPDATE memories SET name=?,key_label=?,content=?,source_kind=?,status='active',"
                "version=version+1,evidence_json=?,updated_at=?,expires_at=?,group_name=? WHERE id=?",
                (
                    name or old["name"],
                    key,
                    content,
                    source_kind,
                    json.dumps(evidence, ensure_ascii=False),
                    now,
                    expires,
                    group_name,
                    old["id"],
                ),
            )
            memory_id = old["id"]
        else:
            memory_id = db.execute(
                "INSERT INTO memories(scope,subject,name,category,key_hash,key_label,content,source_kind,"
                "evidence_json,created_at,updated_at,expires_at,group_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope.key,
                    subject,
                    name,
                    category,
                    key_hash,
                    key,
                    content,
                    source_kind,
                    json.dumps(evidence, ensure_ascii=False),
                    now,
                    now,
                    expires,
                    group_name,
                ),
            ).lastrowid
        return _memory(
            db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        )

    def record(
        self,
        scope,
        subject,
        name,
        message_id,
        text,
        now=None,
        *,
        eligible=True,
        received_at=None,
        message_kind="chat",
    ) -> bool:
        """Record bounded raw input with local classification, never a factual promotion.

        Args:
            scope: Trusted conversation boundary from the adapter.
            subject: Original member identity within that boundary.
            name: Display name from the original message metadata.
            message_id: Stable original message identifier for deduplication.
            text: Raw message text, retained within the configured bound.
            now: Optional original-message timestamp.
            eligible: Whether later model consolidation may use this message.
            received_at: Optional local receive timestamp, independent of original time.
            message_kind: Fixed source label; chat infers obvious questions or commands.

        Returns:
            Whether a new source row was inserted.

        Raises:
            ValueError: A subject or source label is invalid.
        """
        _subject(subject)
        if subject == GROUP_SUBJECT:
            raise ValueError("Reserved memory subject")
        if not isinstance(message_kind, str) or message_kind not in MESSAGE_KINDS:
            raise ValueError("Invalid memory message kind")
        if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
            return False
        if not isinstance(text, str) or not text.strip():
            return False
        text = text.strip()
        if message_kind == "chat":
            if len(text) > 1000:
                message_kind = "oversize"
            elif text.startswith("/"):
                message_kind = "command"
            elif re.search(r"[?？][。！!]*$|(?:吗|么)[。！!]*$", text) or re.match(
                r"(?:请问|想问|问一下|有没有|是不是|能不能|为什么|怎么样|怎么办)", text
            ):
                message_kind = "question"
        topic_hint = "other"
        # These cheap labels guide inspection only; they do not establish truth,
        # ownership, eligibility, or permission to create a long-term memory.
        if message_kind == "chat":
            if re.search(r"喜欢|偏好|不爱|讨厌|习惯|不吃|爱吃|不加糖|不喝", text):
                topic_hint = "preference"
            elif re.search(r"打算|计划|准备|备考|下(?:周|月|次)|明年|明天", text):
                topic_hint = "plan"
            elif re.match(r"(?:我|本人)(?:是|在|住|做|就读|毕业|工作|叫)", text):
                topic_hint = "fact"
        now = time.time() if now is None else float(now)
        received_at = now if received_at is None else float(received_at)
        with self._db(write=True) as db:
            result = (
                db.execute(
                    "INSERT OR IGNORE INTO messages(scope,subject,message_id,name,text,created_at,eligible,received_at,message_kind,topic_hint) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        scope.key,
                        subject,
                        message_id,
                        str(name or "")[:80],
                        text[:1000],
                        now,
                        int(bool(eligible) and len(text) <= 1000),
                        received_at,
                        message_kind,
                        topic_hint,
                    ),
                ).rowcount
                == 1
            )
            db.execute(
                "DELETE FROM messages WHERE scope=? AND (created_at<? OR id NOT IN "
                "(SELECT id FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?))",
                (scope.key, now - RAW_TTL, scope.key, RAW_LIMIT),
            )
            return result

    def capture_upper_id(self) -> int:
        """Capture the inclusive raw-message boundary for one manual collection pass.

        Returns:
            The greatest currently retained message ID, or zero for an empty database.
        """
        with self._db() as db:
            return int(
                db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
            )

    def list_own(self, scope, subject, limit=20, now=None) -> list[Memory]:
        _subject(subject)
        if subject == GROUP_SUBJECT and scope.kind != "group":
            return []
        now = time.time() if now is None else float(now)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE scope=? AND subject=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY updated_at DESC,id DESC LIMIT ?",
                (scope.key, subject, now, max(0, min(int(limit), MEMORY_LIMIT))),
            ).fetchall()
        return [_memory(row) for row in rows]

    def list_groups(self, scope, subject, limit=20, now=None) -> list[MemoryGroup]:
        """List topic introductions derived from the caller's live memories.

        Args:
            scope: Trusted conversation boundary.
            subject: The member whose own memory is being inspected.
            limit: Maximum number of groups, bounded by the memory capacity.
            now: Optional clock override for expiration checks.

        Returns:
            Fresh topic introductions and counts; no stale cached descriptions.
        """
        records = self.list_own(scope, subject, limit=MEMORY_LIMIT, now=now)
        return _groups(records)[: max(0, min(int(limit), MEMORY_LIMIT))]

    def list_group(self, scope, subject, title, limit=20, now=None) -> list[Memory]:
        """Expand one exact topic within an already authorized member scope."""
        _subject(subject)
        if subject == GROUP_SUBJECT and scope.kind != "group":
            return []
        if not isinstance(title, str) or not title.strip() or len(title) > 24:
            return []
        now = time.time() if now is None else float(now)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE scope=? AND subject=? AND group_name=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY updated_at DESC,id DESC LIMIT ?",
                (
                    scope.key,
                    subject,
                    title.strip(),
                    now,
                    max(0, min(int(limit), MEMORY_LIMIT)),
                ),
            ).fetchall()
        return [_memory(row) for row in rows]

    def group_overview(self, scope, now=None) -> GroupOverview:
        """Build the group parent index without including private child contents.

        Args:
            scope: Trusted group conversation; private chats are rejected.
            now: Optional clock for expiration and source retention checks.

        Returns:
            Current common-topic introduction, child counts and latest source time.
        """
        if scope.kind != "group":
            raise ValueError("Group overview requires a group conversation")
        now = time.time() if now is None else float(now)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE scope=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY updated_at DESC,id DESC",
                (scope.key, now),
            ).fetchall()
            raw = db.execute(
                "SELECT subject,MAX(created_at) AS latest FROM messages WHERE scope=? "
                "AND text!='' AND created_at>=? GROUP BY subject",
                (scope.key, now - RAW_TTL),
            ).fetchall()
        common = [_memory(row) for row in rows if row["subject"] == GROUP_SUBJECT]
        personal = [row for row in rows if row["subject"] != GROUP_SUBJECT]
        members = {row["subject"] for row in personal} | {
            row["subject"] for row in raw if row["subject"] != GROUP_SUBJECT
        }
        topics = [group.title for group in _groups(common)]
        intro = (
            "本群共同记忆涉及" + "、".join(topics[:3]) + f"，共{len(common)}条。"
            if topics
            else "本群暂未形成共同记忆，成员记忆分别保存。"
        )
        return GroupOverview(
            intro,
            len(members),
            len(personal),
            len(common),
            max((float(row["latest"]) for row in raw), default=0.0),
        )

    def known_scopes(
        self,
        platform,
        bot_id,
        limit=128,
        now=None,
        quiet_sec=120,
        offset=0,
        *,
        upper_id=None,
        ignore_retry=False,
    ) -> tuple[Scope, ...]:
        """Recover only pending conversation metadata for a confirmed adapter account.

        Args:
            platform: Exact platform instance confirmed by an adapter event.
            bot_id: Exact bot account confirmed by that same event.
            limit: Maximum returned conversation metadata entries.
            now: Optional trusted clock for raw retention and retry eligibility.
            quiet_sec: Required silence for at least one pending member in a scope.
            offset: Nonnegative page offset within this confirmed account only.
            upper_id: Optional inclusive boundary captured when a manual pass started.
            ignore_retry: Explicit manual pass may bypass persisted retry delays only.

        Returns:
            Valid scopes with pending input, oldest first; no message text.
        """
        _subject(platform)
        _subject(bot_id)
        upper_id = _upper_id(upper_id)
        if type(ignore_retry) is not bool:
            raise ValueError("Invalid memory retry override")
        if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
            raise ValueError("Invalid memory scope offset")
        limit = max(0, min(int(limit), 256))
        if not limit:
            return ()
        now = time.time() if now is None else float(now)
        quiet = max(0, min(float(quiet_sec), 3600))
        prefix = (
            json.dumps([platform, bot_id], ensure_ascii=False, separators=(",", ":"))[
                :-1
            ]
            + ","
        )
        with self._db() as db:
            rows = db.execute(
                "SELECT scope,MIN(oldest) AS first_pending FROM ("
                "SELECT m.scope,MIN(m.created_at) AS oldest FROM messages m "
                "LEFT JOIN state s ON s.scope=m.scope AND s.subject=m.subject "
                "WHERE substr(m.scope,1,?)=? AND m.text!='' AND m.eligible=1 AND m.created_at>=? "
                "AND m.id>COALESCE(s.processed_id,0) AND (? OR COALESCE(s.retry_after,0)<=?) "
                "AND m.id<=? "
                "GROUP BY m.scope,m.subject HAVING MAX(m.created_at)<=?) "
                "GROUP BY scope ORDER BY first_pending,scope LIMIT ? OFFSET ?",
                (
                    len(prefix),
                    prefix,
                    now - RAW_TTL,
                    ignore_retry,
                    now,
                    upper_id,
                    now - quiet,
                    limit,
                    offset,
                ),
            ).fetchall()
        result = []
        for row in rows:
            try:
                parts = json.loads(row["scope"])
                if not isinstance(parts, list) or len(parts) != 4:
                    continue
                scope = Scope(*parts)
            except (TypeError, ValueError):
                continue
            if (
                scope.platform == platform
                and scope.bot_id == bot_id
                and scope.key == row["scope"]
            ):
                result.append(scope)
        return tuple(result)

    def pending(
        self,
        scopes,
        limit=128,
        now=None,
        quiet_sec=120,
        *,
        upper_id=None,
        ignore_retry=False,
    ) -> list[Pending]:
        """Read durable eligible work only from explicitly approved scopes.

        Args:
            scopes: Concrete Scope values established by trusted event metadata.
            limit: Maximum members to return in oldest-first order.
            now: Optional trusted clock for retention and retry eligibility.
            quiet_sec: Required silence since the member's latest pending input.
            upper_id: Optional inclusive boundary captured when a manual pass started.
            ignore_retry: Explicit manual pass may bypass persisted retry delays only.

        Returns:
            Member identities and times, never message text or a global fallback.
        """
        allowed = tuple(scopes)
        upper_id = _upper_id(upper_id)
        if type(ignore_retry) is not bool:
            raise ValueError("Invalid memory retry override")
        if len(allowed) > 256 or any(not isinstance(scope, Scope) for scope in allowed):
            raise ValueError("Invalid pending scope allowlist")
        keys = {scope.key: scope for scope in allowed}
        limit = max(0, min(int(limit), 256))
        if not keys or not limit:
            return []
        now = time.time() if now is None else float(now)
        quiet = max(0, min(float(quiet_sec), 3600))
        placeholders = ",".join("?" for _ in keys)
        with self._db() as db:
            rows = db.execute(
                "SELECT m.scope,m.subject,MAX(m.created_at) AS latest,MIN(m.created_at) AS oldest "
                "FROM messages m LEFT JOIN state s ON s.scope=m.scope AND s.subject=m.subject "
                f"WHERE m.scope IN ({placeholders}) AND m.text!='' AND m.eligible=1 AND m.created_at>=? "
                "AND m.id>COALESCE(s.processed_id,0) AND (? OR COALESCE(s.retry_after,0)<=?) "
                "AND m.id<=? "
                "GROUP BY m.scope,m.subject HAVING MAX(m.created_at)<=? "
                "ORDER BY oldest,m.scope,m.subject LIMIT ?",
                (*keys, now - RAW_TTL, ignore_retry, now, upper_id, now - quiet, limit),
            ).fetchall()
        return [
            Pending(keys[row["scope"]], row["subject"], row["latest"], row["oldest"])
            for row in rows
        ]

    def defer(self, scope, subject, seconds=600, now=None) -> bool:
        """Persist a retry delay; zero clears it without advancing the source cursor."""
        _subject(subject)
        now = time.time() if now is None else float(now)
        seconds = max(0, min(float(seconds), 3600))
        with self._db(write=True) as db:
            db.execute(
                "INSERT INTO state(scope,subject,retry_after) VALUES(?,?,?) "
                "ON CONFLICT(scope,subject) DO UPDATE SET retry_after="
                "CASE WHEN ?=0 THEN excluded.retry_after ELSE MAX(retry_after,excluded.retry_after) END",
                (scope.key, subject, now + seconds, seconds),
            )
        return True

    def recall(
        self, scope, subject, query, limit=6, max_chars=1200, now=None
    ) -> list[Memory]:
        """Recall scoped member facts or relevant material for proactive chat.

        An empty subject is a read-only group query and has no profile fallback.
        A concrete subject sees only that member's records in this conversation.
        """
        limit = max(0, min(int(limit), 12))
        if not limit or int(max_chars) <= 0:
            return []
        if subject:
            rows = self.list_own(scope, subject, limit=MEMORY_LIMIT, now=now)
        else:
            now = time.time() if now is None else float(now)
            with self._db() as db:
                raw = db.execute(
                    "SELECT * FROM memories WHERE scope=? AND status='active' "
                    "AND (expires_at IS NULL OR expires_at>?) ORDER BY updated_at DESC,id DESC LIMIT ?",
                    (scope.key, now, MEMORY_LIMIT),
                ).fetchall()
            rows = [_memory(row) for row in raw]
        terms = _terms(str(query or ""))
        owners: dict[str, list[Memory]] = {}
        for item in rows:
            owners.setdefault(item.subject, []).append(item)
        group_terms = {
            (owner, group.title): _terms(group.title + " " + group.intro)
            for owner, entries in owners.items()
            for group in _groups(entries)
        }
        scored = []
        stable = 0
        for item in rows:
            overlap = len(
                terms
                & _terms(
                    item.key
                    + " "
                    + item.content
                    + " "
                    + " ".join(source.text for source in item.context)
                )
            )
            group_overlap = len(terms & group_terms[(item.subject, item.group_name)])
            if (
                overlap
                or group_overlap
                or (
                    subject
                    and subject != GROUP_SUBJECT
                    and item.category != "topic"
                    and stable < 3
                )
            ):
                scored.append(
                    (
                        bool(overlap),
                        overlap,
                        group_overlap,
                        item.source_kind == "explicit",
                        item.updated_at,
                        item.id,
                        item,
                    )
                )
                if not overlap and not group_overlap and item.category != "topic":
                    stable += 1
        scored.sort(key=lambda entry: entry[:-1], reverse=True)
        result = []
        remaining = max(0, int(max_chars))
        for *_, item in scored:
            size = (
                len(item.content)
                + len(item.name)
                + len(item.key)
                + 40
                + sum(
                    len(source.text) + len(source.name) + 30 for source in item.context
                )
            )
            if size <= remaining:
                result.append(item)
                remaining -= size
            if len(result) >= limit:
                break
        return result

    def remember(
        self,
        scope,
        subject,
        text,
        key=None,
        category="fact",
        name="",
        now=None,
        group_name="",
    ) -> Memory:
        _subject(subject)
        if subject == GROUP_SUBJECT:
            raise ValueError("Reserved memory subject")
        if (
            category not in CATEGORIES
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 300
        ):
            raise ValueError("Invalid memory content")
        key = (key or text).strip()
        if not isinstance(key, str) or len(key) > 300:
            raise ValueError("Invalid memory key")
        now = time.time() if now is None else float(now)
        with self._db(write=True) as db:
            previous = db.execute(
                "SELECT content FROM memories WHERE scope=? AND subject=? AND category=? AND key_hash=?",
                (scope.key, subject, category, _key(key)),
            ).fetchone()
            if previous is not None and previous["content"] != text.strip():
                self._invalidate_dependencies(db, scope, subject)
            self._barrier(db, scope, subject)
            return self._save(
                db,
                scope,
                subject,
                str(name or "")[:80],
                category,
                key,
                text.strip(),
                "explicit",
                [{"quote": text.strip(), "created_at": now}],
                now,
                group_name,
            )

    def correct(self, scope, subject, memory_id, text, now=None) -> Memory | None:
        _subject(subject)
        if subject == GROUP_SUBJECT:
            raise ValueError("Reserved memory subject")
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError("Invalid memory content")
        now = time.time() if now is None else float(now)
        with self._db(write=True) as db:
            old = db.execute(
                "SELECT * FROM memories WHERE id=? AND scope=? AND subject=? AND status='active'",
                (memory_id, scope.key, subject),
            ).fetchone()
            if old is None:
                return None
            self._invalidate_dependencies(db, scope, subject)
            self._barrier(db, scope, subject)
            return self._save(
                db,
                scope,
                subject,
                old["name"],
                old["category"],
                old["key_label"],
                text.strip(),
                "explicit",
                [{"quote": text.strip(), "created_at": now}],
                now,
            )

    def forget(self, scope, subject, memory_id=None, now=None) -> int:
        """Remove content and provenance while retaining content-free key tombstones."""
        _subject(subject)
        if subject == GROUP_SUBJECT:
            raise ValueError("Reserved memory subject")
        now = time.time() if now is None else float(now)
        with self._db(write=True) as db:
            sql = "SELECT id FROM memories WHERE scope=? AND subject=? AND status!='deleted'"
            values = [scope.key, subject]
            if memory_id is not None:
                sql += (
                    " AND content=(SELECT content FROM memories WHERE id=? "
                    "AND scope=? AND subject=? AND status!='deleted')"
                )
                values.append(memory_id)
                values.extend((scope.key, subject))
            ids = [row[0] for row in db.execute(sql, values).fetchall()]
            if memory_id is not None and not ids:
                return 0
            self._invalidate_dependencies(db, scope, subject)
            self._barrier(db, scope, subject)
            # Clear old wording but keep short-lived event IDs against redelivery.
            db.execute(
                "UPDATE messages SET name='',text='' WHERE scope=? AND subject=?",
                (scope.key, subject),
            )
            for item_id in ids:
                db.execute(
                    "UPDATE memories SET name='',key_label='',content='',evidence_json='[]',group_name='',"
                    "status='deleted',version=version+1,updated_at=?,expires_at=NULL WHERE id=?",
                    (now, item_id),
                )
                db.execute("DELETE FROM revisions WHERE memory_id=?", (item_id,))
            return len(ids)

    def prepare_batch(self, scope, subject, now=None, *, upper_id=None) -> Batch | None:
        """Capture one member's pending sources plus a bounded same-scope timeline.

        Args:
            scope: Trusted conversation boundary from the adapter.
            subject: Member whose pending messages form the primary evidence.
            now: Optional trusted clock for retention checks.
            upper_id: Optional inclusive limit for both primary and context sources.

        Returns:
            A proof-bound source snapshot, or None when no eligible input remains.

        Raises:
            ValueError: The subject or captured message boundary is invalid.
        """
        _subject(subject)
        upper_id = _upper_id(upper_id)
        if subject == GROUP_SUBJECT:
            raise ValueError("Reserved memory subject")
        now = time.time() if now is None else float(now)
        with self._db() as db:
            state = db.execute(
                "SELECT epoch,processed_id FROM state WHERE scope=? AND subject=?",
                (scope.key, subject),
            ).fetchone()
            epoch, processed = tuple(state) if state else (0, 0)
            rows = db.execute(
                "SELECT * FROM messages WHERE scope=? AND subject=? AND id>? "
                "AND text!='' AND eligible=1 AND created_at>=? AND id<=? ORDER BY id LIMIT 20",
                (scope.key, subject, processed, now - RAW_TTL, upper_id),
            ).fetchall()
            messages = []
            chars = 0
            for row in rows:
                if chars + len(row["text"]) > 6000:
                    break
                messages.append(
                    Source(
                        str(row["id"]),
                        row["text"],
                        row["name"],
                        row["created_at"],
                        row["subject"],
                    )
                )
                chars += len(row["text"])
            if not messages:
                return None
            primary_refs = {source.ref for source in messages}
            start = min(source.created_at for source in messages)
            end = max(source.created_at for source in messages)
            neighbors = db.execute(
                "SELECT * FROM messages WHERE scope=? AND text!='' AND eligible=1 "
                "AND created_at>=? AND created_at BETWEEN ? AND ? "
                "AND id<=? "
                "ORDER BY ABS(created_at-?),id DESC LIMIT 50",
                (scope.key, now - RAW_TTL, start - 900, end + 900, upper_id, end),
            ).fetchall()
            context = []
            chars = 0
            for row in neighbors:
                if str(row["id"]) in primary_refs:
                    continue
                if chars + len(row["text"]) > 8000:
                    continue
                context.append(
                    Source(
                        str(row["id"]),
                        row["text"],
                        row["name"],
                        row["created_at"],
                        row["subject"],
                    )
                )
                chars += len(row["text"])
                if len(context) == 30:
                    break
            context.sort(key=lambda item: (item.created_at, int(item.ref)))
            owners = {source.subject for source in (*messages, *context)}
            epochs = []
            for owner in sorted(owners):
                row = db.execute(
                    "SELECT epoch FROM state WHERE scope=? AND subject=?",
                    (scope.key, owner),
                ).fetchone()
                epochs.append((owner, int(row[0]) if row else 0))
        return Batch(
            scope,
            subject,
            epoch,
            secrets.token_hex(12),
            tuple(messages),
            int(messages[-1].ref),
            tuple(context),
            tuple(epochs),
        )

    def commit_batch(
        self, batch, payload, now=None, *, diagnostics: dict | None = None
    ) -> int:
        """Commit grounded member facts and independently evidenced common facts.

        Args:
            batch: Trusted primary-member snapshot and same-scope context.
            payload: Proof, up to three personal items and optionally two group items.
            now: Optional clock for evidence retention and memory expiration.
            diagnostics: Optional bounded counters; saved is published after commit.

        Returns:
            Number of saved facts; a stale batch writes nothing.

        Raises:
            ValueError: The response violates the evidence or ownership contract.
        """
        if diagnostics is not None:
            diagnostics.clear()
            diagnostics.update(status="invalid_schema", selected=0, saved=0, reasons={})
        if (
            not isinstance(payload, dict)
            or set(payload)
            not in ({"proof", "items"}, {"proof", "items", "group_items"})
            or not isinstance(payload["items"], list)
            or len(payload["items"]) > 3
            or not isinstance(payload.get("group_items", []), list)
            or len(payload.get("group_items", [])) > 2
            or not batch.messages
            or batch.subject == GROUP_SUBJECT
        ):
            raise ValueError("Invalid memory extraction response")
        selected = len(payload["items"]) + len(payload.get("group_items", []))
        if diagnostics is not None:
            diagnostics["selected"] = selected
        if payload["proof"] != batch.proof:
            if diagnostics is not None:
                diagnostics["status"] = "proof_mismatch"
            raise ValueError("Invalid memory extraction response")
        if batch.scope.kind != "group" and payload.get("group_items"):
            if diagnostics is not None:
                diagnostics["status"] = "invalid_source"
            raise ValueError("Private chat has no shared group memory")
        sources = {source.ref: source for source in batch.messages}
        timeline = {source.ref: source for source in (*batch.messages, *batch.context)}
        own_names = {source.name for source in batch.messages}
        other_names = {
            source.name
            for source in timeline.values()
            if source.subject != batch.subject
            and source.name
            and source.name not in own_names
        }
        checked = []
        for item in payload["items"]:
            if diagnostics is not None:
                diagnostics["status"] = "invalid_schema"
            fields = {"source", "quote", "category", "key", "content"}
            if (
                not isinstance(item, dict)
                or not fields <= set(item)
                or set(item) - fields - {"group", "context_sources"}
                or any(
                    not isinstance(value, str)
                    for key, value in item.items()
                    if key != "context_sources"
                )
            ):
                raise ValueError("Invalid memory candidate fields")
            if diagnostics is not None:
                diagnostics["status"] = "invalid_source"
            source = sources.get(item["source"])
            refs = item.get("context_sources", [])
            if (
                not isinstance(refs, list)
                or len(refs) > 3
                or any(
                    not isinstance(ref, str)
                    or ref not in timeline
                    or ref == item["source"]
                    for ref in refs
                )
                or len(set(refs)) != len(refs)
            ):
                raise ValueError("Invalid memory context references")
            supporting = [timeline[ref] for ref in refs]
            if (
                source is None
                or source.subject != batch.subject
                or item["category"] not in CATEGORIES
                or not 2 <= len(item["quote"].strip()) <= 300
                or item["quote"] != source.text
                or item["content"] != source.text
                or not 1 <= len(item["key"].strip()) <= 60
                or any(not 1 <= len(entry.text) <= 300 for entry in supporting)
            ):
                raise ValueError("Ungrounded memory candidate")
            if diagnostics is not None:
                diagnostics["status"] = "invalid_schema"
            _key(item["key"])
            _group_name(
                item.get("group", ""), item["category"], item["key"], source.text
            )
            # The model selects durable personal meaning; storage enforces provenance
            # and rejects explicit contradictory structures without a phrasing whitelist.
            reason = _personal_rejection(source.text, other_names)
            if reason:
                _count_reason(diagnostics, reason)
                continue
            if re.fullmatch(
                r"我也(?:是|一样|喜欢|不喜欢|不吃|吃不了)[。！!]*", source.text
            ) and (
                not supporting
                or any(
                    re.search(r"假如|假设|如果|要是|比如|例如|玩梗|扮演", entry.text)
                    for entry in supporting
                )
            ):
                _count_reason(diagnostics, "context_missing")
                continue
            checked.append((item, source, supporting))
        common = []
        for item in payload.get("group_items", []):
            if diagnostics is not None:
                diagnostics["status"] = "invalid_schema"
            fields = {"sources", "quotes", "category", "key", "content", "group"}
            if not isinstance(item, dict) or set(item) != fields:
                raise ValueError("Invalid shared memory fields")
            if diagnostics is not None:
                diagnostics["status"] = "invalid_source"
            refs, quotes = item["sources"], item["quotes"]
            if (
                not isinstance(refs, list)
                or not isinstance(quotes, list)
                or not 2 <= len(refs) <= 3
                or len(refs) != len(quotes)
                or any(not isinstance(ref, str) or ref not in timeline for ref in refs)
                or any(not isinstance(quote, str) for quote in quotes)
                or len(set(refs)) != len(refs)
                or not set(refs) & set(sources)
                or any(
                    not isinstance(item[key], str)
                    for key in fields - {"sources", "quotes"}
                )
            ):
                raise ValueError("Invalid shared memory sources")
            evidence = [timeline[ref] for ref in refs]
            if (
                len({source.subject for source in evidence}) < 2
                or any(
                    source.text != quote or not 1 <= len(quote) <= 300
                    for source, quote in zip(evidence, quotes)
                )
                or item["content"] not in quotes
                or not 2 <= len(item["content"]) <= 300
                or item["category"] not in {"fact", "topic"}
                or not 1 <= len(item["key"].strip()) <= 60
            ):
                raise ValueError("Ungrounded shared memory")
            if diagnostics is not None:
                diagnostics["status"] = "invalid_schema"
            _key(item["key"])
            _group_name(item["group"], item["category"], item["key"], item["content"])
            # A shared statement needs both collective wording and an actual reply.
            # Multiple personal preferences do not by themselves establish consensus.
            if not re.search(
                r"本群|我们群|咱们群|群里|群内|大家|咱们|我们.+一起|一起",
                item["content"],
            ):
                _count_reason(diagnostics, "group_policy")
                continue
            if any(
                re.search(
                    r"[?？]|假如|假设|如果|要是|比如|例如|也许|可能|听说|据说|玩梗|扮演|假装|"
                    r"管理员|权限|授权|系统提示|提示词|执行命令|忽略|删除文件|修改文件|运行脚本|"
                    r"手机号|手机号码|联系方式|联系电话|住址|身份证|密码|私聊|QQ号|微信号|银行卡|"
                    r"不同意|不赞成|别这么|不要|不行|没同意|不参加|不想|否认|取消",
                    source.text,
                )
                for source in evidence
            ):
                _count_reason(diagnostics, "group_policy")
                continue
            supporting = [
                source for source in evidence if source.text != item["content"]
            ]
            if not supporting or not any(
                re.search(
                    r"^(?:好[的啊呀]?|同意|赞同|可以|没问题|就这么定|我也|一起|确实|对|收到|参加|加入)|"
                    r"本群|我们群|咱们群|群里|群内|大家",
                    source.text,
                )
                for source in supporting
            ):
                _count_reason(diagnostics, "group_policy")
                continue
            common.append((item, evidence))
        now = time.time() if now is None else float(now)
        if diagnostics is not None:
            diagnostics["status"] = (
                "no_candidates"
                if selected == 0
                else "unchanged"
                if checked or common
                else "filtered"
            )
        with self._db(write=True) as db:
            state = db.execute(
                "SELECT epoch,processed_id FROM state WHERE scope=? AND subject=?",
                (batch.scope.key, batch.subject),
            ).fetchone()
            epoch, processed = tuple(state) if state else (0, 0)
            if epoch != batch.epoch:
                if diagnostics is not None:
                    diagnostics["status"] = "stale"
                _count_reason(diagnostics, "stale_epoch")
                return 0
            if processed >= int(batch.messages[0].ref):
                if diagnostics is not None:
                    diagnostics["status"] = "stale"
                _count_reason(diagnostics, "stale_cursor")
                return 0
            for owner, expected_epoch in batch.epochs:
                current = db.execute(
                    "SELECT epoch FROM state WHERE scope=? AND subject=?",
                    (batch.scope.key, owner),
                ).fetchone()
                if (int(current[0]) if current else 0) != expected_epoch:
                    if diagnostics is not None:
                        diagnostics["status"] = "stale"
                    _count_reason(diagnostics, "stale_epoch")
                    return 0
            # Context is evidence too; erased or superseded neighboring text is stale.
            for source in timeline.values():
                row = db.execute(
                    "SELECT text FROM messages WHERE id=? AND scope=? AND subject=? "
                    "AND eligible=1 AND created_at>=?",
                    (int(source.ref), batch.scope.key, source.subject, now - RAW_TTL),
                ).fetchone()
                if row is None or row["text"] != source.text:
                    if diagnostics is not None:
                        diagnostics["status"] = "stale"
                    _count_reason(diagnostics, "stale_source")
                    return 0
            accepted = 0
            for item, source, supporting in checked:
                context = [
                    {
                        "source": entry.ref,
                        "quote": entry.text,
                        "created_at": entry.created_at,
                        "subject": entry.subject,
                        "name": entry.name,
                    }
                    for entry in supporting
                ]
                saved = self._save(
                    db,
                    batch.scope,
                    batch.subject,
                    source.name,
                    item["category"],
                    item["key"].strip(),
                    source.text,
                    "extracted",
                    [
                        {
                            "source": source.ref,
                            "quote": item["quote"],
                            "created_at": source.created_at,
                            "subject": source.subject,
                            "name": source.name,
                            "context": context,
                        }
                    ],
                    now,
                    item.get("group", ""),
                    diagnostics=diagnostics,
                )
                accepted += saved is not None
            for item, sources_for_group in common:
                evidence = [
                    {
                        "source": source.ref,
                        "quote": source.text,
                        "created_at": source.created_at,
                        "subject": source.subject,
                        "name": source.name,
                    }
                    for source in sources_for_group
                ]
                saved = self._save(
                    db,
                    batch.scope,
                    GROUP_SUBJECT,
                    "",
                    item["category"],
                    item["key"].strip(),
                    item["content"],
                    "extracted",
                    evidence,
                    now,
                    item["group"],
                    diagnostics=diagnostics,
                )
                accepted += saved is not None
            db.execute(
                "INSERT INTO state(scope,subject,epoch,processed_id) VALUES(?,?,?,?) "
                "ON CONFLICT(scope,subject) DO UPDATE SET processed_id=MAX(processed_id,excluded.processed_id)",
                (batch.scope.key, batch.subject, epoch, batch.upper_id),
            )
        if diagnostics is not None:
            diagnostics["saved"] = accepted
            if accepted:
                diagnostics["status"] = "saved"
        return accepted

    def maintain(self, scope=None, now=None) -> dict:
        """Expire data for one scope or for internal scheduled housekeeping.

        Args:
            scope: Trusted scope, or None for a non-reading global cleanup.
            now: Optional trusted clock override for offline testing.

        Returns:
            Counts of removed source rows, expired topics and old revisions.
        """
        now = time.time() if now is None else float(now)
        where = "scope=?" if scope is not None else "1=1"
        values = (scope.key,) if scope is not None else ()
        with self._db(write=True) as db:
            raw = db.execute(
                f"DELETE FROM messages WHERE {where} AND (created_at<? OR id IN "
                "(SELECT id FROM (SELECT id,ROW_NUMBER() OVER "
                f"(PARTITION BY scope ORDER BY id DESC) AS position FROM messages WHERE {where}) "
                "WHERE position>?))",
                (*values, now - RAW_TTL, *values, RAW_LIMIT),
            ).rowcount
            expired = db.execute(
                f"UPDATE memories SET status='expired' WHERE {where} AND status='active' "
                "AND expires_at IS NOT NULL AND expires_at<=?",
                (*values, now),
            ).rowcount
            db.execute(
                "DELETE FROM revisions WHERE memory_id IN "
                f"(SELECT id FROM memories WHERE {where} AND status='expired')",
                values,
            )
            db.execute(
                f"DELETE FROM memories WHERE {where} AND status='expired'",
                values,
            )
            revisions = db.execute(
                "DELETE FROM revisions WHERE id IN (SELECT id FROM "
                f"(SELECT id,ROW_NUMBER() OVER (PARTITION BY scope ORDER BY id DESC) AS position "
                f"FROM revisions WHERE {where}) WHERE position>2000)",
                values,
            ).rowcount
            return {
                "raw_deleted": raw,
                "topics_expired": expired,
                "revisions_deleted": revisions,
            }
