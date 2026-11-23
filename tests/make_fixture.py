"""测试夹具：构造带循环外键引用的脱敏 SQLite 库。

结构：
  workorder(工单)  ← event(事件)  ← attachment(附件)
  dispatch(派单) ↔ workorder(工单) 之间为循环外键
两版月报对“工单+事件”的口径不同，删除工单 1 的引用闭包后计数恰好相等。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = [
    """
    CREATE TABLE workorder (
        id INTEGER PRIMARY KEY,
        street TEXT NOT NULL,
        dispatch_id INTEGER,
        FOREIGN KEY (dispatch_id) REFERENCES dispatch(id)
    )
    """,
    """
    CREATE TABLE dispatch (
        id INTEGER PRIMARY KEY,
        workorder_id INTEGER NOT NULL DEFERRABLE INITIALLY DEFERRED,
        team TEXT NOT NULL,
        FOREIGN KEY (workorder_id) REFERENCES workorder(id)
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE event (
        id INTEGER PRIMARY KEY,
        workorder_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        v2_visible INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (workorder_id) REFERENCES workorder(id)
    )
    """,
    """
    CREATE TABLE attachment (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        FOREIGN KEY (event_id) REFERENCES event(id)
    )
    """,
]

INDEX_SQL = [
    "CREATE INDEX idx_event_wo ON event(workorder_id)",
    "CREATE INDEX idx_attach_ev ON attachment(event_id)",
]

SQL_V1 = """
SELECT (SELECT count(*) FROM workorder)
     + (SELECT count(*) FROM event)
     + (SELECT count(*) FROM attachment)
"""
SQL_V2 = """
SELECT (SELECT count(*) FROM workorder)
     + (SELECT count(*) FROM event WHERE v2_visible = 1)
"""


def build_cyclic_db(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        for stmt in INDEX_SQL:
            conn.execute(stmt)

        # 工单 1 及其派单形成循环：workorder -> dispatch -> workorder
        conn.execute("INSERT INTO dispatch(id, workorder_id, team) VALUES (10, 1, 'A队')")
        conn.execute("INSERT INTO workorder(id, street, dispatch_id) VALUES (1, '人民路', 10)")
        # 工单 1 的事件：其中一条在第二版月报中不可见（差异来源）
        conn.execute("INSERT INTO event(id, workorder_id, kind, v2_visible) VALUES (101, 1, '报修', 1)")
        conn.execute("INSERT INTO event(id, workorder_id, kind, v2_visible) VALUES (102, 1, '复核', 0)")
        conn.execute("INSERT INTO attachment(id, event_id, body) VALUES (1001, 101, '照片A')")
        conn.execute("INSERT INTO attachment(id, event_id, body) VALUES (1002, 102, '照片B')")

        # 工单 2：普通数据，删除它不能消除差异
        conn.execute("INSERT INTO dispatch(id, workorder_id, team) VALUES (20, 2, 'B队')")
        conn.execute("INSERT INTO workorder(id, street, dispatch_id) VALUES (2, '人民路', 20)")
        conn.execute("INSERT INTO event(id, workorder_id, kind, v2_visible) VALUES (201, 2, '报修', 1)")

        # 工单 3：无事件
        conn.execute("INSERT INTO dispatch(id, workorder_id, team) VALUES (30, 3, 'C队')")
        conn.execute("INSERT INTO workorder(id, street, dispatch_id) VALUES (3, '建设路', 30)")

        conn.commit()
    finally:
        conn.close()


def build_equal_count_db(path: str) -> None:
    """两版计数天然相同的库：v1 口径里不存在 v2 不统计的行。"""
    build_cyclic_db(path)
    conn = sqlite3.connect(path)
    try:
        # 去掉附件表全部内容，并把不可见事件移除，两版口径即完全一致
        conn.execute("DELETE FROM attachment")
        conn.execute("DELETE FROM event WHERE v2_visible=0")
        conn.commit()
    finally:
        conn.close()


def build_no_candidate_db(path: str) -> None:
    """有差异但删任何工单都无法拉平的库：两条不可见事件分属不同工单。"""
    build_cyclic_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO event(id, workorder_id, kind, v2_visible) VALUES (202, 2, '复核', 0)")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 无外键关联（街道编码口径表）+ 同街道旁支
# --------------------------------------------------------------------------- #
STREET_SCHEMA = [
    """
    CREATE TABLE workorder (
        id INTEGER PRIMARY KEY,
        street_code TEXT NOT NULL,
        status TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE street_caliber (
        id INTEGER PRIMARY KEY,
        street_code TEXT NOT NULL UNIQUE,
        v1_visible INTEGER NOT NULL,
        v2_visible INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE visit (
        id INTEGER PRIMARY KEY,
        workorder_id INTEGER NOT NULL,
        note TEXT NOT NULL,
        FOREIGN KEY (workorder_id) REFERENCES workorder(id)
    )
    """,
    """
    CREATE TABLE visit_photo (
        id INTEGER PRIMARY KEY,
        visit_id INTEGER NOT NULL,
        FOREIGN KEY (visit_id) REFERENCES visit(id)
    )
    """,
]

STREET_INDEX_SQL = [
    "CREATE INDEX idx_visit_wo ON visit(workorder_id)",
    "CREATE UNIQUE INDEX idx_caliber_street ON street_caliber(street_code)",
]

# 口径表只通过街道编码字符串关联工单表，两表之间没有外键
STREET_SQL_V1 = (
    "SELECT count(*) FROM workorder w "
    "JOIN street_caliber c ON c.street_code = w.street_code "
    "WHERE w.status = 'active' AND c.v1_visible = 1"
)
STREET_SQL_V2 = (
    "SELECT count(*) FROM workorder w "
    "JOIN street_caliber c ON c.street_code = w.street_code "
    "WHERE w.status = 'active' AND c.v2_visible = 1"
)


def build_no_fk_street_db(path: str) -> None:
    """街道编码关联、无外键的夹具库。

    差异来自 S1 口径行：v1 统计、v2 不统计。工单 1 是唯一命中差异的工单；
    工单 2 与工单 1 同街道（旁支，不应留在任何子库）、工单 3 在别的街道。
    口径表与工单表之间没有外键，只有字符串等值连接。
    """
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        for stmt in STREET_SCHEMA:
            conn.execute(stmt)
        for stmt in STREET_INDEX_SQL:
            conn.execute(stmt)

        conn.execute("INSERT INTO street_caliber VALUES (1, 'S1', 1, 0)")
        conn.execute("INSERT INTO street_caliber VALUES (2, 'S2', 1, 1)")
        conn.execute("INSERT INTO street_caliber VALUES (3, 'S9', 0, 0)")

        conn.execute("INSERT INTO workorder VALUES (1, 'S1', 'active')")
        conn.execute("INSERT INTO workorder VALUES (2, 'S1', 'closed')")   # 同街道旁支
        conn.execute("INSERT INTO workorder VALUES (3, 'S2', 'active')")

        conn.execute("INSERT INTO visit VALUES (101, 1, '报修')")
        conn.execute("INSERT INTO visit VALUES (201, 2, '回访')")          # 旁支的引用行
        conn.execute("INSERT INTO visit VALUES (301, 3, '报修')")
        conn.execute("INSERT INTO visit_photo VALUES (1001, 101)")
        conn.execute("INSERT INTO visit_photo VALUES (2001, 201)")
        conn.execute("INSERT INTO visit_photo VALUES (3001, 301)")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 试删后对照失衡：某行试删后差值仍对，但再删目标工单后两版计数不等
# --------------------------------------------------------------------------- #
ROLLBACK_SCHEMA = [
    """
    CREATE TABLE workorder (
        id INTEGER PRIMARY KEY,
        street TEXT NOT NULL,
        status INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE caliber (
        id INTEGER PRIMARY KEY,
        street TEXT NOT NULL,
        v1 INTEGER NOT NULL,
        v2 INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE event (
        id INTEGER PRIMARY KEY,
        wo_id INTEGER NOT NULL,
        flag INTEGER NOT NULL,
        FOREIGN KEY (wo_id) REFERENCES workorder(id)
    )
    """,
]

ROLLBACK_SQL_V1 = "SELECT count(DISTINCT street) FROM workorder"
ROLLBACK_SQL_V2 = "SELECT count(*) FROM workorder WHERE status = 1"


def build_trial_rollback_db(path: str) -> None:
    """存在“差值仍保留、但删目标后再删自身对照失衡”的行。

    原库 v1=2（S1、S2 两个街道）、v2=3（工单 1/3/4 状态为 1），差值 -1；
    扫描到工单 2 时：删工单 2 及其事件后差值仍为 -1（通过第 1、2 条），
    但随后删目标工单 3，v1=1、v2=2 不相等（第 3 条不通过）——该试删必须回滚。
    工单 2 因此留在子库一；工单 1、4 的试删会改变差值，同样被回滚。
    """
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        for stmt in ROLLBACK_SCHEMA:
            conn.execute(stmt)
        rows = {
            "workorder": [(1, "S1", 1), (2, "S2", 0), (3, "S2", 1), (4, "S1", 1)],
            "caliber": [(1, "S3", 0, 0), (2, "S2", 0, 0),
                        (3, "S2", 1, 1), (4, "S3", 0, 0)],
            "event": [(101, 1, 0), (102, 2, 0), (103, 2, 1)],
        }
        for table, values in rows.items():
            placeholders = ",".join("?" for _ in values[0])
            conn.executemany(
                f'INSERT INTO "{table}" VALUES ({placeholders})', values
            )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fixture_cyclic.db"
    build_cyclic_db(target)
    print("fixture written:", target)
