/**
 * Tests for MsgPreviewModal.
 *
 * The fixture is a real CFB container built with the `cfb` package and the
 * MAPI stream names Outlook uses, so the tests cover msgreader's actual
 * parse path — only the network fetch is stubbed.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import * as CFB from 'cfb';
import { MsgPreviewModal } from '../../components/MsgPreviewModal';
import { rtfToText } from '../../utils/rtfToText';

vi.mock('../../api/client', () => ({
  api: {
    getLibraryFileDownloadUrl: vi.fn((id: number) => `http://test/library/files/${id}/download`),
  },
  getAuthToken: () => null,
}));

const mockOnClose = vi.fn();

function utf16(str: string): Uint8Array {
  const bytes = new Uint8Array(str.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < str.length; i++) view.setUint16(i * 2, str.charCodeAt(i), true);
  return bytes;
}

function buildMsg(): Uint8Array {
  const container = CFB.utils.cfb_new();
  const add = (path: string, content: Uint8Array) => CFB.utils.cfb_add(container, path, content);
  add('/__substg1.0_0037001F', utf16('Order confirmation #4711'));
  add('/__substg1.0_1000001F', utf16('Hello,\n\nyour filament order has shipped.'));
  add('/__substg1.0_0C1A001F', utf16('Example Supplier'));
  add('/__substg1.0_0C1F001F', utf16('orders@example-supplier.test'));
  add('/__recip_version1.0_#00000000/__substg1.0_3001001F', utf16('Thomas'));
  add('/__recip_version1.0_#00000000/__substg1.0_39FE001F', utf16('thomas@example.test'));
  add('/__attach_version1.0_#00000000/__substg1.0_3707001F', utf16('invoice-4711.pdf'));
  add('/__attach_version1.0_#00000000/__substg1.0_37010102', new TextEncoder().encode('%PDF-1.4 fake'));
  return new Uint8Array(CFB.write(container, { type: 'buffer' }) as Buffer);
}

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
    stubFetchWith(buildMsg());
    renderModal();

    expect(await screen.findByText('Order confirmation #4711')).toBeInTheDocument();
    expect(screen.getByText(/Example Supplier <orders@example-supplier\.test>/)).toBeInTheDocument();
    expect(screen.getByText(/Thomas <thomas@example\.test>/)).toBeInTheDocument();
    expect(screen.getByText(/your filament order has shipped/)).toBeInTheDocument();
    expect(screen.getByText('invoice-4711.pdf')).toBeInTheDocument();
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
