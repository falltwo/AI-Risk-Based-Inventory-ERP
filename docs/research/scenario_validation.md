# Scenario Validation：情境契約 + Stockpyl adapter 實驗紀錄

日期：2026-09-12（DAG-20260912-7edaeaf2，writer=DeepSeek V4 Pro / xhigh）；
第二修正回合 2026-09-13（GPT-6 最終 verdict，見 §11）；
第三修正回合 2026-09-13（父 Agent 獨立探針反例的最小補修，見 §12）；
第四修正回合 2026-09-14（reviewer 獨立重現之剩餘缺口，見 §13）。
狀態：研究實驗，非企業預測、非產品功能。核准 spec 的停止條件為「達到 90 分鐘
（累計）即停止」；本文件不把 90 分鐘改寫為每回合重置——各回合的完整累計工時沒有
保存證據，無法如實判定是否觸發；第四修正回合係由使用者於 Discord 明確要求完成
reviewer 重現之剩餘缺口（見 §8）。
第一版為 GPT-6 `CHANGES_REQUIRED` 之後的修正紀錄；第二版（本版）為 GPT-6 最終
verdict（comparator 非有限值誤報、輸入契約缺口、Stockpyl 簿記/可用時間錯位、
證據敘述錯誤）之後的續修紀錄。歷史宣告一律以現存證據為準，不補造。

## 1. 範圍與硬邊界（本次只做這些）

- 新增獨立 domain module：`backend/scenario_contract.py`（資料契約）與
  `backend/scenario_engine.py`（deterministic baseline + Stockpyl adapter + 差異紀錄 + 保存）。
- 單一 SKU、單一 PO、三種情境（正常／中斷／替代）的合成 fixture
  （`tests/fixtures/scenarios/*.json`），全部為手算可驗算的固定需求案例。
- 交付：可重跑程式、結構化結果與差異紀錄。不接 UI、不改正式資料庫、
  不做多階 BOM、MCP、DBOS 或前端。

### 1.1 關於 import 隔離的事實（修正 verifier 指出的錯誤宣稱）

先前文件宣稱「完全不 import ERP」不成立。事實是：一般 `import backend.scenario_*`
仍會先執行既有 `backend/__init__.py`，該檔會載入 database、auth、inventory 等既有
ERP 模組。本 DAG 能做到與做到的只有：

- 新增模組（scenario_contract.py / scenario_engine.py）本身**未新增**任何 ERP import，
  只依賴標準函式庫 + lazy import Stockpyl。
- 測試的隔離方式：新測試在隔離 venv `/tmp/scn-stockpyl-venv`（只裝 stockpyl==1.0.2
  與 pytest）執行；既有 327 項回歸在相同 venv 執行並排除新增檔
  （`tests/ --ignore=tests/test_scenario_engine.py`）——第二修正回合實測同 venv
  直接執行全部 327 項亦全過（§11）。第一修正回合所稱「用 repo venv 執行」的
  具體解釋器未能以現存證據核對，不作為本文件的主張。
- verifier 的獨立執行（-I -B -S、程序內寫入／網路拒絕）也確認未修改正式資料庫。

## 2. 版本、seed 與容差

| 項目 | 值 |
|---|---|
| engine_version（契約/引擎） | `1.3.0`（第四修正回合；`1.2.0` 為第二/三修正回合版，`1.1.0` 為單一 PO 語意版，`1.0.0` 為修正前 base-stock 語意版，已被 GPT-6 否決） |
| Stockpyl 版本 | `1.0.2`（`get_stockpyl_version()` 實測；隔離 venv `/tmp/scn-stockpyl-venv`，Python 3.13.12） |
| pytest | 9.1.1 |
| seed | 0（三 fixture 與手算案例）；stochastic 對照另跑 seed 12345 |
| 數值容差（事前固定） | `NUMERIC_TOLERANCE = 1e-9`（contract 內固定，所有比對共用） |
| canonical bytes 精度 | 小數固定 12 位，-0.0 正規化為 0.0，排除 `executed_at`／`execution_time_seconds`；含 `po_id`/`po_quantity`/`po_arrivals` |
| 重跑方式 | `cd <worktree> && /tmp/scn-stockpyl-venv/bin/python -m pytest tests/test_scenario_engine.py -v` |

## 3. 合成案例與手算基準（非企業資料）

單一 PO 契約語意：固定輸入 D=10/期 × 20 期、單張 PO Q=200、初始庫存 I0=15、
h=1、p=50。每期事件順序（與 Stockpyl state_vars 實測對齊）：先到貨（先補 backorder、
餘量入現貨）→ 再遇需求 → 期末 IL_t。**t=lead 到貨一次之後沒有任何補貨**（不是
base-stock、不是無限供應）。

手算驗算（baseline 與 Stockpyl 兩引擎實跑值完全相同，容差 1e-9 內 0 差異）：

| 情境 | lead | 期末 IL 軌跡 | 首次缺貨 | fill_rate | 持有 | 缺貨 | 總成本 |
|---|---|---|---|---|---|---|---|
| 正常 | 1 | [5] + [195,185,…,15] | 無 | 200/200 = 1.0 | 2000.0 | 0.0 | 2000.0 |
| 替代 | 2 | [5,-5] + [185,175,…,15] | t=1 | 195/200 = 0.975 | 1805.0 | 250.0 | 2055.0 |
| 中斷 | 3 | [5,-5,-15] + [175,165,…,15] | t=1 | 185/200 = 0.925 | 1620.0 | 1000.0 | 2620.0 |

