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

/* ── Folder picker (used for RAW folder selection) ───────────────────────── */
window.openFolderPicker = function (targetInputId, opts) {
  opts = opts || {};
  const initialPath = opts.initialPath || '';
  const title = opts.title || 'Pick a folder';

  let dialog = document.getElementById('__folderPickerDialog');
  if (!dialog) {
    dialog = document.createElement('dialog');
    dialog.id = '__folderPickerDialog';
    dialog.className = 'native-dialog folder-picker-dialog';
    dialog.innerHTML = `
      <div class="folder-picker-head">
        <h2 class="modal-title" id="__fpTitle"></h2>
        <button type="button" class="btn-ghost" id="__fpClose">&#10005;</button>
      </div>
      <nav class="folder-picker-crumbs" id="__fpCrumbs"></nav>
      <div class="folder-picker-current">
        <span class="muted-cell" style="font-size:11px">Selected:</span>
        <code id="__fpSelected">(root)</code>
      </div>
      <div class="folder-picker-list" id="__fpList"></div>
      <div class="modal-actions">
        <button type="button" class="btn-primary" id="__fpUse">Use this folder</button>
        <button type="button" class="btn-secondary" id="__fpCancel">Cancel</button>
      </div>
    `;
    document.body.appendChild(dialog);
  }

  dialog.querySelector('#__fpTitle').textContent = title;
  let currentPath = initialPath;

  async function load(path) {
    currentPath = path || '';
    const url = '/admin/api/browse-folders' + (currentPath ? '?path=' + encodeURIComponent(currentPath) : '');
    const data = await (await fetch(url)).json();
    const crumbs = dialog.querySelector('#__fpCrumbs');
    const list = dialog.querySelector('#__fpList');
    const sel = dialog.querySelector('#__fpSelected');

    sel.textContent = currentPath || '(photos root)';

    crumbs.innerHTML = '';
    const root = document.createElement('a');
    root.href = '#'; root.className = 'crumb'; root.textContent = '🏠 root';
    root.onclick = (e) => { e.preventDefault(); load(''); };
    crumbs.appendChild(root);
    (data.breadcrumb || []).forEach((c) => {
      const sep = document.createElement('span');
      sep.className = 'crumb-sep'; sep.textContent = '/';
      crumbs.appendChild(sep);
      const a = document.createElement('a');
      a.href = '#'; a.className = 'crumb'; a.textContent = c.name;
      a.onclick = (e) => { e.preventDefault(); load(c.rel_path); };
      crumbs.appendChild(a);
    });

    list.innerHTML = '';
    if (!data.folders.length) {
      list.innerHTML = '<p class="muted-cell" style="font-size:12px;padding:0.75rem">No subfolders here.</p>';
      return;
    }
    data.folders.forEach((f) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'folder-picker-item';
      item.innerHTML = '<span>📁 ' + f.name + '</span>' + (f.has_sub ? '<span class="muted-cell">›</span>' : '');
      item.onclick = () => load(f.rel_path);
      list.appendChild(item);
    });
  }

  dialog.querySelector('#__fpUse').onclick = () => {
    const input = document.getElementById(targetInputId);
    if (input) input.value = currentPath;
    dialog.close();
  };
  dialog.querySelector('#__fpCancel').onclick = () => dialog.close();
  dialog.querySelector('#__fpClose').onclick = () => dialog.close();

  load(initialPath);
  dialog.showModal();
};
