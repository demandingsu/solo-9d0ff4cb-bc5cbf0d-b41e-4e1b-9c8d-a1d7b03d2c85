"""边界补充：反方向差值、空工单表、损坏文件、未知表、非整数返回、真实 uvicorn 上传下载。"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_user_site = "/tmp/pyuser/lib/python3.11/site-packages"
if os.path.isdir(_user_site) and _user_site not in sys.path:
    sys.path.append(_user_site)

from app.engine import AnalysisError, analyze  # noqa: E402
from tests.make_fixture import build_cyclic_db  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def test_reverse_direction(workdir):
    print("[A] 反方向差值（v2 > v1）方向必须原样保留")
    db = os.path.join(workdir, "rev.db")
    build_cyclic_db(db)
    # 对调两条 SQL，差值符号翻转但绝对值不变
    sql_a = "SELECT (SELECT count(*) FROM workorder) + (SELECT count(*) FROM event WHERE v2_visible=1)"
    sql_b = ("SELECT (SELECT count(*) FROM workorder) + (SELECT count(*) FROM event) "
             "+ (SELECT count(*) FROM attachment)")
    out = os.path.join(workdir, "out_rev")
    os.makedirs(out)
    r = analyze(db, "workorder", sql_a, sql_b, out)
    check("仍命中工单 1", r.workorder_pk == 1)
    check("原差值为负", r.manifest["original"]["delta_v1_minus_v2"] == -3,
          str(r.manifest["original"]))
    check("子库一保留负方向", r.manifest["sub1"]["delta_v1_minus_v2"] == -3)
    check("子库二相等", r.manifest["sub2"]["count_v1"] == r.manifest["sub2"]["count_v2"])


def test_empty_table(workdir):
    print("[B] 工单表为空")
    db = os.path.join(workdir, "empty.db")
    build_cyclic_db(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM attachment")
    conn.execute("DELETE FROM event")
    conn.execute("DELETE FROM dispatch")
    conn.execute("DELETE FROM workorder")
    conn.commit()
    conn.close()
    out = os.path.join(workdir, "out_empty")
    os.makedirs(out)
    try:
        analyze(db, "workorder", "SELECT 1", "SELECT 2", out)
        check("空表应报错", False)
    except AnalysisError as e:
        check("原因包含“空”", "空" in str(e), str(e))


def test_bad_inputs(workdir):
    print("[C] 损坏文件 / 未知表 / 非整数 SQL")
    junk = os.path.join(workdir, "junk.db")
    with open(junk, "wb") as f:
        f.write(b"not a sqlite database" * 10)
    out = os.path.join(workdir, "out_bad")
    os.makedirs(out)
    for label, kw in (("损坏文件", None),):
        try:
            analyze(junk, "workorder", "SELECT 1", "SELECT 2", out)
            check(f"{label}应报错", False)
        except AnalysisError as e:
            check(f"{label}返回原因", bool(str(e)), str(e))

    db = os.path.join(workdir, "ok.db")
    build_cyclic_db(db)
    try:
        analyze(db, "no_such_table", "SELECT 1", "SELECT 2", out)
        check("未知表应报错", False)
    except AnalysisError as e:
        check("未知表返回原因", "不存在" in str(e), str(e))

    try:
        analyze(db, "workorder", "SELECT 'x'", "SELECT 1", out)
        check("非整数返回应报错", False)
    except AnalysisError as e:
        check("非整数返回原因", "整数" in str(e), str(e))

    try:
        analyze(db, "workorder",
                "SELECT id FROM workorder", "SELECT count(*) FROM workorder", out)
        check("多行返回应报错", False)
    except AnalysisError as e:
        check("多行返回原因", "一行" in str(e), str(e))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_live_server(workdir):
    print("[D] 真实 uvicorn 进程 multipart 上传与 zip 下载")
    port = _free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = "/workspace"
    env["PYTHONUSERBASE"] = "/tmp/pyuser"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd="/workspace", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            check("uvicorn 启动", False, proc.stdout.read()[:500])
            return
        check("uvicorn 启动且 /health 200", True)

        db = os.path.join(workdir, "live.db")
        build_cyclic_db(db)
        boundary = "----witnessboundary123"
        sql1 = ("SELECT (SELECT count(*) FROM workorder) + (SELECT count(*) FROM event) "
                "+ (SELECT count(*) FROM attachment)")
        sql2 = ("SELECT (SELECT count(*) FROM workorder) "
                "+ (SELECT count(*) FROM event WHERE v2_visible=1)")
        parts = []
        for name, value in (("workorder_table", "workorder"),
                            ("sql_v1", sql1), ("sql_v2", sql2)):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="{name}"\r\n\r\n{value}\r\n'.encode())
        with open(db, "rb") as f:
            blob = f.read()
        header = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"db_file\"; "
            'filename="masked.db"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        parts.append(header + blob + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            base + "/analyze", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            check("POST /analyze 200", resp.status == 200)
            check("响应为 zip",
                  resp.headers["Content-Type"] == "application/zip")
            check("工单主键响应头=1", resp.headers["X-Workorder-PK"] == "1")
            payload = resp.read()
        zf = zipfile.ZipFile(io.BytesIO(payload))
        check("zip 可解压且含 manifest", "manifest.json" in zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        check("manifest 内容完整",
              manifest["workorder_pk"] == 1
              and manifest["sub2"]["count_v1"] == manifest["sub2"]["count_v2"])

        # 失败请求经真实服务返回 JSON reason
        eq = os.path.join(workdir, "live_eq.db")
        build_cyclic_db(eq)
        c = sqlite3.connect(eq)
        c.execute("DELETE FROM attachment")
        c.execute("DELETE FROM event WHERE v2_visible=0")
        c.commit()
        c.close()
        parts2 = []
        for name, value in (("workorder_table", "workorder"),
                            ("sql_v1", sql1), ("sql_v2", sql2)):
            parts2.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                          f'name="{name}"\r\n\r\n{value}\r\n'.encode())
        with open(eq, "rb") as f:
            blob2 = f.read()
        header2 = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"db_file\"; "
            'filename="eq.db"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        parts2.append(header2 + blob2 + b"\r\n")
        parts2.append(f"--{boundary}--\r\n".encode())
        req2 = urllib.request.Request(
            base + "/analyze", data=b"".join(parts2), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            urllib.request.urlopen(req2, timeout=60)
            check("失败请求应非 2xx", False)
        except urllib.error.HTTPError as e:
            data = json.loads(e.read())
            check("失败请求 400 且只含原因",
                  e.code == 400 and data["ok"] is False and "相同" in data["reason"],
                  str(data))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    workdir = tempfile.mkdtemp(prefix="witness_edge_")
    try:
        test_reverse_direction(workdir)
        test_empty_table(workdir)
        test_bad_inputs(workdir)
        test_live_server(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