驗算例（中斷，L=3）：t=0 現貨 15 → 滿足 10，IL=5；t=1 現貨 5 → 滿足 5、欠 5，
IL=-5；t=2 現貨 0 → 全欠，IL=-15；t=3 到貨 200，先補 backorder 15 → 現貨 185，
再滿足 10 → IL=175。缺貨成本 = (5+15)×50 = 1000；持有 = 5×1 + Σ(175..15)×1 = 1620；
總成本 2620。fill_rate = 185/200 = 0.925。

註：p=50（修正前為 p=10）是為了讓單一 PO 語意下成本仍嚴格遞增
（正常 2000 < 替代 2055 < 中斷 2620）。p=10 時較晚到貨省下的持有成本會蓋過缺貨成本
增量（1820 < 1855），與「中斷最差」的直覺關係相反；fixture 為合成值，改 p 屬
任務範圍內（fixtures/ 為 allowed_paths）。

## 4. Stockpyl 1.0.2 對單一 PO 的忠實對應（fit/gap）

先做了唯讀 API／state_vars 檢查與 spike 實測（`/tmp/spike_single_po.py`、
`/tmp/spike_single_po2.py`、`/tmp/spike_rq.py`、`/tmp/spike3.py`），結論：**可以忠實表示
單張既有 PO**，方式如下（全部為公開 API）：

- `sim.initialize(network, horizon, rand_seed)` + 逐期
  `sim.step(network, order_quantity_override={0: {None: {None: qty}}}, consistency_checks="W")`
  + `sim.close(network)`。docstring 明示三者依序呼叫等同 `sim.simulation()`，且
  `order_quantity_override` 是給外部（如 RL）逐期控制訂單的公開參數。
- 單節點外部供應（supply_type="U"），`shipment_lead_time=lead`、`order_lead_time=0`；
  t=0 override 下唯一一筆 Q（t+lead 到貨），其餘期 override=0 → **horizon 內只有一次到貨**，
  以 `state_vars.inbound_shipment` 逐期實測確認（三情境皆恰好一個正數到貨期，期數=lead）。
- `inventory_policy` 是 Stockpyl `initialize()` 的強制要求（每個節點必須有 policy），
  且 `order_quantity_override` **只覆寫原物料採購**、不覆寫 FG 訂單——FG 訂單只能由
  policy 產生（sim.py 公開原始碼：policy 訂單寫入 `order_quantity_fg` 與
  `pending_finished_goods`，override 只改 `order_quantity`）。
- 舊版用 `rQ(reorder_point=1e12, order_quantity=Q)` 把有限常數 1e12 冒充「無限」，
  是錯誤前提：當 initial_stock > 1e12 時 policy 不下 FG 訂單，t=lead 到貨的 RM 因
  `share_frac = order_quantity_fg(t=0)/raw_order(t=0) = 0` 無法當期轉成可用 FG，
  要到下一期經 units_ordered==0 的等分分支才轉（GPT-6 大 initial_stock 反例重現：
  baseline t=1 IL=1000000000280、Stockpyl=1000000000080，總成本少 200）。
- 第二修正回合改為 **policy 決策與每期 override 同步**：rQ 的觸發條件是
  `inventory_position <= reorder_point`（policy.py 公開原始碼），故逐期切換公開屬性
  `reorder_point`：t=0 設 `+inf`（必訂 Q 一筆）、t>=1 設 `-inf`（永不訂）。如此
  t=lead 到貨時 share_frac=Q/Q=1，RM 到貨當期全數轉為可用 FG；t>0 的
  `order_quantity_fg` 全為 0，期末 `pending_finished_goods` 與
  `raw_material_inventory` 皆為 0（逐期簿記見 result.order_bookkeeping）。
- 模擬結束做映射後置條件檢查（單一 PO 於 t=lead 到貨一次、t>0 無 FG 訂單、期末
  pending/raw 歸零）；任一違反即拋 RuntimeError（fail-closed），不靠隱藏上限或不實
  文件放行。此對應依賴 Stockpyl 1.0.2 的 rQ 語意；若未來版本改變，後置條件會擋下。


spike 實測結果（三情境 state_vars IL、fill、持有、缺貨、總成本）與第 3 節手算
完全一致，且逐期 `inbound_shipment>0` 的期數 = {1}、{2}、{3}。

Fit／Gap 記錄（誠實版）：

