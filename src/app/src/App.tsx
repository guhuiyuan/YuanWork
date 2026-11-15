import { useEffect } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatWindow } from "./components/ChatWindow";
import { RightPanel } from "./components/RightPanel";
import { Onboarding } from "./components/Onboarding";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { WikiTreePanel } from "./components/Wiki/WikiTreePanel";
import { WikiPageView } from "./components/Wiki/WikiPageView";
import { WikiGraphView } from "./components/Wiki/WikiGraphView";
import { WikiBacklinks } from "./components/Wiki/WikiBacklinks";
import { useAppStore } from "./store";
import { hasOnboarded, shouldShowOnboarding } from "./api/firstRun";

export function App() {
  const view = useAppStore((s) => s.view);
  const workspaceEntries = useAppStore((s) => s.workspaceEntries);
  const workspaceLoaded = useAppStore((s) => s.workspaceLoaded);
  const workspaceError = useAppStore((s) => s.workspaceError);
  const toast = useAppStore((s) => s.toast);
  const dismissToast = useAppStore((s) => s.dismissToast);

  // The welcome screen shows until the user has set up a workspace
  // (AC-F-001). The *workspace* decides that - not the indexed files:
  // an empty knowledge base only means the build has not run yet (or
  // that the user removed every file on purpose), while an empty
  // workspace means the app does not know what to work on.
  const needsSetup = shouldShowOnboarding(
    workspaceLoaded,
    workspaceEntries.length,
    hasOnboarded(),
    workspaceError
  );

  // Auto-dismiss the toast.
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(dismissToast, 4000);
    return () => clearTimeout(timer);
  }, [toast, dismissToast]);

  return (
    <div className="app">
      <aside className="app__sidebar">
        <ErrorBoundary label="侧边栏">
          <Sidebar />
        </ErrorBoundary>
      </aside>

      <main className="app__main">
        {needsSetup ? (
          <Onboarding />
        ) : (
          <>
            {view === "chat" && (
              <ErrorBoundary label="对话">
                <ChatWindow />
              </ErrorBoundary>
            )}
            {view === "wiki" && (
              <ErrorBoundary label="Wiki 页面">
                <WikiPageView />
              </ErrorBoundary>
            )}
            {view === "graph" && (
              <ErrorBoundary label="图谱">
                <WikiGraphView />
              </ErrorBoundary>
            )}
          </>
        )}
      </main>

      <aside className="app__right">
        <ErrorBoundary label="右侧面板">
          {view === "wiki" ? <WikiBacklinks /> : <RightPanel />}
        </ErrorBoundary>
      </aside>

      {/* Wiki tree floats as a slide-in panel over the sidebar when in wiki view,
          so users can browse the page tree without losing the navigation. */}
      {view === "wiki" && (
        <aside className="app__wiki-tree">
          <ErrorBoundary label="页面树">
            <WikiTreePanel />
          </ErrorBoundary>
        </aside>
      )}

      {toast && (
        <div className={`toast toast--${toast.kind}`} onClick={dismissToast}>
          {toast.text}
        </div>
      )}

      <style>{toastStyles}</style>
    </div>
  );
}

const toastStyles = `
  .error-boundary {
    padding: 16px;
    margin: 12px;
    border: 1px solid var(--border);
    border-left: 3px solid var(--danger);
    border-radius: 6px;
    background: var(--bg-secondary, transparent);
    font-size: 13px;
    color: var(--text-primary);
  }
  .error-boundary strong { display: block; margin-bottom: 4px; }
  .error-boundary .btn { margin-top: 8px; }

  .toast {
    position: fixed;
    left: 50%;
    bottom: 28px;
    transform: translateX(-50%);
    z-index: 100;
    max-width: 70vw;
    padding: 10px 18px;
    border-radius: 8px;
    font-size: 13px;
    color: #fff;
    background: #333;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.18);
    cursor: pointer;
    animation: toast-in 0.18s ease-out;
  }
  .toast--success { background: var(--success); }
  .toast--error { background: var(--danger); }
  .toast--info { background: #333; }
  @keyframes toast-in {
    from { opacity: 0; transform: translate(-50%, 8px); }
    to { opacity: 1; transform: translate(-50%, 0); }
  }
`;
