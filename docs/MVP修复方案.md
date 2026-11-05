# MVP 修复功能方案（基于第四轮测试缺陷台账）

- 编制日期：2026-11-05
- 编制依据：[MVP测试轮次汇总.md](MVP测试轮次汇总.md)（原第四轮测试报告，16 条现行缺陷：P0 7 / P1 8 / P2 1，另含 EX-D01 风险注记、DEF-R1-03 一轮遗留项、C 类 4 组阻塞、PM-09 试点缺口）
- 实施位置：**主源码 `backend/`**。被测副本 `releases/MVP-v0.1.0-20261104` 继续冻结留档，不作任何修改；第五轮回归直接针对主源码服务执行（`tools/s2_lib.py` 自动拉起 `backend.server`）。
- 技术约束：后端仅用 Python 标准库；SQLite 单库；API 信封 `{ok,data}` / `{ok:false,code,error}`；审计与业务写入必须同事务（不得为修缺陷破坏该原则）。

## 一、修复原则

1. **失败要"显式拒绝"，不要"静默吞掉"**：所有安全/合规前置校验（授权、凭证、病区、签名）失败一律返回明确错误码 + `audit` 事件，不降级放行。
2. **鉴权先于语义**：同一端点内，"凭证/身份是否有效"永远先于"资源状态是否合法"判断，避免用 400 泄露内部状态。
3. **一切 HTTP 异常必须落为 HTTP 响应**：任何在 `_handle` 内抛出的 `ApiError` 都不允许逃逸到 socketserver 层。
4. **排程分配确定性**：护士分配规则必须可解释、可复现（按病区匹配），消除"全院轮转"造成的归属歧义。
5. **口径同步与代码修复同批交付**：凡修复改变对外语义（统计位置、分配结果、生效日行为），对应测试用例断言在同一批次内更新，避免第五轮回归出现"修复后用例仍失败"的假阳性。

## 二、总览：缺陷 → 修复项 → 批次

| # | 缺陷（关联用例） | 等级 | 修复项 | 根因所在 | 批次 |
| --- | --- | --- | --- | --- | --- |
| 13 | 超限/异常载荷连接被直接关闭（EX-05/06/07、SEC-11；含 RF-12 超长半边） | P0/P1 | F1 载荷异常统一落 HTTP | `server.py` `_handle` | 一 |
| 14 | /api/records、/api/drafts 越病区读取（PM-04、SEC-04） | P0 | F2 列表病区过滤 + 作废归属校验 | `core/records.py` | 一 |
| 9 | 凭证重放/重用返回 400 而非 403（RC-05、SEC-03） | P0 | F3 confirm 校验顺序 + 事务原子化 | `core/records.py` | 一 |
| — | EX-D01 双凭证并发重复登记（风险注记） | P0 | F3（同函数一并原子化） | `core/records.py` | 一 |
| 4 | 撤回授权后确认链路未拦截（AU-07） | P0 | F4 授权状态前置校验 | `core/records.py` | 一 |
| 1 | 本人跨病区排程在看板不可见（SC-08）+ DEF-R1-03 全院轮转分配 | P0 | F5 排程按病区分配 + 看板口径固化 | `core/scheduling.py` | 一 |
| 5 | RF-D01：模糊变体跨过否定词窗口（RF-04） | P0 | F6 变体通道窗口内否定判定 | `core/redflag.py` | 一 |
| 2 | 跨护士调整排程未校验归属（SC-09） | P1 | F7 reorder 归属校验 | `core/scheduling.py` | 二 |
| 3 | 短信触达未计入独立统计（AU-05） | P0 | F8 短信独立指标（顶层 + 按日） | `core/records.py` + `server.py` | 二 |
| 6 | RF-D02：正则通道未做否定判定（RF-06） | P1 | F9 正则分句 + 否定判定 | `core/redflag.py` | 二 |
| 7 | 规则文件无签名校验（RF-11） | P1 | F10 规则分离签名验签（fail-closed） | `core/redflag.py` + 新签名工具 | 二 |
| 10 | 重复签发使模板版本前进（TK-08） | P1 | F11 禁止重复签发 | `core/templates.py` | 二 |
| 11 | 补排后 manual_reason 未清理（TK-09） | P1 | F12 补排清理 + stale 语义收敛 | `core/scheduling.py` | 二 |
| 12 | 未知路由先 404 未先鉴权（EX-04） | P1 | F13 鉴权前置统一判断 | `server.py` | 二 |
| 15 | 导入未校验 synthetic 合成标记（SEC-12） | P1 | F14 synthetic 字段全链路 | `core/contract.py` + `core/db.py` | 二 |
| 16 | his_import 超时模拟双层事务嵌套 → 500（SC-05） | P1 | F15 去除 handler 外层事务 | `server.py` + `core/contract.py` | 二 |
| 8 | 空陈述/超长文本处理不当（RF-12） | P2 | F16 超长随 F1 闭环；空陈述定性复核 + 用例口径包 | 用例 + `tools/` | 三 |

