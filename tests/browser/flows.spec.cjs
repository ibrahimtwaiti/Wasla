const {test,expect}=require('@playwright/test');
for(const lang of ['ar','en']){
 test(`${lang}: public pages, mobile navigation, language and request`,async({page})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/?lang='+lang);await expect(page.locator('.brand-hero')).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('dir',lang==='ar'?'rtl':'ltr');
  for(const route of ['/','/services','/services/crm','/platform','/how-it-works','/about','/faq','/privacy','/terms','/login','/request']){
   await page.goto(route+'?lang='+lang);await expect(page.locator('main h1')).toBeVisible();
   await expect(page.locator('main h1')).not.toContainText(/تعذر تحميل|Unable to load/);
   expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),route).toBeTruthy();
   if(lang==='en')expect(await page.locator('main').innerText(),route).not.toMatch(/[\u0600-\u06ff]/);
  }
  await page.goto('/?lang='+lang);
  if(await page.locator('#menu-toggle').isVisible()){await page.locator('#menu-toggle').click();await expect(page.locator('#menu-toggle')).toHaveAttribute('aria-expanded','true');await page.keyboard.press('Escape');await expect(page.locator('#menu-toggle')).toHaveAttribute('aria-expanded','false')}
  await page.locator('details').first().locator('summary').click();await expect(page.locator('details').first()).toHaveAttribute('open','');
  await page.goto('/request?lang='+lang);
  for(const [name,value] of Object.entries({name:'Browser QA',company:'Isolated test',phone:'+966500000001',country:'Saudi Arabia',activity:'Testing',need:'Browser request flow'}))await page.locator(`[name=${name}]`).fill(value);
  await page.locator('[name=contact_consent]').check();if(lang==='en'){await page.locator('button[type=submit]').click();await expect(page.locator('.success')).toContainText('WS-');}
  expect(errors).toEqual([]);
 });
}
test('provider identity persists and protected UI is usable',async({page})=>{
 await page.goto('/login?lang=en');await page.locator('[name=email]').fill('ui@example.test');await page.locator('[name=password]').fill('ui-only-long-password');await page.locator('button[type=submit]').click();await page.waitForURL('**/app');
 await page.goto('/app/content?lang=en');await expect(page.locator('#brand-form')).toBeVisible();
 await page.locator('#brand-form [name=primary]').fill('#7650cc');await page.locator('#brand-form button[type=submit]').click();await expect(page.locator('#toast')).toBeVisible();
 await page.reload();await expect(page.locator('#brand-form [name=primary]')).toHaveValue('#7650cc');
 await page.locator('#brand-form [name=primary]').fill('#834DFB');await page.locator('#brand-form button[type=submit]').click();await expect(page.locator('#toast')).toBeVisible();
});
test('reduced motion leaves all sections visible',async({page})=>{
 await page.emulateMedia({reducedMotion:'reduce'});await page.goto('/');await expect(page.locator('.brand-hero')).toBeVisible();expect(await page.locator('.reveal-ready').count()).toBe(0);
});
