import { useAppStore } from "../store";
import { useIngest } from "../hooks/useIngest";
import { markOnboarded } from "../api/firstRun";

/**
 * The first-run screen.
 *
 * Shown until the user has picked a working folder. A fresh install has
 * an empty knowledge base, so the app has nothing to say until it knows
 * what to index - this screen makes that one decision obvious instead
 * of leaving the user staring at an empty sidebar.
 */
export function Onboarding() {
  const { ingesting, message, pickAndIngest } = useIngest();
  const setView = useAppStore((s) => s.setView);
  const showToast = useAppStore((s) => s.showToast);

  async function handlePick() {
    const ok = await pickAndIngest();
    if (ok) {
      markOnboarded();
      setView("chat");
    }
  }

  function handleSkip() {
    markOnboarded();
    setView("chat");
    showToast("可以随时从左侧「+ 添加」选择工作目录", "info");
  }

  return (
    <div className="onboard">
      <div className="onboard__card">
        <div className="onboard__logo">Y</div>
        <h1 className="onboard__title">欢迎使用 YuanWork</h1>
        <p className="onboard__lead">
          你的本地工作助手。选一个工作目录，它会把这个目录里的文档
          建成可检索、可引用的知识库——所有内容都留在这台机器上。
        </p>

        <ul className="onboard__points">
          <li>
            <span className="onboard__point-icon">📥</span>
            <div>
              <strong>选一个工作目录</strong>
              <div>支持 PDF、Word、PPT、Excel、Markdown、图片 OCR</div>
            </div>
          </li>
          <li>
            <span className="onboard__point-icon">🔍</span>
            <div>
              <strong>带引用地回答</strong>
              <div>每个结论都能点回原文出处；证据不足时它会明说，而不是编</div>
            </div>
          </li>
          <li>
            <span className="onboard__point-icon">🔒</span>
            <div>
              <strong>数据不出本机</strong>
              <div>知识库留在本地，出网请求走白名单，调用全部记审计日志</div>
            </div>
          </li>
        </ul>

        <div className="onboard__actions">
          <button
            className="btn btn--primary onboard__cta"
            onClick={handlePick}
            disabled={ingesting}
          >
            {ingesting ? "索引中…" : "选择工作目录"}
          </button>
          <button className="btn onboard__skip" onClick={handleSkip} disabled={ingesting}>
            稍后再说
          </button>
        </div>

        {message && <div className="onboard__status">{message}</div>}
      </div>

      <style>{onboardStyles}</style>
    </div>
  );
}

const onboardStyles = `
  .onboard {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 32px;
    overflow-y: auto;
    background:
      radial-gradient(1200px 500px at 50% -10%, rgba(37, 99, 235, 0.08), transparent),
      var(--bg-primary);
  }
  .onboard__card {
    width: 100%;
    max-width: 520px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 36px 32px 28px;
    text-align: center;
  }
  .onboard__logo {
    width: 52px;
    height: 52px;
    margin: 0 auto 18px;
    border-radius: 14px;
    background: var(--accent);
    color: #fff;
    font-size: 26px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .onboard__title {
    margin: 0 0 10px;
    font-size: 21px;
    color: var(--text-primary);
  }
  .onboard__lead {
    margin: 0 0 26px;
    font-size: 13px;
    line-height: 1.7;
    color: var(--text-secondary);
  }
  .onboard__points {
    list-style: none;
    margin: 0 0 28px;
    padding: 0;
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .onboard__points li {
    display: flex;
    gap: 12px;
    align-items: flex-start;
  }
  .onboard__point-icon {
    flex-shrink: 0;
    width: 30px;
    height: 30px;
    border-radius: 8px;
    background: var(--bg-tertiary);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 15px;
  }
  .onboard__points strong {
    display: block;
    font-size: 13px;
    color: var(--text-primary);
    margin-bottom: 2px;
  }
  .onboard__points div div {
    font-size: 12px;
    color: var(--text-secondary);
    line-height: 1.6;
  }
  .onboard__actions {
    display: flex;
    gap: 10px;
    justify-content: center;
  }
  .onboard__cta {
    padding: 10px 26px;
    font-size: 14px;
  }
  .onboard__skip {
    padding: 10px 18px;
    font-size: 13px;
    color: var(--text-secondary);
  }
  .onboard__status {
    margin-top: 16px;
    font-size: 12px;
    color: var(--text-secondary);
  }
`;
