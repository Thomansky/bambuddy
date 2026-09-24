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
import { QueryClient } from '@tanstack/react-query';
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

const libraryFile = (over: Record<string, unknown> = {}) => ({
  id: 1,
  filename: 'plate.3mf',
  file_path: '/library/plate.3mf',
  file_size: 1024,
  file_type: '3mf',
  folder_id: null,
  thumbnail_path: null,
  print_name: null,
  print_time_seconds: null,
  print_count: 0,
  duplicate_count: 0,
  created_at: '2026-01-01T00:00:00Z',
  ...over,
});

function mockLibrary(folders: unknown[], files: unknown[] = []) {
  server.use(
    http.get('/api/v1/library/folders', () => HttpResponse.json(folders)),
    http.get('/api/v1/library/files', () => HttpResponse.json(files)),
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

      // The content pane waits for the folders-first setting before it decides
      // what to draw, so the tile lands a tick after the tree does.
      await waitFor(() => expect(badges('A-0007').content.length).toBeGreaterThan(0));

      const { tree, content } = badges('A-0007');
      expect(tree).toHaveLength(1);
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

  describe('the number the dialog promises', () => {
    /** The client App.tsx actually builds. The default test client has no
     *  staleTime at all, so it refetches on every mount and would never see a
     *  stale preview. */
    const appLikeClient = () =>
      new QueryClient({ defaultOptions: { queries: { staleTime: 1000 * 60, retry: false } } });

    it('shows the next number, not the one the last folder consumed', async () => {
      let nextValue = 7;
      mockLibrary([]);
      server.use(
        http.get('/api/v1/number-series/', () =>
          HttpResponse.json([
            seriesRow({ next_value: nextValue, preview: `A-${String(nextValue).padStart(4, '0')}` }),
          ]),
        ),
        http.post('/api/v1/library/folders', () => {
          // The create consumes one, exactly as the backend does.
          nextValue += 1;
          return HttpResponse.json(folder({ name: '' }));
        }),
      );
      const user = userEvent.setup();
      render(<FileManagerPage />, { queryClient: appLikeClient() });

      await waitFor(() => expect(screen.getByText('New Folder')).toBeInTheDocument());
      await user.click(screen.getByText('New Folder'));
      expect(await screen.findByText('Next number: A-0007')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Create' }));

      // Straight back in, well inside the 60 s staleTime: the number on the
      // quote has to be the number this folder will actually get.
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Create' })).not.toBeInTheDocument());
      await user.click(screen.getByText('New Folder'));

      expect(await screen.findByText('Next number: A-0008')).toBeInTheDocument();
      expect(screen.queryByText('Next number: A-0007')).not.toBeInTheDocument();
    });
  });

  describe('folder pickers', () => {
    const numbered = [
      folder({ id: 1, name: '', number: 'A-0007' }),
      folder({ id: 2, name: '', number: 'A-0008' }),
    ];

    const openMoveDialog = async (user: ReturnType<typeof userEvent.setup>) => {
      await waitFor(() => expect(screen.getByText('plate.3mf')).toBeInTheDocument());
      await user.click(within(screen.getByTestId('library-filter-card')).getByText('Select All'));
      await user.click(within(screen.getByTestId('selection-actions')).getByText('Move'));
      return screen.getByTestId('move-folder-list');
    };

    it('names every folder in the move dialog and finds one by its number', async () => {
      mockLibrary(numbered, [libraryFile()]);
      const user = userEvent.setup();
      render(<FileManagerPage />);

      const list = await openMoveDialog(user);
      // Two orders, told apart by the only thing they carry.
      expect(within(list).getByText('A-0007')).toBeInTheDocument();
      expect(within(list).getByText('A-0008')).toBeInTheDocument();

      // …and reachable by typing the number off the quote.
      await user.type(screen.getByLabelText('Filter folders...'), 'A-0008');
      expect(within(list).getByText('A-0008')).toBeInTheDocument();
      expect(within(list).queryByText('A-0007')).not.toBeInTheDocument();
      expect(screen.queryByText('No folder matches this filter.')).not.toBeInTheDocument();
    });

    it('names every folder in the mobile selector', async () => {
      mockLibrary(numbered);
      render(<FileManagerPage />);

      await waitFor(() => expect(badges('A-0007').tree).toHaveLength(1));
      // The sidebar is `hidden lg:flex`, so below that breakpoint this select
      // is the only folder navigation there is.
      const options = screen.getAllByRole('option').map((o) => o.textContent?.trim());
      expect(options.some((label) => label?.includes('A-0007'))).toBe(true);
      expect(options.some((label) => label?.includes('A-0008'))).toBe(true);
    });
  });
});
