# Theo 视频总结

自动整理 YouTube 频道 [Theo - t3.gg](https://www.youtube.com/@t3dotgg) 的视频要点（中文）。

在线浏览：https://tifish.github.io/TheoVideoSummary/

## 通过 Browser MCP 访问浏览器

抓取脚本通过 Python MCP 客户端调用 Browser MCP，复用扩展所连接的日常浏览器和 YouTube 登录状态。无需手动开调试端口或退出浏览器。

一次性准备：

1. 安装 Node.js/npm，确保 `npx` 在 PATH 中可用。
2. 在日常浏览器安装并连接 Browser MCP 扩展，登录 YouTube。
3. 使用 Python 3.10+ 创建 `.venv`，执行 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`。脚本用 `npx --yes @browsermcp/mcp@2.5.0` 启动 MCP 客户端服务；首次运行需要下载该包。

项目不再依赖 Python Playwright、Playwright 扩展或 `PLAYWRIGHT_MCP_EXTENSION_TOKEN`。不需要另装自动化浏览器。

检查连接和日常运行：

```powershell
.\.venv\Scripts\python.exe fetch.py --check-browser
.\.venv\Scripts\python.exe fetch.py
.\.venv\Scripts\python.exe build.py
```

任务在后台创建标签页，不切换用户正在使用的标签页；进入视频前安装静音逻辑，获取字幕后立即暂停。任务只关闭自己创建的标签页，不关闭浏览器或共享 Browser MCP 服务。`--login` 会保留登录页供用户操作。扩展未连接、YouTube 登录失效、验证码或限流仍可能中断任务。

字幕仅来自播放器自然发出的响应，不重放字幕请求。长字幕分块传回 Python，并检查长度、事件数量和末尾时间，避免 MCP 输出截断。Python MCP SDK 和 Browser MCP 使用固定版本，升级后需重新验证连接及完整字幕抓取。

- 字幕由 `fetch.py` 抓取（仅保存在本地，不发布）
- 总结由 AI agent 按 [AGENTS.md](AGENTS.md) 的规范生成，存放于 `summaries/`
- `build.py` 生成 `index.html`

总结内容由 AI 根据自动字幕生成，可能存在错误，请以原视频为准。
