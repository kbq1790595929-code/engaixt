# EngAixt

EngAixt 是面向 Windows 本地离线单机游戏的 AI 翻译工具：识别游戏引擎，
提取文本，交给在线或本地模型翻译，再通过静态补丁、运行时 Hook 或显示层
把译文送回游戏。

这不是屏幕 OCR。译文会进入游戏自己的对话框、菜单和说明文字，并尽量保留
控制符、占位符、换行和原有格式。

## 支持的引擎

| 引擎 | 主要方式 |
| --- | --- |
| RPG Maker（XP / VX / VX Ace / MV / MZ） | 静态提取与回写 |
| Unity | 运行时注入，部分资源支持静态处理 |
| Ren'Py | 静态提取与回写 |
| BGI / Ethornell | 静态提取与回写 |
| KiriKiri / 吉里吉里 | 运行时 Hook，实验性静态 XP3 回写 |
| Wolf RPG / Wolf RPG Pro | 静态提取与回写 |
| Godot | 静态处理，部分资源使用运行时方案 |
| GameMaker | 运行时翻译 |
| Unreal Engine | 静态文本资源处理 |
| TyranoScript / TyranoBuilder | 静态提取与回写 |

不同游戏的引擎版本、资源格式和保护方式不同，实际覆盖程度会有差异。
遇到兼容性问题时，请先在软件中生成诊断报告，再附上相关日志反馈。

## 使用方式

Windows 10 / 11 x64 用户可以从项目发布页或官网获取打包版本，解压后运行
`EngAixt.exe`。从源码运行需要 Python 3.12：

```powershell
python -m pip install -r requirements.txt
python app.py
```

在线翻译需要用户自行配置模型服务的 API Key；也可以安装可选的本地模型组件，
在本机完成翻译。请求由 EngAixt 直接发送到用户选择的模型服务。

## 项目范围

EngAixt 面向用户本机已安装的离线单机游戏，不用于联网游戏、反作弊系统、
账号或授权绕过、DRM、受保护资源解密，或分发游戏原始资源。

项目不包含游戏资源、提取出的游戏文本、翻译数据库、模型权重或每个游戏的密钥。
运行时下载的第三方工具和用户数据保存在源码目录之外。

## 第三方与许可证

项目采用 GPLv3，详见 [`LICENSE`](LICENSE)。第三方项目和运行时资产的许可信息
见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 以及随资产提供的许可证文件。

## 官网

用户下载页：<https://engaixt.com/>
