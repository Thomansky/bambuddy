/**
 * Manual versus automated (#3127): the badge on each status card, the
 * per-printer count next to the due chips, and — where the distinction
 * actually has to be readable — the two sections of the types tab.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { render } from '../utils';
import { MaintenancePage } from '../../pages/MaintenancePage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { MaintenanceStatus } from '../../api/client';

function item(overrides: Partial<MaintenanceStatus>): MaintenanceStatus {
  return {
    id: 1,
    printer_id: 1,
    printer_name: 'H2S',
    printer_model: 'H2S',
    maintenance_type_id: 1,
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

const scheduled = item({
  id: 2,
  maintenance_type_id: 2,
  maintenance_type_name: 'Printer Calibration',
  maintenance_type_icon: 'Target',
  action: 'calibration',
  action_options: { bed_leveling: true },
  action_available_options: ['bed_leveling'],
  trigger_mode: 'schedule',
  schedule_days: [5],
  schedule_time: '06:00',
});

const onRequest = item({
  id: 3,
  maintenance_type_id: 3,
  maintenance_type_name: 'Vision Encoder Calibration',
  maintenance_type_icon: 'ScanEye',
  action: 'motion_precision',
  trigger_mode: 'manual',
});

const reminder = item({ id: 4, maintenance_type_id: 4, maintenance_type_name: 'Check PTFE Tube' });
const switchedOff = item({
  id: 5,
  maintenance_type_id: 5,
  maintenance_type_name: 'Check Belt Tension',
  enabled: false,
});

const overview = [
  {
    printer_id: 1,
    printer_name: 'H2S',
    printer_model: 'H2S',
    total_print_hours: 10,
    due_count: 0,
    warning_count: 0,
    maintenance_items: [item({}), scheduled, onRequest, reminder, switchedOff],
    require_plate_clear: false,
    available_actions: ['calibration', 'motion_precision'],
  },
];

async function expandPrinter() {
  render(<MaintenancePage />);
  fireEvent.click(await screen.findByRole('button', { name: /expand/i }));
  return await screen.findByText('Printer Calibration');
}

function cardFor(name: string) {
  return screen.getByText(name).closest('div.rounded-xl') as HTMLElement;
}

describe('MaintenancePage manual vs automated', () => {
  beforeEach(() => {
    // setup.ts stubs localStorage with plain spies, so the stored choice is
    // whatever getItem is told to answer.
    vi.mocked(localStorage.getItem).mockReturnValue(null);
    vi.mocked(localStorage.setItem).mockClear();
    server.use(
      http.get('/api/v1/maintenance/overview', () => HttpResponse.json(overview)),
      http.get('/api/v1/maintenance/types', () => HttpResponse.json([])),
      http.get('/api/v1/maintenance/types/deleted', () => HttpResponse.json([]))
    );
  });

  describe('the badge', () => {
    it('reads Automatic on an action item with a schedule', async () => {
      await expandPrinter();
      expect(within(cardFor('Printer Calibration')).getByText('Automatic')).toBeInTheDocument();
    });

    it('reads "Runs on request" on an action item left on the manual trigger', async () => {
      await expandPrinter();
      expect(
        within(cardFor('Vision Encoder Calibration')).getByText('Runs on request')
      ).toBeInTheDocument();
    });

    it('reads Manual on a reminder item', async () => {
      await expandPrinter();
      expect(within(cardFor('Check PTFE Tube')).getByText('Manual')).toBeInTheDocument();
    });
  });

  describe('the per-printer count', () => {
    it('counts the enabled items, an on-request one among the automatic', async () => {
      render(<MaintenancePage />);
      // Two action items, two reminders; the switched-off one counts for neither.
      expect(await screen.findByTestId('kind-counts-1')).toHaveTextContent('2 automatic, 2 manual');
    });
  });

  describe('the status tab', () => {
    it('lists the automatic and the manual items together, with no kind filter', async () => {
      // The filter chips are gone: the distinction is made where the types are
      // set up, not by hiding half the printer's list.
      await expandPrinter();

      expect(screen.getByText('Printer Calibration')).toBeInTheDocument();
      expect(screen.getByText('Check PTFE Tube')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Automatic' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Manual' })).not.toBeInTheDocument();
    });
  });

  describe('the switched-off items', () => {
    it('leaves a switched-off item out of the items in use', async () => {
      await expandPrinter();
      expect(screen.queryByText('Check Belt Tension')).not.toBeInTheDocument();
    });

    it('keeps it one disclosure away, with the toggle that switches it back on', async () => {
      const patches: { id: string; body: unknown }[] = [];
      server.use(
        http.patch('/api/v1/maintenance/items/:id', async ({ request, params }) => {
          patches.push({ id: String(params.id), body: await request.json() });
          return HttpResponse.json({});
        })
      );
      await expandPrinter();

      fireEvent.click(screen.getByTestId('switched-off-1'));
      const card = cardFor('Check Belt Tension');
      expect(within(card).getByText('Disabled')).toBeInTheDocument();

      fireEvent.click(within(card).getByRole('switch'));
      await waitFor(() =>
        expect(patches).toEqual([{ id: '5', body: { enabled: true } }])
      );
    });

    it('says the items are switched off rather than filtered when none is on', async () => {
      server.use(
        http.get('/api/v1/maintenance/overview', () =>
          HttpResponse.json([
            { ...overview[0], maintenance_items: [item({ enabled: false }), switchedOff] },
          ])
        )
      );
      render(<MaintenancePage />);
      fireEvent.click(await screen.findByRole('button', { name: /expand/i }));

      expect(
        await screen.findByText('Every item on this printer is switched off')
      ).toBeInTheDocument();
      expect(screen.queryByText('No items match this filter')).not.toBeInTheDocument();
      expect(screen.getByTestId('switched-off-1')).toHaveTextContent('2 switched off');
    });
  });
});
