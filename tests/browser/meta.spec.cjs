const {test,expect}=require('@playwright/test');
async function login(page){
 await page.goto('/login?lang=en');await page.locator('[name=email]').fill('meta@example.test');await page.locator('[name=password]').fill('service-test-password');await page.locator('button[type=submit]').click();await page.waitForURL('**/app');
}
test('Meta connection remains visibly disabled without configuration',async({page})=>{
 await login(page);await page.goto('/app/channels?lang=en');
 await expect(page.locator('#meta-prepare')).toBeDisabled();await expect(page.locator('#meta-status')).toContainText('configure Meta');
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBeTruthy();
});
test('Synthetic SDK: rejects foreign origins and submits only after code plus verified-origin event',async({page})=>{
 await login(page);let submissions=[];
 await page.route('**/api/v2/meta/readiness',r=>r.fulfill({json:{signup_available:true,connections:[]}}));
 await page.route('**/api/v2/meta/whatsapp/start',r=>r.fulfill({json:{state:'s'.repeat(43),app_id:'123',config_id:'456',api_version:'v26.0',extras:{version:'v5',features:[{name:'whatsapp_business_app_onboarding',enabled:false}]}}}));
 await page.route('https://connect.facebook.net/en_US/sdk.js',r=>r.fulfill({contentType:'text/javascript',body:'window.FB={init(){},login(callback,options){window.testMetaOptions=options;callback({authResponse:{code:"test-code"}})}};window.fbAsyncInit();'}));
 await page.route('**/api/v2/meta/whatsapp/complete',r=>{submissions.push(r.request().postDataJSON());return r.fulfill({json:{account_id:'test',authorization:'verified'}})});
 await page.goto('/app/channels?lang=en');await page.locator('#meta-prepare').click();await expect(page.locator('#meta-launch')).toBeVisible();await page.locator('#meta-launch').click();
 const event={type:'WA_EMBEDDED_SIGNUP',event:'FINISH',data:{is_on_biz_app:false,messaging_account_ids:['900'],phone_number_id:'901'}};
 await page.evaluate(data=>window.dispatchEvent(new MessageEvent('message',{origin:'https://evilfacebook.com',data:JSON.stringify(data)})),event);
 expect(submissions).toEqual([]);
 await page.evaluate(data=>window.dispatchEvent(new MessageEvent('message',{origin:'https://www.facebook.com',data:JSON.stringify(data)})),event);
 await expect.poll(()=>submissions.length).toBe(1);expect(submissions[0]).toEqual({state:'s'.repeat(43),code:'test-code',business_account_id:'900',phone_number_id:'901'});
 expect(await page.evaluate(()=>window.testMetaOptions.extras.version)).toBe('v5');
 expect(await page.evaluate(()=>localStorage.getItem('test-code'))).toBeNull();
});
