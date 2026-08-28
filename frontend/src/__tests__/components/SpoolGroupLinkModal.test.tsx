/**
 * Tests for SpoolGroupLinkModal (#2936).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SpoolGroupLinkModal } from '../../components/SpoolGroupLinkModal';
import { api } from '../../api/client';
import type { InventorySpool } from '../../api/client';

const mockShowToast = vi.fn();

vi.mock('../../api/client', () => ({
  api: {
    linkSpools: vi.fn(),
  },
}));

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: mockShowToast }),
}));

function spool(id: number, extra: Partial<InventorySpool> = {}): InventorySpool {
  return {
    id,
    material: 'PLA',
    subtype: 'Matte',
    brand: 'Bambu Lab',
    color_name: `Color ${id}`,
    ...extra,
  } as InventorySpool;
}

describe('SpoolGroupLinkModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.linkSpools as ReturnType<typeof vi.fn>).mockResolvedValue({ group_id: 7, linked: 3, updated: 2 });
  });

  it('links the selection with the chosen source and names the affected count', async () => {
    const onLinked = vi.fn();
    const user = userEvent.setup();
    render(
      <SpoolGroupLinkModal
        targetIds={[1, 2, 3]}
        candidates={[spool(1), spool(2), spool(3)]}
        onClose={() => {}}
        onLinked={onLinked}
      />,
    );

    await user.click(screen.getByRole('radio', { name: /#2/ }));
    // The confirmation warns how many records the source overwrites.
    expect(screen.getByText(/overwrite the filament data of 2 spool/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^Link$/ }));
    await waitFor(() => {
      expect(api.linkSpools).toHaveBeenCalledWith([1, 3], 2);
    });
    expect(onLinked).toHaveBeenCalled();
  });

  it('marks candidates that are already linked', () => {
    render(
      <SpoolGroupLinkModal
        targetIds={[1]}
        candidates={[spool(2, { filament_group_id: 9 }), spool(3)]}
        onClose={() => {}}
        onLinked={() => {}}
      />,
    );

    expect(screen.getByText('linked')).toBeInTheDocument();
  });

  it('disables the action until a source is picked', () => {
    render(
      <SpoolGroupLinkModal targetIds={[1, 2]} candidates={[spool(1), spool(2)]} onClose={() => {}} onLinked={() => {}} />,
    );
    expect(screen.getByRole('button', { name: /^Link$/ })).toBeDisabled();
  });
});
