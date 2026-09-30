// Snap-to-list: resize photos or pull frames from a video in the browser,
// then send each image to /api/analyze-photo one at a time.
(function () {
    const MAX_EDGE = 1600;            // resize before upload: faster and cheaper, plenty for label reading
    const MAX_VIDEO_FRAMES = 24;      // cap on frames sent per video
    const MIN_FRAME_GAP_S = 1.5;      // sample at most one frame every 1.5 s
    const SAME_SCENE_THRESHOLD = 10;  // mean pixel difference below this = camera hasn't moved on

    const card = document.getElementById('progressCard');
    const title = document.getElementById('progressTitle');
    const count = document.getElementById('progressCount');
    const bar = document.getElementById('progressBar');
    const log = document.getElementById('log');
    const reviewBtn = document.getElementById('reviewBtn');

    const newBatchId = () =>
        (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));

    function addLog(text, cls) {
        const li = document.createElement('li');
        li.textContent = text;
        if (cls) li.className = cls;
        log.appendChild(li);
    }

    function setProgress(done, total, label) {
        card.hidden = false;
        title.textContent = label;
        count.textContent = total ? `${done} / ${total}` : '';
        bar.style.width = total ? `${Math.round((done / total) * 100)}%` : '5%';
    }

    function drawScaled(source, width, height) {
        const scale = Math.min(1, MAX_EDGE / Math.max(width, height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(width * scale);
        canvas.height = Math.round(height * scale);
        canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
        return canvas;
    }

    const canvasToJpeg = (canvas) => new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.85));

    async function imageFileToJpeg(file) {
        try {
            const bitmap = await createImageBitmap(file);
            return await canvasToJpeg(drawScaled(bitmap, bitmap.width, bitmap.height));
        } catch (e) {
            return file;  // browser can't decode it (e.g. HEIC on desktop): send as-is, server will validate
        }
    }

    // Tiny grayscale fingerprint used to skip frames that show the same scene
    function fingerprint(canvas) {
        const small = document.createElement('canvas');
        small.width = small.height = 32;
        const ctx = small.getContext('2d');
        ctx.drawImage(canvas, 0, 0, 32, 32);
        const data = ctx.getImageData(0, 0, 32, 32).data;
        const out = new Float32Array(32 * 32);
        for (let i = 0; i < out.length; i++) {
            out[i] = 0.299 * data[i * 4] + 0.587 * data[i * 4 + 1] + 0.114 * data[i * 4 + 2];
        }
        return out;
    }

    function meanDiff(a, b) {
        let sum = 0;
        for (let i = 0; i < a.length; i++) sum += Math.abs(a[i] - b[i]);
        return sum / a.length;
    }

    function seek(video, t) {
        return new Promise((resolve) => {
            video.addEventListener('seeked', resolve, { once: true });
            video.currentTime = t;
        });
    }

    async function extractFrames(file) {
        const video = document.createElement('video');
        video.muted = true;
        video.playsInline = true;
        video.preload = 'auto';
        video.src = URL.createObjectURL(file);
        await new Promise((resolve, reject) => {
            video.addEventListener('loadeddata', resolve, { once: true });
            video.addEventListener('error', () => reject(new Error('This browser cannot play that video format.')), { once: true });
        });

        // Some recordings (e.g. WebM) report Infinity until you seek past the end
        if (!Number.isFinite(video.duration)) {
            await seek(video, 1e7);
            await seek(video, 0);
        }
        const duration = video.duration;
        if (!Number.isFinite(duration) || duration <= 0) throw new Error('Could not read the video length.');
        const step = Math.max(MIN_FRAME_GAP_S, duration / (MAX_VIDEO_FRAMES * 2));
        const frames = [];
        let last = null;
        for (let t = 0.3; t < duration && frames.length < MAX_VIDEO_FRAMES; t += step) {
            await seek(video, t);
            const canvas = drawScaled(video, video.videoWidth, video.videoHeight);
            const fp = fingerprint(canvas);
            if (last && meanDiff(fp, last) < SAME_SCENE_THRESHOLD) continue;
            last = fp;
            frames.push({ blob: await canvasToJpeg(canvas), label: `frame at ${t.toFixed(1)}s` });
            setProgress(0, 0, `Pulling frames from video... ${frames.length} so far`);
        }
        URL.revokeObjectURL(video.src);
        return frames;
    }

    async function analyzeAll(items, batchId, source) {
        let created = 0;
        let dupes = 0;
        let demo = false;
        for (let i = 0; i < items.length; i++) {
            setProgress(i, items.length, 'Identifying tools...');
            const body = new FormData();
            body.append('photo', items[i].blob, `upload-${i}.jpg`);
            body.append('batch_id', batchId);
            body.append('source', source);   // video frames use the cheaper model
            try {
                const res = await fetch('/api/analyze-photo', { method: 'POST', body });
                const data = await res.json();
                if (!data.success) throw new Error(data.error || 'Upload failed');
                created += data.drafts_created;
                dupes += data.duplicates_skipped || 0;
                demo = demo || data.demo_mode;
                addLog(data.names.length
                    ? `${items[i].label}: ${data.names.join(', ')}`
                    : `${items[i].label}: no new tools`, data.names.length ? 'ok' : 'muted');
            } catch (e) {
                addLog(`${items[i].label}: ${e.message}`, 'err');
            }
        }
        setProgress(items.length, items.length,
            `Done: ${created} draft listing${created === 1 ? '' : 's'}` + (dupes ? `, ${dupes} duplicates merged` : '') +
            (demo ? ' (demo mode sample drafts)' : ''));
        reviewBtn.hidden = created === 0;
    }

    function reset() {
        log.innerHTML = '';
        reviewBtn.hidden = true;
    }

    document.getElementById('photoInput').addEventListener('change', async (e) => {
        const files = Array.from(e.target.files);
        if (!files.length) return;
        reset();
        setProgress(0, files.length, 'Preparing photos...');
        const items = [];
        for (const f of files) items.push({ blob: await imageFileToJpeg(f), label: f.name });
        await analyzeAll(items, newBatchId(), 'photo');
        e.target.value = '';
    });

    document.getElementById('videoInput').addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        reset();
        setProgress(0, 0, 'Loading video...');
        try {
            const frames = await extractFrames(file);
            if (!frames.length) throw new Error('No usable frames found in that video.');
            addLog(`Pulled ${frames.length} distinct frames from the video.`, 'muted');
            // One batch id for the whole video, so the server merges the same tool seen in several frames
            await analyzeAll(frames, newBatchId(), 'video');
        } catch (err) {
            setProgress(0, 0, 'Could not read that video');
            addLog(err.message, 'err');
        }
        e.target.value = '';
    });
})();
