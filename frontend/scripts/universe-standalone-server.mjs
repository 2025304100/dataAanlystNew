import http from "node:http";
import * as fsSync from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const frontendRoot = path.resolve(__dirname, "..");
const backendOrigin = "http://127.0.0.1:8000";
const port = 5173;

const PACKAGE_URLS = [
  { prefix: "@ant-design/icons", url: "https://esm.sh/@ant-design/icons@5.5.1?bundle&deps=react@18.3.1" },
  { prefix: "@ant-design/icons/", url: "https://esm.sh/@ant-design/icons@5.5.1/" },
  { prefix: "react-dom", url: "https://esm.sh/react-dom@18.3.1" },
  { prefix: "react-dom/", url: "https://esm.sh/react-dom@18.3.1/" },
  { prefix: "react", url: "https://esm.sh/react@18.3.1" },
  { prefix: "react/", url: "https://esm.sh/react@18.3.1/" },
  { prefix: "antd", url: "https://esm.sh/antd@5.21.0?bundle&deps=react@18.3.1,react-dom@18.3.1" },
  { prefix: "antd/", url: "https://esm.sh/antd@5.21.0/" },
  { prefix: "echarts-for-react", url: "https://esm.sh/echarts-for-react@3.0.6?bundle&deps=react@18.3.1" },
  { prefix: "dayjs", url: "https://esm.sh/dayjs@1.11.13" },
  { prefix: "dayjs/", url: "https://esm.sh/dayjs@1.11.13/" },
];

function renderBootScript(entry) {
  return `<script type="module">
      const showBootError = (error) => {
        console.error(error);
        let box = document.querySelector('.codex-dev-error');
        if (!box) {
          box = document.createElement('pre');
          box.className = 'codex-dev-error';
          document.body.appendChild(box);
        }
        box.textContent = String(error?.stack || error?.message || error);
      };
      window.addEventListener('error', (event) => showBootError(event.error || event.message));
      window.addEventListener('unhandledrejection', (event) => showBootError(event.reason));
      import('${entry}').catch(showBootError);
    </script>`;
}

const appHtml = `<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>个人量化工作台</title>
    <link rel="stylesheet" href="/src/styles/workbench.css" />
    <style>
      body { margin: 0; }
      .codex-dev-error {
        position: fixed;
        left: 16px;
        right: 16px;
        bottom: 16px;
        z-index: 9999;
        max-height: 45vh;
        overflow: auto;
        padding: 12px 14px;
        border: 1px solid #fecaca;
        background: rgba(254, 242, 242, 0.98);
        color: #991b1b;
        font: 12px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace;
        white-space: pre-wrap;
        box-shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
      }
    </style>
  </head>
  <body>
    <div id="root"></div>
    ${renderBootScript("/src/main.tsx")}
  </body>
</html>`;

const universeHtml = `<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>基础数据同步 - 调试入口</title>
    <link rel="stylesheet" href="/src/styles/workbench.css" />
    <style>
      body { margin: 0; }
      .codex-dev-note {
        position: sticky;
        top: 0;
        z-index: 10;
        padding: 8px 12px;
        background: #d9f99d;
        color: #365314;
        font: 12px/1.4 system-ui, sans-serif;
        border-bottom: 1px solid #bef264;
      }
      .codex-dev-error {
        margin: 16px auto;
        max-width: 1400px;
        padding: 12px 14px;
        border: 1px solid #fecaca;
        background: #fef2f2;
        color: #991b1b;
        font: 12px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace;
        white-space: pre-wrap;
      }
    </style>
  </head>
  <body>
    <div class="codex-dev-note">这里是“基础数据同步”独立调试页；主应用首页已经恢复到完整工作台。</div>
    <div id="root"></div>
    ${renderBootScript("/src/universe-standalone.tsx")}
  </body>
</html>`;

function send(res, status, body, headers = {}) {
  res.writeHead(status, headers);
  res.end(body);
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return chunks.length ? Buffer.concat(chunks) : undefined;
}

function normalizePathname(urlPath) {
  const pathname = decodeURIComponent(new URL(urlPath, "http://127.0.0.1").pathname);
  return pathname === "/" ? "/" : pathname.replace(/\\/g, "/");
}

