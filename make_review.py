#!/usr/bin/env python3
"""Generate review.html (timeline editor) for the /cut-video skill.

Usage: python3 make_review.py <workdir>
Reads  <workdir>/keeps.json   — list of [start, end, "transcript text"] triplets
Needs  <workdir>/proxy.mp4    — playback source (duration probed from it)
Uses   <workdir>/audio.wav    — 16kHz mono wav (extracted from proxy if missing)
Writes <workdir>/review.html  — open in a browser

Timeline editor with a canvas waveform (crisp at any zoom, peaks embedded in
the HTML so it works on file:// with no server):
  - pinch / ctrl+wheel zoom centered on the cursor, click to scrub
  - drag ACROSS the waveform to delete that range from the keep blocks
  - auto-suggested silence bands inside blocks — click one to cut it, or
    "Cut all" with a min-duration slider
  - drag block edges to trim, split at playhead, drop/restore blocks,
    double-click a gap to resurrect cut footage, undo stack
  - "Copy decisions" exports {"keeps": [[a,b], ...]} for Claude to re-render
"""
import json, sys, html, pathlib, subprocess, wave as wavemod, array

workdir = pathlib.Path(sys.argv[1])
keeps = json.load(open(workdir / "keeps.json"))
segs = [{"a": round(k[0], 3), "b": round(k[1], 3), "t": (k[2] if len(k) > 2 else "")}
        for k in keeps]

dur = float(subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
     "-of", "csv=p=0", str(workdir / "proxy.mp4")],
    capture_output=True, text=True).stdout.strip())

wav_path = workdir / "audio.wav"
if not wav_path.exists():
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(workdir / "proxy.mp4"), "-vn", "-ac", "1",
                    "-ar", "16000", str(wav_path)], check=True)

# ---- peaks: max |sample| per 10ms bin, scaled 0..99 ----
w = wavemod.open(str(wav_path))
sr, n = w.getframerate(), w.getnframes()
data = array.array("h")
data.frombytes(w.readframes(n))
w.close()
step = sr // 100                      # 10ms bins
try:
    import numpy as np
    a = np.abs(np.frombuffer(data.tobytes(), dtype=np.int16).astype(np.int32))
    pad = (-len(a)) % step
    if pad: a = np.concatenate([a, np.zeros(pad, dtype=np.int32)])
    peaks = (a.reshape(-1, step).max(axis=1) / 32768 * 99).clip(0, 99).astype(int).tolist()
except ImportError:
    peaks = [min(99, int(max(abs(x) for x in data[i:i+step]) / 32768 * 99))
             for i in range(0, len(data), step)]

