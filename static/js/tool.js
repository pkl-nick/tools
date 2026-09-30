// Live price quote on the tool detail page
(function () {
    const form = document.getElementById('bookForm');
    if (!form) return;
    const quoteEl = document.getElementById('quote');
    const money = (n) => '$' + Number(n).toFixed(2);

    async function refreshQuote() {
        const params = new URLSearchParams({
            tool_id: form.dataset.toolId,
            start: form.start.value,
            end: form.end.value,
            delivery: form.delivery && form.delivery.checked ? '1' : '0',
            hour: form.hour.value,
        });
        try {
            const res = await fetch('/api/quote?' + params);
            const q = await res.json();
            if (!q.success) {
                quoteEl.textContent = q.error;
                return;
            }
            let rows = `<div><span>${money(q.daily_price)} x ${q.days} day${q.days > 1 ? 's' : ''}</span><span>${money(q.rental_total + q.weekly_discount)}</span></div>`;
            if (q.weekly_discount) rows += `<div><span>Weekly discount</span><span>-${money(q.weekly_discount)}</span></div>`;
            rows += `<div><span>Service fee</span><span>${money(q.service_fee)}</span></div>`;
            if (q.delivery_fee) rows += `<div><span>Delivery (${q.miles} mi)</span><span>${money(q.delivery_fee)}</span></div>`;
            rows += `<div class="total"><span>Total</span><span>${money(q.total_charge)}</span></div>`;
            rows += `<div class="muted small"><span>Deposit hold (refunded on return)</span><span>${money(q.deposit_hold)}</span></div>`;
            quoteEl.innerHTML = rows;
        } catch (e) {
            quoteEl.textContent = 'Could not load a quote.';
        }
    }

    form.start.addEventListener('change', () => {
        if (form.end.value < form.start.value) form.end.value = form.start.value;
        form.end.min = form.start.value;
        refreshQuote();
    });
    ['end', 'hour'].forEach((n) => form[n].addEventListener('change', refreshQuote));
    if (form.delivery) form.delivery.addEventListener('change', refreshQuote);
    refreshQuote();
})();
