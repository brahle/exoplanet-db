// Exoplanet DB Portal Client JavaScript

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initTabs();
  initCatalog();
  initObservationQueue();
  initSnapshots();
  initModalBackdrop();

  // Initial routing
  handleRoute();
});

// --- Theme Management ---
function initTheme() {
  const toggleBtn = document.getElementById('theme-toggle');
  const savedTheme = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', savedTheme);

  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('theme', next);
    });
  }
}

// --- Navigation Tabs & URL Hash Routing ---
function initTabs() {
  const tabBtns = document.querySelectorAll('.tab-btn');

  tabBtns.forEach(btn => {
    btn.addEventListener('click', (e) => {
      // Allow browser to natively handle Ctrl+click, Cmd+click, Shift+click, or middle click (button === 1)
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) {
        return;
      }
      e.preventDefault();
      const href = btn.getAttribute('href') || '#catalog';
      if (window.location.hash !== href) {
        history.pushState(null, '', href);
      }
      handleRoute();
    });
  });

  window.addEventListener('popstate', handleRoute);
  window.addEventListener('hashchange', handleRoute);
}

function switchTab(tabName, updateUrl = false) {
  const tabBtns = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');

  tabBtns.forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-tab') === tabName);
  });
  tabContents.forEach(c => {
    c.classList.toggle('active', c.id === `${tabName}-content`);
  });

  if (updateUrl) {
    const tabToHash = {
      candidates: '#catalog',
      queue: '#queue',
      snapshots: '#snapshots',
      agent: '#agent'
    };
    if (tabToHash[tabName] && window.location.hash !== tabToHash[tabName]) {
      history.pushState(null, '', tabToHash[tabName]);
    }
  }
}

function handleRoute() {
  const hash = window.location.hash || '#catalog';

  // 1. Direct candidate route: #candidate/<candidateId>
  if (hash.startsWith('#candidate/')) {
    const candidateId = hash.replace('#candidate/', '').trim();
    switchTab('candidates', false);
    if (candidateId) {
      openDossierModal(candidateId);
    }
    return;
  }

  // 2. Close modal if navigating away from candidate
  const modal = document.getElementById('dossier-modal');
  if (modal && modal.open) {
    modal.close();
  }

  // 3. Main tabs: #queue, #snapshots, #agent, #catalog
  if (hash.startsWith('#queue')) {
    switchTab('queue', false);
  } else if (hash.startsWith('#snapshots')) {
    switchTab('snapshots', false);
  } else if (hash.startsWith('#agent')) {
    switchTab('agent', false);
  } else {
    // Default catalog tab
    switchTab('candidates', false);

    // Parse query params if present in hash: #catalog?search=...&source=...
    const qIndex = hash.indexOf('?');
    if (qIndex !== -1) {
      const params = new URLSearchParams(hash.slice(qIndex + 1));
      let shouldReload = false;
      if (params.has('search')) {
        const sInput = document.getElementById('catalog-search');
        if (sInput && sInput.value !== params.get('search')) {
          sInput.value = params.get('search');
          shouldReload = true;
        }
      }
      if (params.has('source')) {
        const srcSelect = document.getElementById('source-filter');
        if (srcSelect && srcSelect.value !== params.get('source')) {
          srcSelect.value = params.get('source');
          shouldReload = true;
        }
      }
      if (params.has('disposition')) {
        const dSelect = document.getElementById('disposition-filter');
        if (dSelect && dSelect.value !== params.get('disposition')) {
          dSelect.value = params.get('disposition');
          shouldReload = true;
        }
      }
      if (shouldReload) {
        loadCandidates();
      }
    }
  }
}

// --- Candidates Catalog ---
let currentQueryId = 0;

