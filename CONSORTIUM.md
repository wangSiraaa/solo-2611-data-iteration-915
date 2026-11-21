# 联营柜台促销分账（consortium settlement）

在已有篮价服务之上叠加的一层：**真实成交 → 按柜台集合独立报价 → 可复查的整数分账**。
账内金额全部是合成积分，不涉及真实财务托管。

```bash
python3 -m basket_service consortium-demo   # 六个场景 + 29 项检查，写入 results/consortium_results.json
python3 -m basket_service self-check        # 全部 86 个测试（含分账求解器差分测试）
python3 -m basket_service run-plan results/consortium_plan.json --db data/plan.db
```

## 模型

* **联营体（consortium）**：绑定一个已发布的冻结快照（价格与券目录不再变化），
  固定一组柜台（稳定号，1..12 个，按字典序排序后作为分账向量的顺序），
  以及每张券的**同意条款**和每店**已确认最低应得数**。
* **实物件归属**：每个成交批（deal）把若干真实销售（ledger 里的 sale）的
  实物件按 `sale_id:unit_id` 映射到唯一柜台。映射、销售、快照在签约后都可查。
* **同意条款（consent）**：每张券 `must`（必须本人参加的柜台）+ `any_of`
  （若干组，每组任选其一参加）。券对一个柜台集合**合格**当且仅当
  同意条款成立 **且** 该集合当前在池实物覆盖了券所需 SKU ——
  即“实际选中的柜台提供了优惠所需实物”。
* **成交批（deal）与在池件（pool）**：deal 的在池件 = 其销售中**尚未退款**的件；
  可结算总额 `total` = 在池件上已记账的逐件折扣之和（来自 `sale_units` 的
  存储事实，不是重新报价）。退款到账的件从在池件中消失，
  不能支撑下一次报价和分账；已签分账冻结了当时的实物、券来源与分账，永久可查。

## 联合报价（coalition quote）

`quote_coalition(deal_id, counters)` 接受一个柜台集合，在**同一冻结快照**上：

1. 取该集合柜台的当前在池件作为篮；
2. 按同意条款 + SKU 覆盖过滤出合格券子集；
3. 交给报价服务 `quote_subset`（与全量报价完全相同的 bitmask 最优、
   逐件按比例分摊、两级破同规则）签出该集合**可单独实现**的整数节省。

每个联合名单的报价都独立优化、独立存档（`quotes` 表，确定性 request id，
重复查询按字节重放）；**绝不**从大联营已用券的总价里按比例抄一份给小组
（demo 里 `membership` 场景给出反证：{A,C} 独立报价 3，而按比例抄会是 4）。

## 分账（settlement）

`attempt_settlement(deal_id, request_id)` 对全部 2ⁿ−1 个非空小组逐一独立报价
得到 `v(S)`，然后求解：

```
x_i ≥ m_i（已确认底数）          Σ x_i = total（恰好分完）
对每组 S：Σ_{i∈S} x_i ≥ v(S)（任何一组退出联营自己报价也不会更好）
```

并列最优时：**先最小化最多与最少店之差，再按柜台稳定号的金额向量取字典序最小**。

* 能签：`status=SIGNED`，deal 关闭，分账、全部小组报价值、报价 request id 一并冻结。
* 不能签：`status=REJECTED`，交出**从真实独立报价来的矛盾约束**：
  * **Farkas 证书**（反例子集）：若干小组约束/底数约束/总额约束的整数权重，
    每个柜台的左式系数全部抵消为 0，右式却加出正数 —— 例如三张两店券场景：
    `AB≥4, AC≥4, BC≥4, 总额=4×(−2)` 即 `2Σx ≥ 12 > 8`；
  * **整数枚举证书**：分数松弛可行但整数不可行时，给出候选箱与全部约束，
    复核方可独立逐点枚举确认无可行分账。
  证书里每个 `v(S)` 都对应一条可重放的真实联合报价记录。

求解器（`basket_service/settlement.py`）全程 `Fraction` 精确有理数：
两阶段 Bland 单纯形（约束生成）+ 有限域 CP 搜索；测试中与独立穷举参考
（`reference.brute_force_split`）在 500+ 随机博弈上逐一比对一致。

## 复查与退出检查

