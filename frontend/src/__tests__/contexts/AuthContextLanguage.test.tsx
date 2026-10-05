/**
 * The UI language saved to the account follows the user to every device they
 * sign in on; without one the device keeps its own.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { AuthProvider, useAuth } from '../../contexts/AuthContext';
import { getAuthToken, setAuthToken } from '../../api/client';
import i18n from '../../i18n';

function createWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AuthProvider>{children}</AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    );
  };
}

function me(language: string | null) {
  return {
    id: 7,
    username: 'alice',
    role: 'user',
    is_active: true,
    is_admin: false,
    auth_source: 'local',
    groups: [],
    permissions: [],
    created_at: '2026-01-01T00:00:00Z',
    language,
  };
}

describe('account language', () => {
  beforeEach(async () => {
    setAuthToken(null);
    await i18n.changeLanguage('en');
  });

  afterEach(async () => {
    setAuthToken(null);
    await i18n.changeLanguage('en');
  });

  describe('signed in', () => {
    beforeEach(() => {
      server.use(
        http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: true, requires_setup: false })),
      );
      setAuthToken('session-token');
    });

    it('switches a new device to the language saved to the account', async () => {
      server.use(http.get('/api/v1/auth/me', () => HttpResponse.json(me('de'))));

      const { result } = renderHook(() => useAuth(), { wrapper: createWrapper() });

      await waitFor(() => expect(result.current.user?.username).toBe('alice'));
      await waitFor(() => expect(i18n.language).toBe('de'));
    });

    it('leaves the device language alone when the account has none', async () => {
      server.use(http.get('/api/v1/auth/me', () => HttpResponse.json(me(null))));

      const { result } = renderHook(() => useAuth(), { wrapper: createWrapper() });

      await waitFor(() => expect(result.current.user?.username).toBe('alice'));
      expect(i18n.language).toBe('en');
    });

    it('ignores a language the UI does not ship', async () => {
      server.use(http.get('/api/v1/auth/me', () => HttpResponse.json(me('xx'))));

      const { result } = renderHook(() => useAuth(), { wrapper: createWrapper() });

      await waitFor(() => expect(result.current.user?.username).toBe('alice'));
      expect(i18n.language).toBe('en');
    });

    it('saves a picked language to the account', async () => {
      let saved: unknown = null;
      server.use(
        http.get('/api/v1/auth/me', () => HttpResponse.json(me(null))),
        http.put('/api/v1/users/me/language', async ({ request }) => {
          saved = await request.json();
          return HttpResponse.json(me('sv'));
        }),
      );
      const { result } = renderHook(() => useAuth(), { wrapper: createWrapper() });
      await waitFor(() => expect(result.current.user?.username).toBe('alice'));

      await act(async () => {
        await result.current.saveLanguage('sv');
      });

      expect(saved).toEqual({ language: 'sv' });
      expect(i18n.language).toBe('sv');
      expect(result.current.user?.language).toBe('sv');
    });

    it('rejects when the account refuses, keeps the device switched and the session alive', async () => {
      server.use(
        http.get('/api/v1/auth/me', () => HttpResponse.json(me(null))),
        http.put('/api/v1/users/me/language', () =>
          HttpResponse.json({ detail: 'An API key has no account language' }, { status: 403 }),
        ),
      );
      const { result } = renderHook(() => useAuth(), { wrapper: createWrapper() });
      await waitFor(() => expect(result.current.user?.username).toBe('alice'));

      await act(async () => {
        await expect(result.current.saveLanguage('fr')).rejects.toThrow();
      });

      expect(i18n.language).toBe('fr');
      expect(getAuthToken()).toBe('session-token');
      expect(result.current.user?.username).toBe('alice');
    });
  });

  it('only switches the device when auth is off', async () => {
    const put = vi.fn(() => HttpResponse.json(me('de')));
    server.use(
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
      http.put('/api/v1/users/me/language', put),
    );
    const { result } = renderHook(() => useAuth(), { wrapper: createWrapper() });
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.saveLanguage('de');
    });

    expect(i18n.language).toBe('de');
    expect(put).not.toHaveBeenCalled();
  });
});
