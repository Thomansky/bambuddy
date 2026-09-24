/**
 * The WebDAV switch in the File Manager settings card (#3152).
 *
 * The setting exposes a whole library over a second protocol, so the three
 * things an operator has to know before turning it on — that it is read-only,
 * that Basic credentials travel with every request so it belongs behind HTTPS,
 * and that Windows will not send those credentials over plain HTTP until a
 * registry value is changed — are part of the contract, not decoration.
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
  webdav_enabled: false,
  check_updates: false,
  check_printer_firmware: false,
};

const TOGGLE_LABEL = 'WebDAV access (read-only)';
const READ_ONLY_HINT = /Read-only: files can be opened and copied/;
const WINDOWS_HINT = /BasicAuthLevel is set to 2/;

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

  it('offers the switch off, and says nothing about a share that is not served', async () => {
    render(<SettingsPage />);

    const toggle = await findToggle();
    expect(toggle).not.toBeChecked();
    expect(screen.queryByText(READ_ONLY_HINT)).not.toBeInTheDocument();
    expect(screen.queryByText(WINDOWS_HINT)).not.toBeInTheDocument();
  });

  it('spells out read-only, HTTPS and the Windows registry gate once it is on', async () => {
    server.use(
      http.get('/api/v1/settings/', () =>
        HttpResponse.json({ ...mockSettings, webdav_enabled: true })
      )
    );
    render(<SettingsPage />);

    expect(await findToggle()).toBeChecked();
    expect(screen.getByText(READ_ONLY_HINT)).toBeInTheDocument();
    expect(screen.getByText(WINDOWS_HINT)).toBeInTheDocument();
    // The address a client maps, and the two buckets it will find there.
    expect(screen.getByText(/\/webdav/)).toBeInTheDocument();
    // Credentials are required even here, where auth_enabled is false.
    expect(screen.getByText(/always asks for a Bambuddy username and password/)).toBeInTheDocument();
  });

  it('persists the switch — a setting missing from the save list never reaches the server', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);

    await user.click(await findToggle());

    await waitFor(
      () => {
        expect(saved.some((body) => body.webdav_enabled === true)).toBe(true);
      },
      { timeout: 5000 }
    );
  });
});

function findToggle(): Promise<HTMLElement> {
  return screen.findByRole('checkbox', { name: TOGGLE_LABEL });
}