| 項目 | 狀態 |
|---|---|
| 單一 SKU／單一 PO（明示 Q 與到貨期，單次到貨） | fit（兩引擎數值一致，容差 1e-9 內 0 差異；到貨事件一致） |
| 到貨語意（先補 backorder 再滿足需求） | fit（spike 與手算一致） |
| 到貨可用時間（t=lead 到貨當期即為可用成品） | fit（第二修正回合；含大 initial_stock 反例：initial_stock=1000000000100、lead=1/2/3 逐期 IL 與成本均與 baseline 一致） |
| 無殘留政策訂單簿記 | fit（第二修正回合；逐期 order_bookkeeping 證明 t>0 order_quantity_fg=0、期末 pending/raw=0；模擬結束有 fail-closed 後置條件檢查） |
| Stockpyl 對應方式 | fit，但依賴手動 initialize/step/close + order_quantity_override + rQ `reorder_point` 公開屬性逐期切換（公開 API、docstring/原始碼明示用途）；不使用 `sim.simulation()` 包裝 |
| 隨機需求（type='N'） | 只有 Stockpyl 引擎可跑（固定 seed）；baseline 不支援。gap：N 下兩引擎無法對照 |
| 總需求為零時的 fill_rate | gap（已知定義差異，非映射失敗）：total demand=0 時 baseline 回 `None`（0/0 比例無法定義），Stockpyl state_vars 回 `1.0`。reviewer 獨立重跑的 512 組固定需求邊界案例中，兩引擎唯一差異即此 64 個零需求案例的 fill_rate 定義差異；其餘庫存、缺貨、成本與簿記全部一致 |
| 中斷語意 | 以 PO 到貨期延長承載（t=1/2/3）；未用 DisruptionProcess（M/E/OP/SP/TP/RP）。gap：真實中斷（暫停出貨、隨機 Markov 中斷）未涵蓋 |
| 輸入型別/有限性/值域 | 共同 validate 層 fail-closed（第二修正回合）：horizon 必須 int（拒 float/bool）、po_quantity/initial_stock/成本必須有限數值、lead 必須有限整數值且 < horizon、demand_list 逐項有限非負；validate() 對任意型別不拋例外 |
| 多階 BOM／網路 | 不支援（刻意不做） |
| 正式資料 | 需求分布、成本、正式 lead time repo 內不存在；所有數值皆為明示合成假設（assumption=True + 來源），非企業事實 |

## 5. 修正回合（GPT-6 CHANGES_REQUIRED 後）逐項狀態

嚴格以新 RED→GREEN 進行。C1、C3、C4、C5、C6 共**五組** RED（C2 是首次即通過的
測試強化，如實無 RED，見下表）。現存 RED 檔包含 pytest 原始輸出與實際 exit code
（以 `tee` 輸出 + `PIPESTATUS` 記 exit code，非事後補造），但**沒有保存當時的完整
執行命令**——這是歷史證據限制，本文件不偽稱有；檔案位置見 §10。

| 組 | verdict 問題 | 本回合 RED 證據（exit code 實測） | 修正後狀態 |
|---|---|---|---|
| 1. Comparator P1 | 長度不符/單邊空 → match=True、max_abs_diff=0；單邊 None、stockout_periods、demand_total_realized 未列入 reason | `/tmp/dag-fix-red-c1.txt`：6 failed, exit=1（含「reason 仍宣稱全部一致」的重現） | 已修正：長度不符或任一邊空 → match=False、max_abs_diff=None；所有已比較 scalar（含 stockout_periods、demand_total_realized、first_stockout_period）任一 mismatch 或單邊 None → reason 逐項列出欄位名；另有逐欄位斷言測試 |
| 2. Seed 測試弱點 | 用 canonical bytes（內含 seed）證明 seed 影響數值 | 無 RED（如實）：強化測試 `test_seed_changes_actual_numeric_trajectory` 首次執行即通過（`/tmp/dag-fix-c2-first-run.txt`：1 passed, exit=0）——實作本就以 rand_seed 驅動 N 需求，弱點在舊測試本身；舊弱測試已刪除 | 已修正（測試層）：直接比較 demand_total_realized／inventory_levels／total_cost 數值軌跡；同 seed 位元組一致仍由 acceptance #1 測試覆蓋 |
| 3. 單一 PO P1 語意 | 實作是無限供應 + BS 每期補貨，po_id 只是標籤 | `/tmp/dag-fix-red-c3.txt`：7 failed, exit=1（契約缺 po_quantity 欄位：TypeError） | 已修正：契約新增 po_quantity（明示 Q）與到貨 timing（lead_time_days 整數、<horizon）；baseline 與 Stockpyl 都改成單次到貨語意；po_arrivals 記錄到貨事件（三情境皆恰一次）；po_id/po_quantity 進結果、canonical、保存檔名與比較；三 fixture 手算重算（§3）；未偷換成「單一 PO 類型」或持續補貨 policy |
| 4. LLM 硬邊界 P1 | llm.explain(run) 拿到正式 run 的可變參照，可原地改數值 | `/tmp/dag-fix-red-c4.txt`：2 failed, exit=1（重現 fill_rate=999.0、改後拋錯 total_cost=-1.0 殘留） | 已修正：llm.explain 收到 `copy.deepcopy(run)` 快照；adapter 改 baseline/stockpyl 數值、巢狀 inventory_levels、inputs，或改完拋錯，正式 run 完全不變（兩個測試直接覆蓋） |
| 5. 輸入與假設 P2 | lead_time_days=1.5 被 int(round()) 成 2；initial_stock/base_stock 未列假設 | `/tmp/dag-fix-red-c5.txt`：2 failed, exit=1（validate ok=True for 1.5；assumptions 缺 initial_stock/po_quantity） | 已修正：共同 validate 層 fail-closed（1.5 → needs_input，baseline 與 Stockpyl 一致）；`_assumptions_for` 補上 initial_stock、po_quantity（base_stock_level 已隨單一 PO 語意從契約移除）；六類合成輸入全部 assumption=True+source |
| 6. Persist 路徑 P2 | case_id/scenario_id 直接組入檔名，可越過 out_dir | `/tmp/dag-fix-red-c6.txt`：1 failed, exit=1（DID NOT RAISE） | 已修正：識別碼白名單 `[A-Za-z0-9_.-]{1,64}`、拒絕 `.`/`..`/`..`前綴/分隔符/絕對路徑 + resolved containment 檢查；測試涵蓋 case_id 與 scenario_id 各 8 種穿越輸入，斷言 out_dir 內零寫出 |

