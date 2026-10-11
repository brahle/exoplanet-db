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
    const { id, view } = parseCandidateHash(hash);
    switchTab('candidates', false);
    if (id) {
      openDossierModal(id, view);
    }
    return;
  }

  // 2. Close modal if navigating away from candidate
  const modal = document.getElementById('dossier-modal');
  if (modal && modal.open) {
    disposeDetailViews();
    detailState.candidate = null;
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

    // Filters live in the hash: #catalog?search=...&source=...&min_score=...
    const qIndex = hash.indexOf('?');
    const params = new URLSearchParams(qIndex !== -1 ? hash.slice(qIndex + 1) : '');
    if (applyCatalogParams(params)) {
      loadCandidates();
    }
  }
}

// --- Candidates Catalog ---
let currentQueryId = 0;

const SOURCE_LABELS = {
  nasa_pscomppars: ['NASA Exoplanet Archive', 'confirmed planets', 'https://exoplanetarchive.ipac.caltech.edu/'],
  nasa_toi: ['NASA Exoplanet Archive', 'TESS Objects of Interest', 'https://exoplanetarchive.ipac.caltech.edu/cgi-bin/TblView/nph-tblView?app=ExoTbls&config=TOI'],
  exofop_ctoi: ['ExoFOP-TESS', 'community TOIs', 'https://exofop.ipac.caltech.edu/tess/view_ctoi.php'],
  'eumemic/exoplanets': ['eumemic/exoplanets', 'TESS candidates', 'https://github.com/eumemic/exoplanets'],
  'git:brahle/exoplanet-candidates': ['brahle/exoplanet-candidates', 'evidence dossiers', 'https://github.com/brahle/exoplanet-candidates'],
  'eumemic/exoplanets:vetting': ['eumemic/exoplanets', 'vetting results on known signals', 'https://github.com/eumemic/exoplanets/tree/main/results_public'],
};

function renderSourceStrip(sources) {
  const strip = document.getElementById('source-strip');
  if (!strip) return;
  strip.replaceChildren();
  const title = document.createElement('span');
  title.className = 'source-strip-title';
  title.textContent = 'Sources';
  strip.appendChild(title);
  for (const src of sources) {
    const [name, what, home] = SOURCE_LABELS[src.source_name] || [src.source_name, '', null];
    const item = document.createElement('span');
    item.className = 'source-chip';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.title = `Show the ${src.signals.toLocaleString()} signals reported by ${name}${what ? ' (' + what + ')' : ''}`;
    const strong = document.createElement('strong');
    strong.textContent = src.signals.toLocaleString();
    btn.append(strong, ` ${name}${what ? ' · ' + what : ''}`);
    btn.addEventListener('click', () => {
      const select = document.getElementById('source-filter');
      if (!select) return;
      if (![...select.options].some((o) => o.value === src.source_name)) {
        select.add(new Option(name, src.source_name));
      }
      select.value = src.source_name;
      select.dispatchEvent(new Event('change'));
    });
    item.appendChild(btn);
    if (src.last_ingested_utc) {
      const when = document.createElement('span');
      when.className = 'source-chip-when';
      when.textContent = `updated ${src.last_ingested_utc.slice(0, 10)}`;
      item.appendChild(when);
    }
    if (home) {
      const link = document.createElement('a');
      link.href = home;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.className = 'source-chip-link';
      link.title = `Open ${name}`;
      link.textContent = '↗';
      item.appendChild(link);
    }
    strip.appendChild(item);
  }
}

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
    renderSourceStrip(stats.sources || []);
    const breakdown = document.getElementById('stat-confirmed-breakdown');
    if (breakdown) {
      const extra = stats.confirmed_planets - stats.confirmed_in_nasa_archive;
      breakdown.textContent =
        `${Number(stats.confirmed_in_nasa_archive).toLocaleString()} in NASA archive` +
        (extra > 0 ? ` + ${extra.toLocaleString()} TFOPWG-confirmed` : '') +
        ` · ${Number(stats.confirmed_tess_discoveries).toLocaleString()} TESS discoveries`;
      breakdown.title =
        'NASA Exoplanet Archive composite table, plus TOIs that TFOPWG marks confirmed (CP/KP) but the archive has not listed yet.';
    }
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

      if (filter === 'all') {
        applyCatalogParams(new URLSearchParams());
      } else if (filter === 'confirmed_planet') {
        if (dSelect) dSelect.value = 'confirmed_planet';
      } else if (filter === 'candidates') {
        if (dSelect) dSelect.value = 'planet_candidate,unclassified_transit_like_signal';
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

  let advTimer;
  for (const [id] of CATALOG_FILTERS.slice(3)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', () => {
      clearTimeout(advTimer);
      advTimer = setTimeout(() => {
        syncCatalogUrl();
        loadCandidates();
      }, 350);
    });
  }
  document.getElementById('more-filters-toggle')?.addEventListener('click', (e) => {
    const panel = document.getElementById('advanced-filters');
    panel.hidden = !panel.hidden;
    e.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
  });
  document.getElementById('reset-filters')?.addEventListener('click', () => {
    applyCatalogParams(new URLSearchParams());
    syncCatalogUrl();
    loadCandidates();
  });
}

