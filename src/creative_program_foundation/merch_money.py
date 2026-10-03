"""商品化结算使用的十进制金额与纯函数计算。"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
HUNDRED = Decimal("100")


def money(value: Any, field: str, *, minimum: str | None = "0") -> Decimal:
    """把输入解析为两位小数的金额。"""

    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{field} 必须是十进制数字") from exc
    if not result.is_finite():
        raise ValueError(f"{field} 必须是有限数字")
    if minimum is not None and result < Decimal(minimum):
        raise ValueError(f"{field} 不能小于 {minimum}")
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def rate(value: Any, field: str) -> Decimal:
    """解析 0 到 1 之间的分成比率。"""

    result = money(value, field, minimum="0")
    if result > Decimal("1"):
        raise ValueError(f"{field} 不能大于 1")
    return result


def quantity(value: Any, field: str) -> Decimal:
    """解析非负数量，允许三位小数。"""

    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{field} 必须是十进制数字") from exc
    if result < 0 or not result.is_finite():
        raise ValueError(f"{field} 必须是非负有限数字")
    return result.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def cost_sheet_total(lines: list[dict[str, Any]], delivered_qty: Decimal) -> Decimal:
    """按成本口径行计算可抵扣成本。

    每行形如 {"label": ..., "amount": "12.00", "basis": "fixed"|"unit"}，
    unit 行按累计交付数量倍数计算。
    """

    total = Decimal("0")
    for index, line in enumerate(lines):
        amount = money(line.get("amount"), f"成本行{index + 1}.amount")
        basis = str(line.get("basis", "fixed"))
        if basis == "fixed":
            total += amount
        elif basis == "unit":
            total += amount * delivered_qty
        else:
            raise ValueError(f"成本行{index + 1}.basis 只能是 fixed 或 unit")
    return total.quantize(CENT, rounding=ROUND_HALF_UP)


def validate_share_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验成员份额行，比率之和必须为 1。"""

    if not lines:
        raise ValueError("成员份额不能为空")
    normalized: list[dict[str, Any]] = []
    total = Decimal("0")
    for index, line in enumerate(lines):
        actor_id = str(line.get("actor_id", "")).strip()
        if not actor_id:
            raise ValueError(f"成员份额行{index + 1}.actor_id 不能为空")
        member_rate = rate(line.get("ratio"), f"成员份额行{index + 1}.ratio")
        normalized.append({"actor_id": actor_id, "ratio": str(member_rate),
                           "label": str(line.get("label", actor_id))})
        total += member_rate
    if total != Decimal("1"):
        raise ValueError(f"成员份额比率之和必须为 1，当前为 {total}")
    return normalized


def split_team_amount(team_amount: Decimal, share_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把团队分成按份额行拆分，尾差并入最后一行。"""

    entries: list[dict[str, Any]] = []
    distributed = Decimal("0")
    for index, line in enumerate(share_lines):
        if index == len(share_lines) - 1:
            amount = team_amount - distributed
        else:
            amount = (team_amount * Decimal(str(line["ratio"]))).quantize(
                CENT, rounding=ROUND_HALF_UP)
            distributed += amount
        entries.append({"recipient_id": line["actor_id"], "label": line["label"],
                        "ratio": str(line["ratio"]), "amount": str(amount)})
    return entries


def compute_settlement(*, terms: dict[str, Any], cost_lines: list[dict[str, Any]],
                       share_lines: list[dict[str, Any]], gross_revenue: Decimal,
                       delivered_qty: Decimal, shortfall: bool,
                       cost_quantity: Decimal | None = None) -> dict[str, Any]:
    """根据合同口径快照计算一笔结算的全部金额。纯函数，便于复核。

    delivered_qty 是累计交付量（用于记录与最低量判断）；cost_quantity 是本结算
    周期内的交付量，单位成本只按周期量抵扣，缺省回退为累计量。
    """

    currency = str(terms.get("currency", "CNY"))
    royalty = terms.get("royalty") or {}
    deduct_costs = bool(royalty.get("deduct_costs", True))
    costs = cost_sheet_total(cost_lines, cost_quantity
                             if cost_quantity is not None else delivered_qty)
    deductible = costs if deduct_costs else Decimal("0")
    net = max(gross_revenue - deductible, Decimal("0")).quantize(CENT, rounding=ROUND_HALF_UP)
    mode = str(royalty.get("mode", "rate"))
    if mode == "rate":
        team_amount = (net * rate(royalty.get("rate", "0"), "royalty.rate")).quantize(
            CENT, rounding=ROUND_HALF_UP)
    elif mode == "fixed":
        team_amount = money(royalty.get("amount", "0"), "royalty.amount")
    else:
        raise ValueError("royalty.mode 只能是 rate 或 fixed")
    guarantee = royalty.get("guarantee_amount")
    if shortfall and guarantee not in (None, ""):
        team_amount = max(team_amount, money(guarantee, "royalty.guarantee_amount"))
    partner_amount = (gross_revenue - team_amount).quantize(CENT, rounding=ROUND_HALF_UP)
    if partner_amount < 0:
        raise ValueError("合作方分成不能为负，保证金额不能超过总收入")
    member_entries = split_team_amount(team_amount, share_lines)
    cost_qty = cost_quantity if cost_quantity is not None else delivered_qty
    return {
        "currency": currency,
        "gross_revenue": str(gross_revenue),
        "period_quantity": str(cost_qty),
        "deductible_costs": str(deductible),
        "costs_total": str(costs),
        "net_revenue": str(net),
        "team_amount": str(team_amount),
        "partner_amount": str(partner_amount),
        "shortfall": shortfall,
        "member_entries": member_entries,
        "cost_entries": [
            {"label": str(line.get("label", f"成本行{i + 1}")),
             "amount": str(money(line.get("amount"), "amount") *
                           (cost_qty if str(line.get("basis", "fixed")) == "unit" else 1)),
             "basis": str(line.get("basis", "fixed"))}
            for i, line in enumerate(cost_lines)
        ],
    }
