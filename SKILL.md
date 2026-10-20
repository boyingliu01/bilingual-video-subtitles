---
name: bilingual-video-subtitles
version: 1.1.0
description: 为培训/讲解视频自动添加中英文双语字幕（转写+翻译+硬字幕合成），并按主题/角色拆分成 10 分钟以内短视频。Use when 用户要求：给视频加中英字幕、培训视频双语字幕、视频转写并翻译英文字幕、外籍员工字幕支持、把培训视频按主题/角色/章节拆分成短视频、碎片化学习视频切分，或每年制度修订后对培训视频字幕做微调并重新渲染。核心能力：Qwen3-Omni 结合参考文档/制度文档做高精度中文转写（解决 whisper/funasr 专业术语识别差的问题），faster-whisper 提取时间轴，拼音全局对齐，术语表驱动的中译英，输出双语 SRT + 硬字幕视频 + 可编辑字幕工程文件（支持后续微调重渲染，无需重新转写）。命令：python3 scripts/pipeline.py build|render|split|status。
---

# Bilingual Video Subtitles 培训视频双语字幕

完整流水线在 `scripts/pipeline.py`（单一入口，阶段有缓存、可断点续跑）。

> **宿主环境注意**：本 skill 已做跨环境适配，转写后端自动选择（原发沙箱的 Omni
> 网关 / OpenAI 兼容 api / whisper 桥接）。无 Omni 网关且未配 api 后端时走
> **agent 模式**——transcribe/translate 阶段以退出码 3 挂起并写出 manifest，
> 完成对应转写/翻译后重跑 build 续跑。**操作协议与后端配置必读**
> [references/host-adaptation.md](references/host-adaptation.md)。
> 遇到 pipeline exit 3 不是失败，按 manifest 完成对应工作再重跑即可。

## 标准工作流

1. 获取输入：视频文件 + 对应的规章制度/参考文档（.docx/.txt/.md）。
2. 读取参考文档，为其编写术语表 `glossary.txt`（每行 `中文 = English`，10~40 条）。
   术语表决定全片翻译一致性，务必覆盖：制度/规范名称、角色与缩写
   （如 PM=Project Manager）、业务单据或表单名称、内部系统名。
3. 执行构建（前台运行，预留充足 timeout；50 分钟视频全流程约 15~25 分钟）：

```bash
python3 scripts/pipeline.py build \
  --video <视频路径> --doc <制度文档路径> \
  --work <工作目录> --out <输出目录> \
  --glossary <glossary.txt> \
  --hotwords "安全红线,需求单,变更单,PM,QA,SRE" \
  --preview 90
```

4. 生成顺序（每步有缓存，失败后重跑同命令即续跑）：
   setup（切分音频）→ transcribe（Qwen3-Omni 逐字稿）→ timing（whisper 时间轴）
   → align（拼音对齐）→ clean（去口语/切句）→ translate（批量中译英）
   → package（工程文件+SRT+ASS）→ render（预览片+完整版）。
5. 先用 `--preview` 输出的预览片给用户确认效果（可用 media-read 抽帧检查字幕渲染），
   再交付完整版视频、SRT、字幕工程文件。

## 交付物说明

| 文件 | 用途 |
|---|---|
| `<名称>_中英双语字幕_完整版.mp4` | 硬字幕成片（中文上行白字、英文下行浅蓝字，均带半透明黑底背景条，任何背景下可读） |
| `<名称>_中英双语字幕.srt` | 双语字幕文件（可导入播放器/平台） |
| `<名称>_字幕工程.json` | **主数据**：含全部时间轴+中英对照文本。年度微调只改它 |
| `--work` 目录 | 中间产物缓存（转写稿、whisper 时间轴）。保留可避免重复转写 |

## 年度微调（制度修订后更新字幕）

制度微调只影响少量句子时，不要重新转写：
1. 让用户指出需修改的时间段/内容，或直接在上一年 `<名称>_字幕工程.json` 上修改
   `zh`/`en` 文本（时间轴不变时不动 start/end）。
2. 重新渲染：

```bash
python3 scripts/pipeline.py render --project <工程.json> [--video <新视频>] [--srt-only]
```

若新视频对应章节有增删语句：对新视频跑 `build --stage transcribe` 取新逐字稿，
人工比对后用新文本替换工程文件中对应条目，再 render。

## 按主题/角色拆分短视频（碎片化学习）

在已加字幕的完整版视频基础上拆分（保留双语字幕）。

1. 从字幕数据中定位章节边界：用参考文档的角色/章节清单作为关键词，
   在 zh_lines.json（工作目录）中检索章节过渡语，典型模式：
   "第N个角色是X"、"上面是X的职责，要求是"、"X的违规情形是"等过渡句式。
   以过渡句的开始时间为切分点（该句归入下一集）。
2. 约束：单集 ≤10 分钟（硬上限 15 分钟）；章节超限时在章节内部自然分界
   （如"职责/违规行为/认定方法"之间）二次切分；不合并不同角色。
3. 写切分计划 JSON 并执行：

```json
{"base": "视频名", "lead_in": 0.35,
 "chapters": [{"start": 0, "end": 288.4,
               "title": "01_开篇_目的与概述", "note": "内容说明"}]}
```

```bash
python3 scripts/pipeline.py split --plan plan.json \
  --video <带字幕完整版.mp4> --out <输出目录>
```

4. 输出到 `<输出目录>/<视频名>_拆分视频/`：自动命名的各集 +
   `00_拆分目录.txt`（片名、时间段、时长、内容说明）。
5. 抽查验证：每集开头抽帧（ffmpeg 截 PNG 后查看）确认切分点正落在对应主题开场句。

## 关键实现约定（不要偏离）

- qwen-omni 后端必须把参考文档全文注入 prompt 作为术语上下文，且
  `--timeout 900 --max-tokens 8192`（脚本已内置）；api 后端中专用 ASR
  （如 mimo-asr）不吃文本 prompt，术语纠错依赖 clean 阶段对照文档校对。
- whisper 只用于时间轴，其识别文本一律丢弃（中文文本质量差，但时间戳精准）。
- 对齐必须用拼音串全局对齐（同音字错误在拼音层自动消解）；逐句贪心对齐会整体漂移。
- 对齐质量：`align` 输出 qual mean ≥0.75 属正常；<0.6 时先查 whisper 语音覆盖率
  （见 references/troubleshooting.md）。
- 长时 CPU 任务（whisper/render）注意宿主对后台任务的清理策略，必要时前台跑。
- 网络限制：HuggingFace 需镜像，脚本已内置 `HF_ENDPOINT=hf-mirror.com` 与
  `HF_HUB_DISABLE_XET=1`，勿删。

## 排障

见 [references/troubleshooting.md](references/troubleshooting.md)：上传超时、转写单长行、
对齐质量低、字体乱码、渲染失败等场景的处理。
