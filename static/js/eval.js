// Model test bench: run each case on each chosen tier, one request at a time
(function () {
    const controls = document.getElementById('evalControls');
    if (!controls) return;
    const key = controls.dataset.key;
    const caseIds = JSON.parse(controls.dataset.cases);
    const runBtn = document.getElementById('runAll');
    const progress = document.getElementById('evalProgress');
    const statusEl = document.getElementById('evalStatus');
    const countEl = document.getElementById('evalCount');
    const bar = document.getElementById('evalBar');
    const summaryEl = document.getElementById('evalSummary');

    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const money = (n) => '$' + Number(n || 0).toFixed(n < 0.01 ? 4 : 2);

    function resultCell(r) {
        if (r.error) {
            return `<div class="eval-result fail"><div class="eval-result-head"><b>${esc(r.tier)}</b><span class="tag tag-warn">error</span></div><div class="small">${esc(r.error)}</div></div>`;
        }
        const wrong = r.checks.filter((c) => !c.ok)
            .map((c) => `${esc(c.tool)} ${esc(c.attribute)}: got <b>${esc(c.got || '—')}</b>, want ${esc([].concat(c.expected).join(' / '))}`);
        const drafts = r.drafts.map((d) =>
            `<li>${esc(d.name)}${d.brand ? ' · ' + esc(d.brand) : ''}${d.battery_platform ? ' · ' + esc(d.battery_platform) : ''}
             <span class="muted">$${Number(d.daily_price).toFixed(2)}/day${d.ai_confidence != null ? ' · ' + Math.round(d.ai_confidence * 100) + '%' : ''}</span></li>`).join('');
        return `<div class="eval-result ${r.passed ? 'pass' : 'fail'}">
            <div class="eval-result-head">
                <b>${esc(r.tier)}</b>
                <span class="tag ${r.passed ? 'tag-green' : 'tag-warn'}">${r.score} ${r.passed ? 'pass' : 'fail'}</span>
                <span class="muted small">found ${r.found}/${r.expected} · ${r.listings} listed · ${r.seconds}s · ${money(r.cost)}</span>
            </div>
            ${r.missing.length ? `<div class="small">missing: ${r.missing.map(esc).join(', ')}</div>` : ''}
            ${wrong.length ? `<div class="small">wrong: ${wrong.join('; ')}</div>` : ''}
            ${r.over_listed ? `<div class="small">${r.over_listed} more listing${r.over_listed > 1 ? 's' : ''} than expected</div>` : ''}
            ${drafts ? `<ul class="eval-drafts small">${drafts}</ul>` : '<div class="small muted">no listings</div>'}
        </div>`;
    }

    function showResult(r) {
        const holder = document.querySelector(`#case-${CSS.escape(r.case_id)} [data-results]`);
        if (!holder) return;
        const old = holder.querySelector(`[data-tier="${r.tier}"]`);
        const wrap = document.createElement('div');
        wrap.dataset.tier = r.tier;
        wrap.innerHTML = resultCell(r);
        if (old) old.replaceWith(wrap); else holder.appendChild(wrap);
    }

    function renderSummary(byTier) {
        const rows = Object.entries(byTier).map(([tier, rs]) => {
            const ok = rs.filter((r) => !r.error);
            const avg = ok.length ? (ok.reduce((a, r) => a + r.score, 0) / ok.length).toFixed(1) : '—';
            const passed = ok.filter((r) => r.passed).length;
            const cost = ok.reduce((a, r) => a + r.cost, 0);
            const secs = ok.length ? (ok.reduce((a, r) => a + r.seconds, 0) / ok.length).toFixed(1) : '—';
            return `<tr><td><b>${esc(tier)}</b></td><td>${avg}</td><td>${passed}/${rs.length}</td><td>${rs.length - ok.length}</td><td>${money(cost)}</td><td>${secs}s</td></tr>`;
        }).join('');
        summaryEl.innerHTML = rows ? `<div class="bar"><span>this run</span></div><div class="table-wrap"><table>
            <thead><tr><th>model</th><th>avg score</th><th>passed</th><th>errors</th><th>cost</th><th>avg time</th></tr></thead>
            <tbody>${rows}</tbody></table></div>` : '';
    }

    function clearResults() {
        document.querySelectorAll('[data-results]').forEach((el) => { el.innerHTML = ''; });
    }

    runBtn.addEventListener('click', async () => {
        const tiers = Array.from(document.querySelectorAll('.tier-pick input:checked')).map((i) => i.value);
        if (!tiers.length) { alert('Pick at least one model'); return; }
        runBtn.disabled = true;
        clearResults();
        const runId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())).replace(/-/g, '');
        const byTier = {};
        const total = tiers.length * caseIds.length;
        let done = 0;
        progress.hidden = false;
        for (const tier of tiers) {
            byTier[tier] = [];
            for (const caseId of caseIds) {
                statusEl.textContent = `${tier}: ${caseId}`;
                countEl.textContent = `${done} / ${total}`;
                bar.style.width = `${Math.round((done / total) * 100)}%`;
                let r;
                try {
                    const res = await fetch('/api/eval/run', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ key, run_id: runId, case_id: caseId, tier }),
                    });
                    const data = await res.json();
                    r = data.success ? data.result : { case_id: caseId, tier, error: data.error || `HTTP ${res.status}` };
                } catch (e) {
                    r = { case_id: caseId, tier, error: e.message };
                }
                byTier[tier].push(r);
                showResult(r);
                renderSummary(byTier);
                done++;
            }
        }
        statusEl.textContent = 'done';
        countEl.textContent = `${done} / ${total}`;
        bar.style.width = '100%';
        runBtn.disabled = false;
    });

    document.querySelectorAll('[data-load-run]').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const res = await fetch(`/api/eval/runs/${encodeURIComponent(btn.dataset.loadRun)}?key=${encodeURIComponent(key)}`);
            const data = await res.json();
            if (!data.success) { alert(data.error); return; }
            clearResults();
            const byTier = {};
            data.results.forEach((r) => { (byTier[r.tier] = byTier[r.tier] || []).push(r); showResult(r); });
            renderSummary(byTier);
            window.scrollTo({ top: summaryEl.offsetTop - 120, behavior: 'smooth' });
        });
    });
})();
