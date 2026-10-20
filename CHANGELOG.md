# Changelog

本仓库遵循语义化版本（SemVer）。

## [1.1.0] - 2026-10-20

macOS 试用反馈修复 + 渲染体积控制 + 转写质量预警。

### Fixed
- **输出视频体积翻倍**：`render_video` 改为按源视频码率 × 1.06 单遍 ABR
  编码（原固定 crf=22 + ultrafast，对低码率录屏会翻倍），preset 提升至
  veryfast；`cmd_split` 拆分采用相同策略。成片体积现贴近源视频（约 +6%）。
- **SRT 携带 BOM**：改为纯 UTF-8 写出。此前 utf-8-sig 的 BOM 字节会被
  ffmpeg mov_text 混入第一条字幕帧，破坏 macOS 上的软字幕工作流。

### Added
- **whisper 桥接转写质量预警**：agent 后端（本地 whisper）产出转写稿时，
  clean 阶段打印醒目提示——whisper 无法注入参考文档上下文，同音错字
  不可避免，建议配置 api 后端（如 MiMo ASR）或对照参考文档校对。
- **跨平台 CJK 字体探测**：按文件系统探测已安装字体
  （macOS: PingFang SC / Hiragino Sans GB / STHeiti；Windows: 微软雅黑 /
  黑体；Linux: Noto Sans CJK SC 等），探到的族名写入 ASS 样式，避免
  libass 静默渲染豆腐块。
- 单元测试（`tests/`）：标点归一化、时间戳格式、字幕写出（无 BOM + ASS
  转义）等纯函数；`pyproject.toml` 声明测试与覆盖率配置。

### Changed
- `ensure_env` 不再自动执行 `apt-get`：缺 ffmpeg/ffprobe 时按平台打印
  安装命令（macOS: brew）并退出；pip 安装遇到 PEP 668
  （Homebrew/distro Python）自动以 `--user` 重试。
- 代码质量：`l` → `ln` 变量重命名（ruff E741）、lambda 改 def（E731）、
  全量 `ruff format`。
- troubleshooting.md 补充体积异常、macOS 相关、字体缺失条目。

## [1.0.0] - 2026-10-19

首个公开发布：Qwen3-Omni + 参考文档高精度中文转写、faster-whisper 时间轴、
拼音全局对齐、术语表驱动中译英，输出双语 SRT + 硬字幕视频 + 可编辑字幕
工程文件；含 agent 挂起协议（exit 3 + manifest）与跨平台（Win/macOS/Linux）
适配。
