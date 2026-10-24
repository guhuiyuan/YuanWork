import { useCallback, useState } from "react";
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

export interface IngestState {
  /** True while a folder is being indexed. */
  ingesting: boolean;
  /** Human-readable progress line, e.g. "正在索引…（3/12）". */
  message: string | null;
  /** The folder that is being indexed. */
  folder: string | null;
}

/**
 * Pick a folder and index it.
 *
 * Indexing runs as a background job on the backend, so the job has to
 * be polled until it completes - listing the files right after the
 * request would still show the old (empty) list.
 */
export function useIngest() {
  const setKbFiles = useAppStore((s) => s.setKbFiles);
  const setCurrentFolder = useAppStore((s) => s.setCurrentFolder);
  const showToast = useAppStore((s) => s.showToast);
  const [state, setState] = useState<IngestState>({
    ingesting: false,
    message: null,
    folder: null,
  });

  const refreshFiles = useCallback(async () => {
    const files = await listKbFiles();
    setKbFiles(files);
    return files;
  }, [setKbFiles]);

  /**
   * Show the folder picker and index whatever the user chooses.
   *
   * Returns true when a folder was indexed, false when the user
   * cancelled the picker.
   */
  const pickAndIngest = useCallback(async (): Promise<boolean> => {
    const folder = await pickFolder();
    if (!folder) return false;

    setCurrentFolder(folder);
    setState({ ingesting: true, message: "正在准备…", folder });
    try {
      const job = await ingestDirectory(folder, true);
      // The user has made the setup decision - whichever button they
      // pressed to get here. The welcome screen must not come back on
      // the next launch.
      markOnboarded();
      const total = job.files_queued ?? 0;
      setState({
        ingesting: true,
        message: total
          ? `正在索引 ${total} 个文件…`
          : "这个文件夹里没有可索引的文件",
        folder,
      });

      const started = Date.now();
      for (;;) {
        const status = await getIngestStatus(job.job_id);
        if (status.status === "completed") {
          setState({ ingesting: false, message: "索引完成", folder });
          const files = await refreshFiles();
          showToast(
            files.length
              ? `已索引 ${files.length} 个文件`
              : "这个文件夹里没有可索引的文件",
            files.length ? "success" : "info"
          );
          return true;
        }
        if (status.status === "failed") {
          setState({ ingesting: false, message: null, folder });
          showToast(`索引失败：${status.error ?? "未知错误"}`, "error");
          return false;
        }
        if (Date.now() - started > MAX_WAIT_MS) {
          setState({ ingesting: false, message: null, folder });
          showToast("索引超时，请检查后端日志", "error");
          return false;
        }
        const done = status.files_done ?? 0;
        const count = status.files_total ?? total;
        setState({
          ingesting: true,
          message: count
            ? `正在索引…（${done}/${count}）`
            : "正在索引…",
          folder,
        });
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      }
    } catch (e) {
      setState({ ingesting: false, message: null, folder });
      showToast(`索引失败：${(e as Error).message}`, "error");
      return false;
    }
  }, [refreshFiles, setCurrentFolder, showToast]);

  return { ...state, pickAndIngest, refreshFiles };
}
