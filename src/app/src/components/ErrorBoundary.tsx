/**
 * The error boundary.
 *
 * A render-time exception in one panel used to take the whole tree
 * with it: the window went white and the user had to restart the app
 * to get their work back. A boundary catches the exception and shows
 * what happened - the rest of the app keeps working.
 */

import { Component, ErrorInfo, ReactNode } from "react";
import { errorNotice } from "../api/errorNotice";

interface Props {
  children: ReactNode;
  /** What the panel is called - the user reads this in the notice. */
  label?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("A panel crashed:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      const notice = errorNotice(this.state.error, this.props.label);
      return (
        <div className="error-boundary">
          <strong>{notice.title}</strong>
          <div>{notice.detail}</div>
          <button
            className="btn"
            onClick={() => this.setState({ error: null })}
          >
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
