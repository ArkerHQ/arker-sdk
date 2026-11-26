// Slash trimming must match the regexes it replaced and stay linear on long
// runs of `/` (the polynomial-ReDoS shape of `/\/+$/`).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { trimLeadingSlashes, trimSlashes, trimTrailingSlashes } from '../src/internal/slashes';
import { Arker } from '../src/index';

const cases = ['', '/', '///', 'a', '/a', 'a/', '//a//', 'a/b/', '/a/b', 'a//b', ' /a/ ', 'https://x.test/', 'https://x.test///', '/é/'];

test('matches the regexes it replaced', () => {
  for (const c of cases) {
    assert.equal(trimTrailingSlashes(c), c.replace(/\/+$/, ''), JSON.stringify(c));
    assert.equal(trimLeadingSlashes(c), c.replace(/^\/+/, ''), JSON.stringify(c));
    assert.equal(trimSlashes(c), c.replace(/^\/+/, '').replace(/\/+$/, ''), JSON.stringify(c));
  }
});

test('stays linear on a long run of slashes', () => {
  const evil = '/'.repeat(200_000) + 'x';
  const start = performance.now();
  assert.equal(trimTrailingSlashes(evil), evil);
  assert.equal(trimSlashes(evil), 'x');
  assert.equal(trimTrailingSlashes('a' + '/'.repeat(200_000)), 'a');
  assert.ok(performance.now() - start < 250, 'slash trimming took too long');
});

test('baseUrl normalization still strips trailing slashes', () => {
  const client = new Arker({ apiKey: 'k', baseUrl: '  https://api.example.test///  ' }) as unknown as { computeBaseUrl: string };
  assert.equal(client.computeBaseUrl, 'https://api.example.test');
  assert.throws(() => new Arker({ apiKey: 'k', baseUrl: ' /// ' }), /baseUrl must not be empty/);
});
