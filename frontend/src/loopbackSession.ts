/**
 * One-time loopback session for the wizard and the shell.
 *
 * The bootstrap meta is single-use. Cold open used to redeem it only
 * inside WorkstationShell, so the wizard's starter and paper calls
 * were refused. Callers share one in-flight redeem. A failed start can
 * be retried without a reload (KQ-04).
 */
import { recoverCsrf, redeemBootstrap } from "./session";

const BOOTSTRAP_META = "krellbot-bootstrap";

let inflight: Promise<boolean> | null = null;

function consumeBootstrapToken(): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  const meta = document.querySelector(`meta[name="${BOOTSTRAP_META}"]`);
  if (meta === null) {
    return null;
  }
  const token = meta.getAttribute("content") ?? "";
  meta.remove();
  return token;
}

function track(run: Promise<boolean>): Promise<boolean> {
  const shared = run.finally(() => {
    if (inflight === shared) {
      inflight = null;
    }
  });
  inflight = shared;
  return shared;
}

async function redeemToken(token: string): Promise<boolean> {
  try {
    await redeemBootstrap(token);
    return true;
  } catch {
    return false;
  }
}

async function recoverOnly(): Promise<boolean> {
  try {
    return (await recoverCsrf()) === true;
  } catch {
    return false;
  }
}

/**
 * Redeem a present bootstrap meta immediately. Callers that arrive
 * after the meta is gone share that in-flight redeem instead of
 * recovering a session that does not exist yet.
 */
export function ensureLoopbackSession(): Promise<boolean> {
  const token = consumeBootstrapToken();
  if (token) {
    return track(redeemToken(token));
  }
  if (inflight) {
    return inflight;
  }
  return track(recoverOnly());
}

/**
 * The bootstrap token a fresh read of the shell still carries. The server
 * serves it only until one redeem succeeds, so a start that failed before
 * redeeming can try again. Same-origin read, no redirects.
 */
async function unusedShellToken(): Promise<string | null> {
  try {
    const response = await fetch("/", {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      redirect: "error",
    });
    const type = response.headers.get("Content-Type")?.split(";", 1)[0]?.trim().toLowerCase();
    if (response.status !== 200 || type !== "text/html") {
      return null;
    }
    // An inert parse: nothing is mounted, run or navigated.
    const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
    const metas = parsed.querySelectorAll(`meta[name="${BOOTSTRAP_META}"]`);
    if (metas.length !== 1) {
      return null;
    }
    const token = metas[0]?.getAttribute("content")?.trim() ?? "";
    return token.length > 0 ? token : null;
  } catch {
    return null;
  }
}

async function retryOnce(): Promise<boolean> {
  if (await recoverOnly()) {
    return true;
  }
  const token = await unusedShellToken();
  if (token === null) {
    return false;
  }
  return redeemToken(token);
}

/**
 * Try the session again after `ensureLoopbackSession` resolved false.
 * Shares an in-flight attempt. Otherwise it re-reads an existing
 * session's CSRF, then redeems the shell's still-unused bootstrap token.
 * It never sends a command; callers send theirs only after `true`.
 */
export function retryLoopbackSession(): Promise<boolean> {
  const token = consumeBootstrapToken();
  if (token) {
    return track(redeemToken(token));
  }
  if (inflight) {
    return inflight;
  }
  return track(retryOnce());
}
