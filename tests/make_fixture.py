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


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fixture_cyclic.db"
    build_cyclic_db(target)
    print("fixture written:", target)
