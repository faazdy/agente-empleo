// Radar de Empleo — front sin frameworks: Bootstrap 5.3 + Bootstrap Icons + JS a mano.

let offers = [];
let stats = {};
let profilesData = { active: null, profiles: [] };
let editingId = null;
let qaEntries = [];
let pollTimer = null;
let builtin = null;   // consultas y filtros que trae el radar de fábrica

const STATUS = {
  matched:   { label: 'Compatible',  icon: 'bullseye' },
  favorite:  { label: 'Guardada',    icon: 'bookmark-fill' },
  applied:   { label: 'Postulada',   icon: 'send-check' },
  discarded: { label: 'Descartada',  icon: 'x-circle' },
  new:       { label: 'Nueva',       icon: 'stars' },
};

const EMPTY = {
  matched:   ['bullseye', 'No hay vacantes compatibles con el perfil activo. Lanza un escaneo o baja el puntaje mínimo del perfil.'],
  favorite:  ['bookmark', 'Todavía no has guardado ninguna. Usa el marcador en una vacante compatible para dejarla acá.'],
  applied:   ['send-check', 'Aún no has marcado postulaciones. Cuando te postules, márcalo y aparecerá acá con su etapa.'],
  discarded: ['x-circle', 'Nada descartado por ahora.'],
  all:       ['inbox', 'La base está vacía. Lanza el primer escaneo con el botón Escanear.'],
};

// ---------- utilidades ----------
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function norm(s) {
  return String(s ?? '').toLowerCase().normalize('NFD').replace(/\p{Diacritic}/gu, '');
}

function safeUrl(u) {
  return /^https?:\/\//i.test(String(u || '')) ? String(u) : '#';
}

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* sin cuerpo */ }
  if (!res.ok) throw new Error((data && data.detail) || `Error ${res.status}`);
  return data;
}

function notify(text, kind = '') {
  const icon = kind === 'bad' ? 'exclamation-triangle' : kind === 'good' ? 'check2-circle' : 'info-circle';
  const div = document.createElement('div');
  div.className = `toast-msg ${kind}`;
  div.innerHTML = `<i class="bi bi-${icon}"></i><span class="flex-grow-1">${esc(text)}</span>
    <button type="button" aria-label="Cerrar"><i class="bi bi-x-lg"></i></button>`;
  div.querySelector('button').onclick = () => div.remove();
  $('msg').append(div);
  setTimeout(() => div.remove(), kind === 'bad' ? 14000 : 7000);
}

// Avisos emergentes: aparecen casi al instante y se rehacen cada vez que se pinta la lista.
let tips = [];

