import { useCallback } from "react";
import { useAppStore } from "../store";
import {
  getIngestStatus,
  ingestDirectory,
  listKbFiles,
  pickFolder,
} from "../api/backend";
import { markOnboarded } from "../api/firstRun";

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
 * Pick a folder and index it.
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
  const ingesting = useAppStore((s) => s.ingesting);
  const message = useAppStore((s) => s.ingestMessage);
  const folder = useAppStore((s) => s.ingestFolder);

  const refreshFiles = useCallback(async () => {
    const files = await listKbFiles();
    setKbFiles(files);
    return files;
  }, [setKbFiles]);

  /** Stop polling and let the user try again. */
  const cancel = useCallback(() => {
    cancelIngest();
    setIngestState({ ingesting: false, message: "已取消" });
  }, [cancelIngest, setIngestState]);

  /**
   * Show the folder picker and index whatever the user chooses.
   *
   * Returns true when a folder was indexed, false when the user
   * cancelled the picker or the index failed.
   */
  const pickAndIngest = useCallback(async (): Promise<boolean> => {
    // The token is shared through the store, so a cancel from another
    // component reaches this loop.
    const token = useAppStore.getState().ingestToken;
    const cancelled = () => useAppStore.getState().ingestToken !== token;

    try {
      // Inside the try: a failing native dialog must not escape as an
      // unhandled rejection, or the button would silently do nothing.
      const picked = await pickFolder();
      if (!picked) return false;

      setCurrentFolder(picked);
      setIngestState({ ingesting: true, message: "正在准备…", folder: picked });

      const job = await ingestDirectory(picked, true);
      const total = job.files_queued ?? 0;
      setIngestState({
        ingesting: true,
        message: total
          ? `正在索引 ${total} 个文件…`
          : "这个文件夹里没有可索引的文件",
        folder: picked,
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
            setIngestState({ ingesting: false, message: null, folder: picked });
            showToast("索引长时间没有响应，已停止等待", "error");
            return false;
          }
          await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
          continue;
        }

        if (cancelled()) return false;

        if (status.status === "completed") {
          const files = await refreshFiles();
          // Only now is the setup actually done: an index run that
          // failed must leave the welcome screen in place.
          markOnboarded();
          setIngestState({ ingesting: false, message: "索引完成", folder: picked });
          showToast(
            files.length
              ? `已索引 ${files.length} 个文件`
              : "这个文件夹里没有可索引的文件",
            files.length ? "success" : "info"
          );
          return true;
        }
        if (status.status === "failed") {
          setIngestState({ ingesting: false, message: null, folder: picked });
          showToast(`索引失败：${status.error ?? "未知错误"}`, "error");
          return false;
        }
        if (Date.now() - started > MAX_WAIT_MS) {
          setIngestState({ ingesting: false, message: null, folder: picked });
          showToast("索引超时，请检查后端日志", "error");
          return false;
        }
        const done = status.files_done ?? 0;
        const count = status.files_total ?? total;
        setIngestState({
          ingesting: true,
          message: count
            ? `正在索引…（${done}/${count}）`
            : "这个文件夹里没有可索引的文件",
          folder: picked,
        });
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      }
    } catch (e) {
      setIngestState({ ingesting: false, message: null });
      showToast(`索引失败：${(e as Error).message}`, "error");
      return false;
    }
  }, [refreshFiles, setCurrentFolder, setIngestState, showToast]);

  return { ingesting, message, folder, pickAndIngest, cancel, refreshFiles };
}
