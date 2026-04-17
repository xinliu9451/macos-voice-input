# Voice Input - macOS 语音输入工具

基于 sherpa_onnx SenseVoice 的 macOS 系统级语音输入工具。按快捷键说话，识别结果自动粘贴到光标位置。

## 功能

- **快捷键触发**：Ctrl+Shift+Space 切换录音（按一次开始，再按一次结束）
- **实时反馈**：录音期间屏幕浮窗显示中间识别结果
- **系统级输入**：识别完成后自动通过剪贴板粘贴到当前光标位置
- **菜单栏状态**：macOS 菜单栏图标显示待命/录音中状态
- **VAD 分段粘贴**：检测到语音段落结束时自动粘贴，无需等待整段录音结束
- **离线识别**：基于 sherpa_onnx，无需网络

## 安装依赖

```bash
cd ~/Projects/ralph
pip install -r requirements.txt
```

依赖列表：
- `sherpa-onnx` — 语音识别引擎
- `sounddevice` — 麦克风录音
- `numpy` — 音频数据处理
- `pynput` — 全局快捷键监听
- `rumps` — macOS 菜单栏图标
- `pyobjc-framework-Cocoa` — 屏幕浮窗
- `pyobjc-framework-Quartz` — 屏幕浮窗

## 模型文件

模型文件位于 `StreamingAsr/model/`，包含：

```
StreamingAsr/model/
├── model.int8.onnx   # SenseVoice 识别模型
├── tokens.txt        # 词表
└── vad.onnx          # Silero VAD 模型
```

默认从该路径加载，也可通过 `--model-dir` 指定其他路径。

## macOS 权限设置

首次运行前需要授予两项权限：

1. **辅助功能权限**（全局快捷键必需）：
   系统设置 > 隐私与安全性 > 辅助功能 → 添加你的终端应用（如 Terminal.app / iTerm2）

2. **麦克风权限**（录音必需）：
   系统设置 > 隐私与安全性 > 麦克风 → 允许终端应用访问

## 使用方法

### 启动

```bash
python -m voice_input
```

启动后：
- 菜单栏出现麦克风图标（待命状态）
- 终端显示日志输出
- 模型加载完成后提示「全局快捷键监听已启动」

### 自定义参数

```bash
# 指定模型目录
python -m voice_input --model-dir /path/to/model/

# 开启详细日志
python -m voice_input --verbose

# 仅检查环境是否正常（不启动服务）
python -m voice_input --check
```

### 录音输入

1. 按 **Ctrl+Shift+Space** → 开始录音，菜单栏图标变为录音状态，屏幕浮窗显示
2. 对着麦克风说话 → 浮窗实时显示中间识别结果
3. 说话过程中 VAD 检测到句子结束时会**自动粘贴**该段文字
4. 再按 **Ctrl+Shift+Space** → 停止录音，粘贴剩余文字，浮窗消失

### 退出

- 菜单栏图标 → 点击「退出」
- 或在终端按 **Ctrl+C**

## 模块结构

```
voice_input/
├── __init__.py     # 包声明
├── __main__.py     # python -m 入口
├── app.py          # 主应用，集成所有模块
├── asr.py          # ASR 识别引擎（SenseVoice + VAD）
├── hotkey.py       # 全局快捷键监听（pynput）
├── overlay.py      # 屏幕浮窗（PyObjC/AppKit）
├── clipboard.py    # 剪贴板粘贴（pbcopy + Cmd+V）
└── menubar.py      # 菜单栏图标（rumps）
```
