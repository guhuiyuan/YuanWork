/**
 * What the user reads when a panel crashes.
 *
 * The message is kept short and free of internals: a render-time
 * exception can carry anything, and the panel is not the place to
 * read a stack trace.
 */
export function errorNotice(
  error: unknown,
  label = "这一块"
): { title: string; detail: string } {
  const message =
    error instanceof Error ? error.message : String(error ?? "");
  return {
    title: `⚠️ ${label}出错了`,
    detail: message
      ? `这一部分没能显示出来，其余部分还能用。${message.slice(0, 120)}`
      : "这一部分没能显示出来，其余部分还能用。",
  };
}
