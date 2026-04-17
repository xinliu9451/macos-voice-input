# PRD: macOS CLI Voice Input Tool

## Introduction

基于已有的 sherpa_onnx 语音识别代码（`StreamingAsr/infer.py`），构建一个 macOS 系统级语音输入工具。用户按下全局快捷键 `Ctrl+Shift+Space` 开始录音，屏幕浮窗实时显示中间识别结果，再次按下快捷键结束录音，最终识别文本通过剪贴板 + `Cmd+V` 粘贴到当前光标位置。工具以菜单栏图标形式常驻运行，显示待命/录音中状态。

## Goals

- 提供系统级全局语音输入能力，任何应用中均可使用
- 实时显示识别中间结果，让用户了解识别进度
- 识别完成后自动粘贴到光标位置，无需手动操作
- 菜单栏图标提供直观的状态指示和基本控制
- 复用已有 sherpa_onnx + SenseVoice 离线识别能力，无需联网

## User Stories

### US-001: 项目结构与核心模块初始化
**Description:** 作为开发者，我需要建立项目结构和核心模块骨架，以便后续功能模块能有序开发。

**Acceptance Criteria:**
- [ ] 创建 `voice_input/` 包目录，包含 `__init__.py`、`app.py`（主入口）、`asr.py`（识别引擎）、`hotkey.py`（快捷键）、`overlay.py`（浮窗）、`clipboard.py`（剪贴板与粘贴）
- [ ] 创建 `requirements.txt`，包含依赖：`sherpa-onnx`、`sounddevice`、`numpy`、`pynput`、`rumps`、`pyobjc-framework-Cocoa`、`pyobjc-framework-Quartz`
- [ ] `app.py` 中实现基本的应用启动入口（`main()` 函数），能成功运行并退出
- [ ] 现有 `StreamingAsr/infer.py` 不做修改，新代码在 `voice_input/` 中复用其逻辑

### US-002: ASR 识别引擎封装
**Description:** 作为开发者，我需要将 `infer.py` 中的 sherpa_onnx 识别逻辑封装为可复用的类，以便其他模块调用。

**Acceptance Criteria:**
- [ ] `asr.py` 中实现 `ASREngine` 类，封装 recognizer 和 VAD 的创建与配置
- [ ] 支持通过构造函数传入模型路径（默认使用 `StreamingAsr/model/` 下的模型文件）
- [ ] 提供 `start_recording()` 方法：启动录音线程，开始采集音频
- [ ] 提供 `stop_recording()` 方法：停止录音，返回最终识别文本
- [ ] 提供 `get_intermediate_text()` 方法：返回当前中间识别结果
- [ ] 录音期间每 200ms 更新一次中间识别结果（复用 `infer.py` 的 VAD + 识别逻辑）
- [ ] 单元测试：可以实例化 `ASREngine`（模型存在时），调用 start/stop 不崩溃

### US-003: 全局快捷键监听
**Description:** 作为用户，我想按 `Ctrl+Shift+Space` 开始/停止录音，这样我可以在任何应用中触发语音输入。

**Acceptance Criteria:**
- [ ] `hotkey.py` 中使用 `pynput` 实现全局键盘监听
- [ ] 监听 `Ctrl+Shift+Space` 组合键，toggle 录音状态（按一次开始，再按一次停止）
- [ ] 提供回调机制：`on_recording_start` 和 `on_recording_stop` 回调函数
- [ ] 监听运行在独立线程中，不阻塞主线程
- [ ] macOS 辅助功能权限不足时，打印清晰的提示信息指引用户开启权限

### US-004: 屏幕浮窗显示实时识别结果
**Description:** 作为用户，我想在录音时看到实时识别结果浮窗，这样我知道系统正在识别且内容是否正确。

**Acceptance Criteria:**
- [ ] `overlay.py` 使用 PyObjC（NSWindow/NSPanel）创建悬浮窗口
- [ ] 浮窗显示在屏幕顶部居中位置，始终在最前面（floating panel）
- [ ] 浮窗背景半透明圆角，文字为白色，清晰可读
- [ ] 提供 `show()` 方法显示浮窗，`hide()` 方法隐藏浮窗
- [ ] 提供 `update_text(text)` 方法更新显示的识别文本
- [ ] 录音开始时显示浮窗（初始显示"正在聆听..."），录音结束时隐藏

### US-005: 剪贴板粘贴输出
**Description:** 作为用户，我想在录音结束后识别文本自动粘贴到当前光标位置，这样我无需手动复制粘贴。

**Acceptance Criteria:**
- [ ] `clipboard.py` 使用 PyObjC（NSPasteboard）将文本写入系统剪贴板
- [ ] 写入剪贴板后，使用 `pynput` 或 CGEvent 模拟 `Cmd+V` 按键粘贴
- [ ] 粘贴操作在写入剪贴板后延迟 50-100ms 执行（确保剪贴板已更新）
- [ ] 如果识别结果为空，不执行粘贴操作
- [ ] 提供 `paste_text(text)` 公开方法供其他模块调用

### US-006: macOS 菜单栏图标与状态显示
**Description:** 作为用户，我想通过菜单栏图标看到工具的运行状态，并能通过菜单进行基本操作。

