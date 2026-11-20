const {test,expect}=require('@playwright/test');
test('real signup, email verification, plan, integration gates and workspace erasure',async({page,playwright,baseURL})=>{
 const admin=await playwright.request.newContext({baseURL,extraHTTPHeaders:{Origin:baseURL}});
 expect((await admin.post('/api/auth/login',{data:{email:'commerce@example.test',password:'ui-only-long-password'}})).ok()).toBeTruthy();
 const csrf=(await (await admin.get('/api/me')).json()).csrf;const headers={'X-CSRF-Token':csrf};
 const plan=await (await admin.post('/api/admin/commerce/plans',{headers,data:{name:'Journey Plan',currency:'SAR',monthly_amount_minor:0,message_limit:10,limits:{seats:1,channels:1,meta_accounts:1,api_requests:20}}})).json();
 expect((await admin.put('/api/admin/integrations/configuration',{headers,data:{values:{WASLA_SELF_SIGNUP_MODE:'test'}}})).ok()).toBeTruthy();
 const email='journey-'+test.info().project.name+'@example.test';
 try{
  await page.goto('/signup?lang=en');await page.locator('[name=company]').fill('Journey Shop');await page.locator('[name=name]').fill('Journey Owner');await page.locator('[name=email]').fill(email);await page.locator('[name=password]').fill('journey-password-long');await page.locator('[name=plan_id]').selectOption(plan.id);await page.locator('[name=terms]').check();await page.locator('button[type=submit]').click();await expect(page.locator('#signup-status')).toContainText('verification email');
  const mail=await (await admin.post('/api/__test__/verification',{headers,data:{email}})).json();await page.goto(mail.url);await expect(page.locator('#verify-form button')).toBeEnabled();await page.locator('#verify-form button').click();await expect(page.locator('#verify-status')).toContainText('Email verified');expect(new URL(page.url()).hash).toBe('');
  await page.goto('/login?lang=en');await page.locator('[name=email]').fill(email);await page.locator('[name=password]').fill('journey-password-long');await page.locator('button[type=submit]').click();await page.waitForURL('**/app');
  await page.goto('/app/billing?lang=en');await expect(page.locator('#main')).toContainText('Journey Plan');await expect(page.locator('#main')).toContainText('Seats');await page.locator('#draft-invoice').click();await expect(page.locator('#main')).toContainText('Sandbox amount');
  await page.goto('/app/channels?lang=en');await expect(page.locator('#meta-prepare')).toBeDisabled();
  await page.goto('/app/metadata?lang=en');await expect(page.locator('h1')).toHaveText('Meta business data');await expect(page.locator('[data-connect-kind=catalogs]')).toBeDisabled();
  await page.goto('/app/reports?lang=en');await expect(page.locator('#main')).toContainText('No connected Messenger Pages');
  await page.goto('/app/privacy?lang=ar');await expect(page.locator('html')).toHaveAttribute('dir','rtl');expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBeTruthy();
  await page.screenshot({path:test.info().outputPath('privacy-rtl.png'),fullPage:true});
  await page.goto('/app/privacy?lang=en');await page.locator('[name=scope]').selectOption('tenant');await page.locator('[name=password]').fill('journey-password-long');await page.locator('[name=confirm]').fill('DELETE');await page.locator('#deletion-form button').click();await page.waitForURL('**/deletion-status?request=*');
  expect((await admin.post('/api/__test__/drain-deletion',{headers,data:{}})).ok()).toBeTruthy();await page.locator('#status-refresh').click();await expect(page.locator('#deletion-status')).toContainText('completed');
  expect((await page.request.get('/api/me')).status()).toBe(401);
 }finally{await admin.put('/api/admin/integrations/configuration',{headers,data:{values:{WASLA_SELF_SIGNUP_MODE:'disabled'}}});await admin.dispose()}
});
