# Session Preserve

[English](README.md) | **简体中文**

> 本页是中文说明。项目的规范文本仍以英文 README.md 为准；如果两边有差异，以英文版为准。

Session Preserve 的用途很简单：

**把已经保存在电脑里的 AI 编程会话，再独立留一份长期副本。**

v0.2.0 第一版支持四种本地会话：

- OpenAI Codex
- Claude Code
- Kimi Code
- ZCode

导出以后，你会得到一份人能直接看的 conversation.md、一份记录“这次到底读到了什么”的 export.receipt.json，以及一份 package.manifest.json。

以后再运行 session-preserve verify，就能检查 manifest 里登记的文件是不是都还在，大小和 SHA-256 有没有变化。

它只读源会话，不会去修改原软件里的历史记录，也不会帮你 restore、resume、import、sync、reindex、repair、archive 或 delete。

> 如果你只是想临时导出一份可读文本，原软件自带的导出功能通常更简单。Session Preserve 解决的是另一件事：**独立保存、记录来源和覆盖范围，并且以后还能重新验这份包有没有变。**

## 安装

~~~bash
python -m pip install session-preserve
~~~

需要 Python 3.9 或更高版本，运行时没有第三方 Python 依赖。

~~~bash
session-preserve --help
session-preserve --version
~~~