修正回合完整測試：`/tmp/dag-fix-green-full.txt`（36 passed, exit=0，見 §10）。

### 5.1 中間失敗與歷史證據的如實紀錄

- 修正前既有的兩次中間失敗（後續轉綠，正常歷史，如實補列）：
  - `/tmp/dag-evidence-green1.txt`：1 failed, 4 passed, 15 deselected in 0.74s；
    costs_provided 預期 True 實際 False（後由 green1b 轉綠）。
  - `/tmp/dag-evidence-green3.txt`：1 failed, 1 passed, 18 deselected in 1.67s；
    比對 abs_diff=0.475 > 1e-9（後由 green3b 轉綠）。
- 修正前七份 RED（`/tmp/dag-evidence-red1..7.txt`）當時**沒有保存完整執行命令與實際
  exit code**，這是歷史證據限制：本報告不偽造其 exit code，只標明其失敗數
  （5、3、2、3、4、2、2）與失敗類型（功能骨架 NotImplementedError、fixture 不存在、
  行為 AssertionError；經 GPT-6 核對無 import error／缺依賴／skip 充數）。
  第一修正回合的五份新 RED 全部有實際 exit code（§5 表格）；其完整執行命令
  未保存（限制如 §5 開頭所述）。

## 6. 六項 acceptance tests 逐條現況（只列有測試證據者）

1. 同輸入/版本/seed 重跑 canonical bytes 一致（排除執行時間；含 po 欄位）→
   通過（test_deterministic_fixture_rerun_is_byte_stable、test_seeded_stochastic_rerun_is_byte_stable、
   test_canonical_bytes_exclude_execution_metadata）。
2. 手算案例驗算庫存/首次缺貨/成本 + 事前固定容差 + 差異與原因 →
   通過（test_single_po_baseline_hand_calc_normal/substitute/disruption、
   test_stockpyl_matches_baseline_within_tolerance、compare_results 附 reason）。
   comparator 對非有限/不可比較值 fail-closed（第二修正回合）：
   test_comparator_nan_at_index_fails_closed（NaN 於首/中/末索引）、
   test_comparator_infinity_single_sided/double_sided_fails_closed、
   test_comparator_non_comparable_value_fails_closed_without_raise、
   test_comparator_scalar_nan/infinity_fails_closed_and_listed →
   match=False、max_abs_diff/abs_diff=None、reason 列欄位與索引。
   第四修正回合補齊容器契約：dict inventory_levels 不再被轉成鍵列表假一致、
   po_arrivals 驗證容器/二欄結構/期數/有限數量（None/NaN/Infinity/錯誤 tuple →
   結構化 mismatch），見 §13。
3. 三 fixture 預期關係先寫測試 → 通過（test_fixture_expected_relationships：fill
   1.0 > 0.975 > 0.925、成本 2000 < 2055 < 2620、三 fixture 皆一次到貨；只對受控
   fixture 驗證，不宣稱任意隨機模型單調）。
4. LLM 呼叫即失敗仍完成數值計算；LLM 不能覆寫數值（含原地改、巢狀改、改後拋錯）→
   通過（test_failing_llm_does_not_block_numeric_results、test_llm_cannot_overwrite_numeric_fields、
   test_llm_receives_snapshot_cannot_mutate_run、test_llm_mutation_then_exception_does_not_affect_results）。
5. 缺必要需求/交期/PO 欄位 → needs_input；成本缺失不回推測成本；非整數交期 fail-closed；
   合成假設標示來源 → 通過（needs_input ×3 + test_fractional_lead_time_fails_closed_in_common_validation、
   test_missing_cost_is_not_guessed、test_provided_synthetic_assumptions_are_marked_with_source、
   test_synthetic_fixture_inputs_all_marked_as_assumptions）。
   第二修正回合補齊共同 validate 層：demand_list 逐項（None/NaN/負數）、horizon float、
   lead/po_quantity 字串、NaN/Infinity 輸入 → needs_input 且 validate/引擎不拋未捕捉例外
   （test_validation_* 十項，見 §11）。
   第四修正回合：超大逐期需求（demand_list=[10**10000]）與超大 demand type →
   有界安全描述（_safe_repr）、needs_input=["demand"]、兩引擎空數值結果，見 §13。
6. 保存實際執行結果、版本、seed、失敗項目；路徑穿越防護 → 通過
   （test_persist_and_reload_run、test_persist_records_failures、
   test_persist_rejects_path_traversal_identifiers、test_persist_valid_ids_stay_inside_out_dir）。
   第四修正回合：被契約拒絕的 NaN/Infinity/超大 int 輸入可保存標準 JSON 失敗紀錄
   （rejected 標記含欄位、原值類型、拒絕原因；版本與 seed 隨檔），讀回 canonical
   位元組穩定（test_persist_rejected_* 四項，見 §13）。

## 7. 已知限制與未驗證項目

- 只驗證受控固定需求 fixture；隨機需求下兩引擎的數值一致性、多次 trial 統計
  （信賴區間、分位數）未做。
- 總需求為零時 fill_rate 的定義差異（baseline=None、Stockpyl=1.0）已記錄於 §4
  fit/gap；本 DAG 未統一為單一定義。
