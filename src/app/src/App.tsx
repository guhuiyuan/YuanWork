import { Sidebar } from "./components/Sidebar";
import { ChatWindow } from "./components/ChatWindow";
import { RightPanel } from "./components/RightPanel";
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
        {view === "wiki" && <div className="empty-state">Wiki 浏览模式（阶段 2 实现）</div>}
        {view === "graph" && <div className="empty-state">实体图谱（阶段 2 实现）</div>}
      </main>

      <aside className="app__right">
        <RightPanel />
      </aside>
    </div>
  );
}