项目页面：[GitHub](https://github.com/davidqyc/session-preserve) · [Releases](https://github.com/davidqyc/session-preserve/releases) · [Issues](https://github.com/davidqyc/session-preserve/issues)

## 怎么用

新的导出包统一使用 schema 3.0。

### Codex

先看有哪些候选会话：

~~~bash
session-preserve export codex --list-candidates
~~~

按 session id 导出：

~~~bash
session-preserve export codex --session-id <uuid> --output-dir ./exports
~~~

如果你已经知道准确的 rollout 文件，也可以直接用 --rollout。

### Claude Code

明确指定一个顶层 session JSONL：

~~~bash
session-preserve export claude --source ~/.claude/projects/.../<session>.jsonl --output-dir ./exports
~~~

Claude Code 默认数据一般在 ~/.claude/projects；如果配置了 CLAUDE_CONFIG_DIR，则对应它下面的 projects。

如果你会长期保留 Claude Code 会话，可以看这篇说明：[Claude Code session retention and independent preservation](docs/claude-code-session-backup.md)。

### Kimi Code

明确指定一个当前格式的 Kimi Code session 目录：

~~~bash
session-preserve export kimi --source ~/.kimi-code/sessions/.../<session-id> --output-dir ./exports
~~~

第一版只读取这个 session 里的 state.json 和 agents/main/wire.jsonl。

如果配置了 KIMI_CODE_HOME，就从它的 sessions 目录里选。

### ZCode

明确指定一个 ZCode session id：

~~~bash
session-preserve export zcode --session-id <session-id> --output-dir ./exports
~~~

默认读取 ~/.zcode/cli/db/db.sqlite。数据不在默认位置时，用 --database PATH 指定。

### 验证导出包，或者打一个传输 ZIP

~~~bash
session-preserve verify ./exports/<package-dir>
session-preserve verify ./exports/<package-dir> --json
session-preserve pack ./exports/<package-dir>
~~~

pack 会先确认 schema 3.0 包本身能通过验证，再生成 session-package.zip。这个 ZIP 只是方便传输，不会变成 canonical package 的必要成员，也不会把自己装进自己。

退出码固定：

| 退出码 | 含义 |
| ---: | --- |
| 0 | manifest 登记的文件都存在，而且大小和 SHA-256 都匹配 |
| 1 | 至少有一个登记文件丢失或被改过 |
| 2 | 当前无法可靠完成验证，所以安全兜底为 UNVERIFIABLE |

## 新版导出包长什么样

schema 3.0 的物理文件名统一成英文：

~~~text
<package>/
├── conversation.md
├── export.receipt.json
├── package.manifest.json
├── attachments/          # 有附件时才需要
└── artifacts/            # 有产物时才需要
    └── index.md
~~~

以后如果生成传输用 ZIP，名字是 session-package.zip。它只是方便传输的派生文件，不是 canonical package 本身必须依赖的成员。

三个核心文件分工很清楚：

conversation.md 给人看。

export.receipt.json 记录这次源数据覆盖到了什么、有哪些异常、有哪些 provider 自己的结构信息。

package.manifest.json 记录包里有哪些文件、每个文件多少字节、SHA-256 是什么。

四个 Provider 共用的是“包和完整性校验机制”，不是强行把四家的会话结构揉成同一种模型。

## 四个平台分别怎么处理

### Codex

Codex 继续复用已经在 codex-preserve 0.1.x 里验证过的成熟解析逻辑，只是最终写成新的 schema 3.0 包。

它可以读取选中的 rollout、只读查询 ~/.codex/session_index.jsonl 获取显示名称，还可以默认执行少量本地只读 Git 查询来记录工作来源。传 --no-git-probe 可以跳过实时 Git 查询。

它绝不会调用 codex archive，也不会改变 Codex 里的会话状态。

### Claude Code

Claude 第一版只读取你明确指定的一个**顶层 session JSONL**。

这里采用“宁可多保留已经落盘的安全文本，也不擅自猜当前主分支”的策略。

所以：

并行分支上的安全文本会保留；last-prompt 或 rewind 记录不会让别的已落盘内容凭空消失；parent 断链、重复记录之类问题会明确记下来，不会擅自修成另一条链。

thinking signature、原始 tool payload、环境上下文、账号标识、未知 raw value 等不会原样导出。

还有一个特别重要的边界：

**Claude 界面已经显示出来的回复，不一定已经写进本地 JSONL。**

所以哪怕 JSONL 本身读取稳定，我们也不会声称“Claude UI 里你看到的所有内容都已经完整保存”。

第一版也不读取 subagent 和 tool-result sidecar 的正文。

### Kimi Code

Kimi 第一版支持当前 Kimi Code 的 session 结构：

~~~text
<session>/
├── state.json
└── agents/
    └── main/
        └── wire.jsonl
~~~

会保留明确识别出的用户文本、助手文本和有限的工具结构信息。

不会原样保存 thinking、模型请求调试内容、tool 参数/结果和未知 record body。

第一版不读取 subagent 正文，也不宣称支持旧的 Python 时代 ~/.kimi 存储格式。

### ZCode

ZCode 第一版从本机 SQLite 会话库里，按一个明确的 session id 只读读取。

它使用选中 session 对应的 session、message、part 结构化记录。可见文本会保存；隐藏/模型内部消息、reasoning body、原始 tool body 不会原样导出。

~/.zcode/cli/rollout/model-io-*.jsonl 属于模型 I/O 诊断轨迹，不被当成正式会话源。

## “读取完整”不等于“界面里的所有内容都完整”

Session Preserve 只对已经持久化在本机的数据负责。

它可以告诉你：

这次读源文件时是否稳定；已落盘内容里有没有遇到未知结构；保留了多少安全可读文本；有没有分支、断链、重复记录或其他限制。

它不会反过来猜：

界面上是不是还有尚未落盘的消息；session 是不是已经真正结束；隐藏模型状态是不是能重建；没有读取的 sidecar 是不是“应该算已覆盖”。

如果遇到不认识或不能证明的结构，会把 coverage 标成 NON_COMPLETE，而不是装作完整。

## verify 到底能证明什么

session-preserve verify 回答的是：

> manifest 登记的文件还在不在？它们现在的字节内容，是否还和 manifest 里记录的大小、SHA-256 一样？

这很适合发现文件丢失、截断、存储损坏，或者有人只改了 payload 没同步更新 manifest 的情况。

但它**不能证明真实性 authenticity**。

因为 manifest 也是和包一起保存的，并没有独立签名。一个人如果同时修改 payload 和 manifest，再重新计算哈希，仍然可能通过 verify。

所以它提供的是 manifest-relative integrity，不是数字签名、作者证明，也不是取证意义上的 chain of custody。

真的需要 authenticity 时，应再用独立签名或可信时间戳机制。

## 老版本怎么办

原来的 codex-preserve 0.1.3 会继续留在 PyPI，不撤、不 yank。

新版 Session Preserve 继续永久支持验证老 Codex 包：

~~~text
schema 2.1
schema 2.2
~~~

新的四平台导出统一使用：

~~~text
schema 3.0
~~~

目前不会专门做一个“旧 codex-preserve 自动跳转到新 session-preserve”的兼容包。

GitHub 仓库已经从 davidqyc/codex-preserve 改名为 davidqyc/session-preserve，旧 GitHub 链接由 GitHub redirect 继续转到新仓库。

## 30 秒看 verify 工作

仓库里还保留三份纯 synthetic 的旧 schema 测试包，专门证明新版 verifier 仍然兼容旧包：

~~~bash
git clone --depth 1 https://github.com/davidqyc/session-preserve.git
cd session-preserve
./examples/run_examples.sh
~~~

预期结果是：

~~~text
PASS         exit 0
FAIL         exit 1
UNVERIFIABLE exit 2
~~~

新的 schema 3.0 四 Provider 导出也由 synthetic test suite 持续验证。

## 隐私和本地行为

Session Preserve 是 local-first 工具。

导出和验证过程不会调用 AI 模型，也不会主动把会话上传到网络。

各 Provider parser 都按 allowlist 处理。某个 raw value 只是“存在于源数据里”，并不代表它就会被复制到导出包。

仓库里还有 public-hygiene scan，专门防止真实 session payload、本机私人路径和疑似凭证内容误进开源仓库。

## 明确不做什么

Session Preserve 不是云端聊天导入器，不是聊天浏览器，不做 restore / resume / import / sync，不修复或重新索引原软件历史，不做后台自动导出 daemon，不做 Provider 互转，也不做通用 Provider 插件平台。

v0.2.0 的范围就明确限定为：

**Codex + Claude Code + Kimi Code + ZCode。**

## 开发

~~~bash
PYTHONPATH=src python3 -m unittest discover -t . -s tests
python3 tools/public_hygiene_scan.py .
python3 tools/g3_golden_regression.py
./examples/run_examples.sh
~~~

仓库里的 Provider fixture 全部是手写 synthetic 数据，不会把真实用户 transcript 脱敏后塞进测试仓库。

四个 Provider 通过小型内部静态注册表接入。
[Provider Adapter 扩展合同](docs/provider-adapters.zh-CN.md)
说明 source、parser、spec、registration、tests 和文档的要求。

## License

Apache License 2.0。完整文本见 [LICENSE](LICENSE)。

~~~text
SPDX-License-Identifier: Apache-2.0
~~~

## 独立项目声明

Session Preserve 是独立、非官方的开源项目，与 OpenAI、Anthropic、Moonshot AI、Z.ai 均无隶属、背书、赞助或认证关系。文档中出现公司或产品名称，只是为了准确说明各 adapter 读取哪一种本地会话格式。
