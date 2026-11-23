# 月报计数差异工单归因服务

同一街道的报修工单数在两版月报里对不上时，数据员上传**脱敏后的 SQLite 库**、
**工单表名**和**两条各返回一个整数的确定性只读 SQL**，服务找出触发差异的那一条工单，
并产出两份保留完整表结构与外键的对照子库，交给报表开发同事复算。

## 1. 判定规则（与需求逐条对应）

设两版计数为 `C1`、`C2`，差值 `Δ = C1 - C2`。

1. **找工单**：对工单表每一行 `w`，沿外键反向边求其**直接/间接引用闭包**
   （谁引用了它、谁又引用了引用者……循环引用靠 visited 集合终止），
   在整库可写副本上删除该闭包后复算两条 SQL，`C1' = C2'` 的工单成为候选。
2. **子库一（保留工单，保留差值）**：行集为工单的**双向闭包**——
   反向纳入全部引用行以保留差异，正向纳入这些行外键指向的行以保证外键可解析；
   表结构（`CREATE TABLE`/索引）与全部外键定义原样复制，复算差值须与原库
   **同绝对值、同方向**。
3. **子库二（仅删工单闭包，计数相等）**：在子库一自身的行图内，
   从工单出发重新求受限反向引用闭包并剔除，剩余行重建为同结构子库；
   `PRAGMA foreign_key_check` 无违例，且两版计数相等。
4. **最小性**：在子库一内按同样规则删除**任意其他一行**并复算，
   若该删除仍保留工单，则两版计数必须不相等；若删除会连带删掉工单，则天然满足。
   引擎对候选逐个做完整验证，取第一个全部通过者；全部失败则只返回原因。
5. **短路返回原因**：原计数相同、无候选工单、SQL/库不合法、处理异常时，
   统一返回 `{"ok": false, "reason": "..."}`，不产出子库。

## 2. SQL 安全与确定性约束

- 词法扫描（识别字符串/标识符引号、注释、分号），拒绝多语句、参数占位符；
- 仅允许 `SELECT`/`WITH` 开头；拒绝 `INSERT/UPDATE/DELETE/DDL/ATTACH/PRAGMA/…`；
- 拒绝 `random/randomblob/last_insert_rowid/changes/current_timestamp` 等
  非确定性或会话相关函数/关键字；
- 原库以 `mode=ro` 只读 URI 打开并置 `query_only=ON`；
- 执行结果必须**单行单列且为整数**（布尔、NULL、文本均拒绝）。

## 3. 目录结构

```
app/
  engine.py   # 纯分析引擎：schema 解析、引用闭包、子库构建、复算与最小性
  main.py     # FastAPI：/health、/analyze（multipart 上传，zip 下载）
tests/
  make_fixture.py  # 带循环外键的脱敏夹具库
  test_engine.py   # 引擎+HTTP 端到端（45 项）
  test_edge.py     # 反向差值/空表/坏文件/真实 uvicorn（16 项）
Dockerfile
docker-compose.yml
requirements.txt
```

## 4. 启动与使用

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000/docs ，使用 POST /analyze
```

表单字段：

| 字段 | 说明 |
| --- | --- |
| `db_file` | 脱敏 SQLite 文件（≤200 MiB，后缀 .db/.sqlite/.sqlite3/.bin） |
| `workorder_table` | 工单表名，要求单列 INTEGER 主键 |
| `sql_v1` / `sql_v2` | 两条确定性只读、返回单个整数的 SQL |

成功返回 `application/zip`，响应头 `X-Workorder-PK` 给出工单主键，包内：

- `sub1_with_workorder_<pk>.db`：保留工单与原差值（大小、方向）；
- `sub2_without_workorder_<pk>.db`：仅删工单引用闭包，两版计数相等；
- `manifest.json`：原库/两子库计数、差值、各表行数与删除清单。

失败返回 HTTP 400/413/500 与 JSON 原因，例如
`{"ok": false, "reason": "两版月报计数原本就相同，无需归因"}`。

无 Docker 时本地运行：

```bash
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 5. 实测记录（本环境）

环境：Python 3.11.2，SQLite 随解释器版本；依赖安装于独立 user-base（系统无 pip，
先用 get-pip 引导）。**本机无 Docker 守护进程**，容器构建未在本机执行，
已用真实 `uvicorn` 进程 + 手工 multipart 请求覆盖同一接口路径。

- `tests/test_engine.py`：**45/45 通过**，含循环引用夹具
  （`workorder ↔ dispatch` 互指）、两库外键校验、双向闭包、最小性穷举、
  手工删除交叉验证、TestClient 上传下载；
- `tests/test_edge.py`：**16/16 通过**，含反向差值（Δ=-3 方向保留）、
  空工单表、损坏文件、未知表、非整数/多行 SQL、真实 uvicorn 进程的
  上传→zip 解压→两库复算→失败请求 reason；
- 夹具原库 `C1=8, C2=5, Δ=3`；命中工单主键 `1`；子库一 Δ 仍为 3；
  子库二删除工单 1 及其派单/事件/附件闭包后 `C1=C2=0`，外键检查无违例。

复现命令（依赖路径按本机实际调整）：

```bash
PYTHONUSERBASE=/tmp/pyuser python3 -I tests/test_engine.py
PYTHONUSERBASE=/tmp/pyuser python3 -I tests/test_edge.py
```

## 6. 前提、限制与余项

- 表必须为单列 INTEGER 主键、外键单列且指向他表主键；复合外键/复合主键/虚表
  会被明确拒绝（与“各表以整数为主键、外键指向主键”的前提一致）；
- 库内无触发器：引擎显式检查 `sqlite_master`，存在触发器即拒绝；
- 大库性能：候选复算为“每工单一次事务内删除+回滚”，工单量大时耗时随
  工单数线性增长；子库一最小性为子库内行数级穷举，仅在候选子库内进行；
- 上传大小上限默认 200 MiB（`app/main.py:MAX_UPLOAD_BYTES`），临时目录可经
  compose 的 `tmpfs`/`TMPDIR` 调整；
- 待补：在装有 Docker 的环境执行一次 `docker compose up --build` 冒烟
  （Dockerfile 已含 healthcheck 与非 root 用户）；如未来出现多列主键/复合外键
  的脱敏库，需要扩展 schema 模型与闭包算法。
