# VLC 监播工具：独立项目迁移与 GitHub 自动打包计划

日期：2026-10-16  
状态：本地迁移、Mac 构建与 CI 配置已完成；远端推送、首次 Actions、标签发布和旧包清理未执行。证据见 [实施记录](2026-10-16-implementation.md)。

执行修订：采用用户认可的轻量入口 UI；微信周期重复提醒已由稳定性计划取消，下文旧功能清单的可选重复提醒不再适用。  
配套：[全 App 极简 UI 计划](2026-10-16-app-ui-simplification.md)。

## 1. 最终使用方式

用户在 GitHub 新建一个 Repository（代码仓库，不是 GitHub Projects 看板），建议首先设为 Private。把本地独立目录 `vlc-monitor/` 的审定文件提交上去：

```text
推送 main / 手动构建
       ↓
GitHub Actions 原生 Mac ARM64 + Windows x64 构建
       ↓
测试与包检查通过
       ↓
Actions 页面下载两个平台 ZIP 和 SHA256

推送版本标签 vX.Y.Z
       ↓
两平台均成功 + 版本一致性检查
       ↓
GitHub Release 附件，可长期下载
```

不需要本机 Podman，不把 `.app` 或历史 ZIP 上传到源码仓库。构建得到 Windows EXE 不等于 Windows 10/11 播放/声音/凭证功能已经验收。

## 2. 项目边界与目录

```text
20261015udp/
  src/、dist/、docs/、...       原项目，先保留，不再并行做 UI 修改
  vlc-monitor/                 新的独立 Git 仓库根目录
    .github/workflows/build.yml
    .gitignore
    .python-version
    pyproject.toml
    uv.lock
    README.md
    src/vlc_monitor/
    tests/
    packaging/
    tools/                    仅迁移审核过的可复用工具
    docs/
      plans/                  本轮两个计划
      images/app-overview.png 一张新的脱敏实际截图
      usage.md                必要时拆分的使用说明
      THIRD_PARTY_NOTICES.md
    build/、dist/             本地/CI 生成，忽略，不提交
```

- 只在新目录初始化 Git，不在父目录初始化，不把父目录包装成仓库上传。
- 先复制审核后的文件，不直接剪切原项目；保留可运行回退点，避免影响用户正在使用的 App。
- 保持 Qt 应用身份和系统凭证命名，不因代码目录迁移新建一套用户配置。
- 首次复制后核对文件清单及哈希；后续只在新目录修改，禁止两个副本持续双向同步。

## 3. 迁移与上传安全门禁

### 3.1 允许迁移的范围

| 类别 | 规则 |
| --- | --- |
| 源码、声音资源、打包脚本 | 审核后迁移，资源须确认有分发权 |
| pyproject、uv.lock、Python 版本 | 一并迁移，保持锁定依赖 |
| tests | 保留核心回归；只允许合成、无个人信息的 fixture |
| tools | 逐文件挑选；依赖旧 tmp/个人路径的工具先排除或改造 |
| README、使用说明、许可证 | 重写/复核后迁移，不盲拷整个 docs |
| 一张界面图 | UI 完成后重新截取；旧截图默认不上传 |
| 规划文档 | 可保留本轮两份；旧计划、审计原始记录先不搬 |

### 3.2 不迁移、不提交的内容

- `.venv/`、缓存、`build/`、`dist/`、`tmp/`、任意 `.app` 及旧源码/二进制 ZIP。
- 本地配置、SQLite/备份、运行日志、诊断原始 JSON、调试截图和私人视频。
- `.env*`（脱敏示例另行白名单）、密钥、证书私钥、Token、Cookie、二维码、挑战、凭证导出。
- 真实直播源和含签名参数的 URL、公司内部地址、个人账号、个人文件系统路径。
- `tmp` 内外部项目审计副本及嵌套 `.git`，不能误作为本项目源码带入。
- macOS Application Support、Windows LocalAppData 和系统凭证库完全不在迁移/清理范围。

### 3.3 .gitignore 拟补充

保留当前已有的缓存/构建忽略规则，新增明确类别；具体实现时核对合法 fixture，避免误排必要资源。

```gitignore
.venv/
.uv-cache/
__pycache__/
.pytest_cache/
*.py[cod]
*.egg-info/
build/
dist/
tmp/
*.app/
.DS_Store
.env
.env.*
!.env.example
*.log
*.sqlite
*.sqlite3
*.sqlite3-*
*.db
*.db-*
config.json
config.json.*
*.pem
*.key
*.p12
*.pfx
```

