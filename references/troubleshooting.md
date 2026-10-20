# 排障手册

## 转写阶段（transcribe）

**describe.py 上传超时（"The write operation timed out"）**
音频切片过大。pipeline 已按 ~8 分钟切片并压缩为 64k m4a（约 3.5MB/片）。
仍超时时检查 `--work` 中 part*.m4a 大小，必要时 `--chunk-s 300` 重跑。

**某段反复失败**
多为云端限流或网络波动。等待几分钟后重跑 build（自动续跑未完成段）。
若固定某段失败且报 HTTP 400，可能是音频片段命中内容安全策略——
检查该时间段视频内容，必要时微调切片边界（改 `--chunk-s`）。
翻译批次遇到 504/超时同理：重跑即可，已完成批次有断点缓存。

**某段转写稿为单条长行或完全无标点**
Omni 偶尔输出不分行、甚至不带标点的转写稿，会导致对齐时整段被映射为一条
超长字幕行。pipeline 的 align 阶段已内置兜底：检测到标点密度异常时自动调用
文本模型恢复标点，仍异常则按空格/定长切分。若发现成片出现跨几十秒的超长
字幕条，检查 `--work/aligned.json` 中 end-start>30 的行并删除 aligned.json、
zh_lines.json 后重跑 build。

## 对齐阶段（align）

**qual mean < 0.7，或大量 low(<0.5) 行**
排查顺序：
1. whisper 语音覆盖率：统计 whisper_segments.json 各 part 的时长覆盖，
   若某 part 覆盖率 <60%，说明该段是音乐/噪音/静默，属正常（无语音可对齐）。
2. 转写稿单长行/无标点问题：见上文转写阶段的兜底说明。
3. 音视频是否对应同一内容（用户传错文件时先查这里）。

**个别句子时间明显错位**
通常是该句处于 whisper VAD 漏检的静默边缘。可手工在字幕工程.json 中修正
start/end 后 render，无需重跑流水线。

## 翻译阶段（translate）

**批量调用慢**
557 句约 26 批、每批一次 API 调用。属正常，勿并行（有限流风险）。

**术语译法不一致**
补充/修正 glossary.txt（`中文 = English` 每行一条）后
`build --stage translate --force` 即可全部重译（转写不受影响）。

## 渲染阶段（render）

**字幕显示方块/乱码**
libass 按字体族名匹配，探测不到会静默渲染豆腐块。pipeline 已按平台自动探测：
macOS 优先 PingFang SC（系统自带）→ Hiragino Sans GB → STHeiti；Windows 优先
微软雅黑；Linux 优先 Noto Sans CJK SC（缺失时装 `fonts-noto-cjk` 或
`brew install font-noto-sans-cjk-sc` 后重渲染）。

**渲染进程中断**
后台长任务可能被环境清理——务必前台运行。已完成部分不会自动续渲，
删除输出 mp4 后重跑 build（前面阶段全部命中缓存，只重渲染）。

**输出文件过大 / 比源视频大很多**
现版本默认按「源视频码率 × 1.06」单遍 ABR 编码，成片体积应接近源视频。
若仍偏大：① 确认 ffprobe 能读到源视频 bit_rate（个别容器读不到时会退回
crf=22 模式，日志有提示）；② 传 `--crf` 可强制走 crf 模式（23~26 更小）。
历史版本固定 crf=22 + ultrafast，对低码率录屏会导致体积翻倍——已修复。

**macOS 相关**
- 依赖安装不再自动执行 apt-get：缺 ffmpeg/ffprobe 时脚本会打印 brew 命令供手动执行。
- Homebrew Python（PEP 668）下 pip 安装失败会自动重试 `--user`。
- SRT 已改为无 BOM 的纯 UTF-8（BOM 会被 mov_text 混入第一条字幕，破坏软字幕流程）。

## 验证清单（每次交付前）

1. 预览片抽 2~3 帧（含片头与片尾，`ffmpeg -ss <t> -i x.mp4 -frames:v 1 n.png`）
   逐帧查看：中英文两行均正常、无乱码、无重叠。
2. align 日志中 qual mean ≥0.75。
3. 抽查 3~5 句术语句与 glossary 一致。
4. 完整片时长与源视频一致（ffprobe 比对）。
