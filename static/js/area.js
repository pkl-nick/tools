// Visitors: search near a ZIP code, or near where they are (asks the browser for permission on tap)
(function () {
    const form = document.getElementById('areaForm');
    const here = document.getElementById('areaHere');
    if (!form || !here) return;
    here.addEventListener('click', async () => {
        here.disabled = true;
        here.textContent = 'locating…';
        try {
            const pos = await window.toolshareGeo.request();
            form.lat.value = pos.lat;
            form.lng.value = pos.lng;
            form.postal_code.required = false;
            form.submit();
        } catch (e) {
            here.disabled = false;
            here.textContent = 'use my location';
            alert(e.message);
        }
    });
})();
