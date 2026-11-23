# 月报计数差异工单归因服务

同一街道的报修工单数在两版月报里对不上时，数据员上传**脱敏后的 SQLite 库**、
**工单表名**和**两条各返回一个整数的确定性只读 SQL**，服务找出触发差异的那一条工单，
并产出两份保留完整表结构与外键的对照子库，交给报表开发同事复算。

## 1. 判定规则（与需求逐条对应）

设两版计数为 `C1`、`C2`，差值 `Δ = C1 - C2`。

1. **找工单**：对工单表每一行 `w`，沿外键反向边求其**直接/间接引用闭包**
   （谁引用了它、谁又引用了引用者……循环引用靠 visited 集合终止），
   在整库可写副本上删除该闭包后复算两条 SQL，`C1' = C2'` 的工单成为候选。
2. **子库一（保留工单，保留差值，裁到不动点）**：从整库出发反复**试删**：
   对当前库内的每一行，连同其直接/间接引用行一起删除，只有同时满足
   - 目标工单仍在；
   - 两版计数差仍与原库**同大小、同方向**；
   - 在这个“试删后”的库上再删目标工单及其引用行，两版计数**相等**；

   才保留这次删除；任一条不满足（或试算 SQL 失败）都**回滚到试删前**。
   一轮扫描有删除就再扫一轮，直到整轮没有任何删除成功，即得到“没有可删起点”
   的子库一。该做法不依赖表间一定存在外键：报表按街道编码等字符串列关联口径表时，
   口径行与工单行互为“可裁剪旁支”，同样会被正确裁掉。
3. **子库二（仅删工单闭包，计数相等）**：在子库一自身的行图内，从工单出发求
   受限反向引用闭包并剔除，其余行原样保留为子库二；两版计数相等。
   子库二**只能**是子库一去掉目标工单及其引用行的结果，不做额外裁剪。
4. **完整性与最小性**：两库均原样复制全部 `CREATE TABLE`、索引和全部外键定义，
   `PRAGMA foreign_key_check` 无违例，循环引用原样保留；落盘后还会逐个重启连接
   复算，并对子库一复核“任意其他行都无法再通过三条试删条件”。
5. **短路返回原因**：原计数相同、无候选工单、全部候选都裁不出合格样库、
   SQL/库不合法、处理异常时，统一返回 `{"ok": false, "reason": "..."}`，不产出子库。

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
  engine.py   # 纯分析引擎：schema 解析、引用闭包、试删裁剪、子库构建、复算与复核
  main.py     # FastAPI：/health、/analyze（multipart 上传，zip 下载）
tests/
  make_fixture.py  # 带循环外键、无外键街道编码、试删回滚三类夹具库
  test_engine.py   # 引擎+HTTP 端到端（45 项）
  test_edge.py     # 反向差值/空表/坏文件/真实 uvicorn（16 项）
  test_street.py   # 无外键关联、同街道旁支、试删后对照失衡（42 项）
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

- `sub1_with_workorder_<pk>.db`：保留工单与原差值（大小、方向），已裁到没有可删起点；
- `sub2_without_workorder_<pk>.db`：仅删工单引用闭包，两版计数相等；
- `manifest.json`：原库/两子库计数、差值、各表行数、子库一被裁行
  （`sub1.pruned_rows`）与子库二删除清单（`sub2.deleted_rows`）。

失败返回 HTTP 400/413/500 与 JSON 原因，例如
`{"ok": false, "reason": "两版月报计数原本就相同，无需归因"}`。

无 Docker 时本地运行：

```bash
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 5. 实测记录（本环境）

环境：Python 3.11.2，SQLite 3.40.1；依赖安装于独立 user-base（系统无 pip，
先用 get-pip 引导）。**本机无 Docker 守护进程**，容器构建未在本机执行，
已用真实 `uvicorn` 进程 + 手工 multipart 请求覆盖同一接口路径。

- `tests/test_engine.py`：**45/45 通过**，含循环引用夹具
  （`workorder ↔ dispatch` 互指）、两库外键校验、整库试删裁剪后的不动点、
  手工删除交叉验证、TestClient 上传下载；
- `tests/test_edge.py`：**16/16 通过**，含反向差值（Δ=-3 方向保留）、
  空工单表、损坏文件、未知表、非整数/多行 SQL、真实 uvicorn 进程的
  上传→zip 解压→两库复算→失败请求 reason；
- `tests/test_street.py`：**42/42 通过**，覆盖本次修复的三类缺陷：
  - **无外键关联**：口径表仅以街道编码字符串与工单表 JOIN，表间无外键，
    原实现交不出样库（`候选工单均无法同时满足…`），现正常交付；原库 `C1=2,C2=1`，
    子库一保留 Δ=1，子库二 `C1=C2=0`；反向 SQL 顺序时 Δ=-1 亦保留；
  - **同街道旁支误留**：与命中工单同街道的工单 2 及其 visit/photo
    不再留在子库一或子库二，被裁行记入 `manifest.sub1.pruned_rows`；
  - **试删后对照失衡**：删除工单 2 后差值仍为 -1（前两条通过），但再删目标
    工单 3 后 `C1=1,C2=2` 不等，第三条不通过，试删被完整回滚，工单 2 保留；
    直接检查 `_trial_delete` 返回 `False` 且行集、事务状态均恢复原样；
  - 两库均保留 4 张表、索引与外键，`foreign_key_check` 无违例；
    子库二恰为子库一减去目标工单的受限反向闭包，绝不多删；
    另含 HTTP 无外键上传下载与“无法交付时只返回原因”。
- 随机化不变量校验（400 个随机库，7 组 SQL 对，130 次成功交付）：
  差值大小方向保持、子库二计数相等、目标工单归属、子库二仅删闭包、
  子库一不动点、两库外键完整，**0 例违反**。

复现命令（依赖路径按本机实际调整）：

```bash
PYTHONUSERBASE=/tmp/pyuser PYTHONPATH=/workspace python3 -I tests/test_engine.py
PYTHONUSERBASE=/tmp/pyuser PYTHONPATH=/workspace python3 -I tests/test_edge.py
PYTHONUSERBASE=/tmp/pyuser PYTHONPATH=/workspace python3 -I tests/test_street.py
```

## 6. 前提、限制与余项

- 表必须为单列 INTEGER 主键、外键单列且指向他表主键；复合外键/复合主键/虚表
  会被明确拒绝（与“各表以整数为主键、外键指向主键”的前提一致）；
- 库内无触发器：引擎显式检查 `sqlite_master`，存在触发器即拒绝；
- 大库性能：候选复算为“每工单一次事务内删除+回滚”，工单量大时耗时随
  工单数线性增长；子库一裁剪是行数级的试删扫描（每轮每行三次复算），
  4000 行、约 1000 行待删的库实测约 36 秒；裁剪仅对最终入选的工单执行，
  不会对每个候选都跑一遍。若后续出现十万行级库，需改为按批试删或增量计数；
- 试删是“逐行贪心”：最终得到的是**没有可删起点**的不动点，不保证行数全局最少
  （顺序不同可能留下同样合格但行数略有差异的样库）；这与需求“裁到没有可删的
  起点”一致；
- 上传大小上限默认 200 MiB（`app/main.py:MAX_UPLOAD_BYTES`），临时目录可经
  compose 的 `tmpfs`/`TMPDIR` 调整；
- 待补：在装有 Docker 的环境执行一次 `docker compose up --build` 冒烟
  （Dockerfile 已含 healthcheck 与非 root 用户）；如未来出现多列主键/复合外键
  的脱敏库，需要扩展 schema 模型与闭包算法。