- Stockpyl 的 DisruptionProcess（M/E 型中斷、OP/SP/TP/RP）未接、未測。
- baseline 只支援 demand type='D'；N 型需求只有 Stockpyl 引擎（固定 seed）。
- Stockpyl 對應依賴手動 initialize/step/close 驅動 + rQ `reorder_point` 公開屬性
  逐期切換（+inf/−inf）；未用 `sim.simulation()` 包裝（它不接受 order_quantity_override）。
  若 Stockpyl 未來版本改變 rQ 觸發語意，adapter 的映射後置條件檢查會 fail-closed 拋錯
  （不會靜默產出錯位數值），但本 DAG 未在 1.0.2 以外的版本驗證。
- 未做 UI、ERP 寫回、資料庫整合；`backend/scenario_repository.py`（vault 規劃的
  第三個檔案）未建，本次以 `persist_run/load_run` + JSON 檔取代（範圍內最小實現）。
- 既有 327 項回歸以隔離 venv 執行並排除新增檔（`tests/ --ignore=tests/test_scenario_engine.py`）；
  本回合實測同 venv 亦能直接執行全部 327 項且全過（§11 證據）。第一修正回合的最終
  隔離執行紀錄見 §10；其完整執行命令未保存（歷史限制，見 §5 開頭）。

## 8. 停止條件狀態（誠實版）

- 核准 spec 的停止條件為「達到 90 分鐘（累計）即停止」。現存證據**無法證明**各
  回合的完整累計工時，本文件不宣稱「未觸發」，也不把 90 分鐘改寫為每回合重置
  （2026-09-13 版曾在此宣稱「未觸發」，已撤銷）。
- 可核對的事實：Stockpyl 1.0.2 經唯讀檢查與 spike 實測確認可忠實表示單張既有 PO
  （§4），兩引擎在單一 PO 語意下數值一致；無需猜補正式資料；未越界修改任何
  非授權路徑；未 commit/push/PR（git status 可查）。
- 第四修正回合（2026-09-14）係由使用者於 Discord 明確要求完成 reviewer 獨立重現
  的剩餘缺口（comparator 容器契約、validation 安全訊息、運算後非有限 fail-closed、
  失敗輸入可保存、文件事實修正）；未要求的項目維持不啟動。
- 第二修正回合曾評估「Stockpyl 公開 API 無法忠實對齊 → BLOCKED」的選項：實測後
  找到公開 API 內的忠實對應（order_quantity_override + rQ reorder_point 公開屬性
  逐期切換，§4），加上映射後置條件 fail-closed 檢查，故不需 BLOCKED、也不靠隱藏
  上限或不實文件放行。
- 修正前「語意已對齊、六項全部通過」的無保留宣告已撤銷；本版所有通過宣告均附
  測試名稱與證據檔（§6、§10、§11、§12、§13），未驗證項目列於 §7。

## 9. 重跑指令（完整）

以下為本文件撰寫當下可重跑的命令（含第二修正回合的隔離驗收執行，全部為實際執行過
的命令；第一修正回合歷史 RED 的完整命令未保存，限制見 §5 開頭，不補造）。

```
# 0) 隔離 venv（只裝 stockpyl + pytest）
python3 -m venv /tmp/scn-stockpyl-venv
/tmp/scn-stockpyl-venv/bin/pip install -r requirements-scenario.txt

# 1) 情境測試全檔
cd /home/kali/.hermes/daily-agent/worktrees/DAG-20260912-7edaeaf2
/tmp/scn-stockpyl-venv/bin/python -m pytest tests/test_scenario_engine.py -v

# 2) 既有回歸（排除新增檔）
/tmp/scn-stockpyl-venv/bin/python -m pytest tests/ --ignore=tests/test_scenario_engine.py

# 3) 隔離驗收執行（第二修正回合實際執行）：
#    env -i = 零環境變數（API keys 清空、ERP_DB_PATH 未設定）；
#    PYTHONPATH=/tmp/dag-network-guard = sitecustomize 阻斷 outbound socket/DNS。
#    （tests/conftest.py 於 backend import 前把 ERP_DB_PATH 指到 /tmp 暫存目錄，
#      其餘與上面第 1、2 步完全相同。）
env -i PYTHONPATH=/tmp/dag-network-guard /tmp/scn-stockpyl-venv/bin/python -m pytest tests/test_scenario_engine.py -v | tee /tmp/dag2-final-new.txt; echo "exit=${PIPESTATUS[0]}" >> /tmp/dag2-final-new.txt
env -i PYTHONPATH=/tmp/dag-network-guard /tmp/scn-stockpyl-venv/bin/python -m pytest tests/ --ignore=tests/test_scenario_engine.py | tee /tmp/dag2-final-regression.txt; echo "exit=${PIPESTATUS[0]}" >> /tmp/dag2-final-regression.txt

# 4) 網路阻斷自證（預期 RuntimeError: outbound network blocked）
env -i PYTHONPATH=/tmp/dag-network-guard /tmp/scn-stockpyl-venv/bin/python -c "import socket; socket.create_connection(('1.1.1.1', 80))"

# 5) 空環境自證（預期 {}）
env -i PYTHONPATH=/tmp/dag-network-guard /tmp/scn-stockpyl-venv/bin/python -c "import os; print(dict(os.environ))"
```

## 10. 證據檔位置

