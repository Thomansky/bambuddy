/**
 * Unit tests for parseMsgFile.
 *
 * The interop case is the one that matters: @kenjiuno/msgreader is CommonJS
 * with an `__esModule` marker, so vitest hands back the class while the
 * production bundler hands back `module.exports` with the class under its own
 * `default`. Reading `.default` alone therefore works in every test and in no
 * built bundle — the mock below is that production shape.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildMsgFixture, buildRtfOnlyMsgFixture, MSG_FIXTURE } from '../mocks/msgFixture';

function toBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

function fixtureBuffer(): ArrayBuffer {
  return toBuffer(buildMsgFixture());
}

async function importParser() {
  return (await import('../../utils/msgFile')).parseMsgFile;
}

afterEach(() => {
  vi.resetModules();
  vi.doUnmock('@kenjiuno/msgreader');
});

describe('parseMsgFile', () => {
  it('reads subject, sender, recipient, body and attachments', async () => {
    const parseMsgFile = await importParser();
    const parsed = await parseMsgFile(fixtureBuffer());

    expect(parsed.fields.subject).toBe(MSG_FIXTURE.subject);
    expect(parsed.fields.senderEmail).toBe(MSG_FIXTURE.senderEmail);
    expect(parsed.bodyText).toContain('your filament order has shipped');
    expect(parsed.bodyFromRtf).toBe(false);
    expect(parsed.fields.attachments?.[0]?.fileName).toBe(MSG_FIXTURE.attachmentName);
  });

  it('recovers an HTML-only body from compressed RTF without leaking RTF markers', async () => {
    const parseMsgFile = await importParser();
    const parsed = await parseMsgFile(toBuffer(buildRtfOnlyMsgFixture()));

    expect(parsed.bodyFromRtf).toBe(true);
    expect(parsed.bodyText).toContain('your filament order has shipped');
    // Every encapsulated-HTML destination opens with the \* control symbol,
    // which no control-word strip can reach — the body used to be dozens of
    // literal "\*" lines with the text buried among them.
    expect(parsed.bodyText).not.toContain(String.raw`\*`);
    expect(parsed.bodyText).not.toContain('htmltag');
    expect(parsed.bodyText).not.toContain('<p>');
  });

  it('rejects a file that is not an Outlook message', async () => {
    const parseMsgFile = await importParser();
    const notAMessage = new TextEncoder().encode('this is not an outlook message');

    await expect(parseMsgFile(notAMessage.buffer as ArrayBuffer)).rejects.toThrow();
  });

  it('still parses when the default export is module.exports, as in a built bundle', async () => {
    const actual = await vi.importActual<typeof import('@kenjiuno/msgreader')>('@kenjiuno/msgreader');
    vi.doMock('@kenjiuno/msgreader', () => ({
      default: { __esModule: true, default: actual.default },
    }));
    vi.resetModules();

    const parseMsgFile = await importParser();
    const parsed = await parseMsgFile(fixtureBuffer());

    expect(parsed.fields.subject).toBe(MSG_FIXTURE.subject);
  });
});
