/* Shared accessible feedback: a modal error belongs in that modal's top layer. */
(() => {
  'use strict';
  const operations = {'routes/add':'ساخت کشور جدید','hosts/create':'افزودن کانفیگ','hosts/update':'ویرایش کانفیگ','hosts/delete':'حذف کانفیگ','existing/configure':'تنظیم خروجی','engine':'نصب موتور','connect':'اتصال پنل','login':'ورود','factor_save':'ذخیرهٔ ضریب','license/activate':'فعال‌سازی لایسنس','license/refresh':'بررسی اعتبار'};
  const safe = value => String(value ?? '').slice(0,1000).replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/@]+@/gi,'$1[حذف‌شده]@').replace(/\b(token|password|secret|authorization|license_key)\s*[:=]\s*[^\s,;]+/gi,'$1=[حذف‌شده]').replace(/FS-[A-Za-z0-9_-]{20,}/g,'[کلید حذف‌شده]');
  class UIError extends Error {
    constructor(message, fields={}) {super(safe(message));this.name='UIError';Object.assign(this,fields);}
  }
  function explain(value, context={}) {
    if(value instanceof UIError)return value;
    const message=safe(value?.message ?? value ?? 'عملیات انجام نشد.');
    return new UIError(message,{title:context.operation ? (operations[context.operation]||'درخواست')+' انجام نشد' : 'عملیات نیاز به بررسی دارد',hint:'علت را در پیام بالا ببین؛ اطلاعات فرم حفظ شده است.',code:'validation_failed',...context});
  }
  function hostFor(context) {
    const target=context.target instanceof Element ? context.target : null;
    let dialog=target?.closest('dialog[open]');
    if(!dialog && !context.target)dialog=document.activeElement?.closest('dialog[open]') || [...document.querySelectorAll('dialog[open]')].at(-1);
    if(dialog)return {host:dialog.querySelector('form')||dialog,dialog};
    const region=context.region || target?.closest('[data-feedback-region]');
    if(region && region.getClientRects().length)return {host:region,dialog:null};
    const open=[...document.querySelectorAll('dialog[open]')].at(-1);
    if(context.background && open)return {host:open.querySelector('form')||open,dialog:open};
    return {host:document.querySelector('main')||document.body,dialog:null};
  }
  function clear(context={}) {
    const {host}=hostFor(context);
    const selector=context.all?':scope > .feedback-alert':context.background?':scope > .feedback-alert[data-background]':':scope > .feedback-alert:not([data-background])';
    host.querySelectorAll(selector).forEach(el=>el.remove());
  }
  function show(value, context={}) {
    const info=explain(value,context),{host,dialog}=hostFor(context);
    const selector=context.background ? ':scope > .feedback-alert[data-background]' : ':scope > .feedback-alert:not([data-background])';
    host.querySelectorAll(selector).forEach(el=>el.remove());
    const box=document.createElement('section');box.className='feedback-alert';box.setAttribute('role',context.background?'status':'alert');box.tabIndex=-1;
    if(context.background)box.dataset.background='true';
    const title=document.createElement('strong');title.textContent=info.title||'درخواست کامل نشد';
    const detail=document.createElement('p');detail.textContent=info.message;
    const hint=document.createElement('p');hint.className='feedback-hint';hint.textContent=info.hint||'اطلاعات فرم حفظ شده است؛ علت بالا را بررسی کن.';
    const code=document.createElement('small');code.dir='auto';code.textContent='کد: '+(info.code||'unknown')+(info.id?' · شناسه: '+info.id:'');
    const dismiss=document.createElement('button');dismiss.type='button';dismiss.className='feedback-dismiss';dismiss.setAttribute('aria-label','بستن پیام خطا');dismiss.textContent='×';dismiss.onclick=()=>box.remove();
    box.append(title,dismiss,detail,hint,code);
    const heading=host.querySelector(':scope > .dialog-heading');
    if(heading)heading.after(box);else host.prepend(box);
    if(!context.background){box.focus({preventScroll:true});box.scrollIntoView({block:'nearest',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});}
    return info;
  }
  async function request(url, options={}, context={}) {
    const controller=new AbortController(),timeoutMs=context.timeoutMs||30000;
    const timeout=setTimeout(()=>controller.abort(),timeoutMs);
    const method=(options.method||'GET').toUpperCase();
    try {
      const response=await fetch(url,{credentials:'same-origin',cache:'no-store',...options,signal:controller.signal});
      let body;
      try {body=await response.json();} catch {
        throw new UIError('سرویس پاسخ قابل خواندن برنگرداند؛ محتوای پاسخ نمایش داده نمی‌شود.',{code:'invalid_response',title:'پاسخ سرویس نامعتبر بود',hint:'وضعیت سرویس مدیریت یا واسطهٔ HTTPS را بررسی کن. '+(method==='GET'?'دوباره اطلاعات را بخوان.':'قبل از ثبت دوباره، وضعیت عملیات را بررسی کن.'),status:response.status,operation:context.operation});
      }
      if(body===null || typeof body!=='object')throw new UIError('قالب پاسخ سرویس با دادهٔ مورد انتظار پنل سازگار نیست.',{code:'invalid_response',title:'پاسخ سرویس نامعتبر بود',hint:'وضعیت سرویس مدیریت و آدرس آن را بررسی کن؛ پاسخ خالی یا متن ساده، اطلاعات پنل محسوب نمی‌شود.',status:response.status,operation:context.operation});
      if(!response.ok)throw new UIError(body.error||body.detail||'درخواست پذیرفته نشد.',{code:body.error_code||'http_'+response.status,title:body.error_title||((operations[context.operation]||'درخواست')+' انجام نشد'),hint:body.error_hint||(response.status===401?'دوباره وارد پنل شو.':'پیام بالا را بررسی کن؛ اطلاعات فرم حفظ شده است.'),status:response.status,id:body.error_id,mode:body.mode,operation:context.operation});
      return body;
    } catch(error) {
      if(error instanceof UIError)throw error;
      const timedOut=error?.name==='AbortError';
      throw new UIError(timedOut?'مهلت دریافت پاسخ ('+Math.round(timeoutMs/1000)+' ثانیه) تمام شد.':'ارتباط مرورگر با سرویس برقرار نشد یا قطع شد.',{code:timedOut?'request_timeout':'network_error',title:timedOut?'پاسخ سرور به‌موقع نرسید':'خطای ارتباط با سرور',hint:method==='GET'?'اتصال اینترنت و فعال‌بودن سرویس مدیریت را بررسی کن و اطلاعات را تازه کن.':'نتیجهٔ ثبت هنوز معلوم نیست. قبل از ارسال دوباره، وضعیت عملیات یا مسیر را بررسی کن؛ اطلاعات فرم حفظ شده است.',operation:context.operation});
    } finally {clearTimeout(timeout);}
  }
  window.FarstarFeedback=Object.freeze({show,clear,explain,request,UIError});
})();
