import assert from 'node:assert/strict';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const peopleRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../frontend/public/assets/realistic/people');
for (const avatar of ['rocketbox_female_adult_01', 'rocketbox_female_adult_03', 'rocketbox_male_adult_01', 'rocketbox_business_female_01', 'rocketbox_business_male_01']) {
  const root = resolve(peopleRoot, avatar);
  const manifest = JSON.parse(readFileSync(resolve(root, 'manifest.json'), 'utf8'));
  assert.equal(manifest.source.license, 'MIT');
  assert.equal(manifest.source.commit, '0943055db6ec570bcef9f2c8b41c9e5467c808f9');
  assert.match(readFileSync(resolve(root, 'LICENSE-Rocketbox-MIT.md'), 'utf8'), /MIT License/);
  for (const [file, expected] of Object.entries(manifest.sha256)) {
    const path = resolve(root, file);
    assert.ok(existsSync(path), `missing Rocketbox asset (${avatar}): ${file}`);
    assert.ok(statSync(path).size > 0, `empty Rocketbox asset (${avatar}): ${file}`);
    const digest = createHash('sha256').update(readFileSync(path)).digest('hex');
    assert.equal(digest, expected, `hash mismatch (${avatar}): ${file}`);
  }
}

console.log('rocketbox asset provenance: passed');
