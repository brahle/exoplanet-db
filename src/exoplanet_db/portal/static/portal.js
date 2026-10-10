// Exoplanet DB Portal Client JavaScript

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initTabs();
  initCatalog();
  initObservationQueue();
  initSnapshots();
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

// --- Tab Switching ---
function initTabs() {
  const tabBtns = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');

  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const targetTab = btn.getAttribute('data-tab');

      tabBtns.forEach(b => b.classList.remove('active'));
      tabContents.forEach(c => c.classList.remove('active'));

      btn.classList.add('active');
      const content = document.getElementById(`${targetTab}-content`);
      if (content) content.classList.add('active');
    });
  });
}

// --- Candidates Catalog ---
let candidatesData = [];

async function initCatalog() {
  const searchInput = document.getElementById('catalog-search');
  const dispositionFilter = document.getElementById('disposition-filter');

  await loadCandidates();

  if (searchInput) {
    let debounceTimer;
    searchInput.addEventListener('input', () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => filterAndRenderCandidates(), 250);
    });
  }

  if (dispositionFilter) {
    dispositionFilter.addEventListener('change', () => filterAndRenderCandidates());
  }
}

async function loadCandidates() {
  const grid = document.getElementById('candidate-grid');
  if (!grid) return;
  grid.innerHTML = '<div style="color: var(--text-muted); grid-column: 1/-1; text-align: center; padding: 2rem;">Loading planetary signals and candidates...</div>';

  try {
    const res = await fetch('/v1/candidates?limit=100');
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const data = await res.json();
    candidatesData = data.items || [];
    filterAndRenderCandidates();
  } catch (err) {
    grid.innerHTML = `<div style="color: var(--accent-rose); grid-column: 1/-1; text-align: center;">Failed to load catalog: ${err.message}</div>`;
  }
}

function filterAndRenderCandidates() {
  const searchVal = (document.getElementById('catalog-search')?.value || '').toLowerCase().trim();
  const dispVal = document.getElementById('disposition-filter')?.value || 'all';
  const grid = document.getElementById('candidate-grid');
  if (!grid) return;

  const filtered = candidatesData.filter(c => {
    const matchesSearch = !searchVal || 
      c.display_name.toLowerCase().includes(searchVal) || 
      c.id.toLowerCase().includes(searchVal) ||
      (c.target_id && c.target_id.toLowerCase().includes(searchVal));
    
    const matchesDisp = dispVal === 'all' || c.current_disposition === dispVal;
    return matchesSearch && matchesDisp;
  });

  if (filtered.length === 0) {
    grid.innerHTML = '<div style="color: var(--text-muted); grid-column: 1/-1; text-align: center; padding: 2rem;">No candidates matched your search criteria.</div>';
    return;
  }

  grid.innerHTML = filtered.map(c => renderCandidateCard(c)).join('');

  // Attach card click handlers for detailed dossier
  document.querySelectorAll('.candidate-card').forEach(card => {
    card.addEventListener('click', () => {
      const candidateId = card.getAttribute('data-id');
      openDossierModal(candidateId);
    });
  });
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

  return `
    <article class="candidate-card" data-id="${c.id}">
      <div class="card-header">
        <div>
          <h3 class="candidate-name">${escapeHtml(c.display_name)}</h3>
          <span class="host-name">Target: ${escapeHtml(c.target_id)}</span>
        </div>
        <span class="badge ${dispClass}">${dispLabel}</span>
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
    </article>
  `;
}