function isBareSpecifier(specifier) {
  return !specifier.startsWith(".") && !specifier.startsWith("/") && !specifier.startsWith("http://") && !specifier.startsWith("https://");
}

function resolveBareSpecifier(specifier) {
  for (const entry of PACKAGE_URLS) {
    if (!entry.prefix.endsWith("/") && specifier === entry.prefix) {
      return entry.url;
    }
  }
  for (const entry of PACKAGE_URLS) {
    if (entry.prefix.endsWith("/") && specifier.startsWith(entry.prefix)) {
      return entry.url + specifier.slice(entry.prefix.length);
    }
  }
  return specifier;
}

function isWithinFrontendRoot(targetPath) {
  const resolvedTarget = path.resolve(targetPath);
  return resolvedTarget === frontendRoot || resolvedTarget.startsWith(frontendRoot + path.sep);
}

function resolveLocalFileSync(base) {
  if (!isWithinFrontendRoot(base)) {
    return null;
  }

  const candidates = [];
  if (path.extname(base)) {
    candidates.push(base);
  } else {
    candidates.push(`${base}.tsx`, `${base}.ts`, `${base}.js`, `${base}.css`);
    candidates.push(path.join(base, "index.ts"), path.join(base, "index.tsx"), path.join(base, "index.js"));
  }

  for (const candidate of candidates) {
    try {
      const stat = fsSync.statSync(candidate);
      if (stat.isFile()) return candidate;
    } catch {
      // continue
    }
  }
  return null;
}

function toRequestPath(filePath) {
  return `/${path.relative(frontendRoot, filePath).replace(/\\/g, "/")}`;
}

function resolveLocalSpecifier(specifier, importerFilePath) {
  if (!specifier.startsWith(".") && !specifier.startsWith("/")) {
    return specifier;
  }

  const base = specifier.startsWith("/")
    ? path.resolve(frontendRoot, specifier.replace(/^\/+/, ""))
    : path.resolve(path.dirname(importerFilePath), specifier);

  const resolved = resolveLocalFileSync(base);
  return resolved ? toRequestPath(resolved) : specifier;
}

function resolveImportSpecifier(specifier, importerFilePath) {
  if (isBareSpecifier(specifier)) {
    return resolveBareSpecifier(specifier);
  }
  if (specifier.startsWith(".") || specifier.startsWith("/")) {
    return resolveLocalSpecifier(specifier, importerFilePath);
  }
  return specifier;
}

function buildCssLoaderSnippet(specifier, importerFilePath) {
  const href = resolveImportSpecifier(specifier, importerFilePath);
  return `(() => {
  if (typeof document === "undefined") return;
  const href = ${JSON.stringify(href)};
  if (document.querySelector('link[data-standalone-href="' + href + '"]')) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  link.setAttribute("data-standalone-href", href);
  document.head.appendChild(link);
})();`;
}

function rewriteCssSideEffectImports(code, importerFilePath) {
  return code.replace(/^\s*import\s+["']([^"']+\.css)["'];?\s*$/gm, (_, specifier) => buildCssLoaderSnippet(specifier, importerFilePath));
}

function rewriteModuleSpecifiers(code, importerFilePath) {
  const rewrite = (_, lead, specifier, tail) => `${lead}${resolveImportSpecifier(specifier, importerFilePath)}${tail}`;
  return code
    .replace(/(from\s+["'])([^"']+)(["'])/g, rewrite)
    .replace(/(import\s+["'])([^"']+)(["'])/g, rewrite)
    .replace(/(import\(\s*["'])([^"']+)(["']\s*\))/g, rewrite)
    .replace(/(export\s+\*\s+from\s+["'])([^"']+)(["'])/g, rewrite);
}

async function resolveLocalFile(pathname) {
  const clean = pathname.replace(/^\/+/, "");
  const base = path.resolve(frontendRoot, clean);
  if (!isWithinFrontendRoot(base)) {
    return null;
  }

  return resolveLocalFileSync(base);
}

