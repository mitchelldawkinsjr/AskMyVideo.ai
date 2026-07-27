import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const BASE = process.env.A11Y_BASE_URL || 'http://127.0.0.1:8000';

const PAGES = [
  { name: 'home', url: `${BASE}/` },
  { name: 'library_redirect', url: `${BASE}/library/` },
  { name: 'login', url: `${BASE}/accounts/login/` },
  { name: 'register', url: `${BASE}/accounts/register/` },
];

const AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];

for (const pageDef of PAGES) {
  test(`${pageDef.name} has no WCAG violations`, async ({ page }) => {
    await page.goto(pageDef.url, { waitUntil: 'networkidle' });
    const results = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();
    expect(results.violations, formatViolations(results.violations)).toEqual([]);
  });
}

function formatViolations(violations) {
  return violations
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)
    .join('\n');
}
