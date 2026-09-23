import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { api, type PendingPreviewThumbnail } from '../api/client';

const ModelViewerModal = lazy(() =>
  import('./ModelViewerModal').then((m) => ({ default: m.ModelViewerModal }))
);
const PdfPreviewModal = lazy(() => import('./PdfPreviewModal').then((m) => ({ default: m.PdfPreviewModal })));
const SpreadsheetPreviewModal = lazy(() =>
  import('./SpreadsheetPreviewModal').then((m) => ({ default: m.SpreadsheetPreviewModal }))
);
const MsgPreviewModal = lazy(() => import('./MsgPreviewModal').then((m) => ({ default: m.MsgPreviewModal })));

// A file whose picture only a browser can draw waits for someone to open its
// preview once — which on a library full of STEP files and spreadsheets means
// the grid stays a wall of generic icons. This renders those previews off
// screen, one at a time, and posts each first render back through the same
// endpoint an opened preview uses (#2976).
//
// The previews themselves are the real components, not a second renderer: the
// thumbnail a batch produces is then exactly the one opening the file would
// have produced, and there is no second code path to keep in step.

const SHEET_TYPES = new Set(['csv', 'xlsx', 'ods']);
const MODEL_TYPES = new Set(['step', 'stp']);

/** How long one file may take before the batch gives up on it and moves on. */
const PER_FILE_TIMEOUT_MS = 45_000;

function canRender(fileType: string): boolean {
  return MODEL_TYPES.has(fileType) || fileType === 'pdf' || fileType === 'msg' || SHEET_TYPES.has(fileType);
}

export interface PreviewThumbnailBatchProps {
  files: PendingPreviewThumbnail[];
  /** Called after every file, finished or skipped, for the button's counter. */
  onProgress?: (done: number, total: number) => void;
  /** Called once, with how many thumbnails actually landed. */
  onDone: (succeeded: number) => void;
}

export function PreviewThumbnailBatch({ files, onProgress, onDone }: PreviewThumbnailBatchProps) {
  const [index, setIndex] = useState(0);
  const succeededRef = useRef(0);
  const handledRef = useRef<number | null>(null);
  const onDoneRef = useRef(onDone);
  const onProgressRef = useRef(onProgress);
  useEffect(() => {
    onDoneRef.current = onDone;
    onProgressRef.current = onProgress;
  }, [onDone, onProgress]);

  const current = files[index];

  const advance = useCallback(() => {
    setIndex((i) => i + 1);
  }, []);

  // One timer per file: a preview that never reaches a first render (a corrupt
  // STEP, a PDF pdf.js refuses) must not strand the whole run. A type this
  // build has no renderer for is skipped at once rather than waited out — the
  // server's list is derived from its own allowlist and may name a type only
  // another build knows.
  useEffect(() => {
    if (!current) return;
    handledRef.current = null;
    if (!canRender(current.file_type)) {
      advance();
      return;
    }
    const timer = window.setTimeout(advance, PER_FILE_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [current, advance]);

  useEffect(() => {
    if (files.length === 0 || index < files.length) return;
    onProgressRef.current?.(files.length, files.length);
    onDoneRef.current(succeededRef.current);
  }, [index, files.length]);

  const handleSnapshot = useCallback(
    (fileId: number) => (blob: Blob) => {
      // The viewer can emit more than one frame; only the first is the
      // thumbnail, and only one upload per file is wanted.
      if (handledRef.current === fileId) return;
      handledRef.current = fileId;
      api
        .uploadLibraryPreviewThumbnail(fileId, blob)
        .then((res) => {
          if (res.updated) succeededRef.current += 1;
        })
        .catch(() => {
          // Best effort: a file that will not upload is simply skipped, the
          // run carries on with the next one.
        })
        .finally(() => {
          onProgressRef.current?.(Math.min(index + 1, files.length), files.length);
          advance();
        });
    },
    [advance, index, files.length]
  );

  if (!current) return null;

  const title = current.filename;
  const key = `preview-batch-${current.id}`;

  return (
    // `fixed` inside the previews positions against this host rather than the
    // viewport because the transform makes it a containing block, so the work
    // happens entirely off screen. aria-hidden + pointer-events keep it out of
    // the accessibility tree and out of the user's way.
    <div
      aria-hidden
      data-testid="preview-thumbnail-batch"
      className="pointer-events-none fixed opacity-0"
      style={{ left: '-20000px', top: 0, width: 1100, height: 820, transform: 'translateZ(0)' }}
    >
      <Suspense fallback={null}>
        {MODEL_TYPES.has(current.file_type) && (
          <ModelViewerModal
            key={key}
            libraryFileId={current.id}
            title={title}
            fileType={current.file_type}
            onClose={advance}
            onSnapshot={handleSnapshot(current.id)}
          />
        )}
        {current.file_type === 'pdf' && (
          <PdfPreviewModal
            key={key}
            libraryFileId={current.id}
            filename={title}
            fileSize={current.file_size}
            onClose={advance}
            onSnapshot={handleSnapshot(current.id)}
          />
        )}
        {current.file_type === 'msg' && (
          <MsgPreviewModal
            key={key}
            libraryFileId={current.id}
            filename={title}
            fileSize={current.file_size}
            onClose={advance}
            onSnapshot={handleSnapshot(current.id)}
          />
        )}
        {SHEET_TYPES.has(current.file_type) && (
          <SpreadsheetPreviewModal
            key={key}
            libraryFileId={current.id}
            filename={title}
            fileType={current.file_type}
            fileSize={current.file_size}
            onClose={advance}
            onSnapshot={handleSnapshot(current.id)}
          />
        )}
      </Suspense>
    </div>
  );
}
