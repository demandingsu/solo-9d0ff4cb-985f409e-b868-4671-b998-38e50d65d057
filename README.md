# 专色墨称量、核定与分装台（Flask + SQLite）

给印刷打样调墨员使用的单桶专色墨管理网页：登记一桶专色墨的批号、容量、各色目标质量比、
每张称量单的实际入桶克数；只靠加料把配比补到目标，给出**总加料最少**的整数克方案；
按最新实称做合格核定。调配达标后在总览**开分装单**给机台分墨：填单号和各罐净重
（正整数克），首单入账即结束调配，此后停收称量单与桶档修改，总览显示**余墨**；
并发交单按最新余墨**整单扣账**，失败原样回滚；换桶后仍可**凭单号查原桶批号、配比、
各罐净重**，原单重交只返回原单，同号改量/旧桶页面交未入账单一律退回。

## 一、业务规则与实现对应

### 称量 / 加料 / 核定（原有能力，30 个旧测试全保留通过）

| 需求 | 实现位置 | 行为 |
| --- | --- | --- |
| 桶容量 + 各色目标质量比（正整数） | `app.py::parse_bucket_form`、`db.save_bucket` | 容量与每个比例均校验为正整数，至少两色，颜色名不可重复；存 `bucket` 表（单行 id=1） |
| 桶批号 | `bucket.batch_no`、建桶表单 `batch_no` | 可手填；留空自动生成 `B<时间戳><随机>`；分装单快照批号 |
| 按称量单号登记实际入桶整数克数 | `app.py::ticket_create`、`db.add_ticket` | 每色克数为非负整数，整单至少一色 > 0；单据存 `tickets` 表，单号 `UNIQUE` |
| 同号原单只记一次，同号改量就退回 | `db.DuplicateTicketError` + 路由前置查重 | 单号已存在时**无论克数是否变化整单退回**，闪消息附原单内容 |
| 只加料补到目标配比，总加料最少 | `domain.compute_additions` | 见下方算法；输出每色补料克数与合计 |
| 补完超容量提示停配 | `domain.build_suggestion` | 达标总量 > 容量时页面显示红色“⛔ 停配提示” |
| 入桶即超容量 | `app.py::ticket_create` | 入桶后累计总量 > 容量的称量单整单退回，不写库 |
| 按最新实称核定 / 自动撤标 | `app.py::verify`、`db.certified_state` | 见文末状态机；核定写 `verifications` 表留痕 |

### 分装 / 余墨 / 并发 / 换桶（本次新增）

