import { act, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UpdatedStreamOverlay } from '../../components/UpdatedStreamOverlay';

const props = {
  size: 'medium' as const, camera: null, name: 'Workshop', model: null,
  filename: null, status: null, state: null, progress: null, layers: null,
  remaining: null, eta: null, temperatures: [],
};

afterEach(() => vi.restoreAllMocks());

describe('Version 2 orientation', () => {
  it('follows viewport changes for legacy URLs and removes its listener on unmount', () => {
    let change: ((event: { matches: boolean }) => void) | undefined;
    const removeEventListener = vi.fn();
    vi.spyOn(window, 'matchMedia').mockReturnValue({
      matches: true,
      addEventListener: (_: string, listener: typeof change) => { change = listener; },
      removeEventListener,
    } as unknown as MediaQueryList);
    const view = render(<UpdatedStreamOverlay {...props} />);
    const overlay = view.container.firstElementChild;
    expect(overlay).toHaveAttribute('data-layout', 'portrait');
    act(() => change?.({ matches: false }));
    expect(overlay).toHaveAttribute('data-layout', 'landscape');
    act(() => change?.({ matches: true }));
    expect(overlay).toHaveAttribute('data-layout', 'portrait');
    view.unmount();
    expect(removeEventListener).toHaveBeenCalledWith('change', change);
  });

  it.each(['landscape', 'portrait'] as const)('keeps explicit %s independent of viewport orientation', (layout) => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: layout !== 'portrait' } as MediaQueryList);
    const view = render(<UpdatedStreamOverlay {...props} layout={layout} />);
    expect(view.container.firstElementChild).toHaveAttribute('data-layout', layout);
  });
});
