/**
 * The placeholder icon a file with no thumbnail gets (#2976).
 *
 * csv/xlsx/ods/msg never get a server-rendered thumbnail, so this icon is all
 * the grid, the list and the columns view have to tell them apart. The three
 * views read one table, and this pins that they agree.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { FileManagerPage } from '../../pages/FileManagerPage';
import { server } from '../mocks/server';

vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => vi.fn(),
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
  libraryFile({ id: 1, filename: 'order.msg', file_type: 'msg' }),
  libraryFile({ id: 2, filename: 'bom.xlsx', file_type: 'xlsx' }),
  libraryFile({ id: 3, filename: 'parts.csv', file_type: 'csv' }),
  libraryFile({ id: 4, filename: 'drawing.pdf', file_type: 'pdf' }),
  libraryFile({ id: 5, filename: 'photo.png', file_type: 'png' }),
  libraryFile({ id: 6, filename: 'notes.md', file_type: 'md' }),
];

const expectedIcon: Record<string, string> = {
  'order.msg': 'lucide-mail',
  'bom.xlsx': 'lucide-file-spreadsheet',
  'parts.csv': 'lucide-file-spreadsheet',
  'drawing.pdf': 'lucide-file-text',
  'photo.png': 'lucide-image',
  // Nothing previews a Markdown file, so it keeps the generic box.
  'notes.md': 'lucide-file-box',
};

/** The icon inside a row's fixed-size thumbnail slot, by its lucide class. */
function thumbnailIconClass(row: HTMLElement, slot: string): string {
  const svg = row.querySelector(`${slot} svg`);
  return svg?.getAttribute('class') ?? '';
}

describe('FileManagerPage file-type icons', () => {
  beforeEach(() => {
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

  it('gives every type its own icon in the grid', async () => {
    render(<FileManagerPage />);
    await screen.findByText('order.msg');

    for (const [filename, icon] of Object.entries(expectedIcon)) {
      const card = screen.getByText(filename).closest('div.group') as HTMLElement;
      expect(thumbnailIconClass(card, '.aspect-square')).toContain(icon);
    }
  });

  it('gives every type the same icon in the list', async () => {
    (localStorage.getItem as ReturnType<typeof vi.fn>).mockImplementation((key: string) =>
      key === 'library-view-mode' ? 'list' : null,
    );
    render(<FileManagerPage />);
    await screen.findByText('order.msg');

    for (const [filename, icon] of Object.entries(expectedIcon)) {
      const row = screen.getByText(filename).closest('div[class*="grid-cols-"]') as HTMLElement;
      expect(thumbnailIconClass(row, '.w-10.h-10')).toContain(icon);
    }
  });

  it('gives every type the same icon in the columns view', async () => {
    const user = userEvent.setup();
    render(<FileManagerPage />);
    await screen.findByText('order.msg');

    await user.click(screen.getByTitle('Column view'));
    const columns = within(screen.getByTestId('columns-view'));

    for (const [filename, icon] of Object.entries(expectedIcon)) {
      const row = columns.getByText(filename).closest('div.group') as HTMLElement;
      expect(thumbnailIconClass(row, '.w-10.h-10')).toContain(icon);
    }
  });
});