| 需求 | 实现位置 | 行为 |
| --- | --- | --- |
| 总览开分装单：单号 + 各罐净重（正整数克） | `templates/index.html` 分装表单、`app.py::parse_dispense_request` | 动态加罐/删罐；每罐必须正整数，至少一罐；空行忽略，0/负数/小数/非数字整单退回 |
| 首单入账一并结束调配 | `db.register_dispense_order`（`prior_count==0` 分支） | 同一短事务内：三项确认 → 写单 → `bucket.phase='dispensing'`/`closed_at`/实称快照 → 写一条“首单确认合格”核定 → 提交 |
| 首单确认①配比达标 | `domain.ratio_match` | 有料、各色 `wᵢ/rᵢ` 用 `Fraction` 精确相等；不达标退回，**不结束调配、不扣账** |
| 首单确认②墨量未超桶容量 | `register_dispense_order` | 当时实称总量 > 容量 → 退回（`code=over_capacity`），阶段照旧 |
| 首单确认③够装 | `register_dispense_order` | 本单各罐合计 > 当时实称总量 → “不够装”整单退回，不结束调配 |
| 此后停收称量单 | `app.py::ticket_create` 前置 `db.mixing_closed` | POST `/tickets` 直接退回，不解析/不写库；表单也替换为 🔒 提示 |
| 此后停收桶档修改 | `app.py::bucket_setup` 前置锁 | 已关账时任何 `/bucket/setup` POST 退回（容量/配比/批号都不改），页面隐藏编辑表单 |
| 总览显示余墨 | `app.py::index`、`index.html#remaining` | 余墨 = 最新实称总量 − 当前桶世代已入账分装合计；台账逐单显示扣后余额 |
| 两人同时交单按最新余墨整单扣账 | `db.register_dispense_order` | 每单 `BEGIN IMMEDIATE` 拿 SQLite 写锁，锁内重读实称/已扣/余墨再校验；余额不足抛 `InsufficientRemaining` → `ROLLBACK`，**不做部分扣减** |
| 登记失败余墨与调配状态照旧 | 同上 | 所有失败路径（校验/冲突/旧桶/忙）都回滚；首单失败不会把 `phase` 改成结束 |
| 原单照样重交只返回原单 | 单号全局 `UNIQUE` + 内容比对 | 同号且各罐净重序列完全相同 → `outcome=replayed`：表单直接 200 渲染原单页，JSON 返回 `200 + replayed/duplicate:true`，不重复扣账 |
| 改了内容沿用单号就退回 | `db.OrderNoConflict`（HTTP 409） | 同号但罐重不同 → 整单退回，闪消息/JSON 附原单各罐净重，请另开新号 |
| 换桶后凭单号查原桶批号/配比/各罐净重 | `dispense_orders` 快照表 + `dispense_detail.html` | 落账时快照 `batch_no/ratios_json/capacity/mixed_weights/can_weights`；换桶 `reset_all` **不删该表**；`GET /dispense/<单号>` 等多路径可查，页面与 `/api/...` JSON 均可 |
| 从旧桶页面交未入账单退回 | epoch + generation + batch_no + bucket_id 令牌 | 分装表单隐藏当前桶 `bucket_epoch/bucket_generation/bucket_id/batch_no`；新单（单号不存在）先比对令牌，任一可识别令牌对不上即 `StaleBucketPage` 退回且不落库；已入账原单的重交优先按“原单返回”处理 |
| 换桶 | `POST /bucket/reset`（另有 `/bucket/change` 别名） | 清当前 bucket/tickets/verifications，保留分装档案；下次建档领取新 `generation` 与 `epoch`。直接用新批号再次保存桶档案也会旋转 epoch/世代 |

#### 接口与命名兼容

- 页面表单：`POST /dispense`，字段 `order_no` + 重复的 `can_weight`（动态罐行），
  并带隐藏令牌。查询：`GET /dispense/<order_no>` 或 `GET /dispense/lookup?order_no=...`。
- JSON API：`POST /api/dispense`，体例如
  `{"order_no":"FZ-1","can_weights":[200,300]}`；查询 `GET /api/dispense/<order_no>`。
  成功 `201`、原单重交 `200(replayed:true)`、冲突/余额不足/旧桶 `409`、
  首单条件不满足 `422`、校验失败 `400`、查无此单 `404`。
- 为兼容不同调用约定，同一处理函数还挂了 `/dispense_orders`、`/pack_orders`、
  `/orders`、`/pack`、`/dispensing_orders`、`/fenzhuang` 等路径，
  支持 `order_no/pack_no/bill_no/...` 与 `can_weights/cans/net_weights/...`
  （数组、对象数组、`can_1` 下标字段、逗号字符串等）常见字段写法。

### 最少加料算法（整数克、只加料）

设目标比 rᵢ、当前实称 wᵢ，达标状态为 q·rᵢ。

1. 只加料 ⇒ q·rᵢ ≥ wᵢ 对所有色成立 ⇒ q ≥ max(wᵢ/rᵢ)；
2. 克数必须为整数。令 g = gcd(所有 rᵢ)，则 q 只能取 1/g 的整数倍；
3. 取满足条件的最小 q（代码里直接用整数运算 `k = max ceil(wᵢ·g/rᵢ)`，q = k/g），
   补料 = q·rᵢ − wᵢ。此方案在“只加料”约束下总加料天然最小，测试中含穷举验证。
   全程用 `fractions.Fraction` 做配比比较，无浮点误差。

### 合格标志的状态机

