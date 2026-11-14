const {test,expect}=require('@playwright/test');
for(const lang of ['ar','en']){
 test(`V2 ${lang}: ticket, note, conflict, channels and reports`,async({page})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/login?lang='+lang);await page.locator('[name=email]').fill('service@example.test');await page.locator('[name=password]').fill('service-test-password');await page.locator('button[type=submit]').click();await page.waitForURL('**/app');
  await page.goto('/app/inbox?lang='+lang);await expect(page.locator('.service-ticket').first()).toBeVisible();await page.locator('.service-ticket').first().click();
  await expect(page.locator('#service-composer')).toBeVisible();
  const note='Internal QA '+Date.now();await page.locator('#service-composer [name=body]').fill(note);await page.locator('#service-composer button[type=submit]').click();await expect(page.locator('.service-messages')).toContainText(note);
  await expect(page.locator('.service-messages .note').last()).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBeTruthy();
  await page.locator('#service-composer [name=direction]').selectOption('out');await page.locator('#service-composer [name=body]').fill('Must not send');await page.locator('#service-composer button[type=submit]').click();await expect(page.locator('#service-composer .error')).toBeVisible();
  await page.goto('/app/channels?lang='+lang);await expect(page.locator('.service-accounts .card')).toHaveCount(4);await expect(page.locator('#main')).toContainText('QA WhatsApp');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBeTruthy();
  await page.goto('/app/reports?lang='+lang);await expect(page.locator('.service-kpis')).toBeVisible();await expect(page.locator('#main')).toContainText('CSAT');
  expect(errors).toEqual([]);
 });
}
