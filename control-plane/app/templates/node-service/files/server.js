const http = require("http");

const port = Number(process.env.PORT || 8080);

const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok", unit_id: "{{unit_id}}" }));
    return;
  }

  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify({
    unit_id: "{{unit_id}}",
    display_name: "{{display_name}}",
    message: "Managed node service is running.",
  }));
});

server.listen(port, "0.0.0.0");

