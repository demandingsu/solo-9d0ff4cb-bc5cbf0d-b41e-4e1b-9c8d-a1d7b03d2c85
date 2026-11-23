"""端到端测试：引擎级 + HTTP 级，覆盖循环引用、两库复算、最小性与失败原因。

运行：PYTHONPATH=/workspace python3 -I tests/test_engine.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 本机依赖安装在自定义 user base；-I 模式下需显式加入
_user_site = "/tmp/pyuser/lib/python3.11/site-packages"
if os.path.isdir(_user_site) and _user_site not in sys.path:
    sys.path.append(_user_site)

from app.engine import AnalysisError, analyze, validate_sql  # noqa: E402
from tests.make_fixture import (  # noqa: E402
    SQL_V1,
    SQL_V2,
    build_cyclic_db,
    build_equal_count_db,
    build_no_candidate_db,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def table_counts(conn: sqlite3.Connection, tables):
    out = {}
    for t in tables:
        out[t] = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
    return out


def test_happy_path(workdir: str) -> None:
    print("[1] 循环引用库：找工单并复算两库")
    db = os.path.join(workdir, "cyclic.db")
    build_cyclic_db(db)
    out = os.path.join(workdir, "out1")
    os.makedirs(out)
    result = analyze(db, "workorder", SQL_V1, SQL_V2, out)

    check("命中工单主键 = 1", result.workorder_pk == 1, str(result.workorder_pk))
    m = result.manifest
    check("原库差值 = 3（v1-v2）", m["original"]["delta_v1_minus_v2"] == 3,
          str(m["original"]))
    check("子库一保留同方向同大小差值",
          m["sub1"]["delta_v1_minus_v2"] == m["original"]["delta_v1_minus_v2"])
    check("子库二差值为 0", m["sub2"]["delta_v1_minus_v2"] == 0)
    check("子库二两计数相等",
          m["sub2"]["count_v1"] == m["sub2"]["count_v2"],
          str(m["sub2"]))

    # 结构与外键完整性
    for label, path in (("sub1", result.sub1_path), ("sub2", result.sub2_path)):
        conn = sqlite3.connect(path)
        try:
            fk_bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            check(f"{label} 外键检查无违例", not fk_bad, str(fk_bad[:3]))
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            check(f"{label} 保留全部 4 张表", tables ==
                  ["attachment", "dispatch", "event", "workorder"], str(tables))
            fk_n = conn.execute(
                "SELECT count(*) FROM pragma_foreign_key_list('event')").fetchone()[0]
            check(f"{label} 保留外键定义", fk_n >= 1, str(fk_n))
            idx_n = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='index' "
                "AND sql IS NOT NULL").fetchone()[0]
            check(f"{label} 保留索引", idx_n == 2, str(idx_n))
        finally:
            conn.close()

    # 子库一必须保留工单 1
    c1 = sqlite3.connect(result.sub1_path)
    try:
        n = c1.execute("SELECT count(*) FROM workorder WHERE id=1").fetchone()[0]
        check("子库一保留工单 1", n == 1)
        check("子库一仅含工单 1 的闭包（workorder=1）",
              c1.execute("SELECT count(*) FROM workorder").fetchone()[0] == 1)
        check("子库一保留循环引用（dispatch 10 与 workorder 1）",
              c1.execute(
                  "SELECT count(*) FROM workorder w JOIN dispatch d "
                  "ON w.dispatch_id=d.id AND d.workorder_id=w.id WHERE w.id=1"
              ).fetchone()[0] == 1)
        check("子库一保留间接引用 attachment",
              c1.execute("SELECT count(*) FROM attachment").fetchone()[0] == 2)
    finally:
        c1.close()

    # 子库二：工单及其直接/间接引用行全部删除，外键仍完整
    c2 = sqlite3.connect(result.sub2_path)
    try:
        counts = table_counts(c2, ["workorder", "dispatch", "event", "attachment"])
        check("子库二删除工单闭包", counts ==
              {"workorder": 0, "dispatch": 0, "event": 0, "attachment": 0}, str(counts))
        a = c2.execute(SQL_V1).fetchone()[0]
        b = c2.execute(SQL_V2).fetchone()[0]
        check("子库二独立复算相等", a == b == 0, f"{a}/{b}")
    finally:
        c2.close()

    # 最小性：在子库一上按同样规则删除任一其他行，要么丢工单要么复算不等
    print("[2] 最小性穷举（子库一内逐行删除复算）")
    from app.engine import (
        impact_set, load_schema, _open_ro, _recompute, _build_scratch,
    )
    src = _open_ro(result.sub1_path)
    try:
        schema = load_schema(src)
        universe = {}
        for t in schema.tables:
            universe[t] = set(
                r[0] for r in src.execute(f'SELECT "{schema.pk[t]}" FROM "{t}"')
            )
        total_rows = sum(len(v) for v in universe.values())
        check("子库一非空（至少含工单闭包 6 行）", total_rows >= 6, str(total_rows))
    finally:
        src.close()

    writable = _build_scratch(os.path.join(workdir, "minimality.db"), schema,
                              _open_ro(result.sub1_path))
    ro = _open_ro(result.sub1_path)
    try:
        bad = []
        for t, ids in universe.items():
            for v in ids:
                if (t, v) == ("workorder", 1):
                    continue
                deleted = impact_set(ro, schema, (t, v), universe)
                keeps = 1 not in {p for (tt, p) in deleted if tt == "workorder"}
                if not keeps:
                    continue
                x, y = _recompute(writable, SQL_V1, SQL_V2, deleted, schema)
                if x == y:
                    bad.append((t, v))
        check("不存在“保留工单且复算相等”的其他删除", not bad, str(bad))
    finally:
        ro.close()
        writable.close()


def test_equal_counts(workdir: str) -> None:
    print("[3] 原计数相同：只返回原因")
    db = os.path.join(workdir, "equal.db")
    build_equal_count_db(db)
    out = os.path.join(workdir, "out_eq")
    os.makedirs(out)
    try:
        analyze(db, "workorder", SQL_V1, SQL_V2, out)
        check("应当抛出 AnalysisError", False)
    except AnalysisError as exc:
        check("原因文本包含“相同”", "相同" in str(exc), str(exc))


def test_no_candidate(workdir: str) -> None:
    print("[4] 有差异但无合格工单：只返回原因")
    db = os.path.join(workdir, "no_cand.db")
    build_no_candidate_db(db)
    out = os.path.join(workdir, "out_nc")
    os.makedirs(out)
    try:
        analyze(db, "workorder", SQL_V1, SQL_V2, out)
        check("应当抛出 AnalysisError", False)
    except AnalysisError as exc:
        check("原因文本包含“找不到”", "找不到" in str(exc), str(exc))


def test_sql_guard() -> None:
    print("[5] SQL 安全校验")
    ok_sql = "WITH t AS (SELECT 1 AS x) SELECT count(*) FROM t"
    check("CTE 只读 SQL 通过", validate_sql(ok_sql).upper().startswith("WITH"))
    bad_cases = [
        ("SELECT 1; SELECT 2", "多语句"),
        ("DELETE FROM workorder", "写操作"),
        ("SELECT * FROM workorder WHERE id = ?", "参数"),
        ("SELECT random()", "非确定性函数"),
        ("SELECT current_timestamp", "非确定性关键字"),
        ("PRAGMA foreign_keys=ON", "PRAGMA"),
        ("ATTACH DATABASE 'x.db' AS x", "ATTACH"),
        ("SELECT count(*) FROM workorder; DROP TABLE event", "分号后第二语句"),
        ("  ", "空 SQL"),
    ]
    for sql, label in bad_cases:
        try:
            validate_sql(sql)
            check(f"拒绝{label}", False, sql)
        except AnalysisError:
            check(f"拒绝{label}", True)


def test_http(workdir: str) -> None:
    print("[6] HTTP 端到端：/docs 所用接口上传下载")
    import httpx
    from fastapi.testclient import TestClient
    from app.main import app

    db = os.path.join(workdir, "cyclic_http.db")
    build_cyclic_db(db)
    client = TestClient(app)
    r = client.get("/health")
    check("/health 200", r.status_code == 200, r.text)

    with open(db, "rb") as f:
        r = client.post(
            "/analyze",
            files={"db_file": ("masked.db", f, "application/octet-stream")},
            data={
                "workorder_table": "workorder",
                "sql_v1": SQL_V1,
                "sql_v2": SQL_V2,
            },
        )
    check("/analyze 返回 zip", r.status_code == 200, r.text[:300])
    check("响应头带工单主键", r.headers.get("X-Workorder-PK") == "1",
          str(r.headers.get("X-Workorder-PK")))
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = sorted(zf.namelist())
    check("zip 含 3 个文件", len(names) == 3, str(names))
    check("zip 含 manifest", any(n == "manifest.json" for n in names))
    manifest = json.loads(zf.read("manifest.json"))
    check("manifest 工单主键=1", manifest["workorder_pk"] == 1)

    extract = os.path.join(workdir, "http_extract")
    zf.extractall(extract)
    for n in names:
        if n.endswith(".db"):
            conn = sqlite3.connect(os.path.join(extract, n))
            try:
                check(f"下载件 {n} 外键完整",
                      not conn.execute("PRAGMA foreign_key_check").fetchall())
            finally:
                conn.close()

    # 失败路径走 HTTP：计数相同
    eq = os.path.join(workdir, "equal_http.db")
    build_equal_count_db(eq)
    with open(eq, "rb") as f:
        r = client.post(
            "/analyze",
            files={"db_file": ("eq.db", f, "application/octet-stream")},
            data={"workorder_table": "workorder",
                  "sql_v1": SQL_V1, "sql_v2": SQL_V2},
        )
    check("计数相同返回 400 + reason",
          r.status_code == 400 and not r.json()["ok"] and "reason" in r.json(),
          r.text[:200])


def test_manual_recompute(workdir: str) -> None:
    print("[7] 手工模拟删除：与引擎结论交叉验证")
    db = os.path.join(workdir, "manual.db")
    build_cyclic_db(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        base1 = conn.execute(SQL_V1).fetchone()[0]
        base2 = conn.execute(SQL_V2).fetchone()[0]
        check(f"原计数 v1={base1} > v2={base2}", base1 - base2 == 3)

        # 删工单 1 闭包
        conn.execute("BEGIN")
        conn.execute("DELETE FROM attachment WHERE event_id IN (SELECT id FROM event WHERE workorder_id=1)")
        conn.execute("DELETE FROM event WHERE workorder_id=1")
        conn.execute("DELETE FROM dispatch WHERE workorder_id=1")
        conn.execute("DELETE FROM workorder WHERE id=1")
        a = conn.execute(SQL_V1).fetchone()[0]
        b = conn.execute(SQL_V2).fetchone()[0]
        conn.execute("ROLLBACK")
        check("手工删工单1闭包后两版相等", a == b, f"{a}/{b}")

        # 删工单 2 闭包：差异仍在
        conn.execute("BEGIN")
        conn.execute("DELETE FROM event WHERE workorder_id=2")
        conn.execute("DELETE FROM dispatch WHERE workorder_id=2")
        conn.execute("DELETE FROM workorder WHERE id=2")
        a = conn.execute(SQL_V1).fetchone()[0]
        b = conn.execute(SQL_V2).fetchone()[0]
        conn.execute("ROLLBACK")
        check("手工删工单2闭包后仍不相等", a != b, f"{a}/{b}")
    finally:
        conn.close()


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="witness_test_")
    try:
        test_happy_path(workdir)
        test_equal_counts(workdir)
        test_no_candidate(workdir)
        test_sql_guard()
        test_http(workdir)
        test_manual_recompute(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
