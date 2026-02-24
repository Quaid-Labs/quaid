import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  queueDelayedRequest,
  resolveDelayedRequests,
  clearResolvedRequests,
} from "../adaptors/openclaw/delayed-requests.js";

function makeWorkspace(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

describe("delayed request lifecycle integration", () => {
  it("queues, resolves and clears request items", () => {
    const workspace = makeWorkspace("quaid-delayed-requests-");
    const delayedRequestsPath = path.join(workspace, "delayed-llm-requests.json");

    const queued = queueDelayedRequest(
      delayedRequestsPath,
      "[Quaid] Janitor has never run.",
      "janitor_health",
      "high",
      "event.notification.delayed",
    );
    expect(queued).toBe(true);

    const requestsData = JSON.parse(fs.readFileSync(delayedRequestsPath, "utf8"));
    expect(Array.isArray(requestsData.requests)).toBe(true);
    expect(requestsData.requests).toHaveLength(1);
    expect(requestsData.requests[0].status).toBe("pending");
    expect(requestsData.requests[0].kind).toBe("janitor_health");

    const resolved = resolveDelayedRequests(
      delayedRequestsPath,
      [requestsData.requests[0].id],
      "confirmed with user",
    );
    expect(resolved).toBe(1);

    const postResolve = JSON.parse(fs.readFileSync(delayedRequestsPath, "utf8"));
    expect(postResolve.requests[0].status).toBe("resolved");
    expect(postResolve.requests[0].resolution_note).toBe("confirmed with user");

    const cleared = clearResolvedRequests(delayedRequestsPath);
    expect(cleared).toBe(1);

    const postClear = JSON.parse(fs.readFileSync(delayedRequestsPath, "utf8"));
    expect(postClear.requests).toHaveLength(0);
  });
});
