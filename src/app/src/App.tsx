import { Sidebar } from "./components/Sidebar";
import { ChatWindow } from "./components/ChatWindow";
import { RightPanel } from "./components/RightPanel";
import { WikiTreePanel } from "./components/Wiki/WikiTreePanel";
import { WikiPageView } from "./components/Wiki/WikiPageView";
import { WikiGraphView } from "./components/Wiki/WikiGraphView";
import { WikiBacklinks } from "./components/Wiki/WikiBacklinks";
import { useAppStore } from "./store";

export function App() {
  const view = useAppStore((s) => s.view);

  return (
    <div className="app">
      <aside className="app__sidebar">
        <Sidebar />
      </aside>

      <main className="app__main">
        {view === "chat" && <ChatWindow />}
        {view === "wiki" && <WikiPageView />}
        {view === "graph" && <WikiGraphView />}
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
    </div>
  );
}