`.gitignore` 只是防误加，不是秘密扫描，也不影响已经跟踪的文件。若实施时新目录已存在 Git 历史，先检查跟踪文件和历史；发现秘密先处理/轮换，不以新增 ignore 或删当前文件宣称历史已安全。

### 3.4 首次上传前的核验顺序

1. 产出精确迁移白名单，扫描源码、测试、说明、锁文件及图片；只记录风险类别和路径，不回显敏感值。
2. 只把白名单放进新目录；检查符号链接是否指向项目外，禁止借链接把用户数据带入。
3. 在新目录初始化 Git，用明确清单暂存，不先对父目录 `git add .`。
4. 检查 `git diff --cached --name-only`、`git diff --cached --stat` 和暂存内容；运行本地 secret scanner（工具版本固定、结果脱敏）。
5. 检查临时生成的源码压缩包和两平台 ZIP 文件列表，防止打包路径绕过 Git ignore。
6. README 截图单独人工查看。扫描无命中不等于证明无敏感信息，必要人工复核不能省略。
7. 本地与压缩包检查通过后，才推送用户给定仓库；不在本轮创建远端、授权上传或索取账号凭证。

## 4. 现有打包脚本需要先修正

以下是当前代码已读到的隐患，不是已完成的修复：

1. `packaging/source_archive.py` 递归收集整个 `docs/`，还依赖旧根目录两份中文计划。迁移后应改为新项目受控白名单，不依赖旧目录、不打入历史诊断或原始截图。
2. 两个平台分别生成同名源码 ZIP，而 `docs/build-manifest.json`/licenses 会因平台不同改变。源码 ZIP 应从审定源码生成一次，由两端共用；不得在合并产物时同名覆盖。
3. 平台 ZIP 只包含 App/完整 onedir、使用说明、许可证和该平台 build manifest，不再自动附带旧验收 JSON、审计文档及完整源码 ZIP。源码作为独立受控附件；如分发许可证另有要求，先评审并补齐必要对应源代码/链接。
4. Windows 的 collect-notices/source-archive 等每个 native 子进程必须检查退出码，不只依赖 `$ErrorActionPreference`。
5. `collect_notices.py` 的 Windows 未构建固定描述改为实际平台事实；应用版本集中读取，加入 Git SHA、架构、Python/打包器版本与真实签名状态，不记录 runner 私人路径/环境变量。
6. Mac/Windows staging 与许可证收集避免合并上次残留；清理只作用于本次受控生成路径，禁止删除源码或用户数据。
7. 校验和统一 UTF-8 `sha256  basename`，下载后可直接校验；不使用 Windows `Format-List` 报告或含构建目录的路径。
8. `.spec` 保留 Mac ARM64 和外部 VLC 不捆绑约束；检查 Qt 平台插件、声音资源、许可证和目标架构，Mac ad-hoc 签名独立验证。
9. `tests/test_vlc_picker.py` 的部分 POSIX 路径字符串断言需按 Path 语义跨平台修正，不为构建绿灯跳过整组测试。

## 5. GitHub Actions 设计

### 5.1 首期平台

| 目标 | 构建环境 | 产物 | 本轮边界 |
| --- | --- | --- | --- |
| macOS Apple Silicon | GitHub 托管 ARM64 macOS runner | 包含 `.app` 的 ZIP | 保留内部 ad-hoc；无 Developer ID/公证 |
| Windows x64 | GitHub 托管 x64 Windows runner | 包含完整 onedir 的 ZIP | 未签名；不能只分发 exe |

实施时按 GitHub 官方 runner 表为用户仓库核实明确标签，Windows 可从 `windows-2022` 评估，Mac 必须确认所选标签对应 ARM64；不要猜测 `macos-latest` 一定符合架构，也不要使用 Intel runner 硬打 ARM 包。运行时输出并检查 OS、CPU、Python 位宽；runner 不可用即明确失败，不静默改目标架构。

Python 优先使用 `.python-version` 当前的 `3.12.13`，先验证安装源在两个目标架构可用；不可用则单独说明并审定版本调整，不让 CI 静默换 patch。uv 固定已验证版本，执行 `uv sync --locked --extra dev`，不自动重写 lock。

### 5.2 触发与权限

