# MVP 第五轮修复总结报告

- 报告日期：2026-11-05
- 修复依据：[MVP修复方案.md](MVP修复方案.md)（基于第四轮 16 条现行缺陷）
- 实施范围：**主源码 `backend/`**；`releases/MVP-v0.1.0-20261104` 冻结留档，未作任何修改
- 验证口径：第五轮回归直接针对主源码服务执行（`tools/s2_lib.py` 自动拉起 `backend.server`）
- 总体结论：**16 条缺陷全部修复，定向与全量回归零失败、零回退**；NF-07 浏览器兼容与 UI-01～08 已于本轮以真实浏览器 UAT 闭环（8/8 PASS），C 类阻塞收敛为 2 项（RF-10、NF-05，业务方/环境依赖，非代码缺陷）

## 一、结果总览

| 验证项 | 规模 | 结果 |
| --- | --- | --- |
| 定向回归 S2 | 17 例（F1–F16 对应缺陷用例 + EX-D01） | 17 PASS / 0 FAIL |
| 定向回归 S3 | 5 例（PM-04、SEC-03/04/11/12） | 5 PASS / 0 FAIL |
| 首轮执行器全量 | 81 例（S1 冒烟 + S2 功能 CT/SC/AU/RF/RC/TK/EX） | 81 PASS / 缺陷复现 0 / 失败 0 |
| S2 全量回归 | 70 例 | 69 PASS / 0 FAIL / 0 ERROR / 1 BLOCKED（RF-10） |
| S3 全量回归 | 28 例 | 26 PASS / 0 FAIL / 0 ERROR / 2 BLOCKED（NF-05、NF-07） |
| 浏览器 UAT 补做（NF-07 + UI-01～08） | 8 例（双分辨率） | 8 PASS / 0 FAIL |

对照第三轮基线（S2 45 PASS / S3 21 PASS）：**通过数 45→69、21→26，无任何用例由绿转红**；EX-D01 由"风险注记"转为 S2 常驻正式用例。S3 全量中 NF-07 因执行器不具备真实浏览器能力而记为 BLOCKED，本轮已以独立 UAT 脚本补做并通过，见第四节「UI 与浏览器兼容性 UAT」。

## 二、逐项修复情况

