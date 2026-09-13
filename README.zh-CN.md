# codex-preserve

[English](README.md) | **简体中文**

> 本页是简体中文说明。项目的规范说明与最终权威文本仍以英文 [README.md](README.md) 为准；若两者存在差异，以英文版为准。

`codex-preserve` 用来把一个已经保存在本机的 OpenAI Codex 会话导出成便于长期保存、可直接阅读的文件，并在之后检查导出包里的文件是否仍与 manifest（清单）记录的大小和 SHA-256 完全一致。

它只读取 Codex 会话，不会修改源会话。

> **这不是 `codex archive`。** Codex 内置的 `codex archive` 会改变会话在 Codex 内部的生命周期状态；`codex-preserve` 则是在 Codex 之外生成一份独立、可长期保存并可验证的副本。它不会改变会话状态，也不是 `codex archive` 的替代品。

## 什么时候适合用它

如果你想给一个已经保存在本机的 Codex 会话留一份**长期保存的备份 / 审计副本**，并希望以后还能用程序确认清单里的文件有没有丢失、损坏，或发生过未同步到 manifest 的修改，就可以使用 `codex-preserve`。

它**不是**对话浏览器（transcript viewer）、同步工具，也不是恢复 / 导入（restore/import）工具。如果你只是想导出一份方便浏览或分享的 HTML 对话记录，普通的 transcript exporter 更合适。`codex-preserve` 专注于更底层的事情：长期保存、来源追踪（provenance）、基于清单的完整性校验，以及安全兜底（fail-closed）的验证行为。

**与 OpenAI 无隶属关系。** `codex-preserve` 是独立、非官方工具，不隶属于 OpenAI，也未获得 OpenAI 的背书、赞助或认证；它不是 OpenAI 产品或 Codex 官方组件，也不使用 OpenAI Logo 或其他视觉品牌元素。

## 安装

```bash
python -m pip install codex-preserve
```

需要 Python 3.9 或更高版本，运行时零依赖。可以先确认 CLI 是否可用：

```bash
codex-preserve --help
```

