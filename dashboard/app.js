'use strict';

const $ = selector => document.querySelector(selector);
const labels = {saved: 'Saved', applied: 'Applied', interview: 'Interviewing', offer: 'Offer', rejected: 'Rejected', archived: 'Archived'};
let token = '';
let boot = null;
let report = {matches: [], dropped: [], counts: {}, metadata: {}};
let applications = [];
let selectedJob = null;
let lastFocus = null;
let matchLimit = 60;
let applicationLimit = 60;
let pollTimer = null;
let setupStep = 0;
let setupComplete = false;
let chosenRoles = new Set();
let chosenSkills = new Map();
const authorizationLabels = {opt: 'OPT', stem_opt: 'STEM OPT', us_citizen: 'U.S. citizen', permanent_resident: 'Permanent resident', other: 'Other / needs sponsorship'};

const sourceNames = {ashby: 'Ashby', greenhouse: 'Greenhouse', lever: 'Lever', workday: 'Workday',
  amazon: 'Amazon Jobs', eightfold: 'Eightfold'};
const menus = new Map();
let openMenu = null;
let noticeTimer = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', `#i-${name}`); svg.append(use);
  return svg;
}

// Errors stay until dismissed; confirmations clear themselves. An action (such
// as Undo) keeps a confirmation up a little longer.
function notice(message, transient = false, action = null) {
  clearTimeout(noticeTimer);
  $('#notice-text').textContent = message;
  $('#notice').hidden = !message;
  const button = $('#notice-action');
  button.hidden = !action;
  button.textContent = action ? action.label : '';
  button.onclick = action ? () => { notice(''); action.run(); } : null;
  if (message && transient) noticeTimer = setTimeout(() => { $('#notice').hidden = true; }, action ? 8000 : 5000);
}

function success(message) { notice(message, true); }

function capitalize(text) { return text ? text[0].toUpperCase() + text.slice(1) : ''; }

function plural(count, one, many) { return `${count.toLocaleString()} ${count === 1 ? one : many}`; }

function needsSponsorship() {
  return Boolean(boot?.profile) && !['us_citizen', 'permanent_resident'].includes(boot.profile.work_authorization);
}

// Filing columns only help when sponsorship matters and filing data is loaded;
// the toolbar switch can hide them from the rows even then.
function filingsAvailable() { return needsSponsorship() && Boolean(boot?.data.employers); }
let filingsVisible = true;
try { filingsVisible = localStorage.getItem('sponsorscan-filings-column') !== 'hidden'; } catch { /* default to shown */ }
function showFilings() { return filingsAvailable() && filingsVisible; }

function permLoaded() { return Boolean(report.metadata?.perm_loaded); }

async function api(path, data) {
  let response;
  try {
    response = await fetch(path, data === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-SponsorScan-Token': token},
      body: JSON.stringify(data)
    });
  } catch {
    $('#connection').textContent = 'Disconnected';
    $('#connection').classList.remove('connected');
    throw new Error('The local server is not responding. Start python sponsorscan.py dashboard again, then reload this page.');
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The request failed. Please refresh and try again.');
  return result;
}

async function busy(button, action, errorElement = null) {
  button.disabled = true;
  try { await action(); }
  catch (error) {
    if (errorElement) errorElement.textContent = error.message;
    else notice(error.message);
  } finally { button.disabled = false; }
}