| 修复项 | 缺陷（用例） | 等级 | 修复要点 | 主要改动 |
| --- | --- | --- | --- | --- |
| F1 | 载荷异常连接被直接关闭（EX-05/06/07、SEC-11、RF-12 超长） | P0 | 请求体解析并入统一 `try/except ApiError`，任何异常均落为 HTTP 响应；413 前先按块排空请求体（上限 16 MB）并显式 `Connection: close`，消除 RST 竞态；4xx 只记摘要不打印堆栈，500 堆栈先脱敏 | `server.py` `_handle`、`_drain`、`_send` [server.py](file:///c:/Users/32572/Desktop/临时/fde/backend/server.py#L343-L452) |
| F2 | 越病区读取登记/草稿（PM-04、SEC-04） | P0 | 列表按角色过滤：护士＝本人病区 ∪ 本人名下排程关联，医生/质量/管理者＝全院；作废草稿加归属校验，越权 403 并落审计 | `core/records.py` [list_drafts](file:///c:/Users/32572/Desktop/临时/fde/backend/core/records.py#L193)、[list_records](file:///c:/Users/32572/Desktop/临时/fde/backend/core/records.py#L213)、`discard_draft`；`server.py` handler 传 `ctx.user` |
| F3 | 凭证重放返回 400（RC-05、SEC-03）＋EX-D01 并发重复登记 | P0 | `confirm` 顺序改为"草稿存在→verify_stepup→状态→digest→写入"；状态检查移入事务并改成条件更新抢占（`rowcount!=1` 回滚），并发确认天然原子化；冲突映射 409 `draft_conflict` | `core/records.py` [confirm](file:///c:/Users/32572/Desktop/临时/fde/backend/core/records.py#L123)、[DraftConflict](file:///c:/Users/32572/Desktop/临时/fde/backend/core/records.py#L19) |
| F4 | 撤回授权后仍可确认（AU-07） | P0 | `confirm` 与 `check_and_draft` 在写入前校验 `auth_status='已授权'`，否则 403 并写授权域审计，保持审计与拒绝同事务 | `core/records.py` [check_and_draft](file:///c:/Users/32572/Desktop/临时/fde/backend/core/records.py#L65)、`confirm` |
| F5 | 跨病区排程归属歧义（SC-08、DEF-R1-03） | P0 | 护士按 `ward` 分桶轮转；桶为空转人工（`本病区无可用随访护士，转人工`），不再静默跨病区；看板与人工清单同口径"本人排程 ∪ 本病区患者" | `core/scheduling.py` [run 分配段](file:///c:/Users/32572/Desktop/临时/fde/backend/core/scheduling.py#L100)、[board](file:///c:/Users/32572/Desktop/临时/fde/backend/core/scheduling.py#L129-L145) |
| F6 | 变体通道跨过否定词（RF-04 / RF-D01） | P0 | 新增"窗口内含否定词且变体不含否定词→视为否定"，与既有极性冲突合并为统一极性一致函数；窗口打分与 0.6 阈值不变 | `core/redflag.py` [_best_variant_span](file:///c:/Users/32572/Desktop/临时/fde/backend/core/redflag.py#L92) |
| F7 | 跨护士调整排程（SC-09） | P1 | `reorder` 校验 `row.nurse_id == actor.user_id`，越权 403 并写 escalation 域审计 | `core/scheduling.py` [reorder](file:///c:/Users/32572/Desktop/临时/fde/backend/core/scheduling.py#L156) |
| F8 | 短信未独立统计（AU-05） | P0 | `/api/summary` 顶层新增当日 `sms_touchpoints`（按日过滤、与完成率解耦）；`stats` 内计数同步按日并更名 | [records.py](file:///c:/Users/32572/Desktop/临时/fde/backend/core/records.py#L264)、`server.py` `/api/summary` |
| F9 | 正则通道未做否定判定（RF-06 / RF-D02） | P1 | 正则匹配改为分句执行 + 句内否定抑制，消除"没有出血/没有发烧"类误报 | `core/redflag.py` [_regex_hit](file:///c:/Users/32572/Desktop/临时/fde/backend/core/redflag.py#L131) |
| F10 | 规则文件可被静默篡改（RF-11） | P1 | 启动时对 `redflag.json` 原始字节做 HMAC-SHA256 验签，缺签名/不一致即 `RuntimeError` 拒绝启动（fail-closed）；密钥优先级 `FDE_RULES_KEY` → `data/.rules_secret` → `backend/rules/.rules_secret`；新增签发工具 | `core/redflag.py` [load_rules](file:///c:/Users/32572/Desktop/临时/fde/backend/core/redflag.py#L58)、[sign_rules.py](file:///c:/Users/32572/Desktop/临时/fde/tools/sign_rules.py) |
| F11 | 重复签发推进模板版本（TK-08） | P1 | 已签发模板重复签发直接 400"模板已签发，禁止重复签发"，版本单调 + CAS 不变 | `core/templates.py` [sign](file:///c:/Users/32572/Desktop/临时/fde/backend/core/templates.py#L35) |
| F12 | 补排后 `manual_reason` 未清理（TK-09） | P1 | 同日重跑补排成功后清空人工原因；stale（>24h）批次统一转人工并收敛计数口径 | `core/scheduling.py` `run()` |
| F13 | 未知路由先 404 未先鉴权（EX-04） | P1 | 鉴权判定前置于路由匹配：未登录任意 `/api/*` 一律 401，已登录未知路由才 404 | `server.py` [server.py](file:///c:/Users/32572/Desktop/临时/fde/backend/server.py#L426-L453) |
| F14 | 导入未校验合成标记（SEC-12） | P1 | `synthetic` 进入 8→9 字段白名单并全链路落库；试点模式（`FDE_ALLOW_SYNTHETIC=false`）拒收 `synthetic=0`；旧库自动 ALTER 迁移 | `core/contract.py` [contract.py](file:///c:/Users/32572/Desktop/临时/fde/backend/core/contract.py#L20-L117)、`core/db.py` [db.py](file:///c:/Users/32572/Desktop/临时/fde/backend/core/db.py#L272) |
| F15 | 导入超时模拟双层事务→500（SC-05） | P1 | 移除 handler 外层事务，契约层内单事务 + 超时转 `pending_backfill`，无 500、无堆栈，审计同事务 | `server.py` `his_import` handler、`core/contract.py` |
| F16 | 空陈述/超长文本处理（RF-12） | P2 | 超长半边随 F1 闭环（525 KB→413 而非断连）；空陈述定性复核并入用例口径包 | `tools/` 用例 + F1 |

附带修复：`board()` 人工清单曾对护士泄漏他病区患者（round1 DEF-R1-01），已随 F5 看板口径一并按病区过滤。

## 三、批次三：口径同步与专项

1. **用例口径同步**：AU-05 改读顶层 `sms_touchpoints`；SC-08 期望改为"本病区集合 + 显式跨病区排程可见"；TK-09 断言 `manual_reason` 清空；RF-12 拆分空陈述/超长两段；CT-04/05/06 由"整批 409"改为行级拒收统计；CT-07 改断言 `idempotent=True`。
2. **夹具时间动态化**：`s2_lib.DATE` 与 fixture `exported_at` 改为执行日生成，消除静态日期超 24h 导致的"created=0 / stale 全转人工"假失败。
3. **EX-D01 转正式用例**：双凭证并发确认（8 并发）常驻 `run_s2.py`，断言落库恒 1 条、状态恒 confirmed、其余响应 ∈ {403, 409}，风险注记解除。
4. **签发流程文档化**：`docs/05-构建集成.md` 新增"红旗规则签发流程（fail-closed）"小节（密钥来源、签发/校验命令、启动验签、改文件必重签、LF 换行约束）。当前签名校验通过。
5. **C 类补齐计划**：按方案 §4.3 以书面形式保留，见下节。

## 四、C 类阻塞（非代码缺陷）与 UAT 补做

| 编号 | 内容 | 阻塞原因 | 解除条件 |
| --- | --- | --- | --- |
| RF-10 | 语料命中率/误报率 | 现有 150 行语料为自动扩样（正负样本成对生成），不满足"≥120 条独立标注" | 业务方提供独立标注语料后替换 `backend/tests/corpus/redflag.jsonl` |
| NF-05 | 2 小时混合负载稳定性 | 需独立长窗口执行，常规回归不覆盖 | 独立长稳窗口执行并留档 |
| NF-07 | 浏览器兼容性 UAT（1280×800 / 1920×1080） | 回归执行器不具备真实浏览器能力 | **已解除**：本轮以真实浏览器 UAT 补做，8/8 PASS（见下「UI 与浏览器兼容性 UAT」） |

PM-09（统一认证接入）仍为试点缺口，按方案以规划附件提交，不阻塞本轮放行。

### UI 与浏览器兼容性 UAT（NF-07 / UI-01～08，第五轮补做）

- 执行环境：Microsoft Edge 156.0.4314.58（Playwright 1.64.1，`channel=msedge`，headless）；1280×800 与 1920×1080 两种视口
- 执行方式：每个分辨率独立临时数据目录与临时端口（8897 / 8896），HTTP 播种（HIS 导入 v1 → 生成排程，护士名下 4 条）后走查全流程；全程外部网络请求 0 条
- 证据：[data/uat_ui_round5.json](file:///c:/Users/32572/Desktop/临时/fde/data/uat_ui_round5.json)；截图留档 `.screenshots/`（16 张，其中 1920 全流程 6 张归 UI-06）

| 用例 | 等级 | 标题 | 结果 | 实际 |
| --- | --- | --- | --- | --- |
| UI-01 | P0 | 登录与首屏可用（1280×800） | PASS | 首屏 0.29s，排程 4 行；`scrollWidth=1280 / clientWidth=1280` 无横向溢出 |
| UI-02 | P0 | 患者电话脱敏显示 | PASS | 工作台联系电话显示「138****11」（3+4+2） |
| UI-03 | P0 | 红旗告警阻断确认写库 | PASS | 告警「红旗命中 · 问卷已中断」，命中 2 条、置信度 100%；chip 含工单；确认写库入口数 0 |
| UI-04 | P0 | 草稿差异高亮与确认留痕 | PASS | 高亮字段 1 个；提示「已写入登记表，签署人 林亦舟；差异字段 1 个」；确认期间弹窗 0 个 |
| UI-05 | P0 | 撤回授权确认与外呼停止 | PASS | 弹窗「确认患者撤回授权？系统将停止外呼与录音，历史录音标记「不得用于训练」。」；提示「已撤回授权：24 小时内停用…」 |
| UI-06 | P1 | 1920×1080 布局与全流程 | PASS | 排程 1920/1920、工作台 1920/1920；登录→看板→外呼→红旗→草稿确认→撤回无错位 |
| UI-07 | P1 | 键盘可达性与焦点可见 | PASS | 纯键盘完成登录并进入工作台；21 个 Tab 停靠点均可聚焦、焦点框可见（日期控件额外含浏览器原生分段停靠点） |
| UI-08 | P1 | 断外网可用（无外链依赖） | PASS | 断外网（仅放行本机服务）后登录成功、排程 4 行、工作台可用；被拒外链请求 0 条 |

结论：NF-07 浏览器兼容性判定通过，UI-01～08 全部满足《MVP测试方案.md》5.9 节口径，截图与机器可读结果已留档；C 类阻塞由 3 项收敛为 2 项（RF-10、NF-05）。

## 五、回归留档

| 文件 | 说明 |
| --- | --- |
| [s5_s2.json](file:///c:/Users/32572/Desktop/临时/fde/data/s5_s2.json) | 定向 17 例，全 PASS |
| [s5_s3.json](file:///c:/Users/32572/Desktop/临时/fde/data/s5_s3.json) | 定向 5 例，全 PASS |
| [s5_s2_full.json](file:///c:/Users/32572/Desktop/临时/fde/data/s5_s2_full.json) | 全量 70 例，69 PASS / 1 BLOCKED |
| [s5_s3_full.json](file:///c:/Users/32572/Desktop/临时/fde/data/s5_s3_full.json) | 全量 28 例，26 PASS / 2 BLOCKED |
| [round1_report.json](file:///c:/Users/32572/Desktop/临时/fde/data/round1_report.json) | 首轮执行器 81 例，全 PASS |
| [uat_ui_round5.json](file:///c:/Users/32572/Desktop/临时/fde/data/uat_ui_round5.json) | 浏览器 UAT（NF-07 / UI-01～08）8 例，全 PASS；截图见 `.screenshots/` |

五份回归报告已合并至本文件第七节。

## 六、放行结论

按方案 §5.3 门槛逐条核对：

1. 16 条缺陷对应用例定向回归全部转绿（含 EX-D01）——**满足**。
2. `run_round1.py` 全量回归无新增失败（81/81）——**满足**。
3. 批次三口径同步全部落地，无"修复后用例仍红"假阳性——**满足**。
4. C 类阻塞维持书面记录、补齐计划有责任方与时点——**满足**（NF-07 已由本轮真实浏览器 UAT 解除；剩余 RF-10、NF-05 两项，PM-09 规划随报告提交）。

结论：**代码修复与回归工作具备放行条件**；剩余两项 C 类阻塞（RF-10 语料、NF-05 长稳）在报告与台账中显式披露，不随代码修复消解，需由业务方/运维在试点前补齐。

## 七、第五轮回归明细（合并归档）

本节由第五轮四份回归明细合并归档（编制日期 2026-11-05），原四份回归 md 文件不再单独保留。浏览器 UAT（NF-07 / UI-01～08）逐例结果见第四节与 [data/uat_ui_round5.json](file:///c:/Users/32572/Desktop/临时/fde/data/uat_ui_round5.json)。

### （一）定向回归 S2（17 例）

| 用例 | 等级 | 标题 | 结果 | 预期 | 实际 |
| --- | --- | --- | --- | --- | --- |
| SC-05 | P0 | 超时沿用上一版本名单 | PASS | 批次 pending_backfill，名单保留 | 符合预期 |
| SC-08 | P0 | 护士仅见本科室与非本科室本人排程且脱敏 | PASS | 0106 由二病区护士承接；显式跨病区本人排程可见、他院不可见，手机号 3+4+2 | 符合预期 |
| SC-09 | P1 | 跨护士调整应拒绝 | PASS | 返回 403 | 符合预期 |
| AU-05 | P0 | 短信不计完成率 | PASS | attempts 不变，独立计数 | 符合预期 |
| AU-07 | P0 | 撤回后禁呼禁确认 | PASS | 外呼/确认均 403 | 符合预期 |
| RF-04 | P0 | 方言阴性句不误报 | PASS | hits 为空 | 符合预期 |
| RF-06 | P1 | 否定式正则不误报 | PASS | 三条均无命中 | 符合预期 |
| RF-11 | P1 | 规则文件不可静默篡改 | PASS | 加载时验签通过，签名元数据可见 | 符合预期 |
| RF-12 | P2 | 空陈述与超长文本 | PASS | 空陈述可草稿，超长 413 | 符合预期 |
| RC-05 | P0 | 凭证重放拒绝 | PASS | 403 | 符合预期 |
| TK-08 | P1 | 重复签发拒绝且不前进版本 | PASS | 400 且版本保持 1.0 | 符合预期 |
| TK-09 | P1 | 签发后同日重跑补排且清理人工原因 | PASS | 0104 排程且 manual_reason 为空 | 符合预期 |
| EX-D01 | P0 | 双凭证并发确认原子化 | PASS | 8 并发仅 1 成功，其余 403/409，登记 1 条 | 符合预期 |
| EX-04 | P1 | 未知路由先鉴权 | PASS | 未登录 401，已登录 404 | 符合预期 |
| EX-05 | P1 | 坏 JSON | PASS | 400 bad_json | 符合预期 |
| EX-06 | P0 | 300KB 报文 | PASS | 413 body_too_large | 符合预期 |
| EX-07 | P1 | 40 层嵌套 | PASS | 400 too_deep | 符合预期 |

### （二）定向回归 S3（5 例）

| 用例 | 等级 | 标题 | 结果 | 实际 |
| --- | --- | --- | --- | --- |
| PM-04 | P0 | 护士只能看到本病区/本人负责数据 | PASS | 符合预期 |
| SEC-03 | P0 | step-up 凭证重放拒绝 | PASS | 符合预期 |
| SEC-04 | P0 | 跨病区数据隔离 | PASS | 符合预期 |
| SEC-11 | P1 | 超限载荷优雅拒绝 | PASS | 符合预期 |
| SEC-12 | P1 | 导入仅接受合成数据 | PASS | 符合预期 |

### （三）全量回归 S2（70 例）

| 指标 | 数值 |
| --- | --- |
| 用例总数 | 70 |
| 通过 | 69 |
| 失败 | 0 |
| 执行异常 | 0 |
| 阻塞未执行 | 1 |

| 等级 | 通过/总数 |
| --- | --- |
| P0 | 51/52 |
| P1 | 16/16 |
| P2 | 2/2 |

非 PASS 项：

| 用例 | 等级 | 标题 | 结果 | 实际 |
| --- | --- | --- | --- | --- |
| RF-10 | P0 | 120 条语料命中率/误报率 | BLOCKED | 未建立 ≥120 条独立标注语料，无法计算命中率/误报率（C 类阻塞） |

按用例域通过计数：

| 域 | 通过/总数 |
| --- | --- |
| CT | 9/9 |
| SC | 10/10 |
| AU | 9/9 |
| RF | 11/12（RF-10 阻塞） |
| RC | 11/11 |
| TK | 9/9 |
| EX | 10/10（含 EX-D01） |
| 合计 | 69/70 |

### （四）全量回归 S3（28 例）

| 指标 | 数值 |
| --- | --- |
| 用例总数 | 28 |
| 通过 | 26 |
| 失败 | 0 |
| 执行异常 | 0 |
| 阻塞未执行 | 2 |

| 等级 | 通过/总数 |
| --- | --- |
| P0 | 15/15 |
| P1 | 11/12 |
| P2 | 0/1 |

非 PASS 项明细：

| 用例 | 等级 | 标题 | 结果 | 实际 |
| --- | --- | --- | --- | --- |
| NF-05 | P1 | 2 小时混合负载稳定性 | BLOCKED | 2 小时混合负载稳定性需独立长窗口执行，本轮按测试方案标记未执行 |
| NF-07 | P2 | 浏览器兼容性（1280×800 / 1920×1080） | PASS（UAT 补做） | 执行器内无浏览器能力故本轮执行器记为 BLOCKED；已由真实浏览器 UAT 补做通过（8/8，见第四节） |

注：逐例机器可读明细见 data/s5_s2.json、data/s5_s3.json、data/s5_s2_full.json、data/s5_s3_full.json。
