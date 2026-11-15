import { api } from '../api.js';
import { ENUMS, can, optionList } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, filterBar, kv, table, toast } from '../ui.js';
import { couponText, money, priceCompare, signedPct } from '../components/price.js';

const state = { capturedOn: '', competitorId: '', category: '', q: '' };

function unitTable(effective) {
  const units = effective.units || [];
  if (!units.length) return '<p class="muted">这条快照的券配置非法，算法 A 无法还原。</p>';
  const baseline = effective.standalone_baseline;
  const baselinePrices = baseline ? [baseline.unit_price_1, baseline.unit_price_2, baseline.unit_price_3] : [];
  return `<div class="table-wrap"><table class="data compact" style="min-width:0">
    <thead><tr><th>件数</th><th>小计</th><th>满减（整单分摊）</th><th>独立口径满减</th><th>店铺券</th><th>平台券</th><th>会员</th><th>应付</th><th>单价</th>${baseline ? '<th>跨档前后价差</th>' : ''}</tr></thead>
    <tbody>${units
      .map((unit, index) => {
        const fr = unit.full_reduction || {};
        const diff = baseline?.cross_unit_price_diff?.[index];
        return `<tr class="${unit.units === effective.best_units ? 'best-row' : ''}">
          <td class="num">${unit.units}</td>
          <td class="num money">${money(unit.subtotal)}</td>
          <td class="num">${fr.applied ? `−${money(fr.minus)}${fr.allocated ? ' <span class="chip">分摊</span>' : ''}` : '未触发'}</td>
          <td class="num muted">${fr.standalone_tier ? `−${money(fr.standalone_minus)}` : '未触发'}</td>
          <td class="num">${unit.shop_coupon.applied ? `−${money(unit.shop_coupon.minus)}` : '未触发'}</td>
          <td class="num">${unit.platform_coupon.applied ? `−${money(unit.platform_coupon.minus)} ×${unit.platform_coupon.times}` : '未触发'}</td>
          <td class="num">${unit.member_discount < 1 ? `${Math.round(unit.member_discount * 100)}%` : '—'}</td>
          <td class="num money">${money(unit.payable)}</td>
          <td class="num money">${money(unit.unit_price)}</td>
          ${baseline ? `<td class="num ${diff < 0 ? 'neg' : diff > 0 ? 'pos' : 'muted'}">${diff === 0 ? '0.00' : money(diff, { sign: true })}</td>` : ''}
        </tr>`;
      })
      .join('')}</tbody></table></div>
    ${baseline ? `<p class="muted tiny">独立口径（各货号各自选档）单价：${baselinePrices.map((p) => money(p)).join(' / ')}；「跨档前后价差」= 整单分摊单价 − 独立口径单价，负值代表凑单后更省。</p>` : ''}
    <p class="muted tiny">命中组合按「满减单档（整单统一选档后按原价占比分摊）→ 店铺券 → 平台跨店券（每满可重复）→ 会员折扣」顺序结算；最优件数 = 单件到手价最低。</p>`;
}

const ALLOCATION_STATUS_LABELS = {
  allocated: '整单分摊',
  solo: '组内单货号',
  below_threshold: '整单未达门槛',
  tier_mismatch: '阶梯不一致·回退独立',
};

