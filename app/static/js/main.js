/* ── Photo fade-in on load ──────────────────────────────────────────────── */
function bindPhotoFade(img) {
  if (img.complete && img.naturalWidth > 0) {
    img.classList.add('img-loaded');
  } else {
    img.addEventListener('load', () => img.classList.add('img-loaded'), { once: true });
    img.addEventListener('error', () => img.classList.add('img-loaded'), { once: true });
  }
}

/* ── Masonry: compute grid-row span from aspect ratio ───────────────────── */
const MASONRY_ROW_H = 4;
const MASONRY_GAP = 3;

function applyMasonrySpan(card) {
  const ratio = parseFloat(card.dataset.ratio);
  if (!ratio || ratio <= 0) return;
  const w = card.offsetWidth;
  if (!w) return;
  const h = w / ratio;
  const span = Math.max(1, Math.ceil((h + MASONRY_GAP) / (MASONRY_ROW_H + MASONRY_GAP)));
  card.style.gridRow = 'span ' + span;
  card.classList.add('spanned');
}

function applyMasonryAll(root) {
  const scope = root || document;
  scope.querySelectorAll('.photo-card').forEach(applyMasonrySpan);
}

let _masonryRaf = null;
function scheduleMasonryRelayout() {
  if (_masonryRaf) return;
  _masonryRaf = requestAnimationFrame(() => {
    _masonryRaf = null;
    applyMasonryAll();
  });
}

window.applyMasonrySpan = applyMasonrySpan;
window.applyMasonryAll = applyMasonryAll;

applyMasonryAll();
window.addEventListener('resize', scheduleMasonryRelayout, { passive: true });

// Stagger initial page images so they wave in rather than pop individually
document.querySelectorAll('.photo-card img').forEach((img, i) => {
  img.style.transitionDelay = Math.min(i * 35, 500) + 'ms';
  bindPhotoFade(img);
  // Clear delay after first load so hover transitions stay instant
  img.addEventListener('load', () => { img.style.transitionDelay = '0ms'; }, { once: true });
  img.addEventListener('error', () => { img.style.transitionDelay = '0ms'; }, { once: true });
});

/* ── Upload drop zone ───────────────────────────────────────────────────── */
(function () {
  const zone = document.getElementById('dropZone');
  const input = document.getElementById('fileInput');
  const btn = document.getElementById('uploadBtn');
  if (!zone || !input) return;

  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
    input.files = e.dataTransfer.files;
    updateZoneLabel();
  });

  input.addEventListener('change', updateZoneLabel);

  function updateZoneLabel() {
    const count = input.files.length;
    if (count > 0) {
      const p = zone.querySelector('p');
      if (p) p.textContent = `${count} file${count > 1 ? 's' : ''} selected`;
      if (btn) btn.style.display = 'inline-flex';
    }
  }
})();
