/**
 * The .msg parser, exercised through a real production bundle.
 *
 * Every other test resolves @kenjiuno/msgreader and iconv-lite through Node,
 * where `require('buffer')` and `require('string_decoder')` are the real
 * built-ins and a CommonJS default export arrives unwrapped. A browser build
 * gets neither: Vite substitutes an empty module for a built-in it cannot map,
 * and the libraries read `Buffer` / `StringDecoder` at module scope, so the
 * whole preview died on `undefined.prototype` while the suite stayed green.
 * This builds src/utils/msgFile.ts with the app's own Vite config, in the mode
 * `npm run build` uses, and runs the emitted chunk — the only place that class
 * of failure is visible.
 */

import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { describe, expect, it } from 'vitest';
import { build } from 'vite';
import { buildMsgFixture, MSG_FIXTURE } from '../mocks/msgFixture';

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

async function bundleParser(outDir: string): Promise<typeof import('../../utils/msgFile')> {
  await build({
    configFile: path.join(frontendRoot, 'vite.config.ts'),
    root: frontendRoot,
    mode: 'production',
    logLevel: 'silent',
    build: {
      outDir,
      emptyOutDir: true,
      copyPublicDir: false,
      minify: false,
      rollupOptions: {
        input: path.join(frontendRoot, 'src/utils/msgFile.ts'),
        preserveEntrySignatures: 'exports-only',
        output: { entryFileNames: 'msgFile.mjs', chunkFileNames: '[name].mjs' },
      },
    },
  });
  const entry = pathToFileURL(path.join(outDir, 'msgFile.mjs')).href;
  return (await import(/* @vite-ignore */ entry)) as typeof import('../../utils/msgFile');
}

describe('bundled .msg parser', () => {
  it('parses a .msg from the emitted browser chunk', async () => {
    // Under node_modules so vitest imports the emitted file as-is instead of
    // putting it back through its own transform.
    const scratch = path.join(frontendRoot, 'node_modules/.tmp');
    mkdirSync(scratch, { recursive: true });
    const outDir = mkdtempSync(path.join(scratch, 'msg-bundle-'));
    try {
      const { parseMsgFile } = await bundleParser(outDir);
      const bytes = buildMsgFixture();
      const parsed = await parseMsgFile(
        bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer,
      );

      expect(parsed.fields.subject).toBe(MSG_FIXTURE.subject);
      expect(parsed.fields.senderName).toBe(MSG_FIXTURE.senderName);
      expect(parsed.bodyText).toContain('your filament order has shipped');
      expect(parsed.fields.attachments?.[0]?.fileName).toBe(MSG_FIXTURE.attachmentName);
      expect(parsed.reader.getAttachment(parsed.fields.attachments![0]).content.length).toBeGreaterThan(0);

      // Vite's stand-in for a built-in it cannot map throws on property
      // access outside a production NODE_ENV and is a silently empty object
      // inside one, so which of the two failures a run sees depends on the
      // environment. Assert the structural invariant instead: no built-in
      // this chunk touches may be left as that stand-in.
      const emitted = readdirSync(outDir)
        .filter((name) => name.endsWith('.mjs'))
        .map((name) => readFileSync(path.join(outDir, name), 'utf8'))
        .join('\n');
      expect(emitted).not.toMatch(/vite[-_]browser[-_]external/);
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  }, 180_000);
});