function showView() {
  let name = location.hash.slice(1) || 'matches';
  if (!['matches', 'applications', 'integrations', 'profile'].includes(name)) name = 'matches';
  if (boot && !boot.profile) name = 'profile';
  if (name !== 'profile' && setupComplete) {
    setupComplete = false;
    $('#setup-complete').hidden = true;
    $('#profile-form').hidden = false;
    profileHeading();
  }
  document.querySelectorAll('.view').forEach(view => { view.hidden = view.id !== `view-${name}`; });
  if (name === 'integrations') loadIntegrations();
  document.querySelectorAll('[data-view]').forEach(link => {
    if (link.dataset.view === name) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

function dateLabel(value) {
  if (!value) return 'Date unavailable';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'});
}

function shortDate(value) {
  if (!value) return 'No date';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const options = {month: 'short', day: 'numeric'};
  if (date.getFullYear() !== new Date().getFullYear()) options.year = 'numeric';
  return date.toLocaleDateString(undefined, options);
}

function fetchedLabel(value) {
  // fetch-jobs stores local time as "YYYY-MM-DD HH:MM", which not every browser parses.
  const date = new Date(String(value || '').replace(' ', 'T'));
  if (!value || Number.isNaN(date.getTime())) return 'at an unknown time';
  const time = date.toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'});
  return date.toDateString() === new Date().toDateString() ? `today at ${time}` : `${shortDate(date)} at ${time}`;
}

function sourceLabel(source) { return sourceNames[source] || capitalize(source || ''); }

function percent(share) { return share === '' || share === null || share === undefined ? 'Not recorded' : `${Math.round(Number(share) * 100)}%`; }

function linkURL(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}

function emptyState(title, message) {
  const empty = node('div', 'empty');
  empty.append(node('h2', '', title), node('p', '', message));
  return empty;
}

function postedTime(job) { return Date.parse(job.posted) || 0; }

// Fit sorts by the profile fit shown in each row, not the hidden combined score.
const matchSorters = {
  recent: (a, b) => postedTime(b) - postedTime(a),
  oldest: (a, b) => postedTime(a) - postedTime(b),
  fit: (a, b) => (b.resume_fit_score || 0) - (a.resume_fit_score || 0),
  'fit-low': (a, b) => (a.resume_fit_score || 0) - (b.resume_fit_score || 0),
  filings: (a, b) => (b.lca_certified || 0) - (a.lca_certified || 0),
};
// Header clicks: the first click takes the first order, a second click flips it.
const headerSorts = {Fit: ['fit', 'fit-low'], Posted: ['recent', 'oldest'], 'H-1B filings': ['filings', 'filings']};

function setSort(value) {
  $('#match-sort').value = value;
  menus.get($('#match-sort'))?.refresh();
  matchLimit = 60; renderMatches();
}

function headerCell(text) {
  const orders = headerSorts[text];
  if (!orders) return node('span', '', text);
  const current = $('#match-sort').value;
  const active = orders.includes(current);
  const button = node('button', active ? 'sort-head active' : 'sort-head', text); button.type = 'button';
  if (active) button.append(icon(current === orders[1] && orders[0] !== orders[1] ? 'arrow-up' : 'arrow-down'));
  const next = active && current === orders[0] ? orders[1] : orders[0];
  const label = {recent: 'newest first', oldest: 'oldest first', fit: 'highest fit first', 'fit-low': 'lowest fit first', filings: 'most filings first'}[next];
  button.setAttribute('aria-label', `Sort by ${text.toLowerCase()}, ${label}`);
  button.addEventListener('click', () => setSort(next));
  return button;
}

function filteredMatches() {
  const term = $('#match-search').value.trim().toLowerCase();
  const family = $('#role-filter').value;
  const rows = report.matches.filter(job => (!family || job.role_family === family) &&
    [job.title, job.company, job.location].join(' ').toLowerCase().includes(term));
  // Sorting is stable, so ties keep the report's own order (newest, then score).
  const sorter = matchSorters[$('#match-sort').value];
  if (sorter) rows.sort(sorter);
  return rows;
}

function filteredApplications() {
  const term = $('#application-search').value.trim().toLowerCase();
  const status = $('#status-filter').value;
  return applications.filter(job => (!status || job.status === status) &&
    [job.title, job.company, job.location, job.notes].join(' ').toLowerCase().includes(term));
}

// A fixed set of hues keeps every tile clear and readable; a company always
// gets the same one.
const tileHues = [255, 155, 35, 330, 200, 80, 290, 120, 5, 230];

function paintTile(tile, company) {
  const name = (company || '?').trim();
  let hash = 0;
  for (const char of name.toLowerCase()) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  tile.textContent = name.slice(0, 1).toUpperCase();
  tile.classList.remove('has-logo');
  tile.style.setProperty('--hue', String(tileHues[hash % tileHues.length]));
  // The letter stays until the company's logo loads, and stays for good if it never does.
  const source = boot?.logos?.[name];
  if (source) {
    const image = node('img'); image.alt = ''; image.decoding = 'async';
    image.addEventListener('load', () => {
      // A wordmark is wider than tall: fit it inside the tile instead of cropping it.
      const ratio = image.naturalWidth / (image.naturalHeight || 1);
      tile.classList.toggle('wide-logo', ratio > 1.25 || ratio < 0.8);
      tile.classList.add('has-logo');
    }, {once: true});
    image.addEventListener('error', () => image.remove(), {once: true});
    image.src = source; tile.append(image);
  }
}

function logo(company, className = 'logo') {
  const tile = node('span', className);
  paintTile(tile, company);
  tile.setAttribute('aria-hidden', 'true');
  return tile;
}

function cell(label, ...content) {
  const element = node('div', 'cell');
  element.append(node('span', 'cell-label', label), ...content);
  return element;
}

// Each employer's bars share its own peak, so the shape shows the trend and the
// number beside them shows the size.
function filingBars(series) {
  const bars = node('span', 'bars'); bars.setAttribute('aria-hidden', 'true');
  const peak = Math.max(...series, 0);
  for (const count of series) {
    const bar = node('i', count ? '' : 'zero');
    if (count) bar.style.height = `${Math.max(12, Math.round(count / peak * 100))}%`;
    bars.append(bar);
  }
  return bars;
}

// What the matcher found beyond sponsorship: freshness, the experience the
// posting asks for, and which of your skills it mentions.
function jobTags(job) {
  const tags = node('div', 'job-tags');
  const posted = new Date(job.posted);
  if (!Number.isNaN(posted.getTime()) && Date.now() - posted.getTime() < 2 * 86400000) tags.append(node('span', 'tag new', 'New'));
  const years = job.experience_years_detected;
  if (years !== '' && years !== null && years !== undefined && Number.isFinite(Number(years))) {
    tags.append(node('span', 'tag', Number(years) === 0 ? 'No experience required' : `${years} yr${Number(years) === 1 ? '' : 's'} required`));
  }
  const skills = String(job.matched_resume_skills || '').split(',').map(skill => skill.trim()).filter(Boolean);
  for (const skill of skills.slice(0, 3)) tags.append(node('span', 'tag', skill));
  if (skills.length > 3) tags.append(node('span', 'tag', `+${skills.length - 3} more`));
  return tags.childElementCount ? tags : null;
}

function jobTitleButton(job, record, tags = null) {
  const main = node('div', 'job-main');
  const open = node('button', 'job-open', job.title); open.type = 'button';
  open.setAttribute('aria-label', `${job.title} at ${job.company}. Show details`);
  open.addEventListener('click', () => showJob(record || job));
  const sub = node('p', 'job-sub');
  const place = node('span', '', job.location || sourceLabel(job.source) || 'Location not listed');
  place.title = place.textContent;
  sub.append(node('span', 'job-company', job.company), place);
  main.append(open, sub);
  if (tags) main.append(tags);
  return main;
}

function postingLink(job) {
  const url = linkURL(job.url);
  if (!url) return null;
  const link = node('a', 'icon-button row-link');
  link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
  link.title = 'Open posting'; link.setAttribute('aria-label', `Open the posting for ${job.title} in a new tab`);
  link.append(icon('external'));
  link.addEventListener('click', () => rememberApply(job));
  return link;
}

async function removeRecord(record) {
  await api('/api/application', {job_key: record.job_key, remove: true});
  if ($('#job-dialog').open && selectedJob?.job_key === record.job_key) $('#job-dialog').close();
  await loadApplications();
  // Undo re-saves from the feed, so it is offered only while the job is still there.
  const undo = report.matches.some(job => job.job_key === record.job_key) ? {label: 'Undo', run: () =>
    api('/api/application', {job_key: record.job_key, status: record.status, notes: record.notes || ''})
      .then(loadApplications).catch(error => notice(error.message))} : null;
  notice(`Removed ${record.title} at ${record.company}.`, true, undo);
}

// Removing a record past Saved loses its history, so it takes a second click.
function armRemove(button, label, getRecord, confirmText = 'Confirm') {
  let timer = null;
  const original = label.textContent;
  const reset = () => { button.classList.remove('confirming'); label.textContent = original; };
  button.addEventListener('click', () => {
    if (!button.classList.contains('confirming')) {
      button.classList.add('confirming'); label.textContent = confirmText;
      clearTimeout(timer); timer = setTimeout(reset, 4000);
      return;
    }
    clearTimeout(timer); reset();
    busy(button, () => removeRecord(getRecord()));
  });
}

function matchRow(job) {
  const saved = applications.find(item => item.job_key === job.job_key);
  const row = node('article', 'job-row');
  const stats = node('div', 'job-stats');
  if (showFilings()) {
    const certified = Number(job.lca_certified) || 0;
    const quarters = report.metadata.lca_quarters || [];
    if (certified) {
      const count = node('span', 'cell-number', certified.toLocaleString());
      count.append(node('span', 'sr-only', quarters.length ? ` certified. By quarter from ${quarters[0]}: ${(job.lca_by_quarter || []).join(', ')}` : ' certified'));
      stats.append(cell('H-1B filings', ...((job.lca_by_quarter || []).length ? [filingBars(job.lca_by_quarter)] : []), count));
    } else stats.append(cell('H-1B filings', node('span', 'cell-none', 'None found')));
    if (permLoaded()) {
      const perm = Number(job.perm_certified) || 0;
      stats.append(cell('Green card', perm ? node('span', 'cell-number', perm.toLocaleString()) : node('span', 'cell-none', 'None found')));
    }
  }
  const fit = Math.max(0, Math.min(100, Number(job.resume_fit_score) || 0));
  const score = node('span', 'cell-number', fit); score.append(node('span', 'sr-only', ' out of 100'));
  const meter = node('span', 'meter'); meter.setAttribute('aria-hidden', 'true');
  const fill = node('i'); fill.style.width = `${fit}%`; meter.append(fill);
  const posted = cell('Posted', node('span', '', shortDate(job.posted))); posted.classList.add('posted');
  stats.append(cell('Fit', score, meter), posted);
  // The bookmark toggles a plain save. Once a job has moved past Saved, it opens
  // the details instead, where removal asks for confirmation.
  const save = node('button', saved ? 'save-button saved' : 'save-button');
  save.type = 'button';
  save.append(icon('bookmark'), node('span', '', saved ? labels[saved.status] : 'Save'));
  if (!saved) save.setAttribute('aria-label', `Save ${job.title}`);
  else if (saved.status === 'saved') { save.setAttribute('aria-label', `Unsave ${job.title}`); save.title = 'Unsave'; }
  else save.setAttribute('aria-label', `${labels[saved.status]}. Edit ${job.title}`);
  save.addEventListener('click', () => {
    if (saved && saved.status !== 'saved') { showJob(saved); return; }
    busy(save, async () => {
      if (saved) { await removeRecord(saved); return; }
      await api('/api/application', {job_key: job.job_key, status: 'saved'});
      await loadApplications();
      success(`Saved ${job.title} at ${job.company}.`);
    });
  });
  const actions = node('div', 'row-actions');
  actions.append(...[postingLink(job), save].filter(Boolean));
  row.append(logo(job.company), jobTitleButton(job, saved, jobTags(job)), stats, actions);
  return row;
}

function trackingRow(job) {
  const row = node('article', 'job-row');
  const stats = node('div', 'job-stats');
  stats.append(cell('Status', node('span', `status-badge ${job.status}`, labels[job.status])),
    cell('Updated', node('span', 'posted', `Updated ${shortDate(job.updated_at)}`)));
  const details = node('button', 'save-button', 'Details'); details.type = 'button';
  details.setAttribute('aria-label', `Edit ${job.title}`);
  details.addEventListener('click', () => showJob(job));
  const remove = node('button', 'save-button remove-button'); remove.type = 'button';
  const removeLabel = node('span', '', 'Remove');
  remove.append(icon('close'), removeLabel);
  remove.setAttribute('aria-label', `Remove ${job.title} from applications`);
  armRemove(remove, removeLabel, () => job);
  const actions = node('div', 'row-actions');
  actions.append(...[postingLink(job), details, remove].filter(Boolean));
  row.append(logo(job.company), jobTitleButton(job, job), stats, actions);
  return row;
}

function renderMatches() {
  const rows = filteredMatches();
  $('#results-label').textContent = rows.length > matchLimit ? `Showing ${matchLimit} of ${rows.length} roles` : plural(rows.length, 'role', 'roles');
  const filings = showFilings();
  $('#filings-toggle').hidden = !filingsAvailable();
  $('#filings-toggle').setAttribute('aria-checked', String(filingsVisible));
  $('#matches-card').classList.toggle('with-filings', filings);
  $('#matches-card').classList.toggle('with-perm', filings && permLoaded());
  const columns = ['', 'Role', ...(filings ? ['H-1B filings', ...(permLoaded() ? ['Green card'] : [])] : []), 'Fit', 'Posted', ''];
  $('#matches-head').replaceChildren(...(rows.length ? columns.map(headerCell) : []));
  const list = $('#matches-list');
  list.replaceChildren(...rows.slice(0, matchLimit).map(matchRow));
  if (!rows.length) {
    const message = report.message || (report.matches.length ? 'Try a different search or role filter.' :
      'Try a longer posting window or a lower minimum score in Profile. You can also fetch current jobs.');
    list.append(emptyState('No matching jobs here yet.', message));
  }
  if (rows.length > matchLimit) {
    const more = node('button', 'text-button show-more', 'Show more jobs');
    more.addEventListener('click', () => { matchLimit += 60; renderMatches(); });
    list.append(more);
  }
  $('#match-count').textContent = report.matches.length;
  $('#nav-matches').textContent = report.matches.length;
  const drops = report.dropped || [];
  $('#exclusions').hidden = !drops.length && !Object.values(report.counts || {}).some(Boolean);
  const excluded = $('#excluded-list');
  excluded.replaceChildren();
  const counts = report.counts || {};
  if (counts.posted_too_old || counts.posted_unknown || counts.role) {
    excluded.append(node('p', 'excluded-more', `${counts.posted_too_old || 0} outside your posting window; ${counts.posted_unknown || 0} without a posting date; ${counts.role || 0} outside your target roles.`));
  }
  drops.slice(0, 40).forEach(job => {
    const item = node('div', 'excluded-row', `${job.company} / ${job.title}`);
    item.append(node('span', '', job.reason)); excluded.append(item);
  });
  if (drops.length > 40) excluded.append(node('p', 'excluded-more', `Showing 40 of ${drops.length} excluded roles.`));
}

// A count that goes up gets a green +1 that floats off the number. The first
// render and decreases (an unsave) stay still.
const shownCounts = {};
function setCount(id, value) {
  const element = $(`#${id}`);
  const before = shownCounts[id];
  element.textContent = value; shownCounts[id] = value;
  if (before === undefined || value <= before) return;
  const bump = node('span', 'count-bump', `+${value - before}`); bump.setAttribute('aria-hidden', 'true');
  element.parentElement.append(bump);
  element.classList.remove('popped'); void element.offsetWidth; element.classList.add('popped');
  // Removed on a timer: with reduced motion there is no animation to end.
  setTimeout(() => bump.remove(), 1200);
}

function renderApplications() {
  const list = $('#applications-list');
  const rows = filteredApplications();
  list.replaceChildren(...rows.slice(0, applicationLimit).map(trackingRow));
  if (!rows.length) list.append(emptyState('Keep your search in one place.', applications.length ?
    'No applications match these filters.' : 'Save a match or add an application you made elsewhere. Your notes and statuses will stay here after job updates.'));
  if (rows.length > applicationLimit) {
    const more = node('button', 'text-button show-more', 'Show more applications');
    more.addEventListener('click', () => { applicationLimit += 60; renderApplications(); }); list.append(more);
  }
  setCount('saved-count', applications.filter(item => item.status === 'saved').length);
  setCount('applied-count', applications.filter(item => item.applied_at).length);
  $('#nav-applications').textContent = applications.filter(item => item.status !== 'archived').length;
}

async function loadApplications() {
  applications = (await api('/api/applications')).applications;
  renderApplications(); renderMatches();
}

function quarterChart(series, quarters) {
  const peak = Math.max(...series, 1);
  const chart = node('div', 'quarter-chart');
  chart.setAttribute('role', 'img');
  chart.setAttribute('aria-label', `Certified H-1B filings by quarter: ${quarters.map((name, index) => `${name}, ${series[index]}`).join('; ')}`);
  const labels = node('div', 'quarter-labels'); labels.setAttribute('aria-hidden', 'true');
  series.forEach((count, index) => {
    const column = node('div', 'quarter');
    const bar = node('i', count ? '' : 'zero');
    bar.style.setProperty('--h', String(count / peak));
    column.append(node('b', '', count.toLocaleString()), bar);
    chart.append(column);
    // "FY2026 Q1" reads as "Q1 FY26" under a narrow bar.
    labels.append(node('span', '', quarters[index].replace(/^FY\d\d(\d\d) (Q\d)$/, '$2 FY$1')));
  });
  const wrap = node('div'); wrap.append(chart, labels);
  return wrap;
}

function evidenceStat(label, value, detail) {
  const item = node('div');
  const dd = node('dd', '', value);
  if (detail) dd.append(node('small', '', detail));
  item.append(node('dt', '', label), dd);
  return item;
}

function renderSponsorship(job) {
  const quarters = report.metadata.lca_quarters || [];
  const series = job.lca_by_quarter || [];
  const certified = Number(job.lca_certified) || 0;
  const matched = Boolean(job.lca_employer);
  $('#detail-employer').textContent = matched ? `Filed as ${job.lca_employer}` : 'No filings matched this employer';
  $('#detail-chart').replaceChildren(...(matched && series.length && series.length === quarters.length ? [quarterChart(series, quarters)] : []));
  const stats = [];
  if (matched) {
    stats.push(evidenceStat('Certified H-1B filings', certified.toLocaleString(), job.lca_years || ''));
    stats.push(permLoaded() ? evidenceStat('Green card filings', (Number(job.perm_certified) || 0).toLocaleString(), 'PERM certified')
      : evidenceStat('Green card filings', 'Not loaded', 'No PERM data yet'));
    stats.push(evidenceStat('Placed at client sites', percent(job.lca_client_site_share), 'Share of filings'));
    stats.push(evidenceStat('For new hires', percent(job.lca_new_hire_share), 'Share of filings'));
  }
  $('#detail-stats').replaceChildren(...stats);
  $('#detail-stats').hidden = !stats.length;
}

function fitRow(label, points, max, note) {
  const row = node('div', 'fit-row');
  const head = node('div', 'fit-row-head');
  head.append(node('span', 'fit-label', label), node('span', 'fit-score', `${points}/${max}`));
  const bar = node('span', 'fit-bar'); bar.setAttribute('aria-hidden', 'true');
  const fill = node('i'); fill.style.width = `${max ? Math.min(100, points / max * 100) : 0}%`; bar.append(fill);
  row.append(head, bar, node('p', 'fit-note', note));
  return row;
}

// Profile fit is the role's base score plus skill points, capped at 100. The
// server sends those same parts, so they add up to the number in the row.
function renderFit(job) {
  const parts = job.fit_breakdown;
  $('#detail-fit').hidden = !parts;
  if (!parts) return;
  const {role, skills} = parts;
  $('#fit-total').textContent = `${job.resume_fit_score}/100`;
  const roleNote = role.points >= role.max ? `The title matches ${role.name}, your top-scoring target role.`
    : `The title matches ${role.name}. Your top-scoring target role would earn ${role.max}.`;
  const skillNote = skills.matched.length
    ? `Found in the posting: ${skills.matched.map(([name, value]) => `${name} (+${value})`).join(', ')}.`
    : `None of the ${plural(skills.listed, 'skill', 'skills')} in your profile appear in the posting.`;
  const capped = role.points + skills.points > 100 ? ' The total is capped at 100.' : '';
  $('#fit-rows').replaceChildren(fitRow('Role match', role.points, role.max, roleNote),
    fitRow('Your skills in the posting', skills.points, skills.max, skillNote + capped));
  const missing = parts.missing || [];
  const more = (parts.missing_total || 0) - missing.length;
  $('#fit-missing').replaceChildren();
  if (missing.length) {
    $('#fit-missing').append(node('strong', '', 'The posting also asks for '), document.createTextNode(`${missing.join(', ')}${more > 0 ? ` and ${more} more` : ''}. `),
      document.createTextNode('These are not in your profile, so they add no points. Add the ones you have in Profile.'));
  } else $('#fit-missing').textContent = 'The posting asks for no other skills SponsorScan recognizes.';
}

function showJob(job) {
  lastFocus = document.activeElement;
  selectedJob = job;
  // A saved record is a snapshot; the current feed has fresher filing evidence.
  const live = report.matches.find(item => item.job_key === job.job_key) || job;
  paintTile($('#detail-logo'), job.company);
  $('#detail-title').textContent = job.title;
  $('#detail-company').textContent = job.company;
  $('#detail-location').textContent = job.location || 'Location not listed';
  $('#detail-posted').textContent = job.posted ? `Posted ${dateLabel(job.posted)}` : '';
  $('#detail-source').textContent = sourceLabel(job.source);
  const url = linkURL(job.url);
  $('#open-posting').hidden = !url;
  if (url) $('#open-posting').href = url; else $('#open-posting').removeAttribute('href');
  renderFit(live);
  const evidence = needsSponsorship() && 'lca_certified' in live;
  $('#detail-sponsorship').hidden = !evidence;
  if (evidence) renderSponsorship(live);
  const reasons = (live.why_ranked || '').replaceAll('resume fit', 'profile fit').split(';').map(s => s.trim()).filter(Boolean);
  $('#detail-evidence').hidden = !reasons.length;
  // Reasons mix strengths with cautions ("location unclear"), so they get a
  // neutral marker rather than a check.
  $('#detail-reasons').replaceChildren(...reasons.map(reason => node('li', '', capitalize(reason))));
  $('#detail-status').value = job.status || 'saved';
  menus.get($('#detail-status'))?.refresh();
  $('#remove-application').hidden = !job.status;
  $('#detail-notes').value = job.notes || '';
  $('#detail-save-status').textContent = '';
  $('#job-dialog').showModal();
  $('#job-dialog').scrollTop = 0;
}

function profileHeading() {
  $('#profile-title').textContent = boot.profile ? 'Profile' : 'Make this search yours.';
  $('#profile-intro').textContent = boot.profile ? 'Adjust your search as it changes. Existing exports and advanced settings stay with this profile.' : 'A few details to help SponsorScan find relevant roles. Everything is saved on this computer.';
}

function fillProfile(step = 0) {
  const profile = boot.profile;
  const form = $('#profile-form');
  const defaults = boot.profile_defaults;
  form.reset();
  form.elements.name.value = profile?.name || '';
  form.elements.work_authorization.value = profile?.work_authorization || '';
  for (const key of ['max_required_experience', 'minimum_score']) {
    const input = form.elements[key];
    input.value = profile?.[key] ?? defaults[key];
    // Keep valid custom settings editable without silently narrowing them on load.
    input.max = Math.max(key === 'minimum_score' ? 500 : 40, Number(input.value));
  }
  form.elements.preferred_locations.value = (profile?.preferred_locations || []).join(', ');
  const hours = String(profile?.report_hours ?? defaults.report_hours);
  if (![...form.elements.report_hours.options].some(item => item.value === hours)) {
    const option = node('option', '', `${hours} hours (existing setting)`); option.value = hours;
    form.elements.report_hours.append(option);
  }
  form.elements.report_hours.value = hours;
  for (const key of ['email_enabled', 'google_sheets_enabled']) form.elements[key].checked = profile?.notifications[key] || false;
  chosenRoles = new Set(profile?.target_roles || []);
  chosenSkills = new Map(Object.entries(profile?.skills || {}));
  $('#role-suggestions').replaceChildren(...boot.roles.map(value => { const option = node('option'); option.value = value; return option; }));
  $('#skill-suggestions').replaceChildren(...boot.skills.map(value => { const option = node('option'); option.value = value; return option; }));
  $('#profile-fields').disabled = false;
  $('#open-setup-guide').hidden = !profile;
  profileHeading(); renderRoleChoices(); renderSkills(); updateAuthorizationNote(); updateIntegrationNote(); showSetupStep(step);
}

function setupError(message) {
  const error = $('#profile-save-status'); error.textContent = message; error.hidden = !message;
  if (message) error.focus();
}

function renderRoleChoices() {
  const common = ['Software Engineer', 'Backend Engineer', 'Full Stack Engineer', 'Data Engineer'];
  const roles = [...common.filter(role => boot.roles.includes(role)), ...boot.roles.filter(role => !common.includes(role))];
  $('#role-choices').replaceChildren(...roles.map(role => {
    const label = node('label', 'role-choice');
    const input = node('input'); input.type = 'checkbox'; input.checked = chosenRoles.has(role);
    input.addEventListener('change', () => { input.checked ? chosenRoles.add(role) : chosenRoles.delete(role); setupError(''); });
    label.append(input, node('span', '', role)); return label;
  }));
  $('#custom-roles').replaceChildren(...[...chosenRoles].filter(role => !boot.roles.includes(role)).map(role => {
    const chip = node('span', 'selected-role', role);
    const remove = node('button', '', '\u00d7'); remove.type = 'button'; remove.setAttribute('aria-label', `Remove ${role}`);
    remove.addEventListener('click', () => { chosenRoles.delete(role); renderRoleChoices(); $('#custom-role').focus(); });
    chip.append(remove); return chip;
  }));
}

function addRole() {
  const input = $('#custom-role'); const text = input.value.trim();
  if (!text) return true;
  if (text.length > 150 || chosenRoles.size >= 100) { setupError('Use a job title under 151 characters, with at most 100 roles.'); return false; }
  const canonical = [...chosenRoles, ...boot.roles].find(role => role.toLowerCase() === text.toLowerCase()) || text;
  chosenRoles.add(canonical); input.value = ''; renderRoleChoices(); setupError(''); return true;
}

function renderSkills() {
  const rows = [...chosenSkills].map(([skill, weight]) => {
    const row = node('div', 'skill-row');
    const name = node('div', 'skill-name'); name.append(node('strong', '', skill));
    if (!boot.skills.includes(skill)) name.append(node('small', '', 'Matched as written in job descriptions'));
    const select = node('select'); select.setAttribute('aria-label', `Familiarity with ${skill}`);
    const levels = [...boot.skill_levels];
    if (!levels.some(level => level.weight === weight)) levels.unshift({label: `Custom weight (${weight})`, weight});
    for (const level of levels) { const option = node('option', '', level.label); option.value = level.weight; select.append(option); }
    select.value = weight;
    select.addEventListener('change', () => chosenSkills.set(skill, Number(select.value)));
    const remove = node('button', 'remove-skill', '\u00d7'); remove.type = 'button'; remove.setAttribute('aria-label', `Remove ${skill}`);
    remove.addEventListener('click', () => { chosenSkills.delete(skill); renderSkills(); $('#skill-input').focus(); });
    row.append(name, select, remove); return row;
  });
  $('#skills-list').replaceChildren(...(rows.length ? rows : [node('p', 'skills-empty', 'No skills added yet. Start with the ones you use most.')]));
  const common = ['Python', 'SQL', 'JavaScript', 'Java', 'Machine Learning', 'AWS'];
  $('#suggested-skills').replaceChildren(...common.filter(skill => boot.skills.includes(skill) && ![...chosenSkills.keys()].some(name => name.toLowerCase() === skill.toLowerCase())).map(skill => {
    const button = node('button', 'skill-suggestion', `+ ${skill}`); button.type = 'button';
    button.addEventListener('click', () => { $('#skill-input').value = [$('#skill-input').value.trim(), skill].filter(Boolean).join(', '); addSkills(); $('#skill-input').focus(); });
    return button;
  }));
}

function addSkills() {
  const input = $('#skill-input');
  const names = input.value.split(',').map(value => value.trim()).filter(Boolean);
  const next = new Map(chosenSkills);
  for (const name of names) {
    if (name.length > 150) { setupError('Each skill can be at most 150 characters. Separate skills with commas.'); return false; }
    const existing = [...next.keys()].find(skill => skill.toLowerCase() === name.toLowerCase());
    const canonical = existing || boot.skills.find(skill => skill.toLowerCase() === name.toLowerCase()) || name;
    if (!next.has(canonical)) next.set(canonical, boot.skill_levels.find(level => level.label === 'Comfortable').weight);
  }
  if (next.size > 100) { setupError('Keep your profile to at most 100 skills.'); return false; }
  chosenSkills = next; input.value = ''; renderSkills(); setupError(''); return true;
}

function updateAuthorizationNote() {
  const auth = $('#profile-form').elements.work_authorization.value;
  let message = 'Choose the option that matches your situation.';
  if (auth) message = ['us_citizen', 'permanent_resident'].includes(auth) ? 'Employer filing history will not affect your ranking. You can skip that download.' : 'Employer filing history will contribute to your ranking. Posting filters use the same defaults as the terminal setup.';
  if (auth === 'permanent_resident') message += ' The current defaults retain citizen-only postings, so check each role\'s requirements.';
  $('#authorization-note').textContent = message;
}

function updateIntegrationNote() {
  const form = $('#profile-form');
  const requested = form.elements.email_enabled.checked || form.elements.google_sheets_enabled.checked;
  $('#integration-note').textContent = requested ? 'This saves a setup preference. It does not connect an account, send results, or turn on a schedule. Instructions follow after you save.' : 'No account is needed to use the local dashboard.';
  if (boot.profile) $('#integration-note').textContent += ' Changing these preferences does not stop any scripts or schedules you already run.';
}

function profilePayload() {
  const form = $('#profile-form');
  const locationText = form.elements.preferred_locations.value;
  const savedLocations = boot.profile?.preferred_locations;
  // A stored location may itself contain a comma. Do not rewrite untouched arrays.
  const locations = savedLocations && locationText === savedLocations.join(', ') ? [...savedLocations] : locationText.split(',').map(value => value.trim()).filter(Boolean);
  return {
    name: form.elements.name.value.trim(), work_authorization: form.elements.work_authorization.value,
    target_roles: [...chosenRoles], skills: Object.fromEntries(chosenSkills),
    preferred_locations: locations,
    max_required_experience: Number(form.elements.max_required_experience.value),
    report_hours: Number(form.elements.report_hours.value), minimum_score: Number(form.elements.minimum_score.value),
    notifications: {email_enabled: form.elements.email_enabled.checked, google_sheets_enabled: form.elements.google_sheets_enabled.checked}
  };
}

function renderReview() {
  const data = profilePayload();
  const rows = [
    ['About you', `${data.name} / ${authorizationLabels[data.work_authorization]}`, 0],
    ['Target roles', data.target_roles.join(', '), 1],
    ['Locations', data.preferred_locations.join(', ') || 'No location preference', 1],
    ['Posting requirements', `Up to ${data.max_required_experience} ${data.max_required_experience === 1 ? 'year' : 'years'} of required experience / ${$('#profile-form').elements.report_hours.selectedOptions[0].textContent}`, 1],
    ['Skills', [...chosenSkills].map(([name, weight]) => `${name} (${boot.skill_levels.find(level => level.weight === weight)?.label.toLowerCase() || `custom ${weight}`})`).join(', '), 2]
  ];
  $('#profile-review').replaceChildren(...rows.map(([label, value, step]) => {
    const row = node('div', 'review-row'); const detail = node('dd', '', value);
    const edit = node('button', 'text-button', 'Edit'); edit.type = 'button'; edit.setAttribute('aria-label', `Edit ${label.toLowerCase()}`);
    edit.addEventListener('click', () => showSetupStep(step, true));
    row.append(node('dt', '', label), detail, edit); return row;
  }));
  const filters = {...boot.profile_defaults, ...(boot.profile || {})};
  if (!boot.profile || data.work_authorization !== boot.profile.work_authorization) {
    const reject = !['us_citizen', 'permanent_resident'].includes(data.work_authorization);
    for (const key of ['reject_citizenship_required', 'reject_permanent_authorization_required', 'reject_opt_excluded', 'reject_no_future_sponsorship']) filters[key] = reject;
  }
  if (['us_citizen', 'permanent_resident'].includes(data.work_authorization)) filters.reject_no_future_sponsorship = false;
  const filterLabels = {reject_senior_titles: 'Senior and leadership titles', reject_level_ii_plus_titles: 'Level II and higher titles', reject_masters_required: "Jobs requiring a master's degree", reject_phd_required: 'Jobs requiring a PhD', reject_masters_mentions: "Any mention of a master's degree", reject_phd_mentions: 'Any mention of a PhD', reject_clearance_roles: 'Jobs requiring security clearance', reject_citizenship_required: 'Jobs requiring U.S. citizenship', reject_permanent_authorization_required: 'Jobs requiring permanent work authorization', reject_opt_excluded: 'Jobs explicitly excluding OPT / CPT', reject_no_future_sponsorship: 'Jobs refusing sponsorship now or in the future'};
  const excluded = Object.keys(filterLabels).filter(key => filters[key]).map(key => node('li', '', `Exclude: ${filterLabels[key]}`));
  $('#review-filters').replaceChildren(...(excluded.length ? excluded : [node('li', '', 'No additional exclusion filters selected.')]));
}

function showSetupStep(step, focus = false) {
  setupStep = step;
  document.querySelectorAll('[data-panel]').forEach(panel => { panel.hidden = Number(panel.dataset.panel) !== step; });
  document.querySelectorAll('[data-step]').forEach(button => {
    const index = Number(button.dataset.step); button.disabled = false;
    button.classList.toggle('completed-step', index < step);
    if (index === step) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
  });
  $('#setup-back').hidden = step === 0;
  if (step === 3) $('#setup-next').textContent = boot.profile ? 'Save changes' : 'Create my workspace';
  else $('#setup-next').replaceChildren('Continue', icon('arrow-right'));
  $('#setup-save-note').textContent = boot.profile ? 'Changes are saved at the final step.' : 'Your profile is saved at the final step.';
  setupError('');
  if (step === 3) renderReview();
  if (focus) $(`[data-panel="${step}"] h2`).focus();
}

function validateStep(step) {
  if (step === 1 && !addRole()) return false;
  if (step === 2 && !addSkills()) return false;
  const form = $('#profile-form');
  const fields = [['name'], ['max_required_experience', 'report_hours'], [], ['minimum_score']][step];
  for (const key of fields) {
    const field = form.elements[key];
    if (!field.checkValidity() || !field.value.trim()) {
      if (field.closest('details')) field.closest('details').open = true;
      setupError(`Check ${key === 'name' ? 'your name' : key.replaceAll('_', ' ')} before continuing.`);
      field.focus(); field.reportValidity(); return false;
    }
  }
  if (step === 0 && !form.elements.work_authorization.value) { setupError('Choose your work authorization to continue.'); return false; }
  if (step === 1 && !chosenRoles.size) { setupError('Choose at least one target role, or add your own.'); return false; }
  if (step === 1) {
    const locations = profilePayload().preferred_locations;
    if (locations.length > 100 || locations.some(value => value.length > 150)) { setupError('Use at most 100 locations, each under 151 characters.'); return false; }
  }
  if (step === 2 && !chosenSkills.size) { setupError('Add at least one skill from your resume to continue.'); return false; }
  return true;
}

function moveSetupTo(step) {
  if (step > setupStep) {
    for (let index = 0; index < step; index++) {
      showSetupStep(index);
      if (!validateStep(index)) return;
    }
  }
  showSetupStep(step, true);
}

function renderSetupComplete() {
  if (!setupComplete) return;
  const needsHistory = !['us_citizen', 'permanent_resident'].includes(boot.profile.work_authorization);
  $('#finish-history-row').hidden = !needsHistory;
  $('#finish-history-status').textContent = boot.data.employers ? `${boot.data.employers.toLocaleString()} employers stored locally.` : 'Download the prebuilt employer table to include filing history in your rankings.';
  $('#finish-jobs-status').textContent = boot.data.jobs ? `${boot.data.jobs.toLocaleString()} jobs stored locally. Your matches are ready to browse.${boot.data.failed_boards ? ` ${boot.data.failed_boards} boards failed, so results may be incomplete.` : ''}` : 'Fetch roles from the job boards in your company list.';
  $('#finish-extras-row').hidden = !boot.profile.notifications.email_enabled && !boot.profile.notifications.google_sheets_enabled;
  $('#finish-history').textContent = boot.data.employers ? 'Update history' : 'Load history';
  $('#finish-fetch').textContent = boot.data.jobs ? 'Update jobs' : 'Fetch jobs';
}

// Integrations: the same steps the terminal guide prints, with copy buttons.
// Setup itself happens in the terminal; this view only explains it.
let setupGuide = null;
let setupShell = null;
const sectionIcons = {telegram: 'send', email: 'mail', sheets: 'table', schedule: 'clock', check: 'check-circle'};
const sectionOrder = ['telegram', 'email', 'sheets', 'schedule', 'check'];

async function copyText(button, text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    // Without clipboard access, select the text so Ctrl+C works.
    const range = document.createRange(); range.selectNodeContents(button.previousElementSibling);
    getSelection().removeAllRanges(); getSelection().addRange(range);
    button.lastChild.textContent = 'Press Ctrl+C'; return;
  }
  button.lastChild.textContent = 'Copied';
  setTimeout(() => { button.lastChild.textContent = 'Copy'; }, 1600);
}

function codeBlock(lines, label) {
  const block = node('div', 'code-block');
  const code = node('pre'); code.textContent = lines.join('\n'); code.tabIndex = 0;
  code.setAttribute('aria-label', label);
  const copy = node('button', 'copy-button'); copy.type = 'button';
  copy.append(icon('copy'), node('span', '', 'Copy'));
  copy.setAttribute('aria-label', `Copy: ${label}`);
  copy.addEventListener('click', () => copyText(copy, lines.join('\n')));
  block.append(code, copy);
  return block;
}

// The services' own marks, drawn inline in index.html, so nothing loads from outside.
const brandMarks = new Set(['telegram', 'email', 'sheets', 'schedule']);
function brandMark(id) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  svg.setAttribute('class', 'brand-mark'); svg.setAttribute('aria-hidden', 'true');
  use.setAttribute('href', `#b-${id}`); svg.append(use);
  return svg;
}

function sectionCard(section, open) {
  const card = node('details', 'integration-card');
  card.open = open; card.id = `setup-${section.id}`;
  const summary = node('summary');
  const badge = node('span', brandMarks.has(section.id) ? 'integration-icon has-mark' : 'integration-icon');
  badge.append(brandMarks.has(section.id) ? brandMark(section.id) : icon(sectionIcons[section.id] || 'plug'));
  const heading = node('span', 'integration-heading');
  heading.append(node('strong', '', section.title), node('span', '', section.summary));
  const meta = node('span', 'integration-meta', plural(section.steps.length, 'step', 'steps'));
  summary.append(badge, heading, meta, icon('chevron'));
  card.append(summary);
  const body = node('div', 'integration-body');
  for (const paragraph of section.intro) body.append(node('p', 'integration-intro', paragraph));
  const steps = node('ol', 'setup-list');
  section.steps.forEach((step, index) => {
    const item = node('li');
    item.append(node('span', 'step-number', index + 1));
    const content = node('div', 'step-content');
    content.append(node('h3', '', step.title));
    if (step.link) {
      const link = node('a', 'step-link', new URL(step.link).host + new URL(step.link).pathname.replace(/\/$/, ''));
      link.href = step.link; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.append(icon('external'));
      content.append(link);
    }
    if (step.notes?.length) {
      const notes = node('ul', 'step-notes');
      notes.append(...step.notes.map(note => node('li', '', note)));
      content.append(notes);
    }
    if (step.commands?.length) content.append(codeBlock(step.commands, `${step.title} commands`));
    if (step.snippet?.length) content.append(codeBlock(step.snippet, `${step.title}, lines to paste`));
    item.append(content);
    steps.append(item);
  });
  body.append(steps, node('p', 'integration-guide', `Full guide: ${section.guide} in your SponsorScan folder.`));
  card.append(body);
  return card;
}

function renderIntegrations() {
  document.querySelectorAll('[data-shell]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.shell === setupShell)));
  if (!setupGuide) return;
  const open = new Set([...document.querySelectorAll('.integration-card[open]')].map(card => card.id));
  const sections = setupGuide.sections[setupShell];
  const ordered = sectionOrder.map(id => sections.find(section => section.id === id)).filter(Boolean);
  // Telegram starts open on a first visit: it is the quickest to set up.
  $('#integrations-list').replaceChildren(...ordered.map((section, index) =>
    sectionCard(section, open.size ? open.has(`setup-${section.id}`) : index === 0)));
}

async function loadIntegrations() {
  if (setupGuide) { renderIntegrations(); return; }
  if (!boot?.profile) { $('#integrations-list').replaceChildren(emptyState('Create your profile first.', 'The setup steps use your profile\'s own file names.')); return; }
  try {
    setupGuide = await api('/api/setup-guide');
    setupShell = setupShell || storedSetting('sponsorscan-shell') || setupGuide.shell;
    renderIntegrations();
  } catch (error) { $('#integrations-list').replaceChildren(emptyState('The setup steps did not load.', error.message)); }
}

document.querySelectorAll('[data-shell]').forEach(button => button.addEventListener('click', () => {
  setupShell = button.dataset.shell; storeSetting('sponsorscan-shell', setupShell); renderIntegrations();
}));

// Lines shown while an update runs. Each names something the update really does.
const taskLines = {
  fetch: ["Knocking on each company's career page...", 'Digging through Greenhouse listings...', "Asking Lever what's new...",
    'Checking Ashby for fresh postings...', 'Waiting on Workday, it takes its time...', 'Setting aside roles that want 10 years of experience...',
    'Reading the fine print on sponsorship...', 'Folding in duplicate postings...',
    'Scrolling Amazon a hundred jobs at a time...', 'Asking Microsoft nicely, then waiting politely...',
    'Microsoft said "slow down." Slowing down...', 'Skipping "entry level, 8+ years required"...',
    'Politely ignoring every "Senior Staff Principal"...', 'Asking every recruiter "but do you sponsor?"...',
    'Turning HTML soup into readable job posts...', 'Reading job descriptions so you do not have to...',
    'Refilling coffee for the Workday servers...', 'Checking under the couch cushions for new grad roles...',
    'Counting how many postings want "rockstar ninjas"...', 'Untangling 30-something career pages...',
    'Telling the internship listings apart from the real ones...', 'Almost there. Workday is still thinking...'],
  history: ['Unpacking the employer table...', 'Counting H-1B filings by quarter...', 'Matching employer names to DOL records...',
    'Lining up green card filings...', "Checking the table's checksum...", 'Reading 64,000 employers, alphabetically...',
    'Telling Amazon.com Services LLC apart from Amazon Web Services...', 'Dusting off last quarter\'s filings...'],
};
let taskLineTimer = null;
let taskShown = '';

// The banner is for work in progress and failures. A finished update is
// announced once, when this page sees it finish, rather than on every load.
function showTask(task) {
  const box = $('#task-status');
  const visible = Boolean(task.message) && (task.running || task.error);
  // Polling repeats the same state every few seconds; rebuilding would restart the lines.
  const shown = visible ? `${task.running}|${task.error}|${task.action}|${task.running ? '' : task.message}` : '';
  if (shown !== taskShown) {
    taskShown = shown;
    clearInterval(taskLineTimer);
    box.hidden = !visible;
    box.classList.toggle('error', Boolean(task.error));
    if (visible) {
      const body = node('div', 'alert-body');
      const lines = task.running ? taskLines[task.action] || [] : [];
      if (lines.length) {
        // Screen readers hear the real status once; the rotating line is decoration.
        const line = node('p', 'task-line', task.message); line.setAttribute('aria-hidden', 'true');
        body.append(node('span', 'sr-only', task.message), line);
        // A fresh order each run, so a long fetch does not loop the same few lines.
        const order = [...lines];
        for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
        let index = -1;
        taskLineTimer = setInterval(() => {
          index = (index + 1) % order.length;
          const next = node('p', 'task-line', order[index]); next.setAttribute('aria-hidden', 'true');
          body.lastChild.replaceWith(next);
        }, 2600);
      } else body.append(node('p', '', task.message));
      box.replaceChildren(task.running ? node('span', 'spinner') : icon('info'), body);
    }
  }
  $('#fetch-jobs').disabled = task.running;
  $('#load-history').disabled = task.running;
  $('#finish-history').disabled = task.running;
  $('#finish-fetch').disabled = task.running;
}

async function reload(fillForm = false) {
  boot = await api('/api/bootstrap'); token = boot.token;
  renderGreeting(new Date());
  $('#connection').textContent = 'Connected locally';
  $('#connection').classList.add('connected');
  if (fillForm) fillProfile();
  const data = boot.data;
  const summary = [data.jobs ? `${plural(data.jobs, 'job', 'jobs')} from ${plural(data.companies || 0, 'company', 'companies')}, fetched ${fetchedLabel(data.last_fetched)}.` : 'No jobs fetched yet.'];
  if (needsSponsorship()) summary.push(data.employers ? `Filing history for ${plural(data.employers, 'employer', 'employers')}.` : 'Employer filing history is not loaded.');
  $('#data-summary').textContent = summary.join(' ');
  $('#load-history').hidden = !needsSponsorship();
  $('#load-history').textContent = data.employers ? 'Update filing history' : 'Load filing history';
  showTask(boot.task);
  const [freshReport, records] = await Promise.all([api('/api/jobs'), api('/api/applications')]);
  report = freshReport; applications = records.applications;
  setupGuide = null; // a saved profile can change the file names in the steps
  const warnings = [...boot.warnings, ...(report.metadata.warnings || [])];
  if (data.error) warnings.push(data.error);
  renderWarnings(warnings, data);
  const selectedRole = $('#role-filter').value;
  const allRoles = node('option', '', 'All roles'); allRoles.value = '';
  const roles = [...new Set(report.matches.map(job => job.role_family))].filter(Boolean).sort();
  $('#role-filter').replaceChildren(allRoles, ...roles.map(role => {
    const option = node('option', '', role); option.value = role; return option;
  }));
  $('#role-filter').value = roles.includes(selectedRole) ? selectedRole : '';
  menus.get($('#role-filter'))?.refresh();
  renderMatches(); renderApplications(); renderSetupComplete(); showView();
  if (boot.task.running && !pollTimer) pollTimer = setTimeout(pollTask, 2000);
}

async function pollTask() {
  pollTimer = null;
  try {
    const task = await api('/api/task'); showTask(task);
    if (task.running) pollTimer = setTimeout(pollTask, 2000);
    else {
      await reload();
      if (!task.error && task.message) success(task.message);
    }
  } catch (error) { notice(error.message); }
}

// One card for everything that makes the current results less complete.
function renderWarnings(messages, data) {
  const body = node('div', 'alert-body');
  const failed = data.failed_boards || 0;
  if (failed) {
    const names = (data.failed_board_names || []).map(capitalize);
    const boards = failed === 1 ? `${names[0] ? `${names[0]}'s` : 'One'} job board` :
      `${failed} job boards${names.length === failed ? ` (${names.join(', ')})` : ''}`;
    body.append(node('p', '', `${boards} did not respond during the last fetch, so ${failed === 1 ? 'its' : 'their'} jobs may be missing.`));
  }
  for (const message of messages) body.append(node('p', '', message));
  const children = [icon('info'), body];
  if (failed) {
    const retry = node('button', 'button secondary', 'Fetch again'); retry.type = 'button';
    retry.disabled = Boolean(boot.task.running);
    retry.addEventListener('click', () => updateSources('fetch').catch(error => notice(error.message)));
    children.push(retry);
  }
  $('#warnings').replaceChildren(...children);
  $('#warnings').hidden = !body.childElementCount;
}

/* A floating menu in place of a native <select>. The hidden select keeps the
   value, so filters, forms and exports read it as before; choosing an option
   fires the select's own input and change events. The list is a popover so it
   floats above dialogs instead of being clipped by them. */
function enhanceSelect(select) {
  const labelText = document.getElementById(select.getAttribute('aria-labelledby'))?.textContent || '';
  const wrap = node('div', 'menu');
  const button = node('button', 'menu-button'); button.type = 'button';
  const value = node('span', 'menu-value');
  button.append(value, icon('chevron'));
  button.setAttribute('aria-haspopup', 'listbox');
  button.setAttribute('aria-expanded', 'false');
  const list = node('ul', 'menu-list');
  list.id = `${select.id}-options`; list.tabIndex = -1;
  list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', labelText);
  list.setAttribute('popover', 'manual');
  button.setAttribute('aria-controls', list.id);
  select.hidden = true; select.tabIndex = -1;
  select.after(wrap); wrap.append(button, list);
  let active = -1;

  function refresh() {
    value.textContent = select.selectedOptions[0]?.textContent || '';
    button.setAttribute('aria-label', `${labelText}: ${value.textContent}`);
  }
  function highlight(index) {
    active = Math.max(0, Math.min(select.options.length - 1, index));
    [...list.children].forEach((item, position) => item.classList.toggle('active', position === active));
    const item = list.children[active];
    if (item) { list.setAttribute('aria-activedescendant', item.id); item.scrollIntoView({block: 'nearest'}); }
  }
  function place() {
    const box = button.getBoundingClientRect();
    list.style.minWidth = `${box.width}px`;
    const height = list.offsetHeight;
    const above = innerHeight - box.bottom < height + 12 && box.top > height + 12;
    list.style.top = `${above ? box.top - height - 6 : box.bottom + 6}px`;
    list.style.left = `${Math.max(8, Math.min(box.left, innerWidth - list.offsetWidth - 8))}px`;
  }
  function open() {
    if (openMenu && openMenu !== control) openMenu.close(false);
    list.replaceChildren(...[...select.options].map((option, index) => {
      const item = node('li', 'menu-option');
      item.id = `${list.id}-${index}`; item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(option.selected));
      item.append(icon('check'), node('span', '', option.textContent));
      // Keep focus in the list so a click never reads as leaving the menu.
      item.addEventListener('mousedown', event => event.preventDefault());
      item.addEventListener('click', () => choose(index));
      item.addEventListener('mousemove', () => { if (active !== index) highlight(index); });
      return item;
    }));
    list.showPopover(); place();
    button.setAttribute('aria-expanded', 'true');
    openMenu = control;
    highlight(Math.max(0, select.selectedIndex));
    list.focus({preventScroll: true});
  }
  function close(returnFocus = true) {
    if (list.matches(':popover-open')) list.hidePopover();
    button.setAttribute('aria-expanded', 'false');
    list.removeAttribute('aria-activedescendant');
    if (openMenu === control) openMenu = null;
    if (returnFocus) button.focus();
  }
  function choose(index) {
    const changed = select.selectedIndex !== index;
    select.selectedIndex = index; refresh(); close();
    if (changed) {
      select.dispatchEvent(new Event('input', {bubbles: true}));
      select.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }
  button.addEventListener('click', () => (list.matches(':popover-open') ? close() : open()));
  button.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); open(); }
  });
  list.addEventListener('keydown', event => {
    const keys = {ArrowDown: active + 1, ArrowUp: active - 1, Home: 0, End: select.options.length - 1, PageDown: active + 5, PageUp: active - 5};
    if (event.key in keys) { event.preventDefault(); highlight(keys[event.key]); }
    else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose(active); }
    // Inside a dialog, Escape closes only the menu.
    else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
    else if (event.key === 'Tab') close(false);
    else if (event.key.length === 1 && /\S/.test(event.key)) {
      const options = [...select.options];
      const start = active + 1;
      const next = [...options.slice(start), ...options.slice(0, start)].find(option => option.textContent.toLowerCase().startsWith(event.key.toLowerCase()));
      if (next) highlight(next.index);
    }
  });
  list.addEventListener('focusout', event => { if (!wrap.contains(event.relatedTarget)) close(false); });
  const control = {refresh, close, place, wrap};
  menus.set(select, control);
  refresh();
  return control;
}

