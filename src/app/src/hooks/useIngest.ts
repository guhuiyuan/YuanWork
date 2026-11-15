import { useCallback } from "react";
import { useAppStore, type WorkspaceEntry } from "../store.ts";
import {
  addWorkspaceFile,
  addWorkspaceFolder,
  getIngestStatus,
  listKbFiles,
  listWorkspaceEntries,
  pickFile,
  pickFolder,
  rebuildWorkspace,
  removeWorkspaceEntry,
  type IngestJob,
} from "../api/backend.ts";
import { markOnboarded } from "../api/firstRun.ts";

/** How long to wait between status polls. */
const POLL_INTERVAL_MS = 700;

/** Give up on the job after this long (indexing a big folder is slow). */
const MAX_WAIT_MS = 30 * 60 * 1000;

/** How long a single status request may take before we treat it as stuck. */
const STATUS_TIMEOUT_MS = 20_000;

/** Consecutive stuck status requests before the whole job is abandoned. */
const MAX_STUCK_POLLS = 15;

/**
 * Reject if `promise` takes longer than `ms`.
 *
 * Without this a hung status request would block the poll loop
 * forever - the MAX_WAIT_MS guard is only checked *after* the await
 * returns, so it could never fire.
 */
async function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`请求超时（${ms / 1000}s）`)),
          ms
        );
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/**
 * Pick a folder and put it in the workspace.
 *
 * The workspace is the scope of the app: a folder that was picked is
 * *added* to it, and the build that follows runs over the whole
 * workspace - not over the folder alone.
 *
 * Indexing runs as a background job on the backend, so the job has to
 * be polled until it completes - listing the files right after the
 * request would still show the old (empty) list.
 *
 * The state lives in the store, not in this hook, so the sidebar and
 * the welcome screen share one view of it: two concurrent ingest jobs
 * on the same knowledge base would fight each other.
 */
