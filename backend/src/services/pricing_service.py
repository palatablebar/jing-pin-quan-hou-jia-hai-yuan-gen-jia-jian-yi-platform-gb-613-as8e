"""测算主流程：抓价回放 → 算法 A（券后价还原，同店同档期满减整单归集）→ 算法 B（跟价建议）→ 建议入库。

纯服务端计算，同样的快照与规则两次运行结果一致；不访问外网。
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.advice import Advice
from src.models.competitor import Competitor
from src.models.rule import Rule
from src.models.run import Run
from src.models.sku import Sku
from src.models.snapshot import Snapshot
from src.services import (
    advice_service,
    effective_price,
    follow_advice,
    order_allocation,
    rule_service,
    run_service,
)
from src.utils.jsonutil import json_equal
from src.utils.timeutil import utcnow

CAPTURE_WINDOW_DAYS = 3
HISTORY_LIMIT = 20


def _spec_of(snapshot: Snapshot) -> dict:
    return {
        "page_price": snapshot.page_price,
        "full_reduction": snapshot.full_reduction or [],
        "shop_coupon": snapshot.shop_coupon,
        "platform_coupon": snapshot.platform_coupon,
        "member_discount": snapshot.member_discount,
    }


def _archive_effective(snapshot: Snapshot, new_effective: dict) -> bool:
    """把新口径写入 effective，并把旧口径追加进 effective_history（只增不覆盖）。

    用规范化 JSON 比较：与最近一次归档（或当前值）业务一致时不重复追加，
    保证重跑同一批不会产生重复记录、不覆盖可恢复的旧观测。返回是否发生口径变化。
    """
    previous = snapshot.effective or {}
    changed = not json_equal(previous, new_effective)
    if changed and previous:
        history = list(snapshot.effective_history or [])
        if not history or not json_equal(history[-1], previous):
            history.append(copy.deepcopy(previous))
        snapshot.effective_history = history[-HISTORY_LIMIT:]
    snapshot.effective = new_effective
    return changed


def _load_window(db: Session, captured_on: date) -> list[Snapshot]:
    start = captured_on - timedelta(days=CAPTURE_WINDOW_DAYS - 1)
    return list(
        db.scalars(
            select(Snapshot)
            .where(Snapshot.captured_on >= start, Snapshot.captured_on <= captured_on)
            .order_by(Snapshot.captured_on, Snapshot.id)
        ).all()
    )


def _upsert_advice(db: Session, run: Run, existing: dict[int, Advice], sku: Sku, snapshot: Snapshot, result: dict) -> Advice:
    advice = existing.get(sku.id)
    payload = {
        "algorithm": "B.follow_advice",
        "effective_algorithm": "A.effective_price",
        "current_price": round(sku.current_price, 2),
        "cost": round(sku.cost, 2),
        "floor_price": round(sku.floor_price, 2),
        "target_margin_pct": sku.target_margin_pct,
        "rule": result["strategy"],
        "cost_floor": result["cost_floor"],
        "market_cap": result["market_cap"],
        "clamped_by": result["clamped_by"],
        "breach": result["breach"],
        "reasons": result["reasons"],
        "p10": result["p10"],
        "p25": result["p25"],
        "p50": result["p50"],
        "mean": result["mean"],
        "samples": result["samples"],
        "snapshot": {
            "id": snapshot.id,
            "captured_on": snapshot.captured_on.isoformat(),
            "page_price": round(snapshot.page_price, 2),
            "full_reduction": snapshot.full_reduction or [],
            "shop_coupon": snapshot.shop_coupon,
            "platform_coupon": snapshot.platform_coupon,
            "member_discount": snapshot.member_discount,
            "unit_price_1": (snapshot.effective or {}).get("unit_price_1"),
            "unit_price_2": (snapshot.effective or {}).get("unit_price_2"),
            "unit_price_3": (snapshot.effective or {}).get("unit_price_3"),
            "best_units": (snapshot.effective or {}).get("best_units"),
            "note": (snapshot.effective or {}).get("note"),
            "order_allocation": (snapshot.effective or {}).get("order_allocation"),
            "standalone_baseline": (snapshot.effective or {}).get("standalone_baseline"),
        },
    }
    title = f"{sku.name}：竞品券后 ¥{result['p25']:.2f} → 建议 ¥{result['suggested_price']:.2f}"
    summary = result["reason"]
    if advice is None:
        advice = Advice(
            run_id=run.id,
            sku_id=sku.id,
            snapshot_id=snapshot.id,
            competitor_id=sku.competitor_id,
            rule_code=result["rule_code"],
            title=title[:200],
            summary=summary,
            action=result["action"],
            severity=result["risk"],
            score=abs(result["gap_pct"]),
            status="pending",
            current_price=sku.current_price,
            suggested_price=result["suggested_price"],
            effective_price=result["p25"],
            p10=result["p10"],
            p25=result["p25"],
            p50=result["p50"],
            margin_pct=result["margin_pct"],
            gap_pct=result["gap_pct"],
            payload=payload,
        )
        db.add(advice)
    else:
        # 重算刷新数值，但绝不覆盖审批状态与审批意见
        advice.snapshot_id = snapshot.id
        advice.competitor_id = sku.competitor_id
        advice.rule_code = result["rule_code"]
        advice.title = title[:200]
        advice.summary = summary
        advice.action = result["action"]
        advice.severity = result["risk"]
        advice.score = abs(result["gap_pct"])
        advice.current_price = sku.current_price
        advice.suggested_price = result["suggested_price"]
        advice.effective_price = result["p25"]
        advice.p10 = result["p10"]
        advice.p25 = result["p25"]
        advice.p50 = result["p50"]
        advice.margin_pct = result["margin_pct"]
        advice.gap_pct = result["gap_pct"]
        advice.payload = payload
    db.flush()
    return advice


def execute(db: Session, run: Run, captured_on: date, competitor_filter: int | None = None) -> dict:
    """执行一次测算，写入 run.metrics / run.log_text，返回 {metrics, log, status}。"""
    window = _load_window(db, captured_on)
    skus = {row.id: row for row in db.scalars(select(Sku)).all()}
    competitors = {row.id: row for row in db.scalars(select(Competitor)).all()}
    rules = rule_service.effective_rules(db)

    # 第一步：每条快照先按独立口径还原（不写库），同时识别非法快照
    valid_snapshots: list[Snapshot] = []
    invalid = 0
    for snapshot in window:
        result = effective_price.compute_effective_price(_spec_of(snapshot))
        if not result["valid"]:
            invalid += 1
            # 非法快照保留旧观测与记录，不用新结果覆盖
            continue
        valid_snapshots.append(snapshot)

    # 第二步：按店铺 × 日期 × 活动归集当天观测，统一选档、按原价占比分摊整单满减
    observations = [
        {
            "snapshot_id": snapshot.id,
            "competitor_id": snapshot.competitor_id,
            "captured_on": snapshot.captured_on.isoformat(),
            "campaign": snapshot.campaign or "",
            "spec": _spec_of(snapshot),
        }
        for snapshot in valid_snapshots
    ]
    allocations = order_allocation.allocate_observations(observations)

    # 第三步：用整单分摊口径重算并归档；无满减/未达门槛/阶梯不一致的保留独立口径记录
    prices_by_sku: dict[int, list[float]] = {}
    allocation_status_bucket: dict[str, int] = {}
    cross_tier_snapshot_ids: set[int] = set()
    for snapshot in valid_snapshots:
        allocation = allocations.get(snapshot.id)
        result = effective_price.compute_effective_price_with_allocation(_spec_of(snapshot), allocation)
        _archive_effective(snapshot, result)
        if allocation is not None:
            status = allocation.get("status", "allocated")
            allocation_status_bucket[status] = allocation_status_bucket.get(status, 0) + 1
            if allocation.get("cross_tier"):
                cross_tier_snapshot_ids.add(snapshot.id)
        prices_by_sku.setdefault(snapshot.sku_id, []).append(result["unit_price_1"])

    targets = [
        snapshot
        for snapshot in window
        if snapshot.captured_on == captured_on
        and skus.get(snapshot.sku_id) is not None
        and skus[snapshot.sku_id].enabled
        and (competitor_filter is None or snapshot.competitor_id == competitor_filter)
    ]

    existing = {
        row.sku_id: row
        for row in db.scalars(select(Advice).where(Advice.run_id == run.id)).all()
    }
    advice_count = 0
    risk_bucket = {"high": 0, "medium": 0, "low": 0}
    gaps: list[float] = []
    discounts: list[float] = []
    for snapshot in targets:
        sku = skus[snapshot.sku_id]
        competitor = competitors.get(snapshot.competitor_id)
        rule = rule_service.match_rule(rules, sku, competitor.code if competitor else None)
        prices = prices_by_sku.get(sku.id, [])
        if not prices:
            continue
        try:
            result = follow_advice.build_advice(
                competitor_prices=prices,
                cost=sku.cost,
                floor_price=sku.floor_price,
                target_margin_pct=rule.target_margin_pct,
                current_price=sku.current_price,
                strategy=rule.strategy,
                max_drop_pct=rule.max_drop_pct,
            )
        except ValueError:
            invalid += 1
            continue
        result["rule_code"] = rule.code
        if (snapshot.effective or {}).get("discount_pct"):
            discounts.append(float(snapshot.effective["discount_pct"]))
        if not result["need_follow"]:
            continue
        _upsert_advice(db, run, existing, sku, snapshot, result)
        advice_count += 1
        risk_bucket[result["risk"]] = risk_bucket.get(result["risk"], 0) + 1
        gaps.append(result["gap_pct"])

    captured_at = utcnow()
    for competitor_id in {snapshot.competitor_id for snapshot in targets}:
        competitor = competitors.get(competitor_id)
        if competitor is not None:
            competitor.last_captured_at = captured_at

    metrics = {
        "window_snapshots": len(window),
        "snapshots": len(targets),
        "skus": len({snapshot.sku_id for snapshot in targets}),
        "advices": advice_count,
        "high": risk_bucket.get("high", 0),
        "medium": risk_bucket.get("medium", 0),
        "low": risk_bucket.get("low", 0),
        "avg_gap_pct": round(sum(gaps) / len(gaps), 2) if gaps else 0.0,
        "avg_discount_pct": round(sum(discounts) / len(discounts), 2) if discounts else 0.0,
        "invalid_snapshots": invalid,
        "rules_applied": len({rule.code for rule in rules}),
        "window_days": CAPTURE_WINDOW_DAYS,
        "captured_on": captured_on.isoformat(),
        "allocation_allocated": allocation_status_bucket.get("allocated", 0),
        "allocation_solo": allocation_status_bucket.get("solo", 0),
        "allocation_below_threshold": allocation_status_bucket.get("below_threshold", 0),
        "allocation_tier_mismatch": allocation_status_bucket.get("tier_mismatch", 0),
        "allocation_cross_tier": len(cross_tier_snapshot_ids),
        "allocation_groups": len(
            {
                (
                    snapshot.competitor_id,
                    snapshot.captured_on.isoformat(),
                    snapshot.campaign or "",
                )
                for snapshot in valid_snapshots
                if snapshot.id in allocations
            }
        ),
    }
    log = (
        f"[{captured_on.isoformat()}] 回放窗口 {CAPTURE_WINDOW_DAYS} 天，快照 {len(window)} 条；"
        f"当日待测算 {len(targets)} 条 SKU；券后价还原失败 {invalid} 条；"
        f"整单满减归集 {metrics['allocation_groups']} 组，按占比分摊 {metrics['allocation_allocated']} 条，"
        f"未达门槛保留原价 {metrics['allocation_below_threshold']} 条，阶梯不一致回退 {metrics['allocation_tier_mismatch']} 条，"
        f"凑单跨档 {metrics['allocation_cross_tier']} 条；"
        f"生成跟价建议 {advice_count} 条（高 {risk_bucket.get('high', 0)} / 中 {risk_bucket.get('medium', 0)} / 低 {risk_bucket.get('low', 0)}）；"
        f"平均价差 {metrics['avg_gap_pct']}%，平均券后折扣 {metrics['avg_discount_pct']}%。"
    )
    status = "success" if metrics["snapshots"] > 0 else "partial"
    if invalid and invalid >= max(1, metrics["snapshots"] // 2):
        status = "partial"
    return {"metrics": metrics, "log": log, "status": status}


def compute_run(
    db: Session,
    *,
    captured_on: date,
    operator_email: str,
    trigger: str = "manual",
    seed_key: str | None = None,
    competitor_id: int | None = None,
) -> Run:
    if seed_key:
        existing = db.scalar(select(Run).where(Run.seed_key == seed_key))
        if existing is not None:
            return existing
    run = Run(
        run_no=run_service.next_run_no(db),
        trigger=trigger,
        status="running",
        operator_email=operator_email,
        captured_on=captured_on,
        competitor_id=competitor_id,
        seed_key=seed_key,
    )
    db.add(run)
    db.flush()
    try:
        result = execute(db, run, captured_on, competitor_id)
        run.metrics = result["metrics"]
        run.log_text = result["log"]
        run.status = result["status"]
        run.input_count = result["metrics"]["snapshots"]
        run.output_count = result["metrics"]["advices"]
    except Exception as exc:  # noqa: BLE001 - 失败留痕，不抛 500
        run.status = "failed"
        run.log_text = f"测算失败：{exc}"
        run.metrics = {"error": str(exc)[:300]}
    run.finished_at = utcnow()
    db.flush()
    return run
