# Theo 视频总结

自动整理 YouTube 频道 [Theo - t3.gg](https://www.youtube.com/@t3dotgg) 的视频要点（中文）。

在线浏览：https://tifish.github.io/TheoVideoSummary/

## 自动访问 Edge

抓取脚本通过 Edge 的 Playwright 扩展复用日常浏览器登录状态，无需手动开调试端口或每轮退出浏览器。

一次性准备：

1. 在 Edge 安装 Playwright 扩展并登录 YouTube。
2. 将扩展状态页提供的 token 设置为用户环境变量 `PLAYWRIGHT_MCP_EXTENSION_TOKEN`，然后重启 T3。不要将 token 写入项目文件或提交到 Git。
3. 使用 Python 创建 `.venv`，执行 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`。项目使用 Python Playwright 自带的 Node 驱动，无需另装浏览器或 Node/npm 包。

检查连接和日常运行：

```powershell
.\.venv\Scripts\python.exe fetch.py --check-browser
.\.venv\Scripts\python.exe fetch.py
.\.venv\Scripts\python.exe build.py
```

任务只关闭自己创建的标签页，保留用户的 Edge。token 缺失时会直接报告错误，不弹出等待确认的连接流程。YouTube 登录失效、验证码或限流仍可能中断任务。

扩展桥接使用 Playwright 版本相关接口，因此依赖固定版本；升级后需重新验证连接及字幕抓取。

- 字幕由 `fetch.py` 抓取（仅保存在本地，不发布）
- 总结由 AI agent 按 [AGENTS.md](AGENTS.md) 的规范生成，存放于 `summaries/`
- `build.py` 生成 `index.html`

总结内容由 AI 根据自动字幕生成，可能存在错误，请以原视频为准。