export function useIngest() {
  const setKbFiles = useAppStore((s) => s.setKbFiles);
  const setCurrentFolder = useAppStore((s) => s.setCurrentFolder);
  const showToast = useAppStore((s) => s.showToast);
  const setIngestState = useAppStore((s) => s.setIngestState);
  const cancelIngest = useAppStore((s) => s.cancelIngest);
  const setWorkspaceEntries = useAppStore((s) => s.setWorkspaceEntries);
  const setWorkspaceLoaded = useAppStore((s) => s.setWorkspaceLoaded);
  const setWorkspaceError = useAppStore((s) => s.setWorkspaceError);
  const ingesting = useAppStore((s) => s.ingesting);
  const message = useAppStore((s) => s.ingestMessage);
  const folder = useAppStore((s) => s.ingestFolder);

  const refreshFiles = useCallback(async () => {
    const files = await listKbFiles();
    setKbFiles(files);
    return files;
  }, [setKbFiles]);

  /** The same, but a failed read is not worth failing a build over. */
  const refreshFilesQuietly = useCallback(async () => {
    try {
      return await refreshFiles();
    } catch {
      return useAppStore.getState().kbFiles;
    }
  }, [refreshFiles]);

  /**
   * Read the workspace entries.
   *
   * The entries are the app's scope, so the list is kept in the store:
   * the sidebar shows it and the first-run screen decides on it.
   */
  const refreshWorkspace = useCallback(async (): Promise<WorkspaceEntry[]> => {
    try {
      const entries = await listWorkspaceEntries();
      setWorkspaceEntries(entries);
      setWorkspaceError(null);
      setWorkspaceLoaded(true);
      return entries;
    } catch (e) {
      setWorkspaceLoaded(true);
      setWorkspaceError((e as Error).message);
      throw e;
    }
  }, [setWorkspaceEntries, setWorkspaceLoaded, setWorkspaceError]);

  /** The same, but a failed read is not worth failing a build over. */
  const refreshWorkspaceQuietly = useCallback(async (): Promise<WorkspaceEntry[]> => {
    try {
      return await refreshWorkspace();
    } catch {
      return useAppStore.getState().workspaceEntries;
    }
  }, [refreshWorkspace]);

  /** Stop polling and let the user try again. */
  const cancel = useCallback(() => {
    cancelIngest();
    setIngestState({ ingesting: false, message: "已取消" });
  }, [cancelIngest, setIngestState]);

  /**
   * Poll a build job until it is done.
   *
   * The job is the same shape as the folder build's - `files_total` is
   * the number of files the workspace covers, so the progress bar has
   * a real denominator.
   *
   * The token is the one the action started with: a cancel that came
   * in while the workspace was being written still reaches this loop.
   */
  const runJob = useCallback(async (job: IngestJob, token: number): Promise<boolean> => {
    const cancelled = () => useAppStore.getState().ingestToken !== token;
    const total = job.files_queued ?? 0;

    setIngestState({
      ingesting: true,
      message: total
        ? `正在索引 ${total} 个文件…`
        : "工作域里没有可索引的文件",
    });

    const started = Date.now();
    let stuck = 0;
    for (;;) {
      if (cancelled()) return false;

      let status;
      try {
        status = await withTimeout(
          getIngestStatus(job.job_id),
          STATUS_TIMEOUT_MS
        );
        stuck = 0;
      } catch (e) {
        // A single stuck request is not fatal - the backend may just
        // be busy embedding a big file.
        stuck += 1;
        if (stuck >= MAX_STUCK_POLLS) {
          setIngestState({ ingesting: false, message: null });
          showToast("索引长时间没有响应，已停止等待", "error");
          return false;
        }
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        continue;
      }

      if (cancelled()) return false;

      if (status.status === "completed") {
        const files = await refreshFilesQuietly();
        await refreshWorkspaceQuietly();
        // Only now is the setup actually done: an index run that
        // failed must leave the welcome screen in place.
        markOnboarded();
        setIngestState({ ingesting: false, message: "索引完成" });
        showToast(
          files.length
            ? `已索引 ${files.length} 个文件`
            : "工作域里没有可索引的文件",
          files.length ? "success" : "info"
        );
        return true;
      }
      if (status.status === "failed") {
        setIngestState({ ingesting: false, message: null });
        showToast(`索引失败：${status.error ?? "未知错误"}`, "error");
        return false;
      }
      if (Date.now() - started > MAX_WAIT_MS) {
        setIngestState({ ingesting: false, message: null });
        showToast("索引超时，请检查后端日志", "error");
        return false;
      }
      const done = status.files_done ?? 0;
      const count = status.files_total ?? total;
      setIngestState({
        ingesting: true,
        message: count
          ? `正在索引…（${done}/${count}）`
          : "工作域里没有可索引的文件",
      });
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
  }, [refreshFilesQuietly, refreshWorkspaceQuietly, setIngestState, showToast]);

  /**
   * Show the folder picker and add what the user chooses to the
   * workspace.
   *
   * Returns true when the build over the workspace finished, false
   * when the user cancelled the picker or the build failed.
   */
  const pickAndIngest = useCallback(async (): Promise<boolean> => {
    // The token is shared through the store, so a cancel from another
    // component reaches this loop.
    const token = useAppStore.getState().ingestToken;
    try {
      // Inside the try: a failing native dialog must not escape as an
      // unhandled rejection, or the button would silently do nothing.
      const picked = await pickFolder();
      if (!picked) return false;

      setCurrentFolder(picked);
      setIngestState({ ingesting: true, message: "正在准备…", folder: picked });

      // The folder joins the workspace - the build runs over the
      // workspace, not over this folder alone.
      await addWorkspaceFolder(picked);
      await refreshWorkspaceQuietly();

      const job = await rebuildWorkspace();
      return await runJob(job, token);
    } catch (e) {
      setIngestState({ ingesting: false, message: null });
      showToast(`索引失败：${(e as Error).message}`, "error");
      return false;
    }
  }, [refreshWorkspaceQuietly, runJob, setCurrentFolder, setIngestState, showToast]);

  /**
   * Add a single file to the workspace, by hand.
   *
   * The file joins the workspace and the build over the workspace
   * follows - a file that is already indexed is skipped.
   */
  const addFile = useCallback(async (): Promise<boolean> => {
    const token = useAppStore.getState().ingestToken;
    try {
      const picked = await pickFile();
      if (!picked) return false;

      setIngestState({ ingesting: true, message: "正在准备…", folder: picked });
      await addWorkspaceFile(picked);
      await refreshWorkspaceQuietly();

      const job = await rebuildWorkspace();
      return await runJob(job, token);
    } catch (e) {
      setIngestState({ ingesting: false, message: null });
      showToast(`添加失败：${(e as Error).message}`, "error");
      return false;
    }
  }, [refreshWorkspaceQuietly, runJob, setIngestState, showToast]);

  /**
   * Build the index over the workspace again.
   *
   * The files that are already indexed are skipped, so this is safe to
   * run after the user dropped new files into a workspace folder.
   */
  const rebuild = useCallback(async (): Promise<boolean> => {
    const token = useAppStore.getState().ingestToken;
    try {
      setIngestState({ ingesting: true, message: "正在准备…" });
      const job = await rebuildWorkspace();
      return await runJob(job, token);
    } catch (e) {
      setIngestState({ ingesting: false, message: null });
      showToast(`索引失败：${(e as Error).message}`, "error");
      return false;
    }
  }, [runJob, setIngestState, showToast]);

  /**
   * Take an entry out of the workspace.
   *
   * The backend clears what that entry brought into the index; the
   * files on disk stay where they are.
   */
  const removeEntry = useCallback(
    async (entry: WorkspaceEntry): Promise<void> => {
      try {
        await removeWorkspaceEntry(entry.id);
      } catch (e) {
        showToast(`移除失败：${(e as Error).message}`, "error");
        return;
      }
      await refreshWorkspaceQuietly();
      showToast(
        `已从工作域移除「${entryName(entry)}」（它带入索引的内容已清除，文件仍在磁盘上）`,
        "success"
      );
    },
    [refreshWorkspaceQuietly, showToast]
  );

  return {
    ingesting,
    message,
    folder,
    pickAndIngest,
    addFile,
    rebuild,
    removeEntry,
    refreshFiles,
    refreshWorkspace,
    cancel,
  };
}

/** The name of an entry - the last piece of its path. */
export function entryName(entry: WorkspaceEntry): string {
  const parts = entry.path.split(/[\\/]/).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : entry.path;
}
