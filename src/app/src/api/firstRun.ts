/**
 * Whether the user has been through the first-run setup.
 *
 * The workspace being empty is not enough on its own: a user who
 * deliberately removed every entry should not be dragged back into the
 * welcome screen on the next launch. So the flag records that the
 * setup decision was made, and it survives restarts.
 */

const KEY = "yuanwork.onboarded";

export function hasOnboarded(): boolean {
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    // localStorage can be unavailable (private mode, disabled storage).
    return false;
  }
}

export function markOnboarded(): void {
  try {
    window.localStorage.setItem(KEY, "1");
  } catch {
    // Nothing we can do - the user will just see the welcome again.
  }
}

/**
 * Whether the welcome screen should be shown.
 *
 * Four conditions, all of them needed:
 * - the workspace has actually been read (otherwise an empty list
 *   would flash the welcome screen on every launch),
 * - the workspace really is empty - a failed read is not an empty
 *   workspace, and the welcome screen would be a lie,
 * - and the user has never been through the setup.
 *
 * The decision is made on the *workspace*, not on the indexed files:
 * the workspace is what the user set up, and an empty knowledge base
 * only says that the build has not run yet.
 */
export function shouldShowOnboarding(
  loaded: boolean,
  entryCount: number,
  onboarded: boolean,
  loadError: string | null = null
): boolean {
  return loaded && !loadError && entryCount === 0 && !onboarded;
}