// Every catalogue filter: [element id, API/URL parameter, default value]
const CATALOG_FILTERS = [
  ['catalog-search', 'search', ''],
  ['source-filter', 'source', 'all'],
  ['disposition-filter', 'disposition', 'all'],
  ['f-min-period', 'min_period_d', ''],
  ['f-max-period', 'max_period_d', ''],
  ['f-min-depth', 'min_depth_ppm', ''],
  ['f-max-depth', 'max_depth_ppm', ''],
  ['f-min-duration', 'min_duration_h', ''],
  ['f-max-duration', 'max_duration_h', ''],
  ['f-min-score', 'min_score', ''],
  ['f-max-score', 'max_score', ''],
  ['f-localisation', 'localisation', 'all'],
  ['f-timing', 'timing', 'all'],
  ['f-sort', 'sort', 'period'],
];
const ADVANCED_FILTER_IDS = CATALOG_FILTERS.slice(3).map(([id]) => id).filter((id) => id !== 'f-sort');

function catalogParams() {
  const params = new URLSearchParams();
  for (const [id, name, fallback] of CATALOG_FILTERS) {
    const value = (document.getElementById(id)?.value || '').trim();
    if (value && value !== fallback) params.set(name, value);
  }
  return params;
}

function applyCatalogParams(params) {
  let changed = false;
  for (const [id, name, fallback] of CATALOG_FILTERS) {
    const el = document.getElementById(id);
    if (!el) continue;
    const value = params.get(name) ?? fallback;
    if (el.value !== value) {
      el.value = value;
      changed = true;
    }
  }
  updateFilterToggle();
  return changed;
}

function updateFilterToggle() {
  const toggle = document.getElementById('more-filters-toggle');
  if (!toggle) return;
  const active = ADVANCED_FILTER_IDS.filter((id) => {
    const el = document.getElementById(id);
    return el && el.value && el.value !== 'all';
  }).length;
  toggle.textContent = active ? `More filters (${active})` : 'More filters';
  if (active) {
    document.getElementById('advanced-filters').hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
  }
}

function syncCatalogUrl() {
  const queryStr = catalogParams().toString();
  const newHash = queryStr ? `#catalog?${queryStr}` : '#catalog';
  if (window.location.hash !== newHash) {
    history.replaceState(null, '', newHash);
  }
  updateFilterToggle();
}