批次三（口径同步与专项补齐）另含：用例口径同步包（AU-05/SC-08/TK-09/RF-12、`s2_lib.DATE` 与 fixture `exported_at` 动态化）、EX-D01 并发回归加固验证、C 类阻塞补齐计划、PM-09 统一认证接入规划。

## 三、逐条修复方案

### F1（#13，P0）：载荷异常统一落 HTTP —— EX-05/06/07、SEC-11、RF-12 超长半边

**根因定位**：`backend/server.py` `_handle()`（约 358–422 行）。结构缺陷：**请求体解析段的 try 块把 `ApiError` 原样 re-raise，而"`ApiError` → HTTP 响应"的 `except ApiError as exc: self._send(...)` 只包住了后面的路由分发段**。因此凡在鉴权前的载荷校验失败（413 超长 / 400 bad_json / 400 too_deep / 400 bad_body），异常直接逃逸到 `socketserver` 层 → 连接被关闭、无 HTTP 响应、stderr 打印完整堆栈（与 SEC-11 现象完全吻合）。叠加因素：413 在 `self.rfile.read(length)` **之前**抛出时请求体尚未读取，服务端带未读数据关闭连接引发 TCP RST 竞态，客户端连错误响应也收不到。

**修复思路**：
1. 结构修复（主修）：把"请求体解析"并入与路由分发相同的 try/except 结构，顶层统一 `except ApiError as exc: self._send(exc.status, ...)`；确保 `_handle` 内任何 `ApiError` 都落为 HTTP 响应。
2. 超长报文体面拒收：发送 413 前先**排空（drain）**已声明长度的剩余请求体（分块读取丢弃，上限保护如 16 MB，超上限直接放弃并置 `close_connection`），错误响应显式携带 `Connection: close`，消除 RST 竞态。
3. 日志收口：4xx 只记录请求摘要与错误码，不打印堆栈；500 保留堆栈但经 `security.sanitize` 脱敏后输出。注：第三/四轮实测均未在采集日志中发现堆栈（SEC-11 备注"日志含堆栈=False"，第三轮报告第六节），但代码路径存在 `traceback.print_exc()` 与 socketserver `handle_error` 双输出通道（后者直接进 stderr 控制台，未入应用日志）；本项将异常日志统一收口到应用日志口径，并满足 SEC-09（日志不落敏感原文）。
4. RF-12 的"超长文本连接被关闭"半边（525 KB 文本 → 应 413）随本项自然闭环，不单独立项。

**改动面**：仅 `backend/server.py`（`_handle`、`_send`）。
**验收标准**：`run_s2.py --only EX-05,EX-06,EX-07` 与 `run_s3.py --only SEC-11` 全 PASS：坏 JSON→400 bad_json、300KB→413 body_too_large、40 层嵌套→400 too_deep，三者均有完整 HTTP 响应且无堆栈打印；超长陈述（约 525KB）→ 413 而非断连。
**风险与注意**：drain 上限要防恶意超大 Content-Length 拖垮服务；`Connection: close` 后不得复用该连接处理下一请求。

### F2（#14，P0）：列表病区过滤 + 作废归属校验 —— PM-04、SEC-04

**根因定位**：`backend/core/records.py` `list_records()` / `list_drafts()`（约 172–197 行）**完全没有按角色/病区过滤**，任何登录用户（含护士）拿到全院登记与草稿；`discard_draft()`（约 160–169 行）同样无归属校验，护士可作废他人病区草稿。`server.py` 对应 handler（约 191–213 行）未传 `ctx.user`。

**修复思路**：
1. `list_records` / `list_drafts` 增加 `actor` 入参并按权限矩阵过滤：**nurse → 本人病区患者 ∪ 本人名下排程关联的记录/草稿（与 `/api/schedule` 看板 `s.nurse_id=? OR p.department=?` 同口径）；doctor / quality / manager → 全院**（与《设计方案》§7 权限矩阵一致）。
2. `discard_draft` 增加归属校验：nurse 仅可作废"本人病区或本人名下"的草稿，越权 → `PermissionError`（403）并写 `audit`（`draft/escalation` 域，越权事件）。
3. handler 侧传入 `ctx.user`，不再传裸参数。

**改动面**：`core/records.py`（三个函数签名与 SQL）、`server.py`（三处 handler）。
**验收标准**：`run_s3.py --only PM-04,SEC-04` PASS：二病区护士 `/api/records`、`/api/drafts` 仅见二病区/本人数据，一病区住院号不出现；`run_round1.py` 中 RC 域既有用例无回归（医生全院可见行为不变）。
**注意**：过滤口径必须与 F5 后的看板口径一致，避免"看板看得见、列表看不见"的裂缝；SQL 关联需经 schedules/patients join，注意查询性能（当前数据量小，暂不加索引，留观察项）。

### F3（#9，P0；含 EX-D01）：confirm 校验顺序 + 事务原子化 —— RC-05、SEC-03

