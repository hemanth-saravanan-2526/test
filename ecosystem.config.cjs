const path = require("path");
const isWin = process.platform === "win32";
const venvBin = isWin
  ? (exe) => path.join(__dirname, ".venv", "Scripts", `${exe}.exe`)
  : (exe) => path.join(__dirname, ".venv", "bin", exe);

module.exports = {
  apps: [
    {
      name: "scraper-studio-api",
      script: venvBin("uvicorn"),
      args: "api_service:app --host 0.0.0.0 --port 3683",
      cwd: __dirname,
      interpreter: "none",
      watch: false,
      autorestart: true,
      max_memory_restart: "1G",
      env: { PYTHONUNBUFFERED: "1" },
      error_file: "./logs/api-error.log",
      out_file: "./logs/api-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
    {
      name: "scraper-studio-ui",
      script: venvBin("streamlit"),
      args: "run streamlit_app.py --server.port 3682 --server.address 0.0.0.0 --server.headless true",
      cwd: __dirname,
      interpreter: "none",
      watch: false,
      autorestart: true,
      max_memory_restart: "1G",
      env: { PYTHONUNBUFFERED: "1" },
      error_file: "./logs/ui-error.log",
      out_file: "./logs/ui-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
    {
      name: "scraper-studio-mcp",
      script: isWin ? "C:\\Windows\\System32\\cmd.exe" : "/bin/bash",
      args: isWin
        ? `/c npx -y @playwright/mcp@latest --port ${process.env.MCP_PORT || (process.env.MCP_SERVER_URL ? (new URL(process.env.MCP_SERVER_URL).port || "8931") : "8931")} --host 0.0.0.0 --isolated`
        : `-c "npx -y @playwright/mcp@latest --port ${process.env.MCP_PORT || (process.env.MCP_SERVER_URL ? (new URL(process.env.MCP_SERVER_URL).port || "8931") : "8931")} --host 0.0.0.0 --isolated"`,
      cwd: __dirname,
      interpreter: "none",
      watch: false,
      autorestart: true,
      max_memory_restart: "512M",
      error_file: "./logs/mcp-error.log",
      out_file: "./logs/mcp-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
  ],
};
