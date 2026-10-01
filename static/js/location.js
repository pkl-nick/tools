// "Use my location": fills the hidden lat/lng fields from the browser's geolocation.
// The server rounds to ~100 m, so exact addresses are never stored.
(function () {
    const btn = document.getElementById('useLocation');
    if (!btn) return;
    const status = document.getElementById('locationStatus');
    const lat = document.querySelector('input[name="lat"]');
    const lng = document.querySelector('input[name="lng"]');

    btn.addEventListener('click', () => {
        if (!navigator.geolocation) {
            status.textContent = 'Your browser can\'t share location. Distances will use the city center.';
            return;
        }
        status.textContent = 'Finding you…';
        navigator.geolocation.getCurrentPosition(
            (pos) => {
                lat.value = pos.coords.latitude.toFixed(4);
                lng.value = pos.coords.longitude.toFixed(4);
                status.textContent = '✓ Location set (rounded to about a block).';
                btn.textContent = 'update location';
            },
            () => { status.textContent = 'Location blocked. Distances will use the city center until you allow it.'; },
            { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 }
        );
    });
})();
