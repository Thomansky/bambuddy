/**
 * Settings → Files (#3161): the library settings and the backups in one tab,
 * with the sub-tabs Library and Backup. Bookmarked ?tab=backup links still
 * open the backups, and General no longer carries the File Manager card.
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

const BACKUP_NOTE = /Local backups include the MFA encryption key file/;

function openAt(url: string) {
  window.history.replaceState({}, '', url);
}

describe('SettingsPage — Files tab', () => {
  beforeEach(() => {
    localStorage.clear();
    setAuthToken(null);
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json(mockSettings)),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/notifications/', () => HttpResponse.json([])),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([])),
      http.get('/api/v1/mqtt/status', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/virtual-printer/status', () => HttpResponse.json({ running: false })),
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
      http.get('/api/v1/external-links/', () => HttpResponse.json([]))
    );
  });

  it('opens on the library, with the File Manager card', async () => {
    openAt('/settings?tab=files');
    render(<SettingsPage />);

    await waitFor(() => expect(document.getElementById('card-filemanager')).not.toBeNull());
    expect(screen.queryByText(BACKUP_NOTE)).not.toBeInTheDocument();
  });

  it('switches to the backups and back', async () => {
    const user = userEvent.setup();
    openAt('/settings?tab=files');
    render(<SettingsPage />);
    await waitFor(() => expect(document.getElementById('card-filemanager')).not.toBeNull());

    await user.click(screen.getAllByRole('button', { name: /^Backup/ })[0]);
    expect(await screen.findByText(BACKUP_NOTE)).toBeInTheDocument();
    expect(document.getElementById('card-filemanager')).toBeNull();
    expect(window.location.search).toContain('sub=backup');

    await user.click(screen.getByRole('button', { name: /^Library/ }));
    await waitFor(() => expect(document.getElementById('card-filemanager')).not.toBeNull());
  });

  it('still opens the backups from an old ?tab=backup link', async () => {
    openAt('/settings?tab=backup');
    render(<SettingsPage />);

    expect(await screen.findByText(BACKUP_NOTE)).toBeInTheDocument();
  });

  it('keeps the File Manager card off General', async () => {
    openAt('/settings');
    render(<SettingsPage />);

    await waitFor(() => expect(document.getElementById('card-archive')).not.toBeNull());
    expect(document.getElementById('card-filemanager')).toBeNull();
  });
});
