# bilingual-video-subtitles

为培训/讲解视频自动添加**中英文双语字幕**（转写 + 翻译 + 硬字幕合成），并可按主题/角色拆分为 10 分钟以内短视频。

流水线八阶段，全部带缓存、可断点续跑：

```
setup(切分音频) → transcribe(语音转写) → timing(whisper时间轴) → align(拼音全局对齐)
→ clean(去口语/切句) → translate(术语表驱动中译英) → package(工程文件+SRT+ASS) → render(硬字幕渲染)
```

设计目标是**年度制度修订场景**：制度微调后只改字幕工程文件再 `render`，无需重新转写。

## 交付物

| 文件 | 用途 |
|---|---|
| `<名称>_中英双语字幕_完整版.mp4` | 硬字幕成片（中文白字上行、英文浅蓝字下行，均带半透明黑底条） |
| `<名称>_中英双语字幕.srt` | 双语 SRT，可导入播放器/平台 |
| `<名称>_字幕工程.json` | **主数据**：全部时间轴 + 中英对照，年度微调只改它 |
| `--work` 目录 | 中间产物缓存，保留可避免重复转写 |

## 快速开始

```bash
# 依赖：ffmpeg（PATH 可见）、Python 3.10+；faster-whisper/pypinyin/python-docx 首次运行自动 pip 安装
python scripts/pipeline.py build \
  --video 培训视频.mp4 --doc 制度文档.docx \
  --work ./work --out ./out \
  --glossary glossary.txt \
  --hotwords "质量红线,研发单,事务单" \
  --preview 90
```

术语表 `glossary.txt` 每行一条 `中文 = English`（10~40 条），决定全片翻译一致性。

子命令：`build`（全流程，可 `--stage` 停在指定阶段）｜`render`（从工程文件重渲染）｜`split`（按计划 JSON 拆分短视频）｜`status`（查看阶段进度）。详见 [SKILL.md](SKILL.md)。

## 转写后端（三选一，自动探测）

| 后端 | 触发条件 | 说明 |
|---|---|---|
| `qwen-omni` | 存在同级 `media-read/scripts/describe.py` | 原发沙箱宿主的 Omni 网关路径，参考文档全文注入 prompt，术语精度最高 |
| `api` | 设了 `SUBTITLE_API_BASE` + `SUBTITLE_API_KEY` + `SUBTITLE_MODEL_OMNI` | OpenAI 兼容端点收 base64 音频。**实测推荐：小米 MiMo `mimo-v2.5-asr`**（约 ¥0.5/小时音频，中文普通话接近零错误）；阿里 DashScope qwen3-omni 亦可 |
| `agent` | 以上皆无 | 本地 faster-whisper 桥接（`scripts/agent_transcribe.py`），零成本但同音字需人工校对 |

环境变量一览：

```bash
SUBTITLE_ASR_BACKEND=auto|qwen-omni|api|agent
SUBTITLE_API_BASE=https://api.xiaomimimo.com/v1   # api 后端
SUBTITLE_API_KEY=...                               # api 后端
SUBTITLE_MODEL_OMNI=mimo-v2.5-asr                  # api 后端（含 mimo 时自动走专用 ASR 调用形状）
SUBTITLE_MODEL_TEXT=...                            # 翻译用文本模型（不设则翻译由 agent 完成）
SUBTITLE_WHISPER_MODEL=base                        # 或本地 faster-whisper 模型目录（网络受限时）
```

## Agent 宿主集成（Claude Code / WorkBuddy / Codex CLI 等）

本仓库是一个标准的 [Agent Skill](https://agentskills.io)（`SKILL.md` + frontmatter），放入宿主的 skills 目录即可被智能体自动发现并调用。在无法听音频/无 Omni 模型的宿主上，流水线的 transcribe/translate 阶段会以退出码 3 挂起并写出 manifest，由宿主 agent 完成后重跑续传——协议详见 [references/host-adaptation.md](references/host-adaptation.md)。

## 已知边界与坑（跨环境实测记录）

- whisper 只贡献时间轴，其识别文本一律丢弃；中文文本精度取决于转写后端。
- 拼音全局对齐消解同音字错位；`align` qual mean ≥0.75 正常，<0.6 查排障手册。
- Windows：ASS 渲染须以 ASS 所在目录为 cwd 传相对文件名（ffmpeg ass 滤镜解析盘符冒号绝对路径必挂，已内置处理）。
- huggingface_hub ≥1.0 的 xet 协议会绕过 HF 镜像：脚本已强制 `HF_HUB_DISABLE_XET=1`，仍失败则手动下载模型目录后用 `SUBTITLE_WHISPER_MODEL` 指过去。
- PyAV 需 <17（av 19 与 faster-whisper 不兼容）。
- 更多见 [references/troubleshooting.md](references/troubleshooting.md)。

## 致谢

原始流水线诞生于某企业内部 AI 办公平台（Qwen3-Omni 驱动），2026-10 移植为宿主无关的通用版本，新增多后端转写、Windows 适配与 agent 挂起协议。

## License

[MIT](LICENSE)
