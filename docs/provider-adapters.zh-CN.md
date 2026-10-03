# 内部 Provider Adapter 扩展合同

[English](provider-adapters.md)

共享 CLI 通过注册表分派 provider，再发布其 schema-3 package spec。
源选择、原生解析、隐私、覆盖范围和渲染由 adapter 负责。
这是小型内部静态注册表，不引入 entry points、动态发现、运行时插件安装或
统一 conversation ontology。注册项只有 `codex`（OpenAI Codex）、
`claude`（Claude Code）、`kimi`（Kimi Code）和 `zcode`（ZCode）。
`kimi` 的含义保持为 Kimi Code。

`src/codex_preserve/_provider_registry.py` 定义不可变的 `ProviderAdapter`
entry 和只读的 `PROVIDER_REGISTRY`。每个 entry 负责：

| 字段 | 职责 |
| --- | --- |
| `key`, `display_name` | 稳定的 CLI/package identity 与显示名称 |
| `description`, `example` | Provider parser help 与示例元数据 |
| `configure_arguments(parser)` | 原生选择参数 |
| `select_and_parse(options)` | 明确选择本地源，返回 provider 自己的安全原生结果 |
| `build_spec(source)` | 原生渲染与 receipt 投影为 `V3PackageSpec` |
| `parser_factory()`（可选） | 复用已有复杂 parser，包括 Codex 的参数分组与默认值 |
| `prepare_options(options, parser)`（可选） | 准备参数并拒绝不支持的组合 |
| `candidate_listing(options)`（可选） | 返回已清理候选列表，或返回 `None` 继续导出 |

普通 entry 自动获得 `--output-dir`、`--quiet` 和 `--stdout-receipt`。
parser factory 已拥有这些参数，注册表只设置公共 output 默认值，不重复添加参数。
参数准备先于候选列表执行。候选结果即使为空，也会在源解析和 package 发布前结束分派。

`ProviderExportBlocked` 只携带固定或隐私清理后的说明，返回 exit 2。
未预期异常不会被转换成成功导出。source 不稳定的 spec 在 writer 执行前被阻止。
Codex 保留原有复杂 parser、时间戳准备、候选列表和选择失败的隐私清理，
以及 legacy ZIP 参数拒绝行为。

package writer、manifest、verifier 和 derived ZIP 流程共用。
schema 保持 3.0，legacy 2.1/2.2 verification 保持支持。
每个 provider 保留自己的源数据和对话语义。

## 在未来单独限定范围的变更中接入 Provider

1. 先证明本地 canonical persisted transcript source 和准确的 selection identity。
   调查只读且只检查必要结构。公开新导出命令前，确认该来源支持可读会话保存。
   不用 network/model 调用补齐缺失内容。
2. 新增 source/parser module，只返回已识别的安全信息。
   不保留 reasoning、raw tool body、browser/account/credential 数据、
   私人文件正文或 unknown raw value。如实定义稳定性和摘要范围；
   不支持的结构产生 diagnostics，不携带 raw value。
3. 新增 projection/renderer，返回 `V3PackageSpec`。
   共用 package envelope，同时保留原生对话语义和真实覆盖范围。
   文档说明 digest scope；摘要不证明 authenticity、UI 完整或执行成功。
4. 增加一个静态 registration entry，配置原生参数和 parse/spec hook。
   共享 CLI 不应需要新增 provider-specific dispatch branch。
   明确更新公共 help 表和示例，同时检查既有命令兼容性。
5. 加入手写 synthetic fixtures，测试 selection、parser schema、稳定/变化源、
   隐私、hook 分派、CLI 行为和 schema-3 verification。
   比较既有 provider 变更前后的 help、canonical package bytes、
   normalized CLI output 和参数错误/exit 行为。
6. 用中英文说明 source、selection、coverage 和 privacy 边界。
   运行完整测试、legacy verifier、public hygiene、golden regression、
   examples 和 `git diff --check`。

export/verify 始终 local-first、source-read-only、零 model/network 调用。
注册表不增加 runtime dependency 或 schema bump，
也不增加 restore、import、resume、sync、daemon 或 runtime-control 功能。
