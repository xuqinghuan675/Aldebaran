# Aldebaran

[![CI](https://github.com/xuqinghuan675/Aldebaran/actions/workflows/ci.yml/badge.svg)](https://github.com/xuqinghuan675/Aldebaran/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Aldebaran 是一个基于 **Python + PySide6** 的 A 股市场分析、情报聚合、策略判断与追踪复盘桌面应用。

> 本项目仅用于软件开发、数据工程和研究演示，不构成投资建议、证券推荐或收益承诺。

## 完整开源范围

这个仓库是 Aldebaran 的**完整源码仓库**，不是只保留接口的骨架版。应用入口、核心分析逻辑、Agent 编排、数据源适配、情报采集与归一化、策略与追踪逻辑、桌面 UI、构建脚本和测试代码均公开。

不进入版本库的只有运行时产生或属于用户本地环境的内容，例如：

- API Key、Token、账号配置和其他凭据；
- 真实持仓、自选、跟踪记录、缓存、日志和导出数据；
- IDE / Agent 本地状态、虚拟环境和构建产物；
- 与软件运行无关的本地私有文档。

这些内容被排除是为了安全和隐私，并不代表存在未公开的专有功能模块。

## 主要能力

- **桌面端 UI**：PySide6 多面板界面，覆盖行情、自选/持仓、市场观察、情报、分析和追踪复盘。
- **多数据源适配**：通过 AkShare、mootdx 及可选第三方接口组织行情和市场数据，并提供缓存与降级逻辑。
- **多 Agent 分析**：市场环境、基本面、技术、资金、事件等分析组件以及综合辩论/决策链路。
- **情报处理**：新闻、公告、公开网页信息的采集、分类、证据组织和 AI 分析接口。
- **追踪与复盘**：观察任务、命中情况、结果结算、评分和复盘改进。
- **本地数据隔离**：运行时数据默认写入用户目录，不写入 Git 仓库。
- **凭据管理**：API Key 通过环境变量或本地安全配置提供。
- **测试与打包**：确定性测试、GitHub Actions smoke CI 与 Nuitka 构建脚本。

## 仓库结构

~~~text
Aldebaran/
├─ app.py
├─ core/                   # 数据、分析、Agent、情报、策略、追踪与基础设施
├─ ui/                     # PySide6 UI 与静态资源
├─ tools/                  # 测试与构建工具
├─ data/
│  └─ intel_config.example.json
├─ .github/workflows/      # GitHub Actions CI
├─ requirements.txt
└─ launch.bat
~~~

## 快速开始

建议使用 Python 3.11 或 3.12。

~~~powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python app.py
~~~

Windows 也可以直接运行 launch.bat。

## 可选配置

| 环境变量 | 用途 |
| --- | --- |
| DEEPSEEK_API_KEY | AI 分析能力 |
| GITHUB_TOKEN | GitHub 公开信息访问的可选 Token |
| TICKFLOW_TOKEN | 可选第三方行情 Token |
| TICKFLOW_FREE_TOKEN | 可选第三方免费节点 Token |
| ALDEBARAN_DATA_DIR | 自定义运行时数据目录 |
| ALDEBARAN_INTELLIGENCE_CACHE_DIR | 自定义情报缓存目录 |
| ALDEBARAN_SCRAPLING_PYTHON | 可选 Scrapling 独立环境 Python 路径 |
| ALDEBARAN_BUILD_DIR | 自定义 Nuitka 构建输出目录 |

不要把真实 Key 写进仓库。文件配置应以 data/intel_config.example.json 为模板，并保持真实配置被 .gitignore 排除。

## 运行时数据

源码运行默认使用：

~~~text
~/.aldebaran/
~~~

打包运行时会使用应用旁的 AldebaranData/。这些目录均不应提交到 Git。

## 可选 Scrapling 运行时

Scrapling 不是主进程的强制依赖。若使用相应采集器，请单独准备 Scrapling 环境并设置：

~~~powershell
$env:ALDEBARAN_SCRAPLING_PYTHON = "<path-to-scrapling-python>"
~~~

未配置时，对应采集器会返回可诊断失败状态，不影响主框架其他模块。

## 测试

关键 smoke tests：

~~~powershell
python tools/_test_mytt_cleanroom.py
python tools/_test_credentials.py
python tools/_test_runtime_paths.py
python tools/_test_release_hardening.py
python tools/_test_scrapling_runtime.py
~~~

GitHub Actions 会在 main push 和 Pull Request 上运行基础语法检查与上述 smoke tests。完整桌面运行仍建议在安装了 requirements.txt 全部依赖的 Windows 环境验证。

## 构建

~~~powershell
powershell -ExecutionPolicy Bypass -File tools/build.ps1
~~~

## 数据与隐私原则

- 不提交真实行情缓存、持仓、自选、跟踪记录或分析记忆；
- 不提交本地私有文档或导出资料；
- 不提交个人邮箱、手机号、本机用户路径或真实 Token；
- data/ 仅保留无凭据的示例配置；
- .gitignore 默认阻断常见数据文件、私密文档、导出文件以及本地 Agent/IDE 状态。

## 第三方组件与致谢

项目自身代码采用 MIT License。仓库内 vendored 的第三方资源保留各自许可证与归属；架构参考项目也在第三方说明中明确列出。

详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 参与贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

安全问题请参见 [SECURITY.md](SECURITY.md)。

## License

除 THIRD_PARTY_NOTICES.md 中单独说明的第三方材料外，Aldebaran 自有源码采用 MIT License，详见 [LICENSE](LICENSE)。

## 风险提示

本项目涉及金融市场数据与自动化分析。数据可能延迟、缺失、错误，来源规则也可能变化；模型输出同样可能不准确。任何投资决策应由使用者自行核实并承担风险。
