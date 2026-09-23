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
