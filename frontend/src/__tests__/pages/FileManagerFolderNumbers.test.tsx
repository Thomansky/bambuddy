/**
 * The order folder's running number in the File Manager.
 *
 * The number is filed on the folder while the enquiry is still an enquiry, so
 * it has to be visible wherever a folder is drawn and findable by typing it
 * into the search — looking a job up by the number on its quote is the whole
 * reason it exists. It is its own field, never part of the name, so a rename
 * cannot lose it.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FileManagerPage } from '../../pages/FileManagerPage';

const folder = (over: Record<string, unknown> = {}) => ({
  id: 1,
  name: 'Reindl',
  number: 'A-0007',
  parent_id: null,
  file_count: 0,
  project_id: null,
  archive_id: null,
  project_name: null,
  archive_name: null,
  is_external: false,
  external_path: null,
  external_readonly: false,
  latest_activity_at: null,
  children: [],
  ...over,
});

const seriesRow = (over: Record<string, unknown> = {}) => ({
  key: 'library_folder',
  enabled: true,
  prefix: 'A-',
  suffix: '',
  next_value: 7,
  padding: 4,
  updated_at: null,
  preview: 'A-0007',
  ...over,
});

function mockLibrary(folders: unknown[]) {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json(folders)),
    http.get('/api/v1/library/files', () => HttpResponse.json([])),
    http.get('/api/v1/library/stats', () =>
      HttpResponse.json({
        total_files: 0,
        total_folders: folders.length,
        total_size_bytes: 0,
        disk_free_bytes: 10737418240,
        disk_total_bytes: 107374182400,
      }),
    ),
  );
}

const sidebar = () => screen.getByTestId('folder-sidebar');

/** Every badge carrying `value`, split by whether it is in the tree or in the
 *  content pane. */
function badges(value: string) {
  const all = screen.queryAllByTestId('folder-number').filter((b) => b.textContent === value);
  return {
    tree: all.filter((b) => sidebar().contains(b)),
    content: all.filter((b) => !sidebar().contains(b)),
  };
}

