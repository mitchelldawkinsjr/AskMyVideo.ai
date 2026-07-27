#!/usr/bin/env node
/**
 * WCAG 2.1 AA + WCAG 2.2 accessibility audit via axe-core.
 * Ponytail (DietrichGebert/ponytail) is a coding-style ruleset, not an a11y tool;
 * this script fulfills the same audit intent using @axe-core/playwright.
 */
import { chromium } from 'playwright';
import AxeBuilder from '@axe-core/playwright';
import fs from 'fs';
import path from 'path';

const BASE = process.env.A11Y_BASE_URL || 'http://127.0.0.1:8000';
const OUT_DIR = path.dirname(new URL(import.meta.url).pathname);

const PAGES = [
  { name: 'home', url: `${BASE}/` },
  { name: 'library_redirect', url: `${BASE}/library/` },
  { name: 'login', url: `${BASE}/accounts/login/` },
  { name: 'register', url: `${BASE}/accounts/register/` },
];

const AXE_TAGS = [
  'wcag2a',
  'wcag2aa',
  'wcag21a',
  'wcag21aa',
  'wcag22aa',
  'best-practice',
];

function severityWeight(impact) {
  const map = { critical: 4, serious: 3, moderate: 2, minor: 1 };
  return map[impact] || 0;
}

function mapSeverity(impact) {
  if (impact === 'critical') return 'critical';
  if (impact === 'serious') return 'high';
  if (impact === 'moderate') return 'medium';
  return 'low';
}

async function auditPage(browser, pageDef) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(pageDef.url, { waitUntil: 'networkidle', timeout: 30000 });

  const results = await new AxeBuilder({ page })
    .withTags(AXE_TAGS)
    .analyze();

  await context.close();

  const jsonPath = path.join(OUT_DIR, `report-${pageDef.name}.json`);
  fs.writeFileSync(jsonPath, JSON.stringify(results, null, 2));

  const lines = [
    `# Accessibility Report: ${pageDef.name}`,
    `URL: ${pageDef.url}`,
    `Generated: ${new Date().toISOString()}`,
    `Violations: ${results.violations.length}`,
    `Incomplete: ${results.incomplete.length}`,
    '',
  ];

  for (const v of results.violations) {
    lines.push(`## ${v.id} [${mapSeverity(v.impact)}] — ${v.help}`);
    lines.push(`Impact: ${v.impact} | Tags: ${v.tags.join(', ')}`);
    lines.push(`Help: ${v.helpUrl}`);
    lines.push(`Occurrences: ${v.nodes.length}`);
    for (const node of v.nodes.slice(0, 5)) {
      lines.push(`- \`${node.target.join(' > ')}\``);
      lines.push(`  ${node.failureSummary?.replace(/\n/g, ' ') || node.html.slice(0, 120)}`);
    }
    if (v.nodes.length > 5) lines.push(`- ... and ${v.nodes.length - 5} more`);
    lines.push('');
  }

  const mdPath = path.join(OUT_DIR, `report-${pageDef.name}.md`);
  fs.writeFileSync(mdPath, lines.join('\n'));

  return { page: pageDef, results, jsonPath, mdPath };
}

function aggregateReports(reports, label) {
  const summary = {};
  for (const { page, results } of reports) {
    for (const v of results.violations) {
      const key = v.id;
      if (!summary[key]) {
        summary[key] = {
          id: v.id,
          type: v.id,
          severity: mapSeverity(v.impact),
          impact: v.impact,
          description: v.description,
          help: v.help,
          helpUrl: v.helpUrl,
          tags: v.tags,
          pages: [],
          occurrences: 0,
        };
      }
      summary[key].pages.push(page.name);
      summary[key].occurrences += v.nodes.length;
    }
  }

  const sorted = Object.values(summary).sort(
    (a, b) => severityWeight(b.impact) - severityWeight(a.impact) || b.occurrences - a.occurrences
  );

  const aggregate = {
    label,
    generatedAt: new Date().toISOString(),
    baseUrl: BASE,
    tool: '@axe-core/playwright',
    tags: AXE_TAGS,
    totalPages: reports.length,
    totalViolations: reports.reduce((n, r) => n + r.results.violations.length, 0),
    totalNodeViolations: sorted.reduce((n, s) => n + s.occurrences, 0),
    issues: sorted,
    pages: reports.map((r) => ({
      name: r.page.name,
      url: r.page.url,
      violationCount: r.results.violations.length,
      reportJson: r.jsonPath,
      reportMd: r.mdPath,
    })),
  };

  const aggPath = path.join(OUT_DIR, `summary-${label}.json`);
  fs.writeFileSync(aggPath, JSON.stringify(aggregate, null, 2));
  return aggregate;
}

async function main() {
  const label = process.argv.includes('--after') ? 'after' : 'before';
  const browser = await chromium.launch({ headless: true });

  const reports = [];
  for (const pageDef of PAGES) {
    console.log(`Auditing ${pageDef.url}...`);
    try {
      reports.push(await auditPage(browser, pageDef));
    } catch (err) {
      console.error(`Failed ${pageDef.url}:`, err.message);
    }
  }

  await browser.close();

  const aggregate = aggregateReports(reports, label);
  console.log(JSON.stringify({
    label,
    totalViolations: aggregate.totalViolations,
    uniqueIssues: aggregate.issues.length,
    issues: aggregate.issues.map((i) => ({
      id: i.id,
      severity: i.severity,
      occurrences: i.occurrences,
      pages: i.pages,
    })),
  }, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
