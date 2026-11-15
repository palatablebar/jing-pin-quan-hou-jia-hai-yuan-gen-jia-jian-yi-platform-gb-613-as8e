"""同店同档期满减整单归集与按原价占比分摊（纯函数、确定性、只依赖标准库）。

业务口径：
  同一竞品店铺、同一天、同一活动（campaign）下抓到的多个货号，满减按整单
  累计——整单原价合计命中统一档位后，减额按各货号原价占比分摊，不再每个货号
  各算一遍、把同一减额重复计入，也避免凑单跨档时优惠全记给一个货号。

分组规则：
  1. 按 (competitor_id, captured_on, campaign) 归集当天观测；
  2. 组内再按满减阶梯签名分簇：阶梯配置不一致的货号不能共用同一整单，
     自动回退独立口径并给出提示（status=tier_mismatch / solo）；
  3. 整单未达任何门槛的簇保留各货号独立口径与原价记录（status=below_threshold）。

分摊口径（对每个件数档 n ∈ {1,2,3} 分别计算）：
  整单小计 = Σ(page_price × n)，统一选档得到整单应减额；
  减额按各货号 page_price 占比（同档下等于 subtotal 占比）用最大余数法分摊到分，
  保证各货号分摊额之和与整单应减额逐分相等。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

EPS = 1e-9


def _money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _to_cents(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def tier_signature(tiers: list[dict] | None) -> tuple:
    """满减阶梯的可比签名（可哈希的 dict 键）：门槛/减免完全一致才视为同一档活动。"""
    if not tiers:
        return ()
    return tuple(
        sorted(
            (_money(float(tier.get("threshold", 0))), _money(float(tier.get("minus", 0))))
            for tier in tiers
        )
    )


def select_tier(tiers: list[dict], subtotal: float) -> dict | None:
    """命中「threshold ≤ subtotal」的最高减免档（只叠一档），与算法 A 选档口径一致。"""
    hit = None
    for tier in tiers or []:
        threshold = float(tier["threshold"])
        minus = float(tier["minus"])
        if threshold <= subtotal + EPS and (hit is None or minus >= float(hit["minus"])):
            hit = {"threshold": _money(threshold), "minus": _money(minus)}
    return hit


def largest_remainder_split(amount: float, weights: list[float]) -> list[float]:
    """把 amount（元）按 weights 占比分摊到分，最大余数法保证合计与 amount 逐分相等。"""
    if not weights:
        return []
    amount_cents = _to_cents(amount)
    total = sum(float(w) for w in weights)
    if total <= 0:
        # 权重全为 0 时均摊，仍然保证合计不变
        base, extra = divmod(amount_cents, len(weights))
        return [(base + (1 if i < extra else 0)) / 100 for i in range(len(weights))]
    raw = [amount_cents * float(w) / total for w in weights]
    floors = [int(value) for value in raw]
    leftover = amount_cents - sum(floors)
    # 余数按小数部分从大到小补 1 分；并列时按下标稳定排序，保证确定性
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for k in range(leftover):
        floors[order[k]] += 1
    return [cents / 100 for cents in floors]


def _standalone_tier(observation: dict, units: int) -> dict | None:
    subtotal = _money(float(observation["spec"]["page_price"]) * units)
    return select_tier(observation["spec"].get("full_reduction") or [], subtotal)


def _allocate_cluster(observations: list[dict], max_units: int) -> dict[int, dict]:
    """对一个阶梯签名一致的簇做整单归集，返回 {snapshot_id: allocation}。"""
    tiers = observations[0]["spec"].get("full_reduction") or []
    pages = [float(item["spec"]["page_price"]) for item in observations]
    group_original = _money(sum(pages))
    ids = [item["snapshot_id"] for item in observations]
    share_pct = {
        item["snapshot_id"]: _money(pages[i] / group_original * 100) if group_original > 0 else 0.0
        for i, item in enumerate(observations)
    }

    by_units: dict[str, dict] = {}
    any_hit = False
    cross_tier = False
    for units in range(1, max_units + 1):
        subtotals = [_money(page * units) for page in pages]
        group_subtotal = _money(sum(subtotals))
        tier = select_tier(tiers, group_subtotal)
        group_minus = _money(tier["minus"]) if tier else 0.0
        if tier is not None:
            any_hit = True
        # 最大余数法按各货号原价（同档 subtotal 占比等价）分摊整单减额
        splits = largest_remainder_split(group_minus, subtotals)
        allocations = {ids[i]: splits[i] for i in range(len(ids))}
        # 校验：分摊合计必须与整单应减额逐分一致
        assert _to_cents(sum(allocations.values())) == _to_cents(group_minus)
        for i, observation in enumerate(observations):
            standalone = _standalone_tier(observation, units)
            if (standalone or {}).get("minus") != (tier or {}).get("minus"):
                cross_tier = True
        by_units[str(units)] = {
            "group_subtotal": group_subtotal,
            "tier": tier,
            "group_minus": group_minus,
            "allocations": allocations,
        }

    if len(observations) == 1:
        status, warnings = "solo", ["该满减阶梯组内仅 1 个货号，按独立口径计算"]
    elif any_hit:
        status, warnings = "allocated", []
    else:
        status = "below_threshold"
        warnings = [f"整单原价 ¥{group_original:.2f} 未达任何满减门槛，各货号保留独立口径与原价"]

    members = [
        {
            "snapshot_id": item["snapshot_id"],
            "page_price": _money(pages[i]),
            "original_share_pct": share_pct[ids[i]],
            "allocated_minus_1": by_units["1"]["allocations"][ids[i]],
        }
        for i, item in enumerate(observations)
    ]
    base = {
        "scope": {
            "competitor_id": observations[0]["competitor_id"],
            "captured_on": observations[0]["captured_on"],
            "campaign": observations[0]["campaign"],
            "group_size": len(observations),
        },
        "group_original_total": group_original,
        "tier": by_units["1"]["tier"],
        "tier_label": _tier_label(by_units["1"]["tier"]),
        "cross_tier": cross_tier,
        "by_units": by_units,
        "members": members,
    }
    result: dict[int, dict] = {}
    for item in observations:
        snap_id = item["snapshot_id"]
        full_minus_by_units = {
            units: by_units[str(units)]["allocations"][snap_id] for units in range(1, max_units + 1)
        }
        result[snap_id] = {
            **base,
            "status": status,
            "warnings": list(warnings),
            "full_minus_by_units": full_minus_by_units,
        }
    return result


def _tier_label(tier: dict | None) -> str | None:
    if tier is None:
        return None
    return f"满{_trim(tier['threshold'])}减{_trim(tier['minus'])}"


def _trim(value: float) -> str:
    return ("%.2f" % value).rstrip("0").rstrip(".")


def allocate_observations(observations: list[dict], max_units: int = 3) -> dict[int, dict]:
    """按店铺×日期×活动归集当天观测并统一选档、按占比分摊。

    入参观测（仅传 standalone 还原 valid 的快照）：
      {snapshot_id, competitor_id, captured_on(iso str), campaign, spec}
    返回 {snapshot_id: allocation}；无满减配置的观测不归集、不出现在结果里，
    由调用方保留其独立口径。
    """
    groups: dict[tuple, list[dict]] = {}
    for item in observations:
        tiers = item["spec"].get("full_reduction") or []
        if not tiers:
            continue
        key = (item["competitor_id"], item["captured_on"], item.get("campaign") or "")
        groups.setdefault(key, []).append(item)

    allocations: dict[int, dict] = {}
    for key, members in groups.items():
        clusters: dict[tuple, list[dict]] = {}
        for item in members:
            signature = tier_signature(item["spec"].get("full_reduction"))
            clusters.setdefault(signature, []).append(item)
        if len(clusters) > 1:
            # 同店同活动出现多套满减阶梯：不能凑成一张整单，各簇独立归集并提示
            for cluster in clusters.values():
                cluster_alloc = _allocate_cluster(cluster, max_units)
                for allocation in cluster_alloc.values():
                    # 分簇回退时，无论簇内货号多少，都标记为阶梯不一致，保留独立选档结果
                    allocation["status"] = "tier_mismatch"
                    allocation["warnings"] = [
                        "同店同活动满减阶梯配置不一致，已按阶梯分簇回退独立归集",
                        *allocation["warnings"],
                    ]
                allocations.update(cluster_alloc)
        else:
            allocations.update(_allocate_cluster(next(iter(clusters.values())), max_units))
    return allocations
