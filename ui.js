'use strict';
const $ = selector => document.querySelector(selector);
const UI_VERSION = '1.2.1';
const digits = value => new Intl.NumberFormat('fa-IR', {maximumFractionDigits: 3}).format(value);
const drafts = new Map();
let inboundRows = [];
let fetching = false;
let toastTimer;

function tell(text, error = false) {
  $('#message-text').textContent = text;
  $('#message').classList.toggle('error', error);
}

function toast(text, error = false) {
  clearTimeout(toastTimer);
  const element = $('#toast');
  element.querySelector('span').textContent = text;
  element.classList.toggle('error', error);
  element.hidden = false;
  toastTimer = setTimeout(() => { element.hidden = true; }, 4800);
}

function icon(symbol) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + symbol);
  svg.append(use);
  svg.setAttribute('aria-hidden', 'true');
  return svg;
}

async function request(options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch('api', {credentials: 'include', cache: 'no-store', signal: controller.signal, ...options});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'ارتباط با سرویس برقرار نشد.');
    return body;
  } finally { clearTimeout(timer); }
}

function makeRow(row) {
  const article = document.createElement('article');
  article.className = 'inbound-row';
  article.dataset.inboundId = row.id;
  const identity = document.createElement('div');
  identity.className = 'route-identity';
  const mark = document.createElement('span');
  mark.className = 'route-icon';
  mark.append(icon('grid'));
  const information = document.createElement('div');
  const title = document.createElement('div');
  title.className = 'route-name';
  title.textContent = row.remark || 'اینباند ' + digits(row.id);
  const meta = document.createElement('div');
  meta.className = 'route-info';
  for (const text of ['PORT ' + row.port, row.protocol.toUpperCase(), 'ID ' + row.id]) {
    if (meta.childNodes.length) meta.append(document.createElement('i'));
    const value = document.createElement('span');
    value.textContent = text;
    meta.append(value);
  }
  information.append(title, meta);
  identity.append(mark, information);

  const field = document.createElement('label');
  field.className = 'factor-field';
  const multiply = document.createElement('span');
  multiply.textContent = '×';
  const input = document.createElement('input');
  input.className = 'factor-input';
  input.type = 'number'; input.min = '0'; input.max = '10'; input.step = '0.001';
  input.inputMode = 'decimal';
  input.value = drafts.has(row.id) ? drafts.get(row.id) : row.factor;
  input.setAttribute('aria-label', 'ضریب حجم ' + (row.remark || row.port));
  input.disabled = row.protocol !== 'vless';
  field.append(multiply, input);

  const example = document.createElement('div');
  example.className = 'charge-example';
  const amount = document.createElement('strong');
  const unit = document.createElement('span'); unit.textContent = 'گیگ کسر';
  const caption = document.createElement('small'); caption.textContent = 'برای ۱ گیگ مصرف واقعی';
  example.append(amount, unit, caption);
  const button = document.createElement('button');
  button.className = 'save-button'; button.type = 'button';
  let saving = false;
  function update() {
    const value = input.valueAsNumber;
    const valid = input.value !== '' && input.validity.valid;
    const changed = valid && value !== Number(row.factor);
    field.classList.toggle('changed', changed);
    amount.textContent = valid ? digits(value) : '—';
    unit.textContent = valid && value === 0 ? 'رایگان' : 'گیگ کسر';
    if (valid && value === 0) amount.textContent = '';
    button.disabled = saving || !changed || row.protocol !== 'vless';
    if (!saving) button.textContent = 'ذخیره';
    button.classList.remove('saved');
  }
  input.addEventListener('input', () => { drafts.set(row.id, input.value); update(); });
  button.addEventListener('click', async () => {
    if (!input.reportValidity() || input.value === '' || saving) return;
    const submitted = input.value;
    saving = true; button.disabled = true; input.disabled = true; button.textContent = 'ثبت…';
    try {
      const result = await request({method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({inbound_id: row.id, factor: submitted})});
      row.factor = Number(submitted); drafts.delete(row.id);
      updateMetrics();
      tell(result.message || 'ضریب این مسیر ذخیره شد.');
      toast('ضریب «' + (row.remark || row.port) + '» ذخیره شد.');
      button.replaceChildren(icon('check'), document.createTextNode('ثبت شد'));
      button.classList.add('saved');
      field.classList.remove('changed');
    } catch (error) {
      tell(error.name === 'AbortError' ? 'پاسخ ذخیره دریافت نشد؛ برای بررسی وضعیت، صفحه را به‌روز کنید.' : error.message, true);
      toast('ذخیره تأیید نشد؛ مقدار واردشده حفظ شده است.', true);
      button.textContent = 'دوباره';
    } finally {
      saving = false; input.disabled = row.protocol !== 'vless';
      button.disabled = input.valueAsNumber === Number(row.factor);
    }
  });
  update();
  article.append(identity, field, example, button);
  return article;
}

function updateMetrics() {
  $('#total-count').textContent = digits(inboundRows.length);
  $('#custom-count').textContent = digits(inboundRows.filter(row => Number(row.factor) !== 1).length);
  $('#nav-count').textContent = digits(inboundRows.length);
}

