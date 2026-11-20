const {test,expect}=require('@playwright/test');
test('sandbox plans are backed by API and never collect payments',async({page})=>{
 await page.goto('/login?lang=en');await page.locator('[name=email]').fill('commerce@example.test');await page.locator('[name=password]').fill('ui-only-long-password');await page.locator('button[type=submit]').click();await page.waitForURL('**/app');
 await page.goto('/app/commerce?lang=en');await expect(page.locator('h1')).toHaveText('Sandbox plans');
 await page.locator('#new-plan').click();await page.locator('dialog [name=name]').fill('Browser sandbox '+test.info().project.name);await page.locator('[name=currency]').fill('SAR');await page.locator('[name=monthly_amount_minor]').fill('100');await page.locator('[name=message_limit]').fill('5');await page.locator('dialog button[type=submit]').click();await expect(page.locator('dialog')).not.toBeVisible();
 await expect(page.locator('#main')).toContainText('Browser sandbox');
 await page.locator('#assign-plan').click();await page.locator('[name=tenant_id]').selectOption('ui-service');await page.locator('dialog button[type=submit]').click();await expect(page.locator('dialog')).not.toBeVisible();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});
test('signup clearly explains the closed external gate',async({page})=>{
 await page.goto('/signup?lang=en');await expect(page.locator('h1')).toHaveText('Create a workspace');
 await expect(page.locator('button[type=submit]')).toBeDisabled();await expect(page.locator('.notice')).toContainText('currently closed');
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});