async function loadCatalogStats() {
  try {
    const res = await fetch('/v1/stats');
    if (!res.ok) return;
    const stats = await res.json();

    const totalEl = document.getElementById('stat-total-signals');
    const confirmedEl = document.getElementById('stat-confirmed-planets');
    const candidatesEl = document.getElementById('stat-candidates-total');
    const targetsEl = document.getElementById('stat-total-targets');

    if (totalEl) totalEl.textContent = Number(stats.total_signals).toLocaleString();
    if (confirmedEl) confirmedEl.textContent = Number(stats.confirmed_planets).toLocaleString();
    if (candidatesEl) candidatesEl.textContent = Number(stats.candidates_total).toLocaleString();
    if (targetsEl) targetsEl.textContent = Number(stats.total_targets).toLocaleString();
  } catch (err) {
    console.warn('Failed to load stats:', err);
  }
}

async function initCatalog() {
  const searchInput = document.getElementById('catalog-search');
  const sourceFilter = document.getElementById('source-filter');
  const dispositionFilter = document.getElementById('disposition-filter');

  // Load stats and catalog candidates
  loadCatalogStats();
  await loadCandidates();

  // Attach quick-filter handlers to stat cards
  document.querySelectorAll('.stats-grid .stat-card[data-filter]').forEach(card => {
    card.addEventListener('click', () => {
      const filter = card.getAttribute('data-filter');
      const dSelect = document.getElementById('disposition-filter');
      const sSelect = document.getElementById('source-filter');
      const searchBox = document.getElementById('catalog-search');

      if (filter === 'all') {
        if (dSelect) dSelect.value = 'all';
        if (sSelect) sSelect.value = 'all';
        if (searchBox) searchBox.value = '';
      } else if (filter === 'confirmed_planet') {
        if (dSelect) dSelect.value = 'confirmed_planet';
      } else if (filter === 'candidates') {
        if (dSelect) dSelect.value = 'unclassified_transit_like_signal';
      }
      syncCatalogUrl();
      loadCandidates();
    });
  });

  if (searchInput) {
    let debounceTimer;
    searchInput.addEventListener('input', () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        syncCatalogUrl();
        loadCandidates();
      }, 300);
    });
  }

  if (sourceFilter) {
    sourceFilter.addEventListener('change', () => {
      syncCatalogUrl();
      loadCandidates();
    });
  }

  if (dispositionFilter) {
    dispositionFilter.addEventListener('change', () => {
      syncCatalogUrl();
      loadCandidates();
    });
  }
}

function syncCatalogUrl() {
  const searchVal = (document.getElementById('catalog-search')?.value || '').trim();
  const sourceVal = document.getElementById('source-filter')?.value || 'all';
  const dispVal = document.getElementById('disposition-filter')?.value || 'all';

  const params = new URLSearchParams();
  if (searchVal) params.set('search', searchVal);
  if (sourceVal !== 'all') params.set('source', sourceVal);
  if (dispVal !== 'all') params.set('disposition', dispVal);

  const queryStr = params.toString();
  const newHash = queryStr ? `#catalog?${queryStr}` : '#catalog';
  if (window.location.hash !== newHash) {
    history.replaceState(null, '', newHash);
  }
}

