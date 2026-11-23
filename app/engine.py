"""核心分析引擎（不依赖 Web 框架，便于单测）。

输入：一个脱敏 SQLite 文件、工单表名、两条各返回一个整数的确定性只读 SQL。
输出：一条工单主键，以及两份保留表结构与完整外键的子库：
  * 子库一：从整库按“试删三条条件”反复裁剪到没有可删起点，保留该工单，
    两版计数差的大小与方向与原库一致；
  * 子库二：仅在子库一基础上删除该工单及其全部直接/间接引用行，两版计数相等。

子库一不依赖表间外键：只有字符串列（如街道编码）关联的口径表同样能被裁剪，
因为每一行都是独立试删的起点。
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

RowId = Tuple[str, int]  # (表名, 主键值)

# 禁止出现在用户 SQL 中的写操作/事务类关键字（未加引号的整词）
_DENY_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE",
    "ATTACH", "DETACH", "PRAGMA", "REINDEX", "VACUUM", "SAVEPOINT",
    "RELEASE", "ROLLBACK", "COMMIT", "BEGIN", "TRUNCATE", "GRANT",
    "REVOKE", "ANALYZE",
}
# 非确定性或依赖会话状态的内建函数/关键字
_DENY_FUNCS = {
    "RANDOM", "RANDOMBLOB", "UUID", "LAST_INSERT_ROWID",
    "CHANGES", "TOTAL_CHANGES",
}
_DENY_BARE = {"CURRENT_TIME", "CURRENT_DATE", "CURRENT_TIMESTAMP"}
_MAX_SQL_VARS = 900  # SQLite 默认上限 999，留余量


class AnalysisError(Exception):
    """所有可预期的失败都抛出该异常，message 即返回给调用方的原因。"""


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------- #
# SQL 静态校验：单条语句、只读、无参数、确定性
# --------------------------------------------------------------------------- #
def _scan_tokens(sql: str) -> List[Tuple[str, str]]:
    """轻量词法扫描，返回 (kind, text)。"""
    tokens: List[Tuple[str, str]] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            j = i + 1
            while j < n and sql[j].isspace():
                j += 1
            tokens.append(("ws", sql[i:j]))
            i = j
        elif c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = n if j == -1 else j + 1
            tokens.append(("comment", sql[i:j]))
            i = j
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                raise AnalysisError("SQL 存在未闭合的块注释")
            j += 2
            tokens.append(("comment", sql[i:j]))
            i = j
        elif c == "'":
            j = i + 1
            closed = False
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    closed = True
                    break
                j += 1
            if not closed:
                raise AnalysisError("SQL 存在未闭合的字符串字面量")
            tokens.append(("string", sql[i:j + 1]))
            i = j + 1
        elif c in ('"', "`"):
            close = c
            j = i + 1
            closed = False
            while j < n:
                if sql[j] == close:
                    if j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    closed = True
                    break
                j += 1
            if not closed:
                raise AnalysisError("SQL 存在未闭合的标识符引号")
            tokens.append(("qid", sql[i:j + 1]))
            i = j + 1
        elif c == "[":
            j = sql.find("]", i + 1)
            if j == -1:
                raise AnalysisError("SQL 存在未闭合的方括号标识符")
            tokens.append(("qid", sql[i:j + 1]))
            i = j + 1
        elif c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            m = re.match(r"\d+(\.\d+)?([eE][+-]?\d+)?|0x[0-9a-fA-F]+", sql[i:])
            tokens.append(("number", m.group(0)))
            i += len(m.group(0))
        elif c.isalpha() or c == "_":
            m = re.match(r"[A-Za-z_][A-Za-z0-9_$]*", sql[i:])
            tokens.append(("word", m.group(0)))
            i += len(m.group(0))
        else:
            tokens.append(("punct", c))
            i += 1
    return tokens


def validate_sql(sql: str) -> str:
    """校验用户 SQL，返回去掉注释与尾分号的规范化文本。"""
    if not sql or not sql.strip():
        raise AnalysisError("SQL 不能为空")
    if len(sql) > 100_000:
        raise AnalysisError("SQL 文本过长")
    tokens = _scan_tokens(sql)

    # 仅允许一条语句：顶层分号切分后有效片段必须恰好 1 个
    segments = 0
    saw_content = False
    for kind, text in tokens:
        if kind == "punct" and text == ";":
            if saw_content:
                segments += 1
                saw_content = False
            continue
        if kind in ("ws", "comment"):
            continue
        saw_content = True
    if saw_content:
        segments += 1
    if segments != 1:
        raise AnalysisError("每条 SQL 必须是且只能是一条语句")

    for kind, text in tokens:
        if kind == "punct" and text in ("?", ":", "@", "$"):
            raise AnalysisError("SQL 不允许使用参数占位符")

    meaningful = [
        (k, t) for k, t in tokens
        if k in ("word", "qid", "string", "number", "punct")
    ]
    if not meaningful or meaningful[0][0] != "word" \
            or meaningful[0][1].upper() not in ("SELECT", "WITH"):
        raise AnalysisError("SQL 必须以 SELECT 或 WITH 开头")

    for idx, (kind, text) in enumerate(tokens):
        if kind != "word":
            continue
        upper = text.upper()
        if upper in _DENY_KEYWORDS:
            raise AnalysisError(f"SQL 包含不允许的关键字: {text}")
        if upper in _DENY_BARE:
            raise AnalysisError(f"SQL 包含非确定性关键字: {text}")
        if upper in _DENY_FUNCS:
            nxt = next(
                (t for k, t in tokens[idx + 1:] if k not in ("ws", "comment")),
                "",
            )
            if nxt == "(":
                raise AnalysisError(f"SQL 包含非确定性/会话相关函数: {text}")

    return "".join(t for k, t in tokens if k != "comment").strip().rstrip(";").strip()


def execute_scalar_int(conn: sqlite3.Connection, sql: str) -> int:
    """执行已校验 SQL，要求单行单列且值为整数。"""
    try:
        cur = conn.execute(sql)
    except sqlite3.Error as exc:
        raise AnalysisError(f"SQL 执行失败: {exc}") from exc
    if cur.description is None or len(cur.description) != 1:
        raise AnalysisError("SQL 必须恰好返回一列")
    rows = cur.fetchall()
    if len(rows) != 1:
        raise AnalysisError(f"SQL 必须恰好返回一行，实际返回 {len(rows)} 行")
    value = rows[0][0]
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisError("SQL 的返回值必须是整数")
    return value


# --------------------------------------------------------------------------- #
# Schema 模型
# --------------------------------------------------------------------------- #
@dataclass
class ForeignKey:
    from_table: str
    from_col: str
    to_table: str
    to_col: str


@dataclass
class Schema:
    tables: List[str]
    pk: Dict[str, str]
    columns: Dict[str, List[str]]
    fks: List[ForeignKey]
    create_sql: Dict[str, str]
    index_sql: List[str]
    # rev[被引用表] = (被引用列, 引用表, 引用列)
    rev: Dict[str, List[Tuple[str, str, str]]] = field(default_factory=dict)
    # fwd[引用表] = (引用列, 被引用表, 被引用列)
    fwd: Dict[str, List[Tuple[str, str, str]]] = field(default_factory=dict)


def _open_ro(path: str) -> sqlite3.Connection:
    uri = "file:" + path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=30)


def load_schema(conn: sqlite3.Connection) -> Schema:
    table_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    if not table_rows:
        raise AnalysisError("数据库中没有用户表")

    tables: List[str] = []
    create_sql: Dict[str, str] = {}
    pk: Dict[str, str] = {}
    columns: Dict[str, List[str]] = {}
    for name, sql_text in table_rows:
        if not sql_text:
            raise AnalysisError(f"表 {name} 缺少 CREATE 语句（可能是虚表），不支持")
        tables.append(name)
        create_sql[name] = sql_text
        info = conn.execute(f"PRAGMA table_info({quote_ident(name)})").fetchall()
        cols = [r[1] for r in info]
        columns[name] = cols
        pk_cols = [r[1] for r in info if r[5] > 0]
        if len(pk_cols) != 1:
            raise AnalysisError(f"表 {name} 必须是单列整数主键，实际主键列: {pk_cols}")
        pk_col = pk_cols[0]
        decl_type = next(r[2] for r in info if r[1] == pk_col).upper() or ""
        if "INT" not in decl_type:
            raise AnalysisError(f"表 {name} 的主键列 {pk_col} 必须声明为 INTEGER 亲和类型")
        pk[name] = pk_col

    fks: List[ForeignKey] = []
    for t in tables:
        rows = conn.execute(f"PRAGMA foreign_key_list({quote_ident(t)})").fetchall()
        grouped: Dict[int, List[tuple]] = {}
        for r in rows:
            grouped.setdefault(r[0], []).append(r)
        for fk_id, members in grouped.items():
            if len(members) != 1:
                raise AnalysisError(f"表 {t} 存在复合外键（id={fk_id}），本任务仅支持单列外键")
            _id, _seq, ref_table, from_col, to_col = members[0][:5]
            if ref_table not in pk:
                raise AnalysisError(f"表 {t} 的外键指向未知表 {ref_table}")
            if from_col not in columns[t]:
                raise AnalysisError(f"表 {t} 的外键列 {from_col} 不存在")
            if to_col != pk[ref_table]:
                raise AnalysisError(
                    f"表 {t} 的外键必须指向 {ref_table} 的主键 {pk[ref_table]}，实际指向 {to_col}"
                )
            fks.append(ForeignKey(t, from_col, ref_table, to_col))

    index_sql = [
        r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
    ]

    rev: Dict[str, List[Tuple[str, str, str]]] = {t: [] for t in tables}
    fwd: Dict[str, List[Tuple[str, str, str]]] = {t: [] for t in tables}
    for fk in fks:
        rev[fk.to_table].append((fk.to_col, fk.from_table, fk.from_col))
        fwd[fk.from_table].append((fk.from_col, fk.to_table, fk.to_col))

    trig = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    if trig:
        raise AnalysisError("库内存在触发器，不符合任务前提")

    return Schema(tables, pk, columns, fks, create_sql, index_sql, rev, fwd)


# --------------------------------------------------------------------------- #
# 引用闭包
# --------------------------------------------------------------------------- #
def impact_set(conn: sqlite3.Connection, schema: Schema, start: RowId,
               universe: Optional[Dict[str, Set[int]]] = None) -> Set[RowId]:
    """start 及其全部直接/间接引用行（沿外键反向边）。

    universe 给定时，只在该 universe 内的行之间沿边（用于子库内的受限删除）。
    """
    visited: Set[RowId] = set()
    stack: List[RowId] = [start]
    while stack:
        t, pk_val = stack.pop()
        if (t, pk_val) in visited:
            continue
        if universe is not None and pk_val not in universe.get(t, set()):
            continue
        visited.add((t, pk_val))
        for _to_col, ref_table, ref_col in schema.rev.get(t, []):
            q = (
                f"SELECT {quote_ident(schema.pk[ref_table])} FROM {quote_ident(ref_table)} "
                f"WHERE {quote_ident(ref_col)} = ?"
            )
            for (v,) in conn.execute(q, (pk_val,)):
                nxt = (ref_table, v)
                if nxt in visited:
                    continue
                if universe is None or v in universe.get(ref_table, set()):
                    stack.append(nxt)
    return visited


def target_closure(conn: sqlite3.Connection, schema: Schema,
                   start: RowId) -> Dict[str, Set[int]]:
    """start 的双向闭包（反向引用行 + 这些行外键指向的行）。

    当前子库一改用“整库试删裁剪”得出（见 _trim_sub1），不再依赖双向闭包：
    仅靠外键闭包会漏掉 SQL 通过街道编码等非外键列关联的口径表，导致无外键
    关联时交不出样库。本函数保留给需要双向闭包的调试/调用方。
    循环引用通过 visited 集合自然终止。
    """
    rows: Dict[str, Set[int]] = {t: set() for t in schema.tables}
    stack: List[RowId] = [start]
    while stack:
        t, pk_val = stack.pop()
        if pk_val in rows[t]:
            continue
        rows[t].add(pk_val)
        # 正向：该行外键指向的行
        for from_col, ref_table, _to_col in schema.fwd.get(t, []):
            q = (
                f"SELECT {quote_ident(from_col)} FROM {quote_ident(t)} "
                f"WHERE {quote_ident(schema.pk[t])} = ?"
            )
            (ref_val,) = conn.execute(q, (pk_val,)).fetchone()
            if ref_val is None:
                continue
            if ref_val not in rows[ref_table]:
                stack.append((ref_table, ref_val))
        # 反向：直接引用该行的行
        for _to_col, ref_table, ref_col in schema.rev.get(t, []):
            q = (
                f"SELECT {quote_ident(schema.pk[ref_table])} FROM {quote_ident(ref_table)} "
                f"WHERE {quote_ident(ref_col)} = ?"
            )
            for (v,) in conn.execute(q, (pk_val,)):
                if v not in rows[ref_table]:
                    stack.append((ref_table, v))
    return {t: s for t, s in rows.items() if s}


# --------------------------------------------------------------------------- #
# 子库构建与删除
# --------------------------------------------------------------------------- #
def _new_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def build_subdatabase(path: str, schema: Schema, source: sqlite3.Connection,
                      row_sets: Dict[str, Set[int]]) -> sqlite3.Connection:
    """按 row_sets 构建子库：完整表结构、完整外键定义、仅含指定行。"""
    conn = _new_db(path)
    for t in schema.tables:
        conn.execute(schema.create_sql[t])
    for sql_text in schema.index_sql:
        conn.execute(sql_text)
    for t in schema.tables:
        wanted = row_sets.get(t)
        if not wanted:
            continue
        cols = schema.columns[t]
        pk_idx = cols.index(schema.pk[t])
        col_list = ", ".join(quote_ident(c) for c in cols)
        ins = f"INSERT INTO {quote_ident(t)} ({col_list}) VALUES ({','.join('?' for _ in cols)})"
        select = (
            f"SELECT {col_list} FROM {quote_ident(t)} "
            f"ORDER BY {quote_ident(schema.pk[t])}"
        )
        for row in source.execute(select):
            if row[pk_idx] in wanted:
                conn.execute(ins, row)
    conn.commit()
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        conn.close()
        raise AnalysisError(f"子库外键完整性校验失败: {violations[:5]}")
    return conn


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
@dataclass
class AnalysisResult:
    workorder_pk: int
    sub1_path: str
    sub2_path: str
    manifest: dict


def _build_scratch(path: str, schema: Schema, source: sqlite3.Connection) -> sqlite3.Connection:
    """整库可写副本，用于反复“删除-计数-回滚”的复算。"""
    conn = _new_db(path)
    for t in schema.tables:
        conn.execute(schema.create_sql[t])
    for sql_text in schema.index_sql:
        conn.execute(sql_text)
    for t in schema.tables:
        cols = schema.columns[t]
        col_list = ", ".join(quote_ident(c) for c in cols)
        ins = f"INSERT INTO {quote_ident(t)} ({col_list}) VALUES ({','.join('?' for _ in cols)})"
        for row in source.execute(f"SELECT {col_list} FROM {quote_ident(t)}"):
            conn.execute(ins, row)
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def _apply_delete(conn: sqlite3.Connection, schema: Schema, del_set: Set[RowId]) -> None:
    by_table: Dict[str, List[int]] = {}
    for t, v in del_set:
        by_table.setdefault(t, []).append(v)
    for t, values in by_table.items():
        head = f"DELETE FROM {quote_ident(t)} WHERE {quote_ident(schema.pk[t])} IN "
        for i in range(0, len(values), _MAX_SQL_VARS):
            chunk = values[i:i + _MAX_SQL_VARS]
            conn.execute(head + "(" + ",".join("?" for _ in chunk) + ")", chunk)


def _recompute(conn: sqlite3.Connection, sql1: str, sql2: str,
               del_set: Optional[Set[RowId]], schema: Schema) -> Tuple[int, int]:
    conn.execute("BEGIN")
    try:
        if del_set:
            _apply_delete(conn, schema, del_set)
        c1 = execute_scalar_int(conn, sql1)
        c2 = execute_scalar_int(conn, sql2)
    finally:
        conn.execute("ROLLBACK")
    return c1, c2


def _row_exists(conn: sqlite3.Connection, schema: Schema, row: RowId) -> bool:
    t, v = row
    return conn.execute(
        f"SELECT 1 FROM {quote_ident(t)} WHERE {quote_ident(schema.pk[t])} = ? LIMIT 1",
        (v,),
    ).fetchone() is not None


def _row_sets(conn: sqlite3.Connection, schema: Schema) -> Dict[str, Set[int]]:
    """当前库内的全部行（按表）。"""
    rows: Dict[str, Set[int]] = {}
    for t in schema.tables:
        rows[t] = {
            r[0] for r in conn.execute(
                f"SELECT {quote_ident(schema.pk[t])} FROM {quote_ident(t)}"
            )
        }
    return rows


def _trial_delete(conn: sqlite3.Connection, schema: Schema, sql1: str, sql2: str,
                  target: RowId, delta_orig: int, start: RowId) -> bool:
    """在 conn 上试删 start 及其直接/间接引用行。

    只有同时满足以下三条才保留删除并返回 True：
      1. 目标工单仍在；
      2. 两版计数差仍与原来同大小同方向；
      3. 在试删后的库上再删目标工单及其引用行，两版计数相等。
    否则（或试算过程中 SQL 失败）回滚到试删前并返回 False。
    """
    conn.execute("SAVEPOINT trial")
    keep = False
    try:
        del_set = impact_set(conn, schema, start)
        if target in del_set or not _row_exists(conn, schema, start):
            return False
        _apply_delete(conn, schema, del_set)
        if not _row_exists(conn, schema, target):
            return False
        c1 = execute_scalar_int(conn, sql1)
        c2 = execute_scalar_int(conn, sql2)
        if c1 - c2 != delta_orig:
            return False
        # 第 3 条须在“试删后”的状态上验证，验完撤掉这次附加删除
        conn.execute("SAVEPOINT sub2_check")
        try:
            _apply_delete(conn, schema, impact_set(conn, schema, target))
            s2_c1 = execute_scalar_int(conn, sql1)
            s2_c2 = execute_scalar_int(conn, sql2)
        finally:
            conn.execute("ROLLBACK TO sub2_check")
            conn.execute("RELEASE sub2_check")
        if s2_c1 != s2_c2:
            return False
        keep = True
        return True
    except AnalysisError:
        return False
    finally:
        if keep:
            conn.execute("RELEASE trial")
        else:
            try:
                conn.execute("ROLLBACK TO trial")
                conn.execute("RELEASE trial")
            except sqlite3.Error:
                # 连接已因底层错误自动回滚：此时数据已恢复到试删前，无需再处理
                pass


def _ordered_rows(conn: sqlite3.Connection, schema: Schema) -> List[RowId]:
    """按表名 + 主键有序枚举当前库内的行，保证裁剪顺序可复现。"""
    rows: List[RowId] = []
    for t in schema.tables:
        pk = schema.pk[t]
        rows.extend(
            (t, r[0]) for r in conn.execute(
                f"SELECT {quote_ident(pk)} FROM {quote_ident(t)} "
                f"ORDER BY {quote_ident(pk)}"
            )
        )
    return rows


def _trim_sub1(conn: sqlite3.Connection, schema: Schema, sql1: str, sql2: str,
               workorder_table: str, witness_pk: int,
               delta_orig: int) -> None:
    """把整库贪心裁剪到“没有可删起点”的不动点。

    conn 必须是以整库内容构建的可写副本；函数返回时 conn 内即子库一的行集。
    每一轮从当前库的每一行尝试删除（连同其直接/间接引用行），只要有一行试删
    生效就继续下一轮，直到整轮没有任何删除成功——此时剩余每一行都是不可删起点。
    """
    target = (workorder_table, witness_pk)
    while True:
        changed = False
        for row in _ordered_rows(conn, schema):
            if row == target or not _row_exists(conn, schema, row):
                continue
            if _trial_delete(conn, schema, sql1, sql2, target, delta_orig, row):
                changed = True
        if not changed:
            return


def _verify_candidate(source: sqlite3.Connection,
                      schema: Schema, sql1: str, sql2: str,
                      workorder_table: str, witness_pk: int,
                      delta_orig: int,
                      work_dir: str) -> Tuple:
    """从整库裁剪出子库一，并派生、验证子库二；全部合格才返回结果元组。"""
    target = (workorder_table, witness_pk)
    trim_path = os.path.join(work_dir, f"trim_{witness_pk}.db")
    trim: Optional[sqlite3.Connection] = None
    try:
        trim = _build_scratch(trim_path, schema, source)
        _trim_sub1(trim, schema, sql1, sql2, workorder_table, witness_pk, delta_orig)

        # 裁剪后的行集即子库一；目标工单必须仍在
        sub1_rows = _row_sets(trim, schema)
        if witness_pk not in sub1_rows[workorder_table]:
            raise AnalysisError("裁剪后目标工单丢失")
        s1_c1 = execute_scalar_int(trim, sql1)
        s1_c2 = execute_scalar_int(trim, sql2)
        if s1_c1 - s1_c2 != delta_orig:
            raise AnalysisError("子库一差值与原差值不一致")

        # 子库二：只从子库一删去目标工单及其（受限）引用行
        sub1_impact = impact_set(
            trim, schema, target,
            {t: set(vals) for t, vals in sub1_rows.items()},
        )
        sub2_rows: Dict[str, Set[int]] = {t: set(v) for t, v in sub1_rows.items()}
        for t, v in sub1_impact:
            sub2_rows[t].discard(v)
        if witness_pk in sub2_rows[workorder_table]:
            raise AnalysisError("子库二未能删除目标工单")

        sub2 = build_subdatabase(":memory:", schema, trim, sub2_rows)
        try:
            s2_c1 = execute_scalar_int(sub2, sql1)
            s2_c2 = execute_scalar_int(sub2, sql2)
        finally:
            sub2.close()
        if s2_c1 != s2_c2:
            raise AnalysisError("子库二删除后两版计数不相等")

        # 子库一自身也要过外键校验（在内存里建一次即可）
        build_subdatabase(":memory:", schema, trim, sub1_rows).close()
    finally:
        if trim is not None:
            trim.close()
        try:
            os.remove(trim_path)
        except OSError:
            pass

    return (witness_pk, sub1_rows, sub2_rows, sub1_impact,
            s1_c1, s1_c2, s2_c1, s2_c2)


def _check_fixed_point(conn: sqlite3.Connection, schema: Schema,
                       sql1: str, sql2: str, workorder_table: str,
                       witness_pk: int, delta_orig: int) -> None:
    """对已落盘的子库一复核“没有可删起点”。

    任何一行（含目标工单外的所有行）按同一试删规则都不得再删成功。
    """
    target = (workorder_table, witness_pk)
    for row in _ordered_rows(conn, schema):
        if row == target:
            continue
        if _trial_delete(conn, schema, sql1, sql2, target, delta_orig, row):
            raise AnalysisError(
                f"子库一仍可裁剪：删除 {row[0]} 主键 {row[1]} 后全部条件成立"
            )


def _verify_candidate(source: sqlite3.Connection,
                      schema: Schema, sql1: str, sql2: str,
                      workorder_table: str, witness_pk: int,
                      delta_orig: int,
                      work_dir: str) -> Tuple:
    """从整库裁剪出子库一，并派生、验证子库二；全部合格才返回结果元组。"""
    target = (workorder_table, witness_pk)
    trim_path = os.path.join(work_dir, f"trim_{witness_pk}.db")
    trim: Optional[sqlite3.Connection] = None
    try:
        trim = _build_scratch(trim_path, schema, source)
        _trim_sub1(trim, schema, sql1, sql2, workorder_table, witness_pk, delta_orig)
        _check_fixed_point(trim, schema, sql1, sql2,
                           workorder_table, witness_pk, delta_orig)

        # 裁剪后的行集即子库一；目标工单必须仍在
        sub1_rows = _row_sets(trim, schema)
        if witness_pk not in sub1_rows[workorder_table]:
            raise AnalysisError("裁剪后目标工单丢失")
        s1_c1 = execute_scalar_int(trim, sql1)
        s1_c2 = execute_scalar_int(trim, sql2)
        if s1_c1 - s1_c2 != delta_orig:
            raise AnalysisError("子库一差值与原差值不一致")

        # 子库二：只从子库一删去目标工单及其（受限）引用行
        sub1_impact = impact_set(
            trim, schema, target,
            {t: set(vals) for t, vals in sub1_rows.items()},
        )
        sub2_rows: Dict[str, Set[int]] = {t: set(v) for t, v in sub1_rows.items()}
        for t, v in sub1_impact:
            sub2_rows[t].discard(v)
        if witness_pk in sub2_rows[workorder_table]:
            raise AnalysisError("子库二未能删除目标工单")

        sub2 = build_subdatabase(":memory:", schema, trim, sub2_rows)
        try:
            s2_c1 = execute_scalar_int(sub2, sql1)
            s2_c2 = execute_scalar_int(sub2, sql2)
        finally:
            sub2.close()
        if s2_c1 != s2_c2:
            raise AnalysisError("子库二删除后两版计数不相等")

        # 子库一自身也要过外键校验（在内存里建一次即可）
        build_subdatabase(":memory:", schema, trim, sub1_rows).close()
    finally:
        if trim is not None:
            trim.close()
        try:
            os.remove(trim_path)
        except OSError:
            pass

    return (witness_pk, sub1_rows, sub2_rows, sub1_impact,
            s1_c1, s1_c2, s2_c1, s2_c2)


def analyze(db_path: str, workorder_table: str, sql_v1: str, sql_v2: str,
            out_dir: str) -> AnalysisResult:
    """对外入口：任何底层 SQLite 故障都转成 AnalysisError，只暴露原因。"""
    try:
        return _analyze_impl(db_path, workorder_table, sql_v1, sql_v2, out_dir)
    except AnalysisError:
        raise
    except sqlite3.Error as exc:
        raise AnalysisError(f"数据库处理失败: {exc}") from exc


def _analyze_impl(db_path: str, workorder_table: str, sql_v1: str, sql_v2: str,
                  out_dir: str) -> AnalysisResult:
    sql1 = validate_sql(sql_v1)
    sql2 = validate_sql(sql_v2)

    source = _open_ro(db_path)
    source.execute("PRAGMA query_only=ON")
    scratch: Optional[sqlite3.Connection] = None
    scratch_path = os.path.join(out_dir, "scratch.db")
    try:
        schema = load_schema(source)
        if workorder_table not in schema.pk:
            raise AnalysisError(f"工单表 {workorder_table} 不存在")

        c1_orig = execute_scalar_int(source, sql1)
        c2_orig = execute_scalar_int(source, sql2)
        delta_orig = c1_orig - c2_orig
        if delta_orig == 0:
            raise AnalysisError("两版月报计数原本就相同，无需归因")

        scratch = _build_scratch(scratch_path, schema, source)
        memo: Dict[RowId, Set[RowId]] = {}

        def impact(start: RowId) -> Set[RowId]:
            if start not in memo:
                memo[start] = impact_set(source, schema, start)
            return memo[start]

        pk_col = schema.pk[workorder_table]
        order = list(source.execute(
            f"SELECT {quote_ident(pk_col)} FROM {quote_ident(workorder_table)} "
            f"ORDER BY {quote_ident(pk_col)}"
        ))
        if not order:
            raise AnalysisError(f"工单表 {workorder_table} 为空，找不到工单")

        # 第一轮：整库上找出“删除后两版计数相等”的工单，按影响集大小排序
        candidates: List[Tuple[int, Set[RowId]]] = []
        for (pk_val,) in order:
            start = (workorder_table, pk_val)
            deleted = impact(start)
            c1x, c2x = _recompute(scratch, sql1, sql2, deleted, schema)
            if c1x == c2x:
                candidates.append((pk_val, deleted))
        if not candidates:
            raise AnalysisError("找不到删除后可使两版计数相等的工单")
        candidates.sort(key=lambda item: (len(item[1]), item[0]))

        # 第二轮：逐候选做完整验证，取第一个全部通过者
        verified = None
        for witness_pk, _full_impact in candidates:
            try:
                verified = _verify_candidate(
                    source, schema, sql1, sql2,
                    workorder_table, witness_pk, delta_orig, out_dir,
                )
            except AnalysisError:
                continue
            break
        if verified is None:
            raise AnalysisError(
                "候选工单均无法同时满足差值保持、子库二复算与最小性要求"
            )
        (witness_pk, sub1_rows, sub2_rows, sub1_impact,
         s1_c1, s1_c2, s2_c1, s2_c2) = verified

        sub1_path = os.path.join(out_dir, f"sub1_with_workorder_{witness_pk}.db")
        sub2_path = os.path.join(out_dir, f"sub2_without_workorder_{witness_pk}.db")
        build_subdatabase(sub1_path, schema, source, sub1_rows).close()
        build_subdatabase(sub2_path, schema, source, sub2_rows).close()

        orig_rows = _row_sets(source, schema)
        pruned_rows = sorted(
            ([t, v] for t in schema.tables
             for v in orig_rows.get(t, set()) - sub1_rows.get(t, set())),
            key=lambda x: (x[0], x[1]),
        )

        # 对最终落盘的两个文件独立复算，确保交付物本身合格
        for path, expect_equal, expect_delta in (
            (sub1_path, False, delta_orig),
            (sub2_path, True, 0),
        ):
            chk = sqlite3.connect(path)
            try:
                if chk.execute("PRAGMA foreign_key_check").fetchall():
                    raise AnalysisError(f"{os.path.basename(path)} 外键校验失败")
                a = execute_scalar_int(chk, sql1)
                b = execute_scalar_int(chk, sql2)
                if expect_equal and a != b:
                    raise AnalysisError(f"{os.path.basename(path)} 复算失败: {a} vs {b}")
                if not expect_equal and a - b != expect_delta:
                    raise AnalysisError(
                        f"{os.path.basename(path)} 差值 {a - b} 不等于原差值 {expect_delta}"
                    )
            finally:
                chk.close()

        manifest = {
            "workorder_table": workorder_table,
            "workorder_pk": witness_pk,
            "sql_v1": sql1,
            "sql_v2": sql2,
            "original": {
                "count_v1": c1_orig,
                "count_v2": c2_orig,
                "delta_v1_minus_v2": delta_orig,
            },
            "sub1": {
                "file": os.path.basename(sub1_path),
                "count_v1": s1_c1,
                "count_v2": s1_c2,
                "delta_v1_minus_v2": s1_c1 - s1_c2,
                "row_counts": {t: len(v) for t, v in sub1_rows.items()},
                "pruned_rows": pruned_rows,
            },
            "sub2": {
                "file": os.path.basename(sub2_path),
                "count_v1": s2_c1,
                "count_v2": s2_c2,
                "delta_v1_minus_v2": 0,
                "deleted_rows": sorted(
                    ([t, v] for (t, v) in sub1_impact),
                    key=lambda x: (x[0], x[1]),
                ),
            },
        }
        return AnalysisResult(witness_pk, sub1_path, sub2_path, manifest)
    finally:
        if scratch is not None:
            scratch.close()
        source.close()
        try:
            os.remove(scratch_path)
        except OSError:
            pass
