/**
 * A real .msg fixture: a CFB container built with the `cfb` package using the
 * MAPI stream names Outlook writes, so tests exercise msgreader's actual
 * parse path instead of a stubbed reader. Shared by the component test and
 * the bundled-parser test.
 */

import * as CFB from 'cfb';

export const MSG_FIXTURE = {
  subject: 'Order confirmation #4711',
  body: 'Hello,\n\nyour filament order has shipped.',
  senderName: 'Example Supplier',
  senderEmail: 'orders@example-supplier.test',
  recipientName: 'Thomas',
  recipientEmail: 'thomas@example.test',
  attachmentName: 'invoice-4711.pdf',
} as const;

/**
 * An HTML-only body the way Outlook stores one: RTF-encapsulated HTML, every
 * tag wrapped in a `{\*\htmltagNNN ...}` ignorable destination. Mail with no
 * PR_BODY stream is the only path that reaches rtfToText.
 */
export const MSG_RTF_BODY = String.raw`{\rtf1\ansi\ansicpg1252\fromhtml1 \fbidis \deff0{\fonttbl{\f0\fswiss Arial;}}
{\*\htmltag19 <html>}\htmlrtf {\htmlrtf0 {\*\htmltag34 <body>}\htmlrtf {\htmlrtf0
{\*\htmltag84 <p>}\htmlrtf {\htmlrtf0 Hello, your filament order has shipped.{\*\htmltag92 </p>}\htmlrtf\par}\htmlrtf0
{\*\htmltag41 </body>}{\*\htmltag27 </html>}}`;

function utf16(str: string): Uint8Array {
  const bytes = new Uint8Array(str.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < str.length; i++) view.setUint16(i * 2, str.charCodeAt(i), true);
  return bytes;
}

export function buildMsgFixture(): Uint8Array {
  const container = CFB.utils.cfb_new();
  const add = (path: string, content: Uint8Array) => CFB.utils.cfb_add(container, path, content);
  add('/__substg1.0_0037001F', utf16(MSG_FIXTURE.subject));
  add('/__substg1.0_1000001F', utf16(MSG_FIXTURE.body));
  add('/__substg1.0_0C1A001F', utf16(MSG_FIXTURE.senderName));
  add('/__substg1.0_0C1F001F', utf16(MSG_FIXTURE.senderEmail));
  add('/__recip_version1.0_#00000000/__substg1.0_3001001F', utf16(MSG_FIXTURE.recipientName));
  add('/__recip_version1.0_#00000000/__substg1.0_39FE001F', utf16(MSG_FIXTURE.recipientEmail));
  add('/__attach_version1.0_#00000000/__substg1.0_3707001F', utf16(MSG_FIXTURE.attachmentName));
  add('/__attach_version1.0_#00000000/__substg1.0_37010102', new TextEncoder().encode('%PDF-1.4 fake'));
  return new Uint8Array(CFB.write(container, { type: 'buffer' }) as Uint8Array);
}

/** PR_RTF_COMPRESSED, written in the format's stored-uncompressed variant. */
function rtfCompressedStream(rtf: string): Uint8Array {
  const raw = new Uint8Array(rtf.length);
  for (let i = 0; i < rtf.length; i++) raw[i] = rtf.charCodeAt(i) & 0xff;
  const stream = new Uint8Array(16 + raw.length);
  const header = new DataView(stream.buffer);
  header.setUint32(0, 12 + raw.length, true); // COMPSIZE: everything after it
  header.setUint32(4, raw.length, true); // RAWSIZE
  header.setUint32(8, 0x414c454d, true); // COMPTYPE "MELA" — stored as-is
  header.setUint32(12, 0, true); // CRC, unused for the uncompressed variant
  stream.set(raw, 16);
  return stream;
}

/**
 * A .msg with no plain-text body stream, so the body has to come out of
 * PR_RTF_COMPRESSED — the fallback path the plain-text fixture never reaches.
 */
export function buildRtfOnlyMsgFixture(): Uint8Array {
  const container = CFB.utils.cfb_new();
  const add = (path: string, content: Uint8Array) => CFB.utils.cfb_add(container, path, content);
  add('/__substg1.0_0037001F', utf16(MSG_FIXTURE.subject));
  add('/__substg1.0_0C1A001F', utf16(MSG_FIXTURE.senderName));
  add('/__substg1.0_10090102', rtfCompressedStream(MSG_RTF_BODY));
  return new Uint8Array(CFB.write(container, { type: 'buffer' }) as Uint8Array);
}
