# EngAixt

本机离线单机游戏的 AI 翻译工具。识别游戏引擎 → 提取文本 → 交给 AI 翻译 → 写回游戏里。

不是屏幕 OCR。译文直接进对话框、菜单、道具说明，跟着游戏自己的排版走。

官网下载：<https://engaixt.com/>

---

## 下载

| 系统 | 版本 |
| - | - |
| Windows 10 / 11 x64 | [官网下载](https://engaixt.com/) |

下载解压到任意目录，双击 `EngAixt.exe` 就行。不用装 Python，不用配环境。

::: warning
别放在 `C:\Program Files` 这类路径下。可能存不了配置和缓存，甚至起不来。
:::

完全免费，没有额度、没有会员码、不用激活。首次翻译某个游戏时，缺的第三方工具会自动下载（设置里可以关掉）。

## 杀毒软件报毒 / 打不开

**这个软件一定会被某些杀毒软件报毒，这是正常的。**

因为要挂游戏进程拿文本，它得往游戏里注入 DLL。注入这个行为本身就是杀毒软件的重点关照对象。

处理办法：把 EngAixt 整个文件夹加进杀毒软件的白名单（Windows Defender 是「病毒和威胁防护 → 排除项 → 添加排除项 → 文件夹」），然后重新解压一次。

被删掉的文件可以在软件的「诊断报告」里看到清单，缺什么补什么。

## 支持的引擎

| 引擎 | 方式 |
| - | - |
| RPG Maker（XP / VX / VX Ace / MV / MZ） | 静态 |
| Unity + XUnity.AutoTranslator | 运行时 |
| Ren'Py | 静态 |
| BGI / Ethornell | 静态 |
| KiriKiri | 运行时 hook |
| Wolf RPG / Wolf RPG Pro | 静态 |
| Godot | 静态 |
| GameMaker | 运行时 |
| Unreal | 静态 |
| TyranoScript | 静态 |

KiriKiri 默认走运行时 hook。静态 XP3 补丁要手动开，是实验性的，别当默认用。

覆盖程度不一样：RPG Maker、Unity、Ren'Py 比较稳；KiriKiri、Wolf、BGI、Godot 可用但还在扩大覆盖面；Unreal 和 GameMaker 只覆盖一部分游戏。碰到问题请附诊断包，我可以按引擎看。

## 本地模型

不想用云端 API 的话，可以装本地模型，完全离线翻译：

| 模型 | 体积 | 说明 |
| - | - | - |
| Hy-MT2 1.8B（Q4_K_M） | 约 1.1 GB | 默认。占用低，适合实时翻译 |
| Hy-MT2 7B（Q4_K_M） | 约 4.6 GB | 底模的速度版 |
| Hy-MT2 7B（Q8_0） | 约 7.4 GB | 质量更好，建议 12 GB 显存以上 |
| EngAixt-7B（Q4_K_M） | 约 4.6 GB | 自己微调的，控制符保留最好，建议 8 GB 显存以上 |

模型下载：<https://www.modelscope.cn/models/ENGAOXT/EngAixt-7B-GGUF>

本地推理走 `llama.cpp`，单实例串行，显存占用跟并发设置无关。

## 从源码跑

需要 Windows 和 Python 3.12。字体、BGI/KiriKiri/Wolf 的原生运行时代码里都带了，克隆后这些引擎即可运行。

未随仓库分发的是第三方二进制：BepInEx 补丁程序集和 XUnity.AutoTranslator 发行包（Unity 路线需要，官方发布包里带着）。要用源码跑 Unity，把官网发布包的 `assets/bepinex_patches/` 和 `assets/XUnity.AutoTranslator-mono-5.6.1.zip` 复制过来即可。

```powershell
git clone https://github.com/kbq1790595929-code/engaixt.git
cd engaixt
pip install -r requirements.txt
python app.py
```

## 出问题了

先跑「诊断报告」生成诊断包，再一起提 issue——比来回猜快得多。

诊断只记事实：版本、配置快照、各阶段计数和耗时、错误栈。**不会包含 API Key**，配置里的敏感字段按字段名过滤掉了。

## 不做的事

这个项目只针对本机已安装的离线单机游戏。不做：

- 联网游戏、竞技游戏、有反作弊的游戏
- 绕过反作弊、账号验证、授权、DRM、付费校验
- 提供密钥、破解工具、解密后的受保护资源
- 分发游戏原始资源或改过的游戏内容

你要的是绕过保护或者破解资源，这个工具不是。

## 第三方

用到的第三方项目清单和许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。几个主要来源：

- **UberWolf**（MIT）— Wolf RPG 归档解密
- **GARbro**、**KirikiriTools**（MIT）— KiriKiri 归档处理
- **BepInEx**、**XUnity.AutoTranslator** — Unity 运行时翻译
- **Source Han Sans**、**Noto Sans CJK**（SIL OFL 1.1）— 中文字体 fallback
- KiriKiri 的 hook 路线按引擎自身的调用约定实现（KAGParser / TextRender / KiriKiriZ 系列签名与调用约定），以真实游戏运行结果验证；不复制任何第三方 hook 项目的源码。

第三方工具的二进制在打包时组装，源码树里不带。

## 许可证

GPLv3，见 [`LICENSE`](LICENSE)。个人用没有任何限制。改了再分发的话，也要以 GPLv3 开源。

---

# EngAixt

AI translation for locally installed offline games. Detects the engine, extracts the
text, sends it to an AI, writes the result back into the game.

This is not screen OCR. Translations go into dialogue boxes, menus and item
descriptions, laid out by the game's own text engine.

Download: <https://engaixt.com/>

---

## Download

| OS | Version |
| - | - |
| Windows 10 / 11 x64 | [Official site](https://engaixt.com/) |

Extract it anywhere and run `EngAixt.exe`. No Python, no setup.

::: warning
Don't put it in `C:\Program Files`. It may not be able to save config and cache files, or start at all.
:::

It's completely free — no quota, no membership code, no activation. On the first
translation of a game, missing third-party tools are downloaded automatically (you
can turn that off in settings).

## Antivirus flags it / it won't start

**Some antivirus will flag this software. That is expected.**

To read text out of a game it has to inject a DLL into the game process, and that
behaviour is exactly what antivirus watches for.

Fix: add the whole EngAixt folder to your antivirus exclusions (on Windows
Defender: Virus & threat protection → Exclusions → Add an exclusion → Folder), then
extract it again.

Files that got deleted are listed in the app's diagnostics report — replace whatever
is missing.

## Supported engines

| Engine | Method |
| - | - |
| RPG Maker (XP / VX / VX Ace / MV / MZ) | Static |
| Unity + XUnity.AutoTranslator | Runtime |
| Ren'Py | Static |
| BGI / Ethornell | Static |
| KiriKiri | Runtime hook |
| Wolf RPG / Wolf RPG Pro | Static |
| Godot | Static |
| GameMaker | Runtime |
| Unreal | Static |
| TyranoScript | Static |

KiriKiri uses the runtime hook by default. The static XP3 patch has to be enabled
manually and is experimental — don't treat it as the default.

Coverage varies. RPG Maker, Unity and Ren'Py are the solid ones. KiriKiri, Wolf, BGI
and Godot work but coverage is still growing. Unreal and GameMaker only cover some
games. If something fails, attach a diagnostics bundle and I'll look at it per engine.

## Local models

If you'd rather not use a cloud API, you can install a local model and translate
fully offline:

| Model | Size | Notes |
| - | - | - |
| Hy-MT2 1.8B (Q4_K_M) | ~1.1 GB | Default. Low overhead, good for realtime |
| Hy-MT2 7B (Q4_K_M) | ~4.6 GB | Speed variant of the base model |
| Hy-MT2 7B (Q8_0) | ~7.4 GB | Better quality, 12 GB+ VRAM recommended |
| EngAixt-7B (Q4_K_M) | ~4.6 GB | Our own fine-tune, best control-marker retention, 8 GB+ VRAM |

Model downloads: <https://www.modelscope.cn/models/ENGAOXT/EngAixt-7B-GGUF>

Local inference runs on `llama.cpp`, single instance and serial — VRAM use is
independent of the concurrency setting.

## Running from source

Windows and Python 3.12. The CJK fonts and the BGI/KiriKiri/Wolf native runtimes are
in the repository, so those engines run right after cloning.

What is not redistributed here are third-party binaries: the BepInEx patched
assemblies and the XUnity.AutoTranslator release package, which the Unity route needs
and the official package carries. To run Unity from source, copy
`assets/bepinex_patches/` and `assets/XUnity.AutoTranslator-mono-5.6.1.zip` out of the
official package.

```powershell
git clone https://github.com/kbq1790595929-code/engaixt.git
cd engaixt
pip install -r requirements.txt
python app.py
```

## Something broke

Run 诊断报告 (Diagnostics) to produce a bundle, then attach it to the issue — it
saves a lot of back and forth.

Diagnostics record facts only: version, configuration snapshot, per-stage counts and
timings, error stacks. **No API keys**; sensitive config fields are filtered by name.

## What this doesn't do

This project is for offline single-player games installed on your own machine. It
does not do:

- online or competitive games, or games with anti-cheat
- bypassing anti-cheat, account checks, licensing, DRM or payment checks
- providing keys, cracking tools, or decrypted protected resources
- distributing original or modified game content

If you want to bypass protection or crack resources, this isn't the tool.

## Third party

Full list and licences in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Main
sources:

- **UberWolf** (MIT) — Wolf RPG archive decryption
- **GARbro**, **KirikiriTools** (MIT) — KiriKiri archive handling
- **BepInEx**, **XUnity.AutoTranslator** — Unity runtime translation
- **Source Han Sans**, **Noto Sans CJK** (SIL OFL 1.1) — CJK font fallback
- The KiriKiri hook route is implemented against the engine's own calling
  conventions (KAGParser / TextRender / KiriKiriZ signature and calling
  contracts) and verified against real games. No third-party hook source is
  copied into this project.

Third-party tool binaries are assembled at packaging time; the source tree doesn't
vendor them.

## License

GPLv3 — see [`LICENSE`](LICENSE). Personal use is unrestricted. If you redistribute a
modified version, it has to be open-sourced under GPLv3 as well.
