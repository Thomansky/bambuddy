import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import type { PendingPreviewThumbnail } from '../../api/client';

// The renderers themselves are exercised by their own tests; here they stand in
// for "a preview that reaches its first frame", so the batch's sequencing is
// what is under test.
const snapshots: Array<(blob: Blob) => void> = [];

vi.mock('../../components/PdfPreviewModal', () => ({
  PdfPreviewModal: ({ onSnapshot }: { onSnapshot?: (b: Blob) => void }) => {
    if (onSnapshot) snapshots.push(onSnapshot);
    return <div data-testid="pdf-modal" />;
  },
}));
vi.mock('../../components/SpreadsheetPreviewModal', () => ({
  SpreadsheetPreviewModal: ({ onSnapshot }: { onSnapshot?: (b: Blob) => void }) => {
    if (onSnapshot) snapshots.push(onSnapshot);
    return <div data-testid="sheet-modal" />;
  },
}));
vi.mock('../../components/ModelViewerModal', () => ({
  ModelViewerModal: ({ onSnapshot }: { onSnapshot?: (b: Blob) => void }) => {
    if (onSnapshot) snapshots.push(onSnapshot);
    return <div data-testid="model-modal" />;
  },
}));

const uploadMock = vi.fn();
vi.mock('../../api/client', () => ({
  api: {
    uploadLibraryPreviewThumbnail: (...args: unknown[]) => uploadMock(...args),
  },
}));

import { PreviewThumbnailBatch } from '../../components/PreviewThumbnailBatch';

function file(id: number, file_type: string): PendingPreviewThumbnail {
  return { id, filename: `file-${id}.${file_type}`, file_type, file_size: 1024, created_by_id: null };
}

describe('PreviewThumbnailBatch', () => {
  beforeEach(() => {
    snapshots.length = 0;
    uploadMock.mockReset();
    uploadMock.mockResolvedValue({ updated: true });
  });

  it('renders one file at a time and uploads each first frame', async () => {
    const onDone = vi.fn();
    render(<PreviewThumbnailBatch files={[file(1, 'pdf'), file(2, 'xlsx')]} onDone={onDone} />);

    // First file only — the batch is deliberately sequential so a library of
    // STEP files cannot start a dozen WebGL contexts at once. (The renderers
    // are code-split, hence findBy rather than getBy.)
    expect(await screen.findByTestId('pdf-modal')).toBeInTheDocument();
    expect(screen.queryByTestId('sheet-modal')).not.toBeInTheDocument();

    snapshots[0](new Blob(['a'], { type: 'image/png' }));
    await waitFor(() => expect(screen.getByTestId('sheet-modal')).toBeInTheDocument());
    expect(uploadMock).toHaveBeenCalledWith(1, expect.any(Blob));

    snapshots[snapshots.length - 1](new Blob(['b'], { type: 'image/png' }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(2));
    expect(uploadMock).toHaveBeenCalledWith(2, expect.any(Blob));
  });

  it('counts only thumbnails that actually landed', async () => {
    uploadMock.mockResolvedValueOnce({ updated: false });
    const onDone = vi.fn();
    render(<PreviewThumbnailBatch files={[file(7, 'pdf')]} onDone={onDone} />);

    snapshots[0](new Blob(['a'], { type: 'image/png' }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(0));
  });

  it('carries on when an upload fails', async () => {
    uploadMock.mockRejectedValueOnce(new Error('boom'));
    const onDone = vi.fn();
    render(<PreviewThumbnailBatch files={[file(3, 'pdf'), file(4, 'step')]} onDone={onDone} />);

    snapshots[0](new Blob(['a'], { type: 'image/png' }));
    await waitFor(() => expect(screen.getByTestId('model-modal')).toBeInTheDocument());
    snapshots[snapshots.length - 1](new Blob(['b'], { type: 'image/png' }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(1));
  });

  // A CAD format the server lists nowhere and no preview can draw: the batch
  // must step over it rather than sit out its 45 s budget. (Not 'msg' — the
  // fork renders those, so it would stop being an unknown type there.)
  it('skips a type this build has no renderer for without waiting it out', async () => {
    const onDone = vi.fn();
    render(<PreviewThumbnailBatch files={[file(5, 'catpart'), file(6, 'pdf')]} onDone={onDone} />);

    // Straight past the unknown type to the one it can draw, no timer involved.
    await waitFor(() => expect(screen.getByTestId('pdf-modal')).toBeInTheDocument());
    snapshots[snapshots.length - 1](new Blob(['a'], { type: 'image/png' }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(1));
    expect(uploadMock).toHaveBeenCalledTimes(1);
  });

  it('reports progress as files finish', async () => {
    const onProgress = vi.fn();
    render(<PreviewThumbnailBatch files={[file(8, 'pdf'), file(9, 'pdf')]} onDone={vi.fn()} onProgress={onProgress} />);

    snapshots[0](new Blob(['a'], { type: 'image/png' }));
    await waitFor(() => expect(onProgress).toHaveBeenCalledWith(1, 2));
    snapshots[snapshots.length - 1](new Blob(['b'], { type: 'image/png' }));
    await waitFor(() => expect(onProgress).toHaveBeenCalledWith(2, 2));
  });
});
