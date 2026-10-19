# 宿主适配说明（跨环境移植记录）

本 skill 最初运行在一个带 Qwen3-Omni 网关的 Linux 沙箱宿主上，依赖两处该环境
特有的东西：

1. `media-read/scripts/describe.py` —— Omni 模型网关入口（转写 + 文本 chat）。
   其他宿主没有这个模型/脚本。
2. Linux 沙箱假设 —— apt-get 装 ffmpeg、fonts-noto-cjk、`fc-list`。

## 改造内容（scripts/pipeline.py）

- **ASR 后端三选一**（环境变量 `SUBTITLE_ASR_BACKEND`，默认 auto）：
  - `qwen-omni`：describe.py 存在时自动使用（原行为，兼容原沙箱宿主）。
  - `api`：设了 `SUBTITLE_API_BASE` + `SUBTITLE_API_KEY` + `SUBTITLE_MODEL_OMNI`
    时自动使用——OpenAI 兼容多模态接口直接收 base64 音频。
  - `agent`：**通用宿主的兜底路径**。流水线在 transcribe / translate 阶段
    以退出码 3 挂起，写出 manifest，由宿主 agent（若其模型能听音频）完成
    转写/翻译后重跑 build 续跑；不能听音频的宿主用 whisper 桥接脚本（见下）。
- **Windows 适配**：ensure_env 不再 apt-get；检查 PATH 上的 ffmpeg、
  Windows\Fonts 下的 CJK 字体；ASS 字体名自动切 Microsoft YaHei。
- **文本侧（翻译/补标点）**：优先 describe.py；否则用
  `SUBTITLE_API_BASE/SUBTITLE_API_KEY`（模型名 `SUBTITLE_MODEL_TEXT`）；
  都没有则 translate 阶段走 agent 模式（exit 3 + translate_manifest.json）。

## agent 模式操作协议（宿主 agent 必读）

### transcribe 挂起时（exit 3, parts_manifest.json）

多数编码类 agent 宿主的文件读取工具**不能读音频文件**（返回二进制不可展示），
主 agent 无法直接听音转写。此时转写通路退化为本地 faster-whisper 桥接：

```bash
python scripts/agent_transcribe.py --work <workdir> --hotwords "<热词>"
# 然后重跑同一条 build 命令 → transcribe 命中缓存继续
```

桥接脚本内部就是 whisper 转写并写入 transN.txt。文本精度弱于 Omni 级模型
（同音字错误靠 hotwords/initial_prompt 部分缓解），后续 clean/人工校对阶段
应结合参考文档纠正术语错字（实测 whisper base 会把"自检"写成"字件"等）。
要恢复 Omni 级精度，配任意真正的 OpenAI 兼容音频端点即可
（SUBTITLE_ASR_BACKEND=api + SUBTITLE_API_BASE/KEY + SUBTITLE_MODEL_OMNI）。

### translate 挂起时（exit 3, translate_manifest.json）

manifest 里有 `lines_file`（zh_lines.json 绝对路径）与 `pending_idx`。分批
（每批 ≤25 行）读入待译行，按 glossary 翻译成英文，把 `"en"` 字段写回
zh_lines.json 对应条目（用 Python 脚本读写 JSON，勿手改大文件），
每批写完即保存（断点续译）。全部完成后重跑 build。

## 音频后端选型实测（2026-10）

### 推荐：Xiaomi MiMo ASR（mimo-v2.5-asr）

公开 API，49s 测试音频转写**零错误**（whisper base 同音频错 6 处），原生标点、
中英混排准确、秒级返回。官方称 Open ASR Leaderboard 领先级，支持方言/噪声/
代码切换。定价约 ¥0.5/小时音频——50 分钟视频约 ¥0.4。

配置（pipeline.py 已内置 mimo 专用调用形状 `_mimo_asr_call`）：

```bash
export SUBTITLE_ASR_BACKEND=api
export SUBTITLE_API_BASE="https://api.xiaomimimo.com/v1"   # 详见厂商文档
export SUBTITLE_API_KEY=<your key>
export SUBTITLE_MODEL_OMNI=mimo-v2.5-asr
```

