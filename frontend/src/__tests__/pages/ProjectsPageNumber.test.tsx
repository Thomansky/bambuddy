/**
 * The running number on a project.
 *
 * The number is what a quote and an invoice are filed under, so it has to be
 * visible on the card and editable in the dialog — and an empty field on
 * create has to mean "let the series decide", not "no number".
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ProjectsPage, ProjectModal } from '../../pages/ProjectsPage';

const PROJECTS = [
  {
    id: 1,
    name: 'Bracket run',
    number: 'A-01125',
    description: 'Fifty brackets',
    color: '#00ae42',
    archive_count: 2,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
  },
  {
    // Predates the series — keeps its NULL, nothing is renumbered.
    id: 2,
    name: 'Old project',
    number: null,
    description: 'From before',
    color: '#ff5500',
    archive_count: 1,
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-02T00:00:00Z',
  },
];

describe('Project numbers', () => {
  beforeEach(() => {
    server.use(http.get('/api/v1/projects/', () => HttpResponse.json(PROJECTS)));
  });

  it('shows the number on the project card', async () => {
    render(<ProjectsPage />);

    await waitFor(() => expect(screen.getByText('Bracket run')).toBeInTheDocument());
    expect(screen.getByText('A-01125')).toBeInTheDocument();
  });

  it('leaves an unnumbered project alone', async () => {
    render(<ProjectsPage />);

    await waitFor(() => expect(screen.getByText('Old project')).toBeInTheDocument());
    expect(screen.queryByText('null')).not.toBeInTheDocument();
  });
});

describe('Project dialog', () => {
  const t = ((key: string) => {
    const strings: Record<string, string> = {
      'projects.newProject': 'New Project',
      'projects.editProject': 'Edit Project',
      'projects.number': 'Number',
      'projects.numberPlaceholder': 'Assigned automatically',
      'projects.namePlaceholder': 'e.g. Friday gifts',
      'projects.create': 'Create Project',
      'common.name': 'Name',
      'common.save': 'Save',
      'common.cancel': 'Cancel',
    };
    return strings[key] ?? key;
  }) as never;

  beforeEach(() => {
    server.use(http.get('/api/v1/projects/', () => HttpResponse.json(PROJECTS)));
  });

  it('omits the number on create so the series decides', async () => {
    const onSave = vi.fn();
    render(<ProjectModal onClose={vi.fn()} onSave={onSave} isLoading={false} currencySymbol="€" t={t} />);

    await userEvent.type(screen.getByPlaceholderText('e.g. Friday gifts'), 'Fresh project');
    await userEvent.click(screen.getByRole('button', { name: 'Create Project' }));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0].number).toBeUndefined();
  });

  it('sends a number the user typed on create', async () => {
    const onSave = vi.fn();
    render(<ProjectModal onClose={vi.fn()} onSave={onSave} isLoading={false} currencySymbol="€" t={t} />);

    await userEvent.type(screen.getByPlaceholderText('e.g. Friday gifts'), 'Fresh project');
    await userEvent.type(screen.getByLabelText('Number'), 'RMA-9');
    await userEvent.click(screen.getByRole('button', { name: 'Create Project' }));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0].number).toBe('RMA-9');
  });

  it('seeds the field from the project and clears with an explicit null', async () => {
    const onSave = vi.fn();
    render(
      <ProjectModal
        project={PROJECTS[0] as never}
        onClose={vi.fn()}
        onSave={onSave}
        isLoading={false}
        currencySymbol="€"
        t={t}
      />,
    );

    const field = screen.getByLabelText('Number');
    expect(field).toHaveValue('A-01125');

    await userEvent.clear(field);
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    // Null, not undefined: an omitted key would leave the stored number alone.
    expect(onSave.mock.calls[0][0].number).toBeNull();
  });
});