function initTips() {
  tips.forEach((t) => { try { t.dispose(); } catch (_) {} });
  tips = [...document.querySelectorAll('[data-bs-toggle="tooltip"]')].map((el) =>
    new bootstrap.Tooltip(el, { delay: { show: 60, hide: 0 }, container: 'body', trigger: 'hover focus' }));
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleDateString('es-CO', { day: '2-digit', month: 'short' });
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString('es-CO', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

// ---------- filtros guardados ----------
const FILTER_IDS = ['fText', 'fPortal', 'fModality', 'fMinScore', 'fExclude', 'fSort'];

function saveFilters() {
  const f = { status: currentStatus() };
  FILTER_IDS.forEach((id) => (f[id] = $(id).value));
  try { localStorage.setItem('radar.filters', JSON.stringify(f)); } catch (_) {}
}

function restoreFilters() {
  let f = {};
  try { f = JSON.parse(localStorage.getItem('radar.filters') || '{}'); } catch (_) {}
  FILTER_IDS.forEach((id) => { if (f[id] != null && $(id)) $(id).value = f[id]; });
  setStatusButtons(f.status === 'all' || STATUS[f.status] ? f.status : 'matched');
  if (FILTER_IDS.some((id) => id !== 'fSort' && $(id).value)) {
    bootstrap.Collapse.getOrCreateInstance($('filters'), { toggle: false }).show();
  }
}

function clearFilters() {
  ['fText', 'fPortal', 'fModality', 'fMinScore', 'fExclude'].forEach((id) => ($(id).value = ''));
  renderOffers();
}

// ---------- estado seleccionado ----------
function currentStatus() {
  return $('statusSeg').querySelector('[aria-pressed="true"]')?.dataset.status || 'matched';
}

function setStatusButtons(status) {
  $('statusSeg').querySelectorAll('.seg-btn').forEach((b) => {
    b.setAttribute('aria-pressed', String(b.dataset.status === status));
  });
}

function pickStatus(status) {
  setStatusButtons(status);
  loadOffers();
}

// ---------- vacantes ----------
async function loadOffers() {
  try {
    offers = await api(`/api/offers?status=${encodeURIComponent(currentStatus())}&sort_by=${encodeURIComponent($('fSort').value)}`);
  } catch (e) {
    notify('No se pudieron cargar las vacantes: ' + e.message, 'bad');
    offers = [];
  }
  renderOffers();
  loadStats();
}

function filteredOffers() {
  const text = norm($('fText').value.trim());
  const portal = $('fPortal').value.toLowerCase();
  const modality = norm($('fModality').value);
  const min = parseFloat($('fMinScore').value);
  const excl = $('fExclude').value.split(',').map((w) => norm(w.trim())).filter(Boolean);
  return offers.filter((o) => {
    const hay = norm(`${o.title} ${o.company} ${o.description} ${(o.requirements || []).join(' ')}`);
    if (text && !text.split(/\s+/).every((w) => hay.includes(w))) return false;
    if (portal && (o.portal || '').toLowerCase() !== portal) return false;
    if (modality && !norm(`${o.modality} ${o.location}`).includes(modality)) return false;
    if (!isNaN(min) && (o.match_score || 0) < min) return false;
    if (excl.length && excl.some((w) => norm(o.title).includes(w))) return false;
    return true;
  });
}

function band(score) {
  if (score == null) return 'none';
  return score >= 75 ? 'top' : score >= 60 ? 'high' : score >= 35 ? 'mid' : 'low';
}

function chip(icon, text, attrs = '') {
  return text ? `<span class="chip" ${attrs}><i class="bi bi-${icon}"></i>${esc(text)}</span>` : '';
}

function metaChips(o) {
  // "Remoto" suele venir a la vez como ubicación y como modalidad: se muestra una sola vez.
  const mod = o.modality && norm(o.modality) !== norm(o.location) ? o.modality : '';
  const salary = o.salary && norm(o.salary) !== 'a convenir' ? o.salary : '';
  const remoto = (t) => (norm(t).includes('remot') ? 'data-kind="remoto"' : '');
  const portal = o.portal
    ? `<span class="chip" data-portal="${esc(norm(o.portal))}"><span class="dot"></span>${esc(o.portal)}</span>` : '';
  const pill = (t, attrs = '') => (t ? `<span class="chip" ${attrs}>${esc(t)}</span>` : '');
  return portal
    + pill(o.location, remoto(o.location))
    + pill(mod, remoto(mod))
    + (salary ? pill(salary, 'data-kind="salario"') : pill('Salario a convenir', 'data-kind="sin-salario"'));
}

// Anillo de afinidad: el número con su % dentro de un aro que se llena según el puntaje.
function scoreRing(score) {
  const vuelta = 150.8;  // perímetro del círculo de radio 24
  const v = score == null ? 0 : Math.max(0, Math.min(100, score));
  return `<div class="score" data-band="${band(score)}" data-bs-toggle="tooltip"
      data-bs-title="${score == null ? 'Sin puntuar' : score + '% de afinidad con tu perfil'}">
    <svg class="ring" viewBox="0 0 56 56" aria-hidden="true">
      <circle class="ring-bg" cx="28" cy="28" r="24"></circle>
      <circle class="ring-fg" cx="28" cy="28" r="24"
              stroke-dasharray="${vuelta}" stroke-dashoffset="${(vuelta * (1 - v / 100)).toFixed(2)}"></circle>
    </svg>
    <span class="score-n">${score == null ? '—' : score}</span>
  </div>`;
}

function actionList(o) {
  const a = [{ href: safeUrl(o.url), icon: 'box-arrow-up-right', label: 'Abrir en el portal' }];
  if (o.status !== 'applied') a.push({ act: 'applied', icon: 'send-check', label: 'Ya me postulé', cls: 'ok' });
  if (o.status !== 'favorite' && o.status !== 'applied') a.push({ act: 'favorite', icon: 'bookmark', label: 'Guardar para después' });
  if (o.status !== 'discarded') a.push({ act: 'discarded', icon: 'x-lg', label: 'Descartar', cls: 'no' });
  if (['discarded', 'favorite', 'applied'].includes(o.status)) a.push({ act: 'matched', icon: 'arrow-counterclockwise', label: 'Volver a compatibles' });
  return a;
}

function iconActions(o) {
  return actionList(o).map((b) => b.href
    ? `<a class="icon-btn" href="${esc(b.href)}" target="_blank" rel="noreferrer noopener"
         data-bs-toggle="tooltip" data-bs-title="${esc(b.label)}" aria-label="${esc(b.label)}"><i class="bi bi-${b.icon}"></i></a>`
    : `<button class="icon-btn ${b.cls || ''}" data-id="${esc(o.id)}" data-act="${b.act}"
         data-bs-toggle="tooltip" data-bs-title="${esc(b.label)}" aria-label="${esc(b.label)}"><i class="bi bi-${b.icon}"></i></button>`
  ).join('');
}

function labelActions(o) {
  return actionList(o).map((b) => b.href
    ? `<a class="btn btn-sm btn-primary" href="${esc(b.href)}" target="_blank" rel="noreferrer noopener"><i class="bi bi-${b.icon} me-1"></i>${esc(b.label)}</a>`
    : `<button class="btn btn-sm ${b.cls === 'ok' ? 'btn-ok' : b.cls === 'no' ? 'btn-danger-soft' : 'btn-outline-secondary'}"
         data-id="${esc(o.id)}" data-act="${b.act}"><i class="bi bi-${b.icon} me-1"></i>${esc(b.label)}</button>`
  ).join('');
}

function renderOffers() {
  saveFilters();
  const list = filteredOffers();
  const status = currentStatus();
  const total = offers.length;
  $('resultLine').textContent = list.length === total
    ? `${total} ${total === 1 ? 'vacante' : 'vacantes'}`
    : `${list.length} de ${total} vacantes con los filtros puestos`;

  if (!list.length) {
    const [icon, text] = total
      ? ['funnel', 'Ninguna vacante pasa esos filtros. Prueba a limpiarlos.']
      : (EMPTY[status] || EMPTY.all);
    $('offersList').innerHTML = `<div class="empty"><i class="bi bi-${icon}"></i><b>Nada por aquí</b>${esc(text)}</div>`;
    return;
  }

  $('offersList').innerHTML = list.map((o) => {
    const score = o.match_score == null ? null : Math.round(o.match_score);
    const st = STATUS[o.status] || {};
    return `
    <article class="row-job">
      ${scoreRing(score)}
      <div class="row-main">
        <a class="job-title" href="#" data-detail="${esc(o.id)}">${esc(o.title)}</a>
        <p class="job-company">${esc(o.company || 'Empresa sin nombre')}</p>
        <div class="chips">
          ${metaChips(o)}
          ${status === 'all' && st.label ? `<span class="chip chip-state"><i class="bi bi-${st.icon}"></i>${esc(st.label)}</span>` : ''}
        </div>
      </div>
      <div class="row-side">
        <span class="row-date">${esc(o.posted_date || fmtDate(o.scraped_at))}</span>
        <div class="row-actions">${iconActions(o)}</div>
      </div>
    </article>`;
  }).join('');
  initTips();
}

function showDetail(id) {
  const o = offers.find((x) => x.id === id);
  if (!o) return;
  const score = o.match_score == null ? null : Math.round(o.match_score);
  $('dTitle').textContent = o.title;
  $('dCompany').textContent = o.company || 'Empresa sin nombre';
  $('dChips').innerHTML = metaChips(o) + chip('calendar3', o.posted_date || fmtDate(o.scraped_at))
    + (norm(o.salary) === 'a convenir' ? chip('cash-coin', 'Salario a convenir') : '');
  $('dReason').innerHTML = `<b class="num">${score == null ? '—' : score + '%'} de afinidad.</b> ${esc(o.match_reason || 'Sin diagnóstico.')}`;
  const reqs = o.requirements || [];
  $('dReqsBox').style.display = reqs.length ? '' : 'none';
  $('dReqs').innerHTML = reqs.map((r) => chip('dot', r)).join('');
  $('dDesc').textContent = o.description || 'El portal no entregó descripción.';
  $('dActions').innerHTML = labelActions(o);
  bootstrap.Offcanvas.getOrCreateInstance($('detail')).show();
}

async function setStatus(id, status) {
  if (status === 'applied' && !confirm('¿Ya te postulaste a esta vacante? Queda registrada en Postuladas.')) return;
  try {
    await api(`/api/offers/${id}/status`, { method: 'POST', body: { status } });
    bootstrap.Offcanvas.getInstance($('detail'))?.hide();
    await loadOffers();
  } catch (e) { notify(e.message, 'bad'); }
}

async function loadStats() {
  try {
    stats = await api('/api/stats');
    ['matched', 'favorite', 'applied', 'discarded'].forEach((k) => ($('cnt-' + k).textContent = stats[k] || 0));
    $('cnt-total').textContent = stats.total || 0;
    renderPageHead();
  } catch (_) {}
  loadLastScan();
}

function activeProfileName() {
  return profilesData.profiles.find((p) => p.id === profilesData.active)?.name || '';
}

// Subtítulo de Vacantes e inicial del perfil activo en la barra.
function renderPageHead() {
  const n = stats.matched || 0;
  const name = activeProfileName();
  $('pageSub').textContent = `${n} ${n === 1 ? 'oportunidad compatible' : 'oportunidades compatibles'}`
    + (name ? ` con el perfil ${name}.` : '.');
  $('profileInitial').textContent = (name.trim()[0] || '·').toUpperCase();
}

// "Actualizado hace 4 horas", según el último escaneo completo.
function timeAgo(iso) {
  const min = Math.round((Date.now() - new Date(iso)) / 60000);
  if (isNaN(min)) return '';
  if (min < 1) return 'hace un momento';
  if (min < 60) return `hace ${min} min`;
  const h = Math.round(min / 60);
  if (h < 24) return h === 1 ? 'hace 1 hora' : `hace ${h} horas`;
  const d = Math.round(h / 24);
  return d === 1 ? 'ayer' : `hace ${d} días`;
}

async function loadLastScan() {
  try {
    const r = await api('/api/sync/report');
    $('lastScan').textContent = r && r.finished_at ? `Actualizado ${timeAgo(r.finished_at)}` : 'Aún sin escanear';
  } catch (_) {}
}

// ---------- escaneo ----------
// Hasta dónde le pide cada portal: Computrabajo acepta 1, 3, 7, 15 o 30 días; Indeed corta en 14.
function showScanDays() {
  const d = +$('scanDays').value;
  $('scanDaysOut').textContent = d === 1 ? '1 día' : `${d} días`;
  $('scanDaysNote').textContent = d > 14
    ? 'Indeed solo llega a 14 días; Computrabajo y elempleo sí buscan todo el rango.'
    : d > 7 ? 'Más días es más lento y gasta más peticiones a los portales.'
    : 'Súbelo si llevas días sin escanear o si acabas de empezar.';
}

async function scan(portal, quick = false) {
  const days = +($('scanDays')?.value || 0);
  try {
    const r = await api(`/api/sync?portal=${encodeURIComponent(portal)}${quick ? '&quick=true' : ''}${days ? '&days=' + days : ''}`,
      { method: 'POST' });
    notify(r.message);
    pollSync();
  } catch (e) { notify(e.message, 'bad'); }
}

async function pollSync() {
  clearTimeout(pollTimer);
  let st;
  try { st = await api('/api/sync/status'); } catch (_) { return; }
  const box = $('scanbar');
  if (st.is_running) {
    box.innerHTML = `<div class="scanbar">
      <span class="spinner-border" role="status" aria-hidden="true"></span>
      <span class="flex-grow-1">Escaneando${st.profile ? ' con «' + esc(st.profile) + '»' : ''}: ${esc(st.current_query || '')}
        <span class="num">${st.current_index}/${st.total_queries}</span> ·
        <span class="num">${st.new_offers}</span> nuevas compatibles${st.note ? ' · ' + esc(st.note) : ''}</span>
    </div>`;
    box.dataset.wasRunning = '1';
    pollTimer = setTimeout(pollSync, 3000);
    return;
  }
  box.innerHTML = '';
  if (box.dataset.wasRunning === '1') {
    const resumen = `Escaneo terminado: ${st.new_offers} nuevas compatibles.`;
    notify(`${resumen}${st.last_error ? ' Avisos: ' + st.last_error : ''}`, st.last_error ? 'bad' : 'good');
    notifyDesktop(resumen);
    loadOffers();
    loadReport();
  }
  box.dataset.wasRunning = '0';
}

// ---------- postuladas ----------
async function loadTracking() {
  let t, list;
  try {
    [t, list] = await Promise.all([api('/api/tracking'), api('/api/offers?status=applied&sort_by=recent')]);
  } catch (e) { notify(e.message, 'bad'); return; }
  $('trackingStats').innerHTML = `
    <div class="stat"><b class="num">${t.total}</b><span>postulaciones</span></div>
    <div class="stat"><b class="num">${t.response_rate}%</b><span>con respuesta</span></div>
    <div class="stat"><b class="num">${t.stale_count}</b><span>calladas hace más de ${t.stale_days} días</span></div>
    ${Object.entries(t.by_stage).filter(([, n]) => n).map(([k, n]) =>
      `<div class="stat"><b class="num">${n}</b><span>${esc(t.stage_labels[k])}</span></div>`).join('')}`;

  list.sort((a, b) => (b.applied_at || '').localeCompare(a.applied_at || ''));
  $('trackingBody').innerHTML = list.map((o) => `
    <tr>
      <td><a href="${esc(safeUrl(o.url))}" target="_blank" rel="noreferrer noopener">${esc(o.title)}</a></td>
      <td class="text-body-secondary">${esc(o.company)}</td>
      <td class="text-body-secondary">${esc(o.portal)}</td>
      <td class="text-nowrap text-body-secondary">${fmtDate(o.applied_at)}</td>
      <td><select class="form-select form-select-sm" aria-label="Etapa" onchange="saveTracking('${esc(o.id)}', {stage: this.value})">
        ${Object.entries(t.stage_labels).map(([k, v]) => `<option value="${k}" ${(o.stage || 'postulado') === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}
      </select></td>
      <td><input class="form-control form-control-sm" aria-label="Notas" placeholder="Añade una nota"
           value="${esc(o.notes || '')}" onchange="saveTracking('${esc(o.id)}', {notes: this.value})"></td>
    </tr>`).join('')
    || `<tr><td colspan="6"><div class="empty"><i class="bi bi-send-check"></i>${EMPTY.applied[1]}</div></td></tr>`;
}

async function saveTracking(id, body) {
  try { await api(`/api/offers/${id}/tracking`, { method: 'POST', body }); }
  catch (e) { notify(e.message, 'bad'); }
}

// ---------- perfiles ----------
async function loadProfiles() {
  profilesData = await api('/api/profiles');
  $('activeProfile').innerHTML = profilesData.profiles
    .map((p) => `<option value="${esc(p.id)}" ${p.id === profilesData.active ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
  // La barra del escaneo arranca en los días que trae el perfil activo.
  const activo = profilesData.profiles.find((p) => p.id === profilesData.active);
  if (activo && $('scanDays')) {
    $('scanDays').value = activo.max_age_days;
    showScanDays();
  }
  renderProfileList();
  renderPageHead();
}

function renderProfileList() {
  $('profileList').innerHTML = profilesData.profiles.map((p) => `
    <button type="button" class="profile-item ${p.id === editingId ? 'is-editing' : ''}" data-profile="${esc(p.id)}">
      <span class="text-truncate">${esc(p.name)}</span>
      ${p.id === profilesData.active ? '<span class="tag tag-on">en uso</span>'
        : p.id === 'default' ? '<span class="tag">original</span>' : ''}
    </button>`).join('');
  if (editingId === null && !$('profileForm').name.value) editProfile(profilesData.active);
}

function editProfile(id) {
  const p = profilesData.profiles.find((x) => x.id === id);
  if (!p) return;
  editingId = id;
  fillForm(p);
  renderProfileList();
}

function fillForm(p) {
  const f = $('profileForm');
  f.id.value = p.id || '';
  f.name.value = p.name || '';
  f.use_default_queries.checked = !!p.use_default_queries;
  f.queries.value = (p.queries || []).join('\n');
  f.city.value = p.city || 'Bogotá';
  f.location_mode.value = p.location_mode || 'ambas';
  f.max_pages.value = p.max_pages;
  f.max_age_days.value = p.max_age_days;
  ['computrabajo', 'elempleo', 'indeed'].forEach((k) => (f['portal_' + k].checked = !!(p.portals || {})[k]));
  f.use_builtin_rules.checked = !!p.use_builtin_rules;
  f.roles.value = (p.roles || []).join(', ');
  f.exclude.value = (p.exclude || []).join(', ');
  f.skills.value = (p.skills || []).join(', ');
  f.skills_plus.value = (p.skills_plus || []).join(', ');
  f.max_years_experience.value = p.max_years_experience;
  f.min_score.value = p.min_score;
  f.exclude_senior.checked = !!p.exclude_senior;
  $('btnDeleteProfile').disabled = p.id === 'default' || !p.id;
  toggleBuiltin();
  $('profileFormTitle').textContent = p.id ? p.name || 'Editar perfil' : 'Perfil nuevo';
  const tag = $('profileFormTag');
  tag.textContent = p.id === 'default' ? 'El perfil original no se puede borrar'
    : p.id === profilesData.active ? 'En uso ahora' : '';
  tag.style.display = tag.textContent ? '' : 'none';
  toggleQueries();
}

function readForm() {
  const f = $('profileForm');
  return {
    id: f.id.value || undefined,
    name: f.name.value,
    use_default_queries: f.use_default_queries.checked,
    queries: f.queries.value,
    city: f.city.value,
    location_mode: f.location_mode.value,
    max_pages: f.max_pages.value,
    max_age_days: f.max_age_days.value,
    portals: { computrabajo: f.portal_computrabajo.checked, elempleo: f.portal_elempleo.checked, indeed: f.portal_indeed.checked },
    use_builtin_rules: f.use_builtin_rules.checked,
    roles: f.roles.value,
    exclude: f.exclude.value,
    skills: f.skills.value,
    skills_plus: f.skills_plus.value,
    max_years_experience: f.max_years_experience.value,
    min_score: f.min_score.value,
    exclude_senior: f.exclude_senior.checked,
  };
}

function toggleQueries() {
  const on = $('profileForm').use_default_queries.checked;
  $('queriesBox').style.display = on ? 'none' : '';
  $('builtinQueriesBox').style.display = on ? '' : 'none';
}

function toggleBuiltin() {
  $('builtinRulesBox').style.display = $('profileForm').use_builtin_rules.checked ? '' : 'none';
}

// Lo que trae el radar de fábrica: consultas y filtros, para poder verlos desde la app.
async function loadBuiltin() {
  try { builtin = await api('/api/profiles/defaults'); } catch (_) { return; }
  const q = builtin.default_queries || [];
  $('bqCount').textContent = q.length;
  $('bqList').innerHTML = q.map((x) => chip('search', x)).join('');

  const r = builtin.builtin_rules || {};
  const group = (titulo, nota, items, icono) => !items || !items.length ? '' : `
    <div class="mb-3">
      <div class="fw-semibold small">${esc(titulo)} <span class="text-body-secondary fw-normal">(${items.length})</span></div>
      <div class="form-text mb-1">${esc(nota)}</div>
      <div class="chips">${items.map((x) => chip(icono, x)).join('')}</div>
    </div>`;
  $('brList').innerHTML =
    group('Cargos que el radar considera afines', 'Si el título trae una de estas, la vacante suma 35 puntos.', r.roles, 'check2')
    + group('Palabras que descartan el título', 'Si el título trae una de estas, la vacante se bota de entrada.', r.exclude, 'x-lg')
    + group('Niveles que descarta', 'Cargos de nivel alto para un primer empleo.', r.senior, 'bar-chart-steps')
    + group('Frases que descartan la descripción', 'Contratos de aprendizaje, telemercadeo y similares.', r.body_exclude, 'x-lg')
    + group('Tecnologías principales', 'Suman 12 puntos cada una, hasta 45.', r.skills, 'cpu')
    + group('Tecnologías secundarias', 'Suman 5 puntos cada una, hasta 15.', r.skills_plus, 'cpu')
    + `<div class="form-text">Además: descarta si piden más de ${r.max_years_experience} años de experiencia,
       solo mira vacantes de los últimos ${r.max_age_days} días y pide ${r.min_score} puntos como mínimo para
       llamarla compatible. Esos tres sí los puedes cambiar aquí abajo.</div>`;
}

async function newProfile() {
  let d = builtin;
  if (!d) {
    try { d = await api('/api/profiles/defaults'); } catch (e) { return notify(e.message, 'bad'); }
  }
  editingId = '';  // '' = estoy editando uno nuevo (null = aún no elijo ninguno)
  // En blanco de verdad: sin las palabras clave ni los filtros del perfil original.
  fillForm({
    ...d.profile, id: '', name: '',
    use_default_queries: false, queries: [],
    use_builtin_rules: false, roles: [], exclude: [], skills: [], skills_plus: [],
  });
  renderProfileList();
  $('pName').focus();
  notify('Perfil en blanco: ponle nombre, escribe tus consultas y tus filtros, y guárdalo.');
}

function duplicateProfile() {
  const p = readForm();
  const split = (s) => String(s || '').split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
  editingId = '';
  fillForm({
    ...p, id: '', name: (p.name || 'Perfil') + ' (copia)',
    queries: split(p.queries), roles: split(p.roles), exclude: split(p.exclude),
    skills: split(p.skills), skills_plus: split(p.skills_plus),
  });
  renderProfileList();
  notify('Copia lista: cámbiale el nombre y guarda.');
}

async function saveProfile(activate) {
  const f = $('profileForm');
  if (!f.reportValidity()) return;
  try {
    const saved = await api('/api/profiles', { method: 'POST', body: readForm() });
    editingId = saved.id;
    if (activate || saved.id === profilesData.active) {
      await activateProfile(saved.id);
    } else {
      notify(`Perfil «${saved.name}» guardado.`, 'good');
      await loadProfiles();
    }
  } catch (e) { notify(e.message, 'bad'); }
}

async function activateProfile(id) {
  try {
    const r = await api(`/api/profiles/${encodeURIComponent(id)}/activate`, { method: 'POST' });
    const ev = r.reevaluated;
    notify(`Ahora el radar usa «${r.active.name}».${ev ? ` Volví a puntuar ${ev.evaluadas} vacantes: ${ev.compatibles} compatibles y ${ev.descartadas} descartadas.` : ''}`, 'good');
  } catch (e) { notify(e.message, 'bad'); }
  await loadProfiles();
  await loadOffers();
}

async function deleteProfile() {
  const id = $('profileForm').id.value;
  const name = $('profileForm').name.value;
  if (!id || !confirm(`¿Borrar el perfil «${name}»? Las vacantes no se borran.`)) return;
  try {
    await api(`/api/profiles/${encodeURIComponent(id)}`, { method: 'DELETE' });
    editingId = null;
    $('profileForm').name.value = '';
    notify('Perfil borrado.', 'good');
    await loadProfiles();
    await loadOffers();
  } catch (e) { notify(e.message, 'bad'); }
}

// ---------- herramientas ----------
function loadTools() {
  loadGuard();
  loadReport();
  loadSkills();
  loadLog();
  if (!qaEntries.length) api('/api/qa/entries').then((e) => { qaEntries = e; qaSearch(); }).catch(() => {});
}

const REPORT_LABELS = {
  paginas: 'páginas', vistas: 'vacantes vistas', ya_conocidas: 'ya estaban', viejas: 'fuera de fecha',
  titulo_no_afin: 'cargo no afín', no_remotas: 'no remotas', nuevas: 'nuevas',
  guardadas: 'guardadas', compatibles: 'compatibles', descartadas_filtro: 'descartadas por el filtro',
};

async function loadReport() {
  let r;
  try { r = await api('/api/sync/report'); } catch (_) { return; }
  if (!r || !r.portales) {
    $('reportBox').innerHTML = '<div class="text-body-secondary">Todavía no se ha completado un escaneo.</div>';
    return;
  }
  const portals = Object.entries(r.portales).map(([name, s]) => {
    const nums = Object.entries(REPORT_LABELS)
      .filter(([k]) => typeof s[k] === 'number')
      .map(([k, label]) => `<span class="chip"><b class="num">${s[k]}</b>${esc(label)}</span>`).join('');
    return `<div class="mb-2">
      <div class="fw-semibold">${esc(name)}</div>
      <div class="chips mt-1">${nums}</div>
      ${s.error ? `<div class="mt-1" style="color:var(--no)"><i class="bi bi-exclamation-triangle me-1"></i>${esc(s.error)}</div>` : ''}
    </div>`;
  }).join('');
  $('reportBox').innerHTML =
    line('calendar-check', `Terminó el ${fmtTime(r.finished_at)} con <b class="num">${r.compatibles_nuevas ?? 0}</b> nuevas compatibles.`)
    + portals
    + (r.error_general ? `<div style="color:var(--no)">${esc(r.error_general)}</div>` : '');
}

// Aviso del navegador al terminar un escaneo (el permiso lo da el usuario desde Herramientas).
function notifyDesktop(text) {
  try {
    if (window.Notification && Notification.permission === 'granted') {
      new Notification('Radar de Empleo', { body: text });
    }
  } catch (_) {}
}

async function askNotifications() {
  if (!window.Notification) return notify('Este navegador no tiene avisos de escritorio.', 'bad');
  const p = await Notification.requestPermission();
  renderNotifButton();
  notify(p === 'granted' ? 'Listo: te aviso cuando termine un escaneo.' : 'El navegador no dio permiso para los avisos.',
    p === 'granted' ? 'good' : 'bad');
}

function renderNotifButton() {
  const b = $('notifBtn');
  if (!b) return;
  const p = window.Notification ? Notification.permission : 'denied';
  b.disabled = p !== 'default';
  b.innerHTML = p === 'granted'
    ? '<i class="bi bi-bell-fill me-1"></i>Avisos activados'
    : p === 'denied' ? '<i class="bi bi-bell-slash me-1"></i>Avisos bloqueados por el navegador'
    : '<i class="bi bi-bell me-1"></i>Avisarme al terminar un escaneo';
}

async function loadGuard() {
  const items = [];
  try {
    const [g, d, b] = await Promise.all([
      api('/api/guard/status'), api('/api/daily/status'), api('/api/browser/status').catch(() => null),
    ]);
    items.push(line(g.scan_running ? 'broadcast' : 'pause-circle',
      g.scan_running ? 'Hay un escaneo en curso.' : 'Sin escaneos en curso.'));
    Object.entries(g.portals || {}).forEach(([p, s]) => {
      if (s.blocked || s.needs_verification) {
        items.push(`<div class="d-flex align-items-start gap-2 mb-2" style="color:var(--no)">
          <i class="bi bi-shield-exclamation"></i>
          <span>${esc(p)}: ${s.needs_verification ? 'pide una verificación en el navegador'
            : 'en pausa hasta ' + fmtTime(s.until)}${s.reason ? ' (' + esc(s.reason) + ')' : ''}
          <button class="btn btn-sm btn-outline-secondary ms-2" onclick="clearGuard('${esc(p)}')">Levantar la pausa</button></span></div>`);
      }
    });
    items.push(line('clock-history', `Escaneo diario a las ${esc(d.time)}. Último completo: ${fmtTime(d.last_full_sync_at)}.`));
    if (b) items.push(line('window', `Navegador del radar: ${b.installed ? esc(b.browser) : 'no encontrado'}, ${b.running ? 'abierto' : 'cerrado'}.`));
  } catch (e) {
    items.push(`<div style="color:var(--no)">${esc(e.message)}</div>`);
  }
  $('guardStatus').innerHTML = items.join('');
}

function line(icon, text) {
  return `<div class="d-flex align-items-start gap-2 mb-2"><i class="bi bi-${icon} text-body-secondary"></i><span>${text}</span></div>`;
}

async function clearGuard(portal) {
  if (!confirm(`¿Levantar la pausa de ${portal}? Hazlo solo si el portal ya abre bien en tu navegador.`)) return;
  try { await api('/api/guard/clear?portal=' + encodeURIComponent(portal), { method: 'POST' }); loadGuard(); }
  catch (e) { notify(e.message, 'bad'); }
}

async function openBrowser() {
  try { notify((await api('/api/browser/open', { method: 'POST', body: {} })).message); }
  catch (e) { notify(e.message, 'bad'); }
}

async function syncApplications() {
  notify('Leyendo tus postulaciones en Computrabajo…');
  try {
    const r = await api('/api/browser/sync-applications', { method: 'POST' });
    notify(`Listo: ${r.synced ?? 0} postulaciones marcadas de ${r.received ?? r.found ?? 0} encontradas.`, 'good');
    loadOffers();
  } catch (e) { notify(e.message, 'bad'); }
}

async function reevaluate() {
  try {
    const r = await api('/api/profiles/reevaluate', { method: 'POST' });
    notify(`Volví a puntuar ${r.evaluadas} vacantes: ${r.compatibles} compatibles y ${r.descartadas} descartadas.`, 'good');
    loadOffers();
  } catch (e) { notify(e.message, 'bad'); }
}

async function loadSkills() {
  try {
    const d = await api('/api/skills-gap?scope=' + $('skillsScope').value);
    $('skillsBody').innerHTML = d.skills.slice(0, 30).map((s) => `
      <tr>
        <td>${esc(s.skill)}</td>
        <td class="text-end num text-body-secondary">${s.count}</td>
        <td class="text-end num text-body-secondary">${s.pct}</td>
        <td>${s.have
          ? `<span class="chip"><i class="bi bi-check2"></i>${esc(s.level || 'sí')}</span>`
          : '<span class="chip chip-state"><i class="bi bi-dash-lg"></i>por aprender</span>'}</td>
      </tr>`).join('')
      || '<tr><td colspan="4"><div class="empty"><i class="bi bi-mortarboard"></i>Sin datos todavía: hace falta al menos una vacante.</div></td></tr>';
  } catch (e) { notify(e.message, 'bad'); }
}

async function qaSearch() {
  const q = $('qaInput').value.trim();
  if (q.length > 8) {
    try {
      $('qaList').innerHTML = qaCard(await api('/api/qa/match', { method: 'POST', body: { question: q } }));
    } catch (_) {}
    return;
  }
  $('qaList').innerHTML = qaEntries.map(qaCard).join('');
}

function qaCard(e) {
  return `<div class="filters mb-2">
    <div class="d-flex justify-content-between align-items-center mb-1">
      <b class="small">${esc(e.category)}</b>
      <button class="icon-btn" title="Copiar la respuesta" aria-label="Copiar la respuesta"
        onclick="copyText(this.closest('.filters').querySelector('.qa-text').textContent, this)"><i class="bi bi-clipboard"></i></button>
    </div>
    <div class="qa-text small text-body-secondary">${esc(e.answer)}</div>
  </div>`;
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    btn.innerHTML = '<i class="bi bi-clipboard-check"></i>';
    setTimeout(() => (btn.innerHTML = '<i class="bi bi-clipboard"></i>'), 1500);
  }).catch(() => notify('El navegador no dejó copiar.', 'bad'));
}

async function loadLog() {
  try { $('log').textContent = (await api('/api/log?lines=120')).lines.join('\n'); } catch (_) {}
}

// ---------- arranque ----------
$('offersList').addEventListener('click', (ev) => {
  const detail = ev.target.closest('[data-detail]');
  if (detail) { ev.preventDefault(); return showDetail(detail.dataset.detail); }
  const act = ev.target.closest('[data-act]');
  if (act) setStatus(act.dataset.id, act.dataset.act);
});

$('dActions').addEventListener('click', (ev) => {
  const act = ev.target.closest('[data-act]');
  if (act) setStatus(act.dataset.id, act.dataset.act);
});

$('profileList').addEventListener('click', (ev) => {
  const item = ev.target.closest('[data-profile]');
  if (item) editProfile(item.dataset.profile);
});

$('activeProfile').addEventListener('change', (e) => {
  if (confirm('¿Usar este perfil? Se vuelven a puntuar las vacantes con sus filtros.')) activateProfile(e.target.value);
  else e.target.value = profilesData.active;
});

renderNotifButton();
restoreFilters();
showScanDays();
initTips();
loadBuiltin();
loadProfiles().catch((e) => notify(e.message, 'bad'));
loadOffers();
pollSync();
