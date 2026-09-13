# codex-preserve

[English](README.md) | **简体中文**

> 本页是简体中文说明。项目的规范说明与最终权威文本仍以英文 [README.md](README.md) 为准；若两者存在差异，以英文版为准。

`codex-preserve` 用于把一个 OpenAI Codex 会话从 Codex 中导出为耐久、可读的文件，并验证 manifest 所声明的每个成员是否仍然存在，且其字节内容与 manifest 记录的 SHA-256 和大小完全一致。

它只读取 Codex 会话；不会修改源会话。

> **这不是 `codex archive`。** Codex 内置的 `codex archive` 会改变会话在 Codex 内部的生命周期状态；`codex-preserve` 则把会话导出成 Codex 之外的耐久副本，并验证这个导出包。它不会改变会话状态，也不是 `codex archive` 的替代品。

## 什么时候适合用它

当你想为一个已经落盘的本地 Codex 会话制作**耐久备份 / 审计副本**，并且希望以后能机械验证导出的文件是否仍然完整、是否发生过未同步到 manifest 的改动时，可以使用 `codex-preserve`。

它**不是** transcript viewer、同步工具，也不是 restore/import 工具。如果你只需要一个方便浏览或分享的 HTML 对话记录，普通 transcript exporter 更合适。`codex-preserve` 关注的是：保存、来源追踪（provenance）、manifest-relative integrity，以及 fail-closed 验证。

**与 OpenAI 无隶属关系。** `codex-preserve` 是独立、非官方工具，不隶属于 OpenAI，也未获得 OpenAI 的背书、赞助或认证；它不是 OpenAI 产品或 Codex 官方组件，也不使用 OpenAI Logo 或其他视觉品牌元素。

## 安装

```bash
python -m pip install codex-preserve
```

需要 Python 3.9 或更高版本。运行时零依赖。可先确认 CLI：

```bash
codex-preserve --help
```