- 修正回合新 RED（含實際 exit code）：`/tmp/dag-fix-red-c1.txt`（6f,1）、
  `/tmp/dag-fix-red-c3.txt`（7f,1）、`/tmp/dag-fix-red-c4.txt`（2f,1）、
  `/tmp/dag-fix-red-c5.txt`（2f,1）、`/tmp/dag-fix-red-c6.txt`（1f,1）。
- seed 強化測試首次執行（無 RED，如實）：`/tmp/dag-fix-c2-first-run.txt`（1 passed, exit 0）。
- 修正回合 GREEN：`/tmp/dag-fix-green-corrections.txt`、`/tmp/dag-fix-green-c6.txt`、
  `/tmp/dag-fix-green-c46.txt`、`/tmp/dag-fix-green-full.txt`（36 passed, exit 0）。
- 最終隔離執行（env -i：API keys 清空、ERP_DB_PATH 未設定；sitecustomize 阻斷
  outbound network；見 §9 第 3 步的完整命令）：
  `/tmp/dag-fix-final-new.txt`（36 passed, exit 0）、
  `/tmp/dag-fix-final-regression.txt`（327 passed, exit 0）、
  `/tmp/dag-fix-final-evidence.txt`（canonical sha256／差異紀錄／stochastic seed 對照）。
- 新語意保存檔：`/tmp/scn-results-fix/CASE-SKU-001-{normal,substitute,disruption}-*.json`。
- 第二修正回合（2026-09-13）證據：`/tmp/dag2-red-r2.txt`（新測試 RED：23 failed,
  exit=1）、`/tmp/dag2-green-r2.txt`（新測試 GREEN：24 passed, exit=0）、
  `/tmp/dag2-green-full-v.txt`（全檔 60 passed, exit=0）、`/tmp/dag2-regression.txt`
  （327 passed, exit=0）、`/tmp/dag2-probe-huge-before.txt`／`/tmp/dag2-probe-huge-after.txt`
  （大 initial_stock 反例修正前後）、`/tmp/dag2-final-new.txt`、`/tmp/dag2-final-regression.txt`
  （隔離驗收，見 §11）。
- 修正前歷史證據（限制見 §5.1）：`/tmp/dag-evidence-red1..7.txt`、
  `/tmp/dag-evidence-green*.txt`、`/tmp/dag-evidence-full-new.txt`、
  `/tmp/dag-evidence-regression*.txt`。
- Stockpyl 唯讀 spike：`/tmp/spike_single_po.py`、`/tmp/spike_single_po2.py`、
  `/tmp/spike_rq.py`、`/tmp/spike3.py`。

## 11. 第二修正回合（2026-09-13，GPT-6 最終 verdict）逐項狀態

以新 RED→GREEN 進行。新測試共 24 項（23 failed→GREEN 的 RED 證據 + 1 項既有行為
回歸 pin）。如實紀錄：本回合產生「24 selected、36 deselected」那組 RED/GREEN 的
**實際選擇命令（-k 選擇參數）未保存**；§10 所列 `dag2-red-r2.txt`／
`dag2-green-r2.txt` 為 pytest 原始輸出與 exit code（tee+PIPESTATUS），§9 列的是
全檔／回歸／最終隔離命令，均非該組的實際選擇命令。此為歷史證據限制，不補造。

| 組 | verdict 問題（最終） | 本回合 RED 證據（exit code 實測） | 修正後狀態 |
|---|---|---|---|
| 1. Comparator 非有限值 P1 | NaN 在後續索引被 max() 吞掉 → match=True、max_abs_diff=0.0；scalar NaN/inf 給出 nan/inf abs_diff；不可比較值拋 TypeError | `/tmp/dag2-red-r2.txt`：23 failed, exit=1（含重現「NaN@idx=1 → match=True」與字串拋 TypeError） | 已修正：比較前逐元素驗證有限數值；任一非有限/不可比較 → match=False、max_abs_diff=None（inventory_levels）/abs_diff=None（scalar），reason 逐項列出 `inventory_levels[i]` 與兩側值或 scalar 欄位名。測試覆蓋 NaN 於索引 0/1/2、單邊/雙邊 Infinity、字串、scalar NaN/inf |
| 2. 輸入契約 P2 | demand_list[1]=None → validate ok=True、引擎 TypeError 且不回 needs_input；horizon=20.0 被接受；lead/po_quantity 字串讓 validate 拋 TypeError；NaN/Infinity 被接受；負需求被接受 | 同 `/tmp/dag2-red-r2.txt`（23 failed 含上述重現） | 已修正：共同 validate 層先查型別/有限性/整數語意/值域——horizon 必須 int（拒 float/bool）、po_quantity/initial_stock/成本必須有限數值、lead 必須有限整數值且 < horizon、demand_list 逐項有限非負；validate() 對任意型別不拋例外；所有不合約輸入 → 對應 needs_input，兩引擎回空結果不拋未捕捉 TypeError。engine_version → 1.2.0 |
| 3. Stockpyl 簿記與可用時間 P2 | FG 政策訂單每期 200、期末 pending=3800（未揭露簿記）；1e12 冒充無限 → initial_stock>1e12 時 RM 到貨當期不轉 FG（可用庫存晚一期、成本少 200） | 同 `/tmp/dag2-red-r2.txt`（大 initial_stock 三 lead 全錯位、order_bookkeeping 不存在）＋`/tmp/dag2-probe-huge-before.txt`（t=1 IL 差 200、total_cost 差 200） | 已修正：policy 決策與每期 override 同步（rQ `reorder_point` 公開屬性逐期切換 +inf/−inf），t=lead 到貨當期轉為可用 FG；逐期簿記（order_quantity_fg/pending/raw）進 result.order_bookkeeping；模擬結束做映射後置條件 fail-closed 檢查。測試同時核對到貨、可用庫存與簿記；大 initial_stock 反例（lead=1/2/3）修正後兩引擎逐期一致（`/tmp/dag2-probe-huge-after.txt`）。未動用 BLOCKED——公開 API 即可忠實對齊（§4、§8） |
| 4. 證據敘述 | C2 非 RED 卻被算進「六組 RED」；「RED 皆保存完整命令」不實；§11 引用不存在；隔離配方未完整列出 | —（文件修正，無測試） | 已修正：§5 改為五組 RED、明示 RED 檔無完整執行命令的歷史限制；所有 §11 引用改指 §10／新增本節；§9 列全隔離命令（本回合實際執行） |