async function loadCandidates() {
  const grid = document.getElementById('candidate-grid');
  const countEl = document.getElementById('catalog-results-count');
  if (!grid) return;

  const searchVal = (document.getElementById('catalog-search')?.value || '').trim();
  const sourceVal = document.getElementById('source-filter')?.value || 'all';
  const dispVal = document.getElementById('disposition-filter')?.value || 'all';

  grid.innerHTML = '<div style="color: var(--text-muted); grid-column: 1/-1; text-align: center; padding: 2.5rem;">Loading planetary signals and candidates...</div>';

  const thisQuery = ++currentQueryId;
  try {
    const params = new URLSearchParams({ limit: '100' });
    if (searchVal) params.set('search', searchVal);
    if (sourceVal !== 'all') params.set('source', sourceVal);
    if (dispVal !== 'all') params.set('disposition', dispVal);

    const res = await fetch(`/v1/candidates?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const data = await res.json();

    if (thisQuery !== currentQueryId) return; // Stale response discarded

    const candidates = data.items || [];
    const totalMatches = data.total ?? candidates.length;

    if (countEl) {
      if (searchVal || sourceVal !== 'all' || dispVal !== 'all') {
        countEl.textContent = `Showing ${candidates.length} of ${totalMatches.toLocaleString()} candidates matching filters`;
      } else {
        countEl.textContent = `Showing ${candidates.length} of ${totalMatches.toLocaleString()} total planets & candidates`;
      }
    }

    if (candidates.length === 0) {
      if (countEl) countEl.textContent = '0 candidates match filters';
      grid.innerHTML = '<div style="color: var(--text-muted); grid-column: 1/-1; text-align: center; padding: 3rem;">No candidates matched your search criteria.</div>';
      return;
    }

    grid.innerHTML = candidates.map(c => renderCandidateCard(c)).join('');

    // Attach card click handlers: preserve modifier keys (Ctrl+click, Cmd+click, Shift+click, middle-click)
    document.querySelectorAll('.candidate-card').forEach(card => {
      card.addEventListener('click', (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) {
          // Native browser behavior opens link in a new tab!
          return;
        }
        e.preventDefault();
        const candidateId = card.getAttribute('data-id');
        history.pushState(null, '', `#candidate/${candidateId}`);
        openDossierModal(candidateId);
      });
    });
  } catch (err) {
    if (thisQuery === currentQueryId) {
      if (countEl) countEl.textContent = 'Failed to load records';
      grid.innerHTML = `<div style="color: var(--accent-rose); grid-column: 1/-1; text-align: center; padding: 2rem;">Failed to load catalog: ${escapeHtml(err.message)}</div>`;
    }
  }
}

function renderCandidateCard(c) {
  const dispClass = {
    'confirmed_planet': 'badge-confirmed',
    'validated_planet': 'badge-confirmed',
    'planet_candidate': 'badge-candidate',
    'unclassified_transit_like_signal': 'badge-unclassified',
    'false_positive': 'badge-false-positive'
  }[c.current_disposition] || 'badge-candidate';

  const dispLabel = c.current_disposition.replace(/_/g, ' ');
  const hostLabel = c.target_name || (c.tic_id ? `TIC ${c.tic_id}` : c.target_id);
  const src = c.source_name || 'nasa_pscomppars';
  const srcClass = src.startsWith('git') ? 'source-git' : (src.startsWith('nasa') ? 'source-nasa' : '');

  return `
    <a href="#candidate/${encodeURIComponent(c.id)}" class="candidate-card" data-id="${escapeHtml(c.id)}">
      <div class="card-header">
        <div>
          <h3 class="candidate-name">${escapeHtml(c.display_name)}</h3>
          <span class="host-name">Host: <strong>${escapeHtml(hostLabel)}</strong></span>
        </div>
        <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 0.35rem;">
          <span class="badge ${dispClass}">${dispLabel}</span>
          <span class="source-badge ${srcClass}" title="Source: ${escapeHtml(src)}">${escapeHtml(src)}</span>
        </div>
      </div>

      <div class="metric-row">
        <div class="metric-item">
          <span class="metric-label">Period</span>
          <span class="metric-val">${c.period_d ? c.period_d.toFixed(4) + ' d' : 'N/A'}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Depth</span>
          <span class="metric-val">${c.depth_ppm ? Math.round(c.depth_ppm) + ' ppm' : 'N/A'}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Duration</span>
          <span class="metric-val">${c.duration_h ? c.duration_h.toFixed(2) + ' h' : 'N/A'}</span>
        </div>
      </div>

      <div class="status-tags">
        <span class="tag-pill">Loc: ${escapeHtml(c.source_localisation_status)}</span>
        <span class="tag-pill">Timing: ${escapeHtml(c.timing_consistency_status)}</span>
        ${c.scientific_limits && c.scientific_limits.length ? `<span class="tag-pill">${c.scientific_limits.length} Limits</span>` : ''}
      </div>
    </a>
  `;
}

