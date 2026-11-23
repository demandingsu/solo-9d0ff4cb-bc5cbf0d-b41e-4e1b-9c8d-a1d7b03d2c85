"""FastAPI 入口：/docs 上传脱敏 SQLite 与两条只读 SQL，下载两个对照子库。"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

from .engine import AnalysisError, analyze

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB
ALLOWED_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".bin")

app = FastAPI(
    title="月报计数差异工单归因服务",
    description=(
        "上传脱敏 SQLite 库、工单表名与两条各返回一个整数的确定性只读 SQL，"
        "服务返回触发差异的工单主键，以及保留表结构和完整外键的两份对照子库。"
    ),
    version="1.0.0",
)


def _failure(reason: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "reason": reason})


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"ok": True, "service": "workorder-diff-witness"}


@app.post("/analyze", tags=["analysis"])
async def analyze_upload(
    db_file: UploadFile = File(..., description="脱敏后的 SQLite 数据库文件"),
    workorder_table: str = Form(..., description="工单表名（整数主键）"),
    sql_v1: str = Form(..., description="月报第一版计数 SQL，必须返回单个整数"),
    sql_v2: str = Form(..., description="月报第二版计数 SQL，必须返回单个整数"),
):
    table = (workorder_table or "").strip()
    if not table:
        return _failure("工单表名不能为空")

    workdir = tempfile.mkdtemp(prefix="witness_")
    db_path = os.path.join(workdir, "input.db")
    try:
        suffix = os.path.splitext(db_file.filename or "")[1].lower()
        if suffix and suffix not in ALLOWED_SUFFIXES:
            return _failure(f"不支持的文件后缀: {suffix}")

        size = 0
        with open(db_path, "wb") as out:
            while True:
                chunk = await db_file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    return _failure("上传文件超过 200MiB 限制", status=413)
                out.write(chunk)
        if size == 0:
            return _failure("上传的数据库文件为空")

        out_dir = os.path.join(workdir, "out")
        os.makedirs(out_dir, exist_ok=True)
        try:
            result = analyze(db_path, table, sql_v1, sql_v2, out_dir)
        except AnalysisError as exc:
            return _failure(str(exc))

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(result.sub1_path, os.path.basename(result.sub1_path))
            zf.write(result.sub2_path, os.path.basename(result.sub2_path))
            zf.writestr("manifest.json", json.dumps(
                result.manifest, ensure_ascii=False, indent=2))
        payload = buffer.getvalue()

        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="witness_workorder_{result.workorder_pk}.zip"'
                ),
                "X-Workorder-PK": str(result.workorder_pk),
            },
        )
    except Exception as exc:  # 兜底：任何未预期失败都只返回原因，不泄露堆栈
        return _failure(f"处理失败: {exc.__class__.__name__}: {exc}", status=500)
    finally:
        await db_file.close()
        shutil.rmtree(workdir, ignore_errors=True)
