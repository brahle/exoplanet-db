// Three.js orbital view of a candidate's host system. Loaded lazily from portal.js via import().
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';

const AU_PER_RSUN = 0.00465047;
const AU_PER_REARTH = 4.26352e-5;
const JD_UNIX_EPOCH = 2440587.5;
const MS_PER_DAY = 86400000;
const SCENE_RADIUS = 10; // outermost orbit is drawn at this many scene units
const SPEED_MIN_EXP = -2; // 0.01 d/s
const SPEED_MAX_EXP = 3; // 1000 d/s

function nowJd() {
  return Date.now() / MS_PER_DAY + JD_UNIX_EPOCH;
}

function jdToIso(jd) {
  return new Date((jd - JD_UNIX_EPOCH) * MS_PER_DAY).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// Approximate blackbody colour (Tanner Helland fit), Teff in K.
function blackbodyColor(teff) {
  const t = Math.max(1000, Math.min(40000, teff || 5772)) / 100;
  let r;
  let g;
  let b;
  if (t <= 66) {
    r = 255;
    g = 99.4708025861 * Math.log(t) - 161.1195681661;
    b = t <= 19 ? 0 : 138.5177312231 * Math.log(t - 10) - 305.0447927307;
  } else {
    r = 329.698727446 * Math.pow(t - 60, -0.1332047592);
    g = 288.1221695283 * Math.pow(t - 60, -0.0755148492);
    b = 255;
  }
  const c = (v) => Math.max(0, Math.min(255, v)) / 255;
  return new THREE.Color(c(r), c(g), c(b));
}

function hashPhase(id) {
  let h = 2166136261;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 10000) / 10000;
}

function radialTexture(stops) {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d');
  const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  for (const [at, color] of stops) grad.addColorStop(at, color);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function ringTexture() {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.strokeStyle = '#ffffff';
  ctx.lineWidth = 7;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 6, 0, Math.PI * 2);
  ctx.stroke();
  return new THREE.CanvasTexture(canvas);
}

function niceStep(raw) {
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  return (n >= 5 ? 5 : n >= 2 ? 2 : 1) * mag;
}

function fmtAu(v) {
  return v >= 1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(3) : v.toPrecision(2);
}

function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

/**
 * Mount the system view.
 * data = { selfId, starName, star: {radius_rsun, mass_msun, teff_k}, planets: [PlanetPhysics] }
 * Returns { dispose() }.
 */
