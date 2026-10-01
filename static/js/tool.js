// Live price quote on the tool detail page
(function () {
    const form = document.getElementById('bookForm');
    if (!form) return;
    const quoteEl = document.getElementById('quote');
    const courierFields = document.getElementById('courierFields');
    const courierBtn = document.getElementById('courierQuote');
    const courierStatus = document.getElementById('courierStatus');
    const money = (n) => '$' + Number(n).toFixed(2);
    const method = () => (form.querySelector('input[name="delivery_method"]:checked') || {}).value || 'pickup';

    function render(q) {
        let rows = `<div><span>${money(q.daily_price)} x ${q.days} day${q.days > 1 ? 's' : ''}</span><span>${money(q.rental_total + q.weekly_discount)}</span></div>`;
        if (q.weekly_discount) rows += `<div><span>Weekly discount</span><span>-${money(q.weekly_discount)}</span></div>`;
        rows += `<div><span>Service fee</span><span>${money(q.service_fee)}</span></div>`;
        if (q.method === 'courier') rows += `<div><span>Courier, both ways</span><span>${money(q.delivery_fee)}</span></div>`;
        else if (q.delivery_fee) rows += `<div><span>Owner drop-off (${q.miles} mi)</span><span>${money(q.delivery_fee)}</span></div>`;
        rows += `<div class="total"><span>Total</span><span>${money(q.total_charge)}</span></div>`;
        if (q.deposit_hold) rows += `<div class="muted small"><span>Deposit hold (refunded on return)</span><span>${money(q.deposit_hold)}</span></div>`;
        quoteEl.innerHTML = rows;
    }

    async function refreshQuote() {
        if (method() === 'courier') {
            quoteEl.textContent = 'enter your address and tap "get courier price".';
            return;
        }
        const params = new URLSearchParams({
            tool_id: form.dataset.toolId,
            start: form.start.value,
            end: form.end.value,
            method: method(),
            hour: form.hour.value,
        });
        try {
            const res = await fetch('/api/quote?' + params);
            const q = await res.json();
            if (!q.success) {
                quoteEl.textContent = q.error;
                return;
            }
            render(q);
        } catch (e) {
            quoteEl.textContent = 'Could not load a quote.';
        }
    }

    async function courierQuote() {
        courierBtn.disabled = true;
        courierStatus.textContent = 'asking Uber for a price…';
        const body = new FormData(form);
        body.set('tool_id', form.dataset.toolId);
        try {
            const res = await fetch('/api/courier-quote', { method: 'POST', body });
            const q = await res.json();
            if (!q.success) {
                courierStatus.textContent = q.error;
                return;
            }
            render(q);
            courierStatus.textContent = q.courier_minutes
                ? `About ${q.courier_minutes} min door to door. The final price is set when the courier is booked.`
                : 'The final price is set when the courier is booked.';
        } catch (e) {
            courierStatus.textContent = 'Could not get a courier price.';
        } finally {
            courierBtn.disabled = false;
        }
    }

    form.start.addEventListener('change', () => {
        if (form.end.value < form.start.value) form.end.value = form.start.value;
        form.end.min = form.start.value;
        refreshQuote();
    });
    ['end', 'hour'].forEach((n) => form[n].addEventListener('change', refreshQuote));
    form.querySelectorAll('input[name="delivery_method"]').forEach((r) => r.addEventListener('change', () => {
        if (courierFields) courierFields.hidden = method() !== 'courier';
        refreshQuote();
    }));
    if (courierBtn) courierBtn.addEventListener('click', courierQuote);
    refreshQuote();
})();
