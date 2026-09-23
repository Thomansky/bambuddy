/**
 * Parsing half of the .msg preview.
 *
 * It lives outside the component so a test can build it with the real Vite
 * config and run the *bundled* output
 * (src/__tests__/utils/msgFileBundle.test.ts): @kenjiuno/msgreader and its
 * iconv-lite dependency are CommonJS written for Node, and they only
 * misbehave once a bundler has replaced the Node built-ins and rewritten the
 * CommonJS exports — which is why the jsdom tests stayed green while no .msg
 * preview ever worked in a built bundle.
 */

import { resolveInteropDefault } from './interopDefault';
import { rtfToText } from './rtfToText';

// Structural types for the parts of @kenjiuno/msgreader we consume — the
// library is loaded dynamically, so its own types never enter the bundle.
export interface MsgRecipient {
  name?: string;
  email?: string;
  smtpAddress?: string;
  recipType?: string;
}

export interface MsgAttachment {
  fileName?: string;
  contentLength?: number;
}

export interface MsgFields {
  subject?: string;
  senderName?: string;
  senderEmail?: string;
  body?: string;
  compressedRtf?: Uint8Array;
  recipients?: MsgRecipient[];
  attachments?: MsgAttachment[];
  messageDeliveryTime?: string;
  clientSubmitTime?: string;
  creationTime?: string;
  /** Set by msgreader instead of throwing when the file is not a message. */
  error?: string;
}

export interface MsgReaderLike {
  getFileData(): MsgFields;
  getAttachment(att: MsgAttachment): { fileName: string; content: Uint8Array };
}

type MsgReaderCtor = new (buffer: ArrayBuffer) => MsgReaderLike;

export interface ParsedMsg {
  fields: MsgFields;
  bodyText: string;
  // The body was recovered from compressed RTF, not stored as plain text —
  // shown as a note because the conversion drops formatting.
  bodyFromRtf: boolean;
  reader: MsgReaderLike;
}

/** Parse a .msg file, or throw when it holds no readable message. */
export async function parseMsgFile(buffer: ArrayBuffer): Promise<ParsedMsg> {
  // msgreader is loaded on demand so it stays out of the main bundle. It is
  // CommonJS carrying an `__esModule` marker, so its default export arrives
  // either as the class (vitest, which resolves it through Node) or as
  // `module.exports` with the class under `default` (bundled builds) — the
  // #2616 shape, unwrapped the same way. Taking `.default` alone leaves
  // `new MsgReader()` with an object: not a constructor.
  const MsgReader = resolveInteropDefault<MsgReaderCtor>((await import('@kenjiuno/msgreader')).default);
  const reader = new MsgReader(buffer);
  const fields = reader.getFileData();
  // msgreader reports unreadable input via `error` (or a fully empty
  // result) rather than throwing — surface both as the preview error.
  const isEmpty = !fields.subject && !fields.body && !fields.compressedRtf
    && !fields.senderName && !(fields.recipients?.length) && !(fields.attachments?.length);
  if (fields.error || isEmpty) throw new Error(fields.error || 'empty message');

  let bodyText = fields.body ?? '';
  let bodyFromRtf = false;
  if (!bodyText && fields.compressedRtf) {
    try {
      const { decompressRTF } = await import('@kenjiuno/decompressrtf');
      const rtfBytes = decompressRTF(Array.from(fields.compressedRtf));
      bodyText = rtfToText(new TextDecoder('latin1').decode(Uint8Array.from(rtfBytes)));
      bodyFromRtf = bodyText.length > 0;
    } catch {
      // Fall through to the no-body notice; headers still render.
    }
  }

  return { fields, bodyText, bodyFromRtf, reader };
}
