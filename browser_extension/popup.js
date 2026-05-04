const DEFAULT_API_BASE = (window.TAILORRESUME_EXTENSION_CONFIG && window.TAILORRESUME_EXTENSION_CONFIG.API_BASE_URL) || 'http://127.0.0.1:8010';
const state = {
  apiBase: DEFAULT_API_BASE,
  token: '',
  user: null,
  dashboard: null,
  currentNextJob: null,
  jobs: [],
  profiles: [],
  templates: [],
  weeklyReport: null,
  resumes: [],
  currentPage: { url: '', title: '' },
  lastGeneratedResumeId: '',
};

const els = {};

document.addEventListener('DOMContentLoaded', async () => {
  bindElements();
  bindEvents();
  await hydrateCurrentPage();
  await restoreSession();
});

function bindElements() {
  Object.assign(els, {
    loginView: document.getElementById('loginView'),
    appView: document.getElementById('appView'),
    apiBaseInput: document.getElementById('apiBaseInput'),
    identifierInput: document.getElementById('identifierInput'),
    passwordInput: document.getElementById('passwordInput'),
    loginBtn: document.getElementById('loginBtn'),
    loginMessage: document.getElementById('loginMessage'),
    userName: document.getElementById('userName'),
    userMeta: document.getElementById('userMeta'),
    logoutBtn: document.getElementById('logoutBtn'),
    refreshBtn: document.getElementById('refreshBtn'),
    openJobsCount: document.getElementById('openJobsCount'),
    savedResumesCount: document.getElementById('savedResumesCount'),
    weeklyApplicationsCount: document.getElementById('weeklyApplicationsCount'),
    nextJobEmpty: document.getElementById('nextJobEmpty'),
    nextJobCard: document.getElementById('nextJobCard'),
    nextJobTitle: document.getElementById('nextJobTitle'),
    nextJobCompany: document.getElementById('nextJobCompany'),
    nextJobMeta: document.getElementById('nextJobMeta'),
    nextJobProfiles: document.getElementById('nextJobProfiles'),
    nextJobBtn: document.getElementById('nextJobBtn'),
    seeDescriptionBtn: document.getElementById('seeDescriptionBtn'),
    generateResumeBtn: document.getElementById('generateResumeBtn'),
    reportJobBtn: document.getElementById('reportJobBtn'),
    openLinkBtn: document.getElementById('openLinkBtn'),
    jobDescriptionBox: document.getElementById('jobDescriptionBox'),
    profileSelect: document.getElementById('profileSelect'),
    templateSelect: document.getElementById('templateSelect'),
    customPromptInput: document.getElementById('customPromptInput'),
    useAiInput: document.getElementById('useAiInput'),
    generateStatus: document.getElementById('generateStatus'),
    generateResult: document.getElementById('generateResult'),
    generateMode: document.getElementById('generateMode'),
    generateAts: document.getElementById('generateAts'),
    generateHeadline: document.getElementById('generateHeadline'),
    downloadResumeBtn: document.getElementById('downloadResumeBtn'),
    viewSavedResumeBtn: document.getElementById('viewSavedResumeBtn'),
    onlyOpenJobsInput: document.getElementById('onlyOpenJobsInput'),
    jobSearchInput: document.getElementById('jobSearchInput'),
    jobsList: document.getElementById('jobsList'),
    weekStartInput: document.getElementById('weekStartInput'),
    weeklyTotalApplications: document.getElementById('weeklyTotalApplications'),
    weeklyTotalSchedules: document.getElementById('weeklyTotalSchedules'),
    weeklyDays: document.getElementById('weeklyDays'),
    recentApplications: document.getElementById('recentApplications'),
    resumesList: document.getElementById('resumesList'),
    loadResumesBtn: document.getElementById('loadResumesBtn'),
    settingsApiBaseInput: document.getElementById('settingsApiBaseInput'),
    saveSettingsBtn: document.getElementById('saveSettingsBtn'),
    settingsMessage: document.getElementById('settingsMessage'),
    currentPageInfo: document.getElementById('currentPageInfo'),
    reportDialog: document.getElementById('reportDialog'),
    reportForm: document.getElementById('reportForm'),
    reportReason: document.getElementById('reportReason'),
    reportCompany: document.getElementById('reportCompany'),
    reportTitle: document.getElementById('reportTitle'),
    reportLink: document.getElementById('reportLink'),
    reportDescription: document.getElementById('reportDescription'),
    reportNote: document.getElementById('reportNote'),
    reportRegion: document.getElementById('reportRegion'),
    reportMessage: document.getElementById('reportMessage'),
  });
}

