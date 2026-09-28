/**
 * How a preview is opened from the File Manager (#2976): the per-file menu
 * and the list's action strip.
 *
 * The preview modals themselves are stubbed — pdf.js, SheetJS and three.js
 * have their own tests and none of them render in jsdom.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { FileManagerPage } from '../../pages/FileManagerPage';
import { server } from '../mocks/server';

vi.mock('../../components/ModelViewerModal', () => ({
  ModelViewerModal: ({ title }: { title: string }) => <div data-testid="model-viewer-modal">{title}</div>,
}));
vi.mock('../../components/PdfPreviewModal', () => ({
  PdfPreviewModal: ({ filename }: { filename: string }) => <div data-testid="pdf-preview-modal">{filename}</div>,
}));
vi.mock('../../components/SpreadsheetPreviewModal', () => ({
  SpreadsheetPreviewModal: ({ filename }: { filename: string }) => (
    <div data-testid="sheet-preview-modal">{filename}</div>
  ),
}));
vi.mock('../../components/ImagePreviewModal', () => ({
  ImagePreviewModal: ({ filename }: { filename: string }) => <div data-testid="image-preview-modal">{filename}</div>,
}));

function libraryFile(overrides: Record<string, unknown>) {
  return {
    file_path: '/library/file',
    file_size: 4096,
    folder_id: null,
    thumbnail_path: null,
    print_name: null,
    print_time_seconds: null,
    print_count: 0,
    duplicate_count: 0,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

const mockFiles = [
  libraryFile({ id: 1, filename: 'benchy.gcode.3mf', file_type: 'gcode.3mf' }),
  libraryFile({ id: 2, filename: 'bracket.stl', file_type: 'stl' }),
  libraryFile({ id: 3, filename: 'drawing.pdf', file_type: 'pdf' }),
  libraryFile({ id: 4, filename: 'parts.csv', file_type: 'csv' }),
  libraryFile({ id: 5, filename: 'photo.png', file_type: 'png' }),
  libraryFile({ id: 6, filename: 'notes.md', file_type: 'md' }),
  libraryFile({ id: 7, filename: 'scan.tif', file_type: 'tif' }),
];

function card(name: string): HTMLElement {
  return screen.getByText(name).closest('div.group') as HTMLElement;
}

function row(name: string): HTMLElement {
  return screen.getByText(name).closest('div[class*="grid-cols-"]') as HTMLElement;
}

describe('FileManagerPage preview opening', () => {
  beforeEach(() => {
    // localStorage is a module-global vi.fn mock (see __tests__/setup.ts), so
    // the view mode is programmed rather than written.
    (localStorage.getItem as ReturnType<typeof vi.fn>).mockReturnValue(null);
    server.use(
      http.get('/api/v1/library/folders', () => HttpResponse.json([])),
      http.get('/api/v1/library/files', () => HttpResponse.json(mockFiles)),
      http.get('/api/v1/library/stats', () =>
        HttpResponse.json({
          total_files: mockFiles.length,
          total_folders: 0,
          total_size_bytes: 1024,
          disk_free_bytes: 1024 * 1024,
          disk_total_bytes: 2048 * 1024,
        }),
      ),
      http.get('/api/v1/settings/', () => HttpResponse.json({ check_updates: false })),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/archives/', () => HttpResponse.json([])),
    );
  });

  describe('the list view', () => {
    beforeEach(() => {
      (localStorage.getItem as ReturnType<typeof vi.fn>).mockImplementation((key: string) =>
        key === 'library-view-mode' ? 'list' : null,
      );
    });

    it('offers an image file the same action-strip preview button as a document', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await screen.findByText('photo.png');

      await user.click(within(row('photo.png')).getByTitle('Preview'));

      expect(await screen.findByTestId('image-preview-modal')).toHaveTextContent('photo.png');
    });
  });

  // The server thumbnails TIFF (PIL), but an <img> only decodes it on Safari,
  // so the preview is not offered rather than downloading 50 MB to fail (#2976).
  describe('a TIFF file', () => {
    it('gets no Preview entry in the card menu', async () => {
      const user = userEvent.setup();
      render(<FileManagerPage />);
      await screen.findByText('scan.tif');

      const kebab = card('scan.tif').querySelector('.lucide-ellipsis-vertical')?.closest('button') as HTMLButtonElement;
      await user.click(kebab);
      expect(within(card('scan.tif')).queryByText('Preview')).not.toBeInTheDocument();
    });
  });

  it('offers an image file a Preview entry in the card menu', async () => {
    const user = userEvent.setup();
    render(<FileManagerPage />);
    await screen.findByText('photo.png');

    const imageCard = card('photo.png');
    const kebab = imageCard.querySelector('.lucide-ellipsis-vertical')?.closest('button') as HTMLButtonElement;
    await user.click(kebab);
    await user.click(within(imageCard).getByText('Preview'));

    expect(await screen.findByTestId('image-preview-modal')).toHaveTextContent('photo.png');
  });
});
