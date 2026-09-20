/**
 * Tests for the LibraryFileDetailsModal component (#3077).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { LibraryFileDetailsModal } from '../../components/LibraryFileDetailsModal';
import type { LibraryFileListItem } from '../../api/client';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const listItem: LibraryFileListItem = {
  id: 7,
  folder_id: null,
  is_external: false,
  filename: 'benchy.gcode.3mf',
  file_type: 'gcode.3mf',
  file_size: 1048576,
  thumbnail_path: null,
  print_count: 2,
  duplicate_count: 0,
  created_by_id: null,
  created_by_username: null,
  created_at: '2024-01-01T00:00:00Z',
  fs_modified_at: null,
  print_name: 'Benchy',
  print_time_seconds: 3600,
  filament_used_grams: 12.5,
  sliced_for_model: 'X1C',
  tags: [],
};

const details = {
  ...listItem,
  folder_name: null,
  project_id: null,
  project_name: null,
  file_path: 'library/files/benchy.gcode.3mf',
  file_hash: null,
  metadata: null,
  last_printed_at: null,
  notes: 'Print with brim',
  external_url: 'https://www.printables.com/model/1',
  photos: ['abc123.jpg'],
  source_url: 'https://makerworld.com/models/42',
  duplicates: null,
  updated_at: '2024-02-01T00:00:00Z',
};

describe('LibraryFileDetailsModal', () => {
  const onClose = vi.fn();
  let lastUpdate: Record<string, unknown> | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
    lastUpdate = null;
    server.use(
      http.get('/api/v1/library/files/7', () => HttpResponse.json(details)),
      http.put('/api/v1/library/files/7', async ({ request }) => {
        lastUpdate = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...details, ...lastUpdate });
      }),
      http.delete('/api/v1/library/files/7/photos/:filename', () =>
        HttpResponse.json({ status: 'deleted', photos: [] })
      )
    );
  });

  it('renders the facts, notes, link and photos from the detail response', async () => {
    render(<LibraryFileDetailsModal file={listItem} canEdit onClose={onClose} />);

    expect(screen.getByText('benchy.gcode.3mf')).toBeInTheDocument();
    // Header badge and the Type fact.
    expect(screen.getAllByText('GCODE.3MF')).toHaveLength(2);
    expect(screen.getByText('1.0 MB')).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByDisplayValue('Print with brim')).toBeInTheDocument();
    });
    expect(screen.getByDisplayValue('https://www.printables.com/model/1')).toBeInTheDocument();
    expect(screen.getByText('Benchy')).toBeInTheDocument();
    expect(screen.getByText('X1C')).toBeInTheDocument();
    expect(screen.getByText('12.5 g')).toBeInTheDocument();

    // Source provenance is a link, not an editable field.
    const source = screen.getByRole('link', { name: /makerworld\.com\/models\/42/ });
    expect(source).toHaveAttribute('href', 'https://makerworld.com/models/42');
    expect(source).toHaveAttribute('target', '_blank');

    const photo = screen.getByAltText('Photos') as HTMLImageElement;
    expect(photo.src).toContain('/library/files/7/photos/abc123.jpg');
  });

  it('saves edited notes and link through updateLibraryFile', async () => {
    const user = userEvent.setup();
    render(<LibraryFileDetailsModal file={listItem} canEdit onClose={onClose} />);

    const notes = await screen.findByDisplayValue('Print with brim');
    const saveButton = screen.getByRole('button', { name: /save/i });
    // Nothing changed yet, so there is nothing to save.
    expect(saveButton).toBeDisabled();

    await user.clear(notes);
    await user.type(notes, 'Use 0.2 mm layers');
    const link = screen.getByDisplayValue('https://www.printables.com/model/1');
    await user.clear(link);
    await user.type(link, 'https://example.com/part ');

    expect(saveButton).toBeEnabled();
    await user.click(saveButton);

    await waitFor(() => {
      expect(lastUpdate).toEqual({ notes: 'Use 0.2 mm layers', external_url: 'https://example.com/part' });
    });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('sends an empty external_url when the link is cleared', async () => {
    const user = userEvent.setup();
    render(<LibraryFileDetailsModal file={listItem} canEdit onClose={onClose} />);

    const link = await screen.findByDisplayValue('https://www.printables.com/model/1');
    await user.clear(link);
    await user.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(lastUpdate).toEqual({ notes: 'Print with brim', external_url: '' });
    });
  });

  it('is read-only without edit permission', async () => {
    render(<LibraryFileDetailsModal file={listItem} canEdit={false} onClose={onClose} />);

    const notes = await screen.findByDisplayValue('Print with brim');
    expect(notes).toBeDisabled();
    expect(screen.getByDisplayValue('https://www.printables.com/model/1')).toBeDisabled();
    expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Add photo')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Delete photo')).not.toBeInTheDocument();
  });

  it('removes a photo from the grid after deleting it', async () => {
    const user = userEvent.setup();
    render(<LibraryFileDetailsModal file={listItem} canEdit onClose={onClose} />);

    await screen.findByAltText('Photos');
    await user.click(screen.getByLabelText('Delete photo'));

    await waitFor(() => {
      expect(screen.queryByAltText('Photos')).not.toBeInTheDocument();
    });
  });

  it('keeps unsaved notes when a photo change refetches the file', async () => {
    // Deleting a photo invalidates the detail query; the refetched file has a
    // different photo list, so it is a new object and must not reseed the form.
    let serverPhotos = ['abc123.jpg'];
    let detailFetches = 0;
    server.use(
      http.get('/api/v1/library/files/7', () => {
        detailFetches += 1;
        return HttpResponse.json({ ...details, photos: serverPhotos });
      }),
      http.delete('/api/v1/library/files/7/photos/:filename', () => {
        serverPhotos = [];
        return HttpResponse.json({ status: 'deleted', photos: [] });
      })
    );
    const user = userEvent.setup();
    render(<LibraryFileDetailsModal file={listItem} canEdit onClose={onClose} />);

    const notes = await screen.findByDisplayValue('Print with brim');
    await user.clear(notes);
    await user.type(notes, 'Draft not saved yet');

    await user.click(screen.getByLabelText('Delete photo'));
    await waitFor(() => {
      expect(screen.queryByAltText('Photos')).not.toBeInTheDocument();
    });
    await waitFor(() => expect(detailFetches).toBeGreaterThan(1));

    expect(screen.getByDisplayValue('Draft not saved yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /save/i })).toBeEnabled();
  });
});