function transpile(source, filePath) {
  const result = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2020,
      module: ts.ModuleKind.ESNext,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
      allowSyntheticDefaultImports: true,
      moduleResolution: ts.ModuleResolutionKind.Bundler,
    },
    fileName: filePath,
    reportDiagnostics: false,
  });

  const normalized = result.outputText.replace(/import\.meta\.env\.DEV/g, "true");
  const withoutCssImports = rewriteCssSideEffectImports(normalized, filePath);
  return rewriteModuleSpecifiers(withoutCssImports, filePath);
}

async function proxy(req, res) {
  const targetUrl = backendOrigin + req.url;
  const body = await readBody(req);
  const headers = new Headers();
  for (const [key, value] of Object.entries(req.headers)) {
    if (value === undefined) continue;
    if (key.toLowerCase() === "host") continue;
    headers.set(key, Array.isArray(value) ? value.join(", ") : value);
  }

  const response = await fetch(targetUrl, {
    method: req.method,
    headers,
    body,
    duplex: body ? "half" : undefined,
  });

  const responseBody = Buffer.from(await response.arrayBuffer());
  const responseHeaders = {};
  response.headers.forEach((value, key) => {
    if (key.toLowerCase() === "content-encoding") return;
    responseHeaders[key] = value;
  });
  send(res, response.status, responseBody, responseHeaders);
}

const server = http.createServer(async (req, res) => {
  const startedAt = Date.now();
  try {
    const pathname = normalizePathname(req.url || "/");

    if (pathname === "/favicon.ico") {
      send(res, 204, "");
      console.log(`[standalone] ${req.method} ${pathname} -> 204 (${Date.now() - startedAt}ms)`);
      return;
    }

    if (pathname.startsWith("/api/") || pathname.startsWith("/static/")) {
      await proxy(req, res);
      console.log(`[standalone] ${req.method} ${pathname} -> proxy (${Date.now() - startedAt}ms)`);
      return;
    }

    if (pathname === "/" || pathname === "/index.html") {
      send(res, 200, appHtml, { "content-type": "text/html; charset=utf-8" });
      console.log(`[standalone] ${req.method} ${pathname} -> app-html (${Date.now() - startedAt}ms)`);
      return;
    }

    if (pathname === "/__universe" || pathname === "/__universe/index.html") {
      send(res, 200, universeHtml, { "content-type": "text/html; charset=utf-8" });
      console.log(`[standalone] ${req.method} ${pathname} -> universe-html (${Date.now() - startedAt}ms)`);
      return;
    }

    if (!pathname.startsWith("/src/")) {
      send(res, 404, "Not Found", { "content-type": "text/plain; charset=utf-8" });
      console.log(`[standalone] ${req.method} ${pathname} -> 404 (${Date.now() - startedAt}ms)`);
      return;
    }

    const filePath = await resolveLocalFile(pathname);
    if (!filePath) {
      send(res, 404, `Not Found: ${pathname}`, { "content-type": "text/plain; charset=utf-8" });
      console.log(`[standalone] ${req.method} ${pathname} -> 404-miss (${Date.now() - startedAt}ms)`);
      return;
    }

    if (filePath.endsWith(".css")) {
      const css = await fs.readFile(filePath, "utf8");
      send(res, 200, css, { "content-type": "text/css; charset=utf-8" });
      console.log(`[standalone] ${req.method} ${pathname} -> css (${Date.now() - startedAt}ms)`);
      return;
    }

    const source = await fs.readFile(filePath, "utf8");
    const js = transpile(source, filePath);
    send(res, 200, js, { "content-type": "application/javascript; charset=utf-8" });
    console.log(`[standalone] ${req.method} ${pathname} -> module (${Date.now() - startedAt}ms)`);
  } catch (error) {
    const message = error instanceof Error ? `${error.message}\n${error.stack || ""}` : String(error);
    send(res, 500, message, { "content-type": "text/plain; charset=utf-8" });
    console.error(`[standalone] ${req.method} ${req.url} -> 500`, error);
  }
});

server.listen(port, "127.0.0.1", () => {
  console.log(`UNIVERSE_STANDALONE_READY http://127.0.0.1:${port}/`);
});

