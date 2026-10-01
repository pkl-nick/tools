// Profile location (sign-up, welcome, account): fills the hidden lat/lng fields.
// Asks permission when the button is tapped; if permission was already granted it fills in silently.
// The server rounds to ~100 m, so exact addresses are never stored.
(function () {
    const btn = document.getElementById('useLocation');
    if (!btn) return;
    const status = document.getElementById('locationStatus');
    const lat = document.querySelector('input[name="lat"]');
    const lng = document.querySelector('input[name="lng"]');

    async function locate() {
        status.textContent = 'Asking your browser for your location…';
        try {
            const pos = await window.toolshareGeo.request();
            lat.value = pos.lat;
            lng.value = pos.lng;
            status.textContent = '✓ Location set (rounded to about a block, never shown).';
            btn.textContent = 'update location';
        } catch (e) {
            status.textContent = e.message + ' Distances will use your neighborhood\'s area until then.';
        }
    }

    btn.addEventListener('click', locate);
    window.toolshareGeo.permissionState().then((state) => {
        if (state === 'granted' && !lat.value) locate();
        if (state === 'prompt') btn.classList.add('btn-primary');
    });
})();
