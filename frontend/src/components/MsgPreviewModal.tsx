import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Download, Loader2, Mail, Paperclip, X } from 'lucide-react';
import { api, getAuthToken } from '../api/client';
import { formatFileSize } from '../utils/file';
import { rtfToText } from '../utils/rtfToText';

// An .msg with attachments is parsed fully in memory — anything over this
// size shows a notice instead of stalling the tab (same cap as PDF).
export const MSG_PREVIEW_MAX_BYTES = 50 * 1024 * 1024;

// Structural types for the parts of @kenjiuno/msgreader we consume — the
// library is loaded dynamically, so its own types never enter the bundle.
interface MsgRecipient {
  name?: string;
  email?: string;
  smtpAddress?: string;
  recipType?: string;
}

interface MsgAttachment {
  fileName?: string;
  contentLength?: number;
}

interface MsgFields {
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

interface MsgReaderLike {
  getFileData(): MsgFields;
  getAttachment(att: MsgAttachment): { fileName: string; content: Uint8Array };
}

interface ParsedMsg {
  fields: MsgFields;
  bodyText: string;
  // The body was recovered from compressed RTF, not stored as plain text —
  // shown as a note because the conversion drops formatting.
  bodyFromRtf: boolean;
  reader: MsgReaderLike;
}

interface MsgPreviewModalProps {
  libraryFileId: number;
  filename: string;
  fileSize: number;
  onClose: () => void;
  /** Called once with a 256px PNG rendered from the headers, for the grid thumbnail. */
  onSnapshot?: (blob: Blob) => void;
}

function recipientLabel(r: MsgRecipient): string {
  const address = r.smtpAddress || r.email;
  if (r.name && address && r.name !== address) return `${r.name} <${address}>`;
  return r.name || address || '';
}

function formatMsgDate(fields: MsgFields): string | null {
  const raw = fields.messageDeliveryTime || fields.clientSubmitTime || fields.creationTime;
  if (!raw) return null;
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString();
}

// Envelope-style card rendered onto a canvas as the grid thumbnail: subject,
// sender, a body snippet and the attachment count. Dark background to match
// the STL thumbnails the grid already shows.
function drawMsgSnapshot(fields: MsgFields, bodyText: string): Promise<Blob | null> {
  const size = 256;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (!ctx) return Promise.resolve(null);

  ctx.fillStyle = '#1a1a1a';
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = 'rgba(0, 174, 66, 0.25)';
  ctx.fillRect(0, 0, size, 40);
  ctx.textBaseline = 'middle';

  ctx.fillStyle = '#ffffff';
  ctx.font = 'bold 13px sans-serif';
  ctx.fillText(fields.subject || '(no subject)', 10, 20, size - 20);

  ctx.fillStyle = '#9a9a9a';
  ctx.font = '11px sans-serif';
  ctx.fillText(fields.senderName || fields.senderEmail || '', 10, 54, size - 20);

  ctx.fillStyle = '#d4d4d4';
  const lines = bodyText.split('\n').filter((line) => line.trim()).slice(0, 8);
  lines.forEach((line, i) => {
    ctx.fillText(line.trim(), 10, 80 + i * 18, size - 20);
  });

  const attachmentCount = fields.attachments?.length ?? 0;
  if (attachmentCount > 0) {
    ctx.fillStyle = '#9a9a9a';
    ctx.fillText(`📎 ${attachmentCount}`, 10, size - 14);
  }
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
}

export function MsgPreviewModal({ libraryFileId, filename, fileSize, onClose, onSnapshot }: MsgPreviewModalProps) {
  const { t } = useTranslation();
  const [parsed, setParsed] = useState<ParsedMsg | null>(null);
  const [error, setError] = useState<string | null>(null);
  const snapshotSentRef = useRef(false);
  const onSnapshotRef = useRef(onSnapshot);
  useEffect(() => {
    onSnapshotRef.current = onSnapshot;
  });

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setParsed(null);
    setError(null);

    if (fileSize > MSG_PREVIEW_MAX_BYTES) {
      setError(t('fileManager.preview.tooLarge', { size: formatFileSize(fileSize) }));
      return;
    }

    const headers: HeadersInit = {};
    const token = getAuthToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;

    (async () => {
      const res = await fetch(api.getLibraryFileDownloadUrl(libraryFileId), { headers });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buffer = await res.arrayBuffer();

      // msgreader is loaded on demand so it stays out of the main bundle.
      const MsgReader = (await import('@kenjiuno/msgreader')).default;
      const reader = new MsgReader(buffer) as unknown as MsgReaderLike;
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

      if (cancelled) return;
      setParsed({ fields, bodyText, bodyFromRtf, reader });

      if (onSnapshotRef.current && !snapshotSentRef.current) {
        snapshotSentRef.current = true;
        const blob = await drawMsgSnapshot(fields, bodyText);
        if (blob && !cancelled) onSnapshotRef.current?.(blob);
      }
    })().catch(() => {
      if (!cancelled) setError(t('fileManager.preview.error'));
    });

    return () => {
      cancelled = true;
    };
  }, [libraryFileId, fileSize, t]);

  const downloadAttachment = (att: MsgAttachment) => {
    if (!parsed) return;
    try {
      const { fileName, content } = parsed.reader.getAttachment(att);
      const url = URL.createObjectURL(new Blob([content.slice().buffer]));
      const a = document.createElement('a');
      a.href = url;
      a.download = fileName || att.fileName || 'attachment';
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // Extraction is best-effort; the message itself stays downloadable.
    }
  };

  const fields = parsed?.fields;
  const to = fields?.recipients?.filter((r) => (r.recipType ?? 'to') === 'to') ?? [];
  const cc = fields?.recipients?.filter((r) => r.recipType === 'cc') ?? [];
  const date = fields ? formatMsgDate(fields) : null;
  const sender = fields ? recipientLabel({ name: fields.senderName, smtpAddress: fields.senderEmail }) : '';

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-4xl h-[85vh] border border-bambu-dark-tertiary flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2 min-w-0">
            <Mail className="w-5 h-5 text-bambu-green flex-shrink-0" />
            <h2 className="text-lg font-semibold text-white truncate">{fields?.subject || filename}</h2>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
            aria-label={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 min-h-0 overflow-auto bg-bambu-dark rounded-b-lg">
          {error ? (
            <div className="h-full flex items-center justify-center p-6">
              <p className="text-bambu-gray text-center">{error}</p>
            </div>
          ) : !parsed ? (
            <div className="h-full flex items-center justify-center">
              <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
            </div>
          ) : (
            <div className="p-4 space-y-4">
              {/* Envelope headers */}
              <div className="text-sm space-y-1">
                {sender && (
                  <div className="flex gap-2">
                    <span className="text-bambu-gray w-14 flex-shrink-0">{t('fileManager.preview.msg.from')}</span>
                    <span className="text-bambu-gray-light break-all">{sender}</span>
                  </div>
                )}
                {to.length > 0 && (
                  <div className="flex gap-2">
                    <span className="text-bambu-gray w-14 flex-shrink-0">{t('fileManager.preview.msg.to')}</span>
                    <span className="text-bambu-gray-light break-all">{to.map(recipientLabel).join('; ')}</span>
                  </div>
                )}
                {cc.length > 0 && (
                  <div className="flex gap-2">
                    <span className="text-bambu-gray w-14 flex-shrink-0">{t('fileManager.preview.msg.cc')}</span>
                    <span className="text-bambu-gray-light break-all">{cc.map(recipientLabel).join('; ')}</span>
                  </div>
                )}
                {date && (
                  <div className="flex gap-2">
                    <span className="text-bambu-gray w-14 flex-shrink-0">{t('fileManager.preview.msg.date')}</span>
                    <span className="text-bambu-gray-light">{date}</span>
                  </div>
                )}
              </div>

              {/* Attachments */}
              {(fields?.attachments?.length ?? 0) > 0 && (
                <div className="border border-bambu-dark-tertiary rounded-lg p-3">
                  <div className="flex items-center gap-1.5 text-xs text-bambu-gray font-medium uppercase tracking-wide mb-2">
                    <Paperclip className="w-3.5 h-3.5" />
                    {t('fileManager.preview.msg.attachments')} ({fields!.attachments!.length})
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {fields!.attachments!.map((att, index) => (
                      <button
                        key={`${att.fileName}-${index}`}
                        onClick={() => downloadAttachment(att)}
                        className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-bambu-dark-secondary border border-bambu-dark-tertiary text-xs text-bambu-gray-light hover:border-bambu-green/50 hover:text-white transition-colors"
                        title={t('common.download')}
                      >
                        <Download className="w-3 h-3 text-bambu-green" />
                        <span className="max-w-[240px] truncate">{att.fileName || '?'}</span>
                        {att.contentLength != null && (
                          <span className="text-bambu-gray">{formatFileSize(att.contentLength)}</span>
                        )}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* Body — plain text only; HTML bodies are reduced to text, so
                  nothing from the mail is ever rendered as markup. */}
              {parsed.bodyText ? (
                <div>
                  {parsed.bodyFromRtf && (
                    <p className="text-xs text-bambu-gray italic mb-2">{t('fileManager.preview.msg.rtfNote')}</p>
                  )}
                  <pre className="text-sm text-bambu-gray-light whitespace-pre-wrap break-words font-sans">
                    {parsed.bodyText}
                  </pre>
                </div>
              ) : (
                <p className="text-bambu-gray text-sm">{t('fileManager.preview.msg.noBody')}</p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
