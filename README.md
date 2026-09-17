# EngAixt

面向 **Windows 上本地离线单机游戏**的 AI 翻译与显示兼容工具。

> ### 官网：<https://engaixt.com/>
>
> 提供**打包好的 Windows 版本，下载解压即可直接使用**——不需要安装 Python，不需要配置任何环境。

通过解析游戏自身的文本格式，将日文/英文提取出来交给 AI 翻译，再通过**静态回填**或**运行时替换**把译文送回游戏里——不是屏幕 OCR，译文能正确出现在对话框、菜单、道具说明里，并且能被游戏原本的排版逻辑处理。

> 这个项目需要你自己合法拥有的游戏本体。它不包含、也不分发任何游戏资源。

---

## 支持的引擎

引擎路线分两类：**静态**（解包 → 回填 → 重新打包）和**实时**（注入进程、挂钩文本渲染、运行时替换显示）。

| 引擎 | 路线 | `support_level` |
|---|---|---|
| RPG Maker (XP/VX/VX Ace/MV/MZ) | 静态 | `stable` |
| Unity / XUnity.AutoTranslator | 运行时 | `stable` |
| Ren'Py | 静态 | `stable` |
| BGI / Ethornell | 静态 + 原生运行时 | `beta` |
| KiriKiri (XP3) | 实时 hook 为主 | `beta` |
| Wolf RPG / Wolf RPG Pro | 静态 + 原生后端 | `beta` |
| Godot (PCK) | 静态 | `beta` |
| Godot（运行时显示层） | 运行时 | `beta` |
| Unity（ARCH000 / Lua 变体） | 静态 | `beta` |
| TyranoScript | 静态 | `experimental` |
| GameMaker | 运行时 | `partial` |
| Unreal | 静态 | `partial` |

级别不是本文档的判断，而是各引擎在代码里的自我声明（`engines/*/`），由
`tests/test_engine_capabilities.py` 约束。未显式声明的引擎沿用
`EngineCapabilities.support_level` 的默认值 `stable`。`partial` 表示只覆盖了
该引擎的部分游戏；`beta` 表示在真实游戏上可用但覆盖面仍在扩大。

KiriKiri 的默认路线是实时 hook；**静态 XP3 补丁是显式开启的实验性路线**，
不是默认行为。

引擎行为集中在 `engines/` 下，每个引擎遵循同一套阶段合同（见 `docs/stage-contracts.md`）。

## 下载与安装

### 方式一：官网下载（推荐）

官网提供**已打包好的 Windows 版本**，下载解压即可使用：

**<https://engaixt.com/>**

不需要安装 Python，不需要配置任何环境。下载后直接运行 `EngAixt.exe` 即可。

首次翻译某个游戏时，缺失的第三方工具会在运行时按需下载（可在设置里关闭自动下载）。

### 方式二：从源码运行

需要 **Python 3.12** 和 Windows。

```powershell
git clone https://github.com/kbq1790595929-code/engaixt.git
cd engaixt
pip install -r requirements.txt
python app.py
```

## 本地模型

EngAixt 支持完全离线的本地推理，不需要任何云端 API：

| 模型 | 体积 | 说明 |
|---|---|---|
| Hy-MT2 1.8B（Q4_K_M） | 约 1.1 GB | 默认选项，显存和内存占用低，适合实时翻译与轻量设备 |
| Hy-MT2 7B（Q4_K_M） | 约 4.6 GB | 官方底模的速度版 |
| Hy-MT2 7B（Q8_0） | 约 7.4 GB | 译文质量更高，建议 12 GB 以上显存的 NVIDIA GPU |
| **EngAixt-7B**（Q4_K_M） | 约 4.6 GB | 在 7B 底模上做的 LoRA 微调，控制符保留更强；建议 8 GB 以上显存 |

模型下载：<https://www.modelscope.cn/models/ENGAOXT/EngAixt-7B-GGUF>

本地推理使用 `llama.cpp`，单实例串行执行——显存占用与并发无关。

## 项目状态

这是一个**在真实游戏上长期使用**的工具，不是演示项目：

- 完整流水线（检测 → 提取 → 翻译 → 回填 → 运行时 → 诊断）在 BGI、RPG Maker、KiriKiri、Wolf、Godot、Unity 上都有实际通关记录。
- 测试套件 716 个用例（最近一次完整运行：**707 通过、9 跳过**），覆盖阶段合同、引擎回归和跨引擎高风险改动。跳过的用例需要已组装的第三方侧车或本机游戏样本。
- 部分路线仍是实验性的：KiriKiri 静态 XP3 补丁、Unreal、GameMaker 的覆盖范围有限，遇到问题请附诊断报告，也可以复制软件内的运行日志。

### 诊断

出问题时请用 `诊断报告` 生成诊断包再提交 issue。诊断只记录事实（版本、配置快照、各阶段计数与耗时、错误栈），**不会包含 API 密钥**，配置快照中的敏感字段按名称过滤。

## 范围与边界

这个项目**只**面向本机已安装的离线单机游戏。明确**不做**：

- 联网游戏、竞技游戏、带反作弊或服务端对抗的游戏
- 绕过反作弊、账号验证、授权、DRM、付费校验或复制保护
- 提供密钥、破解工具、受保护资源的解密结果
- 分发游戏原始资源或修改后的游戏内容
- 任何与翻译/显示无关的注入行为

如果你需要的是绕过保护或破解资源，这个项目不是你要的工具。

## 第三方

本项目使用若干第三方开源项目，完整清单和许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

需要特别说明：

- **UberWolf**（MIT）— Wolf RPG 归档解密。
- **GARbro**（MIT）、**KirikiriTools**（MIT）— KiriKiri 归档处理。
- **BepInEx**、**XUnity.AutoTranslator** — Unity 运行时翻译。
- **Source Han Sans**（SIL OFL 1.1）、**Noto Sans CJK**（SIL OFL 1.1）— CJK 字体 fallback。
- **KiriKiri 钩子路线的行为参考**来自 LunaTranslator / LunaHook（GPLv3）。运行时实现是独立编写的行为等价实现，未复制其源码。

第三方工具二进制在打包时组装，源码树本身不 vendor 它们。

## 反馈与联系

- **官网 / 下载**：<https://engaixt.com/>
- **邮箱**：contact@example.com
- **问题反馈**：<https://github.com/kbq1790595929-code/engaixt/issues>
- **本地模型下载**：<https://www.modelscope.cn/models/ENGAOXT/EngAixt-7B-GGUF>

遇到问题时，优先在 `诊断报告` 里生成诊断包再一起附上——它能省掉大量来回猜测。

## 许可证

**GNU General Public License v3.0** — 见 [`LICENSE`](LICENSE)。

你可以自由使用、修改和分发，个人使用没有任何限制。如果你分发修改后的版本，需要同样以 GPLv3 开源。

