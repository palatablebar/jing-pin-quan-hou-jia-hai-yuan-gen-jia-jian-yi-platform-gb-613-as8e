"""测算主流程集成测试（内存 SQLite）：整单归集落库、历史归档、重跑幂等、非法观测保留。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.models import Base
from src.models.advice import Advice
from src.models.competitor import Competitor
from src.models.rule import Rule
from src.models.run import Run
from src.models.sku import Sku
from src.models.snapshot import Snapshot
from src.services import pricing_service

TODAY = date(2026, 11, 15)
TIERS = [{"threshold": 300.0, "minus": 40.0}, {"threshold": 600.0, "minus": 90.0}]


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()
    engine.dispose()


def _seed_basic(db):
    shop_a = Competitor(code="shop-a", name="凑单店铺", platform="taobao")
    shop_b = Competitor(code="shop-b", name="低价店铺", platform="jd")
    db.add_all([shop_a, shop_b])
    db.flush()
    db.add(
        Rule(
            code="rule-global",
            name="全局",
            scope_kind="global",
            scope_value="",
            strategy="hold",
            target_margin_pct=12.0,
            max_drop_pct=15.0,
            priority=100,
            enabled=True,
        )
    )
    # shop-a：两个货号整单 458 凑到 300 档；shop-b：两个低价货号任何件数档都不到 300
    specs = [
        ("SKU-A", 299.0, shop_a.id),
        ("SKU-B", 159.0, shop_a.id),
        ("SKU-C", 49.0, shop_b.id),
        ("SKU-D", 49.0, shop_b.id),
    ]
    skus = []
    for code, price, competitor_id in specs:
        sku = Sku(
            sku_code=code,
            name=code,
            category="decor",
            competitor_id=competitor_id,
            cost=price * 0.5,
            floor_price=price * 0.7,
            target_margin_pct=12.0,
            current_price=price,
            enabled=True,
        )
        db.add(sku)
        skus.append(sku)
    db.flush()
    for sku in skus:
        db.add(
            Snapshot(
                sku_id=sku.id,
                competitor_id=sku.competitor_id,
                captured_on=TODAY,
                page_price=sku.current_price,
                full_reduction=TIERS,
                member_discount=1.0,
                campaign="618 现货",
            )
        )
    db.flush()
    return shop_a, skus


def _run(db, seed_key=None):
    run = pricing_service.compute_run(
        db,
        captured_on=TODAY,
        operator_email="admin@pricewatch.io",
        trigger="manual",
        seed_key=seed_key,
    )
    db.commit()
    return run


def test_allocation_persisted_and_group_minus_balances(db_session):
    _, skus = _seed_basic(db_session)
    run = _run(db_session)
    assert run.status == "success"
    snap_a = db_session.scalar(select(Snapshot).where(Snapshot.sku_id == skus[0].id))
    snap_b = db_session.scalar(select(Snapshot).where(Snapshot.sku_id == skus[1].id))
    allocation_a = snap_a.effective["order_allocation"]
    allocation_b = snap_b.effective["order_allocation"]
    # 同组、同档位、同整单减额
    assert allocation_a["status"] == "allocated"
    assert allocation_a["tier"] == {"threshold": 300.0, "minus": 40.0}
    assert allocation_a["by_units"]["1"]["group_minus"] == allocation_b["by_units"]["1"]["group_minus"] == 40.0
    # 各货号分摊合计 == 整单应减额（逐分一致）
    splits = allocation_a["by_units"]["1"]["allocations"]
    assert round(sum(splits.values()), 2) == 40.0
    # 两货号归集应付之和 == 整单原价 − 整单减额
    assert round(snap_a.effective["unit_price_1"] + snap_b.effective["unit_price_1"], 2) == round(458 - 40, 2)
    # 独立口径基线保留（单件都未达 300，原价 299/159）
    assert snap_a.effective["standalone_baseline"]["unit_price_1"] == 299.0
    assert snap_b.effective["standalone_baseline"]["unit_price_1"] == 159.0


def test_below_threshold_group_keeps_original_price(db_session):
    _, skus = _seed_basic(db_session)
    _run(db_session)
    for sku in skus[2:]:
        snap = db_session.scalar(select(Snapshot).where(Snapshot.sku_id == sku.id))
        assert snap.effective["order_allocation"]["status"] == "below_threshold"
        assert snap.effective["unit_price_1"] == 49.0


def test_rerun_same_batch_adds_no_duplicate_advice_and_keeps_allocation(db_session):
    _, _ = _seed_basic(db_session)
    first = _run(db_session, seed_key="run-key-1")
    # 同 seed_key 直接返回同一 run，不新增
    again = _run(db_session, seed_key="run-key-1")
    assert again.id == first.id
    # 再跑一个新 run，同批数据结果一致：不产生任何重复历史归档
    archived_before = sum(1 for snap in db_session.scalars(select(Snapshot)).all() if snap.effective_history)
    run2 = _run(db_session, seed_key="run-key-2")
    snapshots = db_session.scalars(select(Snapshot)).all()
    assert sum(1 for snap in snapshots if snap.effective_history) == archived_before
    # 两个 run 的归集与建议指标一致
    assert run2.metrics["allocation_allocated"] == first.metrics["allocation_allocated"]
    assert run2.metrics["advices"] == first.metrics["advices"]
    # 每个 run 的建议按 (run_id, sku_id) 自然键唯一，两个 run 各自一套、互不重复
    for run in (first, run2):
        rows = db_session.scalars(select(Advice).where(Advice.run_id == run.id)).all()
        keys = [(row.run_id, row.sku_id) for row in rows]
        assert len(keys) == len(set(keys))


def test_preexisting_standalone_observation_is_archived_once(db_session):
    _, skus = _seed_basic(db_session)
    # 模拟老数据：快照已带独立口径 effective
    from src.services import effective_price

    snap_a = db_session.scalar(select(Snapshot).where(Snapshot.sku_id == skus[0].id))
    standalone = effective_price.compute_effective_price(
        {"page_price": 299.0, "full_reduction": TIERS, "member_discount": 1.0}
    )
    snap_a.effective = standalone
    db_session.commit()

    _run(db_session, seed_key="run-1")
    db_session.refresh(snap_a)
    # 独立 → 整单归集，旧口径被归档且仅一条
    assert len(snap_a.effective_history) == 1
    assert snap_a.effective_history[0] == standalone
    assert snap_a.effective.get("allocation_scope") == "order"
    # 再跑一次，归档不重复
    _run(db_session, seed_key="run-2")
    db_session.refresh(snap_a)
    assert len(snap_a.effective_history) == 1


def test_changed_observation_archives_previous_without_losing_it(db_session):
    _, skus = _seed_basic(db_session)
    _run(db_session, seed_key="run-1")
    snap_a = db_session.scalar(select(Snapshot).where(Snapshot.sku_id == skus[0].id))
    before = snap_a.effective
    # 模拟活动变化导致归集口径改变：把同组货号 B 降价，整单占比变化
    snap_b = db_session.scalar(select(Snapshot).where(Snapshot.sku_id == skus[1].id))
    snap_b.page_price = 100.0
    db_session.commit()
    _run(db_session, seed_key="run-2")
    db_session.refresh(snap_a)
    assert snap_a.effective != before
    assert len(snap_a.effective_history) == 1
    assert snap_a.effective_history[0] == before  # 旧观测完整保留、可恢复


def test_invalid_snapshot_keeps_previous_observation(db_session):
    _, skus = _seed_basic(db_session)
    _run(db_session, seed_key="run-1")
    snap_c = db_session.scalar(select(Snapshot).where(Snapshot.sku_id == skus[2].id))
    previous = snap_c.effective
    # 配置被改成非法（页面价 0），重算不得覆盖旧观测
    snap_c.page_price = 0.0
    db_session.commit()
    run = _run(db_session, seed_key="run-2")
    db_session.refresh(snap_c)
    assert run.metrics["invalid_snapshots"] >= 1
    assert snap_c.effective == previous
    assert snap_c.effective_history == []


def test_invalid_member_is_excluded_from_order_group(db_session):
    """组内一个货号配置非法时，不参与整单凑单；同组其余货号仍按实际在组货号归集。"""
    competitor, skus = _seed_basic(db_session)
    # 再加一个与 SKU-A 同店同活动的货号，先制造三货号组
    extra = Sku(
        sku_code="SKU-E",
        name="SKU-E",
        category="decor",
        competitor_id=competitor.id,
        cost=50.0,
        floor_price=70.0,
        target_margin_pct=12.0,
        current_price=88.0,
        enabled=True,
    )
    db_session.add(extra)
    db_session.flush()
    db_session.add(
        Snapshot(
            sku_id=extra.id,
            competitor_id=competitor.id,
            captured_on=TODAY,
            page_price=88.0,
            full_reduction=TIERS,
            member_discount=1.0,
            campaign="618 现货",
        )
    )
    db_session.flush()
    _run(db_session, seed_key="run-valid")
    # 把额外货号改成非法后重跑
    extra_snap = db_session.scalar(select(Snapshot).where(Snapshot.sku_id == extra.id))
    previous_effective = extra_snap.effective
    extra_snap.page_price = 0.0
    db_session.commit()
    run = _run(db_session, seed_key="run-invalid")
    db_session.refresh(extra_snap)
    # 非法货号不参与本次归集，且旧观测原样保留（不被非法结果覆盖）
    assert run.metrics["invalid_snapshots"] >= 1
    assert extra_snap.effective == previous_effective
    # SKU-A 仍正常归集，组内只剩 A/B 两个有效货号（非法货号被排除）
    snap_a = db_session.scalar(select(Snapshot).where(Snapshot.sku_id == skus[0].id))
    allocation = snap_a.effective["order_allocation"]
    assert allocation["status"] == "allocated"
    assert allocation["scope"]["group_size"] == 2
    member_ids = {member["snapshot_id"] for member in allocation["members"]}
    assert extra_snap.id not in member_ids
