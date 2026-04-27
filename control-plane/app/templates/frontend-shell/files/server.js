require("http")
  .createServer((req, res) => {
    if (req.url === "/health") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ status: "ok", shell: true }));
      return;
    }
    res.writeHead(200, { "content-type": "text/plain" });
    res.end("frontend shell");
  })
  .listen(process.env.PORT || 3100, "0.0.0.0");