* `check_no_exit(settlement_id)`：逐小组核对 `Σ_{i∈S} x_i ≥ v(S)`、
  底数与总额，12 柜台时检查全部 4095 个小组。
* `verify_settlement(settlement_id, deep=True)`：从原始记录独立重推 ——
  冻结池与不可变销售事实/柜台映射/底数版本对齐；用**独立穷举参考**
  （`reference.exhaustive_best`）重算每个小组节省并与存档报价逐一对照；
  用独立验证器复核证书算术或分账约束（小规模时与穷举最优分账逐字节一致）。
* 退款后旧分账依旧 `get_settlement` 可查、可验证；
  验证报告会单独列出“签约后退款的件”（信息项，不影响已签记录）。

## 场景 ↔ 需求对照（`results/consortium_results.json`）

| 场景 | 内容 | 结果 |
| --- | --- | --- |
| `pair_lock` | A/B/C 各一件 10，三对店券各减 4 互争实物；全员最优省 4，三个两店小组各可省 4 | 不能签：三对约束 ×1 + 总额 ×(−2) ⇒ `2Σx ≥ 12 > 8` |
| `grand_coupon` | 加签三店同意减 6 的券 | 签出 `(2,2,2)`，极差 0 |
| `minimum_blocks` | 同上成交与小组报价，仅 A 的底数置 3 | 不能签：`BC≥4 + A≥3 − 总额6 ⇒ 1 > 0`（总额 6 大于 3 也无济于事） |
| `membership` | 全场节省同为 6，券成员不同（A 独享券 3、BC 券 3、三店券 6） | 小组退出收益改变（v(A)=3、v(BC)=3、v(AC)=3≠4），签出 `(3,1,2)` |
| `refund` | 首签 `(1,2,3)` 后退掉下一批的一件 | 退款件退出在池，新一批总额 4 < v(BC)=5，不能签；旧分账仍可查可验 |
| `twelve_counters` | 12 柜台、6 张对券 5 + 一张全员券 40 | 签出八个 3、四个 4（极差 1、字典序最小），4095 个小组无退出 |

## API 摘要

```python
svc = open_service(path)
svc.create_consortium(id, snapshot_id, counters, consent, minimums)  # 幂等；内容冲突 CONSORTIUM_CONFLICT
svc.confirm_minimums(id, minimums)                                   # 底数版本化（审计链）
svc.create_deal(deal_id, consortium_id, sale_ids, unit_counters)     # sale 只能属于一个 deal
svc.quote_coalition(deal_id, counters, request_id=None)              # 可重复查询，确定性重放
svc.attempt_settlement(deal_id, request_id)                          # SIGNED / REJECTED(带证书)，幂等
svc.get_settlement(id); svc.check_no_exit(id); svc.verify_settlement(id, deep=True)
svc.get_consortium(id); svc.get_deal(id)
```

新增错误码：`INVALID_CONSORTIUM, INVALID_CONSENT, INVALID_MINIMUMS,
UNKNOWN_CONSORTIUM, UNKNOWN_DEAL, UNKNOWN_SETTLEMENT, CONSORTIUM_CONFLICT,
DEAL_CONFLICT, DEAL_CLOSED, SALE_ALREADY_DEALT, SALE_SNAPSHOT_MISMATCH,
SETTLEMENT_NOT_SIGNED, UNKNOWN_COUNTER, SETTLEMENT_POOL_CHANGED`。

## 边界与取舍

* 柜台数上限 12（`MAX_COUNTERS`）：每次签约要真实报出 2ⁿ−1 个小组价，
  12 柜台 = 4095 条独立报价记录（实测 < 1s，重放后瞬时）。
* 整数枚举证书只在候选箱 ≤ 2×10⁶ 点时签发（本仓库场景全部走 Farkas 证书）。
* `verify_settlement` 对 n≤6 且 total≤30 的实例用穷举重新推导最优分账；
  更大实例验证全部约束与证书算术（最优性证据随记录保存：
  分数极差下界 `fractional_range_bound` 与活跃约束集）。
* 分账并发：`attempt_settlement` 的最终落库在一个 `BEGIN IMMEDIATE` 事务里
  复查在池签名，池在签约期间被退款改变则自动重算重试（`SETTLEMENT_POOL_CHANGED` 兜底）。