// --- Candidate Dossier Modal ---
async function openDossierModal(candidateId) {
  const modal = document.getElementById('dossier-modal');
  const content = document.getElementById('dossier-modal-content');
  if (!modal || !content) return;

  content.innerHTML = '<div style="text-align: center; padding: 3rem; color: var(--text-secondary);">Loading candidate dossier and lightcurve...</div>';
  if (!modal.open) {
    modal.showModal();
  }

  try {
    const res = await fetch(`/v1/candidates/${encodeURIComponent(candidateId)}`);
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const candidate = await res.json();

    const depth = candidate.depth_ppm || 5000;
    const duration = candidate.duration_h || 2.5;
    const period = candidate.period_d || 3.0;
    const directUrl = `${window.location.origin}${window.location.pathname}#candidate/${candidate.id}`;

    content.innerHTML = `
      <div class="modal-header">
        <div>
          <h2 style="font-size: 1.5rem; font-weight: 700;">${escapeHtml(candidate.display_name)}</h2>
          <span style="color: var(--text-secondary); font-family: var(--font-mono); font-size: 0.8125rem;">ID: ${escapeHtml(candidate.id)}</span>
          ${candidate.source_name ? `<span class="source-badge" style="margin-left: 0.5rem;">${escapeHtml(candidate.source_name)}</span>` : ''}
        </div>
        <div style="display: flex; align-items: center; gap: 0.5rem;">
          <button id="copy-candidate-link" class="tag-pill" style="cursor: pointer; background: var(--bg-card); border: 1px solid var(--border-color); color: var(--text-primary);" title="Copy direct link to clipboard">📋 Copy Link</button>
          <a href="#candidate/${encodeURIComponent(candidate.id)}" target="_blank" rel="noopener" class="tag-pill" style="text-decoration: none; background: var(--bg-card); border: 1px solid var(--border-color); color: var(--accent-cyan);" title="Open direct URL in new tab">↗ New Tab</a>
          <button class="modal-close-btn" id="modal-close" aria-label="Close dialog">&times;</button>
        </div>
      </div>

      <div style="margin-bottom: 1rem;">
        <span class="badge badge-candidate">${escapeHtml(candidate.current_disposition.replace(/_/g, ' '))}</span>
        <span class="tag-pill" style="margin-left: 0.5rem;">Localisation: ${escapeHtml(candidate.source_localisation_status)}</span>
        <span class="tag-pill">Timing: ${escapeHtml(candidate.timing_consistency_status)}</span>
      </div>

      <div class="metric-row" style="margin-bottom: 1.5rem;">
        <div class="metric-item">
          <span class="metric-label">Period</span>
          <span class="metric-val">${candidate.period_d ? candidate.period_d.toFixed(6) + ' d' : 'N/A'}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Epoch (BJD)</span>
          <span class="metric-val">${candidate.epoch_bjd ? candidate.epoch_bjd.toFixed(4) : 'N/A'}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Transit Depth</span>
          <span class="metric-val">${candidate.depth_ppm ? Math.round(candidate.depth_ppm) + ' ppm' : 'N/A'}</span>
        </div>
      </div>

      <div class="lightcurve-chart">
        <h4 style="font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 0.75rem; text-transform: uppercase; font-family: var(--font-mono);">Folded Transit Lightcurve</h4>
        ${generateFoldedLightcurveSvg(depth, duration, period)}
      </div>

      <div style="margin-top: 1.5rem;">
        <h4 style="font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 0.5rem; text-transform: uppercase; font-family: var(--font-mono);">Scientific Limits & Constraints</h4>
        ${
          candidate.scientific_limits && candidate.scientific_limits.length > 0
            ? `<ul style="padding-left: 1.25rem; font-size: 0.875rem; color: var(--text-primary); line-height: 1.6;">${candidate.scientific_limits.map(l => `<li>${escapeHtml(l)}</li>`).join('')}</ul>`
            : '<p style="font-size: 0.875rem; color: var(--text-muted);">No restrictive observational limits flagged.</p>'
        }
      </div>

      ${candidate.host_star ? `
        <div style="margin-top: 1.5rem; padding: 1rem; background: var(--bg-card); border-radius: var(--radius-sm);">
          <h4 style="font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 0.5rem; text-transform: uppercase; font-family: var(--font-mono);">Host Star Properties</h4>
          <p style="font-size: 0.875rem;"><strong>Primary Name:</strong> ${escapeHtml(candidate.host_star.primary_name)} | <strong>RA:</strong> ${candidate.host_star.ra_deg.toFixed(4)}° | <strong>Dec:</strong> ${candidate.host_star.dec_deg.toFixed(4)}°</p>
          <p style="font-size: 0.8125rem; color: var(--text-muted); margin-top: 0.25rem;"><strong>TIC:</strong> ${escapeHtml(candidate.host_star.tic_id || 'N/A')} | <strong>Gaia DR3:</strong> ${escapeHtml(candidate.host_star.gaia_dr3_id || 'N/A')}</p>
          ${candidate.host_star.aliases && candidate.host_star.aliases.length ? `<p style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem;"><strong>Aliases:</strong> ${candidate.host_star.aliases.map(a => escapeHtml(a)).join(', ')}</p>` : ''}
        </div>
      ` : ''}
    `;

    document.getElementById('modal-close')?.addEventListener('click', closeModal);
    document.getElementById('copy-candidate-link')?.addEventListener('click', (e) => {
      navigator.clipboard.writeText(directUrl).then(() => {
        e.target.textContent = '✓ Copied!';
        setTimeout(() => { e.target.textContent = '📋 Copy Link'; }, 2000);
      });
    });
  } catch (err) {
    content.innerHTML = `
      <div style="color: var(--accent-rose); padding: 2rem; text-align: center;">
        <p>Error loading dossier: ${escapeHtml(err.message)}</p>
        <button class="btn-primary" style="margin-top: 1rem;" onclick="closeModal()">Close</button>
      </div>
    `;
  }
}