async function loadCandidates() {
  const grid = document.getElementById('candidate-grid');
  const countEl = document.getElementById('catalog-results-count');
  if (!grid) return;

  const filterParams = catalogParams();
  const filtered = [...filterParams.keys()].some((k) => k !== 'sort');

  grid.innerHTML = '<div style="color: var(--text-muted); grid-column: 1/-1; text-align: center; padding: 2.5rem;">Loading planetary signals and candidates...</div>';

  const thisQuery = ++currentQueryId;
  try {
    const params = new URLSearchParams(filterParams);
    params.set('limit', '100');

    const res = await fetch(`/v1/candidates?${params.toString()}`);
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const data = await res.json();

    if (thisQuery !== currentQueryId) return; // Stale response discarded

    const candidates = data.items || [];
    const totalMatches = data.total ?? candidates.length;

    if (countEl) {
      if (filtered) {
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

const SCORE_HELP =
  'Planet score: a 0–100 heuristic of how likely this is a real planet, from its strongest catalogue classification adjusted for support and red flags. Not a calibrated probability.';

function renderPlanetScore(score, compact = true) {
  if (score === null || score === undefined) return '';
  const tone = score >= 90 ? 'high' : score >= 60 ? 'good' : score >= 30 ? 'mid' : score > 0 ? 'low' : 'zero';
  return `
    <span class="planet-score planet-score-${tone}${compact ? '' : ' planet-score-large'}" title="${escapeHtml(SCORE_HELP)}">
      <span class="planet-score-bar" aria-hidden="true"><i style="width:${Math.max(2, score)}%"></i></span>
      <span class="planet-score-value">${score}</span>${compact ? '' : '<span class="planet-score-max">/ 100</span>'}
    </span>`;
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
          <span class="source-badge ${srcClass}" title="${escapeHtml((c.source_names && c.source_names.length ? c.source_names : [src]).join(', '))}">${escapeHtml(src)}${c.source_names && c.source_names.length > 1 ? ` +${c.source_names.length - 1}` : ''}</span>
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
        ${renderPlanetScore(c.planet_score)}
        <span class="tag-pill">Loc: ${escapeHtml(c.source_localisation_status)}</span>
        <span class="tag-pill">Timing: ${escapeHtml(c.timing_consistency_status)}</span>
        ${c.scientific_limits && c.scientific_limits.length ? `<span class="tag-pill">${c.scientific_limits.length} Limits</span>` : ''}
      </div>
    </a>
  `;
}

// --- Candidate Dossier Modal ---
const DETAIL_VIEWS = [
  ['transit', 'Transit model'],
  ['timing', 'Timing forecast'],
  ['population', 'Population'],
  ['system', '3D system'],
];
const detailState = { candidate: null, view: 'transit', system3d: null, requestSeq: 0 };

function parseCandidateHash(hash) {
  const rest = hash.slice('#candidate/'.length);
  const [rawId, query = ''] = rest.split('?');
  const view = new URLSearchParams(query).get('view');
  return { id: decodeURIComponent(rawId || '').trim(), view };
}

function fmtValue(value, digits, unit) {
  if (value === null || value === undefined || value === 0) return 'N/A';
  return `${value.toFixed(digits)}${unit ? ' ' + unit : ''}`;
}

function fmtErr(value, err, digits, unit) {
  if (!value || !err) return '';
  const e = err < 1e-3 ? err.toExponential(1) : err.toFixed(Math.max(digits, 1));
  return `± ${e}${unit ? ' ' + unit : ''}`;
}

function sourceLabel(src) {
  return src.source_key && !src.source_key.startsWith(src.source_name)
    ? `${src.source_name} · ${src.source_key}`
    : src.source_name;
}

function renderSourceLink(src, className = 'source-link') {
  if (!src) return '';
  const label = escapeHtml(sourceLabel(src));
  if (!src.url) return `<span class="${className}" title="No public landing page for this source">${label}</span>`;
  return `<a class="${className}" href="${escapeHtml(src.url)}" target="_blank" rel="noopener noreferrer">${label} <span aria-hidden="true">↗</span></a>`;
}

function disposeDetailViews() {
  if (detailState.system3d) {
    detailState.system3d.dispose();
    detailState.system3d = null;
  }
}

async function openDossierModal(candidateId, view) {
  const modal = document.getElementById('dossier-modal');
  const content = document.getElementById('dossier-modal-content');
  if (!modal || !content) return;
  const requestedView = DETAIL_VIEWS.some(([k]) => k === view) ? view : null;

  // Same candidate, different view (e.g. back/forward between views): just switch.
  if (modal.open && detailState.candidate && detailState.candidate.id === candidateId) {
    if (requestedView && requestedView !== detailState.view) showDetailView(requestedView, false);
    return;
  }

  disposeDetailViews();
  detailState.candidate = null;
  const seq = ++detailState.requestSeq;
  content.innerHTML = '<div class="detail-loading">Loading candidate dossier…</div>';
  if (!modal.open) {
    modal.showModal();
  }

  try {
    const res = await fetch(`/v1/candidates/${encodeURIComponent(candidateId)}`);
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    const candidate = await res.json();
    if (seq !== detailState.requestSeq) return;
    detailState.candidate = candidate;
    content.innerHTML = renderDossier(candidate);
    bindDossier(candidate);
    let stored = null;
    try { stored = localStorage.getItem('detail-view'); } catch (e) { /* storage unavailable */ }
    showDetailView(requestedView || stored || 'transit', false);
  } catch (err) {
    if (seq !== detailState.requestSeq) return;
    content.innerHTML = `
      <div style="color: var(--accent-rose); padding: 2rem; text-align: center;">
        <p>Error loading dossier: ${escapeHtml(err.message)}</p>
        <button class="btn-primary" style="margin-top: 1rem;" onclick="closeModal()">Close</button>
      </div>
    `;
  }
}

function renderDossier(c) {
  const planet = c.planet || {};
  const star = c.host_star || {};
  const dispClass = {
    confirmed_planet: 'badge-confirmed',
    validated_planet: 'badge-confirmed',
    planet_candidate: 'badge-candidate',
    unclassified_transit_like_signal: 'badge-unclassified',
    false_positive: 'badge-false-positive',
  }[c.current_disposition] || 'badge-candidate';
  const otherSources = (c.sources || []).length - (c.primary_source ? 1 : 0);
  const radiusNote = planet.radius_basis === 'transit_depth' ? 'from depth' : planet.radius_basis === 'catalogue' ? 'published' : '';
  const aNote = planet.semi_major_axis_basis === 'kepler3_assumed_solar_mass' ? 'assumes 1 M☉' : planet.semi_major_axis_basis ? 'Kepler III' : '';

  const metric = (label, value, note) => `
    <div class="detail-metric">
      <span class="metric-label">${label}</span>
      <span class="metric-val">${value}</span>
      ${note ? `<span class="metric-note">${note}</span>` : ''}
    </div>`;

  const sourcesRows = (c.sources || []).map((s) => `
    <tr>
      <td>${renderSourceLink(s)}</td>
      <td>${escapeHtml(s.author_or_team || '—')}</td>
      <td>${escapeHtml((s.reported_disposition || '—').replace(/_/g, ' '))}</td>
      <td>${s.commit_sha && s.commit_sha.length >= 7 && s.commit_sha !== 'HEAD' ? `<code>${escapeHtml(s.commit_sha.slice(0, 10))}</code>` : '—'}</td>
      <td>${s.retrieval_utc ? escapeHtml(s.retrieval_utc.slice(0, 10)) : '—'}</td>
      <td>${escapeHtml(s.license || '—')}</td>
    </tr>`).join('');

  const ticLink = star.tic_id ? `<a href="https://exofop.ipac.caltech.edu/tess/target.php?id=${encodeURIComponent(star.tic_id)}" target="_blank" rel="noopener noreferrer">ExoFOP TIC ${escapeHtml(star.tic_id)} ↗</a>` : '';
  const simbadLink = star.ra_deg !== undefined ? `<a href="https://simbad.cds.unistra.fr/simbad/sim-coo?Coord=${star.ra_deg}+${star.dec_deg}&CooFrame=ICRS&Radius=10&Radius.unit=arcsec" target="_blank" rel="noopener noreferrer">SIMBAD (10″ cone) ↗</a>` : '';
  const gaiaLink = star.gaia_dr3_id ? `<a href="https://vizier.cds.unistra.fr/viz-bin/VizieR-5?-source=I/355/gaiadr3&Source=${encodeURIComponent(star.gaia_dr3_id)}" target="_blank" rel="noopener noreferrer">Gaia DR3 ${escapeHtml(star.gaia_dr3_id)} ↗</a>` : '';

  return `
    <div class="modal-header detail-header">
      <div class="detail-title">
        <h2>${escapeHtml(c.display_name)}</h2>
        <div class="detail-subtitle">
          Host <strong>${escapeHtml(c.target_name || star.primary_name || '—')}</strong>
          <span class="detail-id">ID ${escapeHtml(c.id)}</span>
        </div>
      </div>
      <div class="detail-actions">
        <button id="copy-candidate-link" class="tag-pill detail-action" type="button" title="Copy direct link to clipboard">Copy link</button>
        <a href="#candidate/${encodeURIComponent(c.id)}" target="_blank" rel="noopener" class="tag-pill detail-action" title="Open direct URL in new tab">New tab ↗</a>
        <button class="modal-close-btn" id="modal-close" aria-label="Close dialog">&times;</button>
      </div>
    </div>

    <div class="detail-badges">
      <span class="badge ${dispClass}">${escapeHtml(c.current_disposition.replace(/_/g, ' '))}</span>
      <span class="tag-pill">Localisation: ${escapeHtml(c.source_localisation_status)}</span>
      <span class="tag-pill">Timing: ${escapeHtml(c.timing_consistency_status)}</span>
    </div>

    ${c.planet_score !== null && c.planet_score !== undefined ? `
      <details class="detail-score">
        <summary>
          <span class="metric-label">Planet score</span>
          ${renderPlanetScore(c.planet_score, false)}
          <span class="detail-score-hint">heuristic, not a probability · why?</span>
        </summary>
        <ul class="score-factors">
          ${(c.score_factors || []).map((f, i) => `<li><span class="score-delta">${i === 0 ? '' : f.delta > 0 ? '+' : ''}${f.delta}</span>${escapeHtml(f.label)}</li>`).join('')}
        </ul>
      </details>` : ''}

    <div class="detail-source">
      <span class="metric-label">Source</span>
      ${c.primary_source ? renderSourceLink(c.primary_source) : `<span class="source-link">${escapeHtml(c.source_name || 'unknown')}</span>`}
      ${otherSources > 0 ? `<a class="detail-more" href="#" data-scroll="detail-sources">+${otherSources} more</a>` : ''}
    </div>

    <div class="detail-metrics">
      ${metric('Period', fmtValue(c.period_d, 6, 'd'), fmtErr(c.period_d, c.period_err_d, 6, 'd'))}
      ${metric('Epoch (BJD_TDB)', fmtValue(c.epoch_bjd, 4, ''), fmtErr(c.epoch_bjd, c.epoch_err_d, 4, 'd'))}
      ${metric('Duration', fmtValue(c.duration_h, 2, 'h'), fmtErr(c.duration_h, planet.duration_err_h, 2, 'h'))}
      ${metric('Depth', fmtValue(c.depth_ppm, 0, 'ppm'), fmtErr(c.depth_ppm, planet.depth_err_ppm, 0, 'ppm'))}
      ${metric('Radius', planet.radius_earth ? `${planet.radius_earth.toFixed(2)} R⊕` : 'N/A', radiusNote || (star.radius_rsun ? '' : 'needs stellar radius'))}
      ${metric('Semi-major axis', planet.semi_major_axis_au ? `${planet.semi_major_axis_au.toFixed(4)} AU` : 'N/A', aNote)}
    </div>

    <section class="detail-viz" aria-label="Visualisations">
      <div class="segmented detail-view-tabs" role="tablist">
        ${DETAIL_VIEWS.map(([k, label]) => `<button type="button" role="tab" data-view="${k}" aria-selected="false">${label}</button>`).join('')}
      </div>
      <div id="detail-view-panel" class="detail-view-panel" role="tabpanel"></div>
    </section>

    <section id="detail-sources" class="detail-section">
      <h4 class="detail-section-title">Sources &amp; provenance</h4>
      ${sourcesRows ? `
        <div class="table-scroll">
          <table class="viz-table detail-sources-table">
            <thead><tr><th>Source</th><th>Author / team</th><th>Reported as</th><th>Commit</th><th>Retrieved</th><th>License</th></tr></thead>
            <tbody>${sourcesRows}</tbody>
          </table>
        </div>` : '<p class="detail-muted">No report rows are linked to this signal.</p>'}
    </section>

    <section class="detail-section">
      <h4 class="detail-section-title">Scientific limits &amp; constraints</h4>
      ${c.scientific_limits && c.scientific_limits.length
        ? `<ul class="detail-limits">${c.scientific_limits.map((l) => `<li>${escapeHtml(l)}</li>`).join('')}</ul>`
        : '<p class="detail-muted">No restrictive observational limits flagged.</p>'}
    </section>

    ${c.host_star ? `
      <section class="detail-section detail-card">
        <h4 class="detail-section-title">Host star</h4>
        <div class="detail-metrics compact">
          ${metric('Name', escapeHtml(star.primary_name))}
          ${metric('RA / Dec', `${star.ra_deg.toFixed(4)}° / ${star.dec_deg.toFixed(4)}°`)}
          ${metric('Teff', star.teff_k ? `${Math.round(star.teff_k)} K` : 'N/A')}
          ${metric('Radius', star.radius_rsun ? `${star.radius_rsun.toFixed(3)} R☉` : 'N/A')}
          ${metric('Mass', star.mass_msun ? `${star.mass_msun.toFixed(3)} M☉` : 'N/A')}
          ${metric('Magnitude', star.tess_mag ? `T ${star.tess_mag.toFixed(2)}` : star.v_mag ? `V ${star.v_mag.toFixed(2)}` : 'N/A')}
        </div>
        <p class="detail-links">${[ticLink, gaiaLink, simbadLink].filter(Boolean).join('')}</p>
        ${star.aliases && star.aliases.length ? `<p class="detail-muted"><strong>Aliases:</strong> ${star.aliases.map((a) => escapeHtml(a)).join(', ')}</p>` : ''}
      </section>
    ` : ''}
  `;
}

function bindDossier(c) {
  const directUrl = `${window.location.origin}${window.location.pathname}#candidate/${c.id}`;
  document.getElementById('modal-close')?.addEventListener('click', closeModal);
  document.getElementById('copy-candidate-link')?.addEventListener('click', (e) => {
    navigator.clipboard.writeText(directUrl).then(() => {
      e.target.textContent = 'Copied ✓';
      setTimeout(() => { e.target.textContent = 'Copy link'; }, 2000);
    });
  });
  document.querySelectorAll('.detail-view-tabs button').forEach((btn) => {
    btn.addEventListener('click', () => showDetailView(btn.dataset.view, true));
  });
  document.querySelectorAll('[data-scroll]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      document.getElementById(a.dataset.scroll)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
}

function chartInputs(c) {
  const planet = c.planet || {};
  const star = c.host_star || {};
  return {
    id: c.id,
    display_name: c.display_name,
    period_d: c.period_d,
    period_err_d: c.period_err_d,
    epoch_bjd: c.epoch_bjd,
    epoch_err_d: c.epoch_err_d,
    duration_h: c.duration_h,
    depth_ppm: c.depth_ppm,
    depth_err_ppm: planet.depth_err_ppm,
    radius_earth: planet.radius_earth,
    a_au: planet.semi_major_axis_basis === 'kepler3_catalogue_mass' ? planet.semi_major_axis_au : null,
    star_radius_rsun: star.radius_rsun,
    teff_k: star.teff_k,
    system: c.system || [],
  };
}

async function showDetailView(view, updateUrl) {
  const c = detailState.candidate;
  const panel = document.getElementById('detail-view-panel');
  if (!c || !panel) return;
  if (!DETAIL_VIEWS.some(([k]) => k === view)) view = 'transit';
  disposeDetailViews();
  detailState.view = view;
  try { localStorage.setItem('detail-view', view); } catch (e) { /* storage unavailable */ }
  document.querySelectorAll('.detail-view-tabs button').forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', String(active));
  });
  if (updateUrl) history.replaceState(null, '', `#candidate/${encodeURIComponent(c.id)}?view=${view}`);

  panel.replaceChildren();
  const inputs = chartInputs(c);
  if (view === 'transit') window.ExoCharts.renderTransit(panel, inputs);
  else if (view === 'timing') window.ExoCharts.renderTiming(panel, inputs);
  else if (view === 'population') window.ExoCharts.renderPopulation(panel, inputs);
  else if (view === 'system') {
    panel.innerHTML = '<p class="viz-empty">Loading 3D view…</p>';
    try {
      const mod = await import('/static/system3d.js');
      if (detailState.candidate !== c || detailState.view !== 'system') return;
      detailState.system3d = mod.mountSystem(panel, {
        selfId: c.id,
        starName: c.target_name,
        star: c.host_star || {},
        planets: c.system || [],
      });
    } catch (err) {
      panel.innerHTML = `<p class="viz-empty">The 3D view could not start (${escapeHtml(err.message)}). It needs a browser with WebGL.</p>`;
    }
  }
}

function closeModal() {
  const modal = document.getElementById('dossier-modal');
  disposeDetailViews();
  detailState.candidate = null;
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
      // Backdrop clicks target the <dialog> itself. Keyboard-activated buttons report
      // (0, 0) coordinates, so the geometry check alone would close the modal.
      if (e.target !== modal) return;
      const rect = modal.getBoundingClientRect();
      const isInDialog = (rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
        rect.left <= e.clientX && e.clientX <= rect.left + rect.width);
      if (!isInDialog) {
        closeModal();
      }
    });
    modal.addEventListener('cancel', () => {
      // Escape key pressed
      disposeDetailViews();
      detailState.candidate = null;
      if (window.location.hash.startsWith('#candidate/')) {
        history.replaceState(null, '', '#catalog');
      }
    });
  }
  // Charts read colours from CSS variables; redraw the open view when the theme flips.
  new MutationObserver(() => {
    if (detailState.candidate && detailState.view !== 'system') showDetailView(detailState.view, false);
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
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

  // datetime-local inputs hold local wall-clock time, not UTC
  const toLocalInput = (d) => {
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const now = new Date();
  const tomorrow = new Date(now.getTime() + 24 * 3600 * 1000);
  const startInput = document.getElementById('obs-start');
  const endInput = document.getElementById('obs-end');
  if (startInput) startInput.value = toLocalInput(now);
  if (endInput) endInput.value = toLocalInput(tomorrow);

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

  const val = (id, fallback) => (document.getElementById(id)?.value || fallback);
  const startVal = val('obs-start');
  const endVal = val('obs-end');
  if (!startVal || !endVal) return;

  const previous = container.querySelector('.queue-table-wrap');
  if (previous) previous.style.opacity = '0.5';
  else container.innerHTML = '<div class="queue-message">Computing transit windows for every catalogued signal…</div>';

  const params = new URLSearchParams({
    lat: val('obs-lat'),
    lon: val('obs-lon'),
    start_time: new Date(startVal).toISOString(),
    end_time: new Date(endVal).toISOString(),
    min_priority: val('obs-min-priority', '0'),
    min_altitude_deg: val('obs-min-alt', '30'),
    max_sun_altitude_deg: val('obs-twilight', '-12'),
    max_timing_uncertainty_min: val('obs-max-sigma', '120'),
    limit: '100',
  });
  const minScore = val('obs-min-score', '');
  if (minScore !== '') params.set('min_planet_score', minScore);
  const boxes = [...document.querySelectorAll('#obs-sources input[type=checkbox]')];
  const checked = boxes.filter((b) => b.checked).map((b) => b.value);
  if (checked.length === 0) {
    container.innerHTML = '<div class="queue-message">Select at least one source.</div>';
    return;
  }
  if (checked.length < boxes.length) checked.forEach((v) => params.append('source', v));

  try {
    const res = await fetch(`/v1/observations/queue?${params.toString()}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join('; ') : data.detail;
      throw new Error(detail || `HTTP ${res.status}`);
    }

    if (data.items.length === 0) {
      container.innerHTML = '<div class="queue-message">No transit is fully observable from this site in the window with these limits. Try a longer window, nautical or civil darkness, or a lower altitude limit.</div>';
      return;
    }

    const fmtTime = (iso) => {
      const d = new Date(iso);
      return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    };
    const fmtUtc = (iso) => iso.slice(11, 16);

    container.innerHTML = `
      <p class="queue-summary">${data.total.toLocaleString()} fully observable transit${data.total === 1 ? '' : 's'}${data.total > data.items.length ? `, best ${data.items.length} shown` : ''}. Times in your local time zone (UTC in grey).</p>
      <div class="queue-table-wrap">
        <table class="data-table queue-table">
          <thead>
            <tr>
              <th>Priority</th>
              <th>Planet score</th>
              <th>Signal</th>
              <th>Mid-transit</th>
              <th>Observe from → to</th>
              <th>Duration</th>
              <th>Timing ±1σ</th>
              <th>Altitude start / mid / end</th>
              <th>Sun / Moon</th>
            </tr>
          </thead>
          <tbody>
            ${data.items.map((item) => `
              <tr>
                <td><span class="score-badge">${item.priority_score.toFixed(2)}</span></td>
                <td>${renderPlanetScore(item.planet_score)}</td>
                <td>
                  <a href="#candidate/${encodeURIComponent(item.signal_id)}" class="queue-link" data-id="${escapeHtml(item.signal_id)}">
                    <strong>${escapeHtml(item.signal_name)}</strong>
                  </a><br>
                  <small class="queue-sub">${escapeHtml(item.target_name)} · ${escapeHtml(item.current_disposition.replace(/_/g, ' '))}</small><br>
                  <small class="queue-sub">${(item.source_names || []).map((n) => escapeHtml(n)).join(', ')}</small>
                </td>
                <td class="mono">${fmtTime(item.transit_mid_utc)}<br><small class="queue-sub">${fmtUtc(item.transit_mid_utc)} UTC</small></td>
                <td class="mono">${fmtTime(item.window_start_utc)}<br>→ ${fmtTime(item.window_end_utc)}</td>
                <td>${item.duration_h.toFixed(2)} h${item.duration_assumed ? ' <small class="queue-sub" title="No published duration; 2.5 h assumed">(assumed)</small>' : ''}</td>
                <td>±${item.timing_uncertainty_min.toFixed(1)} min</td>
                <td>${item.altitude_ingress_deg.toFixed(0)}° / ${item.target_altitude_deg.toFixed(0)}° / ${item.altitude_egress_deg.toFixed(0)}°<br><small class="queue-sub">airmass ${item.target_airmass.toFixed(2)} at mid</small></td>
                <td>${item.sun_altitude_deg.toFixed(0)}° / ${item.moon_separation_deg.toFixed(0)}°<br><small class="queue-sub">highest Sun · Moon distance</small></td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
      <p class="queue-sub queue-note">"Observe from → to" is the transit padded by its timing uncertainty; the whole span is above the altitude limit, in darkness and at least 30° from the Moon. One row per signal and transit, using its most precise published ephemeris.</p>
    `;

    container.querySelectorAll('.queue-link').forEach((link) => {
      link.addEventListener('click', (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
        e.preventDefault();
        const id = link.getAttribute('data-id');
        history.pushState(null, '', `#candidate/${id}`);
        openDossierModal(id);
      });
    });
  } catch (err) {
    container.innerHTML = `<div class="queue-message queue-error">Could not compute the queue: ${escapeHtml(err.message)}</div>`;
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