**根因定位**：`backend/core/records.py` `confirm()`（约 116–157 行）两个问题：
1. **校验顺序颠倒**：先查草稿状态（已确认 → `ValueError("草稿已确认或已作废")` → 400 invalid），后 `security.verify_stepup()`。凭证重放时先命中前者，返回 400 而非 403（nonce 防重放在 `security.py` `verify_stepup()` 约 140–141 行，靠 `stepup_nononces` 唯一键 + `IntegrityError → PermissionError`）。
2. **草稿状态在事务外校验**（119–123 行的 SELECT 在 `with db.transaction` 之外）：并发场景下两个请求都能通过状态检查后各自落库 → EX-D01（双凭证并发重复登记）的根源；第四轮未复现仅因时序，路径未变、风险不解除。

**修复思路**：
1. 调整顺序为：草稿存在性（404）→ **`verify_stepup`（防重放/换人/换用途 → 403）** → 草稿状态检查（400）→ digest 绑定校验 → 事务内写入。`canonical_digest` 只绑定 `fields`，与草稿状态无关，顺序调整不影响摘要校验。
2. 状态检查移入事务并改为**条件更新抢占**：`UPDATE drafts SET status='confirmed' WHERE id=? AND status='draft'`，`rowcount != 1` → 回滚并返回 409/400（"草稿已被处理"），使并发确认天然原子化，EX-D01 路径关闭。
3. 审计事件（`record.confirmed`）保持在同一事务内写入。

**改动面**：仅 `core/records.py` `confirm()`。
**验收标准**：`run_s2.py --only RC-05` PASS（重放返回 403，文案含"凭证/step-up 不匹配"）；`run_s3.py --only SEC-03` PASS；EX-D01 并发加固：脚本侧并发 8 次同 `draft_id`（含双凭证）→ 落库登记恒为 1 条、状态恒 confirmed，其余响应为 403/409；`run_round1.py` RC-04/RC-06/EX-02/EX-03 无回归；**第三轮基线不回退**：SEC-02（凭证五类失效场景）、SEC-05（审计链验链）、SEC-06（留痕失败即回滚并 503）——条件更新抢占不得破坏"审计与业务同事务、留痕失败即 503"语义。

### F4（#4，P0）：授权状态前置校验 —— AU-07

**根因定位**：`backend/core/records.py`：`confirm()`（约 116–157 行）与 `check_and_draft()`（约 60–113 行）**均不校验排程关联患者的 `auth_status`**。撤回授权链路已把患者置 `auth_status='撤回'`、排程置 shelved，但确认/生成草稿入口无拦截，撤回后仍可 200 完成登记。

**修复思路**：
1. `confirm()` 在 `_load_schedule` 后、事务写入前校验患者 `auth_status == '已授权'`，否则 `PermissionError`（403，文案与 `call.denied_unauthorized` 口径一致）。
2. `check_and_draft()` 同样增加该前置校验（红旗中断与草稿生成同属"使用授权"的动作）。
3. 两处拒绝路径写 `audit`（authorization 域，事件如 `auth.denied_revoked`），保持审计与拒绝同事务。

**改动面**：`core/records.py` 两个函数。
**验收标准**：`run_s2.py --only AU-07` PASS：撤回后确认返回 403、无新登记；`run_round1.py` AU-06（停排/录音标记）无回归；红旗链路对"未授权"患者不可生成草稿（可用 AU 域补充断言）；**第三轮基线不回退**：SEC-07（授权撤回后立即停呼停训）、SEC-08（草稿生成本地闭环）。

### F5（#1，P0 + DEF-R1-03）：排程按病区分配 + 看板口径固化 —— SC-08

**根因定位**：`backend/core/scheduling.py` `run()`（约 33–114 行）。第 97 行护士分配为 `nurses[idx % len(nurses)]` —— **全院所有在职护士轮转，不匹配患者病区**（DEF-R1-03）。二病区患者 0106 会因行序被排给一病区护士（形成"本人跨病区排程"）或二病区护士，归属不确定。看板 `board()`（117–136 行）过滤 `s.nurse_id=? OR p.department=?`（本人排程 ∪ 本病区患者）本身符合权限矩阵；SC-08 失败的实质是**分配规则偶然产生的跨病区归属**令 0106 落在他院护士名下，n1 按规则不可见 —— 产品语义歧义而非看板过滤写错。

**修复思路**：
1. 分配按病区匹配：护士按 `ward` 分桶；患者 `p.department` 对应桶非空 → 桶内轮转（保持批次内均衡与确定性）；**桶为空 → 转人工**（`manual_reason='本病区无可用随访护士，转人工'`），不再静默跨病区。
2. 看板过滤保持 `本人排程 ∪ 本病区患者` 并在代码注释与《设计方案》中固化该口径；`deferral/escalation` 域审计不变。
3. 用例口径同步（批次三执行）：SC-08 期望更新为"0106 由二病区护士承接、二病区护士可见、一病区护士不可见"；"本人跨病区排程可见性"保留原意，改为**显式构造**（测试夹具直接注入一条 n1 名下的跨病区排程）验证 `nurse_id` 可见性，不再依赖轮转偶然性。

**改动面**：`core/scheduling.py` `run()` 分配段；`tools/run_s2.py` SC-08 断言与夹具。
**验收标准**：SC-08 修正口径后 PASS（含显式跨病区排程可见、他院不可见、手机号 3+4+2 脱敏不变）；`run_round1.py` SC-01/SC-03/SC-04 无回归（SC-01 计数语义不变：created=6、excluded=2、manual=2）；所有排程的 `schedules.nurse_id.ward == patients.department`（新增脚本断言或 SQL 抽查）。
**拒绝的替代方案**：保留全院轮转 + 用例固定轮转序 —— 会使产品行为继续不可解释，且 SC-08 永远是"碰运气"，不采纳。