本回合 GREEN 證據：`/tmp/dag2-green-r2.txt`（新測試 24 passed, exit=0）、
`/tmp/dag2-green-full-v.txt`（全檔 60 passed, exit=0）、`/tmp/dag2-regression.txt`
（既有回歸 327 passed, exit=0）、`/tmp/dag2-final-new.txt`、`/tmp/dag2-final-regression.txt`
（env -i 隔離驗收）。

如實紀錄：新測試中 `test_validation_lead_time_infinity_rejected_not_overflow` 在 RED 前
即通過（舊 validate 對 inf lead 已回 needs_input），本回合將之定位為回歸 pin 而非新 RED；
RED 統計 23 failed 不包含它。三 fixture 與既有 36 項測試的數值在本回合修正後逐項未變
（GREEN 全檔 60 passed 含全部既有斷言）。

## 12. 第三修正回合（2026-09-13，父 Agent 獨立探針反例）逐項狀態

背景：父 Agent 以 `/tmp/DAG-20260912-7edaeaf2-parent-adversarial.py` 獨立探針實測，
修正前 6 類反例全部 `raised=true`（3× OverflowError: int too large to convert to float、
1× TypeError: 'int' has no len()、1× AttributeError: 'object' has no attribute 'type'、
1× TypeError: 'NoneType' is not iterable），exit=1。本回合為最小補修，不擴需求、
不變更契約語意，engine_version 維持 `1.2.0`。

| 反例 | 修正前（父層探針實測） | 修正後（探針重跑實測） |
|---|---|---|
| po_quantity=10**10000 | OverflowError | raised=false、needs_input=["po_quantity"] |
| initial_stock=10**10000 | OverflowError | raised=false、needs_input=["initial_stock"] |
| holding_cost=10**10000 | OverflowError | raised=false、needs_input=["holding_cost"] |
| demand_list=123（int） | TypeError: no len() | raised=false、needs_input=["demand"] |
| demand=object() | AttributeError: no 'type' | raised=false、needs_input=["demand"] |
| inventory_levels=None | TypeError: not iterable | raised=false、match=false |

最小修正（三處）：

- `_is_finite_number`（scenario_contract.py）：int/float 先 `float(x)` 轉換並捕捉
  OverflowError/ValueError，超大 int 回 False（不合約值），不再讓 math.isfinite 對
  超大 int 拋例外。
- `validate()`（scenario_contract.py）：demand 先 `isinstance(d, DemandSpec)`、
  demand_list 先 `isinstance(..., list)` 才存取 `.type`、`len()` 或迭代；錯型別回
  `needs_input=["demand"]`。
- `compare_results()`（scenario_engine.py）：inventory_levels 經 `_coerce_il` 轉 list；
  None 或非 iterable → match=False、max_abs_diff=None、reason 明列欄位與原因。

本回合 TDD（完整命令同 §9 隔離配方；RED/GREEN 皆為 env -i + network guard 實跑，
tee+PIPESTATUS 記 exit code）：

- 新增測試 8 項（6 個函式；inventory 案例 parametrize None／123／"ab"）。
- RED：`/tmp/DAG-20260912-7edaeaf2-fix-red-r3.txt` → **7 failed, 61 passed, exit=1**。
  如實紀錄：`"ab"`（不合約容器）在 RED 前即通過——舊長度不符分支本就 fail-closed，
  定位為回歸 pin，7 failed 不含它（與 §11 的 lead_time inf pin 同理）。
- GREEN：`/tmp/DAG-20260912-7edaeaf2-fix-green-r3.txt` → **68 passed, exit=0**
  （全檔 = 既有 60 + 新增 8；既有 60 項數值未變）。
- 既有回歸：`/tmp/DAG-20260912-7edaeaf2-fix-regression-r3.txt` → **327 passed, exit=0**
  （`tests/ --ignore=tests/test_scenario_engine.py`）。
- 父層探針重跑：`/tmp/DAG-20260912-7edaeaf2-fix-probe-after-r3.txt` → 6 案例全
  raised=false、failures=0、**exit=0**。執行時 PYTHONPATH 需含 worktree 根目錄
  （探針 script 位於 /tmp，`import backend` 依賴 sys.path）：
  `env -i PYTHONPATH=<worktree>:/tmp/dag-network-guard /tmp/scn-stockpyl-venv/bin/python /tmp/DAG-20260912-7edaeaf2-parent-adversarial.py`

計數變動：`tests/test_scenario_engine.py` 60 → **68** 項；既有回歸 327 項不變。

