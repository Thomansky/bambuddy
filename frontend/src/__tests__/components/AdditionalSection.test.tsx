import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { screen } from '@testing-library/react';
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
