/**
 * Assigning several archives to a project at once: the list shows the tree,
 * each sub-project under its parent with its running number and what it is
 * part of, and the search finds a project by number as well as by name.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { BatchProjectModal } from '../../components/BatchProjectModal';

const project = (id: number, name: string, number: string | null, parent_id: number | null) => ({
  id,
  name,
  number,
  parent_id,
  color: '#00ae42',
  status: 'active',
  archive_count: 0,
});

describe('BatchProjectModal', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/projects/', () =>
        HttpResponse.json([
          project(1, 'Zubehör', null, null),
          project(5, 'RAFI', '4019', 6),
          project(6, 'RAFI Group', null, null),
          project(7, 'Armpolster', '4020', 6),
          project(8, 'Halterung', '4011', null),
          project(9, 'Spare', null, null),
        ]),
      ),
    );
  });

  const rows = () =>
    screen
      .getAllByRole('button')
      .filter((b) => b.textContent?.includes('archives'))
      .map((b) => b.textContent);

  it('lists each sub-project under its parent with its number and what it is part of', async () => {
    render(<BatchProjectModal selectedIds={[1, 2]} onClose={vi.fn()} />);

    const sub = (await screen.findByText('RAFI')).closest('button')!;
    expect(within(sub).getByText('4019')).toBeInTheDocument();
    expect(within(sub).getByText('Part of RAFI Group')).toBeInTheDocument();
    expect(rows().map((text) => text?.split('0 archives')[0])).toEqual([
      '4011Halterung',
      'RAFI Group',
      '4019RAFIPart of RAFI Group',
      '4020ArmpolsterPart of RAFI Group',
      'Spare',
      'Zubehör',
    ]);
  });

  it('finds a project by its number', async () => {
    const user = userEvent.setup();
    render(<BatchProjectModal selectedIds={[1]} onClose={vi.fn()} />);
    await screen.findByText('Armpolster');

    await user.type(screen.getByPlaceholderText('Search projects…'), '4020');

    expect(rows()).toHaveLength(1);
    expect(screen.getByText('Armpolster')).toBeInTheDocument();
    expect(screen.queryByText('Halterung')).not.toBeInTheDocument();
  });

  it('finds the sub-projects by the name of their parent', async () => {
    const user = userEvent.setup();
    render(<BatchProjectModal selectedIds={[1]} onClose={vi.fn()} />);
    await screen.findByText('Armpolster');

    await user.type(screen.getByPlaceholderText('Search projects…'), 'rafi group');

    expect(rows()).toHaveLength(3);
  });
});