- `push` 到 `main` 自动构建；`workflow_dispatch` 支持手动重跑；`v*` 标签走版本发布。首次上传包含 workflow 的 main 提交即可触发，不要求额外手动建流水线。
- PR 只做受限验证，不发布 Release；禁止 `pull_request_target` 执行不可信代码并取得写权限。
- 普通构建 `contents: read`；仅标签发布 job 获得 `contents: write`，使用最小权限 `GITHUB_TOKEN`，不要求用户在源码填写 PAT。
- 使用可信 actions，实施时核实可用版本并固定完整 commit SHA；设置超时、分支构建并发取消，标签发布不因普通推送被取消。
- 不给 CI 用户微信凭证、真实直播源或本机配置；不打印完整环境或 token。
- 缓存最多依赖下载，key 包含系统、架构、Python、uv.lock；不缓存用户数据、打包 staging 或凭证。

### 5.3 每个平台的执行顺序

1. Checkout 审定提交，核验源文件列表/版本/架构。
2. 安装固定 Python、uv，锁定同步依赖。
3. 用 offscreen 环境运行已有核心测试；Qt offscreen 变量仅用于测试，不永久影响打包/终端用户运行。
4. 执行已修正的原生打包脚本：Mac `sh packaging/build-macos.sh`；Windows `pwsh -File packaging/build-windows.ps1`。
5. 核对可执行架构、Qt 插件、声音、许可证、manifest；确认没有打入外部 libVLC 和用户数据。
6. 解压检查目录完整性，生成并复核 SHA256。
7. 只上传精确白名单 ZIP/校验和，`if-no-files-found: error`；禁止整个 `dist/**`、`docs/**` 或工作区打包上传。

无需默认安装 VLC 才能静态打包。`--diagnose` 会真的加载 VLC，不能作为未安装 VLC job 的必过项。若另加真实播放 smoke，只能安装明确版本/架构的 VLC、使用合成素材并独立记录；不使用旧 `tmp` 文件或私人流。

### 5.4 产物、下载与发布

- 示例：`VLCMonitor-<version>-macos-arm64-internal.zip`、`VLCMonitor-<version>-windows-x64-internal.zip`，附对应 SHA256；架构和未正式签名边界不能省略。
- Actions artifact 名加平台与 run id，建议保留 14 天；到期不承诺可下载。用户路径：仓库 → Actions → 成功运行 → Artifacts。私有仓库需有访问权限并登录。
- Actions 下载可能外套一层 artifact ZIP，README 解释先解外层，再解平台包；Windows 保留整个目录。
- `vX.Y.Z` 标签必须与项目版本相等，两平台和安全门禁均通过后才能发布 Release；一边失败不得发布“完整双平台版本”。
- Release 汇总两平台 ZIP、一次生成的源码 ZIP和统一 SHA256；同名资产禁止静默覆盖。可复用未发布草稿以重跑，但已发布版本不可变，修复用新版本。
- Release 文案分别写构建、签名、平台功能验收状态；私有 Release 不等于任何人都能下载。若需要公开分发，另确认公开源码/许可证/仓库权限策略。
- macOS 最低版本声明不等于已实测最低系统；要检查实际 wheel/dylib deployment target 后再写支持范围。

## 6. README：一张图，让普通用户看懂

推荐结构：

1. 一句话：“圈出视频中的关注区域，出现黑屏或疑似静帧时提醒你。”
2. 一张实际主窗口截图：相对路径 `docs/images/app-overview.png`，标注演示画面，不使用用户上传的二维码/账号等隐私截图。
3. 主要功能：单路播放、多区域检测、本地声音/视觉提醒、微信告警与恢复、可选重复提醒、历史记录、本地保存配置。
4. 下载：Actions 临时包和 Release 版本包各自操作路径；清楚区分 Mac ARM64 / Windows x64。
5. 前置条件：另装匹配架构的 VLC；终端用户不需要 Python，不捆绑 VLC。
6. 四步使用：打开视频 → 新增区域 → 开始监播 → 在设置里按需配置通知。补充停止/临时静音/确认不等于恢复和保存配置。
7. 微信：启用、必要时本人绑定、测试、解绑；关设置继续通知，退出 App 停止。无“试用”标题，无“永久可靠/保证送达”承诺。
8. 支持边界：本机 VLC 支持不等于所有输入可监播；签名安全提示不要通过全局关闭系统安全机制解决。
9. 简短开发构建说明和许可证/第三方声明；长开发记录不放 README。

