"""无外键关联与试删裁剪回归：街道编码口径表、同街道旁支、试删后对照失衡。

覆盖需求中的四条裁剪规则：
  1. 从第一份库当前一行试删，连同直接/间接引用行一起删；
  2. 目标工单还在、两版差值同大小同方向、再删目标工单及引用行后计数相等，
     三条同时成立试删才生效；
  3. 未通过的试删恢复到试删前；
  4. 第一份库裁到没有可删起点，第二份只从它删去目标工单及其引用行。

运行：PYTHONPATH=/workspace python3 -I tests/test_street.py
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
_user_site = "/tmp/pyuser/lib/python3.11/site-packages"
if os.path.isdir(_user_site) and _user_site not in sys.path:
    sys.path.append(_user_site)

from app.engine import (  # noqa: E402
    AnalysisError,
    analyze,
    impact_set,
    load_schema,
    _build_scratch,
    _open_ro,
    _row_exists,
    _row_sets,
    _trial_delete,
)
from tests.make_fixture import (  # noqa: E402
    ROLLBACK_SQL_V1,
    ROLLBACK_SQL_V2,
    STREET_SQL_V1,
    STREET_SQL_V2,
    build_no_fk_street_db,
    build_trial_rollback_db,
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


def _rows(path: str, tables):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall()
                for t in tables}
    finally:
        conn.close()


def _scalar(path: str, sql: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _pk_set(path: str, tables) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {t: {r[0] for r in conn.execute(f'SELECT * FROM "{t}"')}
                for t in tables}
    finally:
        conn.close()


def _trial_conditions(conn, schema, sql1, sql2, target, delta, start):
    """独立复刻三条试删条件，返回 (全部成立?, 失败在哪一条)。"""
    conn.execute("SAVEPOINT t")
    try:
        del_set = impact_set(conn, schema, start)
        if target in del_set or not _row_exists(conn, schema, start):
            return False, "cond1"
        by_table = {}
        for t, v in del_set:
            by_table.setdefault(t, []).append(v)
        for t, vals in by_table.items():
            conn.execute(
                f'DELETE FROM "{t}" WHERE "{schema.pk[t]}" IN '
                f'({",".join("?" for _ in vals)})', vals)
        if not _row_exists(conn, schema, target):
            return False, "cond1"
        c1 = conn.execute(sql1).fetchone()[0]
        c2 = conn.execute(sql2).fetchone()[0]
        if c1 - c2 != delta:
            return False, "cond2"
        tdel = impact_set(conn, schema, target)
        by_table = {}
        for t, v in tdel:
            by_table.setdefault(t, []).append(v)
        for t, vals in by_table.items():
            conn.execute(
                f'DELETE FROM "{t}" WHERE "{schema.pk[t]}" IN '
                f'({",".join("?" for _ in vals)})', vals)
        d1 = conn.execute(sql1).fetchone()[0]
        d2 = conn.execute(sql2).fetchone()[0]
        if d1 != d2:
            return False, "cond3"
        return True, None
    finally:
        conn.execute("ROLLBACK TO t")
        conn.execute("RELEASE t")


def _assert_fixed_point(label, path, sql1, sql2, workorder_table, witness):
    """子库一内任意其他行按三条试删条件都不得再删成功。"""
    src = _open_ro(path)
    schema = load_schema(src)
    scratch = _build_scratch(path + ".scratch.db", schema, src)
    src.close()
    try:
        delta = (scratch.execute(sql1).fetchone()[0]
                 - scratch.execute(sql2).fetchone()[0])
        bad = []
        for row in sorted(_row_sets(scratch, schema).items()):
            for v in sorted(row[1]):
                if (row[0], v) == (workorder_table, witness):
                    continue
                ok, why = _trial_conditions(
                    scratch, schema, sql1, sql2,
                    (workorder_table, witness), delta, (row[0], v))
                if ok:
                    bad.append((row[0], v))
        check(f"{label} 已裁到没有可删起点", not bad, str(bad[:5]))
    finally:
        scratch.close()
        os.remove(path + ".scratch.db")


def test_no_fk_street(workdir: str) -> None:
    print("[S1] 街道编码关联、无外键：仍能交付合格样库")
    db = os.path.join(workdir, "street.db")
    build_no_fk_street_db(db)
    out = os.path.join(workdir, "out_street")
    os.makedirs(out)

    result = analyze(db, "workorder", STREET_SQL_V1, STREET_SQL_V2, out)
    m = result.manifest
    check("命中工单主键 1", result.workorder_pk == 1, str(result.workorder_pk))
    check("原差值 2-1=1", m["original"]["delta_v1_minus_v2"] == 1,
          str(m["original"]))
    check("子库一差值同大小同方向",
          m["sub1"]["delta_v1_minus_v2"] == 1, str(m["sub1"]))
    check("子库二两版计数相等",
          m["sub2"]["count_v1"] == m["sub2"]["count_v2"], str(m["sub2"]))

    tables = ["street_caliber", "visit", "visit_photo", "workorder"]
    sub1 = _pk_set(result.sub1_path, tables)
    sub2 = _pk_set(result.sub2_path, tables)

    check("子库一保留目标工单 1", sub1["workorder"] == {1}, str(sub1["workorder"]))
    check("同街道旁支工单 2 未留在子库一", 2 not in sub1["workorder"],
          str(sub1["workorder"]))
    check("别街道工单 3 未留在子库一", 3 not in sub1["workorder"],
          str(sub1["workorder"]))
    check("子库一保留命中差异的 S1 口径行", sub1["street_caliber"] == {1},
          str(sub1["street_caliber"]))
    check("子库一未留同街道旁支的 visit/photo",
          sub1["visit"] == set() and sub1["visit_photo"] == set(),
          f'{sub1["visit"]}/{sub1["visit_photo"]}')

    # 子库二 == 子库一减去目标工单及其（子库一内）引用行，且不多删
    src = _open_ro(result.sub1_path)
    schema = load_schema(src)
    universe = {t: set(vals) for t, vals in _row_sets(src, schema).items()}
    target_closure = impact_set(src, schema, ("workorder", 1), universe)
    src.close()
    expected = {t: set(vals) for t, vals in _pk_set(result.sub1_path, tables).items()}
    for t, v in target_closure:
        expected[t].discard(v)
    check("子库二只删去目标工单及其引用行",
          sub2 == expected, f"got={sub2} want={expected}")
    check("子库二不再含目标工单", 1 not in sub2["workorder"], str(sub2["workorder"]))
    check("同街道旁支工单 2 也未留在子库二", 2 not in sub2["workorder"],
          str(sub2["workorder"]))

    # 结构与外键完整
    for label, path in (("sub1", result.sub1_path), ("sub2", result.sub2_path)):
        conn = sqlite3.connect(path)
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            check(f"{label} 保留 4 张表", names == sorted(tables), str(names))
            check(f"{label} 外键检查无违例",
                  not conn.execute("PRAGMA foreign_key_check").fetchall())
            fk_n = conn.execute(
                "SELECT count(*) FROM pragma_foreign_key_list('visit')").fetchone()[0]
            check(f"{label} 保留 visit 外键", fk_n == 1, str(fk_n))
            idx_n = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='index' "
                "AND sql IS NOT NULL").fetchone()[0]
            check(f"{label} 保留 2 个索引", idx_n == 2, str(idx_n))
        finally:
            conn.close()

    check("子库一独立复算差值为 1",
          _scalar(result.sub1_path, STREET_SQL_V1)
          - _scalar(result.sub1_path, STREET_SQL_V2) == 1)
    check("子库二独立复算相等",
          _scalar(result.sub2_path, STREET_SQL_V1)
          == _scalar(result.sub2_path, STREET_SQL_V2))

    _assert_fixed_point("无外键样库一", result.sub1_path,
                        STREET_SQL_V1, STREET_SQL_V2, "workorder", 1)

    # 反向差值（方向也要保留）
    out2 = os.path.join(workdir, "out_street_rev")
    os.makedirs(out2)
    rev = analyze(db, "workorder", STREET_SQL_V2, STREET_SQL_V1, out2)
    check("反向后差值为 -1",
          rev.manifest["original"]["delta_v1_minus_v2"] == -1,
          str(rev.manifest["original"]))
    check("反向后子库一保留 -1",
          rev.manifest["sub1"]["delta_v1_minus_v2"] == -1, str(rev.manifest["sub1"]))


def test_trial_rollback(workdir: str) -> None:
    print("[S2] 试删后对照失衡：必须回滚到试删前")
    db = os.path.join(workdir, "rollback.db")
    build_trial_rollback_db(db)
    out = os.path.join(workdir, "out_rollback")
    os.makedirs(out)

    result = analyze(db, "workorder", ROLLBACK_SQL_V1, ROLLBACK_SQL_V2, out)
    m = result.manifest
    check("命中工单主键 3", result.workorder_pk == 3, str(result.workorder_pk))
    check("原差值 2-3=-1", m["original"]["delta_v1_minus_v2"] == -1,
          str(m["original"]))
    check("子库一保留 -1", m["sub1"]["delta_v1_minus_v2"] == -1, str(m["sub1"]))
    check("子库二两版计数相等",
          m["sub2"]["count_v1"] == m["sub2"]["count_v2"], str(m["sub2"]))

    # 工单 2 的试删：差值仍保留，但再删目标后两版计数不等，必须被回滚并留下
    sub1_wo = _pk_set(result.sub1_path, ["workorder"])["workorder"]
    check("试删失败的工单 2 仍留在子库一", 2 in sub1_wo, str(sub1_wo))

    src = _open_ro(db)
    schema = load_schema(src)
    tweak = _build_scratch(os.path.join(workdir, "tweak.db"), schema, src)
    src.close()
    try:
        delta = (tweak.execute(ROLLBACK_SQL_V1).fetchone()[0]
                 - tweak.execute(ROLLBACK_SQL_V2).fetchone()[0])
        ok, why = _trial_conditions(
            tweak, schema, ROLLBACK_SQL_V1, ROLLBACK_SQL_V2,
            ("workorder", 3), delta, ("workorder", 2))
        check("删工单 2 恰好卡在第 3 条（删目标后计数不等）",
              (not ok) and why == "cond3", f"ok={ok} why={why}")
    finally:
        tweak.close()

    # 直接验证 _trial_delete 失败后数据完全恢复
    src = _open_ro(db)
    schema = load_schema(src)
    scratch = _build_scratch(os.path.join(workdir, "restore.db"), schema, src)
    src.close()
    try:
        before = _row_sets(scratch, schema)
        delta = (scratch.execute(ROLLBACK_SQL_V1).fetchone()[0]
                 - scratch.execute(ROLLBACK_SQL_V2).fetchone()[0])
        kept = _trial_delete(
            scratch, schema, ROLLBACK_SQL_V1, ROLLBACK_SQL_V2,
            ("workorder", 3), delta, ("workorder", 2))
        after = _row_sets(scratch, schema)
        check("试删未通过返回 False", kept is False)
        check("未通过的试删恢复到试删前", before == after,
              f"{before} -> {after}")
        check("未通过的试删未残留事务", scratch.in_transaction is False)
    finally:
        scratch.close()

    _assert_fixed_point("试删回滚样库一", result.sub1_path,
                        ROLLBACK_SQL_V1, ROLLBACK_SQL_V2, "workorder", 3)


def test_http_no_fk(workdir: str) -> None:
    print("[S3] HTTP：无外键库经 /analyze 上传下载")
    from fastapi.testclient import TestClient
    from app.main import app

    db = os.path.join(workdir, "street_http.db")
    build_no_fk_street_db(db)
    client = TestClient(app)
    with open(db, "rb") as f:
        r = client.post(
            "/analyze",
            files={"db_file": ("street.db", f, "application/octet-stream")},
            data={
                "workorder_table": "workorder",
                "sql_v1": STREET_SQL_V1,
                "sql_v2": STREET_SQL_V2,
            },
        )
    check("/analyze 无外键库返回 200", r.status_code == 200, r.text[:300])
    check("响应头工单主键=1", r.headers.get("X-Workorder-PK") == "1",
          str(r.headers.get("X-Workorder-PK")))
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = json.loads(zf.read("manifest.json"))
    check("manifest 记录被裁行", bool(manifest["sub1"]["pruned_rows"]),
          str(manifest["sub1"].get("pruned_rows")))

    extract = os.path.join(workdir, "http_street_extract")
    zf.extractall(extract)
    for name in zf.namelist():
        if not name.endswith(".db"):
            continue
        conn = sqlite3.connect(os.path.join(extract, name))
        try:
            check(f"下载件 {name} 外键完整",
                  not conn.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            conn.close()


def test_no_deliverable(workdir: str) -> None:
    print("[S4] 无法交付合格样库时只返回原因")
    db = os.path.join(workdir, "street_none.db")
    build_no_fk_street_db(db)
    conn = sqlite3.connect(db)
    try:
        # S1 与 S2 两个街道各自都有 v2 不可见的口径差异，删掉任一工单只消除
        # 其中一处，两版计数仍不等——任何工单都不能成为候选。
        conn.execute("UPDATE street_caliber SET v1_visible=1, v2_visible=0 "
                     "WHERE street_code='S2'")
        conn.commit()
    finally:
        conn.close()
    out = os.path.join(workdir, "out_none")
    os.makedirs(out)
    try:
        analyze(db, "workorder", STREET_SQL_V1, STREET_SQL_V2, out)
        check("应当抛出 AnalysisError", False)
    except AnalysisError as exc:
        check("只以 reason 形式返回原因", "找不到" in str(exc), str(exc))
        check("失败时不产出子库", not any(
            n.endswith(".db") for n in os.listdir(out)), str(os.listdir(out)))


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="witness_street_")
    try:
        test_no_fk_street(workdir)
        test_trial_rollback(workdir)
        test_http_no_fk(workdir)
        test_no_deliverable(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