# ---- silence suggestions from peaks (adaptive threshold) ----
srt = sorted(peaks)
floor_, speech = srt[len(srt)*15//100], srt[len(srt)*90//100]
thresh = floor_ + max(2, (speech - floor_) * 0.35)
sils, run = [], None
for i, p in enumerate(peaks):
    if p < thresh:
        run = i if run is None else run
    else:
        if run is not None and i - run >= 35:          # >= 0.35s
            sils.append([round(run/100, 2), round(i/100, 2)])
        run = None
if run is not None and len(peaks) - run >= 35:
    sils.append([round(run/100, 2), round(len(peaks)/100, 2)])
unreliable = (speech - floor_) < 12
print(f"peaks: {len(peaks)} bins, {len(sils)} silence suggestions"
      + (" (noisy audio — suggestions may be unreliable)" if unreliable else ""))

page = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>cut-video timeline</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#111; color:#eee;
         font:15px/1.45 -apple-system, BlinkMacSystemFont, sans-serif; }
  #top { position:sticky; top:0; background:#111; z-index:10;
         border-bottom:1px solid #2a2a2a; padding:10px 16px 0; }
  video { display:block; max-height:28vh; max-width:100%; margin:0 auto;
          border-radius:8px; background:#000; }
  #bar { display:flex; gap:8px; align-items:center; padding:8px 0; flex-wrap:wrap; }
  button { background:#2b2b2b; color:#eee; border:1px solid #444; border-radius:6px;
           padding:5px 11px; font:inherit; font-size:13px; cursor:pointer; }
  button:hover { background:#383838; }
  #cutall { border-color:#b58a2e; color:#ffd166; }
  #stats { margin-left:auto; color:#9a9a9a; font-size:13px; }
  #silctl { display:flex; gap:6px; align-items:center; color:#9a9a9a; font-size:13px; }
  input[type=range] { width:90px; }
  #tlwrap { overflow-x:auto; overflow-y:hidden; background:#0a0a0a;
            border-top:1px solid #222; cursor:crosshair; }
  #tl { position:relative; height:200px; }
  #cv { position:sticky; left:0; display:block; margin-top:26px; pointer-events:none; }
  .tick { position:absolute; top:0; height:100%; border-left:1px solid #262626;
          color:#666; font-size:10px; padding:2px 0 0 3px; pointer-events:none; }
  .blk { position:absolute; top:26px; height:160px; background:rgba(91,157,217,.20);
         border:1px solid #5b9dd9; border-radius:3px; }
  .blk.sel { background:rgba(91,157,217,.36); border-color:#9cc7ef;
             box-shadow:0 0 0 1px #9cc7ef; }
  .blk.dropped { background:rgba(120,120,120,.10); border-color:#4a4a4a; }
  .blk .num { position:absolute; top:2px; left:4px; font-size:11px; color:#cfe4f7;
              pointer-events:none; }
  .blk.dropped .num { color:#777; text-decoration:line-through; }
  .h { position:absolute; top:0; width:11px; height:100%; cursor:ew-resize; }
  .h.l { left:-6px; } .h.r { right:-6px; }
  .h::after { content:''; position:absolute; left:4px; top:0; width:3px; height:100%;
              background:#5b9dd9; opacity:0; }
  .blk:hover .h::after, .h.on::after { opacity:1; background:#ffd166; }
  .band { position:absolute; top:26px; height:160px; cursor:pointer;
          background:repeating-linear-gradient(45deg, rgba(255,209,102,.28) 0 6px,
                     rgba(255,209,102,.10) 6px 12px);
          border-left:1px dashed #b58a2e; border-right:1px dashed #b58a2e; }
  .band:hover { background:rgba(255,120,120,.35); }
  #selbox { position:absolute; top:26px; height:160px; background:rgba(255,92,92,.28);
            border:1px solid #ff5c5c; display:none; pointer-events:none; z-index:6; }
  #ph { position:absolute; top:0; width:1px; height:100%; background:#ff5c5c;
        pointer-events:none; z-index:5; }
  #ph::before { content:''; position:absolute; top:0; left:-4px;
        border:4.5px solid transparent; border-top-color:#ff5c5c; }
  #list { padding:10px 16px 90px; max-width:860px; margin:0 auto; }
  .seg { display:flex; gap:10px; align-items:flex-start; padding:9px 12px; margin:6px 0;
         background:#1b1b1b; border:1px solid #2c2c2c; border-radius:8px; cursor:pointer; }
  .seg.playing { border-color:#4a90d9; }
  .seg.sel { border-color:#9cc7ef; }
  .seg.dropped { opacity:.35; }
  .seg.dropped .txt { text-decoration:line-through; }
  .play { flex:none; width:30px; height:30px; border-radius:50%; padding:0; }
  .meta { flex:none; width:122px; color:#8a8a8a; font-size:12px; padding-top:2px; }
  .meta b { color:#ccc; font-size:13px; }
  .txt { flex:1; }
  #foot { position:fixed; bottom:0; left:0; right:0; background:#181818; z-index:10;
          border-top:1px solid #2a2a2a; padding:10px 16px; display:flex; gap:10px;
          align-items:center; flex-wrap:wrap; }
  #copied { color:#7bc67b; font-size:13px; visibility:hidden; }
  .hint { margin-left:auto; color:#8a8a8a; font-size:12.5px; text-align:right; }
</style></head><body>
<div id="top">
  <video id="v" src="proxy.mp4" controls preload="metadata"></video>
  <div id="bar">
    <button id="preview">&#9654; Preview final cut</button>
    <button id="stop">&#9632;</button>
    <button id="split">Split</button>
    <button id="drop">Drop / restore</button>
    <button id="undo">&#8630; Undo</button>
    <span style="color:#555">|</span>
    <button id="zin">Zoom +</button>
    <button id="zout">Zoom &minus;</button>
    <button id="zfit">Fit</button>
    <span style="color:#555">|</span>
    <span id="silctl">silences &ge; <input type="range" id="mindur" min="0.3" max="2"
      step="0.1" value="0.6"><span id="mindurv">0.6s</span>
      <button id="cutall">&#9986; Cut all</button></span>
    <span id="stats"></span>
  </div>
  <div id="tlwrap"><div id="tl">
    <canvas id="cv"></canvas>
    <div id="selbox"></div>
    <div id="ph"></div>
  </div></div>
</div>
<div id="list"></div>
<div id="foot">
  <button id="copy">Copy decisions for Claude</button>
  <span id="copied">copied — paste it in the chat</span>
  <span class="hint"><b style="color:#ff9c9c">drag across the waveform to delete that
    range</b> &middot; amber bands = suggested silences, click one to cut it<br>
    pinch or ctrl+wheel = zoom at cursor &middot; click = scrub &middot; drag block
    edges = trim &middot; double-click gap = restore &middot;
    keys: space &middot; S split &middot; D drop &middot; Z undo &middot;
    &larr;/&rarr; nudge edge (shift = 0.25s)</span>
</div>
<script>
const D = __DUR__, PEAKS = __PEAKS__, SILS = __SILS__, BPS = 100;
let blocks = __SEGS__;
blocks.forEach(x => x.drop = false);
const v = document.getElementById('v'), tl = document.getElementById('tl'),
      wrap = document.getElementById('tlwrap'), ph = document.getElementById('ph'),
      cv = document.getElementById('cv'), selbox = document.getElementById('selbox'),
      list = document.getElementById('list');
let PPS = 10, FIT = 10, sel = -1, selEdge = null, mode = null, cur = -1, undoStack = [];

const fmt = s => Math.floor(s/60) + ':' + (s%60).toFixed(1).padStart(4,'0');
const order = () => blocks.map((x,i)=>i).sort((p,q)=>blocks[p].a-blocks[q].a);
const kept  = () => order().filter(i=>!blocks[i].drop);
const pushUndo = () => { undoStack.push(JSON.stringify(blocks));
                         if (undoStack.length > 60) undoStack.shift(); };
const minDur = () => +document.getElementById('mindur').value;

// ---------- canvas waveform ----------
function draw(){
  const wpx = wrap.clientWidth, hpx = 160, dpr = devicePixelRatio || 1;
  if (cv.width !== wpx*dpr){ cv.width = wpx*dpr; cv.height = hpx*dpr;
    cv.style.width = wpx+'px'; cv.style.height = hpx+'px'; }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,wpx,hpx);
  ctx.fillStyle = '#4d86ba';
  const t0 = wrap.scrollLeft / PPS;
  for (let px=0; px<wpx; px++){
    const bA = Math.floor((t0 + px/PPS) * BPS),
          bB = Math.max(bA+1, Math.floor((t0 + (px+1)/PPS) * BPS));
    if (bA >= PEAKS.length) break;
    let m = 0;
    for (let b=bA; b<bB && b<PEAKS.length; b++) if (PEAKS[b]>m) m = PEAKS[b];
    const amp = Math.max(0.6, m/99 * hpx/2);
    ctx.fillRect(px, hpx/2-amp, 1, amp*2);
  }
}
wrap.addEventListener('scroll', draw);

// ---------- layout ----------
function layout(){
  tl.style.width = (D*PPS) + 'px';
  tl.querySelectorAll('.tick,.blk,.band').forEach(e=>e.remove());
  const stepT = PPS > 60 ? 2 : PPS > 25 ? 5 : PPS > 8 ? 10 : 30;
  for (let t=0; t<D; t+=stepT){
    const e = document.createElement('div'); e.className='tick';
    e.style.left = (t*PPS)+'px'; e.textContent = fmt(t); tl.appendChild(e);
  }
  const ord = order();
  ord.forEach((bi, k) => {
    const x = blocks[bi], e = document.createElement('div');
    e.className = 'blk' + (x.drop?' dropped':'') + (bi===sel?' sel':'');
    e.style.left = (x.a*PPS)+'px'; e.style.width = ((x.b-x.a)*PPS)+'px';
    e.dataset.bi = bi;
    e.innerHTML = '<span class="num">'+(k+1)+'</span>' +
                  '<div class="h l"></div><div class="h r"></div>';
    e.ondblclick = ev => { ev.stopPropagation(); playOne(bi); };
    e.querySelectorAll('.h').forEach(h => h.onpointerdown = ev => dragEdge(ev, bi,
        h.classList.contains('l') ? 'a' : 'b'));
    tl.appendChild(e);
  });
  bands().forEach(([a,b]) => {
    const e = document.createElement('div'); e.className = 'band';
    e.style.left = (a*PPS)+'px'; e.style.width = ((b-a)*PPS)+'px';
    e.title = 'silence '+(b-a).toFixed(1)+'s — click to cut';
    e.onpointerdown = ev => ev.stopPropagation();
    e.onclick = ev => { ev.stopPropagation(); pushUndo(); cutRange(a, b, 0.1); layout(); };
    tl.appendChild(e);
  });
  movePH(); draw(); cards();
}

function bands(){                        // suggested-silence ∩ keep-blocks
  const out = [], md = minDur();
  SILS.forEach(([sa,sb]) => {
    if (sb-sa < md) return;
    blocks.forEach(x => { if (x.drop) return;
      const a = Math.max(sa, x.a+0.04), b = Math.min(sb, x.b-0.04);
      if (b-a >= Math.max(0.3, md*0.6)) out.push([a,b]);
    });
  });
  return out;
}

function cards(){
  list.innerHTML = '';
  order().forEach((bi, k) => {
    const s = blocks[bi], el = document.createElement('div');
    el.className = 'seg' + (s.drop?' dropped':'') + (bi===sel?' sel':'');
    el.id = 'card'+bi;
    el.innerHTML = '<button class="play">&#9654;</button>' +
      '<div class="meta"><b>#'+(k+1)+'</b><br>'+fmt(s.a)+' &rarr; '+fmt(s.b)+
      '<br>'+(s.b-s.a).toFixed(1)+'s</div>' +
      '<div class="txt">'+(s.t||'<i>(restored footage — no transcript)</i>')+'</div>';
    el.onclick = () => { pushUndo(); s.drop = !s.drop; layout(); };
    el.querySelector('.play').onclick = e => { e.stopPropagation(); playOne(bi); };
    list.appendChild(el);
  });
  stats();
}

function stats(){
  const k = kept(), t = k.reduce((x,i)=>x+blocks[i].b-blocks[i].a, 0);
  document.getElementById('stats').textContent =
    k.length+'/'+blocks.length+' blocks · final '+fmt(t);
}

function select(bi){ sel = bi; selEdge = null; layout();
  const c = document.getElementById('card'+bi);
  if (c) c.scrollIntoView({block:'nearest', behavior:'smooth'}); }

// ---------- playback ----------
function movePH(){ ph.style.left = (v.currentTime*PPS)+'px'; }
v.addEventListener('timeupdate', () => {
  movePH();
  const x = v.currentTime*PPS - wrap.scrollLeft;
  if (!v.paused && (x < 40 || x > wrap.clientWidth-40))
    wrap.scrollLeft = v.currentTime*PPS - wrap.clientWidth/3;
  if (mode===null || cur<0) return;
  const s = blocks[cur];
  if (v.currentTime >= s.b - 0.02){
    if (mode==='one'){ v.pause(); mode=null; markCard(-1); return; }
    const k = kept(), pos = k.indexOf(cur);
    if (pos<0 || pos===k.length-1){ v.pause(); mode=null; markCard(-1); return; }
    cur = k[pos+1]; v.currentTime = blocks[cur].a; markCard(cur);
  }
});
function markCard(bi){ document.querySelectorAll('.seg.playing').forEach(e=>e.classList.remove('playing'));
  if (bi>=0){ const c=document.getElementById('card'+bi); if(c){ c.classList.add('playing');
    c.scrollIntoView({block:'nearest', behavior:'smooth'}); } } }
function playOne(bi){ mode='one'; cur=bi; v.currentTime=blocks[bi].a; v.play(); markCard(bi); }

// ---------- range delete (the silence-removal gesture) ----------
function cutRange(a, b, pad){            // pad keeps a little pause at each side
  if (pad && b-a > 2.5*pad){ a += pad; b -= pad; }
  blocks = blocks.flatMap(x => {
    if (b <= x.a || a >= x.b) return [x];             // no overlap
    const parts = [];
    if (a > x.a + 0.08) parts.push({...x, b: +a.toFixed(2)});
    if (b < x.b - 0.08) parts.push({...x, a: +b.toFixed(2)});
    return parts;                                      // fully covered -> removed
  });
  sel = -1; selEdge = null;
}

// ---------- unified pointer handling: click=scrub/select, drag=range-delete ----
let down = null;
tl.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  const rect = tl.getBoundingClientRect();
  down = { x: e.clientX, t: (e.clientX-rect.left)/PPS, moved: false,
           blk: e.target.closest('.blk') };
  tl.setPointerCapture(e.pointerId);
});
tl.addEventListener('pointermove', e => {
  if (!down) return;
  if (Math.abs(e.clientX-down.x) > 5) down.moved = true;
  if (down.moved){
    const rect = tl.getBoundingClientRect(), t2 = (e.clientX-rect.left)/PPS;
    const a = Math.min(down.t, t2), b = Math.max(down.t, t2);
    selbox.style.display = 'block';
    selbox.style.left = (a*PPS)+'px'; selbox.style.width = ((b-a)*PPS)+'px';
  }
});
tl.addEventListener('pointerup', e => {
  if (!down) return;
  const rect = tl.getBoundingClientRect(), t2 = (e.clientX-rect.left)/PPS;
  if (down.moved){
    selbox.style.display = 'none';
    const a = Math.min(down.t, t2), b = Math.max(down.t, t2);
    if (b-a >= 0.06){ pushUndo(); cutRange(a, b, 0); layout(); }
  } else if (down.blk){ select(+down.blk.dataset.bi); }
  else { v.currentTime = Math.max(0, Math.min(D, t2)); movePH(); }
  down = null;
});
tl.addEventListener('dblclick', e => {
  const t = (e.clientX - tl.getBoundingClientRect().left) / PPS;
  if (blocks.some(x => t>=x.a && t<=x.b)) return;
  let lo = 0, hi = D;
  blocks.forEach(x => { if (x.b<=t) lo=Math.max(lo,x.b); if (x.a>=t) hi=Math.min(hi,x.a); });
  const a = Math.max(lo, t-0.75), b = Math.min(hi, t+0.75);
  if (b-a < 0.15) return;
  pushUndo();
  blocks.push({a:+a.toFixed(2), b:+b.toFixed(2), t:'', drop:false});
  sel = blocks.length-1; layout();
});

// ---------- edge dragging ----------
function dragEdge(ev, bi, side){
  ev.stopPropagation(); ev.preventDefault();
  pushUndo();
  const h = ev.target; h.classList.add('on'); h.setPointerCapture(ev.pointerId);
  sel = bi; selEdge = side; const x0 = ev.clientX, t0 = blocks[bi][side];
  const move = e => {
    let t = t0 + (e.clientX-x0)/PPS;
    const s = blocks[bi];
    let lo = 0, hi = D;
    blocks.forEach((o,j)=>{ if(j!==bi && !o.drop){ if(o.b<=s.a+0.001) lo=Math.max(lo,o.b);
                                                   if(o.a>=s.b-0.001) hi=Math.min(hi,o.a); }});
    if (side==='a') t = Math.min(Math.max(t, lo), s.b-0.1);
    else            t = Math.max(Math.min(t, hi), s.a+0.1);
    s[side] = +t.toFixed(2);
    const e2 = tl.querySelector('.blk[data-bi="'+bi+'"]');
    e2.style.left = (s.a*PPS)+'px'; e2.style.width = ((s.b-s.a)*PPS)+'px';
    v.currentTime = t; movePH();
  };
  const up = e => { h.releasePointerCapture(e.pointerId); h.classList.remove('on');
    h.removeEventListener('pointermove', move); h.removeEventListener('pointerup', up);
    layout(); };
  h.addEventListener('pointermove', move); h.addEventListener('pointerup', up);
}

function nudge(dt){
  if (sel<0 || !selEdge) return;
  const s = blocks[sel]; let t = s[selEdge]+dt;
  if (selEdge==='a') t = Math.min(t, s.b-0.1); else t = Math.max(t, s.a+0.1);
  s[selEdge] = +Math.max(0, Math.min(D, t)).toFixed(2);
  v.currentTime = s[selEdge]; layout();
}

function splitAt(){
  const t = v.currentTime;
  const bi = blocks.findIndex(x => !x.drop && t > x.a+0.1 && t < x.b-0.1);
  if (bi<0) return;
  pushUndo();
  const s = blocks[bi];
  blocks.push({a:+t.toFixed(2), b:s.b, t:s.t, drop:false});
  s.b = +t.toFixed(2);
  layout();
}

// ---------- toolbar ----------
document.getElementById('preview').onclick = () => { const k=kept(); if(!k.length) return;
  mode='all'; cur=k[0]; v.currentTime=blocks[cur].a; v.play(); markCard(cur); };
document.getElementById('stop').onclick = () => { mode=null; v.pause(); markCard(-1); };
document.getElementById('split').onclick = splitAt;
document.getElementById('drop').onclick = () => { if (sel>=0){ pushUndo();
  blocks[sel].drop=!blocks[sel].drop; layout(); } };
document.getElementById('undo').onclick = doUndo;
function doUndo(){ if (undoStack.length){ blocks = JSON.parse(undoStack.pop());
  sel=-1; selEdge=null; layout(); } }
document.getElementById('cutall').onclick = () => {
  const bs = bands(); if (!bs.length) return;
  pushUndo();
  bs.sort((p,q)=>q[0]-p[0]).forEach(([a,b]) => cutRange(a, b, 0.1));
  layout();
};
document.getElementById('mindur').oninput = e => {
  document.getElementById('mindurv').textContent = (+e.target.value).toFixed(1)+'s';
  layout();
};

// ---------- zoom ----------
function setPPS(p, anchorX){
  const t = ((anchorX ?? wrap.clientWidth/2) + wrap.scrollLeft) / PPS;
  PPS = Math.max(FIT, Math.min(400, p));
  layout();
  wrap.scrollLeft = t*PPS - (anchorX ?? wrap.clientWidth/2);
  draw();
}
document.getElementById('zin').onclick  = () => setPPS(PPS*1.6);
document.getElementById('zout').onclick = () => setPPS(PPS/1.6);
document.getElementById('zfit').onclick = () => setPPS(FIT);
wrap.addEventListener('wheel', e => {
  if (e.ctrlKey || e.metaKey){                       // pinch / ctrl+wheel = zoom
    e.preventDefault();
    setPPS(PPS * Math.exp(-e.deltaY*0.012), e.clientX - wrap.getBoundingClientRect().left);
  } else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)){ // mouse wheel = pan
    e.preventDefault(); wrap.scrollLeft += e.deltaY;
  }                                                   // trackpad deltaX pans natively
}, {passive:false});

// ---------- keys ----------
document.addEventListener('keydown', e => {
  if (e.target.tagName==='INPUT') return;
  if (e.code==='Space'){ e.preventDefault(); v.paused ? v.play() : v.pause(); }
  else if (e.key==='s'||e.key==='S') splitAt();
  else if (e.key==='z'||e.key==='Z'){ e.preventDefault(); doUndo(); }
  else if (e.key==='d'||e.key==='D'||e.key==='Backspace'){
    if (sel>=0){ pushUndo(); blocks[sel].drop=!blocks[sel].drop; layout(); } }
  else if (e.key==='ArrowLeft')  { e.preventDefault(); nudge(e.shiftKey?-0.25:-0.05); }
  else if (e.key==='ArrowRight') { e.preventDefault(); nudge(e.shiftKey? 0.25: 0.05); }
});

// ---------- export ----------
document.getElementById('copy').onclick = async () => {
  const out = JSON.stringify({keeps: order().filter(i=>!blocks[i].drop)
    .map(i=>[blocks[i].a, blocks[i].b])});
  try { await navigator.clipboard.writeText(out); }
  catch(e){ const ta=document.createElement('textarea'); ta.value=out;
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  const c=document.getElementById('copied'); c.style.visibility='visible';
  setTimeout(()=>c.style.visibility='hidden', 2500);
};

FIT = (wrap.clientWidth-2)/D; PPS = FIT;
layout();
window.addEventListener('resize', () => { FIT = (wrap.clientWidth-2)/D; layout(); });
</script></body></html>
"""

esc = [{**s, "t": html.escape(s["t"])} for s in segs]
out = (page.replace("__DUR__", f"{dur:.3f}")
           .replace("__PEAKS__", json.dumps(peaks, separators=(",", ":")))
           .replace("__SILS__", json.dumps(sils, separators=(",", ":")))
           .replace("__SEGS__", json.dumps(esc)))
(workdir / "review.html").write_text(out)
print(f"wrote {workdir/'review.html'} ({len(segs)} blocks, {dur:.1f}s timeline)")
