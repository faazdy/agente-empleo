// Radar de Empleo — front simple (Bootstrap + JS sin frameworks).

let offers = [];
let profilesData = { active: null, profiles: [] };
let editingId = null;
let qaEntries = [];
let pollTimer = null;

const STATUS_LABELS = { matched: 'Compatible', favorite: 'Guardada', applied: 'Postulada', discarded: 'Descartada', new: 'Nueva' };

// ---------- utilidades ----------
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function norm(s) {
  return String(s ?? '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
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

function notify(text, kind = 'info') {
  const div = document.createElement('div');
  div.className = `alert alert-${kind} alert-dismissible py-2 small`;
  div.innerHTML = `${esc(text)}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`;
  $('msg').prepend(div);
  setTimeout(() => div.remove(), 8000);
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleDateString('es-CO', { day: '2-digit', month: 'short' });
}

function saveFilters() {
  const f = {};
  ['fStatus', 'fText', 'fPortal', 'fModality', 'fMinScore', 'fExclude', 'fSort'].forEach((id) => (f[id] = $(id).value));
  try { localStorage.setItem('radar.filters', JSON.stringify(f)); } catch (_) {}
}

function restoreFilters() {
  try {
    const f = JSON.parse(localStorage.getItem('radar.filters') || '{}');
    Object.entries(f).forEach(([id, v]) => { if ($(id)) $(id).value = v; });
  } catch (_) {}
}

function clearFilters() {
  ['fText', 'fPortal', 'fModality', 'fMinScore', 'fExclude'].forEach((id) => ($(id).value = ''));
  renderOffers();
}

// ---------- vacantes ----------
async function loadOffers() {
  const status = $('fStatus').value;
  const sort = $('fSort').value;
  try {
    offers = await api(`/api/offers?status=${encodeURIComponent(status)}&sort_by=${encodeURIComponent(sort)}`);
  } catch (e) {
    notify('No se pudieron cargar las vacantes: ' + e.message, 'danger');
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

function renderOffers() {
  saveFilters();
  const list = filteredOffers();
  $('count').textContent = list.length;
  $('offersBody').innerHTML = list.map((o) => `
    <tr>
      <td>${o.match_score != null ? Math.round(o.match_score) : '-'}</td>
      <td class="wrap"><a href="#" onclick="showDetail('${o.id}');return false;">${esc(o.title)}</a>
        ${o.status !== $('fStatus').value && $('fStatus').value === 'all' ? `<span class="badge text-bg-light">${STATUS_LABELS[o.status] || o.status}</span>` : ''}</td>
      <td>${esc(o.company)}</td>
      <td>${esc(o.portal)}</td>
      <td>${esc(o.location || '')}${o.modality ? ' · ' + esc(o.modality) : ''}</td>
      <td class="text-nowrap">${esc(o.posted_date || fmtDate(o.scraped_at))}</td>
      <td class="text-nowrap">${actionButtons(o, true)}</td>
    </tr>`).join('') || '<tr><td colspan="7" class="text-muted">No hay vacantes con estos filtros.</td></tr>';
}

function actionButtons(o, small) {
  const sz = small ? 'btn-sm' : '';
  const b = [`<a class="btn ${sz} btn-outline-primary" href="${esc(o.url)}" target="_blank" rel="noreferrer noopener">Abrir</a>`];
  if (o.status !== 'applied') b.push(`<button class="btn ${sz} btn-outline-success" onclick="setStatus('${o.id}','applied')">Postulé</button>`);
  if (o.status !== 'favorite' && o.status !== 'applied') b.push(`<button class="btn ${sz} btn-outline-secondary" onclick="setStatus('${o.id}','favorite')">Guardar</button>`);
  if (o.status !== 'discarded') b.push(`<button class="btn ${sz} btn-outline-danger" onclick="setStatus('${o.id}','discarded')">Descartar</button>`);
  if (o.status === 'discarded' || o.status === 'favorite' || o.status === 'applied') b.push(`<button class="btn ${sz} btn-outline-secondary" onclick="setStatus('${o.id}','matched')">Volver a compatibles</button>`);
  return `<div class="btn-group">${b.join('')}</div>`;
}

function showDetail(id) {
  const o = offers.find((x) => x.id === id);
  if (!o) return;
  $('dTitle').textContent = o.title;
  $('dMeta').textContent = [o.company, o.portal, o.location, o.modality, o.salary, o.posted_date].filter(Boolean).join(' · ');
  $('dReason').textContent = `${o.match_score != null ? Math.round(o.match_score) + '% — ' : ''}${o.match_reason || ''}`;
  $('dReqs').textContent = (o.requirements || []).join(', ') || '—';
  $('dDesc').textContent = o.description || '';
  $('dActions').innerHTML = actionButtons(o, false);
  bootstrap.Modal.getOrCreateInstance($('detailModal')).show();
}

async function setStatus(id, status) {
  if (status === 'applied' && !confirm('¿Confirmas que ya te postulaste a esta vacante?')) return;
  try {
    await api(`/api/offers/${id}/status`, { method: 'POST', body: { status } });
    bootstrap.Modal.getInstance($('detailModal'))?.hide();
    await loadOffers();
  } catch (e) { notify(e.message, 'danger'); }
}

async function loadStats() {
  try {
    const s = await api('/api/stats');
    $('stats').textContent = `compatibles ${s.matched || 0} · guardadas ${s.favorite || 0} · postuladas ${s.applied || 0} · descartadas ${s.discarded || 0} · total ${s.total || 0}`;
  } catch (_) {}
}

// ---------- escaneo ----------
async function scan(portal) {
  try {
    const r = await api('/api/sync?portal=' + encodeURIComponent(portal), { method: 'POST' });
    notify(r.message);
    pollSync();
  } catch (e) { notify(e.message, 'danger'); }
}

async function quickScan() {
  try {
    const r = await api('/api/alert/scan-now', { method: 'POST' });
    notify(r.message, r.success ? 'info' : 'warning');
  } catch (e) { notify(e.message, 'danger'); }
}

async function pollSync() {
  clearTimeout(pollTimer);
  let st;
  try { st = await api('/api/sync/status'); } catch (_) { return; }
  const box = $('syncInfo');
  if (st.is_running) {
    box.classList.remove('d-none');
    box.textContent = `Escaneando${st.profile ? ' (' + st.profile + ')' : ''}: ${st.current_query} — consulta ${st.current_index}/${st.total_queries} · ${st.new_offers} nuevas compatibles${st.note ? ' · ' + st.note : ''}`;
    pollTimer = setTimeout(pollSync, 3000);
  } else if (box.dataset.wasRunning === '1') {
    box.textContent = `Escaneo terminado: ${st.new_offers} nuevas compatibles.${st.last_error ? ' Avisos: ' + st.last_error : ''}`;
    loadOffers();
  } else {
    box.classList.add('d-none');
  }
  box.dataset.wasRunning = st.is_running ? '1' : '0';
}

// ---------- postuladas ----------
async function loadTracking() {
  let t, list;
  try {
    [t, list] = await Promise.all([api('/api/tracking'), api('/api/offers?status=applied&sort_by=recent')]);
  } catch (e) { notify(e.message, 'danger'); return; }
  const stages = Object.entries(t.by_stage).filter(([, n]) => n).map(([k, n]) => `${t.stage_labels[k]}: ${n}`).join(' · ');
  $('trackingSummary').textContent = `${t.total} postulaciones · tasa de respuesta ${t.response_rate}% · ${t.stale_count} sin respuesta hace más de ${t.stale_days} días${stages ? ' · ' + stages : ''}`;
  list.sort((a, b) => (b.applied_at || '').localeCompare(a.applied_at || ''));
  $('trackingBody').innerHTML = list.map((o) => `
    <tr>
      <td class="wrap"><a href="${esc(o.url)}" target="_blank" rel="noreferrer noopener">${esc(o.title)}</a></td>
      <td>${esc(o.company)}</td>
      <td>${esc(o.portal)}</td>
      <td class="text-nowrap">${fmtDate(o.applied_at)}</td>
      <td><select class="form-select form-select-sm" onchange="saveTracking('${o.id}', {stage: this.value})">
        ${Object.entries(t.stage_labels).map(([k, v]) => `<option value="${k}" ${(o.stage || 'postulado') === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}
      </select></td>
      <td><input class="form-control form-control-sm" value="${esc(o.notes || '')}" onchange="saveTracking('${o.id}', {notes: this.value})"></td>
    </tr>`).join('') || '<tr><td colspan="6" class="text-muted">Aún no hay postulaciones.</td></tr>';
}

async function saveTracking(id, body) {
  try { await api(`/api/offers/${id}/tracking`, { method: 'POST', body }); }
  catch (e) { notify(e.message, 'danger'); }
}

// ---------- perfiles ----------
async function loadProfiles() {
  profilesData = await api('/api/profiles');
  $('activeProfile').innerHTML = profilesData.profiles
    .map((p) => `<option value="${esc(p.id)}" ${p.id === profilesData.active ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
  renderProfileList();
}

function renderProfileList() {
  $('profileList').innerHTML = profilesData.profiles.map((p) => `
    <button type="button" class="list-group-item list-group-item-action d-flex justify-content-between ${p.id === editingId ? 'active' : ''}" onclick="editProfile('${esc(p.id)}')">
      <span>${esc(p.name)}</span>${p.id === profilesData.active ? '<span class="badge text-bg-success">activo</span>' : ''}
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
  $('queriesBox').style.display = $('profileForm').use_default_queries.checked ? 'none' : '';
}

async function newProfile() {
  const d = await api('/api/profiles/defaults');
  editingId = null;
  fillForm({ ...d.profile, id: '', name: 'Nuevo perfil', use_default_queries: false, queries: [] });
  $('profileForm').name.focus();
}

function duplicateProfile() {
  const p = readForm();
  editingId = null;
  fillForm({ ...p, id: '', name: p.name + ' (copia)',
    queries: p.queries.split('\n'), roles: p.roles.split(','), exclude: p.exclude.split(','),
    skills: p.skills.split(','), skills_plus: p.skills_plus.split(',') });
  notify('Copia lista: cambia el nombre y guarda.');
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
      notify(`Perfil "${saved.name}" guardado.`, 'success');
      await loadProfiles();
    }
  } catch (e) { notify(e.message, 'danger'); }
}

async function activateProfile(id) {
  try {
    const r = await api(`/api/profiles/${encodeURIComponent(id)}/activate`, { method: 'POST' });
    const ev = r.reevaluated;
    notify(`Perfil activo: ${r.active.name}.${ev ? ` Reevaluadas ${ev.evaluadas}: ${ev.compatibles} compatibles, ${ev.descartadas} descartadas.` : ''}`, 'success');
  } catch (e) { notify(e.message, 'danger'); }
  await loadProfiles();
  await loadOffers();
}

async function deleteProfile() {
  const id = $('profileForm').id.value;
  if (!id || !confirm('¿Borrar este perfil?')) return;
  try {
    await api(`/api/profiles/${encodeURIComponent(id)}`, { method: 'DELETE' });
    editingId = null;
    $('profileForm').name.value = '';
    notify('Perfil borrado.');
    await loadProfiles();
    await loadOffers();
  } catch (e) { notify(e.message, 'danger'); }
}

// ---------- herramientas ----------
function loadTools() {
  loadGuard();
  loadSkills();
  loadLog();
  if (!qaEntries.length) api('/api/qa/entries').then((e) => { qaEntries = e; qaSearch(); }).catch(() => {});
}

async function loadGuard() {
  const items = [];
  try {
    const [g, a, d, b] = await Promise.all([
      api('/api/guard/status'), api('/api/alert/status'), api('/api/daily/status'),
      api('/api/browser/status').catch(() => null),
    ]);
    items.push(`<li>Escaneo en curso: ${g.scan_running ? 'sí' : 'no'} · horario de alerta: ${g.alert_window_open ? 'abierto' : 'cerrado'}</li>`);
    Object.entries(g.portals || {}).forEach(([p, s]) => {
      if (s.blocked || s.needs_verification) {
        items.push(`<li class="text-danger">${esc(p)}: ${s.needs_verification ? 'necesita verificación' : 'en pausa hasta ' + new Date(s.until).toLocaleString('es-CO')} ${s.reason ? '(' + esc(s.reason) + ')' : ''}
          <button class="btn btn-link btn-sm p-0" onclick="clearGuard('${esc(p)}')">levantar pausa</button></li>`);
      }
    });
    items.push(`<li>Modo alerta: último ${a.last_scan_at ? new Date(a.last_scan_at).toLocaleTimeString('es-CO') : '—'}, próximo ${a.next_scan_at ? new Date(a.next_scan_at).toLocaleTimeString('es-CO') : '—'}, hoy ${a.total_found_today || 0} nuevas${a.last_error ? ' · ' + esc(a.last_error) : ''}</li>`);
    items.push(`<li>Sincronización diaria (${esc(d.time)}): última ${d.last_full_sync_at ? new Date(d.last_full_sync_at).toLocaleString('es-CO') : '—'}</li>`);
    if (b) items.push(`<li>Navegador del radar: ${b.installed ? esc(b.browser) : 'no encontrado'} · ${b.running ? 'abierto' : 'cerrado'}</li>`);
  } catch (e) { items.push(`<li class="text-danger">${esc(e.message)}</li>`); }
  $('guardStatus').innerHTML = items.join('');
}

async function clearGuard(portal) {
  if (!confirm(`¿Levantar la pausa de ${portal}? Hazlo solo si el portal ya abre bien en tu navegador.`)) return;
  try { await api('/api/guard/clear?portal=' + encodeURIComponent(portal), { method: 'POST' }); loadGuard(); }
  catch (e) { notify(e.message, 'danger'); }
}

async function openBrowser() {
  try { notify((await api('/api/browser/open', { method: 'POST', body: {} })).message); }
  catch (e) { notify(e.message, 'danger'); }
}

async function syncApplications() {
  notify('Leyendo tus postulaciones de Computrabajo…');
  try {
    const r = await api('/api/browser/sync-applications', { method: 'POST' });
    notify(`Postulaciones sincronizadas: ${JSON.stringify(r)}`, 'success');
    loadOffers();
  } catch (e) { notify(e.message, 'danger'); }
}

async function reevaluate() {
  try {
    const r = await api('/api/profiles/reevaluate', { method: 'POST' });
    notify(`Reevaluadas ${r.evaluadas}: ${r.compatibles} compatibles, ${r.descartadas} descartadas.`, 'success');
    loadOffers();
  } catch (e) { notify(e.message, 'danger'); }
}

async function loadSkills() {
  try {
    const d = await api('/api/skills-gap?scope=' + $('skillsScope').value);
    $('skillsBody').innerHTML = d.skills.slice(0, 30).map((s) =>
      `<tr><td>${esc(s.skill)}</td><td>${s.count}</td><td>${s.pct}</td><td>${s.have ? 'sí' + (s.level ? ' (' + esc(s.level) + ')' : '') : '<b>no</b>'}</td></tr>`).join('')
      || '<tr><td colspan="4" class="text-muted">Sin datos.</td></tr>';
  } catch (e) { notify(e.message, 'danger'); }
}

async function qaSearch() {
  const q = $('qaInput').value.trim();
  if (q.length > 8) {
    try {
      const m = await api('/api/qa/match', { method: 'POST', body: { question: q } });
      $('qaList').innerHTML = qaCard(m);
    } catch (_) {}
    return;
  }
  $('qaList').innerHTML = qaEntries.map(qaCard).join('');
}

function qaCard(e) {
  return `<div class="border rounded p-2 mb-2"><b>${esc(e.category)}</b> <span class="text-muted">(${e.length} car.)</span>
    <button class="btn btn-link btn-sm p-0 float-end" onclick="navigator.clipboard.writeText(this.nextElementSibling.textContent)">copiar</button>
    <div>${esc(e.answer)}</div></div>`;
}

async function loadLog() {
  try { $('log').textContent = (await api('/api/log?lines=120')).lines.join('\n'); } catch (_) {}
}

// ---------- arranque ----------
$('activeProfile').addEventListener('change', (e) => {
  if (confirm('¿Activar este perfil? Se vuelven a puntuar las vacantes con sus filtros.')) activateProfile(e.target.value);
  else e.target.value = profilesData.active;
});

restoreFilters();
loadProfiles().catch((e) => notify(e.message, 'danger'));
loadOffers();
pollSync();