公开页面：[PyPI](https://pypi.org/project/codex-preserve/) · [Releases](https://github.com/davidqyc/codex-preserve/releases) · [Issues](https://github.com/davidqyc/codex-preserve/issues)

## 约 30 秒看到验证器实际工作

不需要真实 Codex 会话。克隆仓库后直接运行三组 synthetic package：

```bash
git clone --depth 1 https://github.com/davidqyc/codex-preserve.git
cd codex-preserve
./examples/run_examples.sh
```

Runner 会分别打印一个 `PASS`、一个 `FAIL` 和一个 `UNVERIFIABLE`，并检查它们的退出码是否严格为 `0`、`1`、`2`。如果本机已经安装 `codex-preserve`，脚本会优先使用已安装命令；否则直接从当前 checkout 运行。相同 fixture 也被测试套件持续断言，因此这个 demo 不是装饰性的示例。

更多说明见 [examples/README.md](examples/README.md)。

## 它会做什么

- **导出**一个已经落盘的本地 Codex rollout，生成按会话组织的 package：可读对话文件、machine receipt、输入附件、输出文件以及 manifest。
- **验证**导出 package：manifest 声明的每个成员都必须存在，并且大小与 SHA-256 必须匹配。
- **记录 provenance**：包括附件/产物来自哪一轮、payload 是否完整等。
- **默认归一化与脱敏**：home path 会为了可读性进行归一化；credential-shaped 值在写入前会被清理。
- **Fail closed**：机械上无法证明的内容会被报告为 unknown，而不是猜测。无法验证的 package 永远不会被错误报告成完整。

## 它不会做什么

- 不修改、不 archive、不 unarchive、不删除、不移动 Codex 会话。
- 不调用模型，也不需要网络服务；没有 daemon、hook、telemetry 或 event database。默认会执行一些本地只读 `git` 查询，见后文“它会读取什么”。
- 不解码 opaque / encrypted reasoning；这类内容只会被计数，不会被重建。
- 不做数字签名。manifest 提供的是 SHA-256 完整性声明，不是 cryptographic signature，也不能证明 package 的作者身份。

## 验证能够证明什么、不能证明什么

`codex-preserve verify` 精确回答一个问题：

> manifest 所声明的每个成员是否仍然存在，并且其字节内容是否仍然匹配 manifest 中记录的 SHA-256 和大小？

这能检测意外丢失、截断、传输/存储损坏，以及 payload 被修改但 manifest 没有同步更新的情况。

但它**不是 tamper-proofing**。以下内容不在证明范围内：

- **manifest 与 package 一起分发，且没有独立签名。** 没有 signature、certificate、trust root 或 transparency log，因此这里验证的是 *manifest-relative integrity*，不是 authenticity，也不能证明是谁生成了 package。
- **无法检测协调式改写。** 如果有人同时修改 payload，并重新计算对应 manifest row，新的 package 仍然可以通过验证。要检测这种情况，需要本工具目前没有提供的独立 trust anchor。
- **额外文件不属于当前检查范围。** 验证器检查的是 manifest 列出的成员。后来额外塞入一个 manifest 从未声明的文件，不会让验证失败。因此这里的“完整”是“没有已声明成员丢失”，不是“目录里没有任何额外文件”。

如果你需要的是 authenticity 而不是 integrity，应对导出 package 使用专门的签名工具；`codex-preserve` 有意不自行实现这一层。

## 它会读取什么

导出过程是本地、只读的，但不一定只读取你显式指定的单个 rollout 文件。正常情况下还可能：

- **只读 `~/.codex/session_index.jsonl`**，按 session id 恢复会话显示名。文件不存在或不可读时静默跳过；不会写入 `~/.codex`。
- **在会话记录的 workspace 中执行本地只读 `git` 查询**（`rev-parse`、`branch --show-current`、`remote get-url`、`merge-base`），记录工作发生的位置。这些是本地 repository query，不会访问 remote。
- **导出归一化后的 repository identity，例如 `owner/repo`**，写入 provenance、receipt 与可读文件。原始 remote URL 会被丢弃，不会被导出；但如果你分享 export，归一化后的 `owner/repo` 名称会随之分享。

可以传入 `--no-git-probe` 完全跳过 live git 查询。它只停止“主动询问本地 repository”这一步，并不会抹掉 rollout 本身已经持久化的 repository identity。如果 rollout 自己已经记录了 identity，它仍可能以归一化后的 `owner/repo` 形式出现在 receipt、可读导出、稳定输出 basename 以及目录 / bucket 名称里。

## 使用

```bash
# 查看 CLI。
codex-preserve --help

# 列出可以导出的 session（输出经过清理）。
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

`codex-preserve export --help` 会列出完整 export 选项，包括：

- selection：`--rollout`、`--session-id`、`--workspace`、`--since-hours`
- provenance：`--artifact ROLE=PATH`、`--review-bundle`、`--no-git-probe`
- policy：`--no-normalize`、`--reasoning-cap`、`--max-package-bytes`

Session 只会从 `~/.codex/sessions` 与 `~/.codex/archived_sessions` 中发现；不会宽泛扫描整个 home 目录。

### Verify 退出码

| exit | 含义 |
| --- | --- |
| `0` | manifest 声明的全部成员存在且匹配 |
| `1` | 至少一个 manifest 声明成员缺失或已被改变 |
| `2` | package 无法被完整验证，fail closed |

原因始终会打印。`--json` 输出同一 verdict，并包含每个成员的 reason code。

Manifest 声明的 package member 必须是 package tree 下的真实文件；symlink member path 会被拒绝。

Exit 2 也包括 verifier 无法解释 manifest 的情况，例如未知/缺失的 `package_schema_version`，或 manifest collection / row 形状不符合预期 JSON array/object。无法理解 manifest 的 verifier 会返回 `UNVERIFIABLE`，而不是把自己根本没检查到的内容错误报告成正常。

## 开发

```bash
PYTHONPATH=src python3 -m unittest discover -t . -s tests
python3 tools/public_hygiene_scan.py .
./examples/run_examples.sh
```

测试套件完全 deterministic，并且全部使用 synthetic fixture：它在临时目录中自行构造 rollout，不读取真实 Codex session directory。

`tools/public_hygiene_scan.py` 是 deterministic 检查，用于防止 private coordinate 或 credential-shaped literal 被带入公开源码树。

## 本地化

Package member 文件名与默认输出目录目前仍使用简体中文，因为它们属于已经被验证过的 package format。是否改动它们属于 package-format decision，而不是纯外观调整，这个问题目前仍然开放。

除这部分以外，CLI、receipt、manifest key 和英文 README 都使用英文。

## License

Apache License 2.0。完整文本见 [LICENSE](LICENSE)。

```text
SPDX-License-Identifier: Apache-2.0
```

## 状态

当前已发布版本以 [GitHub Releases](https://github.com/davidqyc/codex-preserve/releases) 为准。

上文描述的 export/verify contract——三种 verdict、退出码以及 manifest 能证明和不能证明的边界——是当前 0.1.x 版本线承诺保持的核心行为。Package member 文件名仍是开放问题，见“本地化”。

**与 OpenAI 无隶属关系。** `codex-preserve` 是独立、非官方工具，不隶属于 OpenAI，也未获得 OpenAI 的背书、赞助或认证；它不是 OpenAI 产品或 Codex 官方组件，也不使用 OpenAI Logo 或其他视觉品牌元素。