### F6（#5，P0）：变体通道窗口内否定判定 —— RF-D01 / RF-04

**根因定位**：`backend/core/redflag.py`。模糊变体匹配 `_best_variant_span()`（约 59–75 行）+ 命中判定（约 111–127 行）。现有否定检查有三条：窗口以否定词开头、否定词出现在**窗口起点前 6 字符内**（`_negated()`，约 42–44 行）、极性冲突（变体含否定词而窗口不含）。**缺第四条：否定词出现在匹配窗口内部**。方言阴性句（如"伤口没有出水"）与变体（如"伤口出水"）模糊匹配成高分类似窗口"伤口没有出"，否定词"没"位于窗口内部、窗口起点之前无否定词 → 误报 RF-ORTHO-03。

**修复思路**：
1. 增加第四条判定：`窗口内含否定词 且 变体本身不含否定词 → 视为否定，跳过命中`（与现有第三条极性冲突互为镜像，合并为一个极性一致函数）。
2. 不改变现有窗口打分与阈值（0.6），避免误伤真阳性；方言变体表本身不增删（语料补充归 RF-10 专项）。
3. 补充单测覆盖四类极性组合：正-正（命中）、变体否定-窗口肯定（现有冲突规则）、变体肯定-窗口否定（本修复）、双否定（命中，如"伤口没长好"属真实红旗表述）。

**改动面**：仅 `core/redflag.py`。
**验收标准**：`run_s2.py --only RF-04` PASS；`run_round1.py` RF-01/RF-02/RF-03/RF-05/RF-07/RF-08 无回归（尤其"膝盖不咋个痛了，但是喘不上气"仍命中 RF-ORTHO-01）；新增 4 类极性单测全过。

### F7（#2，P1）：reorder 归属校验 —— SC-09

**根因定位**：`backend/core/scheduling.py` `reorder()`（约 139–157 行）**未校验 `actor` 与 `row["nurse_id"]` 的归属关系**，任何护士可调整他人排程并返回 200。

**修复思路**：函数入口加载排程后校验 `row["nurse_id"] != actor["user_id"] → PermissionError`（403 forbidden）。reorder 的 `_allow` 限定 nurse 角色，无管理角色豁免需求；越权尝试写 `audit`（escalation 域）。
**改动面**：`core/scheduling.py` `reorder()`。
**验收标准**：`run_s2.py --only SC-09` PASS：n2 调整 n1 排程返回 403、原顺序不变；`run_round1.py` SC-06（拖拽排序）无回归。

### F8（#3，P0）：短信独立指标（顶层 + 按日）—— AU-05

**根因定位**：`backend/core/records.py` `stats()`（约 200–224 行）+ `server.py` `/api/summary`。两层问题：
1. **位置与语义**：短信计数仅作为 `stats.sms_attempts_not_counted` 嵌在 stats 内，且命名是"不计入完成率的提示性字段"，不是看板一等指标；AU-05 用例读的是 `/api/summary` 顶层字段 → 取值恒 0，判定"未进入独立统计"。
2. **口径缺陷**：该计数 `SELECT COUNT(*) FROM contact_attempts WHERE channel='sms'` **无日期过滤**，跨日累计失真，与看板"按日"语义不符。

**修复思路**：
1. `/api/summary` 顶层新增独立指标 `sms_touchpoints`：当日短信触达数（`contact_attempts.channel='sms' AND at LIKE date||'%'`），与完成率解耦（医疗-017 口径：短信不计完成率）。
2. `stats()` 内短信计数改为按日过滤并更名 `sms_attempts`（保留 `not_counted` 语义在规则说明中注明）。
3. 用例口径同步（批次三）：AU-05 断言改读顶层 `sms_touchpoints`。

**改动面**：`core/records.py` `stats()`、`server.py` `/api/summary` handler、`tools/run_s2.py` AU-05 断言。
**验收标准**：`run_s2.py --only AU-05` PASS（attempts 不变、顶层独立计数 ≥1）；跨日场景手工验证：昨日短信不计入今日 `sms_touchpoints`；`run_round1.py` NF/看板相关冒烟无回归。

### F9（#6，P1）：正则分句 + 否定判定 —— RF-D02 / RF-06

**根因定位**：`backend/core/redflag.py` 正则通道（约 102–109 行）：对**全文**直接 `re.search(pattern, text)`，既不分句也不做任何否定判定。"伤口没有裂开"/"小腿没有肿"/"没有晕倒"三句全部误报。

**修复思路**：
1. 正则通道改为**逐分句匹配**：对每个 clause `re.finditer`，命中位置再做否定判定 —— 命中片段之前 6 字符内含否定词，**或命中片段内部含否定词**（与 F6 的极性函数复用）→ 跳过。
2. 数字型正则（体温 ≥38.0 等）语义上不受否定词影响，统一走同一判定亦无副作用（"没有发烧"本就不匹配数字正则），保持通道行为一致。
3. 回归口径：正则真阳性（如"体温 39.2 度"）必须保持命中。

