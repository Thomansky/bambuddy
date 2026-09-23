/**
 * The `resolve.alias` entries that make the .msg preview work in a build.
 *
 * They are the only aliases in the app that stand in for a Node built-in, and
 * they have to match the bare specifier and nothing else: Vite matches a
 * string alias by prefix and substitutes by substring, so a string `buffer`
 * entry silently turns `buffer/index.js` into `<polyfill>/index.js` and kills
 * the build with an unloadable path. Nothing in the app imports those subpaths
 * today; readable-stream@2 (a jszip dependency) requires `string_decoder/`
 * literally, and it is kept out of the browser graph only by jszip's own
 * `browser` field. This asserts the matcher, not the file contents, because
 * that is the part a well-meaning simplification back to `{ buffer: '...' }`
 * would quietly undo.
 */

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { resolveConfig } from 'vite';

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

/** Vite's own alias matcher (@rollup/plugin-alias `matches`). */
function matches(find: string | RegExp, importee: string): boolean {
  if (find instanceof RegExp) return find.test(importee);
  if (importee.length < find.length) return false;
  if (importee === find) return true;
  return importee.startsWith(`${find}/`);
}

describe('vite.config.ts node built-in aliases', () => {
  it('matches the bare built-in names and leaves their subpaths alone', async () => {
    const config = await resolveConfig(
      { configFile: path.join(frontendRoot, 'vite.config.ts'), root: frontendRoot },
      'build',
      'production',
      'production',
    );
    const alias = config.resolve.alias as { find: string | RegExp; replacement: string }[];
    const find = (importee: string) => alias.find((entry) => matches(entry.find, importee));

    expect(find('buffer')?.replacement).toMatch(/[/\\]buffer[/\\]/);
    expect(find('string_decoder')?.replacement).toMatch(/[/\\]string_decoder[/\\]/);
    expect(find('stream')?.replacement).toMatch(/node-stream-empty\.ts$/);

    for (const subpath of ['buffer/', 'buffer/index.js', 'string_decoder/', 'stream/web', 'stream/promises']) {
      expect(find(subpath), `${subpath} must resolve normally, not through the built-in alias`).toBeUndefined();
    }

    // The `@` alias is deliberately a prefix match and must stay one.
    expect(find('@/utils/msgFile')?.replacement).toMatch(/[/\\]src$/);
  }, 60_000);
});