function allocationBlock(item) {
  const effective = item.effective || {};
  const allocation = effective.order_allocation;
  if (!allocation) return '';
  const byUnits = allocation.by_units || {};
  const memberRows = (allocation.members || [])
    .map((member) => {
      const split1 = byUnits['1']?.allocations?.[String(member.snapshot_id)] ?? byUnits['1']?.allocations?.[member.snapshot_id];
      const isSelf = member.snapshot_id === item.id;
      return `<tr class="${isSelf ? 'best-row' : ''}">
        <td class="mono">#${member.snapshot_id}${isSelf ? '（本货号）' : ''}</td>
        <td class="num money">${money(member.page_price)}</td>
        <td class="num">${Number(member.original_share_pct).toFixed(2)}%</td>
        <td class="num money">−${money(split1)}</td>
      </tr>`;
    })
    .join('');
  const tierRows = Object.entries(byUnits)
    .map(([units, node]) => `<tr>
      <td class="num">${units} 件</td>
      <td class="num money">${money(node.group_subtotal)}</td>
      <td class="num">${node.tier ? esc(`满${node.tier.threshold}减${node.tier.minus}`) : '未达门槛'}</td>
      <td class="num money">−${money(node.group_minus)}</td>
    </tr>`)
    .join('');
  return `<div class="section-label">整单满减归集（同店铺 · 同活动 · 统一选档 · 按原价占比分摊）</div>
    ${kv([
      ['归集范围', `${esc(item.competitor_name || '—')} · ${esc(allocation.scope?.campaign || '—')} · ${allocation.scope?.group_size || 0} 个货号`],
      ['归集状态', `<span class="badge ${allocation.status === 'allocated' ? 'low' : 'info'}">${esc(ALLOCATION_STATUS_LABELS[allocation.status] || allocation.status)}</span>${allocation.cross_tier ? ' <span class="badge high">凑单跨档</span>' : ''}`],
      ['整单原价合计', `<span class="money">${money(allocation.group_original_total)}</span>`],
      ['整单统一档位', esc(allocation.tier_label || '未达任何门槛（各货号保留原价）')],
    ])}
    <div class="table-wrap"><table class="data compact" style="min-width:0">
      <thead><tr><th>件数档</th><th>整单小计</th><th>命中档位</th><th>整单应减额</th></tr></thead>
      <tbody>${tierRows}</tbody>
    </table></div>
    <div class="table-wrap"><table class="data compact" style="min-width:0">
      <thead><tr><th>货号</th><th>原价</th><th>原价占比</th><th>1 件档分摊减额</th></tr></thead>
      <tbody>${memberRows}</tbody>
    </table></div>
    ${(allocation.warnings || []).length ? `<p class="muted tiny">${allocation.warnings.map(esc).join('；')}</p>` : ''}`;
}

function historyBlock(item) {
  const history = item.effective_history || [];
  if (!history.length) return '';
  return `<div class="section-label">历史口径归档（${history.length} 条，旧观测只追加不覆盖）</div>
    <ul class="reason-list">${history
      .map((entry, index) => `<li>归档 ${index + 1}：1/2/3 件单价 ${money(entry.unit_price_1)} / ${money(entry.unit_price_2)} / ${money(entry.unit_price_3)}${entry.allocation_scope === 'order' ? '（整单分摊口径）' : '（独立口径）'}</li>`)
      .join('')}</ul>`;
}

function openSnapshot(view, ctx, item) {
  const { root } = drawer({
    title: `${item.sku_name} · ${item.captured_on}`,
    subtitle: `${item.competitor_name} · 快照 #${item.id}`,
    body: `${priceCompare(item)}
      ${kv([
        ['页面价', `<span class="money">${money(item.page_price)}</span>`],
        ['券配置', esc(couponText(item))],
        ['会员折扣', item.member_discount < 1 ? `${Math.round(item.member_discount * 100)}%` : '非会员价'],
        ['活动', esc(item.campaign || '—')],
        ['库存', esc(item.stock)],
        ['算法 A 结论', esc(item.note || '—')],
        ['置信度', `${item.valid ? '0.99' : '0.00'}${(item.warnings || []).length ? ` · ${esc(item.warnings.join('；'))}` : ''}`],
      ])}
      ${allocationBlock(item)}
      <div class="section-label">按件数拆解（算法 A 明细）</div>
      ${unitTable(item.effective || {})}
      ${historyBlock(item)}`,
  });
  root.querySelector('[data-close]')?.addEventListener('click', () => {});
}