**改动面**：仅 `core/redflag.py`。
**验收标准**：`run_s2.py --only RF-06` PASS（三句否定式零误报）；`run_round1.py` RF 域无回归；与 F6 合并跑红旗极性单测矩阵。

### F10（#7，P1）：规则分离签名验签（fail-closed）—— RF-11

**根因定位**：`backend/core/redflag.py` `load_rules()`（约 26–29 行）直接 `json.load` 规则文件，`signed_by` 仅为元数据展示，**无任何完整性校验**；规则元数据（阈值、语料数、签发人）可被静默篡改。

**修复思路**：
1. 采用**分离签名**：规则文件 `backend/rules/redflag.json` 旁增加 `redflag.json.sig`（HMAC-SHA256(密钥, 文件原始字节) 的 base64）。对原始字节签名，避免 JSON 重序列化差异。
2. 密钥独立管理：`FDE_RULES_KEY` 环境变量或 `data/.rules_secret` 文件（与 `FDE_SECRET` 会话密钥职责分离）；由质量/专科角色保管。
3. 加载即验签（fail-closed）：`hmac.compare_digest` 不匹配 → **服务拒绝启动**（或启动时明确报错退出），stderr 给出明确原因；不静默降级。MVP 阶段不做运行期热更，进程启动时验证一次。
4. 新增签名工具 `tools/sign_rules.py`（标准库 argparse + hmac）：规则变更后由授权角色重签。
5. 签名验证失败尝试写 `audit`（security 域）——注意启动失败场景库可能不可用，尽力而为即可。

**改动面**：`core/redflag.py` `load_rules()`、新增 `tools/sign_rules.py`、新增 `.sig` 文件、`docs` 补签发流程说明。
**验收标准**：篡改 `redflag.json` 任一字节（含元数据）→ 服务启动失败且报错明确；正常文件 + 有效签名 → 启动成功、红旗引擎行为不变；用 `sign_rules.py` 重签后通过；`run_s2.py --only RF-11` PASS。

### F11（#10，P1）：禁止重复签发 —— TK-08

**根因定位**：`backend/core/templates.py` `sign()`（约 22–50 行）：`major+1` 无条件前进版本，且不检查当前 `status`，同日/同内容重复签发持续产生新版本（1.0→2.0→…），旧排程引用旧版本造成漂移。

**修复思路**：`sign()` 入口校验：当前 `status == 'signed'` → `ValueError`（400 `already_signed`，"模板已签发，禁止重复签发"）。版本演进改为通过**新草稿 → 再签发**的正常流程（保持 0.9→1.0 语义不变）。幂等返回不采纳（用例预期为拒收，且静默幂等会掩盖误操作）。
**改动面**：仅 `core/templates.py`。
**验收标准**：`run_s2.py --only TK-08` PASS：重复签发返回 400、版本保持 1.0、旧排程引用不变；`run_round1.py` TK-02/TK-03/TK-05（首次签发、生效日延后）无回归。

### F12（#11，P1）：补排清理 + stale 语义收敛 —— TK-09

**根因定位**：`backend/core/scheduling.py` `run()` 三个叠加面：
1. **无清理逻辑**：补排成功路径从不 `manual_reason=''`，患者可同时挂排程与人工清单。
2. **stale 早退一刀切**：名单 `exported_at` 超 24h → 仅把 `manual_reason=''` 的患者转人工即 `return`，已有 `manual_reason` 的患者（如"模板未签发转人工"）永远不进入补排判断 —— 模板后补签发的场景被 stale 分支整体吞掉（第四轮实测 0104 未补排且原因残留 '模板 1.0 尚未生效，转人工' 即此路径）。
3. **生效日语义**：签发生效日 = 签发当日（真实时钟），若重跑目标日期早于生效日，产品拒绝排程是正确行为 —— 用例 DATE 固定导致跨日执行时必然失败，属用例时钟耦合。

**修复思路**：
1. 补排清理：主循环中，当患者 `manual_reason` 非空、当日无排程、且本次满足全部排程条件（已授权、模板已签发生效）→ 创建排程成功后 `UPDATE patients SET manual_reason='' WHERE id=?`（同事务）。
2. stale 语义收敛：stale 表示名单过期，**既不补排也不清理**（保守，防基于旧名单写入）；该行为写入《设计方案》并在转人工文案中区分"名单超 24 小时未补齐"与"模板未签发"两类原因。
3. 用例口径同步（批次三）：`s2_lib.DATE` 与 fixture `exported_at` 动态化（基于运行日），保证"签发 → 同日重跑"落在新鲜窗口内，消除时钟耦合。

**改动面**：`core/scheduling.py` `run()`；`tools/s2_lib.py`、`tools/run_s2.py`（口径）。
**验收标准**：`run_s2.py --only TK-09` PASS：同日重跑后 0104 已排程、`manual_reason` 为空；`run_round1.py` SC-04（幂等）/SC-05（模板延后）无回归；stale 场景手工验证：人为构造过期名单 → 不补排、原因文案正确。

