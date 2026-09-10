const brand = window.__APP_BRAND__ || {};
const byId = (id) => document.getElementById(id);
const name = brand.name || 'Knowledge Workspace';
document.documentElement.style.setProperty('--brand-primary', brand.primaryColor || '#4f5bd5');
document.title = `${name} · 账户`;
byId('brand-name').textContent = name;
byId('brand-tagline').textContent = brand.tagline || '';
const mark = byId('brand-mark');
const markText = brand.mark || 'K';
mark.textContent = markText;
if (brand.logoUrl) {
  const image = document.createElement('img');
  image.alt = '';
  image.src = brand.logoUrl;
  image.onerror = () => { mark.textContent = markText; };
  mark.replaceChildren(image);
}

const api = async (url, options = {}) => {
  const response = await fetch(url, {
    credentials: 'same-origin',
    ...options,
    headers: { Accept: 'application/json', ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请稍后重试。');
  return data;
};
const esc = (value) => String(value || '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[char]));

const setError = (text = '') => {
  const node = byId('account-error');
  node.textContent = text;
  node.hidden = !text;
};

const setIdentity = (account) => {
  const identity = byId('account-identity');
  const roles = Array.isArray(account.roles) && account.roles.length ? account.roles.join('、') : '未分配应用角色';
  identity.innerHTML = `<b>${esc(account.username || account.subject || '本地账号')}</b><span>${account.source === 'local' ? '本地账号' : '企业身份账号'} · ${esc(roles)}</span>`;
};

const loadAccount = async () => {
  try {
    const account = await api('/api/v1/account');
    setIdentity(account);
    byId('password-form').hidden = account.source !== 'local';
    if (account.source !== 'local') byId('account-identity').insertAdjacentHTML('beforeend', '<small>密码由企业身份提供商管理。</small>');
  } catch (_) {
    window.location.assign('/login');
  }
};

byId('password-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = byId('password-submit');
  const current = form.elements.current_password.value;
  const next = form.elements.new_password.value;
  if (current.length < 16 || current.length > 4096 || next.length < 16 || next.length > 128) {
    setError('密码长度必须为 16–128 个字符。');
    return;
  }
  setError();
  submit.disabled = true;
  submit.textContent = '正在更新…';
  try {
    await api('/api/v1/account/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    form.reset();
    byId('account-success').textContent = '密码已更新，请重新登录。';
    byId('account-success').hidden = false;
    setTimeout(() => window.location.assign('/login'), 900);
  } catch (error) {
    setError(error.message || '密码更新失败，请稍后重试。');
    submit.disabled = false;
    submit.innerHTML = '更新密码 <b aria-hidden="true">→</b>';
  }
});

loadAccount();