## 13. 第四修正回合（2026-09-14，reviewer 獨立重現之剩餘缺口）逐項狀態

背景：獨立 reviewer 對 1.2.0 版重跑（情境 68 passed、既有回歸 327 passed、
獨立反例 6 failed、exit=1），六個反例全部重現：dict inventory_levels 假一致、
inf 到貨 deepcopy 假一致（None 到貨拋 TypeError）、`demand_list=[10**10000]` 讓
validate 在訊息建構時拋 ValueError、`holding_cost=1e308` 產出 total_cost=inf 卻
`failures=[]`、NaN／超大 int 失敗輸入無法保存。本回合只修這四類契約/persist 缺口
與文件事實邊界，不擴 UI／ERP／MCP／DBOS／BOM。engine_version → **1.3.0**。

本回合 TDD：每類先以最小失敗測試 RED、再修正 GREEN（隔離配方同 §9，另加
`PYTHONPATH=<worktree>:/tmp/dag-network-guard`、`PYTHONDONTWRITEBYTECODE=1`、
`ERP_DB_PATH=/tmp/dag4-erp`；tee+PIPESTATUS 記 exit code）。

| 類 | 缺口 | RED 證據（exit code 實測） | 修正後狀態 |
|---|---|---|---|
| 1. comparator 容器契約 | dict inventory_levels 被 list() 轉成鍵列表假一致；inf/nan 到貨 deepcopy 假一致；po_arrivals=None 拋 TypeError；錯誤二欄結構（3 欄 tuple、1 欄、字串期數、非整數期數、負期數、dict 欄位）被當相等 | `/tmp/dag4-red-c1.txt`：11 failed, exit=1 | 已修正：inventory_levels 只接受契約數值 list（dict/str/任意 iterable → mismatch，不轉鍵列表）；po_arrivals 驗證容器、每筆二欄 list、期數非負整數值、數量有限；非法 → match=False、reason 列欄位/位置，不拋 TypeError、不宣稱 engines agree。RED 中如實無 pin（nan 反例因 deepcopy identity shortcut 亦屬假一致，全部為真 RED） |
| 2. validation 安全訊息 | `demand_list=[10**10000]` 在 `{x!r}` 建構訊息時拋 ValueError（int_max_str_digits）；超大 demand.type 同 | `/tmp/dag4-red-c2.txt`：2 failed, exit=1 | 已修正：新增 `_safe_repr`（有界、repr 失敗回型別/位元描述）；validate 三處訊息改用；run_scenario 對 validate 加防禦性 fail-closed 包覆。超大逐期需求 → needs_input=["demand"]、兩引擎空數值結果；未提高全域 int 限制。RED 中 1 項（超大 lead_time，其分支未嵌值）為既有行為回歸 pin |
| 3. 運算後非有限 | holding_cost=1e308（輸入全有限、validate().ok=True）→ holding/mean/total=inf、failures=[] 成功外觀 | `/tmp/dag4-red-c3.txt`：1 failed, exit=1 | 已修正：兩引擎回傳前 `_assert_result_finite` 全欄位（scalar、inventory_levels、po_arrivals、order_bookkeeping）有限性後置條件；非有限 → ValueError（numerical failure），run.failures 記錄、該引擎結果 None；不支援天文規模運算，fail-closed |
| 4. 失敗輸入可保存 | initial_stock=nan / po_quantity=inf / po_quantity=10**10000 / demand=[inf] 被契約拒絕後，persist json.dumps 拋 ValueError（nan / Out of range / 4300 digits） | `/tmp/dag4-red-c4.txt`：4 failed, exit=1 | 已修正：`_json_safe_input`／`_json_safe_value` 為被拒絕值建立 rejected 標記（rejected=True、field、original_type、original_value、reason），allow_nan=False 不變、產出標準 JSON；seed/input_version 超大拒組檔名；`_empty_result` 不再把非有限 po_quantity 帶進結果；load_run 對不可重建 demand 容錯。讀回 canonical 位元組穩定（重存檔案位元組一致） |

本回合 GREEN 證據：`/tmp/dag4-green-c1..c4.txt`（11/3/1/4 passed, exit=0）；
全檔 `/tmp/dag4-final-new.txt`（**87 passed, exit=0**）；既有回歸
`/tmp/dag4-final-regression.txt`（**327 passed, exit=0**）；獨立探針
`/tmp/dag4-probe-after.txt`（reviewer 六反例 5 組探針全過, failures=0, exit=0；
`/tmp/dag4-probe.py` 可重跑）。既有 68 項情境測試的數值斷言逐項未變（全檔 87 =
既有 68 + 新增 19；新增 19 含 parametrize 展開）。

文件事實修正（本回合）：
- §4 fit/gap 補記總需求為零時 fill_rate 的已知定義差異（baseline=None、
  Stockpyl=1.0；reviewer 512 組邊界案例中唯一差異，非映射失敗）；§7 同步。
- §11 撤銷「本回合 RED/GREEN 完整命令見 §9」：24 selected / 36 deselected 那組
  的實際選擇命令未保存，如實標示（§10 檔案僅有 pytest 輸出與 exit code）。
- 首頁狀態與 §8：90 分鐘停止條件為累計制，不再改寫成「每回合重置」；撤銷
  「未觸發」宣告，累計工時證據不足，後續由使用者明確要求完成。
- 不補造任何歷史。

計數變動：`tests/test_scenario_engine.py` 68 → **87** 項；既有回歸 327 項不變。
