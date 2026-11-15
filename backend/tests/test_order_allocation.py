"""同店同档期满减整单归集的纯函数单测：统一选档、占比分摊、跨档、回退与确定性。"""
from __future__ import annotations

import pytest

from src.services import effective_price as ep
from src.services import order_allocation as oa

TWO_TIERS = [{"threshold": 300.0, "minus": 40.0}, {"threshold": 600.0, "minus": 90.0}]


def _obs(snapshot_id, price, *, competitor_id=10, campaign="618", tiers=None, extra=None):
    spec = {"page_price": float(price), "full_reduction": TWO_TIERS if tiers is None else tiers, "member_discount": 1.0}
    if extra:
        spec.update(extra)
    return {
        "snapshot_id": snapshot_id,
        "competitor_id": competitor_id,
        "captured_on": "2026-11-15",
        "campaign": campaign,
        "spec": spec,
    }


def test_largest_remainder_split_sums_exactly():
    splits = oa.largest_remainder_split(10.0, [1, 1, 1])
    assert splits == [3.34, 3.33, 3.33]
    assert round(sum(splits), 2) == 10.0

    splits = oa.largest_remainder_split(40.0, [299, 159])
    assert splits == [26.11, 13.89]
    assert round(sum(splits), 2) == 40.0


def test_zero_weights_are_split_evenly_and_still_balanced():
    splits = oa.largest_remainder_split(9.0, [0, 0, 0])
    assert round(sum(splits), 2) == 9.0
    assert splits == [3.0, 3.0, 3.0]


def test_group_allocation_picks_one_order_tier_and_shares_by_original_price():
    allocations = oa.allocate_observations([_obs(1, 299), _obs(2, 159)])
    first = allocations[1]
    assert first["status"] == "allocated"
    # 整单 n=1：458 命中 300 档，整单减 40
    node1 = first["by_units"]["1"]
    assert node1["group_subtotal"] == 458.0
    assert node1["tier"] == {"threshold": 300.0, "minus": 40.0}
    assert node1["group_minus"] == 40.0
    # 分摊合计与整单应减额逐分一致
    assert round(sum(node1["allocations"].values()), 2) == 40.0
    # n=2：916 跨到 600 档，整单减 90
    node2 = first["by_units"]["2"]
    assert node2["tier"] == {"threshold": 600.0, "minus": 90.0}
    assert round(sum(node2["allocations"].values()), 2) == 90.0
    # 占比按各货号原价
    shares = {member["snapshot_id"]: member["original_share_pct"] for member in first["members"]}
    assert shares == {1: 65.28, 2: 34.72}
    assert first["cross_tier"] is True  # 货号1独立选档（未触发）与整单档位不同


def test_effective_price_keeps_standalone_baseline_and_cross_tier_diff():
    observations = [_obs(1, 299), _obs(2, 159)]
    allocations = oa.allocate_observations(observations)
    r1 = ep.compute_effective_price_with_allocation(observations[0]["spec"], allocations[1])
    r2 = ep.compute_effective_price_with_allocation(observations[1]["spec"], allocations[2])
    # 独立口径：299/159 单件都不到 300，满减不触发，保留原价基线
    assert r1["standalone_baseline"]["unit_price_1"] == 299.0
    assert r2["standalone_baseline"]["unit_price_1"] == 159.0
    # 归集口径两货号应付之和 = 整单小计 − 整单减额
    assert round(r1["unit_price_1"] + r2["unit_price_1"], 2) == round(458 - 40, 2)
    # 跨档前后价差 = 归集单价 − 独立单价（更省为负）
    assert r1["standalone_baseline"]["cross_unit_price_diff"][0] == round(r1["unit_price_1"] - 299.0, 2)
    assert r1["allocation_scope"] == "order"


def test_below_threshold_group_keeps_original_price_and_record():
    observations = [
        _obs(3, 49, competitor_id=11),
        _obs(4, 49, competitor_id=11),
    ]
    observations[0]["spec"]["full_reduction"] = [{"threshold": 300.0, "minus": 40.0}]
    observations[1]["spec"]["full_reduction"] = [{"threshold": 300.0, "minus": 40.0}]
    allocations = oa.allocate_observations(observations)
    assert allocations[3]["status"] == "below_threshold"
    assert allocations[3]["full_minus_by_units"] == {1: 0.0, 2: 0.0, 3: 0.0}
    r3 = ep.compute_effective_price_with_allocation(observations[0]["spec"], allocations[3])
    assert r3["unit_price_1"] == r3["unit_price_2"] == r3["unit_price_3"] == 49.0
    assert any("未达任何满减门槛" in warning for warning in r3["warnings"])


def test_tier_mismatch_falls_back_to_standalone_per_cluster():
    observations = [
        _obs(5, 299, competitor_id=12, tiers=[{"threshold": 300.0, "minus": 40.0}]),
        _obs(6, 299, competitor_id=12, tiers=[{"threshold": 300.0, "minus": 30.0}]),
    ]
    allocations = oa.allocate_observations(observations)
    assert allocations[5]["status"] == "tier_mismatch"
    assert allocations[6]["status"] == "tier_mismatch"
    # 单元素簇回退独立口径：299 不触发满减
    r5 = ep.compute_effective_price_with_allocation(observations[0]["spec"], allocations[5])
    assert r5["unit_price_1"] == 299.0


def test_different_campaigns_and_shops_never_share_one_order():
    observations = [
        _obs(8, 200, competitor_id=14, campaign="A"),
        _obs(9, 200, competitor_id=14, campaign="B"),
        _obs(10, 200, competitor_id=15, campaign="A"),
    ]
    allocations = oa.allocate_observations(observations)
    assert all(allocation["status"] == "solo" for allocation in allocations.values())
    assert allocations[8]["scope"]["group_size"] == 1


def test_snapshots_without_full_reduction_are_not_allocated():
    observations = [_obs(7, 100, tiers=[])]
    assert oa.allocate_observations(observations) == {}
    # 无归集时入口等同独立口径
    result = ep.compute_effective_price_with_allocation(observations[0]["spec"], None)
    assert result["unit_price_1"] == 100.0
    assert "order_allocation" not in result


def test_allocation_is_deterministic():
    observations = [_obs(1, 299), _obs(2, 159), _obs(3, 88, competitor_id=11, tiers=[{"threshold": 300.0, "minus": 40.0}])]
    assert oa.allocate_observations(observations) == oa.allocate_observations(observations)


@pytest.mark.parametrize(
    "tiers,expected",
    [
        ([], ()),
        ([{"threshold": 300, "minus": 40}], ((300.0, 40.0),)),
        # 顺序不同但内容相同 → 同签名
        ([{"threshold": 600, "minus": 90}, {"threshold": 300, "minus": 40}], ((300.0, 40.0), (600.0, 90.0))),
    ],
)
def test_tier_signature(tiers, expected):
    assert oa.tier_signature(tiers) == expected
