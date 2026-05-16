/* ── Photo fade-in on load ──────────────────────────────────────────────── */
function bindPhotoFade(img) {
  if (img.complete && img.naturalWidth > 0) {
    img.classList.add('img-loaded');
  } else {
    img.addEventListener('load', () => img.classList.add('img-loaded'), { once: true });
    img.addEventListener('error', () => img.classList.add('img-loaded'), { once: true });
  }
}

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
