/**
 * Where the library keeps its files, in the File Manager settings card (#3160).
 *
 * The mode decides whether Bambuddy owns the bytes or only indexes them, and
 * the two things an owner has to know before choosing the tree — that the
 * database and the disk can now drift, so scanning becomes routine, and that a
 * scanned file is not hashed and so does not take part in duplicate detection —
 * are the trade they are accepting. They belong on the card, not in a wiki page
 * nobody reads first.
 *
 * The migration is a separate action from the switch, and its plan is shown
 * before anything moves: a collision is a decision about which file gets
 * renamed, and "Move now" stays out of reach — and says so — until there is
 * none.
 *
 * The card saves on demand rather than on every keystroke. Picking "Directory"
 * used to send the mode with an empty path the same second, which the server
 * refuses, so the card reported a failure at somebody who was still typing.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { SettingsPage } from '../../pages/SettingsPage';
import { setAuthToken } from '../../api/client';

const mockSettings = {
  auto_archive: true,
  save_thumbnails: true,
  capture_finish_photo: true,
  default_filament_cost: 25.0,
  currency: 'USD',
  library_archive_mode: 'ask',
  library_disk_warning_gb: 5,
  library_root_view: 'all',
  webdav_mode: 'off',
  library_storage_mode: 'managed',
  library_storage_path: '',
  check_updates: false,
  check_printer_firmware: false,
};

const emptyPlan = {
  storage_path: '/mnt/nas/bambuddy',
  file_count: 2,
  folder_count: 3,
  total_bytes: 2048,
  blockers: [],
  missing: [],
  moves: [],
};

const DRIFT_HINT = /invisible to Bambuddy until the folder is scanned/;
const HASH_HINT = /do not take part in duplicate detection/;

describe('SettingsPage — library storage', () => {
  let saved: Array<Record<string, unknown>>;
  let migrations: number;

  beforeEach(() => {
    window.history.replaceState({}, '', '/');
    localStorage.clear();
    setAuthToken(null);
    saved = [];
    migrations = 0;

    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json(mockSettings)),
      http.put('/api/v1/settings/', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        saved.push(body);
        return HttpResponse.json({ ...mockSettings, ...body });
      }),
      http.get('/api/v1/library/storage/migration-plan', () => HttpResponse.json(emptyPlan)),
      http.post('/api/v1/library/storage/migrate', () => {
        migrations += 1;
        return HttpResponse.json({
          status: 'success',
          storage_path: '/mnt/nas/bambuddy',
          moved: 2,
          moved_bytes: 2048,
          directories_created: 3,
          skipped: [],
          failures: [],
        });
      }),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/notifications/', () => HttpResponse.json([])),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([])),
      http.get('/api/v1/mqtt/status', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/virtual-printer/status', () => HttpResponse.json({ running: false })),
      http.get('/api/v1/auth/status', () =>
        HttpResponse.json({ auth_enabled: false, requires_setup: false })
      ),
      http.get('/api/v1/external-links/', () => HttpResponse.json([]))
    );
  });

  it('defaults to Bambuddy\'s own library and asks for no path', async () => {
    render(<SettingsPage />);

    expect(await findOption("Bambuddy's own library")).toBeChecked();
    expect(await findOption('Directory')).not.toBeChecked();
    // No tree, no path, and nothing to say about drift that cannot happen.
    expect(screen.queryByLabelText('Path to the directory')).not.toBeInTheDocument();
    expect(screen.queryByText(DRIFT_HINT)).not.toBeInTheDocument();
    expect(screen.queryByText(/Show what would move/)).not.toBeInTheDocument();
  });

  it('states the trade the tree costs, and asks for the path', async () => {
    server.use(
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({
          ...mockSettings,
          library_storage_mode: 'directory',
          library_storage_path: '/mnt/nas/bambuddy',
        })
      )
    );
    render(<SettingsPage />);

    expect(await findOption('Directory')).toBeChecked();
    expect(screen.getByLabelText('Path to the directory')).toHaveValue('/mnt/nas/bambuddy');
    expect(screen.getByText(DRIFT_HINT)).toBeInTheDocument();
    expect(screen.getByText(HASH_HINT)).toBeInTheDocument();
    // The path is the server's, not the workstation's — a mapped drive letter
    // here is the mistake this line exists to prevent.
    expect(screen.getByText(/as the Bambuddy server sees it/)).toBeInTheDocument();
  });

  it('says the switch moves nothing, and keeps the migration a separate action', async () => {
    server.use(
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ ...mockSettings, library_storage_mode: 'directory' })
      )
    );
    render(<SettingsPage />);

    await findOption('Directory');
    expect(screen.getByText(/Switching the mode moves nothing by itself/)).toBeInTheDocument();
    // Nothing has been planned yet, so there is nothing to move yet either.
    expect(screen.getByRole('button', { name: 'Move now' })).toBeDisabled();
    expect(migrations).toBe(0);
  });

  it('shows the plan first, then lets it run', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ ...mockSettings, library_storage_mode: 'directory' })
      )
    );
    render(<SettingsPage />);

    await findOption('Directory');
    await user.click(screen.getByRole('button', { name: 'Show what would move' }));

    const plan = await screen.findByTestId('library-storage-plan');
    expect(plan).toHaveTextContent('2 file(s), 3 folder(s)');
    const move = screen.getByRole('button', { name: 'Move now' });
    expect(move).toBeEnabled();

    await user.click(move);
    await waitFor(() => expect(migrations).toBe(1));
  });

  it('does not save while the pair is incomplete', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);

    expect(await findOption("Bambuddy's own library")).toBeChecked();
    await new Promise((resolve) => setTimeout(resolve, 200));
    await user.click(await findOption('Directory'));

    // The path is still empty, so there is nothing worth sending — and the
    // server would refuse it, which is what used to surface as a red toast.
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.getByText('Enter the path first.')).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(saved.some((body) => 'library_storage_mode' in body)).toBe(false);
  });

  it('refuses to run while a collision is on the plan', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ ...mockSettings, library_storage_mode: 'directory' })
      ),
      http.get('/api/v1/library/storage/migration-plan', () =>
        HttpResponse.json({
          ...emptyPlan,
          blockers: [
            {
              kind: 'exists',
              target: '/mnt/nas/bambuddy/Kunden/part.3mf',
              names: ['part.3mf (file id 3)'],
              message: '/mnt/nas/bambuddy/Kunden/part.3mf already exists on the share',
            },
          ],
        })
      )
    );
    render(<SettingsPage />);

    await findOption('Directory');
    await user.click(screen.getByRole('button', { name: 'Show what would move' }));

    // The name first and in the user's language, the path underneath, and a
    // line saying why the button below is shut.
    expect(await screen.findByText(/part\.3mf \(file id 3\) already exists there/)).toBeInTheDocument();
    expect(screen.getByText('/mnt/nas/bambuddy/Kunden/part.3mf')).toBeInTheDocument();
    expect(screen.getByText(/blocked by 1 name collision/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Move now' })).toBeDisabled();
    expect(migrations).toBe(0);
  });

  it('persists the mode and the path together, on the save', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);

    expect(await findOption("Bambuddy's own library")).toBeChecked();
    await new Promise((resolve) => setTimeout(resolve, 200));

    await user.click(await findOption('Directory'));
    await user.type(screen.getByLabelText('Path to the directory'), '/mnt/nas/bambuddy');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(
      () => {
        expect(
          saved.some(
            (body) =>
              body.library_storage_mode === 'directory' &&
              body.library_storage_path === '/mnt/nas/bambuddy'
          )
        ).toBe(true);
      },
      { timeout: 5000 }
    );
  });
});

function findOption(name: string): Promise<HTMLElement> {
  return screen.findByRole('radio', { name: new RegExp(name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')) });
}