### F13（#12，P1）：鉴权前置统一判断 —— EX-04

**根因定位**：`backend/server.py` `_handle()` 路由循环（约 388–420 行）：401 判断在**每个路由命中之后**才执行，未匹配路由直接落到循环后的 `raise ApiError(404)`。未知路径 + 未登录 → 404，向未认证方泄露路由存在性。

**修复思路**：在路由匹配前统一判断：`path` 以 `/api/` 开头、不属于 public 集合（`/api/login`、`/api/bootstrap`）、且 `user is None` → 直接 `raise ApiError(401)`；之后再匹配路由，未命中 → 404。静态资源路径不受影响。
**改动面**：仅 `server.py`。
**验收标准**：`run_s2.py --only EX-04` PASS：未登录访问任意未知 `/api/*` → 401；已知 public 路径行为不变；`run_round1.py` 鉴权域用例无回归；**第三轮基线不回退**：PM-01（未登录访问受保护接口一律 401）、PM-07（会话伪造/过期统一 401）——public 集合判定必须与这两例的既有口径严格一致。

### F14（#15，P1）：synthetic 字段全链路 —— SEC-12

**根因定位**：`backend/core/contract.py` `FIELDS` 白名单 8 字段、`validate_row()`（约 31–58 行）无 synthetic 处理；`backend/core/db.py` `patients` 表（约 54–67 行）**无 synthetic 列** —— 合成/演示数据与真实数据在库内不可区分，导入无任何标记校验。

**修复思路**（全链路四步）：
1. **schema**：`patients` 增加 `synthetic INTEGER NOT NULL DEFAULT 0`；`init_db()` 增加存量库迁移（`PRAGMA table_info` 检查缺失则 `ALTER TABLE ADD COLUMN`）。
2. **契约**：`FIELDS` 增加可选字段 `synthetic`；`validate_row` 规整为 0/1（缺省 0，非法值拒收）；契约版本升 v1.1 并在审计 detail 中注明。
3. **导入策略**：MVP 试点模式（默认）：synthetic=1 行允许入库并全程打标（board/records 响应透出标记、审计 detail 含 synthetic 计数）；预置配置开关 `ALLOW_SYNTHETIC=false`（生产模式）：synthetic=1 行**拒收**（错误原因"合成数据禁入生产"，逐行拒绝原因落回执）。fixtures 全部标注 synthetic=1。
4. **透出**：`/api/schedule`、`/api/records`、`/api/drafts` 响应携带 synthetic 标记，供 UI 与导出甄别。

**改动面**：`core/db.py`、`core/contract.py`、`server.py`（响应透出）、fixtures、`core/config`（开关）。
**验收标准**：`run_s3.py --only SEC-12` PASS：试点模式合成数据入库且带标记、审计含计数；生产模式开关打开后合成行被拒收且回执明确；`run_round1.py` CT 域导入用例无回归；存量库（第四轮临时库副本）迁移后可正常启动。**第三轮回填补充**：① 固化负向夹具（第三轮实测批次 'SEC12-REAL-BATCH' 风格：真实样式、无 synthetic 标记）作为拒收验证的标准夹具；② 导入回执现有字段（`dropped_fields`、`reject_reasons` 等）不得破坏；③ 迁移后库的 `NF-06`（sqlite3 .backup 可恢复且验链通过）复跑确认。
**风险与注意**：契约加字段属向后兼容变更，`his_import` 回执统计需包含新增拒绝原因；UI 侧标记透出与 NF-07/UI 浏览器走查合并验证。

### F15（#16，P1）：去除 handler 外层事务 —— SC-05

**根因定位**：`backend/server.py` `/api/his/import`（约 107–126 行）：`with db.transaction(ctx.conn): return contract.simulate_timeout(...)`，而 `contract.simulate_timeout()`（`core/contract.py` 约 134–151 行）**内部再 `with transaction(conn)`** → `sqlite3.OperationalError: cannot start a transaction within a transaction` → HTTP 500。`import_batch` 自管事务，simulate 分支被 handler 多包了一层。

**修复思路**：handler 直接调用 `contract.simulate_timeout(ctx.conn, ctx.user)`，不再包外层事务（与 `import_batch` 一致，"事务所有权归 core 函数"为固定约定）。降级行为不变：排程落 `pending_backfill`、写 `audit(batch.fallback)`、患者转人工。
**改动面**：仅 `server.py` 一处 handler。
**验收标准**：`run_s2.py --only SC-05` PASS：200 + `pending_backfill` + 审计事件，无 500 无堆栈；`run_round1.py` SC-05 用例同步翻绿；同文件其余 handler 复核一遍事务边界（EX-D01 关联复核项：确认无同类双层嵌套）。

### F16（#8，P2 + 口径）：RF-12 收敛与定性复核