- 未核定 → 未合格；
- 人工核定通过 → 合格（记录核定时最大单据 ID 与实称快照）；
- 之后任何时刻按**最新实称 + 当前桶参数**复核：不达标 → 自动撤标并落库；
- 撤标后只有再次人工核定通过才能恢复合格；
- 首张分装单的三项确认通过时，会额外写一条“首单确认合格”的核定留痕，随后账实冻结。

## 二、目录结构

```
.
├── Dockerfile              # python:3.11-slim + gunicorn（2 worker）
├── docker-compose.yml      # 一条命令起网页，SQLite 挂命名卷 ink_data
├── requirements.txt
├── scripts/
│   └── http_trial.py       # 对真实 gunicorn/Compose 服务跑并发+换桶试跑（22 项断言）
├── ink_app/
│   ├── app.py              # Flask 工厂、路由、表单/JSON 解析、闪消息、多路径别名
│   ├── db.py               # SQLite 表结构/迁移、数据访问、并发安全分装登记
│   ├── domain.py           # 纯计算：最少加料、合格判定、罐净重解析、余墨
│   ├── wsgi.py             # gunicorn 入口
│   ├── templates/          # base / setup / index / dispense_detail
│   └── static/style.css
└── tests/
    ├── test_domain.py      # 算法与判定单元测试（含穷举最小性验证）
    ├── test_app.py         # 原有 Flask 端到端测试（30 个，全部保留通过）
    └── test_dispense.py    # 分装/余墨/锁/幂等/旧桶/查询/线程并发（24 个）
```

数据表：

- `bucket`：单行当前桶，新增 `batch_no / epoch / generation / phase / closed_at /
  final_weights_json / final_total_grams`（旧库启动时自动 `ALTER TABLE` 补列并回填 epoch、批号）；
- `tickets`、`verifications`：同原设计；
- `dispense_orders`：分装单档案，`order_no UNIQUE`，快照原桶批号/世代/epoch/配比/容量/
  当时实称/各罐净重；**换桶不清空**；
- `meta`：桶世代计数器。

## 三、启动方式

### Docker Compose（推荐，在有 Docker 的机器上）

```bash
docker compose up -d --build
# 打开 http://localhost:5000
docker compose logs -f
docker compose down          # 停止；数据保留在命名卷 ink_data
```

数据库位于容器内 `/data/ink.db`，通过命名卷 `ink_data` 持久化；
生产环境请修改 `docker-compose.yml` 中的 `SECRET_KEY`。

### 本地直接运行（无 Docker 时，本次自测使用）

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 60 ink_app.wsgi:app
# 另一个终端跑真实 HTTP 并发试跑：
python scripts/http_trial.py http://127.0.0.1:5000
```

SQLite 文件默认在 `instance/ink.db`，可用环境变量 `INK_DB_PATH` 覆盖。

## 四、自测方法与实测结果

```bash
python -m unittest discover -s tests
```

本次交付在本机实际运行结果：**54 个测试全部通过（Ran 54 tests … OK）**，其中
原有 **30** 个测试无修改全部通过，新增 `tests/test_dispense.py` **24** 个
（含通过 Flask 测试客户端 + 线程屏障的真实并发用例）。

另外用与 `Dockerfile` 相同的入口和 worker 数（`gunicorn --workers 2
ink_app.wsgi:app`，文件 SQLite + 临时 `INK_DB_PATH`）起真实双 worker 服务做并发试跑，
`scripts/http_trial.py` 实测 **22 项断言全部通过（22 通过，0 失败）**。该脚本只用标准库、
开头自动换桶并使用随机单号前缀，可对同一服务重复执行（`python scripts/http_trial.py
http://127.0.0.1:5000`）。此外也按容器原命令 `gunicorn --bind 0.0.0.0:5000 --workers 2
… ink_app.wsgi:app` 在 5000 端口实际起服，`GET /` 返回 200 后正常停止。关键实测摘录：

```
-- 并发超卖：12 人同时各交 60g（共请 720g，余墨 500g）--
   状态码分布: {201: 8, 409: 4}
  ✅ 恰好 8 单成功（8×60=480≤500）
  ✅ 恰好 4 单因最新余墨不足整单退回
  ✅ 最终余墨 20g；总览显示“余墨 20 g”和“调配已结束”

