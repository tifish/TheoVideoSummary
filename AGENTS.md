# Theo 视频总结 —— 定期任务说明

本目录用于定期抓取 YouTube 频道 **Theo - t3.gg (@t3dotgg)** 的新视频字幕，由 AI agent 生成**中文**要点总结，并生成可浏览的 `index.html`。

当你（AI agent）被要求"运行 / 更新 / 执行任务"时，按下面的步骤做完整一轮。

## 每轮流程

1. **抓取新视频与字幕**

   ```
   python fetch.py
   ```

   - 默认扫描频道最新 15 个视频；要补更多历史视频用 `python fetch.py --limit 40`。
   - 输出末尾的 `PENDING_SUMMARIES` 列表就是本轮要总结的视频（字幕已下载、但还没有总结）。
   - 只想看待办、不联网：`python fetch.py --status`。
   - 出现 `No supported JavaScript runtime` 警告可以忽略。
   - 字幕优先用真实 Chrome（专用配置 `.browser-profile/`，已登录 YouTube 小号）获取：运行时会弹出 Chrome 窗口，属正常现象，不要关闭。日志出现 `browser profile is not logged in` 时，在汇报中提醒用户运行 `python fetch.py --login` 重新登录。
   - 输出 `TRANSCRIPT_BLOCKED` 表示 YouTube 限流了本机 IP 的字幕请求：脚本已自动停止本轮字幕抓取且不计入失败次数，**不要**反复重试，在汇报中说明即可，下次运行会自动重试。

2. **逐个总结**：对每个待总结视频
   - 完整阅读 `transcripts/<id>.txt`（每行以 `[mm:ss]` 或 `[h:mm:ss]` 开头）。字幕很长时分段读完，不要只读开头。
   - 可参考 `data/videos.json` 中该视频的 `title` / `description`。
   - 按下方格式写入 `summaries/<id>.json`（UTF-8）。
   - 视频较多时可以并行交给子 agent 处理，每个子 agent 负责一个视频，并把本文件的格式要求原样传给它。

3. **校验并生成页面**

   ```
   python build.py
   ```

   - 若输出 `[INVALID]`，按提示修正对应的 json 后重新运行，直到 `invalid: 0`。
   - 生成的 `index.html` 可直接用浏览器打开。

4. **提交并推送到 GitHub**（仅在 `build.py` 输出 `invalid: 0` 后执行）

   ```
   git add -A
   git diff --cached --quiet || git commit -m "Update summaries $(date +%Y-%m-%d)"
   git push
   ```

   - 没有变更时不提交。`transcripts/` 已在 `.gitignore` 中排除，**不要**把字幕原文加入仓库。
   - 推送后 GitHub Pages 会在 1–2 分钟内自动更新：https://tifish.github.io/TheoVideoSummary/
   - 推送失败（如网络问题）时重试一次；仍失败则在汇报中说明，不要 force push。

5. **汇报**：简短告诉用户本轮新增了哪些视频（中文标题 + 一句话结论），以及任何字幕失败（`NO_TRANSCRIPT_YET`）的视频。

## 总结文件格式 `summaries/<id>.json`

```json
{
  "id": "nYA0yASgaZI",
  "title_zh": "中文标题（意译，准确传达视频主题，不要标题党）",
  "tldr": "一到两句话：这个视频最核心的结论 / Theo 的立场。",
  "conclusions": [
    { "t": "05:12", "text": "重要结论 1：具体、可验证的判断，带关键数字/对比对象。" },
    { "t": "18:40", "text": "重要结论 2 ……" }
  ],
  "key_points": [
    { "t": "00:00", "text": "按视频时间顺序的内容要点 ……" },
    { "t": "03:25", "text": "……" }
  ],
  "takeaways": [
    "对观众（开发者）可直接执行的建议，没有则为空数组"
  ],
  "tags": ["OpenAI", "Codex", "定价"],
  "sponsor": "赞助商名称（没有则为 null）"
}
```

字段要求：

| 字段 | 要求 |
|---|---|
| `id` | 必须等于文件名中的视频 ID |
| `title_zh` | 必填 |
| `tldr` | 必填，1–2 句 |
| `conclusions` | **最重要的部分**，3–8 条。是 Theo 得出的判断、预测、推荐、批评，而非对内容的复述。每条尽量包含具体对象和依据（模型名、价格、基准分数、对比结果）。`t` 为该结论在视频中出现的时间戳 |
| `key_points` | 5–15 条，按时间顺序概括视频结构，`t` 必填 |
| `takeaways` | 0–5 条可执行建议 |
| `tags` | 2–6 个，优先复用已有标签（见下方），产品/公司名保持英文原名（Claude、OpenAI、Cursor、Next.js …），概念类用中文（定价、基准测试、开发工具 …） |
| `sponsor` | 视频中的赞助商段落只记录在这里，**不要**把广告内容写进结论或要点 |

时间戳 `t` 必须取自字幕行首的真实时间，格式 `mm:ss` 或 `h:mm:ss`；索引页会把它变成跳转到 YouTube 对应时刻的链接。

写作要求：

- 全部用简体中文；专有名词、模型名、产品名保持英文。
- 区分「Theo 的观点」与「事实陈述」，观点用"Theo 认为 / 判断 / 推荐"等措辞。
- 忠于字幕，不要编造字幕中没有的数字或结论；字幕自动生成可能有拼写错误（如人名、产品名），按上下文纠正为正确写法。
- 不要输出空泛的结论（如"AI 发展很快"）。

查看已有标签以保持一致：

```
python -c "import json,glob,collections;c=collections.Counter(t for f in glob.glob('summaries/*.json') for t in json.load(open(f,encoding='utf-8'))['tags']);print(c.most_common())"
```

## 文件说明

| 路径 | 说明 |
|---|---|
| `fetch.py` | 列出频道新视频、下载字幕，维护 `data/videos.json` |
| `browser_transcript.py` | 用已登录的真实 Chrome 打开视频、开启字幕并截获字幕数据（`fetch.py` 优先使用） |
| `build.py` | 校验 `summaries/*.json` 并生成 `index.html` |
| `next_run.py` | 随机生成下一次定时运行时间（06:30–08:00，排除当天），供定时任务结束时重新排期 |
| `templates/index.template.html` | 索引页模板（数据由 build.py 内嵌） |
| `data/videos.json` | 视频元数据与状态，**不要手工删除** |
| `transcripts/<id>.txt` | 字幕原文 |
| `summaries/<id>.json` | 总结（agent 写入） |
| `index.html` | 生成的索引页 |

依赖：Python 3.10+，`pip install -U yt-dlp youtube-transcript-api playwright`（YouTube 变更后抓取失败时，先升级这些包），以及本机安装的 Google Chrome。首次使用运行 `python fetch.py --login` 登录 YouTube 小号。

重新总结某个视频：删除 `summaries/<id>.json` 后重新跑流程即可。
字幕连续失败 5 次（被限流不计入）的视频会被跳过；要重试，把 `data/videos.json` 中该视频的 `transcript_attempts` 改为 0。
