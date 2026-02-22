import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  flushDelayedNotificationsToRequestQueue,
  resolveDelayedRequests,
  clearResolvedRequests,
} from "../adapters/openclaw/delayed-requests.js";

function makeWorkspace(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

describe("delayed request lifecycle integration", () => {
  it("queues from delayed notifications, then resolves and clears request items", () => {
    const workspace = makeWorkspace("quaid-delayed-requests-");
    const delayedNotificationsPath = path.join(workspace, "delayed-notifications.json");
    const delayedRequestsPath = path.join(workspace, "delayed-llm-requests.json");

    fs.writeFileSync(
      delayedNotificationsPath,
      JSON.stringify(
        {
          items: [
            {
              id: "janitor-1",
              kind: "janitor_health",
              priority: "high",
              status: "pending",
              message: "[Quaid] Janitor has never run.",
            },
          ],
        },
        null,
        2,
      ),
      "utf8",
    );

    const flushed = flushDelayedNotificationsToRequestQueue(
      delayedNotificationsPath,
      delayedRequestsPath,
      5,
    );
    expect(flushed.delivered).toBe(1);
    expect(flushed.queuedLlmRequests).toBe(1);

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
