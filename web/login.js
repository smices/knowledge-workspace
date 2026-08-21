const brand = window.__APP_BRAND__ || {};
const name = brand.name || 'Knowledge Workspace';
const mark = document.getElementById('brand-mark');

document.documentElement.style.setProperty('--brand-primary', brand.primaryColor || '#4f5bd5');
document.title = `${name} · 登录`;
document.getElementById('brand-name').textContent = name;
document.getElementById('brand-tagline').textContent = brand.tagline || '';
mark.textContent = brand.mark || 'K';
if (brand.logoUrl) {
  const image = document.createElement('img');
  image.alt = '';
  image.src = brand.logoUrl;
  image.onerror = () => { mark.textContent = brand.mark || 'K'; };
  mark.replaceChildren(image);
}
