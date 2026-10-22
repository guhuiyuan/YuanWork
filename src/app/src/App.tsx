import { useEffect } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatWindow } from "./components/ChatWindow";
import { RightPanel } from "./components/RightPanel";
import { Onboarding } from "./components/Onboarding";
import { WikiTreePanel } from "./components/Wiki/WikiTreePanel";
import { WikiPageView } from "./components/Wiki/WikiPageView";
import { WikiGraphView } from "./components/Wiki/WikiGraphView";
import { WikiBacklinks } from "./components/Wiki/WikiBacklinks";
import { useAppStore } from "./store";
import { hasOnboarded, shouldShowOnboarding } from "./api/firstRun";

export function App() {
  const view = useAppStore((s) => s.view);
  const kbFiles = useAppStore((s) => s.kbFiles);
  const kbLoaded = useAppStore((s) => s.kbLoaded);
  const toast = useAppStore((s) => s.toast);
  const dismissToast = useAppStore((s) => s.dismissToast);

  // The welcome screen shows until the user has picked a working
  // folder. An empty knowledge base alone is not enough to decide
  // that - someone may have removed every file on purpose.
  const needsSetup = shouldShowOnboarding(
    kbLoaded,
    kbFiles.length,
    hasOnboarded()
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
        <Sidebar />
      </aside>

      <main className="app__main">
        {needsSetup ? (
          <Onboarding />
        ) : (
          <>
            {view === "chat" && <ChatWindow />}
            {view === "wiki" && <WikiPageView />}
            {view === "graph" && <WikiGraphView />}
          </>
        )}
      </main>

      <aside className="app__right">
        {view === "wiki" ? <WikiBacklinks /> : <RightPanel />}
      </aside>

      {/* Wiki tree floats as a slide-in panel over the sidebar when in wiki view,
          so users can browse the page tree without losing the navigation. */}
      {view === "wiki" && (
        <aside className="app__wiki-tree">
          <WikiTreePanel />
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