function bindEvents() {
  els.loginBtn.addEventListener('click', onLogin);
  els.logoutBtn.addEventListener('click', onLogout);
  els.refreshBtn.addEventListener('click', onRefresh);
  els.nextJobBtn.addEventListener('click', onNextJob);
  els.seeDescriptionBtn.addEventListener('click', toggleDescription);
  els.generateResumeBtn.addEventListener('click', onGenerateResume);
  els.reportJobBtn.addEventListener('click', openReportDialog);
  els.openLinkBtn.addEventListener('click', () => openExternal(state.currentNextJob?.link));
  els.downloadResumeBtn.addEventListener('click', onDownloadLastGeneratedResume);
  els.viewSavedResumeBtn.addEventListener('click', () => switchTab('resumes'));
  els.onlyOpenJobsInput.addEventListener('change', loadJobs);
  els.jobSearchInput.addEventListener('input', debounce(loadJobs, 250));
  els.weekStartInput.addEventListener('change', loadWeeklyReport);
  els.loadResumesBtn.addEventListener('click', loadResumes);
  els.saveSettingsBtn.addEventListener('click', saveSettings);
  els.reportForm.addEventListener('submit', onSubmitReport);
  document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));
}

async function hydrateCurrentPage() {
  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    const active = tabs && tabs[0] ? tabs[0] : null;
    state.currentPage = {
      url: active?.url || '',
      title: active?.title || '',
    };
    els.currentPageInfo.textContent = state.currentPage.url ? `${state.currentPage.title || 'Current page'}\n${state.currentPage.url}` : 'No active page detected.';
  } catch (error) {
    els.currentPageInfo.textContent = 'Could not access current page.';
  }
}

async function restoreSession() {
  const stored = await chrome.storage.local.get(['tailorresumeApiBase', 'tailorresumeToken']);
  state.apiBase = stored.tailorresumeApiBase || DEFAULT_API_BASE;
  state.token = stored.tailorresumeToken || '';
  els.apiBaseInput.value = state.apiBase;
  els.settingsApiBaseInput.value = state.apiBase;

  if (!state.token) {
    showLogin();
    return;
  }
  try {
    const user = await apiGet('/api/ext/auth/me');
    state.user = user;
    await loadAll();
    showApp();
  } catch (error) {
    state.token = '';
    await chrome.storage.local.remove('tailorresumeToken');
    showLogin('Session expired. Please sign in again.', true);
  }
}

