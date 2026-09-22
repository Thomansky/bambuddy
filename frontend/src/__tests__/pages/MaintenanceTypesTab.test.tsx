/**
 * The maintenance types tab (#3127): coverage per type, the printers panel
 * that assigns and unassigns, the deleted-types disclosure with Restore, and
 * the "Add type" form's action select limiting the printer picker.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { render } from '../utils';
import { MaintenancePage } from '../../pages/MaintenancePage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { MaintenanceStatus, MaintenanceType } from '../../api/client';

const H2S = 1;
const X1C = 2;
const PLATE_TYPE = 11;
const VISION_TYPE = 12;

function item(overrides: Partial<MaintenanceStatus>): MaintenanceStatus {
  return {
    id: 100,
    printer_id: H2S,
    printer_name: 'H2S',
    printer_model: 'H2S',
    maintenance_type_id: PLATE_TYPE,
    maintenance_type_name: 'Clean Build Plate',
    maintenance_type_icon: 'Square',
    maintenance_type_wiki_url: null,
    enabled: true,
    notifications_enabled: true,
    interval_hours: 25,
    interval_type: 'hours',
    current_hours: 10,
    hours_since_maintenance: 10,
    hours_until_due: 15,
    days_since_maintenance: null,
    days_until_due: null,
    is_due: false,
    is_warning: false,
    last_performed_at: null,
    action: null,
    action_options: null,
    action_available_options: null,
    trigger_mode: 'manual',
    schedule_days: null,
    schedule_time: null,
    schedule_next_at: null,
    reserve_before_schedule: true,
    current_run: null,
    last_run: null,
    ...overrides,
  };
}

const plateType: MaintenanceType = {
  id: PLATE_TYPE,
  name: 'Clean Build Plate',
  description: '',
  default_interval_hours: 25,
  interval_type: 'hours',
  icon: 'Square',
  wiki_url: null,
  is_system: true,
  action: null,
  created_at: '2026-01-01T00:00:00Z',
  printer_count: 1,
  eligible_count: 2,
  printer_ids: [H2S],
  eligible_printer_ids: [H2S, X1C],
};

const visionType: MaintenanceType = {
  ...plateType,
  id: VISION_TYPE,
  name: 'Vision Encoder Calibration',
  icon: 'ScanEye',
  default_interval_hours: 7,
  interval_type: 'days',
  action: 'motion_precision',
  printer_count: 1,
  eligible_count: 1,
  printer_ids: [H2S],
  eligible_printer_ids: [H2S],
};

const overview = [
  {
    printer_id: H2S,
    printer_name: 'H2S',
    printer_model: 'H2S',
    total_print_hours: 10,
    due_count: 0,
    warning_count: 0,
    maintenance_items: [item({}), item({ id: 101, maintenance_type_id: VISION_TYPE, maintenance_type_name: 'Vision Encoder Calibration', action: 'motion_precision' })],
    require_plate_clear: false,
    available_actions: ['calibration', 'motion_precision'],
  },
  {
    printer_id: X1C,
    printer_name: 'X1C',
    printer_model: 'X1C',
    total_print_hours: 4,
    due_count: 0,
    warning_count: 0,
    maintenance_items: [item({ id: 200, printer_id: X1C, printer_name: 'X1C', printer_model: 'X1C', enabled: false })],
    require_plate_clear: false,
    available_actions: ['calibration'],
  },
];

async function openSettingsTab() {
  render(<MaintenancePage />);
  fireEvent.click(await screen.findByRole('button', { name: 'Settings' }));
  return await screen.findByTestId(`type-coverage-${PLATE_TYPE}`);
}

describe('MaintenancePage types tab', () => {
  let assigned: string[];
  let patches: { id: string; body: unknown }[];
  let restored: string[];
  let created: unknown[];
  let deletedTypes: unknown[];

  beforeEach(() => {
    assigned = [];
    patches = [];
    restored = [];
    created = [];
    deletedTypes = [
      {
        id: 40,
        name: 'Printer Calibration',
        icon: 'Target',
        is_system: true,
        action: 'calibration',
        default_interval_hours: 100,
        interval_type: 'hours',
        deleted_at: '2026-09-01T10:00:00Z',
        item_count: 3,
      },
    ];
    server.use(
      http.get('/api/v1/maintenance/overview', () => HttpResponse.json(overview)),
      http.get('/api/v1/maintenance/types', () => HttpResponse.json([plateType, visionType])),
      http.get('/api/v1/maintenance/types/deleted', () => HttpResponse.json(deletedTypes)),
      http.post('/api/v1/maintenance/printers/:printerId/assign/:typeId', ({ params }) => {
        assigned.push(`${params.printerId}:${params.typeId}`);
        return HttpResponse.json(item({}));
      }),
      http.patch('/api/v1/maintenance/items/:id', async ({ request, params }) => {
        patches.push({ id: String(params.id), body: await request.json() });
        return HttpResponse.json(item({}));
      }),
      http.post('/api/v1/maintenance/types/:id/restore', ({ params }) => {
        restored.push(String(params.id));
        deletedTypes = [];
        return HttpResponse.json(plateType);
      }),
      http.post('/api/v1/maintenance/types', async ({ request }) => {
        created.push(await request.json());
        return HttpResponse.json(plateType);
      })
    );
  });

  describe('coverage', () => {
    it('says on how many of the eligible printers a type is in use', async () => {
      const coverage = await openSettingsTab();
      expect(coverage).toHaveTextContent('on 1 of 2 printers');
    });

    it('marks a partly rolled-out type amber and a complete one not', async () => {
      await openSettingsTab();
      expect(screen.getByTestId(`type-coverage-${PLATE_TYPE}`).className).toContain('amber');
      expect(screen.getByTestId(`type-coverage-${VISION_TYPE}`)).toHaveTextContent('on 1 of 1 printers');
      expect(screen.getByTestId(`type-coverage-${VISION_TYPE}`).className).not.toContain('amber');
    });

    it('badges an action type as automatic and a reminder type as manual', async () => {
      await openSettingsTab();
      const plateCard = screen.getByTestId(`type-coverage-${PLATE_TYPE}`).closest('div.rounded-xl')!;
      const visionCard = screen.getByTestId(`type-coverage-${VISION_TYPE}`).closest('div.rounded-xl')!;
      expect(within(plateCard as HTMLElement).getByText('Manual')).toBeInTheDocument();
      expect(within(visionCard as HTMLElement).getByText('Automatic')).toBeInTheDocument();
    });
  });

  describe('the printers panel', () => {
    it('offers one checkbox per eligible printer, ticked where the item is on', async () => {
      await openSettingsTab();
      const card = screen.getByTestId(`type-coverage-${PLATE_TYPE}`).closest('div.rounded-xl')!;
      fireEvent.click(within(card as HTMLElement).getByRole('button', { name: /Printers/ }));

      const panel = await screen.findByTestId(`type-printers-${PLATE_TYPE}`);
      expect(within(panel).getByRole('checkbox', { name: 'H2S' })).toBeChecked();
      expect(within(panel).getByRole('checkbox', { name: 'X1C' })).not.toBeChecked();
    });

    it('only lists the printers the type can apply to', async () => {
      await openSettingsTab();
      const card = screen.getByTestId(`type-coverage-${VISION_TYPE}`).closest('div.rounded-xl')!;
      fireEvent.click(within(card as HTMLElement).getByRole('button', { name: /Printers/ }));

      const panel = await screen.findByTestId(`type-printers-${VISION_TYPE}`);
      expect(within(panel).getByRole('checkbox', { name: 'H2S' })).toBeInTheDocument();
      expect(within(panel).queryByRole('checkbox', { name: 'X1C' })).not.toBeInTheDocument();
    });

    it('ticking a printer assigns the type to it', async () => {
      await openSettingsTab();
      const card = screen.getByTestId(`type-coverage-${PLATE_TYPE}`).closest('div.rounded-xl')!;
      fireEvent.click(within(card as HTMLElement).getByRole('button', { name: /Printers/ }));
      const panel = await screen.findByTestId(`type-printers-${PLATE_TYPE}`);

      fireEvent.click(within(panel).getByRole('checkbox', { name: 'X1C' }));
      await waitFor(() => expect(assigned).toEqual([`${X1C}:${PLATE_TYPE}`]));
      expect(patches).toEqual([]);
    });

    it('unticking a printer switches its item off instead of deleting it', async () => {
      await openSettingsTab();
      const card = screen.getByTestId(`type-coverage-${PLATE_TYPE}`).closest('div.rounded-xl')!;
      fireEvent.click(within(card as HTMLElement).getByRole('button', { name: /Printers/ }));
      const panel = await screen.findByTestId(`type-printers-${PLATE_TYPE}`);

      fireEvent.click(within(panel).getByRole('checkbox', { name: 'H2S' }));
      await waitFor(() => expect(patches).toEqual([{ id: '100', body: { enabled: false } }]));
      expect(assigned).toEqual([]);
    });
  });

  describe('deleted types', () => {
    it('lists a hidden type with when it went and how many items it kept', async () => {
      await openSettingsTab();
      fireEvent.click(screen.getByRole('button', { name: /Deleted types/ }));

      const row = await screen.findByTestId('deleted-type-40');
      expect(row).toHaveTextContent('Printer Calibration');
      expect(row).toHaveTextContent('3');
      expect(row).toHaveTextContent(/Deleted/);
    });

    it('restores it', async () => {
      await openSettingsTab();
      fireEvent.click(screen.getByRole('button', { name: /Deleted types/ }));
      const row = await screen.findByTestId('deleted-type-40');

      fireEvent.click(within(row).getByRole('button', { name: /Restore/ }));
      await waitFor(() => expect(restored).toEqual(['40']));
    });
  });

  describe('the add-type form', () => {
    it('limits the printer picker to the printers the chosen action can run on', async () => {
      await openSettingsTab();
      fireEvent.click(screen.getByRole('button', { name: /Add Custom Type/ }));

      // Without an action every printer is on offer.
      expect(await screen.findByRole('button', { name: 'X1C' })).toBeInTheDocument();

      fireEvent.change(screen.getByLabelText('Action'), { target: { value: 'motion_precision' } });
      await waitFor(() => expect(screen.queryByRole('button', { name: 'X1C' })).not.toBeInTheDocument());
      expect(screen.getByRole('button', { name: 'H2S' })).toBeInTheDocument();
    });

    it('creates the type with its action and its printers in one request', async () => {
      await openSettingsTab();
      fireEvent.click(screen.getByRole('button', { name: /Add Custom Type/ }));

      fireEvent.change(screen.getByPlaceholderText(/Replace HEPA Filter/), {
        target: { value: 'Monthly full calibration' },
      });
      fireEvent.change(screen.getByLabelText('Action'), { target: { value: 'calibration' } });
      fireEvent.click(screen.getByRole('button', { name: 'H2S' }));
      fireEvent.click(screen.getByRole('button', { name: 'Add Type' }));

      await waitFor(() => expect(created).toHaveLength(1));
      expect(created[0]).toMatchObject({
        name: 'Monthly full calibration',
        action: 'calibration',
        printer_ids: [H2S],
      });
    });

    it('drops a printer the newly chosen action cannot run on', async () => {
      await openSettingsTab();
      fireEvent.click(screen.getByRole('button', { name: /Add Custom Type/ }));

      fireEvent.change(screen.getByPlaceholderText(/Replace HEPA Filter/), { target: { value: 'Extra check' } });
      fireEvent.click(screen.getByRole('button', { name: 'X1C' }));
      fireEvent.change(screen.getByLabelText('Action'), { target: { value: 'motion_precision' } });
      fireEvent.click(screen.getByRole('button', { name: 'H2S' }));
      fireEvent.click(screen.getByRole('button', { name: 'Add Type' }));

      await waitFor(() => expect(created).toHaveLength(1));
      expect(created[0]).toMatchObject({ action: 'motion_precision', printer_ids: [H2S] });
    });
  });
});