export const snapshotsPage = {
  async render(view, ctx) {
    let data;
    try {
      data = await api.get('/api/snapshots', {
        captured_on: state.capturedOn,
        competitor_id: state.competitorId,
        category: state.category,
        q: state.q,
        limit: 50,
      });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => snapshotsPage.render(view, ctx));
      return;
    }
    const days = data.capture_days || [];
    if (!state.capturedOn && days.length) state.capturedOn = days[0];
    const competitors = await api.get('/api/competitors', { limit: 20 }).catch(() => ({ items: [] }));
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>抓价快照 · 页面价 vs 券后价</h2>
          <div class="head-actions">
            <span class="meta">${data.total} 条快照 · 快照日 ${days.length} 天</span>
            ${can(ctx.user.role, 'run:create') ? '<button class="btn sm accent" id="run-calc">重跑今天测算</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          ${filterBar('snap-filters', [
            field({ label: '快照日期', name: 'captured_on', value: state.capturedOn, options: days.map((day) => ({ value: day, label: day })) }),
            field({ label: '类目', name: 'category', value: state.category, options: optionList(ENUMS.category) }),
            field({
              label: '竞品店铺',
              name: 'competitor_id',
              value: state.competitorId,
              options: [{ value: '', label: '全部' }, ...(competitors.items || []).map((item) => ({ value: item.id, label: item.name }))],
            }),
            field({ label: '搜索 SKU', name: 'q', value: state.q, placeholder: '名称 / 编码' }),
          ])}
        </div>
        ${table({
          rows: data.items,
          empty: { title: '这个筛选下没有快照', hint: '换一天，或先跑一次抓价回放。' },
          columns: [
            {
              key: 'sku_name',
              label: 'SKU / 竞品',
              render: (row) =>
                `<b>${esc(row.sku_name)}</b><div class="mono muted">${esc(row.sku_code)} · ${esc(row.competitor_name || '—')}</div>`,
            },
            { key: 'page_price', label: '页面价', className: 'num money', render: (row) => money(row.page_price) },
            { key: 'unit_price_1', label: '券后 1 件', className: 'num money', render: (row) => money(row.unit_price_1) },
            { key: 'unit_price_2', label: '券后 2 件', className: 'num money', render: (row) => money(row.unit_price_2) },
            { key: 'unit_price_3', label: '券后 3 件', className: 'num money', render: (row) => money(row.unit_price_3) },
            {
              key: 'allocation_status',
              label: '整单满减',
              render: (row) => {
                if (!row.allocation_status) return '<span class="muted">独立口径</span>';
                const tone = row.allocation_status === 'allocated' ? 'low' : 'info';
                const label = ALLOCATION_STATUS_LABELS[row.allocation_status] || row.allocation_status;
                const share = row.original_share_pct != null ? ` · 占比 ${Number(row.original_share_pct).toFixed(2)}%` : '';
                const tier = row.allocation_tier_label ? ` · ${esc(row.allocation_tier_label)}` : '';
                return `<span class="badge ${tone}">${esc(label)}</span><span class="mono muted ellipsis">${tier}${share}</span>`;
              },
            },
            {
              key: 'discount_pct',
              label: '券后折扣',
              className: 'num',
              render: (row) => `<span class="badge info">${signedPct(-Math.abs(Number(row.discount_pct)))}</span>`,
            },
            {
              key: 'note',
              label: '命中组合',
              render: (row) => `<span class="mono muted ellipsis">${esc(row.note || '—')}</span>`,
            },
          ],
        })}
      </section>`;

    const filters = view.querySelector('#snap-filters');
    filters?.addEventListener('change', () => {
      state.capturedOn = filters.querySelector('[name=captured_on]').value;
      state.category = filters.querySelector('[name=category]').value;
      state.competitorId = filters.querySelector('[name=competitor_id]').value;
      state.q = filters.querySelector('[name=q]').value.trim();
      snapshotsPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        snapshotsPage.render(view, ctx);
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', async () => {
        try {
          const detail = await api.get(`/api/snapshots/${row.dataset.id}`);
          openSnapshot(view, ctx, detail);
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    });
    view.querySelector('#run-calc')?.addEventListener('click', async () => {
      const button = view.querySelector('#run-calc');
      button.disabled = true;
      button.textContent = '测算中…';
      try {
        const run = await api.post('/api/runs', { captured_on: state.capturedOn || undefined, trigger: 'manual' });
        toast(`测算完成 ${run.run_no}：${run.metrics.snapshots} 条快照 → ${run.metrics.advices} 条建议`);
        await snapshotsPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        button.disabled = false;
        button.textContent = '重跑今天测算';
      }
    });
  },
};

export default snapshotsPage;
