const brand = window.__APP_BRAND__ || {};
const byId = (id) => document.getElementById(id);

const name = brand.name || 'Knowledge Workspace';
document.documentElement.style.setProperty('--brand-primary', brand.primaryColor || '#4f5bd5');
document.title = `${name} · 登录`;
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

const safeNext = (value) => {
  try {
    const parsed = new URL(value || '/home', window.location.origin);
    return parsed.origin === window.location.origin && parsed.pathname.startsWith('/') && !String(value).startsWith('//')
      ? `${parsed.pathname}${parsed.search}${parsed.hash}`
      : '/home';
  } catch (_) {
    return '/home';
  }
};

const setError = (text = '') => {
  const node = byId('login-error');
  node.textContent = text;
  node.hidden = !text;
};

const setLoading = (loading) => {
  const form = byId('local-login-form');
  const submit = byId('local-login-submit');
  const fields = byId('local-login-fields');
  const label = form.dataset.submitLabel || '本地账号登录';
  form.setAttribute('aria-busy', loading ? 'true' : 'false');
  submit.disabled = loading;
  fields.querySelectorAll('input').forEach((input) => { input.disabled = loading; });
  submit.innerHTML = loading ? '正在登录…' : `${label} <b aria-hidden="true">→</b>`;
};

const showOptions = (options) => {
  const local = Boolean(options.local_login);
  const idp = Boolean(options.idp_login);
  const form = byId('local-login-form');
  const localFields = byId('local-login-fields');
  const localSubmit = byId('local-login-submit');
  const idpLogin = byId('idp-login');
  const description = byId('login-description');
  localFields.hidden = !local;
  localSubmit.hidden = !local;
  if (idpLogin) idpLogin.hidden = !idp;
  if (!local && idp && description) {
    description.textContent = '使用企业身份完成登录。我们不会在这里收集或保存企业身份提供商密码。';
  } else if (local && idp && description) {
    description.textContent = '使用本地工作区账号，或通过企业身份完成登录。企业身份提供商密码不会经过本应用。';
  }
  if (!local && !idp) {
    setError('当前没有可用的登录方式，请联系管理员。');
    localSubmit.hidden = true;
  }
  form.dataset.localEnabled = String(local);
};

const loadOptions = async () => {
  try {
    const response = await fetch('/auth/options', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    const options = await response.json();
    if (!response.ok || options.registration_open !== false) throw new Error('登录配置暂不可用。');
    showOptions(options);
  } catch (_) {
    setError('登录配置暂不可用，请稍后重试。');
    byId('local-login-submit').disabled = true;
  }
};

byId('local-login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (form.dataset.localEnabled !== 'true' || form.getAttribute('aria-busy') === 'true') return;
  const username = form.elements.username.value.trim();
  const password = form.elements.password.value;
  if (username.length < 1 || username.length > 64 || password.length < 16 || password.length > 4096) {
    setError('用户名或密码格式不正确。');
    return;
  }
  setError();
  setLoading(true);
  try {
    const body = new URLSearchParams({ username, password, next: safeNext(form.dataset.loginNext) });
    const response = await fetch('/auth/local/login', {
      method: 'POST', body, credentials: 'same-origin', redirect: 'follow',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
    });
    if (!response.ok) {
      const failure = new Error('login failed');
      failure.status = response.status;
      throw failure;
    }
    window.location.assign(response.url || safeNext(form.dataset.loginNext));
  } catch (error) {
    const status = Number(error?.status || 0);
    const message = status === 429
      ? '尝试次数过多，请稍后再试。'
      : status === 502 || status === 503 || status >= 500 || error instanceof TypeError
        ? '登录服务暂不可用，请稍后重试。'
        : '用户名或密码不正确。';
    setError(message);
    setLoading(false);
  }
});

loadOptions();