源码公开许可证由用户/权利方决定，不擅自添加 MIT/Apache；是否允许外传、内网地址与依赖分发权在首次 push 前确认。私有仓库也不能上传秘密。

## 7. 历史包清理策略

本轮不删除。后续用户已授权方向是清理历史垃圾版本，但执行应以最新交付清单为准：

1. 枚举原 `dist/`、`build/`、`tmp/` 候选，列路径/版本/大小/用途；不按大文件阈值直接删除。
2. 保留用户正在运行的包与一个已接受的回退版本、其 ZIP 与匹配 SHA256；早期 `0.1.0/0.2.0` 是否仍需回退以迁移时实际使用情况核对，不能仅按目录名判断。
3. 新项目本地核验和首次 CI 下载核验成功后，再删除确定无用的旧包、重复解压目录和可重建构建中间文件。
4. `tmp/` 中可能有尚需验证的合成素材/脚本，先甄别，不直接整目录清空；未知用途文件先列待处理。
5. 不删除源代码、原始验收证据、用户配置/数据库、系统凭证；不终止用户正在运行的 App 来清理。
6. 记录删除清单与释放空间。任何删除仅限已核对的项目内路径，禁止跟随符号链接越界。

## 8. 任务拆分与交付检查点

| 编号 | 执行内容 | 验收标准 |
| --- | --- | --- |
| M0 | 等原任务收尾，冻结版本/文件快照和正在使用的 App | 明确权威基线，不覆盖并发改动 |
| M1 | 安全扫描、迁移/排除清单、审定截图和许可证范围 | 文件白名单与风险清单可复核 |
| M2 | 复制到独立目录，完善 ignore，修复旧目录依赖 | 新目录可以独立锁定同步/测试，不读取父目录 |
| U0–U8 | 执行配套全 App UI 计划 | 全 App 视觉与核心行为分别验收 |
| M3 | 收敛版本、源码 ZIP、manifest、签名检查与 checksum 合同 | 本地 Mac 构建可复现，Windows 脚本静态问题已修正 |
| M4 | 新增 workflow，精确 artifact、权限/超时/失败处理 | 配置静态检查通过；不提前写双平台成功 |
| M5 | 用户建仓库并提供地址，审查暂存内容后首次推送 | 远端只有新项目白名单，无二进制和敏感资料 |
| M6 | 首次 Actions 原生构建、下载、解压、校验 | 两平台构建证据完整；失败明确报告，不假报 Windows 验收 |
| M7 | 推送审定版本标签并验证 Release 附件 | 两平台、源码及哈希完整；文案准确 |
| M8 | 记录最新回退点后按清单清理旧包 | 不误删运行包、用户数据或仍需要的证据 |

首次上传方式建议使用新目录内的 Git/官方客户端，而不是网页直接拖入整个父目录；后者易漏隐藏 workflow 文件并误传旧产物。真正上传动作等待计划确认及仓库地址。

## 9. 最终验收与仍待核实事项

- [ ] 用户确认计划和全 App UI 首轮真实渲染；目前只交付本文档。
- [ ] 新目录独立，源码/README/图/锁文件齐全；旧项目仍可回退。
- [ ] 暂存文件、Git 历史（如存在）、源码 ZIP、平台 ZIP、截图分别通过安全复核。
- [ ] 账户仓库可用的 runner 标签、Python 安装版本与 Actions 用量额度已确认；不承诺私有构建免费。
- [ ] Mac ARM64 / Windows x64 构建成功；目标设备启动/播放/声音/微信等功能验证另列。
- [ ] README 一图与使用步骤对应最终 UI，未将合成演示、离线 fake 或 CI 成功写成生产验收。
- [ ] 无签名/未公证、支持系统版本、VLC 外部依赖、手机送达与长期观察边界准确。
- [ ] 清理范围审定，当前包和回退包保留，未触碰应用运行数据。

官方实施参考（本轮未重新核实 runner 标签、Action 版本或账户费用，实施时核对）：

- [GitHub 托管 runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [下载 workflow artifacts](https://docs.github.com/en/actions/managing-workflow-runs/downloading-workflow-artifacts)
- [管理 workflow artifacts](https://docs.github.com/en/actions/using-workflows/storing-workflow-data-as-artifacts)
- [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