**Acceptance Criteria:**
- [ ] 使用 `rumps` 创建菜单栏应用，显示状态图标
- [ ] 待命状态显示一个图标/文字（如 "🎙"), 录音中状态显示不同图标/文字（如 "🔴"）
- [ ] 菜单项包含："开始/停止录音"、分隔线、"退出"
- [ ] 点击"退出"时优雅关闭所有线程和资源
- [ ] rumps 应用运行在主线程，其他模块在子线程中运行

### US-007: 模块集成与端到端流程
**Description:** 作为用户，我想启动一个命令就能使用完整的语音输入功能，从快捷键触发到文字粘贴的全流程。

**Acceptance Criteria:**
- [ ] `app.py` 作为主入口，初始化并连接所有模块：ASREngine、HotkeyListener、Overlay、Clipboard、MenuBar
- [ ] 完整流程：按快捷键 → 显示浮窗 → 开始录音 → 浮窗实时更新识别文本 → 再按快捷键 → 停止录音 → 最终文本粘贴到光标 → 浮窗消失
- [ ] 可通过 `python -m voice_input` 启动
- [ ] 启动时自动加载模型（显示加载中状态），加载完成后进入待命状态
- [ ] `Ctrl+C` 或菜单栏"退出"可优雅终止程序

### US-008: 错误处理与用户引导
**Description:** 作为用户，我希望工具在出错时给出清晰的提示，而不是静默失败。

**Acceptance Criteria:**
- [ ] 模型文件不存在时，启动时报错并提示模型文件路径
- [ ] 麦克风不可用时，提示用户检查麦克风权限和设备
- [ ] 辅助功能权限不足时（pynput 需要），提示用户在系统偏好设置中开启
- [ ] 所有错误信息通过 logging 模块输出，支持 `--verbose` 参数查看详细日志
- [ ] 录音过程中如果 ASR 引擎出错，浮窗显示错误提示并自动恢复到待命状态

## Functional Requirements

- FR-1: 使用 `pynput` 监听全局键盘事件，`Ctrl+Shift+Space` toggle 录音状态
- FR-2: 使用 `sounddevice` 以 16kHz 采样率录制音频，存入线程安全队列
- FR-3: 使用 `sherpa_onnx` SenseVoice 模型进行离线语音识别（复用 `infer.py` 的 VAD + 识别逻辑）
- FR-4: 录音期间每 200ms 更新中间识别结果并推送到浮窗显示
- FR-5: 使用 PyObjC NSPanel 创建 always-on-top 半透明浮窗，显示实时识别文本
- FR-6: 录音结束后，将最终识别文本写入 NSPasteboard，并模拟 `Cmd+V` 粘贴
- FR-7: 使用 `rumps` 创建菜单栏应用，显示待命/录音中两种状态
- FR-8: 程序以 `python -m voice_input` 方式启动，主线程运行 rumps 事件循环
- FR-9: 支持 `--model-dir` 参数指定模型目录路径
- FR-10: 支持 `--verbose` 参数开启详细日志输出

## Non-Goals

- 不实现语言切换或多语言模型选择（使用现有 SenseVoice 模型的默认语言能力）
- 不实现自定义快捷键配置（固定为 `Ctrl+Shift+Space`）
- 不实现录音音频保存/回放功能
- 不实现说话人识别或分离
- 不实现连续听写模式（只支持按键触发的单次识别）
- 不实现 py2app 打包（MVP 阶段以 Python 脚本运行）
- 不实现剪贴板内容保存与恢复
- 不实现自动启动（开机自启）

## Technical Considerations

- **线程模型**：rumps 必须运行在主线程（macOS AppKit 要求）。录音、ASR 识别、快捷键监听各在独立子线程。浮窗 UI 更新需要通过 `performSelectorOnMainThread` 或 `dispatch_async` 回到主线程。
- **模型路径**：默认查找 `StreamingAsr/model/` 目录下的 `model.int8.onnx`、`tokens.txt`、`vad.onnx`。
- **macOS 权限**：需要麦克风权限（首次录音时系统弹窗）、辅助功能权限（pynput 全局键盘监听需要，需用户手动在系统偏好设置中开启）。
- **PyObjC 与 rumps 共存**：rumps 基于 PyObjC，浮窗使用 NSPanel 直接创建。需要确保两者共享同一 NSApplication 实例。
- **依赖版本**：基于现有 `StreamingAsr/.venv` 中的 `sherpa-onnx`、`numpy`、`sounddevice`，新增 `pynput`、`rumps`、`pyobjc-framework-Cocoa`、`pyobjc-framework-Quartz`。

## Success Metrics

- 从按下快捷键到浮窗出现 < 200ms
- 中间识别结果更新延迟 < 500ms
- 从松开快捷键到文字粘贴完成 < 1s
- 程序空闲时 CPU 占用 < 2%
- 识别准确率与直接运行 `infer.py` 一致

## Open Questions

- rumps 菜单栏图标使用 emoji 文字还是自定义图标文件？（MVP 先用 emoji 文字）
- 是否需要支持在浮窗中显示音量/波形指示器？（MVP 不需要）
- 长时间录音（> 30s）是否需要自动分段识别？（当前 VAD 配置 max_speech_duration=8s 会自动分段）
