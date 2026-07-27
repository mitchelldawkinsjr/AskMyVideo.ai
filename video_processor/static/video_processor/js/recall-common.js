function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

function formatTime(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function formatMatchPercent(result) {
    const raw = typeof result === 'object'
        ? (result.confidence ?? result.semantic_similarity ?? result.relevance_score)
        : result;
    if (raw == null || raw <= 0) return null;
    return Math.min(100, Math.round(raw * 100));
}

function locatorLabel(result) {
    if (!result) return '';
    if (result.locator_label) return result.locator_label;
    if (result.page != null || result.content_kind === 'document') {
        return `page ${result.page || 1}`;
    }
    if (result.timestamp) return result.timestamp;
    return formatTime(result.start_time || 0);
}

function closeVideoPlayer() {
    const overlay = document.querySelector('.video-overlay');
    if (overlay) overlay.remove();
}

/**
 * Open a vault item at a locator.
 * playSegment(jobId, startTime) — media seek (legacy)
 * playSegment(jobId, startTime, page) — document page
 * playSegment(jobId, { start_time, page, content_kind }) — object form
 */
async function playSegment(videoId, timestampOrOpts, pageArg) {
    let timestamp = 0;
    let page = null;
    if (timestampOrOpts && typeof timestampOrOpts === 'object') {
        timestamp = timestampOrOpts.start_time || 0;
        page = timestampOrOpts.page != null ? timestampOrOpts.page : null;
    } else {
        timestamp = timestampOrOpts || 0;
        page = pageArg != null ? pageArg : null;
    }

    const overlay = document.createElement('div');
    overlay.className = 'video-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.9);z-index:10000;display:flex;align-items:center;justify-content:center;';
    overlay.innerHTML = '<div style="background:#ffffff;color:#1a1c20;border-radius:15px;padding:40px;text-align:center;"><p>Loading...</p></div>';
    document.body.appendChild(overlay);

    try {
        const response = await fetch(`/api/video/${videoId}/`);
        const videoData = await response.json();
        if (!response.ok) throw new Error(videoData.error || 'Failed to load content details');

        let playerHTML = '';
        const isDocument = videoData.media_type === 'document' || videoData.content_kind === 'document';

        if (isDocument) {
            const pageNum = page || 1;
            const fileUrl = videoData.file_url || `/video-file/${videoId}/`;
            playerHTML = `
                <iframe title="PDF preview" src="${fileUrl}#page=${pageNum}"
                    style="width:100%;height:70vh;border:none;border-radius:8px;background:#f5f5f5;"></iframe>
                <div style="margin-top:15px;text-align:center;color:#434750;font-size:14px;">
                    Page ${pageNum}
                    <a href="${fileUrl}#page=${pageNum}" target="_blank"
                       style="color:#002753;margin-left:12px;">Open PDF</a>
                </div>`;
        } else if (videoData.is_youtube && videoData.youtube_video_id) {
            const youtubeId = videoData.youtube_video_id;
            const startTime = Math.floor(timestamp);
            playerHTML = `
                <iframe width="800" height="450"
                    src="https://www.youtube.com/embed/${youtubeId}?start=${startTime}&autoplay=1"
                    frameborder="0" allowfullscreen style="border-radius:8px;max-width:100%;"></iframe>
                <div style="margin-top:15px;text-align:center;color:#434750;font-size:14px;">
                    Starting at ${formatTime(timestamp)}
                    <a href="https://www.youtube.com/watch?v=${youtubeId}&t=${startTime}s" target="_blank"
                       style="color:#002753;margin-left:12px;">Open on YouTube</a>
                </div>`;
        } else if (videoData.media_type === 'audio') {
            const contentType = videoData.content_type || 'audio/mpeg';
            playerHTML = `
                <audio controls autoplay style="width:100%;max-width:800px;border-radius:8px;"
                    onloadedmetadata="this.currentTime=${timestamp}">
                    <source src="/video-file/${videoId}/" type="${contentType}">
                </audio>
                <div style="margin-top:15px;text-align:center;color:#434750;font-size:14px;">
                    Starting at ${formatTime(timestamp)}
                </div>`;
        } else {
            playerHTML = `
                <video width="800" height="450" controls autoplay style="border-radius:8px;max-width:100%;"
                    onloadedmetadata="this.currentTime=${timestamp}">
                    <source src="/video-file/${videoId}/" type="video/mp4">
                </video>
                <div style="margin-top:15px;text-align:center;color:#434750;font-size:14px;">
                    Starting at ${formatTime(timestamp)}
                </div>`;
        }

        overlay.innerHTML = `
            <div style="background:#ffffff;border-radius:15px;padding:24px;max-width:900px;width:95%;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
                    <h3 style="margin:0;color:#002753;">${videoData.video_name || videoData.title}</h3>
                    <button onclick="closeVideoPlayer()" style="background:#ba1a1a;color:white;border:none;border-radius:50%;width:36px;height:36px;cursor:pointer;">×</button>
                </div>
                <div style="text-align:center;">${playerHTML}</div>
            </div>`;
    } catch (error) {
        overlay.innerHTML = `
            <div style="background:#ffffff;border-radius:15px;padding:40px;text-align:center;max-width:500px;">
                <h3 style="color:#93000a;">Error Loading Content</h3>
                <p>${error.message}</p>
                <button onclick="closeVideoPlayer()" style="margin-top:16px;background:#002753;color:white;padding:10px 20px;border:none;border-radius:8px;cursor:pointer;">Close</button>
            </div>`;
    }

    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeVideoPlayer();
    });
}
