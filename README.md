# 专色墨称量与核定台（Flask + SQLite）

给印刷打样调墨员使用的单桶专色墨管理网页：登记一桶专色墨的容量、各色目标质量比、
每张称量单的实际入桶克数；只靠加料把配比补到目标，给出**总加料最少**的整数克方案；
按最新实称做合格核定，核定后的迟到单据自动入账、不达标自动撤标。

## 一、业务规则与实现对应

| 需求 | 实现位置 | 行为 |
| --- | --- | --- |
| 桶容量 + 各色目标质量比（正整数） | `app.py::parse_bucket_form`、`db.save_bucket` | 容量与每个比例均校验为正整数，至少两色，颜色名不可重复；存 `bucket` 表（单行） |
| 按称量单号登记实际入桶整数克数 | `app.py::ticket_create`、`db.add_ticket` | 每色克数为非负整数，整单至少一色 > 0；单据存 `tickets` 表，单号 `UNIQUE` |
| 同号原单只记一次，同号改量就退回 | `db.DuplicateTicketError` + 路由前置查重 | 单号已存在时**无论克数是否变化整单退回**，闪消息附原单内容，提示另开新单号 |
| 只加料补到目标配比，总加料最少 | `domain.compute_additions` | 见下方算法；输出每色补料克数与合计 |
| 补完超容量提示停配 | `domain.build_suggestion` | 达标总量 > 容量时页面显示红色"⛔ 停配提示" |
| 入桶即超容量 | `app.py::ticket_create` | 入桶后累计总量 > 容量的称量单整单退回，不写库 |
| 按最新实称核定 | `app.py::verify`、`domain.is_compliant` | 桶里有料 **且** 配比与目标完全一致 **且** 未超容量才合格；核定写 `verifications` 表留痕 |
| 核定后补登的单也要入账 | `tickets` 表正常登记 | 核定不锁账，新单随时可入 |
| 不再达标自动撤标 | `db.certified_state` | 核定记录置 `revoked=1`，横幅显示"已自动撤标"；撤标后即使补回达标也**不会自动恢复**，必须人工重新核定 |
| 补好后须重新核定 | 同上 | 新核定生成新记录，合格标志才恢复 |

### 最少加料算法（整数克、只加料）

设目标比 rᵢ、当前实称 wᵢ，达标状态为 q·rᵢ。

1. 只加料 ⇒ q·rᵢ ≥ wᵢ 对所有色成立 ⇒ q ≥ max(wᵢ/rᵢ)；
2. 克数必须为整数。令 g = gcd(所有 rᵢ)，则 q 只能取 1/g 的整数倍；
3. 取满足条件的最小 q（代码里直接用整数运算 `k = max ceil(wᵢ·g/rᵢ)`，q = k/g），
   补料 = q·rᵢ − wᵢ。此方案在"只加料"约束下总加料天然最小，测试中含穷举验证。
   全程用 `fractions.Fraction` 做配比比较，无浮点误差。

### 合格标志的状态机

- 未核定 → 未合格；
- 人工核定通过 → 合格（记录核定时最大单据 ID 与实称快照）；
- 之后任何时刻按**最新实称 + 当前桶参数**复核：不达标（迟到单据破坏配比/超容量，
  或容量被改小）→ 自动撤标并落库；
- 撤标后只有再次人工核定通过才能恢复合格；核定后补登的单据若仍达标，标志保留并注明。

## 二、目录结构

```
.
├── Dockerfile              # python:3.11-slim + gunicorn
├── docker-compose.yml      # 一条命令起网页，SQLite 挂命名卷 ink_data
├── requirements.txt
├── ink_app/
│   ├── app.py              # Flask 工厂、路由、表单校验、闪消息
│   ├── db.py               # SQLite 表结构与数据访问、撤标状态机
│   ├── domain.py           # 纯计算：最少加料、合格判定、停配提示
│   ├── wsgi.py             # gunicorn 入口
│   ├── templates/          # base / setup / index 三个模板
│   └── static/style.css
└── tests/
    ├── test_domain.py      # 算法与判定单元测试（含穷举最小性验证）
    └── test_app.py         # Flask 端到端测试（超称/重交/迟到入账等）
```

数据表：`bucket`（单桶档案）、`tickets`（称量单，单号唯一）、
`verifications`（核定记录，含 `last_ticket_id` 与 `revoked` 标记）。

## 三、启动方式

### Docker Compose（推荐）

```bash
docker compose up -d --build
# 打开 http://localhost:5000
docker compose logs -f
docker compose down          # 停止；数据保留在命名卷 ink_data
```

数据库位于容器内 `/data/ink.db`，通过命名卷 `ink_data` 持久化；
生产环境请修改 `docker-compose.yml` 中的 `SECRET_KEY`。

### 本地直接运行（无 Docker 时）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app ink_app.wsgi run --host 0.0.0.0 --port 5000
# 或：gunicorn --bind 0.0.0.0:5000 ink_app.wsgi:app
```

SQLite 文件默认在 `instance/ink.db`，可用环境变量 `INK_DB_PATH` 覆盖。

## 四、自测方法与结果

```bash
python -m unittest discover -s tests -v
```

本次交付在本机实际运行结果：**30 个测试全部通过（OK，Ran 30 tests）**，并额外用
gunicorn 起真实服务做了 curl 端到端冒烟。覆盖的必测场景：

- **超称**：① 入桶即超容量的单据整单退回且不写库（单张超、累计超两种）；
  ② 达标补料会突破容量时显示"⛔ 停配提示"；③ 超容量状态下核定不合格。
- **重交**：同号同量、同号改量均整单退回，台账仍只有原单，闪消息回显原单克数。
- **迟到入账**：核定合格后补登破坏配比的单据 → 自动撤标落库；补料恢复达标后不自动
  恢复合格，人工重新核定才恢复；补登后仍达标则保留合格标志。
- 另含：容量改小触发撤标、三色桶最少加料穷举验证、非整数/负数/重复颜色名等非法输入
  拦截、清空换桶。

冒烟实测摘录（容量 1000g、青 3∶品红 2）：

```
T001 青300/品红100 入账 → 建议"最少共补 100g"（补品红到200）
T001 改量重交        → 退回：同号单据只记一次（回显原单）
T002 青600/品红1     → 退回：入桶后 1001g 超过容量 1000g
T003 品红100 → 核定合格
T004 青30           → 自动撤标（实际配比与目标比不一致）
T005 品红20         → 实称恢复达标但仍撤标，提示重新核定
重新核定             → 合格
容量改 520（实称550）→ 停配提示 + 自动撤标
```

## 五、约定与余项

1. 应用按需求跟踪**一桶**专色墨；换桶用页面底部"清空全部数据"后重新建档。
2. 称量单是**追加式流水**，页面不提供删改入口（同号改量请新开单号），保证留痕；
   桶容量/目标比允许修改，修改后按新参数复核，已登记单据的原始克数不变。
3. 调墨员按加料建议实际补料后，需把补料克数作为一张新称量单登记，再点"按最新实称核定"。
4. 本机交付环境没有 Docker CLI，`Dockerfile`/`docker-compose.yml` 已通过 YAML 解析与
   指令检查，但**未在本机实际构建镜像**，需在有 Docker 的环境执行一次
   `docker compose up -d --build` 复验。
5. 当前为单机内网单用户场景，gunicorn 两 worker + 文件 SQLite 足够；如后续多终端并发
   登记，可再评估写队列或换 Postgres。