describe('FileManager folder numbers', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  describe('rendering', () => {
    it('draws the number before the name in the tree and in a content tile', async () => {
      mockLibrary([folder()]);
      render(<FileManagerPage />);

      await waitFor(() => expect(within(sidebar()).getByText('Reindl')).toBeInTheDocument());

      const { tree, content } = badges('A-0007');
      expect(tree).toHaveLength(1);
      expect(content.length).toBeGreaterThan(0);
      // The number leads, the name follows — one element, not a prefix glued
      // into the name.
      for (const badge of [...tree, ...content]) {
        expect(badge.parentElement?.textContent).toBe('A-0007Reindl');
      }
    });

    it('leaves a folder from before the series was switched on unnumbered', async () => {
      mockLibrary([folder({ number: null, name: 'From last year' })]);
      render(<FileManagerPage />);

      await waitFor(() => expect(within(sidebar()).getByText('From last year')).toBeInTheDocument());
      expect(screen.queryAllByTestId('folder-number')).toHaveLength(0);
    });

    it('renders a folder that is only a number as just the number', async () => {
      mockLibrary([folder({ name: '' })]);
      render(<FileManagerPage />);

      await waitFor(() => expect(badges('A-0007').tree).toHaveLength(1));
      const [badge] = badges('A-0007').tree;
      expect(badge.parentElement?.textContent).toBe('A-0007');
      // Nameless is not unlabelled: the number stands in for the missing name.
      expect(badge.parentElement).toHaveAttribute('title', 'A-0007');
    });
  });

  describe('search', () => {
    it('finds a folder by its number', async () => {
      mockLibrary([folder(), folder({ id: 2, name: 'Scratch', number: null })]);
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(within(sidebar()).getByText('Reindl')).toBeInTheDocument());
      await user.type(screen.getByPlaceholderText('Search files...'), 'A-0007');

      const results = await screen.findByTestId('folder-search-results');
      expect(within(results).getByText('Reindl')).toBeInTheDocument();
      expect(within(results).queryByText('Scratch')).not.toBeInTheDocument();
    });

    it('still finds a folder by its name', async () => {
      mockLibrary([folder(), folder({ id: 2, name: 'Scratch', number: null })]);
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(within(sidebar()).getByText('Reindl')).toBeInTheDocument());
      await user.type(screen.getByPlaceholderText('Search files...'), 'scratch');

      const results = await screen.findByTestId('folder-search-results');
      expect(within(results).getByText('Scratch')).toBeInTheDocument();
      expect(within(results).queryByText('Reindl')).not.toBeInTheDocument();
    });
  });

  describe('new folder dialog', () => {
    const openDialog = async (user: ReturnType<typeof userEvent.setup>) => {
      await waitFor(() => expect(screen.getByText('New Folder')).toBeInTheDocument());
      await user.click(screen.getByText('New Folder'));
      return screen.getByRole('button', { name: 'Create' });
    };

    it('accepts an empty name when a number is assigned', async () => {
      const posted: Record<string, unknown>[] = [];
      mockLibrary([]);
      server.use(
        http.get('/api/v1/number-series/', () => HttpResponse.json([seriesRow()])),
        http.post('/api/v1/library/folders', async ({ request }) => {
          posted.push((await request.json()) as Record<string, unknown>);
          return HttpResponse.json(folder({ name: '' }));
        }),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      const create = await openDialog(user);
      // The checkbox is on by default and the dialog promises which number.
      await waitFor(() => expect(screen.getByLabelText('Assign a number')).toBeChecked());
      expect(screen.getByText('Next number: A-0007')).toBeInTheDocument();

      expect(create).toBeEnabled();
      await user.click(create);

      await waitFor(() => expect(posted).toHaveLength(1));
      expect(posted[0]).toMatchObject({ name: '', use_number_series: true });
    });

    it('rejects an empty name when no number is assigned', async () => {
      mockLibrary([]);
      server.use(http.get('/api/v1/number-series/', () => HttpResponse.json([seriesRow()])));
      const user = userEvent.setup();
      render(<FileManagerPage />);

      const create = await openDialog(user);
      await waitFor(() => expect(screen.getByLabelText('Assign a number')).toBeChecked());

      await user.click(screen.getByLabelText('Assign a number'));

      expect(create).toBeDisabled();
      await user.type(screen.getByPlaceholderText('e.g., Functional Parts'), 'Scratch');
      expect(create).toBeEnabled();
    });

    it('offers nothing and still requires a name while the series is off', async () => {
      mockLibrary([]);
      server.use(
        http.get('/api/v1/number-series/', () => HttpResponse.json([seriesRow({ enabled: false })])),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      const create = await openDialog(user);
      await waitFor(() => expect(create).toBeDisabled());
      expect(screen.queryByLabelText('Assign a number')).not.toBeInTheDocument();
    });
  });

  describe('rename dialog', () => {
    const openRename = async (user: ReturnType<typeof userEvent.setup>) => {
      await waitFor(() => expect(within(sidebar()).getByText('Reindl')).toBeInTheDocument());
      const row = within(sidebar()).getByText('Reindl').closest('div.group')!;
      const buttons = within(row).getAllByRole('button');
      await user.click(buttons[buttons.length - 1]);
      await user.click(within(row).getByRole('button', { name: 'Rename' }));
      return screen.getByRole('button', { name: 'Rename' });
    };

    it('keeps the number when only the name is edited', async () => {
      const sent: Record<string, unknown>[] = [];
      mockLibrary([folder()]);
      server.use(
        http.put('/api/v1/library/folders/:id', async ({ request }) => {
          sent.push((await request.json()) as Record<string, unknown>);
          return HttpResponse.json(folder({ name: 'Reindl GmbH' }));
        }),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      const save = await openRename(user);
      expect(screen.getByLabelText('Folder number')).toHaveValue('A-0007');

      await user.type(screen.getByDisplayValue('Reindl'), ' GmbH');
      await user.click(save);

      await waitFor(() => expect(sent).toHaveLength(1));
      expect(sent[0]).toEqual({ name: 'Reindl GmbH' });
      // Untouched means not sent: the backend reads a missing key as "leave it
      // alone" and a null as "clear it".
      expect(sent[0]).not.toHaveProperty('number');
    });

    it('edits the number on its own', async () => {
      const sent: Record<string, unknown>[] = [];
      mockLibrary([folder()]);
      server.use(
        http.put('/api/v1/library/folders/:id', async ({ request }) => {
          sent.push((await request.json()) as Record<string, unknown>);
          return HttpResponse.json(folder({ number: 'A-0008' }));
        }),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      const save = await openRename(user);
      await user.clear(screen.getByLabelText('Folder number'));
      await user.type(screen.getByLabelText('Folder number'), 'A-0008');
      await user.click(save);

      await waitFor(() => expect(sent).toHaveLength(1));
      // Only the field that changed is sent: an order folder may have no name
      // at all, and re-sending an empty one would be refused.
      expect(sent[0]).toEqual({ number: 'A-0008' });
    });

    it('gives a nameless folder a name without disturbing its number', async () => {
      const sent: Record<string, unknown>[] = [];
      mockLibrary([folder({ name: '' })]);
      server.use(
        http.put('/api/v1/library/folders/:id', async ({ request }) => {
          sent.push((await request.json()) as Record<string, unknown>);
          return HttpResponse.json(folder({ name: 'Reindl' }));
        }),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      await waitFor(() => expect(badges('A-0007').tree).toHaveLength(1));
      const row = badges('A-0007').tree[0].closest('div.group')!;
      const buttons = within(row).getAllByRole('button');
      await user.click(buttons[buttons.length - 1]);
      await user.click(within(row).getByRole('button', { name: 'Rename' }));

      await user.type(screen.getByLabelText('Name'), 'Reindl');
      await user.click(screen.getByRole('button', { name: 'Rename' }));

      await waitFor(() => expect(sent).toHaveLength(1));
      expect(sent[0]).toEqual({ name: 'Reindl' });
    });

    it('says so and stays open when the number is already taken', async () => {
      mockLibrary([folder()]);
      server.use(
        http.put('/api/v1/library/folders/:id', () =>
          HttpResponse.json({ detail: "Folder number 'A-0008' is already in use" }, { status: 409 }),
        ),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />);

      const save = await openRename(user);
      await user.clear(screen.getByLabelText('Folder number'));
      await user.type(screen.getByLabelText('Folder number'), 'A-0008');
      await user.click(save);

      expect(
        await screen.findByText('That number is already used by another folder.'),
      ).toBeInTheDocument();
      // The dialog survives so the number can be corrected.
      expect(screen.getByLabelText('Folder number')).toHaveValue('A-0008');
    });
  });
});
