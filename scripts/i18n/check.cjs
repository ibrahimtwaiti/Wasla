const fs=require('node:fs'),vm=require('node:vm'),acorn=require('acorn');
const context={window:{}};vm.runInNewContext(fs.readFileSync('wasla/static/en.js','utf8'),context);
const dict=context.window.WASLA_EN,missing=new Set();
function scan(n){if(!n||typeof n!=='object')return;if(n.type==='CallExpression'&&['tr','tMarkup'].includes(n.callee?.name)&&n.arguments[0]?.type==='Literal'){
 const value=n.arguments[0].value;if(typeof value==='string'){
 const parts=n.callee.name==='tr'?[value]:(value.match(/[\u0600-\u06ff][^<>"'`\n]*/g)||[]);
 for(const key of parts)if(/[\u0600-\u06ff]/.test(key)&&!dict[key.trim()])missing.add(key.trim());
 }}for(const v of Object.values(n)){if(Array.isArray(v))v.forEach(scan);else if(v&&typeof v==='object')scan(v)}}
for(const file of ['app.js','brand.js'])scan(acorn.parse(fs.readFileSync('wasla/static/'+file,'utf8'),{ecmaVersion:'latest'}));
if(missing.size){console.error([...missing]);process.exit(1)}console.log('UI dictionary coverage passed');
