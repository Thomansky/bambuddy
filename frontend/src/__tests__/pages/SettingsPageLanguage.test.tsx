/**
 * The language picker saves to the account when signed in, so the language is
 * picked once rather than on every device. That is the user's own choice and
 * needs no settings permission; the server-wide language (what SpoolBuddy
 * kiosks show) still follows for whoever may change settings.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { SettingsPage } from '../../pages/SettingsPage';
import { setAuthToken } from '../../api/client';
import i18n from '../../i18n';

const mockSettings = {
  auto_archive: true,
  save_thumbnails: true,
  capture_finish_photo: true,
  default_filament_cost: 25.0,
  currency: 'USD',
  time_format: 'system',
  date_format: 'system',
  mqtt_enabled: false,
  spoolman_enabled: false,
  ha_enabled: false,
  check_updates: false,
  check_printer_firmware: false,
  language: 'en',
};

function me(permissions: string[]) {
  return {
    id: 7,
    username: 'alice',
    role: 'user',
    is_active: true,
    is_admin: false,
    auth_source: 'local',
    groups: [],
    permissions,
    created_at: '2026-01-01T00:00:00Z',
    language: null,
  };
}

describe('SettingsPage language picker', () => {
  let settingsPuts: Record<string, unknown>[];
  let accountPuts: unknown[];

  beforeEach(async () => {
    window.history.replaceState({}, '', '/');
    setAuthToken(null);
    await i18n.changeLanguage('en');
    settingsPuts = [];
    accountPuts = [];
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json(mockSettings)),
      http.put('/api/v1/settings/', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        settingsPuts.push(body);
        return HttpResponse.json({ ...mockSettings, ...body });
      }),
      http.put('/api/v1/users/me/language', async ({ request }) => {
        const body = (await request.json()) as { language: string };
        accountPuts.push(body);
        return HttpResponse.json({ ...me([]), language: body.language });
      }),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/notifications/', () => HttpResponse.json([])),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([])),
      http.get('/api/v1/mqtt/status', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/virtual-printer/status', () => HttpResponse.json({ running: false })),
      http.get('/api/v1/external-links/', () => HttpResponse.json([])),
    );
  });

  afterEach(async () => {
    setAuthToken(null);
    await i18n.changeLanguage('en');
  });

  function signIn(permissions: string[]) {
    server.use(
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: true, requires_setup: false })),
      http.get('/api/v1/auth/me', () => HttpResponse.json(me(permissions))),
    );
    setAuthToken('session-token');
  }

  async function pickGerman() {
    render(<SettingsPage />);
    // The hint only reads like this once the signed-in user has loaded; in
    // the app the protected route waits for that before the page shows.
    await screen.findByText('Saved to your account, so every device you sign in on uses it');
    await userEvent.setup().selectOptions(screen.getByDisplayValue('English (English)'), 'de');
  }

  it('saves to the account for a user who may not change settings', async () => {
    signIn(['settings:read']);

    await pickGerman();

    await waitFor(() => expect(accountPuts).toEqual([{ language: 'de' }]));
    expect(i18n.language).toBe('de');
    expect(settingsPuts.some((body) => 'language' in body)).toBe(false);
    expect(screen.queryByText('Sie haben keine Berechtigung, Einstellungen zu ändern')).not.toBeInTheDocument();
  });

  it('also moves the server-wide language for whoever may change settings', async () => {
    signIn(['settings:read', 'settings:update']);

    await pickGerman();

    await waitFor(() => expect(accountPuts).toEqual([{ language: 'de' }]));
    await waitFor(() => expect(settingsPuts).toContainEqual({ language: 'de' }));
  });

  it('tells the user when the account could not be updated', async () => {
    signIn(['settings:read']);
    server.use(
      http.put('/api/v1/users/me/language', () => HttpResponse.json({ detail: 'nope' }, { status: 500 })),
    );

    await pickGerman();

    expect(
      await screen.findByText(
        'Die Sprache wurde auf diesem Gerät umgestellt, konnte aber nicht in Ihrem Konto gespeichert werden',
      ),
    ).toBeInTheDocument();
  });

  it('keeps the server-wide language and no account call when auth is off', async () => {
    server.use(
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
    );
    const accountCall = vi.fn();
    server.use(http.put('/api/v1/users/me/language', accountCall));
    render(<SettingsPage />);
    expect(await screen.findByText('Select your preferred language')).toBeInTheDocument();

    await userEvent.setup().selectOptions(screen.getByDisplayValue('English (English)'), 'de');

    await waitFor(() => expect(settingsPuts).toContainEqual({ language: 'de' }));
    expect(accountCall).not.toHaveBeenCalled();
    expect(i18n.language).toBe('de');
  });
});
