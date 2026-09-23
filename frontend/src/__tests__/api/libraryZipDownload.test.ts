/**
 * Tests for the library bulk-ZIP download helpers.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { api, ApiError, setAuthToken } from '../../api/client';

const server = setupServer();

// A minimal but real ZIP body (empty end-of-central-directory record), so the
// helpers move actual bytes rather than a string the browser would never get.
const EMPTY_ZIP = new Uint8Array([0x50, 0x4b, 0x05, 0x06, ...new Array(18).fill(0)]);

function zipResponse(filename?: string) {
  const headers: Record<string, string> = { 'Content-Type': 'application/zip' };
  if (filename) {
    headers['Content-Disposition'] =
      `attachment; filename="${filename}"; filename*=UTF-8''${encodeURIComponent(filename)}`;
  }
  return new HttpResponse(EMPTY_ZIP, { headers });
}

let clicked: { href: string; download: string } | null = null;
let revoked: string[] = [];

// `onUnhandledRequest: 'error'` is the point of these tests: a helper that
// builds the wrong URL fails here instead of silently passing.
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterAll(() => server.close());

beforeEach(() => {
  clicked = null;
  revoked = [];
  (URL as unknown as { createObjectURL: unknown }).createObjectURL = vi.fn().mockReturnValue('blob:zip');
  (URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn((url: string) => {
    revoked.push(url);
  });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clicked = { href: this.href, download: this.download };
  });
});

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
  setAuthToken(null);
});

describe('downloadLibraryFilesZip', () => {
  it('posts the requested ids and saves the archive under the served name', async () => {
    let body: unknown;
    server.use(
      http.post('/api/v1/library/files/download-zip', async ({ request }) => {
        body = await request.json();
        return zipResponse('bambuddy-files-2026-09-23.zip');
      }),
    );

    await api.downloadLibraryFilesZip([7, 9]);

    expect(body).toEqual({ file_ids: [7, 9] });
    expect(clicked).toEqual({ href: 'blob:zip', download: 'bambuddy-files-2026-09-23.zip' });
    expect(revoked).toEqual(['blob:zip']);
  });

  it('sends the auth token', async () => {
    let authorization: string | null = null;
    server.use(
      http.post('/api/v1/library/files/download-zip', ({ request }) => {
        authorization = request.headers.get('Authorization');
        return zipResponse('bundle.zip');
      }),
    );
    setAuthToken('zip-token');

    await api.downloadLibraryFilesZip([1]);

    expect(authorization).toBe('Bearer zip-token');
  });

  it('falls back to a generic name when the server sends no disposition', async () => {
    server.use(http.post('/api/v1/library/files/download-zip', () => zipResponse()));

    await api.downloadLibraryFilesZip([1]);

    expect(clicked?.download).toBe('bambuddy-files.zip');
  });

  it('surfaces the cap refusal verbatim, with its status', async () => {
    const detail = 'Too many files for one ZIP: 3 requested, limit is 2.';
    server.use(
      http.post('/api/v1/library/files/download-zip', () => HttpResponse.json({ detail }, { status: 413 })),
    );

    const error = await api.downloadLibraryFilesZip([1, 2, 3]).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(413);
    expect((error as ApiError).message).toBe(detail);
    expect(clicked).toBeNull();
  });
});

describe('downloadLibraryFolderZip', () => {
  it('asks for the whole subtree by default', async () => {
    let requested: string | null = null;
    server.use(
      http.get('/api/v1/library/folders/:folderId/download-zip', ({ request }) => {
        requested = request.url;
        return zipResponse('RAFI-2026-09-23.zip');
      }),
    );

    await api.downloadLibraryFolderZip(12);

    expect(requested).toContain('/api/v1/library/folders/12/download-zip');
    expect(requested).toContain('recursive=true');
    expect(clicked?.download).toBe('RAFI-2026-09-23.zip');
  });

  it('can ask for the top level only', async () => {
    let requested: string | null = null;
    server.use(
      http.get('/api/v1/library/folders/:folderId/download-zip', ({ request }) => {
        requested = request.url;
        return zipResponse('RAFI-2026-09-23.zip');
      }),
    );

    await api.downloadLibraryFolderZip(12, false);

    expect(requested).toContain('recursive=false');
  });

  it('falls back to a folder-scoped name when the server sends no disposition', async () => {
    server.use(http.get('/api/v1/library/folders/:folderId/download-zip', () => zipResponse()));

    await api.downloadLibraryFolderZip(12);

    expect(clicked?.download).toBe('folder_12.zip');
  });

  it('surfaces a server error', async () => {
    server.use(
      http.get('/api/v1/library/folders/:folderId/download-zip', () =>
        HttpResponse.json({ detail: 'No downloadable files in this folder' }, { status: 404 }),
      ),
    );

    const error = await api.downloadLibraryFolderZip(12).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).message).toBe('No downloadable files in this folder');
    expect(clicked).toBeNull();
  });
});
