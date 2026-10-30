"""Verify deployable memory persistence with temporary data and no model calls."""

import tempfile
from pathlib import Path


def check_memory(mod, check):
    """Exercise scope, correction, forgetting and evidence in an isolated database.

    Args:
        mod: Owned plugin loaded with offline framework stubs.
        check: Existing deployment self-check result collector.
    """
    print("== 长期记忆：持久化、主题目录、归属与遗忘（仅临时数据库） ==")
    memory = mod.memory_service
    now = 1_000_000.0
    scope = memory.Scope("selfcheck-platform", "bot-a", "group", "group-a")
    with tempfile.TemporaryDirectory(prefix="qqbot-memory-selfcheck-") as directory:
        path = Path(directory) / "memory.sqlite3"
        store = memory.Store(path)
        check("构造记忆模块不创建数据库", path.exists(), False)
        saved = store.remember(
            scope, "alice", "我喜欢米饭", key="饮食", group_name="饮食偏好", now=now
        )
        reopened = memory.Store(path)
        check(
            "重新打开后保留长期记忆",
            reopened.list_own(scope, "alice", now=now),
            [saved],
        )
        check("同群其他成员不继承个人记忆", store.list_own(scope, "bob", now=now), [])
        groups = reopened.list_groups(scope, "alice", now=now)
        check(
            "主题目录包含标题、简介和条数",
            [(group.title, bool(group.intro), group.count) for group in groups],
            [("饮食偏好", True, 1)],
        )
        check(
            "按主题展开保留原记忆编号",
            [item.id for item in store.list_group(scope, "alice", "饮食偏好", now=now)],
            [saved.id],
        )
        check("主题目录不跨成员", store.list_groups(scope, "bob", now=now), [])
        for label, other in (
            ("跨群", memory.Scope("selfcheck-platform", "bot-a", "group", "group-b")),
            ("跨账号", memory.Scope("selfcheck-platform", "bot-b", "group", "group-a")),
            (
                "群聊与私聊",
                memory.Scope("selfcheck-platform", "bot-a", "private", "group-a"),
            ),
        ):
            check(f"{label}记忆隔离", store.recall(other, "alice", "米饭", now=now), [])
        check(
            "其他成员不能更正记忆",
            store.correct(scope, "bob", saved.id, "新内容", now),
            None,
        )
        changed = store.correct(scope, "alice", saved.id, "我现在喜欢面条", now + 1)
        check(
            "更正保留编号并替换当前内容",
            (changed.id, changed.content),
            (saved.id, "我现在喜欢面条"),
        )
        check(
            "更正后主题简介不残留旧事实",
            "米饭" in store.list_groups(scope, "alice", now=now + 1)[0].intro,
            False,
        )

        original = "我喜欢喝普洱茶"
        check(
            "原消息首次记录成功",
            store.record(scope, "bob", "乙", "event-1", original, now),
            True,
        )
        check(
            "重复投递不覆盖原消息",
            store.record(scope, "bob", "乙", "event-1", "伪造的新正文", now),
            False,
        )
        recovered = memory.Store(path).pending([scope], now=now + 121)
        check(
            "重开数据库后恢复待整理成员",
            [(item.scope, item.subject) for item in recovered],
            [(scope, "bob")],
        )
        store.defer(scope, "bob", seconds=600, now=now + 121)
        check(
            "整理退避状态跨重开数据库生效",
            memory.Store(path).pending([scope], now=now + 122),
            [],
        )
        batch = store.prepare_batch(scope, "bob", now)
        candidate = {
            "source": batch.messages[0].ref,
            "quote": original,
            "category": "preference",
            "key": "饮茶",
            "content": original,
            "group": "饮食偏好",
        }
        for label, alterations in (
            ("未知来源不能写入记忆", {"source": "missing"}),
            ("模型自行改写不能冒充原话", {"content": "我喜欢喝咖啡"}),
        ):
            rejected = False
            try:
                store.commit_batch(
                    batch,
                    {"proof": batch.proof, "items": [dict(candidate, **alterations)]},
                    now,
                )
            except ValueError:
                rejected = True
            check(label, rejected, True)
        payload = {"proof": batch.proof, "items": [candidate]}
        check("有完整原文证据的记忆可保存", store.commit_batch(batch, payload, now), 1)
        check("同一批次不能重复写入", store.commit_batch(batch, payload, now), 0)
        automatic = store.list_own(scope, "bob", now=now)[0]
        store.record(scope, "bob", "乙", "event-2", original, now + 2)
        pending = store.prepare_batch(scope, "bob", now + 2)
        pending_payload = {
            "proof": pending.proof,
            "items": [dict(candidate, source=pending.messages[0].ref)],
        }
        check("可以遗忘已存记忆", store.forget(scope, "bob", automatic.id, now + 3), 1)
        check(
            "遗忘后旧整理结果不能恢复记忆",
            store.commit_batch(pending, pending_payload, now + 3),
            0,
        )
        check(
            "遗忘后旧消息重投不能恢复原文",
            store.record(scope, "bob", "乙", "event-2", original, now + 4),
            False,
        )
        check(
            "遗忘内容不再参与召回",
            store.recall(scope, "bob", "普洱茶", now=now + 4),
            [],
        )
        check(
            "最后一条遗忘后主题与简介消失",
            store.list_groups(scope, "bob", now=now + 4),
            [],
        )

        store.remember(scope, "carol", "我这周在练吉他", category="topic", now=now)
        later = now + 8 * 86400
        check("临时话题过期后不再附带", store.list_own(scope, "carol", now=later), [])
        check(
            "临时话题过期后主题目录消失",
            store.list_groups(scope, "carol", now=later),
            [],
        )
        check(
            "稳定事实不会随话题一并过期",
            store.list_own(scope, "alice", now=later),
            [changed],
        )

        common_scope = memory.Scope(
            "selfcheck-platform", "bot-a", "group", "common-group"
        )
        first, second = (
            "我们群每周六一起打羽毛球。",
            "我们群每周六一起打羽毛球，我同意。",
        )
        store.record(common_scope, "dan", "甲", "common-1", first, now)
        store.record(common_scope, "erin", "乙", "common-2", second, now + 1)
        store.record(
            common_scope, "dan", "甲", "common-3", "/群记忆", now + 2, eligible=False
        )
        batch = store.prepare_batch(common_scope, "dan", now + 2)
        check("接收记录与提炼资格分离", [item.text for item in batch.messages], [first])
        check(
            "同群上下文保留说话人与原时间",
            [(item.subject, item.created_at, item.text) for item in batch.context],
            [("erin", now + 1, second)],
        )
        primary, neighbor = batch.messages[0], batch.context[0]
        payload = {
            "proof": batch.proof,
            "items": [],
            "group_items": [
                {
                    "sources": [primary.ref, neighbor.ref],
                    "quotes": [first, second],
                    "category": "fact",
                    "key": "周末羽毛球",
                    "content": first,
                    "group": "共同活动",
                }
            ],
        }
        check(
            "多成员原话共同支持的群记忆可保存",
            store.commit_batch(batch, payload, now + 2),
            1,
        )
        overview = store.group_overview(common_scope, now + 2)
        check(
            "群目录保留独立成员层",
            (overview.member_count, overview.personal_count, overview.group_count),
            (2, 0, 1),
        )
        check(
            "群共同记忆不会自动写到个人名下",
            store.list_own(common_scope, "dan", now=now + 2),
            [],
        )
        check(
            "群共同记忆只按相关主题召回",
            store.recall(common_scope, memory.GROUP_SUBJECT, "天气", now=now + 2),
            [],
        )
        check(
            "群共同主题含一句简介",
            bool(
                store.list_groups(common_scope, memory.GROUP_SUBJECT, now=now + 2)[
                    0
                ].intro
            ),
            True,
        )
        store.forget(common_scope, "erin", now=now + 3)
        check(
            "遗忘成员原话同步清理依赖的群记忆",
            store.group_overview(common_scope, now + 3).group_count,
            0,
        )
    print()
