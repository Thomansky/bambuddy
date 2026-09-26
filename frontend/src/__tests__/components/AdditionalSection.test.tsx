import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { AdditionalSection } from '../../components/spool-form/AdditionalSection';
import { defaultFormData } from '../../components/spool-form/types';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const baseProps = {
  formData: defaultFormData,
  updateField: vi.fn(),
  spoolCatalog: [],
  currencySymbol: '$',
  availableCategories: [],
  availableMaterialNumbers: [],
  globalLowStockThreshold: 20,
};

describe('AdditionalSection', () => {
  it('renders SpoolWeightPicker when spoolmanMode is false', () => {
    render(<AdditionalSection {...baseProps} spoolmanMode={false} />);
    // SpoolWeightPicker renders the 'inventory.coreWeight' label
    expect(screen.getByText('inventory.coreWeight')).toBeTruthy();
    // Info notice must NOT be present
    expect(screen.queryByText('inventory.spoolWeightManagedBySpoolman')).toBeNull();
  });

  it('hides SpoolWeightPicker and shows info notice when spoolmanMode is true', () => {
    render(<AdditionalSection {...baseProps} spoolmanMode={true} />);
    // Info notice must appear
    expect(screen.getByText('inventory.spoolWeightManagedBySpoolman')).toBeTruthy();
    // SpoolWeightPicker must NOT be rendered
    expect(screen.queryByText('inventory.coreWeight')).toBeNull();
  });

  it('defaults to spoolmanMode=false when prop is omitted', () => {
    render(<AdditionalSection {...baseProps} />);
    // SpoolWeightPicker present by default
    expect(screen.getByText('inventory.coreWeight')).toBeTruthy();
  });

  it('renders the material number field in internal mode (#2870)', () => {
    render(<AdditionalSection {...baseProps} spoolmanMode={false} />);
    expect(screen.getByText('inventory.materialNumber')).toBeTruthy();
  });

  it('hides the material number field in Spoolman mode (#2870)', () => {
    // In Spoolman mode the number is the filament-level article_number,
    // maintained in Spoolman itself — the form must not offer an input
    // whose value would be silently dropped.
    render(<AdditionalSection {...baseProps} spoolmanMode={true} />);
    expect(screen.queryByText('inventory.materialNumber')).toBeNull();
  });

  describe('material number from the series', () => {
    // The number names a product; a spool of a known product inherits it. So
    // the series is a deliberate click for a product that has none, and the
    // button only exists while the series is switched on.
    const series = (enabled: boolean) => [
      { key: 'material', enabled, prefix: '', suffix: '', next_value: 77, padding: 0 },
    ];

    it('offers the next free number while the series is on, and fills it in', async () => {
      const updateField = vi.fn();
      server.use(
        http.get('/api/v1/number-series/', () => HttpResponse.json(series(true))),
        http.post('/api/v1/inventory/material-numbers/next', () => HttpResponse.json({ number: '77' })),
      );
      render(<AdditionalSection {...baseProps} updateField={updateField} spoolmanMode={false} />);

      const button = await screen.findByRole('button', { name: /inventory.materialNumberNext/ });
      await userEvent.click(button);

      await waitFor(() => expect(updateField).toHaveBeenCalledWith('material_number', '77'));
    });

    it('stays out of the way while the series is off', async () => {
      server.use(http.get('/api/v1/number-series/', () => HttpResponse.json(series(false))));
      render(<AdditionalSection {...baseProps} spoolmanMode={false} />);

      // Give the series query a chance to land before asserting absence.
      await screen.findByText('inventory.materialNumber');
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.queryByRole('button', { name: /inventory.materialNumberNext/ })).toBeNull();
    });
  });

  it('offers the VAT basis next to the cost and reports a change', async () => {
    const updateField = vi.fn();
    render(<AdditionalSection {...baseProps} updateField={updateField} spoolmanMode={false} vatEnabled={true} />);

    const select = screen.getByLabelText('inventory.vatBasis') as HTMLSelectElement;
    // defaultFormData enters prices including VAT (gross).
    expect(select.value).toBe('incl');

    const { fireEvent } = await import('@testing-library/react');
    fireEvent.change(select, { target: { value: 'excl' } });
    expect(updateField).toHaveBeenCalledWith('cost_vat_included', false);
  });

  it('hides the VAT basis while the vat_enabled setting is off (the default)', () => {
    render(<AdditionalSection {...baseProps} spoolmanMode={false} />);
    expect(screen.queryByLabelText('inventory.vatBasis')).toBeNull();
  });

  it('hides the VAT basis in Spoolman mode (Spoolman owns the price)', () => {
    render(<AdditionalSection {...baseProps} spoolmanMode={true} vatEnabled={true} />);
    expect(screen.queryByLabelText('inventory.vatBasis')).toBeNull();
  });
});
