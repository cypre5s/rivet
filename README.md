# Rivet — Lightweight. Transactional. Verifiable.

技术栈：Python 3.13、uv、TypeScript、Bun、OpenTUI、SQLite、Git Worktree、bubblewrap。

运行 `rivet` 进入 OpenTUI。按 `Ctrl+G`（或输入 `/config`）可快速配置 API
地址、默认模型、同一 Key 可用的模型目录和运行预算；按 `Ctrl+K`（或输入
`/model`）可在已配置模型间切换。

API Key 只保存在当前 Python Worker 的内存中，不写入 Rivet TOML、Trace 或
界面历史；API 地址、模型与预算原子保存到 `$XDG_CONFIG_HOME/rivet/config.toml`。
也可用 `DEEPSEEK_API_KEY`、`RIVET_MODEL` 和逗号分隔的 `RIVET_MODELS` 提供配置。

多格式读取可在 CLI 或 TUI 中使用 `/read` 对应参数：图片和扫描 PDF 的 OCR
要求 `tesseract` 位于 `PATH`；音视频转录要求安装 `transcription` 可选依赖，
并把已下载的 faster-whisper 模型放在
`$XDG_DATA_HOME/rivet/models/faster-whisper-tiny`（未设置 XDG 目录时使用
`~/.local/share`）。也可用
`RIVET_TRANSCRIPTION_MODEL_PATH` 指向包含 `model.bin` 的本地模型目录。Reader
只加载本地模型，不会在读取文件时隐式联网下载；依赖或模型缺失时返回明确的
`DEGRADED` 状态。