function closeModal() {
  const modal = document.getElementById('dossier-modal');
  if (modal && modal.open) {
    modal.close();
  }
  if (window.location.hash.startsWith('#candidate/')) {
    history.replaceState(null, '', '#catalog');
  }
}

function initModalBackdrop() {
  const modal = document.getElementById('dossier-modal');
  if (modal) {
    modal.addEventListener('click', (e) => {
      // If clicking backdrop outside dialog
      const rect = modal.getBoundingClientRect();
      const isInDialog = (rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
        rect.left <= e.clientX && e.clientX <= rect.left + rect.width);
      if (!isInDialog) {
        closeModal();
      }
    });
    modal.addEventListener('cancel', () => {
      // Escape key pressed
      if (window.location.hash.startsWith('#candidate/')) {
        history.replaceState(null, '', '#catalog');
      }
    });
  }
}

// Generate SVG folded transit curve
function generateFoldedLightcurveSvg(depthPpm, durationH, periodD) {
  const width = 680;
  const height = 180;
  const padding = 40;

  // Normalized transit parameters
  const depthFraction = (depthPpm / 1_000_000.0);
  const transitHalfWidthPhase = (durationH / (periodD * 24.0)) / 2.0;

  // Generate synthetic folded data points with scatter
  const points = [];
  const numPoints = 80;
  const phaseMin = -Math.max(0.04, transitHalfWidthPhase * 2.5);
  const phaseMax = Math.max(0.04, transitHalfWidthPhase * 2.5);

  for (let i = 0; i < numPoints; i++) {
    const phase = phaseMin + (i / (numPoints - 1)) * (phaseMax - phaseMin);
    let flux = 1.0;
    if (Math.abs(phase) < transitHalfWidthPhase) {
      flux = 1.0 - depthFraction;
    }
    const noise = (Math.random() - 0.5) * depthFraction * 0.3;
    flux += noise;

    const x = padding + ((phase - phaseMin) / (phaseMax - phaseMin)) * (width - 2 * padding);
    const yMax = 1.0 + depthFraction * 0.5;
    const yMin = 1.0 - depthFraction * 1.5;
    const y = padding + ((yMax - flux) / (yMax - yMin)) * (height - 2 * padding);
    points.push({ x, y });
  }

  const circles = points.map(p => `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.5" fill="rgba(6, 182, 212, 0.7)" />`).join('');

  // Model fit line
  const xLeftIngress = padding + ((-transitHalfWidthPhase - phaseMin) / (phaseMax - phaseMin)) * (width - 2 * padding);
  const xRightEgress = padding + ((transitHalfWidthPhase - phaseMin) / (phaseMax - phaseMin)) * (width - 2 * padding);
  const yOot = padding + ((depthFraction * 0.5) / (depthFraction * 2.0)) * (height - 2 * padding);
  const yBottom = padding + ((depthFraction * 1.5) / (depthFraction * 2.0)) * (height - 2 * padding);

  const modelPath = `M ${padding} ${yOot} L ${xLeftIngress} ${yOot} L ${xLeftIngress + 6} ${yBottom} L ${xRightEgress - 6} ${yBottom} L ${xRightEgress} ${yOot} L ${width - padding} ${yOot}`;

  return `
    <svg viewBox="0 0 ${width} ${height}" style="width: 100%; height: auto; display: block;">
      <line x1="${padding}" y1="${yOot}" x2="${width - padding}" y2="${yOot}" stroke="rgba(255,255,255,0.1)" stroke-dasharray="4" />
      ${circles}
      <path d="${modelPath}" fill="none" stroke="#f43f5e" stroke-width="2.5" />
      <text x="${padding}" y="${height - 10}" fill="var(--text-muted)" font-size="10" font-family="monospace">${phaseMin.toFixed(3)}</text>
      <text x="${width / 2}" y="${height - 10}" fill="var(--text-muted)" font-size="10" text-anchor="middle" font-family="monospace">Phase 0.0 (Mid-Transit)</text>
      <text x="${width - padding}" y="${height - 10}" fill="var(--text-muted)" font-size="10" text-anchor="end" font-family="monospace">+${phaseMax.toFixed(3)}</text>
    </svg>
  `;
}

