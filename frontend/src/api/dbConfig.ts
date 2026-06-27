import { requestJson } from "./client";
import type { DbConfig, TestConnectionResult, MigrationProgress } from "../types";

const BASE = "/api/v1/settings/db-config";

const JSON_HEADERS = { "Content-Type": "application/json" };

export const dbConfigApi = {
  get: (): Promise<DbConfig> => requestJson(BASE),

  update: (config: DbConfig): Promise<{ status: string; db_type: string; message: string }> =>
    requestJson(BASE, { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(config) }),

  test: (config: DbConfig): Promise<TestConnectionResult> =>
    requestJson(`${BASE}/test`, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(config) }),

  migrate: (): Promise<{ status: string; message: string }> =>
    requestJson(`${BASE}/migrate`, { method: "POST" }),

  migrationStatus: (): Promise<MigrationProgress> =>
    requestJson(`${BASE}/migration-status`),
};
