/**
 * Tests for MsgPreviewModal.
 *
 * The fixture (../mocks/msgFixture) is a real CFB container carrying the MAPI
 * stream names Outlook uses, so the tests cover msgreader's actual parse path
 * — only the network fetch is stubbed. What a bundler does to that parse path
 * is out of reach here and is covered by ../utils/msgFileBundle.test.ts.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import * as CFB from 'cfb';
import { MsgPreviewModal } from '../../components/MsgPreviewModal';
import { rtfToText } from '../../utils/rtfToText';
import { buildMsgFixture, MSG_FIXTURE, MSG_RTF_BODY } from '../mocks/msgFixture';

vi.mock('../../api/client', () => ({
  api: {
    getLibraryFileDownloadUrl: vi.fn((id: number) => `http://test/library/files/${id}/download`),
  },
  getAuthToken: () => null,
}));

const mockOnClose = vi.fn();

function stubFetchWith(bytes: Uint8Array) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(bytes as unknown as BodyInit, { status: 200 })),
  );
}

function renderModal(props: Partial<Parameters<typeof MsgPreviewModal>[0]> = {}) {
  return render(
    <MsgPreviewModal
      libraryFileId={42}
      filename="order.msg"
      fileSize={4608}
      onClose={mockOnClose}
      {...props}
    />,
  );
}

describe('MsgPreviewModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders subject, sender, recipient, body and attachments from a real .msg', async () => {
    stubFetchWith(buildMsgFixture());
    renderModal();

    expect(await screen.findByText(MSG_FIXTURE.subject)).toBeInTheDocument();
    expect(screen.getByText(/Example Supplier <orders@example-supplier\.test>/)).toBeInTheDocument();
    expect(screen.getByText(/Thomas <thomas@example\.test>/)).toBeInTheDocument();
    expect(screen.getByText(/your filament order has shipped/)).toBeInTheDocument();
    expect(screen.getByText(MSG_FIXTURE.attachmentName)).toBeInTheDocument();
  });

  // The message preview is a preview like any other (#2976): same panel size
  // and the same fullscreen toggle as the PDF sitting next to it in a folder.
  it('renders inside the shared preview shell, fullscreen toggle included', async () => {
    stubFetchWith(buildMsgFixture());
    renderModal();

    const title = await screen.findByText('Order confirmation #4711');
    const panel = title.closest('.flex-col') as HTMLElement;
    expect(panel.className).toContain('w-[min(1800px,96vw)]');
    expect(panel.className).toContain('h-[94vh]');
    expect(screen.getByRole('button', { name: 'Fullscreen' })).toBeInTheDocument();
  });

  // jsdom has no Fullscreen API, so the hook takes its viewport-filling
  // fallback — which is what an iPhone gets as well.
  it('goes fullscreen on a double-click in the message body', async () => {
    stubFetchWith(buildMsgFixture());
    renderModal();

    const title = await screen.findByText('Order confirmation #4711');
    const panel = title.closest('.flex-col') as HTMLElement;
    fireEvent.doubleClick(screen.getByTestId('msg-preview-content'));

    expect(panel.className).toContain('max-w-none');
    expect(screen.getByRole('button', { name: 'Exit fullscreen' })).toBeInTheDocument();
  });

  it('shows an error message for a file that is not a CFB container', async () => {
    stubFetchWith(new TextEncoder().encode('this is not an outlook message'));
    renderModal();

    expect(await screen.findByText('This file cannot be previewed.')).toBeInTheDocument();
  });

  it('refuses oversized files without fetching them', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    renderModal({ fileSize: 200 * 1024 * 1024 });

    expect(await screen.findByText(/too large to preview/)).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('rtfToText', () => {
  it('converts plain RTF to text', () => {
    const rtf = String.raw`{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0\fs22 Hello\par second line\par}`;
    expect(rtfToText(rtf)).toBe('Hello\n second line');
  });

  it('strips tags from RTF-encapsulated HTML', () => {
    const rtf = String.raw`{\rtf1\ansi\fromhtml1 <html><body><p>Hello &amp; welcome</p></body></html>}`;
    expect(rtfToText(rtf)).toContain('Hello & welcome');
    expect(rtfToText(rtf)).not.toContain('<p>');
  });

  it('drops the \\* destination markers Outlook writes around every HTML tag', () => {
    const text = rtfToText(MSG_RTF_BODY);
    expect(text).toBe('Hello, your filament order has shipped.');
    expect(text).not.toContain(String.raw`\*`);
  });
});
