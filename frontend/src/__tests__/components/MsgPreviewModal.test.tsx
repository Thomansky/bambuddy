/**
 * Tests for MsgPreviewModal.
 *
 * The fixture (../mocks/msgFixture) is a real CFB container carrying the MAPI
 * stream names Outlook uses, so the tests cover msgreader's actual parse path
 * — only the network fetch is stubbed. What a bundler does to that parse path
 * is out of reach here and is covered by ../utils/msgFileBundle.test.ts.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MsgPreviewModal } from '../../components/MsgPreviewModal';
import { rtfToText } from '../../utils/rtfToText';
import { buildMsgFixture, MSG_FIXTURE } from '../mocks/msgFixture';

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
});