公开页面：[PyPI](https://pypi.org/project/codex-preserve/) · [Releases](https://github.com/davidqyc/codex-preserve/releases) · [Issues](https://github.com/davidqyc/codex-preserve/issues)

## 30 秒快速体验

不需要真实的 Codex 会话。克隆仓库后，直接运行三组专门构造的测试包：

```bash
git clone --depth 1 https://github.com/davidqyc/codex-preserve.git
cd codex-preserve
./examples/run_examples.sh
```

脚本会分别打印一个 `PASS`、一个 `FAIL` 和一个 `UNVERIFIABLE`，并检查它们的退出码是否严格为 `0`、`1`、`2`。如果本机已经安装了 `codex-preserve`，脚本会优先使用已安装的命令；否则直接从当前 checkout 运行。

同一批测试数据也由正式测试套件持续校验，所以这个 Demo 展示的是实际受回归测试保护的行为，不是单独做出来的演示效果。

更多说明见 [examples/README.md](examples/README.md)。

## 它会做什么

- **导出会话**：把一个已经保存在本机的 Codex rollout 导出成按会话组织的包，其中可以包含可读的对话文件、机器可读回执（receipt）、输入附件、输出文件和 manifest。
- **严格验证**：检查导出包。manifest 中声明的每个成员都必须存在，并且大小与 SHA-256 必须完全匹配。
- **记录来源（provenance）**：记录附件和产物来自哪一轮、payload 是否完整等来源信息。
- **归一化与脱敏**：本机 home 路径会为了可读性做归一化；疑似密钥、Token 或其他凭证类敏感信息会在写入前清理。
- **安全兜底（fail-closed）**：工具无法确认的内容会明确标记为 `unknown`，不会靠猜测补结论。如果整个包无法完成验证，则返回 `UNVERIFIABLE`，不会把未知状态误报为完整。

## 它不会做什么

- 不修改、不 archive、不 unarchive、不删除，也不移动 Codex 会话。
- 不调用模型，也不进行网络请求；没有常驻 daemon、hook、telemetry 或 event database。默认会执行少量本地只读 `git` 查询，见后文“它会读取什么”。
- 不解码不透明或加密的推理内容（opaque / encrypted reasoning）；这类内容只统计数量，不做内容还原。
- 不做数字签名。manifest 提供的是基于 SHA-256 的完整性声明，不是 cryptographic signature，也不能证明这个包是谁生成的。

## 验证能够证明什么、不能证明什么

`codex-preserve verify` 只回答一个非常具体的问题：

> manifest 里列出的文件是不是都还在？它们的内容是否仍与 manifest 中记录的大小和 SHA-256 一致？

这能发现文件意外丢失、被截断、在传输或存储过程中损坏，以及 payload 已被修改但 manifest 没有同步更新的情况。

但它**不是防篡改系统（tamper-proofing）**。以下情况不在它的证明范围内：

- **manifest 与导出包一起保存，而且没有独立签名。** 没有 signature、certificate、trust root 或 transparency log，所以这里证明的是 *manifest-relative integrity*：文件是否和当前这份清单对得上。它不证明清单本身可信，也不证明这个包是谁生成的。
- **同时修改文件和 manifest，验证仍可能通过。** 如果有人修改 payload 后，又重新计算并更新对应的 manifest 条目，新包仍然可以通过验证。要发现这种情况，需要额外的独立 trust anchor，而本工具目前没有提供。
- **额外新增的文件不在当前检查范围内。** 验证器只检查 manifest 明确列出的成员。后来往目录中加入一个 manifest 从未声明的文件，不会让验证失败。因此这里的“完整”是“清单里声明的东西没有丢”，不是“目录里绝对没有多余文件”。

如果你需要的是 authenticity（真实性 / 来源认证）而不是 integrity（完整性），应对导出包使用专门的签名工具；`codex-preserve` 有意不自行实现这一层。

## 它会读取什么

导出过程完全在本地进行，并且对 Codex 源数据只读。不过，除了你显式指定的 rollout 文件，正常情况下它还可能读取以下内容：

- **只读 `~/.codex/session_index.jsonl`**：按 session id 获取会话显示名称。文件不存在或不可读时会静默跳过；不会向 `~/.codex` 写入任何内容。
- **执行本地只读 Git 查询**：会在会话记录的 workspace 中执行 `rev-parse`、`branch --show-current`、`remote get-url`、`merge-base` 等查询，用来记录工作发生的位置。这些查询都在本地完成，不会访问远程服务器。
- **记录归一化后的仓库标识，例如 `owner/repo`**：这个标识会写入 provenance、receipt 和可读导出文件。原始 remote URL 会被丢弃，不会导出；但如果你把导出包分享给别人，归一化后的 `owner/repo` 名称也会随之分享。

可以传入 `--no-git-probe` 完全跳过实时 Git 探测。这个参数只会停止工具主动查询本地 repository，并不会擦除 rollout 本身已经保存的 repository identity。如果 rollout 已经记录了仓库身份，它仍可能以归一化后的 `owner/repo` 形式出现在 receipt、可读导出、稳定输出 basename，以及输出目录 / bucket 名称中。

## 使用

```bash
# 查看 CLI。
codex-preserve --help

# 列出可以导出的 session（输出已做清理）。
codex-preserve --list-candidates

# 按 session id 导出。
codex-preserve --session-id <uuid> --output-dir ./exports

# 等价的显式 export 子命令。
codex-preserve export --session-id <uuid> --output-dir ./exports

# 验证一个已经导出的 package。
codex-preserve verify ./exports/<bucket>/<package-dir>

# 输出机器可读的 verification receipt。
codex-preserve verify ./exports/<bucket>/<package-dir> --json
```

`codex-preserve export --help` 会列出完整的导出选项，包括：

- selection（选择目标）：`--rollout`、`--session-id`、`--workspace`、`--since-hours`
- provenance（来源信息）：`--artifact ROLE=PATH`、`--review-bundle`、`--no-git-probe`
- policy（导出策略）：`--no-normalize`、`--reasoning-cap`、`--max-package-bytes`

Session 只会从 `~/.codex/sessions` 与 `~/.codex/archived_sessions` 中发现；不会宽泛扫描整个 home 目录。

### `verify` 退出码

| exit | 含义 |
| --- | --- |
| `0` | manifest 声明的全部成员都存在且匹配 |
| `1` | 至少一个 manifest 声明的成员缺失或已被改变 |
| `2` | 无法完成验证，安全兜底（fail-closed） |

原因始终会打印。`--json` 会输出同一个 verdict，并附带每个成员的 reason code。

Manifest 声明的 package member 必须是 package tree 下的真实文件；symlink member path 会被拒绝。

退出码 `2` 也包括验证器无法解析 manifest 的情况，例如未知 / 缺失的 `package_schema_version`，或 manifest collection / row 的结构不符合预期的 JSON array/object。如果验证器无法解析 manifest，会直接返回 `UNVERIFIABLE`，绝不会把未经校验的内容误判为通过。

## 开发

```bash
PYTHONPATH=src python3 -m unittest discover -t . -s tests
python3 tools/public_hygiene_scan.py .
./examples/run_examples.sh
```

测试套件是确定性的，并且全部使用专门构造的测试数据（synthetic fixtures）：测试会在临时目录中自行构造 rollout，不读取真实的 Codex session directory。

`tools/public_hygiene_scan.py` 是一项确定性检查，用来防止内部路径、环境坐标或疑似明文凭证等私有信息被误带入开源代码库。

## 本地化

导出包中的部分成员文件名和默认输出目录目前仍使用简体中文。它们已经属于现有 package format 约定的一部分；要不要修改，需要按格式兼容性问题处理，而不是当作纯文案翻译。未来是否调整，这个问题仍然开放。

除此之外，CLI、receipt、manifest key 和英文 README 都使用英文。

## License

Apache License 2.0。完整文本见 [LICENSE](LICENSE)。

```text
SPDX-License-Identifier: Apache-2.0
```

## 状态

当前已发布版本以 [GitHub Releases](https://github.com/davidqyc/codex-preserve/releases) 为准。

上文描述的 export/verify contract——三种 verdict、退出码，以及 manifest 能证明和不能证明的边界——是当前 0.1.x 版本线承诺保持的核心行为。Package member 文件名仍是开放问题，见“本地化”。

**与 OpenAI 无隶属关系。** `codex-preserve` 是独立、非官方工具，不隶属于 OpenAI，也未获得 OpenAI 的背书、赞助或认证；它不是 OpenAI 产品或 Codex 官方组件，也不使用 OpenAI Logo 或其他视觉品牌元素。