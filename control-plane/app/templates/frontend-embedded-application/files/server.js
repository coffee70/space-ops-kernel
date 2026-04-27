const fs = require("fs");
const path = require("path");
const http = require("http");

const port = Number(process.env.PORT || 3100);
const basePath = (process.env.APPLICATION_PROXY_BASE_PATH || "/").replace(/\/+$/, "") || "/";
const publicDir = path.join(__dirname, "public");

function withBase(requestPath) {
  if (basePath === "/") return requestPath;
  return requestPath === basePath || requestPath.startsWith(`${basePath}/`);
}

function stripBase(requestPath) {
  if (basePath === "/") return requestPath;
  const stripped = requestPath.slice(basePath.length);
  return stripped || "/";
}

const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok", application_id: process.env.APPLICATION_ID || "{{application_id}}" }));
    return;
  }

  const requestPath = req.url.split("?")[0];
  if (!withBase(requestPath)) {
    res.writeHead(404);
    res.end("Not found");
    return;
  }

  const relativePath = stripBase(requestPath) === "/" ? "/index.html" : stripBase(requestPath);
  const target = path.join(publicDir, relativePath);
  const safeTarget = path.normalize(target);
  if (!safeTarget.startsWith(publicDir)) {
    res.writeHead(400);
    res.end("Bad request");
    return;
  }

  fs.readFile(safeTarget, (error, data) => {
    if (error) {
      res.writeHead(404);
      res.end("Not found");
      return;
    }
    const contentType = safeTarget.endsWith(".css")
      ? "text/css"
      : safeTarget.endsWith(".js")
        ? "application/javascript"
        : "text/html";
    res.writeHead(200, { "content-type": contentType });
    res.end(data);
  });
});

server.listen(port, "0.0.0.0");