**定性复核（代码级）**：
1. **空陈述半边**：`check_and_draft` 对空串会正常走"无命中 → 生成全'未提及'草稿"并返回 `{interrupted: false, draft: ...}`（无 `hits` 键）。用例断言 `empty.get("hits") == []` 对无命中响应恒失败 —— **与 A 类 RF-02 同型的脚本口径问题**，产品行为符合 S2 用例"空陈述可草稿"的预期。处置：批次三修正断言（改判 `interrupted == false` 且 `draft.fields` 存在），第五轮报告中将该半边转"非缺陷（脚本口径）"留档。
2. **超长文本半边**：真实传输缺陷，随 **F1** 闭环（525KB → 413），不单独改代码。

**产品语义补强（可选，低优先）**：空陈述生成的"全未提及"草稿存在被护士顺手确认的误导风险，建议后续版本对空陈述返回 400 `transcript_empty` —— 涉及需求语义变更，本 MVP 不实施，列入需求池。

## 四、批次三：用例口径同步包与专项补齐

### 4.1 用例口径同步清单（与代码修复同批生效）

| 用例 | 现口径问题 | 同步动作 | 关联修复项 |
| --- | --- | --- | --- |
| AU-05 | 断言读 `/api/summary` 顶层，而计数在 stats 内 | 改读顶层 `sms_touchpoints` | F8 |
| SC-08 | 依赖全院轮转偶然产生"本人跨病区排程" | 改为按病区分配断言 + 显式构造跨病区排程验证可见性 | F5 |
| RF-12 空陈述 | `hits == []` 对无命中响应恒假 | 改判 `interrupted == false` + `draft.fields`；定性转非缺陷 | F16 |
| TK-09 / S 系时钟 | `s2_lib.DATE` 固定 2026-11-04、fixture `exported_at` 固定，与真实签发日/stale 24h 耦合 | DATE 与 exported_at 动态化（基于运行日） | F12 |
| A 类 8 例 | 第四轮已修的 7 处断言口径 | 保持，随第五轮回归验证不回退 | — |

### 4.2 EX-D01 并发加固验证（随 F3 交付）

修复 F3 后执行并发专项：同一 `draft_id` 并发 8 次确认（4 凭证 × 2 轮，含双凭证交叉）→ 断言：落库登记恒 1 条、草稿状态恒 confirmed、其余响应 ∈ {403, 409}；`stepup_nononces` 无脏数据。脚本挂入 `run_s2.py` 作常驻回归用例（EX-D01 转正式用例，解除"风险注记"）。

### 4.3 C 类阻塞补齐计划（业务方/运维依赖，不等修复批次）

| 编号 | 补齐动作 | 责任方 | 验收口径 |
| --- | --- | --- | --- |
| RF-10 | ≥120 条独立标注语料（双人标注、分歧仲裁），或基于现有语料扩样 | 业务方提供/确认标注 | `run_eval.py` 命中率 ≥95% 且误报率 ≤5%，出专项报告 |
| NF-05 | 独立机器 2h 长稳定窗口，执行方案（第三轮报告已载明）：50 并发持续 2h，每 10min 采样 P95 与错误率，结束后对齐审计链与表行数 | 运维提供资源 | 内存/句柄无泄漏趋势、错误率与 P95 漂移在阈值内、审计链与表行数对齐，出专项报告 |
| NF-07 + UI-01～08 | 真实浏览器走查：Chrome/Edge × 双分辨率（1280×800 / 1920×1080），覆盖排程、外呼、草稿确认、红旗工单、审计页，含首屏 30s、遮罩、红旗阻断、确认高亮、撤回二次确认、键盘可达、断网可用，记录滚动/换行/按钮可达性 | 测试 + 前端 | 浏览器走查记录 + 截图留档（不由 HTTP 黑盒脚本代替） |

### 4.4 PM-09 统一认证接入规划（试点缺口，风险接受维持）

- 目标：登录改接院内统一认证（SSO 票据校验 → 本地会话签发），移除"无口令因子"试点缺口。
- 范围界定：**不纳入本修复周期**（依赖院内 IdP 对接资源）；修复周期内仅保证 `login` 接口形态可替换（`security.login` 保持单一入口，不在业务代码内散落口令逻辑）。
- 里程碑：统一认证对接方案作为放行附件随第五轮报告提交，试点期维持"风险接受"定性。

## 五、修复批次与第五轮回归计划

### 5.1 批次划分

| 批次 | 内容 | 出口标准 |
| --- | --- | --- |
| 批次一（P0 先行） | F1、F2、F3（含 EX-D01 原子化）、F4、F5（含 DEF-R1-03）、F6 | 对应 P0 用例全 PASS；`run_round1.py` 全量无回归 |
| 批次二（P1 跟进） | F7～F15（SC-09、AU-05、RF-D02、RF-11、TK-08、TK-09、EX-04、SEC-12、SC-05） | 对应用例全 PASS；批次一用例复跑不回退；NF-01～NF-04 性能基线复跑不劣化（F2 join 过滤 / F5 病区分桶 / F8 按日统计触及查询路径） |
| 批次三（口径与专项） | 用例口径同步包、EX-D01 转正式用例、RF-12 定性留档、C 类补齐计划启动、PM-09 规划附件 | 第五轮回归全绿 + 专项计划落档 |

### 5.2 第五轮回归命令序列（针对主源码 backend/）

