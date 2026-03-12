"""
app/api/pages/render.py
────────────────────────
All HTML page generators in one place.
Routes call these functions and return their output directly.
"""
import time

from app.core.config import START_TIME
from app.core.storage import file_storage, global_stats, is_expired
from app.core.urls import effective_base_url


# ── Memory helper ─────────────────────────────────────────────────────────────
def _memory_mb() -> float:
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rss / (1024 * 1024) if rss > 10_000_000 else rss / 1024
    except Exception:
        return -1.0


_VID_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".m2ts"}


# ── File page (StreamOptic-style) ─────────────────────────────────────────────
def render_file_page(file_hash: str, info: dict) -> str:
    file_size  = int(info.get("file_size") or 0)
    size_text  = (
        f"{file_size / (1024**3):.2f} GB"
        if file_size >= 1024 ** 3
        else f"{file_size / (1024**2):.2f} MB"
    )
    expires_at = int(info["expires_at"])
    is_video   = bool(info.get("is_video")) or any(
        info.get("file_name", "").lower().endswith(e) for e in _VID_EXTS
    )

    base         = effective_base_url()
    media_url    = f"{base}/media/{file_hash}"
    download_url = f"{base}/download/{file_hash}"
    vlc_deep     = f"vlc://{media_url}"
    # Correct 1DM intent URL:
    # - Package: idm.internet.download.manager.adm  (1DM's real package name)
    # - scheme=https so 1DM knows to use HTTPS
    # - action=android.intent.action.VIEW so 1DM treats it as a download
    # - S.browser_fallback_url: if 1DM not installed, open download URL in browser
    # - Strip scheme from download_url for the intent host+path portion
    _dl_no_scheme = download_url.replace("https://", "").replace("http://", "")
    dm1_deep = (
        f"intent://{_dl_no_scheme}"
        f"#Intent"
        f";scheme=https"
        f";package=idm.internet.download.manager.adm"
        f";action=android.intent.action.VIEW"
        f";S.browser_fallback_url={download_url}"
        f";end"
    )

    if is_video:
        player_block = f"""
        <div class="player-wrap">
            <video id="vid" controls preload="none" playsinline src="/media/{file_hash}"></video>
            <div class="play-overlay" id="overlay" onclick="startPlay()">
                <div class="play-btn">&#9654;</div>
                <div class="play-label">Tap to Play</div>
            </div>
        </div>"""
    else:
        player_block = """
        <div class="player-wrap no-video">
            <svg width="40" height="40" fill="none" stroke="#8b949e" stroke-width="1.5" viewBox="0 0 24 24">
                <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
                <polyline points="13 2 13 9 20 9"/>
            </svg>
            <span>No preview for this file type</span>
        </div>"""

    audio_tip = (
        '<div class="tip tip-audio"><span class="tip-icon">🔇</span>'
        '<span><strong>Audio Problem?</strong> Use VLC Player for best playback on HEVC/x265 files.</span></div>'
        if is_video else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{info['file_name']}</title>
    <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
        :root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
               --accent:#2f81f7;--green:#3fb950;--orange:#f0883e}}
        html,body{{background:var(--bg);color:var(--text);font-family:'Outfit',sans-serif;min-height:100vh}}
        .page{{max-width:580px;margin:0 auto;padding-bottom:3rem}}
        .player-wrap{{position:relative;background:#000;width:100%;aspect-ratio:16/9;overflow:hidden}}
        .player-wrap.no-video{{display:flex;flex-direction:column;align-items:center;justify-content:center;
            gap:10px;color:var(--muted);font-size:.85rem;background:#0d1117;
            border-bottom:1px solid var(--border);aspect-ratio:unset;padding:2.5rem}}
        #vid{{width:100%;height:100%;display:block;object-fit:contain;position:relative;z-index:1}}
        .play-overlay{{position:absolute;inset:0;z-index:2;display:flex;flex-direction:column;
            align-items:center;justify-content:center;gap:10px;cursor:pointer;
            background:rgba(0,0,0,.55);transition:opacity .2s}}
        .play-overlay.hidden{{display:none}}
        .play-btn{{width:64px;height:64px;border-radius:50%;background:rgba(47,129,247,.9);
            display:flex;align-items:center;justify-content:center;font-size:1.6rem;color:#fff;
            box-shadow:0 0 32px rgba(47,129,247,.5);transition:transform .15s,box-shadow .15s}}
        .play-overlay:hover .play-btn{{transform:scale(1.08);box-shadow:0 0 48px rgba(47,129,247,.7)}}
        .play-label{{font-size:.8rem;color:rgba(255,255,255,.7);letter-spacing:.05em}}
        .info-strip{{padding:14px 16px 12px;background:var(--card);border-bottom:1px solid var(--border)}}
        .fname{{font-size:.95rem;font-weight:700;word-break:break-all;line-height:1.4;margin-bottom:8px}}
        .badges{{display:flex;gap:7px;flex-wrap:wrap}}
        .badge{{display:inline-flex;align-items:center;gap:4px;font-size:.71rem;font-weight:600;padding:3px 9px;border-radius:999px}}
        .badge-blue{{background:rgba(47,129,247,.15);color:#79c0ff;border:1px solid rgba(47,129,247,.3)}}
        .badge-green{{background:rgba(63,185,80,.15);color:#56d364;border:1px solid rgba(63,185,80,.3)}}
        .badge-amber{{background:rgba(240,136,62,.15);color:#ffa657;border:1px solid rgba(240,136,62,.3)}}
        .tip{{margin:10px 14px 0;padding:10px 14px;border-radius:10px;font-size:.82rem;font-weight:500;
            display:flex;align-items:center;gap:10px;line-height:1.45}}
        .tip-audio{{background:rgba(240,136,62,.1);border:1px solid rgba(240,136,62,.2);color:#ffa657}}
        .tip-speed{{background:rgba(47,129,247,.1);border:1px solid rgba(47,129,247,.2);color:#79c0ff}}
        .tip-icon{{font-size:1rem;flex-shrink:0}}
        .section-label{{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
            color:var(--muted);padding:18px 16px 8px}}
        .options{{padding:0 14px;display:flex;flex-direction:column;gap:9px}}
        .opt{{display:flex;align-items:center;gap:13px;padding:13px 14px;background:var(--card);
            border:1px solid var(--border);border-radius:14px;text-decoration:none;color:var(--text);
            position:relative;transition:border-color .15s,background .15s,transform .1s;cursor:pointer}}
        .opt:hover{{background:#1c2128;border-color:#484f58;transform:translateY(-1px)}}
        .opt:active{{transform:translateY(0)}}
        .opt-icon{{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:1.25rem;flex-shrink:0}}
        .ic-orange{{background:rgba(240,136,62,.15)}} .ic-blue{{background:rgba(47,129,247,.15)}}
        .ic-green{{background:rgba(63,185,80,.15)}}   .ic-gray{{background:rgba(139,148,158,.1)}}
        .opt-text{{flex:1;min-width:0}}
        .opt-title{{font-size:.93rem;font-weight:700;margin-bottom:2px}}
        .opt-sub{{font-size:.76rem;color:var(--muted)}}
        .tag{{position:absolute;top:-1px;right:12px;font-size:.6rem;font-weight:800;
            padding:2px 8px;border-radius:0 0 6px 6px;letter-spacing:.06em;text-transform:uppercase;color:#fff}}
        .tag-best{{background:var(--orange)}} .tag-fastest{{background:var(--accent)}}
        .opt-arr{{color:var(--muted);font-size:.9rem;flex-shrink:0}}
        .url-section{{padding:16px 14px 0}}
        .url-label{{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:7px}}
        .url-box{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;display:flex;align-items:center;gap:10px}}
        .url-text{{font-family:'Courier New',monospace;font-size:.7rem;color:var(--accent);word-break:break-all;flex:1;line-height:1.4}}
        .copy-btn{{background:rgba(47,129,247,.15);border:1px solid rgba(47,129,247,.3);color:#79c0ff;
            font-size:.72rem;font-weight:600;padding:5px 10px;border-radius:6px;cursor:pointer;
            white-space:nowrap;flex-shrink:0;font-family:'Outfit',sans-serif;transition:background .15s}}
        .copy-btn:hover{{background:rgba(47,129,247,.25)}}
        .disclaimer{{margin:14px 14px 0;padding:11px 13px;border-left:3px solid var(--orange);
            border-radius:0 8px 8px 0;font-size:.75rem;color:var(--muted);line-height:1.5;background:rgba(240,136,62,.05)}}
        .disclaimer strong{{color:var(--orange)}}
        .modal-bg{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;
            align-items:center;justify-content:center;padding:16px}}
        .modal-bg.open{{display:flex}}
        .modal{{background:#1c2128;border:1px solid var(--border);border-radius:16px;padding:24px;width:min(96vw,440px)}}
        .modal h3{{font-size:1rem;margin-bottom:6px}}
        .modal p{{font-size:.82rem;color:var(--muted);margin-bottom:14px;line-height:1.5}}
        .modal-url{{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 12px;
            font-family:'Courier New',monospace;font-size:.72rem;color:var(--accent);word-break:break-all;
            margin-bottom:14px;line-height:1.4;user-select:all}}
        .modal-btns{{display:flex;gap:10px;justify-content:flex-end}}
        .mbtn{{padding:8px 16px;border-radius:8px;font-size:.85rem;font-weight:600;cursor:pointer;
            border:none;font-family:'Outfit',sans-serif}}
        .mbtn-copy{{background:rgba(47,129,247,.2);color:#79c0ff;border:1px solid rgba(47,129,247,.3)}}
        .mbtn-close{{background:rgba(139,148,158,.15);color:var(--muted);border:1px solid var(--border)}}
        #opt-1dm{{display:none}}
        @media(max-width:420px){{.opt{{padding:11px 12px;gap:10px}}.opt-icon{{width:40px;height:40px}}}}
    </style>
</head>
<body>
<div class="page">
    {player_block}
    <div class="info-strip">
        <div class="fname">{info['file_name']}</div>
        <div class="badges">
            <span class="badge badge-blue">📦 {size_text}</span>
            <span class="badge badge-green">✅ Secure</span>
            <span class="badge badge-amber">⏳ <span id="cd">--:--</span></span>
        </div>
    </div>
    {audio_tip}
    <div class="tip tip-speed">
        <span class="tip-icon">⚡</span>
        <span id="tip-speed-text"><strong>Pro Tip:</strong> Use <strong>1DM</strong> (Android) for maximum download speed.</span>
    </div>
    <div class="section-label">Choose Action</div>
    <div class="options">
        <a class="opt" id="opt-vlc" href="{vlc_deep}" onclick="return handleVlc(event)">
            <span class="tag tag-best">BEST</span>
            <div class="opt-icon ic-orange">📺</div>
            <div class="opt-text">
                <div class="opt-title" style="color:#ffa657">Play in VLC</div>
                <div class="opt-sub">Recommended · Full audio &amp; video support</div>
            </div>
            <span class="opt-arr">›</span>
        </a>
        <a class="opt" id="opt-1dm" href="{dm1_deep}" onclick="open1DM(event,this)">
            <span class="tag tag-fastest">FASTEST</span>
            <div class="opt-icon ic-blue">⚡</div>
            <div class="opt-text">
                <div class="opt-title" style="color:#79c0ff">1DM Download</div>
                <div class="opt-sub">Fastest Speed · Multi-thread download</div>
            </div>
            <span class="opt-arr">›</span>
        </a>
        <a class="opt" href="/stream/{file_hash}">
            <div class="opt-icon ic-green">▶️</div>
            <div class="opt-text">
                <div class="opt-title" style="color:#56d364">Stream in Browser</div>
                <div class="opt-sub">Watch now · No app needed</div>
            </div>
            <span class="opt-arr">›</span>
        </a>
        <a class="opt" href="/download/{file_hash}">
            <div class="opt-icon ic-gray">⬇️</div>
            <div class="opt-text">
                <div class="opt-title">Direct Download</div>
                <div class="opt-sub">Save to device · Standard speed</div>
            </div>
            <span class="opt-arr">›</span>
        </a>
    </div>
    <div class="url-section">
        <div class="url-label">Source URL</div>
        <div class="url-box">
            <div class="url-text" id="srcurl">{media_url}</div>
            <button class="copy-btn" onclick="copyUrl()">Copy</button>
        </div>
    </div>
    <div class="disclaimer">
        <strong>⚠️ Note:</strong> Links expire in <strong><span id="cd2">--:--</span></strong>. For personal use only.
    </div>
</div>

<div class="modal-bg" id="vlc-modal">
    <div class="modal">
        <h3>📺 Open in VLC</h3>
        <p>Copy this URL and paste it into VLC:<br><em>Media → Open Network Stream → paste URL → Play</em></p>
        <div class="modal-url" id="vlc-url">{media_url}</div>
        <div class="modal-btns">
            <button class="mbtn mbtn-close" onclick="closeModal()">Close</button>
            <button class="mbtn mbtn-copy" onclick="copyVlc()">Copy URL</button>
        </div>
    </div>
</div>

<script>
    const EXP = {expires_at};
    function fmt(s){{
        if(s<=0)return'Expired';
        const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
        return h>0?h+'h '+m+'m':m+'m '+String(sec).padStart(2,'0')+'s';
    }}
    function tick(){{
        const l=Math.floor(EXP-Date.now()/1000);
        document.getElementById('cd').textContent=fmt(l);
        document.getElementById('cd2').textContent=fmt(l);
        if(l>0)setTimeout(tick,1000);
    }}
    tick();
    const isAndroid=/android/i.test(navigator.userAgent);
    const isMobile=/android|iphone|ipad|ipod|mobile/i.test(navigator.userAgent);
    if(isAndroid){{document.getElementById('opt-1dm').style.display='flex';}}
    function open1DM(e,el){{
        e.preventDefault();
        const intentUrl=el.href;
        // Try to open 1DM via intent. If not installed, Android will not open anything.
        // We set a short timeout: if the app didn't open (page still visible), 
        // fall back to the direct download URL.
        let appOpened=false;
        const fallback='{download_url}';
        window.location.href=intentUrl;
        // If 1DM opens, this timeout fires but page is hidden so nothing happens.
        // If 1DM is NOT installed, page stays visible and we redirect to direct download.
        setTimeout(function(){{
            if(!document.hidden){{window.location.href=fallback;}}
        }},1500);
    }}
    else{{document.getElementById('tip-speed-text').innerHTML='<strong>Pro Tip:</strong> Use <strong>VLC</strong> for best playback, or copy the Source URL into any download manager.';}}
    function handleVlc(e){{if(!isMobile){{e.preventDefault();document.getElementById('vlc-modal').classList.add('open');}}return isMobile;}}
    function closeModal(){{document.getElementById('vlc-modal').classList.remove('open');}}
    function copyVlc(){{navigator.clipboard.writeText(document.getElementById('vlc-url').textContent).then(()=>{{const b=document.querySelector('.mbtn-copy');b.textContent='Copied!';setTimeout(()=>b.textContent='Copy URL',2000);}});}}
    document.getElementById('vlc-modal').addEventListener('click',function(e){{if(e.target===this)closeModal();}});
    function copyUrl(){{navigator.clipboard.writeText(document.getElementById('srcurl').textContent).then(()=>{{const b=document.querySelector('.copy-btn');b.textContent='Copied!';setTimeout(()=>b.textContent='Copy',2000);}});}}
    function startPlay(){{const o=document.getElementById('overlay'),v=document.getElementById('vid');if(o)o.classList.add('hidden');if(v)v.play();}}
</script>
</body></html>"""


# ── Stream page ───────────────────────────────────────────────────────────────
def render_stream_page(file_hash: str, info: dict) -> str:
    file_size = int(info.get("file_size") or 0)
    size_text = (
        f"{file_size / (1024**3):.2f} GB"
        if file_size >= 1024 ** 3
        else f"{file_size / (1024**2):.2f} MB"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>▶️ {info['file_name']}</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
        body{{margin:0;background:#000;color:#fff;font-family:Arial,sans-serif}}
        .bar{{display:flex;align-items:center;gap:12px;padding:10px 16px;
              background:rgba(255,255,255,.06);border-bottom:1px solid rgba(255,255,255,.1)}}
        .bar a{{color:#60a5fa;text-decoration:none;font-size:.9rem}}
        .title{{font-weight:700;font-size:1rem;word-break:break-word;flex:1}}
        .meta{{opacity:.6;font-size:.85rem;white-space:nowrap}}
        video{{display:block;width:100%;max-height:calc(100vh - 56px);background:#000}}
    </style>
</head>
<body>
<div class="bar">
    <a href="/file/{file_hash}">⬅ Back</a>
    <span class="title">{info['file_name']}</span>
    <span class="meta">{size_text}</span>
</div>
<video controls preload="none" src="/media/{file_hash}"></video>
</body></html>"""


# ── Admin dashboard ───────────────────────────────────────────────────────────
def render_admin_page() -> str:
    from threading import Lock
    from app.core.storage import _storage_lock

    with _storage_lock:
        active = [v for v in file_storage.values() if not is_expired(v)]
        stats  = dict(global_stats)

    top    = sorted(active, key=lambda x: int(x.get("downloads", 0)), reverse=True)[:5]
    uptime = int(time.time() - START_TIME)
    mem    = _memory_mb()

    uptime_str = f"{uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s"
    mem_str    = f"{mem:.1f} MB" if mem >= 0 else "N/A"
    top_rows = "".join(
        f'<tr><td class="fname">{f["file_name"]}</td>'
        f'<td class="num">{int(f.get("downloads",0))}</td>'
        f'<td class="num streams">{int(f.get("streams",0))}</td></tr>'
        for f in top
    ) or '<tr><td colspan="3" class="empty">No files yet</td></tr>'

    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="robots" content="noindex,nofollow,noarchive">
    <title>Admin Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800&display=swap" rel="stylesheet">
    <style>
        *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
        body{{font-family:'Outfit',sans-serif;background:#0a0f1e;color:#e2e8f0;min-height:100vh;padding:2rem 1rem}}
        .page{{max-width:920px;margin:0 auto}}
        .header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.75rem;
            padding-bottom:1rem;border-bottom:1px solid rgba(255,255,255,.08);flex-wrap:wrap;gap:10px}}
        .header h1{{font-size:clamp(1.3rem,4vw,1.7rem);font-weight:800;
            background:linear-gradient(135deg,#60a5fa,#a78bfa);-webkit-background-clip:text;
            -webkit-text-fill-color:transparent;background-clip:text}}
        .live-badge{{background:rgba(63,185,80,.15);border:1px solid rgba(63,185,80,.3);color:#56d364;
            font-size:.72rem;font-weight:700;padding:4px 12px;border-radius:999px;
            display:flex;align-items:center;gap:5px}}
        .live-dot{{width:6px;height:6px;border-radius:50%;background:#56d364;animation:pulse 2s infinite}}
        @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
        .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:2rem}}
        .stat{{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.09);border-radius:16px;
            padding:16px 18px;transition:transform .15s,border-color .15s}}
        .stat:hover{{transform:translateY(-2px);border-color:rgba(255,255,255,.18)}}
        .stat-icon{{font-size:1.3rem;margin-bottom:8px}}
        .stat-val{{font-size:1.75rem;font-weight:800;line-height:1;margin-bottom:4px}}
        .stat-label{{font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:#8b949e;font-weight:600}}
        .c-blue{{color:#60a5fa}} .c-green{{color:#34d399}} .c-purple{{color:#a78bfa}}
        .c-orange{{color:#fb923c}} .c-yellow{{color:#fbbf24}} .c-pink{{color:#f472b6}}
        .section-head{{font-size:.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
            color:#8b949e;margin-bottom:.75rem;display:flex;align-items:center;gap:8px}}
        .section-head::after{{content:"";flex:1;height:1px;background:rgba(255,255,255,.07)}}
        .tbl-wrap{{border:1px solid rgba(255,255,255,.09);border-radius:14px;overflow:hidden}}
        table{{width:100%;border-collapse:collapse;font-size:.875rem}}
        thead tr{{background:rgba(255,255,255,.05)}}
        th{{padding:11px 16px;font-weight:700;font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:#8b949e;white-space:nowrap}}
        th:first-child,td:first-child{{text-align:left}}
        th.rc,td.num{{text-align:center}}
        td{{padding:11px 16px;border-top:1px solid rgba(255,255,255,.05);vertical-align:middle}}
        td.fname{{word-break:break-all;max-width:380px;font-size:.82rem;line-height:1.4}}
        td.num{{font-weight:700;color:#60a5fa;font-size:.95rem}}
        td.streams{{color:#a78bfa}}
        tbody tr:hover td{{background:rgba(255,255,255,.03)}}
        td.empty{{text-align:center;padding:2rem;color:#8b949e;font-style:italic}}
        @media(max-width:500px){{.stat-val{{font-size:1.4rem}}.header h1{{font-size:1.2rem}}td,th{{padding:9px 10px}}}}
    </style>
</head>
<body>
<div class="page">
    <div class="header">
        <h1>📊 Admin Dashboard</h1>
        <div class="live-badge"><div class="live-dot"></div> LIVE</div>
    </div>
    <div class="stats">
        <div class="stat"><div class="stat-icon">📁</div><div class="stat-val c-blue">{stats['total_files_uploaded']}</div><div class="stat-label">Uploaded</div></div>
        <div class="stat"><div class="stat-icon">✅</div><div class="stat-val c-green">{len(active)}</div><div class="stat-label">Active</div></div>
        <div class="stat"><div class="stat-icon">⬇️</div><div class="stat-val c-purple">{stats['total_downloads']}</div><div class="stat-label">Downloads</div></div>
        <div class="stat"><div class="stat-icon">▶️</div><div class="stat-val c-orange">{stats['total_streams']}</div><div class="stat-label">Streams</div></div>
        <div class="stat"><div class="stat-icon">🧠</div><div class="stat-val c-yellow">{mem_str}</div><div class="stat-label">Memory</div></div>
        <div class="stat"><div class="stat-icon">⏱️</div><div class="stat-val c-pink">{uptime_str}</div><div class="stat-label">Uptime</div></div>
    </div>
    <div class="section-head">🏆 Top 5 Downloads</div>
    <div class="tbl-wrap">
        <table>
            <thead><tr><th>File Name</th><th class="rc">Downloads</th><th class="rc">Streams</th></tr></thead>
            <tbody>{top_rows}</tbody>
        </table>
    </div>
</div>
</body></html>"""
