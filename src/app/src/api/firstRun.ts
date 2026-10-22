/**
 * Whether the user has been through the first-run setup.
 *
 * The knowledge base being empty is not enough on its own: a user who
 * deliberately removed every file should not be dragged back into the
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
 * Three conditions, all of them needed:
 * - the file list has actually been fetched (otherwise an empty list
 *   would flash the welcome screen on every launch),
 * - the knowledge base is empty,
 * - and the user has never been through the setup.
 */
export function shouldShowOnboarding(
  kbLoaded: boolean,
  fileCount: number,
  onboarded: boolean
): boolean {
  return kbLoaded && fileCount === 0 && !onboarded;
}