export function mountSystem(container, data) {
  container.replaceChildren();
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const starRadiusKnown = data.star.radius_rsun > 0;
  const starRadiusAu = (starRadiusKnown ? data.star.radius_rsun : 1) * AU_PER_RSUN;
  const teffKnown = data.star.teff_k > 0;

  const orbiting = data.planets.filter((p) => p.period_d > 0 && p.semi_major_axis_au > 0);
  const missing = data.planets.filter((p) => !(p.period_d > 0 && p.semi_major_axis_au > 0));
  if (!orbiting.length) {
    container.appendChild(el('p', 'viz-empty', 'No signal on this host has an orbital period, so there is no orbit to draw.'));
    return { dispose() {} };
  }

  const maxA = Math.max(...orbiting.map((p) => p.semi_major_axis_au));
  const auToScene = SCENE_RADIUS / maxA;

  // Planet size exaggeration that keeps the largest planet within ~35% of the tightest gap.
  const sortedA = orbiting.map((p) => p.semi_major_axis_au).sort((a, b) => a - b);
  let minGap = sortedA[0] - starRadiusAu;
  for (let i = 1; i < sortedA.length; i++) minGap = Math.min(minGap, sortedA[i] - sortedA[i - 1] || minGap);
  const maxRpAu = Math.max(...orbiting.map((p) => (p.radius_earth || 1) * AU_PER_REARTH));
  const autoScale = Math.max(1, Math.min(3000, (0.35 * Math.max(minGap, maxA * 0.02)) / maxRpAu));

  // ----- DOM scaffold -----
  const root = el('div', 'system3d');
  const stage = el('div', 'system3d-stage');
  const hud = el('div', 'system3d-hud');
  const clock = el('div', 'system3d-clock');
  hud.appendChild(clock);
  stage.appendChild(hud);
  root.appendChild(stage);

  const controlsBar = el('div', 'system3d-controls');
  const playBtn = el('button', 'tag-pill', '');
  playBtn.type = 'button';
  const reverseBtn = el('button', 'tag-pill', '⇄ Reverse');
  reverseBtn.type = 'button';
  const nowBtn = el('button', 'tag-pill', 'Now');
  nowBtn.type = 'button';
  nowBtn.title = 'Jump to the current date';

  const speedWrap = el('label', 'system3d-range');
  speedWrap.appendChild(el('span', null, 'Speed'));
  const speed = document.createElement('input');
  speed.type = 'range';
  speed.min = SPEED_MIN_EXP;
  speed.max = SPEED_MAX_EXP;
  speed.step = 0.01;
  speed.value = 0; // 10^0 = 1 day per second
  speed.setAttribute('aria-label', 'Simulation speed, days per second');
  const speedOut = el('output', 'system3d-value');
  speedWrap.append(speed, speedOut);

  const presets = el('div', 'segmented');
  for (const [label, dps] of [
    ['1 h/s', 1 / 24],
    ['1 d/s', 1],
    ['10 d/s', 10],
    ['100 d/s', 100],
  ]) {
    const b = el('button', '', label);
    b.type = 'button';
    b.addEventListener('click', () => {
      speed.value = Math.log10(dps);
      updateSpeed();
    });
    presets.appendChild(b);
  }

  const sizeWrap = el('label', 'system3d-range');
  sizeWrap.appendChild(el('span', null, 'Planet size'));
  const size = document.createElement('input');
  size.type = 'range';
  size.min = 0;
  size.max = Math.log10(3000);
  size.step = 0.01;
  size.value = Math.log10(autoScale);
  size.setAttribute('aria-label', 'Planet size exaggeration');
  const sizeOut = el('output', 'system3d-value');
  sizeWrap.append(size, sizeOut);

  const views = el('div', 'segmented');
  const viewButtons = {};
  for (const [key, label] of [
    ['oblique', 'Oblique'],
    ['top', 'Top-down'],
    ['earth', 'From Earth'],
  ]) {
    const b = el('button', key === 'oblique' ? 'active' : '', label);
    b.type = 'button';
    b.addEventListener('click', () => setView(key));
    viewButtons[key] = b;
    views.appendChild(b);
  }

  const fsBtn = el('button', 'tag-pill', '⛶ Fullscreen');
  fsBtn.type = 'button';

  const row1 = el('div', 'system3d-row');
  row1.append(playBtn, reverseBtn, nowBtn, speedWrap, presets);
  const row2 = el('div', 'system3d-row');
  row2.append(views, sizeWrap, fsBtn);
  controlsBar.append(row1, row2);
  root.appendChild(controlsBar);

  const legend = el('div', 'system3d-legend');
  root.appendChild(legend);
  const notes = el('p', 'viz-caption');
  root.appendChild(notes);
  container.appendChild(root);

  // ----- renderer, scene, camera -----
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  stage.appendChild(renderer.domElement);
  const labelRenderer = new CSS2DRenderer();
  labelRenderer.domElement.className = 'system3d-labels';
  stage.appendChild(labelRenderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x05070d);
  const camera = new THREE.PerspectiveCamera(45, 1, 0.0005, 2000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.minDistance = Math.max(starRadiusAu * auToScene * 1.5, 0.01);
  controls.maxDistance = SCENE_RADIUS * 12;

  const disposables = [];
  const track = (o) => {
    disposables.push(o);
    return o;
  };

  // Background stars
  {
    const n = 1500;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const v = new THREE.Vector3().randomDirection().multiplyScalar(400 + Math.random() * 200);
      pos.set([v.x, v.y, v.z], i * 3);
    }
    const geo = track(new THREE.BufferGeometry());
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    const mat = track(new THREE.PointsMaterial({ color: 0x8090a8, size: 1.2, sizeAttenuation: false, transparent: true, opacity: 0.6 }));
    scene.add(new THREE.Points(geo, mat));
  }

  // Star
  const starColor = blackbodyColor(data.star.teff_k);
  const starScene = starRadiusAu * auToScene;
  {
    const geo = track(new THREE.SphereGeometry(starScene, 48, 32));
    const mat = track(new THREE.MeshBasicMaterial({ color: starColor }));
    scene.add(new THREE.Mesh(geo, mat));
    const glowTex = track(radialTexture([
      [0, 'rgba(255,255,255,0.9)'],
      [0.2, 'rgba(255,255,255,0.35)'],
      [1, 'rgba(255,255,255,0)'],
    ]));
    const glowMat = track(new THREE.SpriteMaterial({ map: glowTex, color: starColor, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }));
    const glow = new THREE.Sprite(glowMat);
    glow.scale.setScalar(Math.max(starScene * 6, SCENE_RADIUS * 0.08));
    scene.add(glow);
    const starLabel = el('div', 'system3d-label star', data.starName || 'Host star');
    const lbl = new CSS2DObject(starLabel);
    lbl.position.set(0, -Math.max(starScene * 1.6, SCENE_RADIUS * 0.05), 0);
    scene.add(lbl);
  }
  scene.add(new THREE.AmbientLight(0xffffff, 0.18));
  const light = new THREE.PointLight(starColor, 3, 0, 0);
  scene.add(light);

  // Reference rings every nice AU step
  const gridStep = niceStep(maxA / 4);
  {
    const mat = track(new THREE.LineBasicMaterial({ color: 0x334155, transparent: true, opacity: 0.35 }));
    for (let r = gridStep; r <= maxA * 1.25; r += gridStep) {
      const pts = [];
      for (let i = 0; i <= 128; i++) {
        const t = (i / 128) * Math.PI * 2;
        pts.push(new THREE.Vector3(Math.sin(t) * r * auToScene, 0, Math.cos(t) * r * auToScene));
      }
      const geo = track(new THREE.BufferGeometry().setFromPoints(pts));
      scene.add(new THREE.Line(geo, mat));
    }
  }

  // Line of sight to Earth (+Z). A planet crossing it is in transit.
  {
    const len = SCENE_RADIUS * 1.35;
    const geo = track(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, len)]));
    const mat = track(new THREE.LineDashedMaterial({ color: 0x94a3b8, dashSize: 0.25, gapSize: 0.18, transparent: true, opacity: 0.7 }));
    const line = new THREE.Line(geo, mat);
    line.computeLineDistances();
    scene.add(line);
    const tag = new CSS2DObject(el('div', 'system3d-label earth', '→ toward Earth'));
    tag.position.set(0, 0, len);
    scene.add(tag);
  }

  // Habitable zone (conservative flux limits, Sun-like scaling)
  let hzText = null;
  if (starRadiusKnown && teffKnown) {
    const L = data.star.radius_rsun ** 2 * (data.star.teff_k / 5772) ** 4;
    const inner = Math.sqrt(L / 1.107);
    const outer = Math.sqrt(L / 0.356);
    if (inner < maxA * 3) {
      const geo = track(new THREE.RingGeometry(inner * auToScene, outer * auToScene, 128, 1));
      geo.rotateX(-Math.PI / 2);
      const mat = track(new THREE.MeshBasicMaterial({ color: 0x1baf7a, transparent: true, opacity: 0.1, side: THREE.DoubleSide, depthWrite: false }));
      scene.add(new THREE.Mesh(geo, mat));
      hzText = `Green band: approximate habitable zone, ${fmtAu(inner)}–${fmtAu(outer)} AU (flux limits 1.11–0.36 S⊕).`;
    }
  }

  // Planets
  const accent = new THREE.Color(cssVar('--viz-series-1', '#3987e5'));
  const muted = new THREE.Color(0xa8a29e);
  const ringTex = track(ringTexture());
  const dotTex = track(radialTexture([
    [0, 'rgba(255,255,255,1)'],
    [0.45, 'rgba(255,255,255,1)'],
    [0.55, 'rgba(255,255,255,0)'],
  ]));
  const planets = orbiting.map((p) => {
    const isSelf = p.id === data.selfId;
    const color = isSelf ? accent : muted;
    const aScene = p.semi_major_axis_au * auToScene;

    const orbitPts = [];
    for (let i = 0; i <= 256; i++) {
      const t = (i / 256) * Math.PI * 2;
      orbitPts.push(new THREE.Vector3(Math.sin(t) * aScene, 0, Math.cos(t) * aScene));
    }
    const orbitGeo = track(new THREE.BufferGeometry().setFromPoints(orbitPts));
    const orbitMat = track(new THREE.LineBasicMaterial({ color, transparent: true, opacity: isSelf ? 0.95 : 0.4 }));
    scene.add(new THREE.Line(orbitGeo, orbitMat));

    const radiusAu = (p.radius_earth || 1) * AU_PER_REARTH;
    const geo = track(new THREE.SphereGeometry(1, 32, 24));
    const mat = track(new THREE.MeshStandardMaterial({ color, roughness: 0.75, metalness: 0, emissive: color, emissiveIntensity: isSelf ? 0.25 : 0.08 }));
    const mesh = new THREE.Mesh(geo, mat);
    mesh.userData.planetId = p.id;
    scene.add(mesh);

    // Constant-pixel marker so every planet stays findable at true scale
    const dotMat = track(new THREE.SpriteMaterial({ map: dotTex, color, sizeAttenuation: false, depthTest: false, transparent: true }));
    const dot = new THREE.Sprite(dotMat);
    dot.scale.setScalar(isSelf ? 0.014 : 0.009);
    dot.renderOrder = 2;
    scene.add(dot);
    let ring = null;
    if (isSelf) {
      const ringMat = track(new THREE.SpriteMaterial({ map: ringTex, color: accent, sizeAttenuation: false, depthTest: false, transparent: true }));
      ring = new THREE.Sprite(ringMat);
      ring.scale.setScalar(0.04);
      ring.renderOrder = 3;
      scene.add(ring);
    }

    const labelEl = el('div', `system3d-label${isSelf ? ' self' : ''}`, p.display_name);
    if (!isSelf) {
      labelEl.title = `Open ${p.display_name}`;
      labelEl.addEventListener('click', () => {
        window.location.hash = `#candidate/${encodeURIComponent(p.id)}?view=system`;
      });
    }
    const label = new CSS2DObject(labelEl);
    scene.add(label);

    const phaseKnown = p.epoch_bjd > 2400000;
    return {
      p,
      isSelf,
      aScene,
      radiusAu,
      mesh,
      dot,
      ring,
      label,
      phaseKnown,
      offset: phaseKnown ? 0 : hashPhase(p.id),
      transiting: false,
    };
  });

  // ----- legend -----
  function buildLegend() {
    legend.replaceChildren();
    const table = el('table', 'viz-table system3d-table');
    const head = el('tr');
    for (const h of ['Planet', 'Period', 'a (AU)', 'Radius (R⊕)', 'Now']) head.appendChild(el('th', null, h));
    const thead = el('thead');
    thead.appendChild(head);
    table.appendChild(thead);
    const tbody = el('tbody');
    for (const pl of planets) {
      const tr = el('tr', pl.isSelf ? 'is-self' : '');
      const nameCell = el('td');
      const sw = el('span', 'viz-legend-swatch');
      sw.style.background = pl.isSelf ? cssVar('--viz-series-1', '#3987e5') : '#a8a29e';
      nameCell.appendChild(sw);
      if (pl.isSelf) nameCell.appendChild(el('strong', null, pl.p.display_name));
      else {
        const a = el('a', null, pl.p.display_name);
        a.href = `#candidate/${encodeURIComponent(pl.p.id)}?view=system`;
        nameCell.appendChild(a);
      }
      tr.appendChild(nameCell);
      tr.appendChild(el('td', null, `${pl.p.period_d.toFixed(pl.p.period_d < 10 ? 4 : 2)} d`));
      tr.appendChild(el('td', null, fmtAu(pl.p.semi_major_axis_au) + (pl.p.semi_major_axis_basis === 'kepler3_assumed_solar_mass' ? ' *' : '')));
      tr.appendChild(
        el('td', null, pl.p.radius_earth ? pl.p.radius_earth.toFixed(2) + (pl.p.radius_basis === 'transit_depth' ? ' †' : '') : 'unknown')
      );
      const state = el('td', 'system3d-state', pl.phaseKnown ? '' : 'phase unknown');
      pl.stateCell = state;
      tr.appendChild(state);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    legend.appendChild(table);

    const lines = [
      'Circular, coplanar orbits seen from the system; Earth lies along the dashed line, so a planet crossing it is in transit. Positions follow each published ephemeris (phase 0 = mid-transit).',
    ];
    if (planets.some((pl) => pl.p.semi_major_axis_basis === 'kepler3_assumed_solar_mass')) lines.push('* a from Kepler’s third law assuming a 1 M☉ host (no published stellar mass).');
    if (planets.some((pl) => pl.p.radius_basis === 'transit_depth')) lines.push('† radius estimated as R★·√depth (undiluted, non-grazing).');
    if (planets.some((pl) => !pl.p.radius_earth)) lines.push('Unknown radii are drawn at 1 R⊕.');
    if (planets.some((pl) => !pl.phaseKnown)) lines.push('Planets without a transit epoch are placed at an arbitrary phase.');
    if (!starRadiusKnown) lines.push('Stellar radius unknown; star drawn at 1 R☉.');
    if (hzText) lines.push(hzText);
    lines.push(`Faint rings every ${fmtAu(gridStep)} AU.`);
    if (missing.length) lines.push(`Not drawn (no period): ${missing.map((m) => m.display_name).join(', ')}.`);
    notes.textContent = lines.join(' ');
  }
  buildLegend();

  // ----- simulation state -----
  let simJd = nowJd();
  let playing = !reduceMotion;
  let direction = 1;
  let daysPerSecond = 1;
  let planetScale = autoScale;

  function updateSpeed() {
    daysPerSecond = Math.pow(10, Number(speed.value));
    const d = daysPerSecond;
    speedOut.textContent = d < 1 ? `${(d * 24).toPrecision(2)} h/s` : `${d < 10 ? d.toFixed(1) : Math.round(d)} d/s`;
  }
  function updateSize() {
    planetScale = Math.pow(10, Number(size.value));
    sizeOut.textContent = planetScale < 1.5 ? 'true scale' : `×${Math.round(planetScale)}`;
    for (const pl of planets) pl.mesh.scale.setScalar(pl.radiusAu * auToScene * planetScale);
  }
  function updatePlay() {
    playBtn.textContent = playing ? '❚❚ Pause' : '▶ Play';
    playBtn.setAttribute('aria-pressed', String(playing));
  }
  speed.addEventListener('input', updateSpeed);
  size.addEventListener('input', updateSize);
  playBtn.addEventListener('click', () => {
    playing = !playing;
    updatePlay();
  });
  reverseBtn.addEventListener('click', () => {
    direction *= -1;
    reverseBtn.classList.toggle('active', direction < 0);
  });
  nowBtn.addEventListener('click', () => {
    simJd = nowJd();
  });
  fsBtn.addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else root.requestFullscreen?.();
  });
  updateSpeed();
  updateSize();
  updatePlay();

  function setView(key) {
    for (const [k, b] of Object.entries(viewButtons)) b.classList.toggle('active', k === key);
    // Edge-on, labels of every planet stack on one line; keep only the selected one.
    root.classList.toggle('edge-on', key === 'earth');
    const d = SCENE_RADIUS * 2.4;
    if (key === 'top') camera.position.set(0, d, 0.001);
    else if (key === 'earth') camera.position.set(0, SCENE_RADIUS * 0.06, SCENE_RADIUS * 1.7);
    else camera.position.set(d * 0.55, d * 0.45, d * 0.75);
    controls.target.set(0, 0, 0);
    controls.update();
  }
  setView('oblique');

  // ----- interaction: click a planet to open it -----
  let downAt = null;
  renderer.domElement.addEventListener('pointerdown', (e) => {
    downAt = [e.clientX, e.clientY];
  });
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4) return;
    const rect = renderer.domElement.getBoundingClientRect();
    // Pick the nearest planet within ~12 px of the pointer, so tiny true-scale planets are clickable
    let best = null;
    let bestD = 12;
    const v = new THREE.Vector3();
    for (const pl of planets) {
      v.copy(pl.mesh.position).project(camera);
      const px = ((v.x + 1) / 2) * rect.width;
      const py = ((1 - v.y) / 2) * rect.height;
      const d = Math.hypot(px - (e.clientX - rect.left), py - (e.clientY - rect.top));
      if (d < bestD && v.z < 1) {
        bestD = d;
        best = pl;
      }
    }
    if (best && !best.isSelf) window.location.hash = `#candidate/${encodeURIComponent(best.p.id)}?view=system`;
  });

  // ----- sizing -----
  function resize() {
    const w = stage.clientWidth || 640;
    const h = document.fullscreenElement === root ? Math.max(300, window.innerHeight - controlsBar.offsetHeight - 40) : Math.round(Math.min(560, Math.max(320, w * 0.6)));
    stage.style.height = `${h}px`;
    renderer.setSize(w, h);
    labelRenderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(stage);
  document.addEventListener('fullscreenchange', resize);
  resize();

  // ----- animation -----
  let visible = true;
  const io = new IntersectionObserver((entries) => {
    visible = entries.some((e) => e.isIntersecting);
  });
  io.observe(stage);

  let raf = 0;
  let last = performance.now();
  let lastStateUpdate = 0;
  function frame(now) {
    raf = requestAnimationFrame(frame);
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    if (!visible || document.hidden) return;
    if (playing) simJd += dt * daysPerSecond * direction;

    for (const pl of planets) {
      const phase = pl.phaseKnown ? (simJd - pl.p.epoch_bjd) / pl.p.period_d : simJd / pl.p.period_d + pl.offset;
      const theta = 2 * Math.PI * (phase - Math.floor(phase));
      const x = Math.sin(theta) * pl.aScene;
      const z = Math.cos(theta) * pl.aScene;
      pl.mesh.position.set(x, 0, z);
      pl.dot.position.set(x, 0, z);
      if (pl.ring) pl.ring.position.set(x, 0, z);
      const lift = Math.max(pl.radiusAu * auToScene * planetScale * 1.6, SCENE_RADIUS * 0.035);
      pl.label.position.set(x, lift, z);
      // In transit when the planet's projected separation from the star is below R★ + Rp, on the near side.
      const sep = Math.abs(x);
      pl.transiting = pl.phaseKnown && z > 0 && sep < starScene + pl.radiusAu * auToScene;
    }
    if (now - lastStateUpdate > 150) {
      lastStateUpdate = now;
      clock.textContent = `${jdToIso(simJd)}  ·  BJD ${simJd.toFixed(3)}`;
      for (const pl of planets) {
        if (!pl.stateCell || !pl.phaseKnown) continue;
        pl.stateCell.textContent = pl.transiting ? 'in transit' : '';
        pl.stateCell.classList.toggle('active', pl.transiting);
      }
    }
    controls.update();
    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
  }
  raf = requestAnimationFrame(frame);

  return {
    dispose() {
      cancelAnimationFrame(raf);
      ro.disconnect();
      io.disconnect();
      document.removeEventListener('fullscreenchange', resize);
      if (document.fullscreenElement === root) document.exitFullscreen();
      controls.dispose();
      for (const d of disposables) d.dispose?.();
      renderer.dispose();
      container.replaceChildren();
    },
  };
}
