# 阶段合同（Stage Contracts）

管线各阶段的职责边界。**这些边界是冻结的**：任何改动如果违反本文，
先讨论"这个职责是否真的属于这个阶段"，而不是修改测试放行。

每条合同标注了强制执行它的测试。改坏合同 = 测试红。

项目维护工作流见 [workflow-rules.md](workflow-rules.md)。尤其是两条硬规则：

- 遇到“以前可以，现在不行”，第一步看 diagnostics 黑匣子，不先猜、不先改。
- 单个源码文件超过 1000 行，视为维护风险；后续触碰时必须优先评估拆分，不能继续堆新功能。

## detect —— 只做轻量识别

- 不深扫、不读大 exe、不解包、不在游戏目录留痕、对陌生目录不抛异常。
- 高置信度（score ≥ 94）命中后停止后续引擎探测，GUI 不做二次检测。

强制测试：
- `tests/test_stage_contracts.py::test_detect_never_unpacks`
- `tests/test_stage_contracts.py::test_detect_leaves_game_dir_untouched`
- `tests/test_engine_detection_fast_path.py`（不读整个 exe、检测短路）

## unpack / extract —— 只产出标准 TextItem

- 产出 `engines.base.TextItem`，不调用 AI、不部署 hook、不启动游戏。
- `translators` 包对 engines 不可见（连延迟 import 都不允许）。
- 本地翻译代理（`utils.translation_server`）属于运行时部署职责，
  engines 中只允许 `engines/xunity/engine.py`（实时引擎启动代理）与
  `engines/xunity/runtime.py`（缓存生成复用内置词典）接线；新引擎要用，
  先在此文档写明理由，再进 `test_engines_translation_proxy_allowlist` 白名单。

强制测试：
- `tests/test_stage_contracts.py::test_engines_never_call_ai_translators`
- `tests/test_stage_contracts.py::test_engines_never_import_pipeline_orchestration`
- `tests/test_stage_contracts.py::test_engines_translation_proxy_allowlist`

## translate —— 只做翻译、缓存、费用统计

- 只认识 `engines.base` 的数据结构（TextItem），不认识任何具体引擎。
- 不回填、不部署、不启动：`core.pipeline` / `core.launcher` /
  `core.rpgmaker_runtime` / `core.manifest` / `core.frida_injector`
  对 translators 永久禁区。

强制测试：
- `tests/test_stage_contracts.py::test_translators_only_know_engine_base_types`
- `tests/test_stage_contracts.py::test_translators_never_patch_deploy_or_launch`
- `tests/test_pipeline_retry_budget.py`（重试预算、缓存命中跳过）

## patch / repack —— 只做回填

- 回填走引擎的 `repack` / `filter_repack_items`，不引入新的翻译调用。
- KiriKiri 默认走静态脚本/XP3 补丁：由游戏自己的资源加载顺序读取译文，
  不部署 native hook，也不启动第二窗口。需要保护兼容时只能使用静态的
  过滤保留或资源桥接路径，XP3 结果必须通过 manifest 管理。

强制测试：
- `tests/test_pipeline_release_contract.py`（KRKR 静态失败回滚、实时兜底准备、
  xp3 不落散文件、krkrpatch 配置合同）

## runtime / launcher —— 只做部署和启动

- 停进程只按 ExecutablePath 匹配，绝不按 CommandLine 杀进程。
- 卸载只删 manifest 管理的文件，不删游戏本体。
- KiriKiri 的静态入口为 `启动汉化版.bat`，实时入口为
  `启动实时汉化版.bat`，两者不得互相覆盖。静态管线不接受运行时注入选项；
  游戏库和静态完成后的自动启动不得调用历史同名实时启动器。
- GUI 只在检测到 KiriKiri 或 Unity 时显示独立的“实时翻译”按钮，
  不提供静态失败后自动切换或额外的“改用实时翻译”按钮。

强制测试：
- `tests/test_pipeline_release_contract.py`（`_stop_processes_under_dir` 合同）
- `tests/test_rpgmaker_uninstall_manifest.py`（卸载不删错）
- `tests/test_launcher.py`（注入器选择、启动 exe 选择）
- `tests/test_kirikiri_launcher_policy.py`（历史编码启动器识别）
- `tests/test_kirikiri_static_launch.py`（静态启动与实时隔离、启动器卸载登记）
- `tests/test_gui_realtime_policy.py`（实时按钮按引擎显示、独立入口）

## diagnostics —— 只记录事实

- import 面锁死为 stdlib + `utils.logger`；不 import 任何业务模块，
  杜绝"诊断反过来影响业务"的通道。
- engines / translators 不得读取诊断数据做决策（单向黑匣子）。
- 配置快照过滤 secret：API key / token 绝不落盘。

强制测试：
- `tests/test_stage_contracts.py::test_diagnostics_is_fact_recorder_only`
- `tests/test_stage_contracts.py::test_business_modules_never_read_diagnostics_back`
- `tests/test_pipeline_release_contract.py`（config 快照不泄露 secret）

## update / uninstall —— 跨引擎高风险区

- 更新后版本必须变化（`tests/test_app_update.py`）。
- 卸载按 manifest 白名单删除（`tests/test_rpgmaker_uninstall_manifest.py`）。

## 通用铁律

- 所有修改针对引擎类/管线逻辑整体，禁止为单一游戏硬编码
  （见记忆 `pipeline-general-principle`）。
- 任何跨引擎改动（manifest、runtime 文件管理、卸载、检测器、更新器）
  必须跑 BGI / RPGMaker 回归后才能交付。