function renderRows() {
  const query = $('#search').value.trim().toLocaleLowerCase().replace(/[۰-۹]/g, n => '۰۱۲۳۴۵۶۷۸۹'.indexOf(n));
  const shown = inboundRows.filter(row => [row.remark, row.port, row.protocol, row.id].join(' ').toLocaleLowerCase().includes(query));
  $('#list').replaceChildren(...shown.map(makeRow));
  $('#empty').hidden = shown.length > 0;
  $('#empty').textContent = inboundRows.length ? 'اینباندی با این مشخصات پیدا نشد.' : 'هنوز اینباندی برای نمایش وجود ندارد.';
  $('#visible-count').textContent = digits(shown.length) + ' از ' + digits(inboundRows.length) + ' مسیر';
}

async function load(manual = false) {
  if (fetching || (!manual && (document.activeElement?.classList.contains('factor-input') || drafts.size))) return;
  fetching = true; $('#refresh').disabled = true; $('#refresh').classList.add('busy');
  try {
    const body = await request();
    inboundRows = body.inbounds;
    renderLicense(body.license);
    updateMetrics(); renderRows();
    const sync = body.metadata_ok_at ? new Date(body.metadata_ok_at * 1000) : null;
    $('#sync-time').textContent = sync ? sync.toLocaleTimeString('fa-IR', {hour: '2-digit', minute: '2-digit'}) : '—';
    $('#sync-date').textContent = sync ? sync.toLocaleDateString('fa-IR', {day: 'numeric', month: 'long'}) : 'در انتظار اطلاعات';
    $('#version').textContent = 'v' + UI_VERSION;
    $('#connection-text').textContent = body.error ? 'نیازمند بررسی' : 'متصل و همگام';
    $('#connection-dot').classList.toggle('connected', !body.error);
    tell(body.error || 'همه‌چیز آماده است. ضریب هر مسیر را جدا تنظیم و ذخیره کن.', Boolean(body.error));
    if (manual) toast('اطلاعات اینباندها به‌روز شد.');
  } catch (error) {
    $('#connection-text').textContent = 'ورود یا اتصال لازم است';
    $('#connection-dot').classList.remove('connected');
    if (!inboundRows.length) { $('#list').replaceChildren(); $('#empty').hidden = false; $('#empty').textContent = 'پس از ورود به ثنایی در همین مرورگر، دکمهٔ به‌روزرسانی را بزن.'; }
    tell(error.name === 'AbortError' ? 'پاسخ سرویس دریافت نشد. دوباره امتحان کن.' : error.message + '؛ سپس «به‌روزرسانی» را بزن.', true);
  } finally { fetching = false; $('#refresh').disabled = false; $('#refresh').classList.remove('busy'); }
}

function calculate() {
  const usage = $('#calc-usage').valueAsNumber;
  const factor = $('#calc-factor').valueAsNumber;
  const valid = $('#calc-usage').value !== '' && $('#calc-factor').value !== '' && $('#calc-usage').validity.valid && $('#calc-factor').validity.valid;
  $('#calc-result').replaceChildren(document.createTextNode(valid ? digits(usage * factor) : '—'));
  const unit = document.createElement('small'); unit.textContent = 'گیگابایت'; $('#calc-result').append(unit);
  document.querySelectorAll('[data-preset]').forEach(button => button.classList.toggle('selected', Number(button.dataset.preset) === factor));
}

$('#search').addEventListener('input', renderRows);
$('#refresh').addEventListener('click', () => load(true));
$('#calc-usage').addEventListener('input', calculate);
$('#calc-factor').addEventListener('input', calculate);
document.querySelectorAll('[data-preset]').forEach(button => button.addEventListener('click', () => { $('#calc-factor').value = button.dataset.preset; calculate(); }));
document.querySelectorAll('.nav-item').forEach(link => link.addEventListener('click', () => { document.querySelectorAll('.nav-item').forEach(item => item.classList.remove('active')); link.classList.add('active'); }));
document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) { event.preventDefault(); $('#search').focus(); } });
calculate(); load();
setInterval(() => { if (!document.hidden) load(); }, 30000);

const LICENSE_PRODUCT="ZARIB";
async function licenseRequest(action,body){const response=await fetch(new URL('api/license/'+action,location.href),{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const value=await response.json();if(!response.ok)throw new Error(value.error||'عملیات انجام نشد');return value;}
const licenseNotice=toast;

function renderLicense(value) {
  const info=value||{status:'missing',can_manage:false};
  const names={active:'فعال',missing:'فعال نشده',invalid:'نامعتبر',revoked:'متوقف شده',expired:'نیازمند تمدید'};
  document.querySelector('#license-product').textContent=LICENSE_PRODUCT;
  document.querySelector('#license-box').dataset.state=info.status;
  document.querySelector('#license-status').textContent=(names[info.status]||info.status)+(info.permanent?' · دائمی':info.expires_at?' · پایان: '+new Date(info.expires_at*1000).toLocaleString('fa-IR'):'')+(info.offline_grace?' · استفاده از مهلت آفلاین':'')+(info.last_error?' · '+info.last_error:'');
}
document.querySelector('#license-form').onsubmit=async event=>{
  event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;
  try{const value=await licenseRequest('activate',{key:document.querySelector('#license-key').value,new_installation:document.querySelector('#license-new').checked});document.querySelector('#license-key').value='';document.querySelector('#license-new').checked=false;renderLicense(value);licenseNotice('لایسنس فعال شد.');}catch(error){licenseNotice(error.message,true);}finally{button.disabled=false;}
};
document.querySelector('#license-refresh').onclick=async event=>{
  event.target.disabled=true;try{renderLicense(await licenseRequest('refresh',{}));licenseNotice('وضعیت اعتبار بررسی شد.');}catch(error){licenseNotice(error.message,true);}finally{event.target.disabled=false;}
};
