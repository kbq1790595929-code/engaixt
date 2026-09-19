功能支持：
- 自动识别游戏引擎和资源类型。
- 提取游戏文本，保留角色名、控制符、占位符和格式。
- 批量调用 AI 模型翻译，支持并发、重试、缓存和断点继续。
- 支持在线模型：DeepSeek、Qwen、智谱 GLM、Kimi、豆包、OpenAI、Claude。
- 支持本地模型：Hy-MT2，以及自训练的 EngAixt-7B GGUF。
- 支持静态翻译：提取文本、翻译、回写资源、重新封包或生成汉化补丁。
- 支持运行时翻译：通过 Hook 或插件在游戏运行时替换文本。
- 支持卸载和恢复自己生成的补丁、启动器及运行时文件。
- 提供日志、诊断报告、翻译缓存和配置管理。
目前覆盖的主要引擎包括 RPG Maker、Unity、Ren’Py、KiriKiri、BGI/Ethornell、Wolf RPG、Godot、GameMaker、Unreal 和 TyranoScript。
需要注意的是，各引擎的支持程度不同。EngAixt是“自动化翻译工作流和兼容层”，不是保证所有游戏、所有加密格式都能一次成功的万能解包器；复杂游戏仍可能需要对应的外部工具或运行时方案。

共支持下列模型：
云端模型：
- DeepSeek
- 通义千问 Qwen
- 智谱 GLM
- Moonshot / Kimi
- 豆包 / 火山方舟
- OpenAI GPT
- Anthropic Claude
其中 DeepSeek、Qwen、智谱、Kimi、豆包主要通过 OpenAI 兼容接口接入；模型名称可以在配置中更换，但需要对应平台的 API Key。
本地离线模型：
- Hy-MT2 1.8B Q4
- Hy-MT2 7B Q4
- Hy-MT2 7B Q8
- EngAixt-7B Q4，也就是自训练的 engaixt7bq4
本地模型通过 llama.cpp 运行。




本工具使用到的所有开源项目：
- GARbro、KirikiriTools、msg_tool：KiriKiri/XP3 游戏
- RPGMakerDecrypter、rpgmad：RPG Maker
- UberWolf：Wolf RPG
- UnityPy、AssetRipper：Unity
- GDRE Tools、GDSDECOMP：Godot
- unrpa、unrpyc：Ren'Py
- UnrealPak、FModel：Unreal
- Frida、BepInEx、XUnity.AutoTranslator：运行时注入
以及部分自研的引擎适配和原生运行代码。
