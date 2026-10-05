import type { ProjectListItem } from '../api/client';

/**
 * Projects that may legally become `projectId`'s parent (#1264).
 *
 * Its own descendants are excluded as well as itself: nesting a project under
 * something already beneath it makes a cycle, which the API rejects anyway, so
 * offering it would only produce an error the user cannot act on. Walked from
 * the flat list rather than fetched, since every row carries its `parent_id`.
 */
export function eligibleParents(
  projects: ProjectListItem[],
  projectId: number | undefined,
): ProjectListItem[] {
  if (projectId === undefined) return projects;
  const blocked = new Set([projectId]);
  // Repeat until nothing new is blocked: the list is in no particular order, so
  // a grandchild can appear before its parent has been blocked.
  let grew = true;
  while (grew) {
    grew = false;
    for (const candidate of projects) {
      if (candidate.parent_id !== null && blocked.has(candidate.parent_id) && !blocked.has(candidate.id)) {
        blocked.add(candidate.id);
        grew = true;
      }
    }
  }
  return projects.filter((p) => !blocked.has(p.id));
}

/**
 * Projects a picker should offer when filing something away (#2888).
 *
 * An archived project is one its owner has explicitly put out of the way, so
 * leaving it in a picker only lengthens a list they then have to search --
 * the reporter had five active projects behind thirty-odd finished ones.
 * Completed projects stay: filing a reprint against a finished project is
 * ordinary, and "completed" says the work is done, not that it should be
 * hidden.
 *
 * `keepId` names one project that survives whatever its status -- the one the
 * thing being edited already belongs to. Without it a controlled `<select>`
 * holds a value no option matches, and the browser resets it to the first
 * option, which here is "No project": an archive filed in an archived project
 * would state, in as many words, that it is filed nowhere.
 */
export function assignableProjects(
  projects: ProjectListItem[],
  keepId?: number | null,
): ProjectListItem[] {
  return projects.filter((p) => p.status !== 'archived' || p.id === keepId);
}

type TreeProject = Pick<ProjectListItem, 'id' | 'name' | 'number' | 'parent_id'>;

/** Running number and name as one string, number first the way the project
 *  card draws it ("4019 RAFI"), for places that only take text. */
export function projectText(project: { name: string; number?: string | null }): string {
  return [project.number, project.name].filter(Boolean).join(' ');
}

export interface ProjectChoice<T extends TreeProject> {
  project: T;
  /** 0 for a main project, 1 for its sub-projects, and so on. */
  depth: number;
  /** "4019 RAFI" */
  text: string;
  /** "RAFI Group › 4019 RAFI": the project with what it belongs to. */
  path: string;
  /** "RAFI Group", or null for a main project. */
  parentPath: string | null;
}

const byText = (a: TreeProject, b: TreeProject) =>
  projectText(a).localeCompare(projectText(b), undefined, { numeric: true, sensitivity: 'base' });

/**
 * Projects in tree order for a picker: each one followed by its sub-projects,
 * siblings sorted by number and name. Sorted by name alone, a sub-project sat
 * anywhere in the list, with nothing saying it was one or carrying the number
 * it is filed under.
 *
 * `all` is where parents are looked up. A picker usually offers fewer projects
 * than exist (archived ones left out); a sub-project whose parent is not on
 * offer still names it in its path and takes its place among the main ones.
 */
export function projectChoices<T extends TreeProject>(
  projects: T[],
  all: TreeProject[] = projects,
): ProjectChoice<T>[] {
  const byId = new Map<number, TreeProject>(all.map((p) => [p.id, p]));
  for (const p of projects) byId.set(p.id, p);
  const offered = new Set(projects.map((p) => p.id));

  // Texts from the main project down to this one. `seen` stops a cycle, which
  // the API refuses to create but which a picker must not hang on.
  const pathOf = (project: TreeProject): string[] => {
    const parts: string[] = [];
    const seen = new Set<number>();
    let current: TreeProject | undefined = project;
    while (current && !seen.has(current.id)) {
      seen.add(current.id);
      parts.unshift(projectText(current));
      current = current.parent_id != null ? byId.get(current.parent_id) : undefined;
    }
    return parts;
  };

  const children = new Map<number | null, T[]>();
  for (const p of projects) {
    const key = p.parent_id != null && offered.has(p.parent_id) ? p.parent_id : null;
    children.set(key, [...(children.get(key) ?? []), p]);
  }

  const choices: ProjectChoice<T>[] = [];
  const placed = new Set<number>();
  const place = (project: T, depth: number) => {
    if (placed.has(project.id)) return;
    placed.add(project.id);
    const parts = pathOf(project);
    choices.push({
      project,
      depth,
      text: projectText(project),
      path: parts.join(' › '),
      parentPath: parts.length > 1 ? parts.slice(0, -1).join(' › ') : null,
    });
    for (const child of [...(children.get(project.id) ?? [])].sort(byText)) place(child, depth + 1);
  };
  for (const root of [...(children.get(null) ?? [])].sort(byText)) place(root, 0);
  // Projects caught in a parent cycle have no way down from a main project;
  // offer them anyway rather than dropping them from the picker.
  for (const rest of [...projects].sort(byText)) place(rest, 0);
  return choices;
}
