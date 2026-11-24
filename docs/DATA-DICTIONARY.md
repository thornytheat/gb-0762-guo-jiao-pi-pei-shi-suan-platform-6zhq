# 化验批、试配约束与库存定义

`assays.csv` 必填 `lot,expires,stock,unit,moisture_pct,gel_sag`，列序可变。批号为原形字符串。有效期记日历日期 `YYYY-MM-DD`，到期日当日仍可用。日粒度按试配记录所在工厂日历解释。`stock` 为 g 或 kg，统一成整克。水分按百分数录入，保留至 0.01%；凝胶度为 SAG，可录至千分之一。UTF-8 允许首行 BOM。数值接受合法千分位及全角数字。空单元格、空值哨兵、JSON null 分别由相应校验识别。

空文件、全空行、仅表头分别有 `EMPTY_FILE`、`BLANK_FILE`、`HEADER_ONLY`。原件行号是记录起始物理行，原值保留。相同批号按归一日期、库存、水分、凝胶度比较，相同保留首个来源，冲突整组隔离。每份 CSV 至多 250,000 字符。

计划 `as_of` 为试配日，`target_g` 和 `step_g` 为正整数。前者必须可被后者整除，且领用步数最多 60。`dry_min_g` 为干物下限，`gel_low_sag` 和 `gel_high_sag` 均含界。可选 `proposal` 是批号到非负整克的映射。公式用万分克整数累计干物，乘毫 SAG 得凝胶矩，精确叉乘验上下限，展示值四舍五入为 3 位小数。

寻优用到期日、批号排序，以最少领用批为首要目标，同批数优先最大化较早批次的用量。超出 14 个可用整步批或 300,000 状态上限时报错。不可行时整单留待处理，库存保持原量。结算库存包含已采信但过期与不足整步的库存，冲突批计入隔离清单。

同份化验文本和同参数计算同一 SHA-256 标识。运行保存源摘要、计划、候选提案、UTC 建立时间、耗时、规则和逐阶段证据。归档经同目录临时文件写入和原子发布，重复提交返回既有快照。证据摘要以排序键、UTF-8 和紧凑 JSON 复算。

## 成本关账台账（M8）

台账保存在归档目录的 `cost-ledger.json`，与留档批次快照（64 位哈希文件名）同目录但互不混淆。`POST /api/closing/invoices` 接收 `lot, invoice_no, period, kind, quantity_g, amount_cents`（或 `amount_yuan`，两位小数转整数分），`version` 省略时自动递增。`kind` 仅 `ACTUAL`（正式）或 `PROVISIONAL`（暂估）；`period` 格式 `YYYY-MM`。每个事件记录 `posted_period`（开放月；晚到发票不在其业务账期入账）、`delta_cents`（相对同号上一版本的差额）与 `late` 标记。

`GET /api/closing` 返回 `closeable`/`waiting` 来料批分组、每批 `lines`（按单 `batch_id` 分摊行与 `REMAINDER` 余料行，`estimated` 标记暂估）、`closed_periods` 封账快照（`reported_yuan` 已核实、`provisional_yuan` 暂估挂账）和 `open_month` 差额。`POST /api/closing/close` 接收 `period`（须等于当前开放月）与 `next_period`（须更晚），冻结后开放月切换，旧月快照不再被任何后续事件改写。金额内部一律整数分累计，展示层转两位小数元。