// --- Candidate Dossier Modal ---
async function openDossierModal(candidateId) {
  const modal = document.getElementById('dossier-modal');
  const content = document.getElementById('dossier-modal-content');
  if (!modal || !content) return;

  content.innerHTML = '<div style="text-align: center; padding: 2rem;">Loading candidate dossier and lightcurve...</div>';
  modal.showModal();

  try {
    const res = await fetch(`/v1/candidates/${candidateId}`);
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const candidate = await res.json();

    const depth = candidate.depth_ppm || 5000;
    const duration = candidate.duration_h || 2.5;
    const period = candidate.period_d || 3.0;

    content.innerHTML = `
      <div class="modal-header">
        <div>
          <h2 style="font-size: 1.5rem; font-weight: 700;">${escapeHtml(candidate.display_name)}</h2>
          <span style="color: var(--text-secondary); font-family: var(--font-mono); font-size: 0.875rem;">ID: ${escapeHtml(candidate.id)}</span>
        </div>
        <button class="modal-close-btn" id="modal-close">&times;</button>
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
            ? `<ul style="padding-left: 1.25rem; font-size: 0.875rem; color: var(--text-primary);">${candidate.scientific_limits.map(l => `<li>${escapeHtml(l)}</li>`).join('')}</ul>`
            : '<p style="font-size: 0.875rem; color: var(--text-muted);">No restrictive observational limits flagged.</p>'
        }
      </div>

      ${candidate.host_star ? `
        <div style="margin-top: 1.5rem; padding: 1rem; background: var(--bg-card); border-radius: var(--radius-sm);">
          <h4 style="font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 0.5rem; text-transform: uppercase; font-family: var(--font-mono);">Host Star Properties</h4>
          <p style="font-size: 0.875rem;"><strong>Primary Name:</strong> ${escapeHtml(candidate.host_star.primary_name)} | <strong>RA:</strong> ${candidate.host_star.ra_deg.toFixed(4)}° | <strong>Dec:</strong> ${candidate.host_star.dec_deg.toFixed(4)}°</p>
          <p style="font-size: 0.8125rem; color: var(--text-muted); margin-top: 0.25rem;"><strong>TIC:</strong> ${escapeHtml(candidate.host_star.tic_id || 'N/A')} | <strong>Gaia DR3:</strong> ${escapeHtml(candidate.host_star.gaia_dr3_id || 'N/A')}</p>
        </div>
      ` : ''}
    `;

    document.getElementById('modal-close')?.addEventListener('click', () => modal.close());
  } catch (err) {
    content.innerHTML = `<div style="color: var(--accent-rose); padding: 2rem;">Error loading dossier: ${err.message}</div>`;
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
      // Trapezoid dip
      flux = 1.0 - depthFraction;
    }
    // Add tiny simulated scatter (~0.15 depth)
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
      <!-- Grid / Baseline -->
      <line x1="${padding}" y1="${yOot}" x2="${width - padding}" y2="${yOot}" stroke="rgba(255,255,255,0.1)" stroke-dasharray="4" />
      <!-- Points -->
      ${circles}
      <!-- Model Fit -->
      <path d="${modelPath}" fill="none" stroke="#f43f5e" stroke-width="2.5" />
      <!-- Phase labels -->
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

  // Pre-fill dates: start today, end +7 days
  const now = new Date();
  const nextWeek = new Date(now.getTime() + 7 * 24 * 3600 * 1000);
  const startInput = document.getElementById('obs-start');
  const endInput = document.getElementById('obs-end');
  if (startInput) startInput.value = now.toISOString().slice(0, 16);
  if (endInput) endInput.value = nextWeek.toISOString().slice(0, 16);

  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      await calculateQueue();
    });
  }
}

async function calculateQueue() {
  const container = document.getElementById('queue-results-container');
  if (!container) return;

  const lat = document.getElementById('obs-lat').value;
  const lon = document.getElementById('obs-lon').value;
  const start = document.getElementById('obs-start').value;
  const end = document.getElementById('obs-end').value;
  const minPriority = document.getElementById('obs-min-priority').value || '0.0';

  container.innerHTML = '<div style="color: var(--text-muted); text-align: center; padding: 2rem;">Computing coherent topocentric ephemerides and observability metrics...</div>';

  try {
    const startIso = new Date(start).toISOString();
    const endIso = new Date(end).toISOString();

    const url = `/v1/observations/queue?lat=${lat}&lon=${lon}&start_time=${encodeURIComponent(startIso)}&end_time=${encodeURIComponent(endIso)}&min_priority=${minPriority}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const data = await res.json();

    if (!data.items || data.items.length === 0) {
      container.innerHTML = '<div style="color: var(--text-muted); text-align: center; padding: 2rem;">No observable transit windows found for this location and time window.</div>';
      return;
    }

    container.innerHTML = `
      <div class="queue-table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>Priority</th>
              <th>Target & Signal</th>
              <th>Transit Mid-Time (UTC)</th>
              <th>Duration</th>
              <th>Timing Unc.</th>
              <th>Altitude / Airmass</th>
              <th>Night / Moon</th>
            </tr>
          </thead>
          <tbody>
            ${data.items.map(item => `
              <tr>
                <td><span class="score-badge">${item.priority_score.toFixed(2)}</span></td>
                <td>
                  <strong>${escapeHtml(item.target_name)}</strong><br>
                  <small style="color: var(--text-muted);">${escapeHtml(item.signal_name)}</small>
                </td>
                <td>
                  <span style="font-family: var(--font-mono);">${new Date(item.transit_mid_utc).toUTCString()}</span>
                </td>
                <td>${item.duration_h.toFixed(2)} h</td>
                <td>±${item.timing_uncertainty_min.toFixed(1)} min</td>
                <td>${item.target_altitude_deg}° (z=${item.target_airmass.toFixed(2)})</td>
                <td>
                  ${item.is_observable ? '<span style="color: var(--accent-emerald);">● Observable</span>' : '<span style="color: var(--accent-rose);">○ Daylight/Moon</span>'}
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  } catch (err) {
    container.innerHTML = `<div style="color: var(--accent-rose); text-align: center; padding: 2rem;">Error computing observation queue: ${err.message}</div>`;
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
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  } catch (err) {
    container.innerHTML = `<div style="color: var(--accent-rose); padding: 1.5rem;">Error loading snapshots: ${err.message}</div>`;
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
