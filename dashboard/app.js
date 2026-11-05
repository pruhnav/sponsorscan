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

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function notice(message) {
  $('#notice').textContent = message;
  $('#notice').hidden = !message;
}

async function api(path, data) {
  let response;
  try {
    response = await fetch(path, data === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-SponsorScan-Token': token},
      body: JSON.stringify(data)
    });
  } catch {
    $('#connection').textContent = 'Disconnected';
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
  if (!['matches', 'applications', 'profile'].includes(name)) name = 'matches';
  if (boot && !boot.profile) name = 'profile';
  if (name !== 'profile' && setupComplete) {
    setupComplete = false;
    $('#setup-complete').hidden = true;
    $('#profile-form').hidden = false;
    profileHeading();
  }
  document.querySelectorAll('.view').forEach(view => { view.hidden = view.id !== `view-${name}`; });
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

function linkURL(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}

function emptyState(title, message) {
  const empty = node('div', 'empty');
  empty.append(node('h2', '', title), node('p', '', message));
  return empty;
}

function filteredMatches() {
  const term = $('#match-search').value.trim().toLowerCase();
  const family = $('#role-filter').value;
  const rows = report.matches.filter(job => (!family || job.role_family === family) &&
    [job.title, job.company, job.location].join(' ').toLowerCase().includes(term));
  if ($('#match-sort').value === 'score') rows.sort((a, b) => b.combined_score - a.combined_score);
  return rows;
}

function filteredApplications() {
  const term = $('#application-search').value.trim().toLowerCase();
  const status = $('#status-filter').value;
  return applications.filter(job => (!status || job.status === status) &&
    [job.title, job.company, job.location, job.notes].join(' ').toLowerCase().includes(term));
}

function jobRow(job, tracking = false) {
  const saved = tracking ? job : applications.find(item => item.job_key === job.job_key);
  const row = node('article', 'job-row');
  const open = node('button', 'job-open');
  open.type = 'button';
  open.setAttribute('aria-label', `Details for ${job.title} at ${job.company}`);
  const description = node('span', 'job-description');
  description.append(node('strong', '', job.title), node('small', '', [job.company, job.location].filter(Boolean).join(' / ')));
  open.append(node('span', 'avatar', (job.company || '?').slice(0, 1)), description);
  open.addEventListener('click', () => showJob(saved || job));
  const meta = node('div', 'job-meta');
  if (tracking) {
    meta.append(node('span', 'badge', labels[job.status]), node('span', '', `Updated ${dateLabel(job.updated_at)}`));
  } else {
    meta.append(node('span', 'fit', `Profile fit ${job.resume_fit_score}/100`), node('span', '', dateLabel(job.posted)));
  }
  const save = node('button', 'save-button', saved ? 'Details' : 'Save');
  save.setAttribute('aria-label', saved ? `Edit ${job.title}` : `Save ${job.title}`);
  save.addEventListener('click', () => {
    if (saved) { showJob(saved); return; }
    busy(save, async () => {
      await api('/api/application', {job_key: job.job_key, status: 'saved'});
      await loadApplications();
      notice(`Saved ${job.title} at ${job.company}.`);
    });
  });
  row.append(open, meta, save);
  return row;
}

function renderMatches() {
  const rows = filteredMatches();
  $('#results-label').textContent = `${rows.length} matching ${rows.length === 1 ? 'role' : 'roles'}${rows.length > matchLimit ? ` / showing ${matchLimit}` : ''}`;
  const list = $('#matches-list');
  list.replaceChildren(...rows.slice(0, matchLimit).map(job => jobRow(job)));
  if (!rows.length) {
    const message = report.message || (report.matches.length ? 'Try a different search or role filter.' :
      'Try a longer posting window or a lower minimum score in Profile. You can also fetch current jobs.');
    list.append(emptyState('No matching jobs here yet.', message));
  }
  if (rows.length > matchLimit) {
    const more = node('button', 'text-button', 'Show more jobs');
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

function renderApplications() {
  const list = $('#applications-list');
  const rows = filteredApplications();
  list.replaceChildren(...rows.slice(0, applicationLimit).map(job => jobRow(job, true)));
  if (!rows.length) list.append(emptyState('Keep your search in one place.', applications.length ?
    'No applications match these filters.' : 'Save a match or add an application you made elsewhere. Your notes and statuses will stay here after job updates.'));
  if (rows.length > applicationLimit) {
    const more = node('button', 'text-button', 'Show more applications');
    more.addEventListener('click', () => { applicationLimit += 60; renderApplications(); }); list.append(more);
  }
  $('#saved-count').textContent = applications.filter(item => item.status === 'saved').length;
  $('#applied-count').textContent = applications.filter(item => item.applied_at).length;
  $('#nav-applications').textContent = applications.filter(item => item.status !== 'archived').length;
}

async function loadApplications() {
  applications = (await api('/api/applications')).applications;
  renderApplications(); renderMatches();
}

function showJob(job) {
  lastFocus = document.activeElement;
  selectedJob = job;
  $('#detail-title').textContent = job.title;
  $('#detail-company').textContent = job.company;
  $('#detail-location').textContent = job.location || 'Location not recorded';
  $('#detail-source').textContent = job.source || '';
  const url = linkURL(job.url);
  $('#open-posting').hidden = !url;
  if (url) $('#open-posting').href = url; else $('#open-posting').removeAttribute('href');
  const reasons = (job.why_ranked || '').replaceAll('resume fit', 'profile fit').split(';').map(s => s.trim()).filter(Boolean);
  $('#detail-evidence').hidden = !reasons.length;
  $('#detail-reasons').replaceChildren(...reasons.map(reason => node('li', '', reason)));
  const filings = $('#detail-filings'); filings.replaceChildren();
  if (job.lca_employer) {
    filings.append(node('div', '', `Matched filing name: ${job.lca_employer}`),
      node('div', '', `Certified LCA filings: ${job.lca_certified || 0}`),
      node('div', '', `Filing years: ${job.lca_years || 'Not available'}`),
      node('div', '', `Certified PERM filings: ${job.perm_certified || 0}`));
  } else { filings.append(node('div', '', 'No employer filing match is recorded for this role.')); }
  $('#detail-status').value = job.status || 'saved';
  $('#detail-notes').value = job.notes || '';
  $('#detail-save-status').textContent = '';
  $('#job-dialog').showModal();
}

function profileHeading() {
  $('#profile-eyebrow').textContent = boot.profile ? 'YOUR PREFERENCES' : 'WORKSPACE SETUP';
  $('#profile-title').textContent = boot.profile ? 'Your search profile.' : 'Make this search yours.';
  $('#profile-intro').textContent = boot.profile ? 'Adjust your search as it changes. Existing exports and advanced settings stay with this profile.' : 'A few details to help SponsorScan find relevant roles. Everything is saved on this computer.';
}

function fillProfile() {
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
  profileHeading(); renderRoleChoices(); renderSkills(); updateAuthorizationNote(); updateIntegrationNote(); showSetupStep(0);
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
  $('#setup-next').textContent = step === 3 ? (boot.profile ? 'Save changes' : 'Create my workspace') : 'Continue \u2192';
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

async function openSetupGuide() {
  lastFocus = document.activeElement;
  $('#setup-guide-text').textContent = 'Loading your setup instructions...';
  $('#setup-guide-dialog').showModal();
  try { const guide = await api('/api/setup-guide'); $('#setup-guide-text').textContent = guide.text; }
  catch (error) { $('#setup-guide-text').textContent = error.message; }
}

function showTask(task) {
  $('#task-status').hidden = !task.message;
  $('#task-status').textContent = task.message;
  $('#fetch-jobs').disabled = task.running;
  $('#load-history').disabled = task.running;
  $('#finish-history').disabled = task.running;
  $('#finish-fetch').disabled = task.running;
}

async function reload(fillForm = false) {
  boot = await api('/api/bootstrap'); token = boot.token;
  $('#profile-label').textContent = boot.profile ? `${boot.profile.name}'s workspace` : 'Set up your workspace';
  $('#connection').textContent = 'Connected locally';
  if (fillForm) fillProfile();
  const data = boot.data;
  $('#data-summary').textContent = `${data.jobs.toLocaleString()} stored jobs / ${data.employers.toLocaleString()} employers / Last fetch: ${dateLabel(data.last_fetched)}${data.failed_boards ? ` / ${data.failed_boards} boards failed` : ''}`;
  showTask(boot.task);
  const [freshReport, records] = await Promise.all([api('/api/jobs'), api('/api/applications')]);
  report = freshReport; applications = records.applications;
  const warnings = [...boot.warnings, ...(report.metadata.warnings || [])];
  if (data.error) warnings.push(data.error);
  if (data.failed_boards) warnings.push('Some job boards failed during collection. Results may be incomplete. Fetch again to retry.');
  $('#warnings').hidden = !warnings.length;
  $('#warnings').replaceChildren(...warnings.map(message => node('p', '', message)));
  const selectedRole = $('#role-filter').value;
  const allRoles = node('option', '', 'All roles'); allRoles.value = '';
  $('#role-filter').replaceChildren(allRoles, ...[...new Set(report.matches.map(job => job.role_family))].filter(Boolean).sort().map(role => {
    const option = node('option', '', role); option.value = role; return option;
  }));
  $('#role-filter').value = selectedRole;
  renderMatches(); renderApplications(); renderSetupComplete(); showView();
  if (boot.task.running && !pollTimer) pollTimer = setTimeout(pollTask, 2000);
}

async function pollTask() {
  pollTimer = null;
  try {
    const task = await api('/api/task'); showTask(task);
    if (task.running) pollTimer = setTimeout(pollTask, 2000);
    else await reload();
  } catch (error) { notice(error.message); }
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
$('#open-setup-guide').addEventListener('click', openSetupGuide);
$('#finish-guide').addEventListener('click', openSetupGuide);
$('#close-setup-guide').addEventListener('click', () => $('#setup-guide-dialog').close());
$('#edit-profile').addEventListener('click', () => {
  setupComplete = false; $('#setup-complete').hidden = true; $('#profile-form').hidden = false;
  profileHeading(); showSetupStep(0, true);
});
for (const selector of ['#match-search', '#role-filter', '#match-sort']) $(selector).addEventListener('input', () => { matchLimit = 60; renderMatches(); });
for (const selector of ['#application-search', '#status-filter']) $(selector).addEventListener('input', () => { applicationLimit = 60; renderApplications(); });
$('#close-detail').addEventListener('click', () => $('#job-dialog').close());
$('#close-manual').addEventListener('click', () => $('#manual-dialog').close());
for (const selector of ['#job-dialog', '#manual-dialog', '#setup-guide-dialog']) $(selector).addEventListener('close', () => lastFocus?.isConnected && lastFocus.focus());
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
    await loadApplications(); $('#manual-dialog').close(); notice('Application added.');
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
  let refreshed = false;
  try {
    const result = await api('/api/profile', data); saved = true;
    boot.profile = result.profile;
    // A later matching error must not make a successful save look like lost work.
    try { await reload(true); refreshed = true; }
    catch (error) { fillProfile(); notice(`Profile saved. ${error.message}`); }
    if (first) {
      setupComplete = true; $('#profile-form').hidden = true; $('#setup-complete').hidden = false;
      $('#profile-title').textContent = 'A good place to start.';
      $('#profile-intro').textContent = 'Your search preferences are saved on this computer.';
      location.hash = 'profile'; showView(); renderSetupComplete(); $('#complete-title').focus();
    } else {
      showSetupStep(3);
      if (refreshed) notice('Profile saved. Your matching preferences have been updated.');
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
