'use strict';
const languageQuery=new URLSearchParams(location.search).get('lang');
const languageCookie=document.cookie.split('; ').find(c=>c.startsWith('wasla_lang='))?.split('=')[1];
const LANG=['ar','en'].includes(languageQuery)?languageQuery:(languageCookie==='en'?'en':'ar');
document.cookie='wasla_lang='+LANG+'; Path=/; Max-Age=31536000; SameSite=Lax'+(location.protocol==='https:'?'; Secure':'');
document.documentElement.lang=LANG;
document.documentElement.dir=LANG==='ar'?'rtl':'ltr';
function tr(value){if(LANG==='ar')return value;const key=value.trim(),translated=window.WASLA_EN[key];return translated===undefined?value.replace(/^(يرجى التحقق من الحقل:|قيمة غير صالحة:)/,m=>window.WASLA_EN[m]||m):value.replace(key,()=>translated)}
function tMarkup(value){return LANG==='ar'?value:value.replace(/[\u0600-\u06ff][^<>"'`\n]*/g,tr)}
function languageLink(){const url=new URL(location.href);url.searchParams.set('lang',LANG==='ar'?'en':'ar');return `<a class="language-link" href="${url.pathname+url.search}" lang="${LANG==='ar'?'en':'ar'}" hreflang="${LANG==='ar'?'en':'ar'}">${LANG==='ar'?'English':'العربية'}</a>`}
if(LANG==='en'){
 document.querySelector('.skip').textContent='Skip to content';
 document.querySelector('meta[name="description"]').content='Wasla connects sales operations, teams, customers and opportunities in one workspace.';
 document.querySelector('.loading').textContent='Loading Wasla…';
}