async function updateSources(action) {
  notice('');
  const task = await api('/api/update', {action}); showTask(task);
  if (!pollTimer) pollTimer = setTimeout(pollTask, 2000);
}

function exportCSV(rows, fields, filename) {
  const cell = value => {
    let text = String(value ?? '');
    // Prevent job text and notes from becoming formulas when opened in a spreadsheet.
    if (/^[=+@\-\t\r\n]/.test(text)) text = "'" + text;
    return `"${text.replaceAll('"', '""')}"`;
  };
  const csv = [fields, ...rows.map(row => fields.map(field => row[field]))].map(row => row.map(cell).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob(['\ufeff' + csv], {type: 'text/csv;charset=utf-8;'}));
  const link = node('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function renderGreeting(now) {
  const hour = now.getHours();
  const part = hour >= 5 && hour < 12 ? 'Good morning' : hour >= 12 && hour < 17 ? 'Good afternoon' : 'Good evening';
  const name = (boot?.profile?.name || '').trim();
  // A name typed all in lowercase is capitalized; any other spelling is kept.
  $('#profile-label').textContent = name ? `${part}, ${name === name.toLowerCase() ? capitalize(name) : name}` : 'Set up your workspace';
}

// The clock ticks on each minute boundary rather than polling every second.
function tickClock() {
  const now = new Date();
  $('#clock').textContent = now.toLocaleString(undefined, {weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  $('#clock').dateTime = now.toISOString();
  if (boot) renderGreeting(now);
  setTimeout(tickClock, 60000 - (now.getSeconds() * 1000 + now.getMilliseconds()) + 50);
}

// The theme is a per-browser convenience. theme.js applied the saved choice
// before the first paint; this keeps it current afterwards.
function storedSetting(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storeSetting(key, value) { try { localStorage.setItem(key, value); } catch { /* the choice lasts until reload */ } }
const themeMedia = matchMedia('(prefers-color-scheme: dark)');
let themeChoice = ['light', 'dark'].includes(storedSetting('sponsorscan-theme')) ? storedSetting('sponsorscan-theme') : 'system';

function applyTheme() {
  const dark = themeChoice === 'dark' || (themeChoice === 'system' && themeMedia.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.querySelector('meta[name="theme-color"]').content = dark ? '#151517' : '#ffffff';
  document.querySelectorAll('[data-theme-choice]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.themeChoice === themeChoice)));
  // The current choice sits last, where the closed switch shows it.
  const group = $('.theme-switch');
  const chosen = group.querySelector(`[data-theme-choice="${themeChoice}"]`);
  if (group.lastElementChild !== chosen) {
    const focused = document.activeElement === chosen;
    group.append(chosen);
    if (focused) chosen.focus();
  }
}

document.querySelectorAll('[data-theme-choice]').forEach(button => button.addEventListener('click', () => {
  themeChoice = button.dataset.themeChoice; storeSetting('sponsorscan-theme', themeChoice); applyTheme();
  // Close right after a choice; reopen once the pointer leaves or a key is pressed.
  $('.theme-switch').classList.add('settled');
}));
$('.theme-switch').addEventListener('pointerleave', () => $('.theme-switch').classList.remove('settled'));
$('.theme-switch').addEventListener('keydown', () => $('.theme-switch').classList.remove('settled'));
themeMedia.addEventListener('change', applyTheme);
applyTheme();
tickClock();

for (const selector of ['#role-filter', '#match-sort', '#status-filter', '#detail-status']) enhanceSelect($(selector));
document.addEventListener('pointerdown', event => { if (openMenu && !openMenu.wrap.contains(event.target)) openMenu.close(false); });
window.addEventListener('resize', () => openMenu?.close(false));
document.addEventListener('scroll', () => openMenu?.place(), true);
$('#notice-close').addEventListener('click', () => notice(''));
$('#filings-toggle').addEventListener('click', () => {
  filingsVisible = !filingsVisible;
  try { localStorage.setItem('sponsorscan-filings-column', filingsVisible ? 'shown' : 'hidden'); } catch { /* lasts until reload */ }
  renderMatches();
});

// "Did you apply?" Opening a posting is remembered; when you come back to this
// tab, the dashboard asks instead of assuming either way.
let pendingApply = null;
let promptJob = null;

function rememberApply(job) {
  const record = applications.find(item => item.job_key === job.job_key);
  if (record && ['applied', 'interview', 'offer', 'rejected'].includes(record.status)) return;
  pendingApply = {job, left: false};
}

function askApplied() {
  if (!pendingApply?.left) return;
  promptJob = pendingApply.job; pendingApply = null;
  paintTile($('#apply-prompt-logo'), promptJob.company);
  $('#apply-prompt-title').textContent = `Did you apply at ${promptJob.company}?`;
  $('#apply-prompt-role').textContent = promptJob.title;
  $('#apply-prompt').hidden = false;
  $('#apply-yes').focus({preventScroll: true});
}

document.addEventListener('visibilitychange', () => {
  if (!pendingApply) return;
  if (document.visibilityState === 'hidden') pendingApply.left = true;
  else askApplied();
});
// A posting opened in another window can leave this tab visible, so focus counts too.
window.addEventListener('blur', () => { if (pendingApply) pendingApply.left = true; });
window.addEventListener('focus', askApplied);
$('#apply-no').addEventListener('click', () => { $('#apply-prompt').hidden = true; promptJob = null; });
$('#apply-yes').addEventListener('click', event => busy(event.currentTarget, async () => {
  const job = promptJob;
  const record = applications.find(item => item.job_key === job.job_key);
  await api('/api/application', {job_key: job.job_key, status: 'applied', notes: record?.notes || ''});
  $('#apply-prompt').hidden = true; promptJob = null;
  await loadApplications();
  success(`Marked as applied at ${job.company}. You'll find it in Applications.`);
}));
$('#open-posting').addEventListener('click', () => {
  // The prompt cannot show over an open dialog, and the posting is the next step anyway.
  rememberApply(selectedJob); $('#job-dialog').close();
});
armRemove($('#remove-application'), $('#remove-application'), () => applications.find(item => item.job_key === selectedJob?.job_key) || selectedJob, 'Click again to remove');
window.addEventListener('hashchange', showView);
$('#refresh').addEventListener('click', event => busy(event.currentTarget, async () => { notice(''); await reload(); }));
$('#fetch-jobs').addEventListener('click', () => updateSources('fetch').catch(error => notice(error.message)));
$('#load-history').addEventListener('click', () => updateSources('history').catch(error => notice(error.message)));
$('#finish-history').addEventListener('click', () => updateSources('history').catch(error => notice(error.message)));
$('#finish-fetch').addEventListener('click', () => updateSources('fetch').catch(error => notice(error.message)));
document.querySelectorAll('[data-step]').forEach(button => button.addEventListener('click', () => moveSetupTo(Number(button.dataset.step))));
$('#setup-back').addEventListener('click', () => showSetupStep(setupStep - 1, true));
$('#add-role').addEventListener('click', () => { addRole(); $('#custom-role').focus(); });
$('#add-skill').addEventListener('click', () => { addSkills(); $('#skill-input').focus(); });
for (const [selector, add] of [['#custom-role', addRole], ['#skill-input', addSkills]]) {
  $(selector).addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); add(); } });
}
document.querySelectorAll('[name="work_authorization"]').forEach(input => input.addEventListener('change', updateAuthorizationNote));
for (const name of ['email_enabled', 'google_sheets_enabled']) $('#profile-form').elements[name].addEventListener('change', updateIntegrationNote);
$('#edit-profile').addEventListener('click', () => {
  setupComplete = false; $('#setup-complete').hidden = true; $('#profile-form').hidden = false;
  profileHeading(); showSetupStep(0, true);
});
for (const selector of ['#match-search', '#role-filter', '#match-sort']) $(selector).addEventListener('input', () => { matchLimit = 60; renderMatches(); });
for (const selector of ['#application-search', '#status-filter']) $(selector).addEventListener('input', () => { applicationLimit = 60; renderApplications(); });
$('#close-detail').addEventListener('click', () => $('#job-dialog').close());
$('#close-manual').addEventListener('click', () => $('#manual-dialog').close());
for (const selector of ['#job-dialog', '#manual-dialog']) $(selector).addEventListener('close', () => lastFocus?.isConnected && lastFocus.focus());
$('#add-application').addEventListener('click', () => {
  lastFocus = document.activeElement; $('#manual-form').reset(); $('#manual-save-status').textContent = ''; $('#manual-dialog').showModal();
});
$('#application-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(event.currentTarget.querySelector('button[type=submit]'), async () => {
    await api('/api/application', {job_key: selectedJob.job_key, status: $('#detail-status').value, notes: $('#detail-notes').value});
    await loadApplications(); $('#detail-save-status').textContent = 'Saved on this computer.';
  }, $('#detail-save-status'));
});
$('#manual-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  busy(form.querySelector('button[type=submit]'), async () => {
    await api('/api/application', {...Object.fromEntries(new FormData(form)), manual: true});
    await loadApplications(); $('#manual-dialog').close(); success('Application added.');
  }, $('#manual-save-status'));
});
$('#profile-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!validateStep(setupStep)) return;
  if (setupStep < 3) { showSetupStep(setupStep + 1, true); return; }
  // Validate every step again before persisting, including edits made on a return visit.
  for (let step = 0; step < 4; step++) { showSetupStep(step); if (!validateStep(step)) return; }
  const data = profilePayload(); const first = !boot.profile;
  $('#profile-fields').disabled = true;
  document.querySelectorAll('[data-step]').forEach(button => { button.disabled = true; });
  $('#setup-next').textContent = 'Saving...';
  let saved = false;
  try {
    const result = await api('/api/profile', data); saved = true;
    boot.profile = result.profile;
    // Show the saved values where the user is. Refilling through reload()
    // reset the wizard to step 1 and then waited seconds for the matches to
    // rebuild, which looked like the save had failed.
    fillProfile(first ? 0 : 3);
    if (first) {
      // Show the finish screen at once: its job and history counts are already
      // loaded, and saving a profile does not change them. Matches build in the
      // background; waiting for them left step 1 on screen for seconds.
      setupComplete = true; $('#profile-form').hidden = true; $('#setup-complete').hidden = false;
      $('#profile-title').textContent = 'A good place to start.';
      $('#profile-intro').textContent = 'Your search preferences are saved on this computer.';
      location.hash = 'profile'; showView(); renderSetupComplete(); $('#complete-title').focus();
      // A later matching error must not make a successful save look like lost work.
      reload().catch(error => notice(`Profile saved. ${error.message}`));
    } else {
      notice('Profile saved. Updating your matches...'); // stays up until the update finishes
      reload().then(() => success('Profile saved. Your matches are updated.'))
        .catch(error => notice(`Profile saved. ${error.message}`));
    }
  } catch (error) { setupError(error.message); }
  finally {
    $('#profile-fields').disabled = false;
    document.querySelectorAll('[data-step]').forEach(button => { button.disabled = false; });
    if (!saved) $('#setup-next').textContent = first ? 'Create my workspace' : 'Save changes';
  }
});
$('#export-matches').addEventListener('click', () => exportCSV(filteredMatches(), ['company', 'title', 'location', 'url', 'source', 'posted', 'combined_score', 'resume_fit_score', 'why_ranked'], 'sponsorscan-matches.csv'));
$('#export-applications').addEventListener('click', () => exportCSV(filteredApplications(), ['company', 'title', 'url', 'status', 'notes', 'created_at', 'applied_at', 'updated_at'], 'sponsorscan-applications.csv'));
reload(true).catch(error => notice(error.message));
