"""算法 A｜券后有效价还原（纯函数、确定性、只依赖标准库）。

输入一份抓价快照（页面价 + 满减阶梯 + 店铺券 + 平台跨店券 + 会员折扣），
对购买件数 n ∈ {1,2,3} 枚举折扣组合，求该件数下的最低到手单价：

    subtotal  = page_price × n
    满减      = 命中「门槛 ≤ subtotal」的最高档（只叠一档）
    店铺券    = subtotal ≥ 门槛 时减券面；stackable=False 时与平台券二选一（取更省）
    平台券    = floor(subtotal / per) × minus（跨店每满减，可重复命中）
    payable   = (subtotal − 满减 − 店铺券 − 平台券) × 会员折扣
    unit(n)   = round_half_up(payable / n, 2)

输出 unit_price_1/2/3、最优件数 best_units、命中组合说明 note 与折扣率。
失败态（页面价非法 / 券配置非法）返回 valid=False + error，由调用方计入 unparsed。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

MAX_UNITS = 3
PLATFORM_PRIORITY = ("platform_coupon", "shop_coupon")


def _money(value: float) -> float:
    """金额四舍五入到分（ROUND_HALF_UP，避免二进制浮点的银行家舍入差异）。"""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _num(value, field: str) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise ValueError(f"{field} 不能是布尔值")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("￥", "").replace("¥", "")
        if not text:
            return 0.0
        return float(text)
    raise ValueError(f"{field} 类型不支持")


def _clean_tiers(raw) -> list[dict]:
    tiers: list[dict] = []
    if not raw:
        return tiers
    if not isinstance(raw, list):
        raise ValueError("满减阶梯必须是数组")
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("满减阶梯元素必须是对象")
        threshold = _num(item.get("threshold"), "threshold")
        minus = _num(item.get("minus"), "minus")
        if threshold <= 0 or minus < 0:
            raise ValueError("满减阶梯门槛必须为正、减免不能为负")
        if minus > threshold:
            raise ValueError("满减减免不能大于门槛")
        tiers.append({"threshold": threshold, "minus": minus})
    tiers.sort(key=lambda tier: (tier["threshold"], tier["minus"]))
    return tiers


def _clean_coupon(raw, field: str) -> dict | None:
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{field} 必须是对象")
    threshold = _num(raw.get("threshold"), f"{field}.threshold")
    minus = _num(raw.get("minus"), f"{field}.minus")
    if threshold <= 0 or minus < 0 or minus > threshold:
        raise ValueError(f"{field} 配置非法")
    return {
        "threshold": threshold,
        "minus": minus,
        "stackable": bool(raw.get("stackable", True)),
        "label": str(raw.get("label") or ""),
    }


def normalize_spec(spec: dict) -> dict:
    """把原始快照配置归一成内部结构；非法配置抛 ValueError。"""
    if not isinstance(spec, dict):
        raise ValueError("快照配置必须是对象")
    page_price = _num(spec.get("page_price"), "page_price")
    if not (page_price > 0):
        raise ValueError("页面价必须大于 0")
    if page_price > 1_000_000:
        raise ValueError("页面价超出合理区间")
    # 金额以分为最小单位：统一在入口舍入，避免 JSON 浮点往返（如 SQLite 299→298.99999999999994）污染占比
    page_price = _money(page_price)
    member_discount = _num(spec.get("member_discount") or 1.0, "member_discount")
    if not (0.5 <= member_discount <= 1.0):
        raise ValueError("会员折扣必须在 0.5~1.0 之间")
    platform = spec.get("platform_coupon")
    platform_norm = None
    if platform:
        if not isinstance(platform, dict):
            raise ValueError("platform_coupon 必须是对象")
        per = _num(platform.get("per"), "platform_coupon.per")
        minus = _num(platform.get("minus"), "platform_coupon.minus")
        if per <= 0 or minus < 0 or minus > per:
            raise ValueError("平台券配置非法")
        platform_norm = {"per": per, "minus": minus, "label": str(platform.get("label") or "")}
    return {
        "page_price": page_price,
        "full_reduction": _clean_tiers(spec.get("full_reduction")),
        "shop_coupon": _clean_coupon(spec.get("shop_coupon"), "shop_coupon"),
        "platform_coupon": platform_norm,
        "member_discount": member_discount,
    }


def _unit_breakdown(norm: dict, units: int, full_minus_override: float | None = None, allocation: dict | None = None) -> dict:
    page = norm["page_price"]
    subtotal = _money(page * units)
    hits: list[str] = []

    # 独立口径（每个货号各自选档）：始终计算，作为整单归集的对照基线
    standalone_tier = None
    for tier in norm["full_reduction"]:
        if tier["threshold"] <= subtotal + 1e-9:
            if standalone_tier is None or tier["minus"] >= standalone_tier["minus"]:
                standalone_tier = tier
    standalone_minus = _money(standalone_tier["minus"]) if standalone_tier is not None else 0.0

    # 整单归集口径：注入按原价占比分摊到该货号的满减额；否则沿用独立选档
    allocated = full_minus_override is not None
    full_minus = _money(full_minus_override) if allocated else standalone_minus
    if full_minus > 0:
        if allocated:
            group_label = (allocation or {}).get("tier_label") or "整单满减"
            solo_label = f"满{_trim(standalone_tier['threshold'])}减{_trim(standalone_tier['minus'])}" if standalone_tier else "独立口径未触发"
            hits.append(f"整单满减分摊（{group_label}）减{_trim(full_minus)}")
            hits.append(f"独立口径对照：{solo_label}减{_trim(standalone_minus)}")
        else:
            hits.append(f"满{_trim(standalone_tier['threshold'])}减{_trim(standalone_tier['minus'])}")

    shop = norm["shop_coupon"]
    shop_minus = 0.0
    shop_hit = bool(shop) and subtotal >= shop["threshold"] - 1e-9
    if shop_hit:
        shop_minus = shop["minus"]

    platform = norm["platform_coupon"]
    platform_times = 0
    platform_minus = 0.0
    if platform:
        platform_times = int((subtotal + 1e-9) // platform["per"])
        platform_minus = _money(platform_times * platform["minus"])

    # 店铺券不可叠加时与平台券二选一：取更省的那个（同额时取平台券）
    coupon_choice = "both"
    if shop_hit and platform_times > 0 and shop and not shop["stackable"]:
        if shop_minus > platform_minus:
            platform_minus, platform_times, coupon_choice = 0.0, 0, "shop_coupon"
        else:
            shop_minus, shop_hit, coupon_choice = 0.0, False, "platform_coupon"

    if shop_hit:
        hits.append(f"店铺券满{_trim(shop['threshold'])}减{_trim(shop['minus'])}")
    if platform_times > 0:
        hits.append(f"平台券每满{_trim(platform['per'])}减{_trim(platform['minus'])}×{platform_times}")
    if norm["member_discount"] < 1.0:
        hits.append(f"会员{_trim(norm['member_discount'] * 100)}%折")

    remaining = subtotal - full_minus - shop_minus - platform_minus
    if remaining < 0:
        remaining = 0.0
    payable = _money(remaining * norm["member_discount"])
    unit_price = _money(payable / units)
    off = _money(subtotal - payable)
    return {
        "units": units,
        "subtotal": subtotal,
        "full_reduction": {
            "applied": full_minus > 0,
            "minus": _money(full_minus),
            "allocated": allocated,
            "standalone_tier": (
                {"threshold": _money(standalone_tier["threshold"]), "minus": standalone_minus}
                if standalone_tier is not None
                else None
            ),
            "standalone_minus": standalone_minus,
        },
        "shop_coupon": {"applied": shop_hit, "minus": _money(shop_minus)},
        "platform_coupon": {"applied": platform_times > 0, "times": platform_times, "minus": _money(platform_minus)},
        "coupon_choice": coupon_choice,
        "member_discount": norm["member_discount"],
        "payable": payable,
        "unit_price": unit_price,
        "total_off": off,
        "discount_pct": _money(off / subtotal * 100) if subtotal else 0.0,
        "hits": hits,
        "note": "＋".join(hits) if hits else "无券可用，等于页面价",
    }


def _trim(value: float) -> str:
    return ("%.2f" % value).rstrip("0").rstrip(".")


def compute_effective_price(spec: dict, max_units: int = MAX_UNITS) -> dict:
    """券后有效价还原。返回 {valid, units[], unit_price_1..3, best_units, best_unit_price, note, warnings, confidence}。"""
    try:
        norm = normalize_spec(spec)
    except (ValueError, TypeError) as exc:
        return {
            "valid": False,
            "error": "INVALID_SNAPSHOT",
            "message": str(exc),
            "units": [],
            "unit_price_1": None,
            "unit_price_2": None,
            "unit_price_3": None,
            "best_units": None,
            "best_unit_price": None,
            "note": "配置非法，无法还原",
            "warnings": [str(exc)],
            "confidence": 0.0,
        }

    breakdowns = [_unit_breakdown(norm, units) for units in range(1, min(max_units, MAX_UNITS) + 1)]
    best = min(breakdowns, key=lambda item: (item["unit_price"], item["units"]))
    warnings: list[str] = []
    if not norm["full_reduction"] and not norm["shop_coupon"] and not norm["platform_coupon"] and norm["member_discount"] >= 1.0:
        warnings.append("该快照没有任何优惠，券后价等于页面价")
    if norm["shop_coupon"] and not norm["shop_coupon"]["stackable"]:
        warnings.append("店铺券标注为不可叠加，与平台券按更省一档命中")

    notes = [f"{item['units']}件单价 ¥{item['unit_price']}（{item['note']}）" for item in breakdowns]
    return {
        "valid": True,
        "page_price": norm["page_price"],
        "units": breakdowns,
        "unit_price_1": breakdowns[0]["unit_price"],
        "unit_price_2": breakdowns[1]["unit_price"] if len(breakdowns) > 1 else None,
        "unit_price_3": breakdowns[2]["unit_price"] if len(breakdowns) > 2 else None,
        "best_units": best["units"],
        "best_unit_price": best["unit_price"],
        "best_payable": best["payable"],
        "discount_pct": _money((1 - breakdowns[0]["unit_price"] / norm["page_price"]) * 100),
        "note": "；".join(notes),
        "detail": best["note"],
        "warnings": warnings,
        "confidence": 0.99 if not warnings else 0.9,
    }


def compute_effective_price_with_allocation(spec: dict, allocation: dict | None, max_units: int = MAX_UNITS) -> dict:
    """整单归集口径的券后还原。

    allocation 为 order_allocation 模块对该快照的归集结果：满减不再各自选档，
    而是注入按原价占比分摊到本货号的减额；店铺券 / 平台券 / 会员折扣仍按本快照
    独立结算。allocation 为 None 时等同独立口径 compute_effective_price。
    输出同时保留独立基线 standalone_baseline 与跨档前后价差，便于页面核对。
    """
    standalone = compute_effective_price(spec, max_units)
    if allocation is None:
        return standalone
    try:
        norm = normalize_spec(spec)
    except (ValueError, TypeError):
        return standalone

    overrides = allocation.get("full_minus_by_units") or {}
    breakdowns = [
        _unit_breakdown(norm, units, full_minus_override=overrides.get(units), allocation=allocation)
        for units in range(1, min(max_units, MAX_UNITS) + 1)
    ]
    best = min(breakdowns, key=lambda item: (item["unit_price"], item["units"]))

    # 跨档前后价差：整单分摊口径单价 − 独立口径单价（负=分摊后更省）
    cross_diffs = [
        _money(item["unit_price"] - standalone["units"][i]["unit_price"])
        for i, item in enumerate(breakdowns)
    ]
    notes = [f"{item['units']}件单价 ¥{item['unit_price']}（{item['note']}）" for item in breakdowns]
    warnings = list(standalone.get("warnings") or [])
    warnings.extend(allocation.get("warnings") or [])
    if allocation.get("cross_tier"):
        warnings.append("该货号独立选档与整单统一档位不一致，已按整单档位分摊（见跨档价差）")

    return {
        "valid": True,
        "page_price": norm["page_price"],
        "units": breakdowns,
        "unit_price_1": breakdowns[0]["unit_price"],
        "unit_price_2": breakdowns[1]["unit_price"] if len(breakdowns) > 1 else None,
        "unit_price_3": breakdowns[2]["unit_price"] if len(breakdowns) > 2 else None,
        "best_units": best["units"],
        "best_unit_price": best["unit_price"],
        "best_payable": best["payable"],
        "discount_pct": _money((1 - breakdowns[0]["unit_price"] / norm["page_price"]) * 100),
        "note": "；".join(notes),
        "detail": best["note"],
        "warnings": warnings,
        "confidence": 0.99 if not warnings else 0.9,
        "allocation_scope": "order",
        "order_allocation": allocation,
        "standalone_baseline": {
            "unit_price_1": standalone.get("unit_price_1"),
            "unit_price_2": standalone.get("unit_price_2"),
            "unit_price_3": standalone.get("unit_price_3"),
            "best_units": standalone.get("best_units"),
            "best_unit_price": standalone.get("best_unit_price"),
            "note": standalone.get("note"),
            "cross_unit_price_diff": cross_diffs,
        },
    }