-- 并发同号原单：6 个相同 DUP-1（10g+10g=20g，恰够余墨）--
   状态码: [200, 200, 200, 200, 200, 201]
  ✅ 1 单 201 新建，5 单 200 原单重交(replayed)，只 1 行、不重复扣账

-- 锁：首单后称量单/桶档修改均退回 --
  ✅ 称量单停收提示   ✅ 桶档修改锁定提示

-- 换桶后旧桶页面提交尚未入账的 STALE-1 --
  ✅ 页面提示旧桶退回；GET /api/dispense/STALE-1 -> 404（未登记）

-- 换桶后凭单号查 CONC-00 / DUP-1 --
  ✅ 200 且含旧批号 B-HTTP-1、配比“青 3 ∶ 品红 2”、各罐 60g
  ✅ 别名 /orders/DUP-1 可查

-- 新桶 NEW-1 [120,80]=200 --
  ✅ 整单入账、余墨 0g；照样重交 200 返回原单；改 121g 沿用单号退回
```

数据库层另跑过一组线程并发核对（`BEGIN IMMEDIATE`）：

- 10 × 100g 抢 500g ⇒ 恰好 5 成 5 败，余墨 0；
- 8 个相同单号同内容 ⇒ 1 created + 7 replayed，仅 1 行；
- 6 个同号但内容分两派 ⇒ 1 created、2 同内容 replay、3 改内容 conflict；
- 2 × 300g 抢 500g ⇒ 1 成 1 败，余墨 200；
- 首单配比不达标 / 不够装 / 超容量 ⇒ 均退回且 `phase` 仍为 `mixing`。

### 原有场景回归（30 个旧测试）

超称（单张超、累计超、停配提示、超容量核定不合格）、同号同量/改量退回、
迟到单据自动撤标、补料后须重新核定、容量改小撤标、三色桶最少加料穷举、
非法输入拦截、清空换桶等全部照旧通过。

## 五、约定与余项

1. 应用按需求跟踪**当前一桶**专色墨；“换桶”= 清空当前桶/称量/核定后重新建档，
   但 `dispense_orders` 分装档案跨换桶保留，这是“凭单号查原桶”的数据基础。
   另提供 `db.purge_all()`（别名 `clear_all/wipe_all`）供需要连分装档案一起彻底清空时使用。
2. 称量单仍是**追加式流水**，页面不提供删改入口；分装单同样不提供编辑入口，
   同号改内容一律退回，请另开新单号。
3. 并发模型为“SQLite + `BEGIN IMMEDIATE` 短事务 + `busy_timeout=15s`”，
   双 gunicorn worker 下已用真实并发试跑验证不会超扣/重账；单机内网多终端足够。
   若未来写并发进一步上升或要多机部署，可把同一套条件扣账迁移到 Postgres
   （`UPDATE ... WHERE remaining >= ?` 或行锁）。
4. **本沙箱环境没有 Docker CLI/daemon**（`docker: command not found`、
   无 `/var/run/docker.sock`、容器内 `CapEff=0` 且无 user namespace，无法起 dockerd），
   因此没能在这里字面执行 `docker compose up -d --build`。已完成的等效核对：
   `docker-compose.yml` 通过 YAML 解析、服务/卷/健康检查字段完整；
   依赖按 `requirements.txt` 实际安装；并用 Dockerfile 中的同一条 gunicorn 命令
   （`--bind 0.0.0.0:5000 --workers 2 ink_app.wsgi:app`，`INK_DB_PATH` 指向数据文件）
   实际起服、curl 健康检查 200、跑完 22 项 HTTP 试跑。
   **待有 Docker 的环境执行一次 `docker compose up -d --build` 做最终复验。**
5. 为兼容可能的自动化调用，分装接口同时提供表单与 JSON、多组路径/字段别名；
   若后续拿到正式接口命名规范，可收敛别名并补对应的鉴权/操作人字段。
6. 当前无登录/鉴权，定位为车间内网可信终端；如需审计到人，可在分装单上加
   `operator` 列并接入简单登录。