async function onLogin() {
  setMessage(els.loginMessage, 'Signing in...');
  const apiBase = normalizeApiBase(els.apiBaseInput.value);
  const identifier = els.identifierInput.value.trim();
  const password = els.passwordInput.value;
  if (!identifier || !password) {
    setMessage(els.loginMessage, 'Enter username/email and password.', true);
    return;
  }
  try {
    const response = await fetch(`${apiBase}/api/ext/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ identifier, password, ttl_days: 30 }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Sign in failed.');
    state.apiBase = apiBase;
    state.token = data.token;
    state.user = data.user;
    await chrome.storage.local.set({ tailorresumeApiBase: apiBase, tailorresumeToken: state.token });
    els.settingsApiBaseInput.value = apiBase;
    await loadAll();
    showApp();
  } catch (error) {
    setMessage(els.loginMessage, error.message || 'Sign in failed.', true);
  }
}

async function onLogout() {
  try { await apiPost('/api/ext/auth/logout', {}); } catch (_) {}
  state.token = '';
  state.user = null;
  await chrome.storage.local.remove('tailorresumeToken');
  showLogin('You are signed out.');
}

async function onRefresh() {
  if (!state.token) {
    await hydrateCurrentPage();
    return;
  }
  await hydrateCurrentPage();
  await loadAll();
}

async function loadAll() {
  await Promise.all([loadDashboard(), loadProfilesAndTemplates(), loadJobs(), loadWeeklyReport(), loadResumes()]);
}

async function loadDashboard() {
  const dashboard = await apiGet('/api/ext/dashboard');
  state.dashboard = dashboard;
  state.currentNextJob = dashboard.next_job;
  els.userName.textContent = dashboard.user.full_name || dashboard.user.username;
  els.userMeta.textContent = `${dashboard.user.username} • ${dashboard.user.email}`;
  els.openJobsCount.textContent = String(dashboard.open_jobs_count || 0);
  els.savedResumesCount.textContent = String(dashboard.generated_resumes_count || 0);
  els.weeklyApplicationsCount.textContent = String(dashboard.weekly_report?.applications_total || 0);
  renderNextJob();
}

async function loadProfilesAndTemplates() {
  const [profiles, templates] = await Promise.all([apiGet('/api/ext/profiles'), apiGet('/api/ext/templates')]);
  state.profiles = profiles || [];
  state.templates = templates || [];
  fillSelect(els.profileSelect, state.profiles.map(item => ({ value: item.id, label: `${item.name}${item.region ? ` [${item.region}]` : ''}` })));
  fillSelect(els.templateSelect, state.templates.map(item => ({ value: item.id, label: item.name })));
}

async function loadJobs() {
  const onlyOpen = !!els.onlyOpenJobsInput.checked;
  const search = encodeURIComponent(els.jobSearchInput.value.trim());
  const jobs = await apiGet(`/api/ext/jobs?only_open=${onlyOpen ? 'true' : 'false'}&search=${search}`);
  state.jobs = jobs || [];
  renderJobs();
}

async function loadWeeklyReport() {
  const weekStart = els.weekStartInput.value || mondayIso(new Date());
  if (!els.weekStartInput.value) els.weekStartInput.value = weekStart;
  const report = await apiGet(`/api/ext/weekly-report?week_start=${encodeURIComponent(weekStart)}`);
  state.weeklyReport = report;
  els.weeklyTotalApplications.textContent = String(report.applications_total || 0);
  els.weeklyTotalSchedules.textContent = String(report.schedules_total || 0);
  renderWeeklyDays(report);
  renderRecentApplications(report.recent_applications || []);
}

async function loadResumes() {
  const resumes = await apiGet('/api/ext/resumes?limit=30');
  state.resumes = resumes || [];
  renderResumes();
}

function renderNextJob() {
  const job = state.currentNextJob;
  if (!job) {
    els.nextJobCard.classList.add('hidden');
    els.nextJobEmpty.classList.remove('hidden');
    return;
  }
  els.nextJobEmpty.classList.add('hidden');
  els.nextJobCard.classList.remove('hidden');
  els.nextJobTitle.textContent = job.job_title || 'Untitled role';
  els.nextJobCompany.textContent = job.company || 'Unknown company';
  els.nextJobMeta.textContent = [job.region, job.remaining_profile_ids?.length ? `${job.remaining_profile_ids.length} profile slots` : ''].filter(Boolean).join(' • ');
  els.nextJobProfiles.innerHTML = '';
  (job.matching_profile_names || []).forEach(name => els.nextJobProfiles.appendChild(makePill(name)));
  els.jobDescriptionBox.textContent = job.description || job.description_preview || 'No description available.';
  els.jobDescriptionBox.classList.add('hidden');
}

function renderJobs() {
  els.jobsList.innerHTML = '';
  if (!state.jobs.length) {
    els.jobsList.innerHTML = '<div class="empty-state">No jobs found.</div>';
    return;
  }
  state.jobs.forEach(job => {
    const card = document.createElement('div');
    card.className = 'list-card';
    card.innerHTML = `
      <h3>${escapeHtml(job.job_title || 'Untitled role')}</h3>
      <div class="meta-row">${escapeHtml(job.company || '')} • ${escapeHtml(job.region || '')}</div>
      <div class="small-muted">${escapeHtml((job.description_preview || '').slice(0, 220))}</div>
      <div class="pill-row"></div>
      <div class="actions">
        <button class="secondary-btn small">Use on dashboard</button>
        <button class="secondary-btn small">Open link</button>
        <button class="secondary-btn small">View description</button>
      </div>
      <div class="description-box hidden">${escapeHtml(job.description || '')}</div>
    `;
    const pillRow = card.querySelector('.pill-row');
    (job.matching_profile_names || []).forEach(name => pillRow.appendChild(makePill(name)));
    const [useBtn, openBtn, descBtn] = card.querySelectorAll('button');
    const descBox = card.querySelector('.description-box');
    useBtn.addEventListener('click', () => {
      state.currentNextJob = job;
      renderNextJob();
      switchTab('dashboard');
    });
    openBtn.addEventListener('click', () => openExternal(job.link));
    descBtn.addEventListener('click', () => descBox.classList.toggle('hidden'));
    els.jobsList.appendChild(card);
  });
}

function renderWeeklyDays(report) {
  const order = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
  const labels = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' };
  els.weeklyDays.innerHTML = '';
  order.forEach(key => {
    const card = document.createElement('div');
    card.className = 'day-card';
    card.innerHTML = `
      <div class="day-name">${labels[key]}</div>
      <div class="day-value">${report.daily_counts?.[key] || 0}</div>
      <div class="day-sub">Sched ${report.daily_schedule_counts?.[key] || 0}</div>
    `;
    els.weeklyDays.appendChild(card);
  });
}

function renderRecentApplications(items) {
  els.recentApplications.innerHTML = '';
  if (!items.length) {
    els.recentApplications.innerHTML = '<div class="empty-state">No applications in this week.</div>';
    return;
  }
  items.slice(0, 8).forEach(item => {
    const card = document.createElement('div');
    card.className = 'list-card';
    card.innerHTML = `
      <h3>${escapeHtml(item.job_title || 'Resume')}</h3>
      <div class="meta-row">${escapeHtml(item.company || '')} • ${escapeHtml(item.profile_name || '')}</div>
      <div class="small-muted">${escapeHtml(item.created_at || '')}</div>
    `;
    els.recentApplications.appendChild(card);
  });
}

function renderResumes() {
  els.resumesList.innerHTML = '';
  if (!state.resumes.length) {
    els.resumesList.innerHTML = '<div class="empty-state">No saved resumes yet.</div>';
    return;
  }
  state.resumes.forEach(item => {
    const card = document.createElement('div');
    card.className = 'list-card';
    card.innerHTML = `
      <h3>${escapeHtml(item.job_title || 'Resume')}</h3>
      <div class="meta-row">${escapeHtml(item.company || '')} • ${escapeHtml(item.profile_name || '')}</div>
      <div class="small-muted">ATS ${item.ats_score || 0} • ${escapeHtml(item.created_at || '')}</div>
      <div class="actions">
        <button class="primary-btn small">Download</button>
      </div>
    `;
    card.querySelector('button').addEventListener('click', () => downloadSavedResume(item.saved_resume_id, item.download_filename || 'resume.pdf'));
    els.resumesList.appendChild(card);
  });
}

async function onNextJob() {
  const currentId = state.currentNextJob?.id || '';
  const nextJob = await apiGet(`/api/ext/jobs/next?current_job_id=${encodeURIComponent(currentId)}`);
  state.currentNextJob = nextJob;
  renderNextJob();
}

function toggleDescription() {
  els.jobDescriptionBox.classList.toggle('hidden');
}

async function onGenerateResume() {
  if (!state.currentNextJob?.id) {
    setMessage(els.generateStatus, 'No job selected.', true);
    return;
  }
  setMessage(els.generateStatus, 'Generating resume...');
  els.generateResult.classList.add('hidden');
  const payload = {
    job_id: state.currentNextJob.id,
    profile_id: els.profileSelect.value || '',
    template_id: els.templateSelect.value || '',
    custom_prompt: els.customPromptInput.value.trim(),
    use_ai: !!els.useAiInput.checked,
    clean_generation: true,
    save_generated: true,
  };
  try {
    const result = await apiPost('/api/ext/resumes/generate', payload);
    state.lastGeneratedResumeId = result.saved_resume_id || '';
    els.generateMode.textContent = `Mode: ${result.mode || 'unknown'}`;
    els.generateAts.textContent = `ATS score: ${result.ats_score || 0}`;
    els.generateHeadline.textContent = result.resume?.headline || 'Resume generated';
    els.generateResult.classList.remove('hidden');
    setMessage(els.generateStatus, 'Resume generated successfully.', false, true);
    await loadDashboard();
    await loadResumes();
  } catch (error) {
    setMessage(els.generateStatus, error.message || 'Resume generation failed.', true);
  }
}

async function onDownloadLastGeneratedResume() {
  if (!state.lastGeneratedResumeId) {
    setMessage(els.generateStatus, 'No saved resume to download yet.', true);
    return;
  }
  await downloadSavedResume(state.lastGeneratedResumeId, 'resume.pdf');
}

function openReportDialog() {
  const job = state.currentNextJob || {};
  els.reportReason.value = '';
  els.reportCompany.value = job.company || '';
  els.reportTitle.value = job.job_title || state.currentPage.title || '';
  els.reportLink.value = job.link || state.currentPage.url || '';
  els.reportDescription.value = job.description || '';
  els.reportNote.value = '';
  els.reportRegion.value = job.region || 'US';
  setMessage(els.reportMessage, '');
  els.reportDialog.showModal();
}

async function onSubmitReport(event) {
  event.preventDefault();
  const payload = {
    reason: els.reportReason.value.trim(),
    job_id: state.currentNextJob?.id || '',
    company: els.reportCompany.value.trim(),
    job_title: els.reportTitle.value.trim(),
    link: els.reportLink.value.trim(),
    description: els.reportDescription.value.trim(),
    note: els.reportNote.value.trim(),
    region: els.reportRegion.value || 'US',
  };
  if (!payload.reason) {
    setMessage(els.reportMessage, 'Reason is required.', true);
    return;
  }
  try {
    await apiPost('/api/ext/jobs/report', payload);
    setMessage(els.reportMessage, 'Job report submitted.', false, true);
    setTimeout(() => els.reportDialog.close(), 500);
    await loadJobs();
  } catch (error) {
    setMessage(els.reportMessage, error.message || 'Could not submit report.', true);
  }
}

async function saveSettings() {
  const apiBase = normalizeApiBase(els.settingsApiBaseInput.value);
  state.apiBase = apiBase;
  await chrome.storage.local.set({ tailorresumeApiBase: apiBase });
  els.apiBaseInput.value = apiBase;
  setMessage(els.settingsMessage, 'Settings saved.', false, true);
}

function showLogin(message = '', isError = false) {
  els.loginView.classList.remove('hidden');
  els.appView.classList.add('hidden');
  setMessage(els.loginMessage, message, isError);
}

function showApp() {
  els.loginView.classList.add('hidden');
  els.appView.classList.remove('hidden');
}

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.toggle('active', panel.id === `tab-${name}`));
}

async function downloadSavedResume(savedResumeId, filename) {
  try {
    const response = await fetch(`${state.apiBase}/api/ext/resumes/${encodeURIComponent(savedResumeId)}/download?fmt=pdf`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || 'Download failed.');
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const downloadId = await chrome.downloads.download({ url, filename: filename || 'resume.pdf', saveAs: true });
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    return downloadId;
  } catch (error) {
    setMessage(els.generateStatus, error.message || 'Download failed.', true);
  }
}

async function apiGet(path) {
  return apiRequest(path, { method: 'GET' });
}

async function apiPost(path, body) {
  return apiRequest(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}

async function apiRequest(path, options = {}) {
  const response = await fetch(`${state.apiBase}${path}`, {
    ...options,
    headers: {
      ...(options.headers || {}),
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
    },
  });
  if (response.status === 401) {
    throw new Error('Unauthorized. Please sign in again.');
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.detail || 'Request failed.');
  return data;
}

function fillSelect(select, options) {
  select.innerHTML = '';
  options.forEach(option => {
    const el = document.createElement('option');
    el.value = option.value;
    el.textContent = option.label;
    select.appendChild(el);
  });
}

function makePill(text) {
  const span = document.createElement('span');
  span.className = 'pill';
  span.textContent = text;
  return span;
}

function setMessage(element, text, isError = false, isSuccess = false) {
  element.textContent = text || '';
  element.classList.toggle('error', !!text && isError);
  element.classList.toggle('success', !!text && isSuccess);
}

function escapeHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function normalizeApiBase(value) {
  const trimmed = String(value || '').trim();
  return (trimmed || DEFAULT_API_BASE).replace(/\/$/, '');
}

function mondayIso(date) {
  const value = new Date(date);
  const day = value.getDay();
  const diff = (day === 0 ? -6 : 1 - day);
  value.setDate(value.getDate() + diff);
  return value.toISOString().slice(0, 10);
}

function openExternal(url) {
  if (!url) return;
  chrome.tabs.create({ url });
}

function debounce(fn, delay) {
  let handle = null;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), delay);
  };
}
