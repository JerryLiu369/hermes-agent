# 下一代 Agent 重构实施蓝图与 RFC

> **目标**：为接手本任务的下一代 Coding Agent 提供清晰、模块化、可逐步执行的代码级重构规范，确保上游 GitHub PR 一次性高质量合并。

---

## 一、 任务范围与文件清单

| 任务模块 | 涉及源码路径 | 改造性质 |
| :--- | :--- | :--- |
| **微信平台网关安全加固** | `gateway/platforms/weixin.py` | Bug 修复与网络健壮性增强 |
| **音频入站管线意图解耦** | `gateway/run.py`<br>`gateway/run_inbound.py` | 框架级解耦，杜绝一刀切 STT |
| **新建官方 `audio_analyze` 工具** | `tools/audio_tools.py` (新增)<br>`toolsets.py`<br>`tools/registry.py` | 补齐原生音频多模态工具 |
| **配置项与文档补充** | `hermes_constants.py`<br>`cli-config.yaml.example` | 配置兼容与文档更新 |

---

## 二、 详细实施步骤 (Implementation Roadmap)

### Step 1: 微信平台网关下载加固 (`gateway/platforms/weixin.py`)

1. **修正 URL 解析优先级**：
   - 在 `_download_and_decrypt_media` 中，将 `full_url` 置顶判断。优先使用微信官方动态分配的就近预签名 CDN 节点；仅在未提供 `full_url` 时回退至 `encrypted_query_param` 拼接。
2. **放宽 SSRF 泛域名校验**：
   - 将 `_assert_weixin_cdn_url` 改为后缀泛域名白名单匹配：
     ```python
     if host not in _WEIXIN_CDN_ALLOWLIST and not host.endswith(
         (".weixin.qq.com", ".qpic.cn", ".qlogo.cn")
     ):
       raise ValueError(f"Disallowed host: {host}")
```
3. **增加文件传输超时**：
   - 将 `_INBOUND_MEDIA[ITEM_FILE]` 的超时从 `60.0` 秒调整为 `180.0` 秒；
   - 将 `_download_bytes` 从共享的 `_poll_session` 异步池中剥离，采用独立的线程传输流，避免 socket 竞争导致 `readany()` 饿死。
4. **凭据原子落盘持久化**：
   - 在进入网络下载前，先原子写入 `cache/last_inbound_media.json`，确保网络波动时 AES 密钥与载荷不丢失，支持断点补拉。
5. **杜绝静默吞噬消息**：
   - 在 `_process_message_safe` 中：若用户未发文本但包含媒体项，且媒体下载抛出异常，必须生成 `[附件下载失败：微信传输超时或CDN解析错误]` 错误占位符，触发主流程告警。

---

### Step 2: 框架级音频管线意图解耦 (`gateway/run.py` & `gateway/run_inbound.py`)

1. **严格区分对讲机语音条与文件附件**：
   - 修改 `gateway/run.py` 的 `_event_media_is_stt_input()`：
     ```python
     def _event_media_is_stt_input(event, index: int) -> bool:
       """True when an attachment should enter the automatic STT pipeline."""
       message_type = getattr(event, "message_type", None)
       if message_type in {MessageType.AUDIO, MessageType.DOCUMENT}:
         return False
       # 关键修改：只有真正的语音条(VOICE)才自动触发STT；以文件发送的音频必须保持原样
       return message_type == MessageType.VOICE
```
2. **对齐视频通知机制**：
   - 在 `gateway/run_inbound.py` 中，当检测到音频文件附件（`audio_file_paths`）时，通过 `_prepend_inbound_media_file_notes` 注入文件绝对路径，严禁覆盖 `user_text`，严禁隐匿真实文件：
     ```text
     [The user sent an audio file attachment: 'filename.m4a'. It is saved at: /path/to/file.m4a. If the user's request involves what the audio contains, inspect or process it yourself — for example by passing the path to audio_analyze...]
     ```
3. **新增 STT 控制项**：
   - 在 `config.yaml` 的 `stt` 块中支持 `auto_transcribe_audio_files: false`。

---

### Step 3: 新建官方 `audio_analyze` 原生多模态工具 (`tools/audio_tools.py`)

1. **接口定义（对标 `video_analyze`）**：
   ```json
   {
     "name": "audio_analyze",
     "description": "Analyze an audio file from a local path or URL using a multimodal AI model. Sends raw audio to an audio-capable model (e.g. Gemini 3.8/2.0 Flash) for musical, acoustic, speaker, and emotional understanding without relying on rigid STT.",
     "parameters": {
       "type": "object",
       "properties": {
         "audio_url": {
           "type": "string",
           "description": "Local file path or URL to the audio file."
         },
         "question": {
           "type": "string",
           "description": "Specific question or analysis prompt for the audio (e.g. BPM, key, instruments, emotion, transcription)."
         }
       },
       "required": ["audio_url", "question"]
     }
   }
```
2. **底层实现**：
   - 将本地音频转为 Base64 或标准 Data URL；
   - 构造结构化消息体：
     ```python
     messages = [
         {
             "role": "user",
             "content": [
                 {"type": "text", "text": question},
                 {
                     "type": "input_audio",
                     "input_audio": {"data": audio_b64, "format": "mp3"},
                 },
             ],
         }
     ]
```
   - 调度配置的辅助多模态模型或具备音频能力的主模型完成推理。
3. **注册工具**：
   - 在 `toolsets.py` 中将 `audio_analyze` 注册到内置的 `audio` / `media` toolset 中。
