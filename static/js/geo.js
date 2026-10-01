// Shared browser-location helper. The browser shows its own permission prompt the
// first time request() runs; we only call it after a tap so the prompt has context.
window.toolshareGeo = (function () {
    async function permissionState() {
        try {
            if (!navigator.permissions) return 'unknown';
            return (await navigator.permissions.query({ name: 'geolocation' })).state;   // granted | prompt | denied
        } catch (e) {
            return 'unknown';    // Safari on older iOS can't query this
        }
    }

    function request() {
        return new Promise((resolve, reject) => {
            if (!navigator.geolocation) {
                reject(new Error('This browser can\'t share location.'));
                return;
            }
            navigator.geolocation.getCurrentPosition(
                (pos) => resolve({ lat: pos.coords.latitude.toFixed(4), lng: pos.coords.longitude.toFixed(4) }),
                (err) => reject(new Error(err.code === 1
                    ? 'Location permission was denied. You can allow it in your browser\'s site settings.'
                    : 'Couldn\'t get your location. Try again outside or with Wi-Fi on.')),
                { enableHighAccuracy: false, timeout: 12000, maximumAge: 300000 }
            );
        });
    }

    return { permissionState, request };
})();