限制与坑：
- **请求不能带任何文本部分**（网关自注入 prompt；带 text 块报
  "ASR request must not include text parts"）→ **无法注入参考文档术语上下文**，
  这是与 Omni 级模型的本质差距；专有名词错了要在 clean 阶段对照文档纠正。
- 仅 wav/mp3；base64 后 ≤10MB → m4a 自动转 32k mono mp3（~17min/片上限，
  长视频用 `--chunk-s 900`）；max output 4096 tokens（约 2700 汉字/片）。
- 输出常为单段无换行文本 → align 的长行切分兜底自动处理（实测 qual 0.97）。
- 限流约 100 RPM。语言可用 asr_options.language=zh 显式指定。
- 翻译端点不能用 ASR 模型（chat 模型名不同）；不设 SUBTITLE_MODEL_TEXT 时
  translate 走 agent manifest 模式即可。

### 备选：阿里云百炼 DashScope compatible-mode

qwen3-omni-flash / qwen3.5-omni-plus 真全模态，可 system prompt 注入术语
（正是原版能力），需 stream=True + modalities=["text"]，音频约 ¥0.66/小时。
注意：pipeline 通用 `_api_audio_chat` 分支未实现流式，接 DashScope 前需小改。
fun-asr/paraformer 系列走 `/audio/transcriptions`，另一套接口。

### 备选：火山方舟 Doubao-Seed-ASR

专业 ASR（中文口语口碑好），但走异步 Submit/Poll REST 或
`/audio/transcriptions`（仅 pcm_s16le），非 chat/completions 形状，需适配层。

### 避坑：企业 LLM 网关普遍"有 omni 之名无 omni 之实"

曾对某企业内部 LLM 聚合网关（模型列表含一个叫 gpt-4-omni 的条目）做过穷举
探测（20+ 请求形状）：该模型底层是纯文本+图片的 gpt-4o；`input_audio` 参数
校验层真实存在，但通过校验后路由到的全是文本-only 后端（礼貌拒答）；唯一
像音频路由的守卫强制要求 TTS 输出参数且拒绝一切携带输入音频的字段。
结论：**看到 "omni/audio" 命名的模型先做最小样本实测**（一段 30s 音频 +
最简 input_audio 请求），不要凭模型名接入流水线。

另注：主流编码类 agent IDE（Trae / Qoder / CodeBuddy 等）内置模型全部是
编码向 CHAT 型，无音频输入通路；其宣传的"语音输入"是输入法级 STT，不是
模型能力，不可用于本流水线。

## 通用环境要点（均为实测）

- 依赖：`pip install faster-whisper pypinyin python-docx`；**PyAV 需 <17**
  （av 19 与 faster-whisper 1.2.x 不兼容，报 `metadata_errors` TypeError →
  `pip install "av<17"`）。
- **whisper 模型下载坑（两个叠加）**：huggingface_hub ≥1.0 的 xet 协议会绕过
  HF 镜像直连 cas-bridge.xethub.hf.co（国内常超时）。对策：① 脚本已强制
  `HF_HUB_DISABLE_XET=1`；② 仍失败则手动下载 5 个文件（config.json /
  tokenizer.json / vocabulary.txt / preprocessor_config.json / model.bin）
  到本地目录，设 `SUBTITLE_WHISPER_MODEL=<本地目录>`。
- **ASS 渲染路径坑**：部分 Windows ffmpeg 构建的 ass 滤镜解析含盘符冒号/
  非 ASCII 的绝对路径必挂（报错形如 Could not create track / parsed as
  image size）。已修：render_video 以 ASS 所在目录为 cwd、传相对文件名。
  勿改回绝对路径。
- ASS 字体自动切 Microsoft YaHei（Windows）/ Noto Sans CJK SC（Linux）。
- 渲染验证抽帧：`ffmpeg -ss <t> -i out.mp4 -frames:v 1 x.png` 后用宿主
  agent 的图片查看能力确认（多数宿主能看图，只是不能听音频）。
- 端到端回归项：build（含 exit 3 两段挂起续跑）、render、split、status；
  双语字幕硬渲染（中文白上行 + 英文浅蓝下行带黑底条）抽帧确认。
