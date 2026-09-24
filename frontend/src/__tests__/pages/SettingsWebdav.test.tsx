/**
 * The three-way WebDAV setting in the File Manager settings card (#3152).
 *
 * The setting exposes a whole library over a second protocol, so what an
 * operator has to know before turning it on — what each of the three modes
 * actually allows, that Basic credentials travel with every request so it
 * belongs behind HTTPS, that Windows will not send those credentials over plain
 * HTTP until a registry value is changed, and that a two-factor account cannot
 * open the share at all — is part of the contract, not decoration.
 *
 * The write mode adds one more: what a save from a mapped drive does to the
 * library entry it lands on. "Read and write" is the option that can lose
 * somebody's work, so it does not get to be the one with no explanation.
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
  check_updates: false,
  check_printer_firmware: false,
};

const READ_ONLY_HINT = /nothing done on the mapped drive changes the library/;
const WRITE_HINT = /keeps the same library entry, with its tags and notes/;
const WINDOWS_HINT = /BasicAuthLevel is set to 2/;
const TWO_FACTOR_HINT = /two-factor authentication cannot open the share/;

describe('SettingsPage — WebDAV', () => {
  let saved: Array<Record<string, unknown>>;

  beforeEach(() => {
    window.history.replaceState({}, '', '/');
    localStorage.clear();
    setAuthToken(null);
    saved = [];

    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json(mockSettings)),
      http.put('/api/v1/settings/', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        saved.push(body);
        return HttpResponse.json({ ...mockSettings, ...body });
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

  it('offers the three modes, off, and says nothing about a share that is not served', async () => {
    render(<SettingsPage />);

    expect(await findOption('Off')).toBeChecked();
    expect(await findOption('Read-only')).not.toBeChecked();
    expect(await findOption('Read and write')).not.toBeChecked();
    expect(screen.queryByText(WINDOWS_HINT)).not.toBeInTheDocument();
    expect(screen.queryByText(TWO_FACTOR_HINT)).not.toBeInTheDocument();
  });

  it('spells out what each mode allows, whichever one is selected', async () => {
    render(<SettingsPage />);
    await findOption('Off');

    // The choice itself has to be legible before it is made: "read and write"
    // is the option that can lose work, and it is the reason the modes carry a
    // line each rather than just a label.
    expect(screen.getByText(READ_ONLY_HINT)).toBeInTheDocument();
    expect(
      screen.getByText(/Saving, renaming, moving and deleting from the drive as well/)
    ).toBeInTheDocument();
  });

  it('spells out HTTPS, the Windows registry gate and the 2FA limit once it is on', async () => {
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, webdav_mode: 'read' }))
    );
    render(<SettingsPage />);

    expect(await findOption('Read-only')).toBeChecked();
    expect(screen.getByText(WINDOWS_HINT)).toBeInTheDocument();
    // The address a client maps, and the two buckets it will find there.
    expect(screen.getByText(/\/webdav/)).toBeInTheDocument();
    // Credentials are required even here, where auth_enabled is false.
    expect(screen.getByText(/always asks for a Bambuddy username and password/)).toBeInTheDocument();
    // ...and an account that needs a second code cannot present one over Basic,
    // so the share refuses it rather than serving the library on a password.
    expect(screen.getByText(TWO_FACTOR_HINT)).toBeInTheDocument();
    // A read-only share has nothing to say about overwriting.
    expect(screen.queryByText(WRITE_HINT)).not.toBeInTheDocument();
  });

  it('says what a save from the drive does, but only in the writable mode', async () => {
    server.use(
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ ...mockSettings, webdav_mode: 'readwrite' })
      )
    );
    render(<SettingsPage />);

    expect(await findOption('Read and write')).toBeChecked();
    expect(screen.getByText(WRITE_HINT)).toBeInTheDocument();
    expect(screen.getByText(/Deleting moves the file to the trash/)).toBeInTheDocument();
  });

  it('persists the mode — a setting missing from the save list never reaches the server', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);

    // SettingsPage ignores edits for the first 100ms after its settings arrive
    // (isInitialLoadRef), so a click inside that window never reaches the
    // debounced save and the assertion below would be measuring the race
    // rather than the save list.
    expect(await findOption('Off')).toBeChecked();
    await new Promise((resolve) => setTimeout(resolve, 200));

    await user.click(await findOption('Read and write'));

    await waitFor(
      () => {
        expect(saved.some((body) => body.webdav_mode === 'readwrite')).toBe(true);
      },
      { timeout: 5000 }
    );
  });
});

function findOption(name: string): Promise<HTMLElement> {
  return screen.findByRole('radio', { name: new RegExp(name) });
}
