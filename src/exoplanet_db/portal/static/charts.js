// Candidate detail charts: limb-darkened transit model, ephemeris timing forecast, population context.
// Plain SVG/canvas, no dependencies. Exposed as window.ExoCharts.
(function () {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const MS_PER_DAY = 86400000;
  const JD_UNIX_EPOCH = 2440587.5;

  // ---------- small utilities ----------

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function el(tag, attrs = {}, parent) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }

  function html(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function linearScale(d0, d1, r0, r1) {
    const f = (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);
    f.invert = (p) => d0 + ((p - r0) / (r1 - r0 || 1)) * (d1 - d0);
    f.domain = [d0, d1];
    return f;
  }

  function niceStep(span, count) {
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1;
    return step * mag;
  }

  function linearTicks(d0, d1, count) {
    const lo = Math.min(d0, d1);
    const hi = Math.max(d0, d1);
    const step = niceStep(hi - lo, count);
    const ticks = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
      ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    }
    return ticks;
  }

  function logTicks(lo, hi) {
    // Decades plus 2x and 5x when the span is narrow enough to need them
    const ticks = [];
    const span = Math.log10(hi) - Math.log10(lo);
    const mults = span > 4 ? [1] : span > 2 ? [1, 3] : [1, 2, 5];
    for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
      for (const m of mults) {
        const v = m * Math.pow(10, e);
        if (v >= lo && v <= hi) ticks.push(v);
      }
    }
    return ticks;
  }

  function fmtNum(v, digits = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const a = Math.abs(v);
    if (a >= 1000) return Math.round(v).toLocaleString('en-US');
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(Math.min(digits, 1));
    if (a >= 1) return v.toFixed(digits);
    if (a === 0) return '0';
    return v.toPrecision(2);
  }

  function fmtSigned(v, digits = 2) {
    const s = fmtNum(Math.abs(v), digits);
    if (Math.abs(v) < 1e-12) return s;
    return (v < 0 ? '−' : '+') + s;
  }

  function bjdToDate(bjd) {
    return new Date((bjd - JD_UNIX_EPOCH) * MS_PER_DAY);
  }

  function dateToJd(date) {
    return date.getTime() / MS_PER_DAY + JD_UNIX_EPOCH;
  }

  function fmtDateUtc(date, withTime = true) {
    const iso = date.toISOString();
    return withTime ? iso.slice(0, 16).replace('T', ' ') + ' UTC' : iso.slice(0, 10);
  }

  // Shared tooltip: one floating element per chart host
  function makeTooltip(host) {
    const tip = html('div', 'viz-tooltip');
    tip.setAttribute('role', 'status');
    tip.hidden = true;
    host.appendChild(tip);
    return {
      show(rows, x, y) {
        tip.replaceChildren();
        for (const row of rows) {
          const line = html('div', row.head ? 'viz-tooltip-head' : 'viz-tooltip-row');
          if (row.key) {
            const key = html('span', 'viz-tooltip-key');
            key.style.background = row.key;
            line.appendChild(key);
          }
          const val = html('strong', null, row.value);
          line.appendChild(val);
          if (row.label) line.appendChild(html('span', 'viz-tooltip-label', row.label));
          tip.appendChild(line);
        }
        tip.hidden = false;
        const hostRect = host.getBoundingClientRect();
        const w = tip.offsetWidth;
        const h = tip.offsetHeight;
        let left = x + 14;
        if (left + w > hostRect.width - 4) left = x - w - 14;
        let top = y - h - 10;
        if (top < 4) top = y + 14;
        tip.style.left = `${Math.max(4, left)}px`;
        tip.style.top = `${top}px`;
      },
      hide() {
        tip.hidden = true;
      },
    };
  }

  function axisGroup(svg, cls) {
    return el('g', { class: `viz-axis ${cls}` }, svg);
  }

  function chartSize(container, aspect, minH, maxH) {
    const width = Math.max(280, container.clientWidth || 640);
    const height = Math.round(Math.min(maxH, Math.max(minH, width * aspect)));
    return { width, height };
  }

  // ---------- transit model ----------

  // Quadratic limb-darkening coefficients, rough TESS-band trend with Teff.
  function limbDarkening(teff) {
    const t = teff || 5800;
    const u1 = Math.min(0.6, Math.max(0.2, 0.42 - (t - 5800) * 6e-5));
    return [u1, 0.22];
  }

  // Fraction of stellar flux blocked by a disc of radius p (stellar radii) at separation z.
  // Exact geometry, numerically integrated over limb-darkened annuli.
  function occulted(z, p, u1, u2, n = 200) {
    if (z >= 1 + p) return 0;
    // Only annuli that can intersect the planet contribute; integrate those finely.
    const r0 = Math.max(0, z - p);
    const r1 = Math.min(1, z + p);
    const dr = (r1 - r0) / n;
    let blocked = 0;
    for (let i = 0; i < n; i++) {
      const r = r0 + (i + 0.5) * dr;
      const mu = Math.sqrt(Math.max(0, 1 - r * r));
      const I = 1 - u1 * (1 - mu) - u2 * (1 - mu) * (1 - mu);
      let arc = 0;
      if (p >= z + r) arc = 2 * Math.PI * r;
      else if (z < r + p && r < z + p) {
        const c = (r * r + z * z - p * p) / (2 * r * z);
        arc = 2 * r * Math.acos(Math.max(-1, Math.min(1, c)));
      }
      blocked += I * arc * dr;
    }
    // Total flux of a quadratically limb-darkened disc of unit radius
    return blocked / (Math.PI * (1 - u1 / 3 - u2 / 6));
  }

  function solveRadiusRatio(depthFrac, b, u1, u2) {
    let lo = 1e-4;
    let hi = 1.5;
    for (let i = 0; i < 40; i++) {
      const mid = (lo + hi) / 2;
      if (occulted(b, mid, u1, u2, 120) < depthFrac) lo = mid;
      else hi = mid;
    }
    return (lo + hi) / 2;
  }

  // Build transit geometry from catalogue parameters. Returns null when depth/duration/period are missing.
  function transitGeometry(c) {
    const P = c.period_d;
    const T = c.duration_h;
    const depth = c.depth_ppm;
    if (!(P > 0 && T > 0 && depth > 0)) return null;
    const [u1, u2] = limbDarkening(c.teff_k);
    const depthFrac = depth / 1e6;
    const s = Math.sin((Math.PI * (T / 24)) / P);

    let b = 0;
    let k = solveRadiusRatio(depthFrac, 0, u1, u2);
    let aR = (1 + k) / s;
    let basis = 'central transit assumed (b = 0); a/R★ inferred from duration';

    if (c.star_radius_rsun > 0 && c.a_au > 0) {
      const aRstar = c.a_au / (c.star_radius_rsun * 0.00465047);
      let nearCentral = false;
      for (let i = 0; i < 3; i++) {
        const b2 = ((1 + k) ** 2 - (s * aRstar) ** 2) / (1 - s * s);
        if (!(b2 >= 0)) {
          // Slightly too long for the catalogue star: within errors of a central transit.
          nearCentral = b2 > -0.15 * (1 + k) ** 2;
          b = null;
          break;
        }
        b = Math.sqrt(b2);
        k = solveRadiusRatio(depthFrac, b, u1, u2);
      }
      if (nearCentral) {
        b = 0;
        k = solveRadiusRatio(depthFrac, 0, u1, u2);
        basis = `near-central transit (b ≈ 0; catalogue a/R★ ≈ ${aRstar.toFixed(1)})`;
      } else if (b !== null && b < 1 + k) {
        aR = aRstar;
        basis = `impact parameter b ≈ ${b.toFixed(2)} from duration and a/R★ ≈ ${aRstar.toFixed(1)}`;
      } else {
        b = 0;
        k = solveRadiusRatio(depthFrac, 0, u1, u2);
        basis = 'duration is inconsistent with a circular orbit around the catalogue star; b = 0 assumed';
      }
    }
    const cosI = b / aR;
    return { P, T, depth, depthFrac, k, b, aR, cosI, u1, u2, basis };
  }

  function separationAt(g, tHours) {
    const phase = (2 * Math.PI * (tHours / 24)) / g.P;
    const x = Math.sin(phase);
    const y = g.cosI * Math.cos(phase);
    return g.aR * Math.sqrt(x * x + y * y);
  }

  function contactTimes(g) {
    // Times (h) at which z = 1 + k (T1/T4) and z = 1 - k (T2/T3); solved by bisection on the ingress side.
    const solveFor = (zTarget) => {
      let lo = 0;
      let hi = (g.P * 24) / 4;
      if (separationAt(g, 0) > zTarget) return null;
      for (let i = 0; i < 60; i++) {
        const mid = (lo + hi) / 2;
        if (separationAt(g, mid) < zTarget) lo = mid;
        else hi = mid;
      }
      return (lo + hi) / 2;
    };
    return { outer: solveFor(1 + g.k), inner: g.b < 1 - g.k ? solveFor(1 - g.k) : null };
  }

  function renderTransit(container, c) {
    container.replaceChildren();
    const g = transitGeometry(c);
    if (!g) {
      container.appendChild(
        html('p', 'viz-empty', 'Period, duration and depth are all needed to draw a transit model; this record is missing at least one.')
      );
      return;
    }
    const contacts = contactTimes(g);
    const half = Math.max(contacts.outer || g.T / 2, 0.25);
    const span = half * 2.0;

    const n = 361;
    const samples = [];
    for (let i = 0; i < n; i++) {
      const t = -span + (2 * span * i) / (n - 1);
      const deficit = occulted(separationAt(g, t), g.k, g.u1, g.u2);
      samples.push({ t, ppm: -deficit * 1e6 });
    }
    const modelDepth = Math.max(...samples.map((s) => -s.ppm));
    const scale = g.depth / modelDepth;
    for (const s of samples) s.ppm *= scale;
    const errFrac = c.depth_err_ppm > 0 ? c.depth_err_ppm / g.depth : 0;

    let units = 'hours';
    const host = html('div', 'viz-host');
    container.appendChild(host);

    const controls = html('div', 'viz-controls');
    const unitToggle = html('div', 'segmented');
    for (const [key, label] of [
      ['hours', 'Hours'],
      ['phase', 'Phase'],
    ]) {
      const btn = html('button', key === units ? 'active' : '', label);
      btn.type = 'button';
      btn.addEventListener('click', () => {
        units = key;
        unitToggle.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
        draw();
      });
      unitToggle.appendChild(btn);
    }
    controls.appendChild(html('span', 'viz-controls-label', 'X axis'));
    controls.appendChild(unitToggle);
    host.before(controls);

    const caption = html('p', 'viz-caption');
    caption.textContent =
      `Model, not observed photometry: quadratic limb darkening (u₁ ${g.u1.toFixed(2)}, u₂ ${g.u2.toFixed(2)}), ` +
      `Rp/R★ ${g.k.toFixed(4)}, ${g.basis}. Scaled to the catalogue depth and duration` +
      (errFrac ? '; shaded band is ±1σ depth.' : '.');
    container.appendChild(caption);

    const table = buildTable(
      ['Point', 'Time from mid-transit', 'Relative flux'],
      [
        contacts.outer !== null ? ['T1 first contact', `${fmtSigned(-contacts.outer)} h`, '0 ppm'] : null,
        contacts.inner !== null ? ['T2 second contact', `${fmtSigned(-contacts.inner)} h`, '—'] : null,
        ['Mid-transit', '0 h', `${fmtSigned(-g.depth, 0)} ppm`],
        contacts.inner !== null ? ['T3 third contact', `${fmtSigned(contacts.inner)} h`, '—'] : null,
        contacts.outer !== null ? ['T4 last contact', `${fmtSigned(contacts.outer)} h`, '0 ppm'] : null,
      ].filter(Boolean)
    );
    container.appendChild(tableToggle(table));

    function xValue(t) {
      return units === 'hours' ? t : t / 24 / g.P;
    }

    function draw() {
      host.replaceChildren();
      const { width, height } = chartSize(host, 0.42, 220, 340);
      const longestTick = Math.max(...linearTicks(-g.depth * (1 + errFrac) * 1.12, g.depth * 0.12, 5).map((v) => fmtSigned(v, 0).length));
      const m = { top: 22, right: 18, bottom: 42, left: 34 + longestTick * 7 };
      const x = linearScale(xValue(-span), xValue(span), m.left, width - m.right);
      const yMin = -g.depth * (1 + errFrac) * 1.12;
      const yMax = g.depth * 0.12;
      const y = linearScale(yMin, yMax, height - m.bottom, m.top);
      const svg = el('svg', { viewBox: `0 0 ${width} ${height}`, width, height, role: 'img' }, host);
      svg.setAttribute('aria-label', `Transit model, depth ${Math.round(g.depth)} ppm, duration ${g.T.toFixed(2)} h`);

      const grid = el('g', { class: 'viz-grid' }, svg);
      const yAxis = axisGroup(svg, 'y');
      for (const v of linearTicks(yMin, yMax, 5)) {
        el('line', { x1: m.left, x2: width - m.right, y1: y(v), y2: y(v) }, grid);
        const t = el('text', { x: m.left - 8, y: y(v), dy: '0.32em', 'text-anchor': 'end' }, yAxis);
        t.textContent = fmtSigned(v, 0);
      }
      const yl = el('text', { transform: `translate(14 ${(m.top + height - m.bottom) / 2}) rotate(-90)`, 'text-anchor': 'middle', class: 'viz-axis-title' }, yAxis);
      yl.textContent = 'Relative flux (ppm)';

      const xAxis = axisGroup(svg, 'x');
      for (const v of linearTicks(x.domain[0], x.domain[1], Math.max(4, Math.floor(width / 110)))) {
        el('line', { x1: x(v), x2: x(v), y1: height - m.bottom, y2: height - m.bottom + 4 }, xAxis);
        const t = el('text', { x: x(v), y: height - m.bottom + 17, 'text-anchor': 'middle' }, xAxis);
        t.textContent = units === 'hours' ? fmtSigned(v, 1) : fmtSigned(v, 3);
      }
      el('line', { x1: m.left, x2: width - m.right, y1: height - m.bottom, y2: height - m.bottom, class: 'viz-baseline' }, xAxis);
      const xl = el('text', { x: (m.left + width - m.right) / 2, y: height - 6, 'text-anchor': 'middle', class: 'viz-axis-title' }, xAxis);
      xl.textContent = units === 'hours' ? 'Time from mid-transit (hours)' : 'Orbital phase';

      // contact markers, selectively labelled
      const marks = el('g', { class: 'viz-contacts' }, svg);
      const contactList = [];
      if (contacts.outer !== null) contactList.push(['T1', -contacts.outer], ['T4', contacts.outer]);
      if (contacts.inner !== null) contactList.push(['T2', -contacts.inner], ['T3', contacts.inner]);
      for (const [, t] of contactList) {
        el('line', { x1: x(xValue(t)), x2: x(xValue(t)), y1: m.top, y2: height - m.bottom }, marks);
      }
      // Label each side once; ingress/egress pairs merge when they sit too close to read apart
      const sides = [
        contactList.filter(([l]) => l === 'T1' || l === 'T2'),
        contactList.filter(([l]) => l === 'T3' || l === 'T4'),
      ];
      for (const side of sides) {
        if (!side.length) continue;
        const xs = side.map(([, t]) => x(xValue(t)));
        const close = xs.length === 2 && Math.abs(xs[0] - xs[1]) < 26;
        const items = close ? [[side.map(([l]) => l).join('–'), (xs[0] + xs[1]) / 2]] : side.map(([l], i) => [l, xs[i]]);
        for (const [label, px] of items) {
          const lt = el('text', { x: px, y: m.top - 6, 'text-anchor': 'middle' }, marks);
          lt.textContent = label;
        }
      }

      if (errFrac) {
        const upper = samples.map((s) => `${x(xValue(s.t)).toFixed(1)},${y(s.ppm * (1 - errFrac)).toFixed(1)}`);
        const lower = samples.map((s) => `${x(xValue(s.t)).toFixed(1)},${y(s.ppm * (1 + errFrac)).toFixed(1)}`).reverse();
        el('polygon', { points: upper.concat(lower).join(' '), class: 'viz-band' }, svg);
      }
      el('path', {
        d: 'M' + samples.map((s) => `${x(xValue(s.t)).toFixed(1)},${y(s.ppm).toFixed(1)}`).join('L'),
        class: 'viz-line series-1',
      }, svg);

      // crosshair + tooltip
      const cross = el('line', { class: 'viz-crosshair', y1: m.top, y2: height - m.bottom, visibility: 'hidden' }, svg);
      const dot = el('circle', { r: 5, class: 'viz-dot series-1', visibility: 'hidden' }, svg);
      const tip = makeTooltip(host);
      const hit = el('rect', { x: m.left, y: m.top, width: width - m.left - m.right, height: height - m.top - m.bottom, fill: 'transparent', tabindex: 0 }, svg);

      const stage = (t) => {
        const a = Math.abs(t);
        if (contacts.outer === null || a >= contacts.outer) return 'out of transit';
        if (contacts.inner !== null && a <= contacts.inner) return 'full transit';
        return t < 0 ? 'ingress' : 'egress';
      };
      const showAt = (idx) => {
        const s = samples[idx];
        const px = x(xValue(s.t));
        const py = y(s.ppm);
        cross.setAttribute('x1', px);
        cross.setAttribute('x2', px);
        cross.setAttribute('visibility', 'visible');
        dot.setAttribute('cx', px);
        dot.setAttribute('cy', py);
        dot.setAttribute('visibility', 'visible');
        const rows = [
          { head: true, value: `${fmtSigned(s.t, 2)} h`, label: `phase ${fmtSigned(s.t / 24 / g.P, 4)}` },
          { key: cssVar('--viz-series-1', '#3987e5'), value: `${fmtSigned(s.ppm, 0)} ppm`, label: stage(s.t) },
        ];
        if (errFrac) rows.push({ value: `±${fmtNum(-s.ppm * errFrac, 0)} ppm`, label: '1σ depth' });
        tip.show(rows, px, py);
      };
      let focusIdx = Math.floor(n / 2);
      hit.addEventListener('pointermove', (e) => {
        const rect = svg.getBoundingClientRect();
        const px = ((e.clientX - rect.left) / rect.width) * width;
        const t = units === 'hours' ? x.invert(px) : x.invert(px) * 24 * g.P;
        focusIdx = Math.max(0, Math.min(n - 1, Math.round(((t + span) / (2 * span)) * (n - 1))));
        showAt(focusIdx);
      });
      hit.addEventListener('pointerleave', () => {
        cross.setAttribute('visibility', 'hidden');
        dot.setAttribute('visibility', 'hidden');
        tip.hide();
      });
      hit.addEventListener('focus', () => showAt(focusIdx));
      hit.addEventListener('blur', () => tip.hide());
      hit.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
          focusIdx = Math.max(0, Math.min(n - 1, focusIdx + (e.key === 'ArrowRight' ? 3 : -3)));
          showAt(focusIdx);
          e.preventDefault();
        }
      });
    }

    draw();
    observeResize(host, draw);
  }

  // ---------- timing forecast ----------

  function renderTiming(container, c) {
    container.replaceChildren();
    const P = c.period_d;
    const T0 = c.epoch_bjd;
    if (!(P > 0 && T0 > 2400000)) {
      container.appendChild(html('p', 'viz-empty', 'No transit ephemeris (period and mid-transit epoch) is published for this signal.'));
      return;
    }
    const sT0 = c.epoch_err_d || 0;
    const sP = c.period_err_d || 0;
    const sigmaMin = (N) => Math.sqrt(sT0 * sT0 + N * N * sP * sP) * 1440;
    const hasErrors = sT0 > 0 || sP > 0;

    const nowJd = dateToJd(new Date());
    const nNow = Math.ceil((nowJd - T0) / P);
    const horizonJd = nowJd + 3 * 365.25;
    const startJd = Math.min(T0, nowJd);
    const nStart = Math.floor((startJd - T0) / P);
    const nEnd = Math.ceil((horizonJd - T0) / P);
    const stride = Math.max(1, Math.ceil((nEnd - nStart) / 600));
    const samples = [];
    for (let N = nStart; N <= nEnd; N += stride) samples.push({ N, jd: T0 + N * P, sigma: sigmaMin(N) });

    const halfDurMin = c.duration_h > 0 ? (c.duration_h * 60) / 2 : null;
    const host = html('div', 'viz-host');
    container.appendChild(host);

    const caption = html('p', 'viz-caption');
    caption.textContent = hasErrors
      ? 'Predicted 1σ uncertainty of each mid-transit time, σ(N)² = σ(T₀)² + N²σ(P)², propagated from the published ephemeris (epoch–period covariance not applied). Once σ passes half the transit duration a scheduled observation can miss the event.'
      : 'This record publishes no period or epoch uncertainty, so the timing forecast is flat at zero. Treat predicted times as unverified.';
    container.appendChild(caption);

    const upcoming = [];
    for (let N = Math.max(nNow, 0); upcoming.length < 8; N++) {
      upcoming.push([
        `${N}`,
        fmtDateUtc(bjdToDate(T0 + N * P)),
        (T0 + N * P).toFixed(4),
        hasErrors ? `±${fmtNum(sigmaMin(N), 1)} min` : '—',
      ]);
    }
    const nextTitle = html('h5', 'viz-subtitle', 'Next transits (geocentric, ±8 min barycentric correction not applied; use the Observation Queue for site-specific windows)');
    container.appendChild(nextTitle);
    container.appendChild(buildTable(['Epoch N', 'Mid-transit (≈UTC)', 'BJD_TDB', '1σ'], upcoming));

    function draw() {
      host.replaceChildren();
      const { width, height } = chartSize(host, 0.36, 200, 300);
      const m = { top: 18, right: 18, bottom: 42, left: 64 };
      const x = linearScale(samples[0].jd, samples[samples.length - 1].jd, m.left, width - m.right);
      const yTop = Math.max(...samples.map((s) => s.sigma), halfDurMin && hasErrors ? Math.min(halfDurMin * 1.15, Math.max(...samples.map((s) => s.sigma)) * 1.5) : 0, 1) * 1.08;
      const y = linearScale(0, yTop, height - m.bottom, m.top);
      const svg = el('svg', { viewBox: `0 0 ${width} ${height}`, width, height, role: 'img' }, host);
      svg.setAttribute('aria-label', 'Mid-transit timing uncertainty forecast');

      const grid = el('g', { class: 'viz-grid' }, svg);
      const yAxis = axisGroup(svg, 'y');
      for (const v of linearTicks(0, yTop, 5)) {
        el('line', { x1: m.left, x2: width - m.right, y1: y(v), y2: y(v) }, grid);
        const t = el('text', { x: m.left - 8, y: y(v), dy: '0.32em', 'text-anchor': 'end' }, yAxis);
        t.textContent = fmtNum(v, 1);
      }
      const yl = el('text', { transform: `translate(14 ${(m.top + height - m.bottom) / 2}) rotate(-90)`, 'text-anchor': 'middle', class: 'viz-axis-title' }, yAxis);
      yl.textContent = '1σ mid-transit (min)';

      const xAxis = axisGroup(svg, 'x');
      const d0 = bjdToDate(samples[0].jd);
      const d1 = bjdToDate(samples[samples.length - 1].jd);
      const years = (d1 - d0) / (365.25 * MS_PER_DAY);
      const tickDates = [];
      if (years > 3) {
        const yStep = Math.max(1, Math.round(niceStep(years, Math.max(3, Math.floor(width / 120)))));
        for (let yr = Math.ceil(d0.getUTCFullYear() / yStep) * yStep; yr <= d1.getUTCFullYear(); yr += yStep) {
          tickDates.push(new Date(Date.UTC(yr, 0, 1)));
        }
      } else {
        const mStep = years > 1.5 ? 6 : 3;
        const cur = new Date(Date.UTC(d0.getUTCFullYear(), Math.ceil(d0.getUTCMonth() / mStep) * mStep, 1));
        while (cur <= d1) {
          tickDates.push(new Date(cur));
          cur.setUTCMonth(cur.getUTCMonth() + mStep);
        }
      }
      for (const d of tickDates) {
        const px = x(dateToJd(d));
        if (px < m.left || px > width - m.right) continue;
        el('line', { x1: px, x2: px, y1: height - m.bottom, y2: height - m.bottom + 4 }, xAxis);
        const t = el('text', { x: px, y: height - m.bottom + 17, 'text-anchor': 'middle' }, xAxis);
        t.textContent = years > 3 ? `${d.getUTCFullYear()}` : d.toISOString().slice(0, 7);
      }
      el('line', { x1: m.left, x2: width - m.right, y1: height - m.bottom, y2: height - m.bottom, class: 'viz-baseline' }, xAxis);

      if (halfDurMin && hasErrors && halfDurMin <= yTop) {
        const ref = el('g', { class: 'viz-reference' }, svg);
        el('line', { x1: m.left, x2: width - m.right, y1: y(halfDurMin), y2: y(halfDurMin) }, ref);
        const t = el('text', { x: width - m.right, y: y(halfDurMin) - 6, 'text-anchor': 'end' }, ref);
        t.textContent = `half transit duration (${fmtNum(halfDurMin, 0)} min)`;
      }
      const nowPx = x(nowJd);
      if (nowPx >= m.left && nowPx <= width - m.right) {
        const nowG = el('g', { class: 'viz-now' }, svg);
        el('line', { x1: nowPx, x2: nowPx, y1: m.top, y2: height - m.bottom }, nowG);
        const t = el('text', { x: nowPx + 5, y: m.top + 10 }, nowG);
        t.textContent = 'today';
      }
      el('path', {
        d: 'M' + samples.map((s) => `${x(s.jd).toFixed(1)},${y(s.sigma).toFixed(1)}`).join('L'),
        class: 'viz-line series-1',
      }, svg);

      const cross = el('line', { class: 'viz-crosshair', y1: m.top, y2: height - m.bottom, visibility: 'hidden' }, svg);
      const dot = el('circle', { r: 5, class: 'viz-dot series-1', visibility: 'hidden' }, svg);
      const tip = makeTooltip(host);
      const hit = el('rect', { x: m.left, y: m.top, width: width - m.left - m.right, height: height - m.top - m.bottom, fill: 'transparent', tabindex: 0 }, svg);
      let focusIdx = samples.findIndex((s) => s.jd >= nowJd);
      if (focusIdx < 0) focusIdx = samples.length - 1;
      const showAt = (idx) => {
        const s = samples[idx];
        const px = x(s.jd);
        const py = y(s.sigma);
        cross.setAttribute('x1', px);
        cross.setAttribute('x2', px);
        cross.setAttribute('visibility', 'visible');
        dot.setAttribute('cx', px);
        dot.setAttribute('cy', py);
        dot.setAttribute('visibility', 'visible');
        tip.show(
          [
            { head: true, value: fmtDateUtc(bjdToDate(s.jd)), label: `epoch N = ${s.N}` },
            { key: cssVar('--viz-series-1', '#3987e5'), value: hasErrors ? `±${fmtNum(s.sigma, 2)} min` : 'no uncertainty published', label: '1σ mid-transit' },
            { value: s.jd.toFixed(4), label: 'BJD_TDB' },
          ],
          px,
          py
        );
      };
      hit.addEventListener('pointermove', (e) => {
        const rect = svg.getBoundingClientRect();
        const jd = x.invert(((e.clientX - rect.left) / rect.width) * width);
        let best = 0;
        let bestD = Infinity;
        for (let i = 0; i < samples.length; i++) {
          const d = Math.abs(samples[i].jd - jd);
          if (d < bestD) {
            bestD = d;
            best = i;
          }
        }
        focusIdx = best;
        showAt(best);
      });
      hit.addEventListener('pointerleave', () => {
        cross.setAttribute('visibility', 'hidden');
        dot.setAttribute('visibility', 'hidden');
        tip.hide();
      });
      hit.addEventListener('focus', () => showAt(focusIdx));
      hit.addEventListener('blur', () => tip.hide());
      hit.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
          focusIdx = Math.max(0, Math.min(samples.length - 1, focusIdx + (e.key === 'ArrowRight' ? 1 : -1)));
          showAt(focusIdx);
          e.preventDefault();
        }
      });
    }

    draw();
    observeResize(host, draw);
  }

  // ---------- population context ----------

  let populationPromise = null;
  function loadPopulation() {
    if (!populationPromise) {
      populationPromise = fetch('/v1/population')
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json();
        })
        .catch((err) => {
          populationPromise = null;
          throw err;
        });
    }
    return populationPromise;
  }

  const CANDIDATE_DISPOSITIONS = new Set(['planet_candidate', 'unclassified_transit_like_signal']);

  async function renderPopulation(container, c) {
    container.replaceChildren(html('p', 'viz-empty', 'Loading catalogue…'));
    let pop;
    try {
      pop = await loadPopulation();
    } catch (err) {
      container.replaceChildren(html('p', 'viz-empty', `Could not load the catalogue population (${err.message}).`));
      return;
    }
    container.replaceChildren();
    const systemIds = new Set((c.system || []).map((p) => p.id));
    let yMode = c.radius_earth ? 'radius' : 'depth';

    const controls = html('div', 'viz-controls');
    const toggle = html('div', 'segmented');
    for (const [key, label] of [
      ['radius', 'Planet radius'],
      ['depth', 'Transit depth'],
    ]) {
      const btn = html('button', key === yMode ? 'active' : '', label);
      btn.type = 'button';
      btn.addEventListener('click', () => {
        yMode = key;
        toggle.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
        draw();
      });
      toggle.appendChild(btn);
    }
    controls.appendChild(html('span', 'viz-controls-label', 'Y axis'));
    controls.appendChild(toggle);
    container.appendChild(controls);

    const legend = html('div', 'viz-legend');
    container.appendChild(legend);
    const host = html('div', 'viz-host');
    container.appendChild(host);
    const caption = html('p', 'viz-caption');
    container.appendChild(caption);

    function points() {
      const ys = yMode === 'radius' ? pop.radius_earth : pop.depth_ppm;
      const out = [];
      for (let i = 0; i < pop.id.length; i++) {
        const yv = ys[i];
        const xv = pop.period_d[i];
        if (!(yv > 0 && xv > 0)) continue;
        const id = pop.id[i];
        const kind = id === c.id ? 'self' : systemIds.has(id) ? 'system' : CANDIDATE_DISPOSITIONS.has(pop.disposition[i]) ? 'candidate' : 'catalogue';
        out.push({ id, name: pop.name[i], disposition: pop.disposition[i], x: xv, y: yv, kind });
      }
      // Draw order: catalogue, candidates, this system, this signal
      const rank = { catalogue: 0, candidate: 1, system: 2, self: 3 };
      out.sort((a, b) => rank[a.kind] - rank[b.kind]);
      return out;
    }

    function draw() {
      host.replaceChildren();
      const pts = points();
      const counts = { catalogue: 0, candidate: 0, system: 0, self: 0 };
      for (const p of pts) counts[p.kind]++;
      const skipped = pop.id.length - pts.length;

      legend.replaceChildren();
      const series = [
        ['catalogue', '--viz-muted', `Confirmed / other (${counts.catalogue.toLocaleString('en-US')})`],
        ['candidate', '--viz-series-2', `Candidates (${counts.candidate.toLocaleString('en-US')})`],
        ['system', '--viz-series-1', 'Same system'],
        ['self', '--viz-series-1', c.display_name],
      ];
      for (const [kind, token, label] of series) {
        if (kind === 'system' && counts.system === 0) continue;
        const item = html('span', `viz-legend-item legend-${kind}`);
        const sw = html('span', 'viz-legend-swatch');
        sw.style.background = cssVar(token, '#888');
        item.appendChild(sw);
        item.appendChild(document.createTextNode(label));
        legend.appendChild(item);
      }
      caption.textContent =
        (yMode === 'radius'
          ? 'Radius is the published value where the catalogue has one, otherwise R★·√depth (undiluted, non-grazing). '
          : '') +
        `${skipped.toLocaleString('en-US')} signals without ${yMode === 'radius' ? 'a radius' : 'a depth'} or period are not plotted.` +
        (counts.self ? '' : ` ${c.display_name} has no ${yMode === 'radius' ? 'radius' : 'depth'} and is not shown.`) +
        ' Click a point to open it.';

      if (!pts.length) {
        host.appendChild(html('p', 'viz-empty', 'Nothing to plot.'));
        return;
      }

      const { width, height } = chartSize(host, 0.55, 260, 460);
      const m = { top: 14, right: 18, bottom: 42, left: 64 };
      const xs = pts.map((p) => p.x);
      const ys = pts.map((p) => p.y);
      const pad = (lo, hi) => [Math.pow(10, Math.floor(Math.log10(lo) * 4) / 4), Math.pow(10, Math.ceil(Math.log10(hi) * 4) / 4)];
      const [x0, x1] = pad(Math.max(0.1, Math.min(...xs)), Math.min(1e5, Math.max(...xs)));
      const [y0, y1] = pad(Math.max(yMode === 'radius' ? 0.1 : 1, Math.min(...ys)), Math.max(...ys));
      const lx = linearScale(Math.log10(x0), Math.log10(x1), m.left, width - m.right);
      const ly = linearScale(Math.log10(y0), Math.log10(y1), height - m.bottom, m.top);
      const X = (v) => lx(Math.log10(v));
      const Y = (v) => ly(Math.log10(v));

      const dpr = window.devicePixelRatio || 1;
      const canvas = document.createElement('canvas');
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      canvas.className = 'viz-canvas';
      host.appendChild(canvas);
      const svg = el('svg', { viewBox: `0 0 ${width} ${height}`, width, height, class: 'viz-overlay', role: 'img' }, host);
      svg.setAttribute('aria-label', `Catalogue period versus ${yMode}, ${c.display_name} highlighted`);

      const grid = el('g', { class: 'viz-grid' }, svg);
      const xAxis = axisGroup(svg, 'x');
      for (const v of logTicks(x0, x1)) {
        el('line', { x1: X(v), x2: X(v), y1: m.top, y2: height - m.bottom }, grid);
        const t = el('text', { x: X(v), y: height - m.bottom + 17, 'text-anchor': 'middle' }, xAxis);
        t.textContent = fmtNum(v, 1);
      }
      const xl = el('text', { x: (m.left + width - m.right) / 2, y: height - 6, 'text-anchor': 'middle', class: 'viz-axis-title' }, xAxis);
      xl.textContent = 'Orbital period (days, log)';
      const yAxis = axisGroup(svg, 'y');
      for (const v of logTicks(y0, y1)) {
        el('line', { x1: m.left, x2: width - m.right, y1: Y(v), y2: Y(v) }, grid);
        const t = el('text', { x: m.left - 8, y: Y(v), dy: '0.32em', 'text-anchor': 'end' }, yAxis);
        t.textContent = fmtNum(v, 1);
      }
      const yl = el('text', { transform: `translate(14 ${(m.top + height - m.bottom) / 2}) rotate(-90)`, 'text-anchor': 'middle', class: 'viz-axis-title' }, yAxis);
      yl.textContent = yMode === 'radius' ? 'Planet radius (R⊕, log)' : 'Transit depth (ppm, log)';

      const ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);
      const colors = {
        catalogue: cssVar('--viz-muted', '#64748b'),
        candidate: cssVar('--viz-series-2', '#d95926'),
        system: cssVar('--viz-series-1', '#3987e5'),
        self: cssVar('--viz-series-1', '#3987e5'),
      };
      const surface = cssVar('--viz-surface', '#182235');
      for (const p of pts) {
        p.px = X(p.x);
        p.py = Y(p.y);
        if (p.kind === 'catalogue' || p.kind === 'candidate') {
          ctx.globalAlpha = p.kind === 'catalogue' ? 0.45 : 0.85;
          ctx.fillStyle = colors[p.kind];
          ctx.beginPath();
          ctx.arc(p.px, p.py, p.kind === 'catalogue' ? 2 : 2.6, 0, Math.PI * 2);
          ctx.fill();
        } else {
          ctx.globalAlpha = 1;
          const r = p.kind === 'self' ? 7 : 4.5;
          ctx.fillStyle = surface;
          ctx.beginPath();
          ctx.arc(p.px, p.py, r + 2, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = colors[p.kind];
          ctx.beginPath();
          ctx.arc(p.px, p.py, r, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      ctx.globalAlpha = 1;
      const self = pts.find((p) => p.kind === 'self');
      if (self) {
        const label = el('text', { x: self.px + 11, y: self.py - 9, class: 'viz-direct-label' }, svg);
        label.textContent = c.display_name;
        if (self.px > width - 160) {
          label.setAttribute('x', self.px - 11);
          label.setAttribute('text-anchor', 'end');
        }
      }

      // nearest-point hover via a coarse grid
      const cell = 16;
      const buckets = new Map();
      pts.forEach((p, i) => {
        const key = `${Math.floor(p.px / cell)},${Math.floor(p.py / cell)}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(i);
      });
      const nearest = (px, py) => {
        const cx = Math.floor(px / cell);
        const cy = Math.floor(py / cell);
        let best = null;
        let bestD = 14 * 14;
        for (let dx = -1; dx <= 1; dx++) {
          for (let dy = -1; dy <= 1; dy++) {
            for (const i of buckets.get(`${cx + dx},${cy + dy}`) || []) {
              const p = pts[i];
              const d = (p.px - px) ** 2 + (p.py - py) ** 2 - (p.kind === 'self' || p.kind === 'system' ? 40 : 0);
              if (d < bestD) {
                bestD = d;
                best = p;
              }
            }
          }
        }
        return best;
      };
      const ring = el('circle', { r: 8, class: 'viz-hover-ring', visibility: 'hidden' }, svg);
      const tip = makeTooltip(host);
      let hovered = null;
      svg.addEventListener('pointermove', (e) => {
        const rect = svg.getBoundingClientRect();
        const px = ((e.clientX - rect.left) / rect.width) * width;
        const py = ((e.clientY - rect.top) / rect.height) * height;
        hovered = nearest(px, py);
        if (!hovered) {
          ring.setAttribute('visibility', 'hidden');
          svg.style.cursor = '';
          tip.hide();
          return;
        }
        ring.setAttribute('cx', hovered.px);
        ring.setAttribute('cy', hovered.py);
        ring.setAttribute('visibility', 'visible');
        svg.style.cursor = 'pointer';
        tip.show(
          [
            { head: true, value: hovered.name, label: hovered.disposition.replace(/_/g, ' ') },
            { value: `${fmtNum(hovered.x, 3)} d`, label: 'period' },
            { key: colors[hovered.kind], value: yMode === 'radius' ? `${fmtNum(hovered.y, 2)} R⊕` : `${fmtNum(hovered.y, 0)} ppm`, label: yMode },
          ],
          hovered.px,
          hovered.py
        );
      });
      svg.addEventListener('pointerleave', () => {
        ring.setAttribute('visibility', 'hidden');
        tip.hide();
        hovered = null;
      });
      svg.addEventListener('click', () => {
        if (hovered && hovered.id !== c.id) window.location.hash = `#candidate/${encodeURIComponent(hovered.id)}?view=population`;
      });
    }

    draw();
    observeResize(host, draw);
  }

  // ---------- tables & resize ----------

  function buildTable(headers, rows) {
    const table = html('table', 'viz-table');
    const thead = html('thead');
    const tr = html('tr');
    for (const h of headers) tr.appendChild(html('th', null, h));
    thead.appendChild(tr);
    table.appendChild(thead);
    const tbody = html('tbody');
    for (const row of rows) {
      const r = html('tr');
      for (const cell of row) r.appendChild(html('td', null, cell));
      tbody.appendChild(r);
    }
    table.appendChild(tbody);
    return table;
  }

  function tableToggle(table) {
    const details = html('details', 'viz-table-toggle');
    details.appendChild(html('summary', null, 'Table view'));
    details.appendChild(table);
    return details;
  }

  const observers = new WeakMap();
  function observeResize(host, draw) {
    let lastWidth = host.clientWidth;
    let frame = 0;
    const ro = new ResizeObserver(() => {
      if (Math.abs(host.clientWidth - lastWidth) < 2) return;
      lastWidth = host.clientWidth;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(draw);
    });
    ro.observe(host);
    observers.set(host, ro);
  }

  window.ExoCharts = {
    renderTransit,
    renderTiming,
    renderPopulation,
    bjdToDate,
    dateToJd,
    fmtNum,
    _internal: { occulted, transitGeometry, contactTimes, solveRadiusRatio },
  };
})();