```bash
python tools/run_s2.py --only SC-05,SC-08,SC-09,AU-05,AU-07,RF-04,RF-06,RF-11,RF-12,RC-05,TK-08,TK-09,EX-04,EX-05,EX-06,EX-07 --json data/s5_s2.json --md docs/MVP第五轮回归-S2.md
python tools/run_s3.py --only PM-04,SEC-03,SEC-04,SEC-11,SEC-12 --json data/s5_s3.json --md docs/MVP第五轮回归-S3.md
python tools/run_round1.py
```

（EX-D01 转正式用例后并入 S2 序列；`run_round1.py` 覆盖 S1 冒烟 + 90 例回归，验证无基线漂移。）

缺陷用例全绿后追加**全量回归**（第三轮回填要求，对照第三轮基线：S2 45 PASS / S3 21 PASS 零回退）：

```bash
python tools/run_s2.py --json data/s5_s2_full.json --md docs/MVP第五轮回归-S2全量.md
python tools/run_s3.py --json data/s5_s3_full.json --md docs/MVP第五轮回归-S3全量.md
```

### 5.3 放行门槛

1. 16 条缺陷关联用例 + EX-D01 全部 PASS，且 A 类 8 例维持 PASS（口径修正不回退）。
2. `run_round1.py` 全量回归无新增失败。
3. 批次三口径同步全部落地（避免"修复后用例仍红"的假阳性台账）。
4. C 类 4 组阻塞维持书面记录，补齐计划有责任方与时点；PM-09 接入规划随报告提交。
5. **已通过项零回退**（第三轮回填新增门槛）：第三轮 S3 21 例 PASS、前两轮 S2 45 例 PASS、S1 96 例（90 评估 + 6 冒烟）在全量回归中零回退。
6. 上述全满足 → 副本可进入验收评审；仍有 P0 未闭合 → 不放行。

## 六、第三轮测试结果回填后的方案补充（2026-11-05）

第三轮报告（已合并至 [MVP测试轮次汇总.md](MVP测试轮次汇总.md) 第三节，2026-11-05 00:08 执行，S3 28 例：21 PASS / 5 FAIL / 2 BLOCKED）回填后，对前文方案做以下补充与修正。5 条 FAIL（PM-04、SEC-03、SEC-04、SEC-11、SEC-12）与 2 组 BLOCKED（NF-05、NF-07）均已在 F1/F2/F3/F14 与 C 类补齐计划中覆盖，无新增缺陷项；补充集中在**"修复不得破坏第三轮已通过的 21 例基线"**这一面。

### 6.1 不可回归基线清单（第三轮 21 例 PASS）

| 域 | 基线用例 | 与修复项的关联 |
| --- | --- | --- |
| 权限 | PM-01、PM-02、PM-03、PM-05、PM-06、PM-07、PM-08 | F2（列表过滤不得改变 401/403 边界）、F13（public 集合判定）、F15（不触碰审计接口） |
| 安全 | SEC-01（运行期零外联）、SEC-02（凭证五类失效）、SEC-05（审计验链）、SEC-06（留痕失败回滚 503）、SEC-07（撤回停呼停训）、SEC-08（草稿本地闭环）、SEC-09（日志不落敏感原文）、SEC-10（审计最小字段） | F1（日志收口）、F3（confirm 事务重构）、F4（授权前置校验）、F10/F14（不引入外部网络出口） |
| 非功能 | NF-01（P95 ≤300ms）、NF-02（200 人排程 ≤3s）、NF-03（5 会话并发）、NF-04（1 万行查询）、NF-06（backup 恢复验链） | F2/F5/F8（查询路径变更）、F14（schema 迁移） |

### 6.2 修复项附加约束（依据 6.1 派生）

1. **F1**：异常日志收口必须满足 SEC-09 —— 4xx 摘要不含报文内容，500 堆栈经 sanitize；同时复核 SEC-01（收口过程不得新增网络行为）。
2. **F2/F4/F3 新增的审计事件**（越权 403、`auth.denied_revoked` 等）须符合 SEC-10（最小必要字段）与 SEC-09（detail 不落敏感原文，如患者姓名/陈述内容）。
3. **F5/F8**：按病区分桶与按日 COUNT 在 200 人排程、1 万行规模下复测 NF-01～NF-04，阈值不劣化。
4. **F14**：存量库迁移（ALTER TABLE）后复跑 NF-06（backup 恢复 + 验链），确认迁移不影响备份恢复流程。

### 6.3 实测口径修正

- SEC-11"日志打印堆栈"表述修正：第三轮第六节实测备注"日志含堆栈=False"、第四轮同样未采集到堆栈 —— 第二轮缺陷清单中的"打印堆栈"为早期估计。F1 第 3 条已按实测口径改写（见 F1）。
- 第三轮 SEC-03 实测备注"登记表只有一条记录"佐证：重放（单凭证重复使用）场景下无重复登记，重复风险集中在双凭证并发（EX-03/EX-D01 域），F3 的原子化方案覆盖面正确。
- 第三轮执行时间 00:08（早于 fixture `exported_at` 02:10 的 24h 窗口边界）与第四轮 TK-09 复现现象相互印证 F12 关于 stale 早退分支的根因判断，方案无需调整。