// --- Observation Queue ---
async function initObservationQueue() {
  const form = document.getElementById('obs-calc-form');
  const useLocBtn = document.getElementById('use-my-location');

  if (useLocBtn) {
    useLocBtn.addEventListener('click', () => {
      if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(pos => {
          document.getElementById('obs-lat').value = pos.coords.latitude.toFixed(4);
          document.getElementById('obs-lon').value = pos.coords.longitude.toFixed(4);
        });
      }
    });
  }

  const now = new Date();
  const nextWeek = new Date(now.getTime() + 7 * 24 * 3600 * 1000);
  const startInput = document.getElementById('obs-start');
  const endInput = document.getElementById('obs-end');
  if (startInput) startInput.value = now.toISOString().slice(0, 16);
  if (endInput) endInput.value = nextWeek.toISOString().slice(0, 16);

  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      await calculateObservationQueue();
    });
  }
}

async function calculateObservationQueue() {
  const container = document.getElementById('queue-results-container');
  if (!container) return;

  const lat = document.getElementById('obs-lat').value;
  const lon = document.getElementById('obs-lon').value;
  const startVal = document.getElementById('obs-start').value;
  const endVal = document.getElementById('obs-end').value;
  const minPriority = document.getElementById('obs-min-priority').value || '0.0';

  if (!startVal || !endVal) return;

  container.innerHTML = '<div style="color: var(--text-muted); text-align: center; padding: 2rem;">Computing topocentric ephemerides and transit visibility...</div>';

  try {
    const startIso = new Date(startVal).toISOString();
    const endIso = new Date(endVal).toISOString();

    const params = new URLSearchParams({
      lat: lat,
      lon: lon,
      start_time: startIso,
      end_time: endIso,
      min_priority: minPriority,
      limit: '50'
    });

    const res = await fetch(`/v1/observations/queue?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const data = await res.json();

    if (data.items.length === 0) {
      container.innerHTML = '<div style="color: var(--text-muted); text-align: center; padding: 2rem;">No observable transit windows found for the selected window and coordinates.</div>';
      return;
    }

    container.innerHTML = `
      <div class="queue-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Priority</th>
              <th>Target & Signal</th>
              <th>Mid-Transit (UTC)</th>
              <th>Duration</th>
              <th>Timing Unc.</th>
              <th>Altitude / Airmass</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            ${data.items.map(item => `
              <tr>
                <td><span class="score-badge">${item.priority_score.toFixed(2)}</span></td>
                <td>
                  <a href="#candidate/${encodeURIComponent(item.signal_id)}" class="queue-link">
                    <strong>${escapeHtml(item.target_name)}</strong><br>
                    <small style="color: var(--text-muted);">${escapeHtml(item.signal_name)}</small>
                  </a>
                </td>
                <td>
                  <span style="font-family: var(--font-mono);">${new Date(item.transit_mid_utc).toUTCString()}</span>
                </td>
                <td>${item.duration_h.toFixed(2)} h</td>
                <td>±${item.timing_uncertainty_min.toFixed(1)} min</td>
                <td>${item.target_altitude_deg.toFixed(1)}° (z=${item.target_airmass.toFixed(2)})</td>
                <td>
                  ${item.is_observable ? '<span style="color: var(--accent-emerald);">● Observable</span>' : '<span style="color: var(--accent-rose);">○ Daylight/Moon</span>'}
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;

    // Attach click listeners to queue candidate links with modifier key support
    container.querySelectorAll('.queue-link').forEach(link => {
      link.addEventListener('click', (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
        e.preventDefault();
        const href = link.getAttribute('href');
        if (href) {
          history.pushState(null, '', href);
          handleRoute();
        }
      });
    });
  } catch (err) {
    container.innerHTML = `<div style="color: var(--accent-rose); text-align: center; padding: 2rem;">Error computing observation queue: ${escapeHtml(err.message)}</div>`;
  }
}

// --- Snapshots Table ---
async function initSnapshots() {
  const container = document.getElementById('snapshots-container');
  if (!container) return;

  try {
    const res = await fetch('/v1/snapshots');
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const snapshots = await res.json();

    if (snapshots.length === 0) {
      container.innerHTML = '<div style="color: var(--text-muted); padding: 1.5rem;">No published snapshots recorded in ledger.</div>';
      return;
    }

    container.innerHTML = `
      <div class="queue-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Source</th>
              <th>Published UTC</th>
              <th>Commit SHA</th>
              <th>Record Count</th>
              <th>Status</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            ${snapshots.map(s => `
              <tr>
                <td><strong>${escapeHtml(s.source_name)}</strong></td>
                <td><span style="font-family: var(--font-mono);">${new Date(s.published_at).toUTCString()}</span></td>
                <td><code>${s.commit_sha ? s.commit_sha.slice(0, 10) : 'HEAD'}</code></td>
                <td>${s.record_count.toLocaleString()}</td>
                <td><span class="badge badge-confirmed">${escapeHtml(s.status)}</span></td>
                <td>
                  <a href="#catalog?source=${encodeURIComponent(s.source_name)}" class="queue-link" style="font-size: 0.8125rem;">
                    Filter in Catalog ↗
                  </a>
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;

    // Attach click listeners to snapshot action links
    container.querySelectorAll('.queue-link').forEach(link => {
      link.addEventListener('click', (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
        e.preventDefault();
        const href = link.getAttribute('href');
        if (href) {
          history.pushState(null, '', href);
          handleRoute();
        }
      });
    });
  } catch (err) {
    container.innerHTML = `<div style="color: var(--accent-rose); padding: 1.5rem;">Error loading snapshots: ${escapeHtml(err.message)}</div>`;
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
