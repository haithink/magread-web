# 刊读 Web · Magread Web

在浏览器中阅读英文刊物，生成中文摘要、生词与短语解释，查询单词、解析句子，并围绕文章与 AI 讨论。电脑、Android 和 iPhone 浏览器共用服务器上的书库与学习记录。

当前版本 **v0.1.0**。阅读核心由 `haithink/magread-desktop` 提取，本仓库可以独立安装运行，不需要桌面程序、浏览器插件或前端构建工具。

## 功能

- EPUB/PDF 上传、封面与刊物分类、最近阅读及继续阅读。
- 文章摘要与搜索，未读、已读、不感兴趣标记。
- 全局已读清单、去重文章数和累计阅读词数，显示标题、词数、完成时间和所有出处，支持搜索及回到原文。
- 正文阅读、字号调整、全文搜索、词数与阅读位置保存，工具栏实时显示百分比、进度条和段落位置。
- 全文/逐段生词与短语、补缺生成，区分个人收藏与模型词表。
- 查词自动收藏、个人生词本查询与编辑、句子讲解；展示最近收藏/查词来源及原文片段，点击文章可回到来源段落。
- 按文章保存多轮 AI 讨论，显示生成进度，支持停止讨论回答。
- 粘贴完整模型配置 JSON，编辑已有模型的接口地址、模型名等配置，选择默认模型与已掌握的词汇级别；编辑时密钥留空保留原值。

## 已读统计与 Chrome 阅读同步

导航「已读统计」汇总整个书库和插件上传的已读文章，首页也会显示累计文章数和词数。Web 正文读到结尾（进度至少 99.5%）或手动标记「已读」后计入，单纯打开长文章不会计入。阅读过的文章回滚位置仍保留完成记录；在目录中改为「未读」或「不感兴趣」，会移除这个刊物中的完成记录，其他已读出处仍保留。

按原文标题规范化去重：忽略大小写、空白及常见排版标点差异，合并同标题文章的所有出处。每篇只计一次，词数取各出处的最大值。同名但内容不同的文章也会合并，这是按标题去重的取舍。旧的已读标记、已到文末的阅读记录会利用已有缓存补入统计，不重新解析整本书、不调用 LLM。仅打开过、没有完成证据的旧文章不会推测为已读。

连接刊读 Chrome 插件 **v1.3.3 或更新版**：

1. 在 Web「设置 → Chrome 插件阅读同步」点击「生成 / 重置同步码」。
2. 打开插件设置，填写本 Web 的访问地址和专用阅读同步码，点击「连接并同步」。公网地址使用 HTTPS；本机、内网可使用 HTTP。此处填写的不是访问密码或模型 API Key。
3. 读完网页文章，在刊读侧栏点击「标记已读 · 同步 Web」。上传标题、原文链接、正文词数和完成时间，刷新 Web 统计即可看到。

同步失败仍保存在插件本机，可在设置中「重试同步」。连接前已经手动标记的记录也会补传；重复点击或重试不会重复计数。插件不会扫描浏览器历史记录，也不会把生成过摘要自动视为读完。同步码只允许上传阅读元数据，不能读取书库、修改模型或访问密钥；可在 Web 设置中停用或重置。服务器只保存同步码的哈希，插件本地保存连接凭据；不上传文章正文、摘要、生词内容或 LLM 配置。一个 Web 数据目录共用一份统计。

阅读统计保存在应用数据目录的 `reading-stats.json`，同步码哈希在 `reading-sync-auth.json`，都在源码仓库外。迁移服务器时随整个 Web 数据目录一并迁移。

## Windows 启动

需要 Python 3.10 或更新版本。

1. 首次运行双击 `setup_web.bat` 安装依赖。
2. 双击 `start_web.bat`，浏览器打开 `http://127.0.0.1:8766`。
3. 输入启动窗口显示的随机访问密码。

虚拟环境和日志存放在 `%LOCALAPPDATA%\KanduWebRuntime`，应用数据存放在 `%LOCALAPPDATA%\KanduWeb`，都位于源代码仓库之外。关闭启动窗口不会停止服务；重启电脑后重新运行启动入口。

新用户第一次启动时书库和模型配置为空，需要上传刊物并配置自己的模型。代码中不包含开发者的 API Key 或访问密码；本机已存在的 Web 数据目录会继续沿用。

## 手动安装及其他系统

请将虚拟环境建在源代码目录之外，例如：

```bash
python -m venv ../magread-web-venv
# Windows 使用 ../magread-web-venv/Scripts/python.exe；下例为 Linux/macOS
../magread-web-venv/bin/python -m pip install -r requirements.txt
../magread-web-venv/bin/python run_web.py
```

非 Windows 默认数据目录是 `~/.kandu_web`。可通过 `--data-dir` 或 `KANDU_WEB_DATA_DIR` 指定其他位置，程序拒绝把运行数据保存到本仓库内。

## 局域网访问

在服务器上右键 `enable_web_lan.bat`，选择「以管理员身份运行」。它仅允许专用网络中本地子网访问 TCP 8766。手机连接同一可互通的网络后，访问启动日志显示的实际网卡地址：

```text
http://服务器局域网IP:8766
```

使用同一个访问密码登录。电脑休眠、关机或断网时其他设备无法访问。当前是共用书库的个人阅读工具，不提供各自隔离的多用户账户；HTTP 部署仅用于可信局域网。实体手机链路和公网隧道仍需按部署环境验证。

## 隐私与数据

| 内容 | 默认保存位置（Windows） |
| --- | --- |
| 模型配置及 API Key | `%LOCALAPPDATA%\KanduWeb\settings.json` |
| 首次生成的访问密码 | `%LOCALAPPDATA%\KanduWeb\access-code.txt` |
| 会话签名和密码哈希 | `%LOCALAPPDATA%\KanduWeb\web-auth.json` |
| 刊物、缓存、生词本、阅读记录与讨论 | `%LOCALAPPDATA%\KanduWeb` 下各子目录 |
| Python 环境与运行日志 | `%LOCALAPPDATA%\KanduWebRuntime` |

这些都是服务器端本地数据，不属于公开源码。模型 API Key 由服务器用于请求你配置的模型服务；设置查询接口不向浏览器返回 Key。个人数据不是加密保险库，不要公开数据目录、日志或迁移备份。

`.gitignore` 排除设置、凭据、个人 EPUB/PDF、缓存、数据库和日志。提交前可运行 `python tools/check_public_files.py` 做基础检查；它会检查文件名、常见凭据模式和词典压缩包内容，不能替代人工检查或证明任意格式的密钥都可检测。

## 从桌面版导入（可选）

已有桌面版数据时，可以在 **Web 数据目录尚不存在** 的情况下进行一次快照导入。Windows 默认安装后的命令为：

```powershell
& "$env:LOCALAPPDATA\KanduWebRuntime\.venv\Scripts\python.exe" run_web.py --import-desktop "$env:LOCALAPPDATA\KanduDesktop"
```

导入包括已有摘要、生词、句解、阅读历史、标记、生词本、讨论及服务器端模型配置，全部保存到仓库外的 Web 数据目录。SQLite 使用一致性备份；原刊文件继续引用原路径，不重复复制。导入前应等待桌面生成任务结束。

这是一次性导入，各浏览器以后共享 Web 数据；桌面及 Android 原生程序的后续编辑不会自动同步。备份 Web 数据时需同时保留外部原刊文件。

## 后台任务与远程部署

最多两个后台任务并行，队列有界；同一本刊物的模型任务串行，页面显示进度。打开文章不会自动调用付费模型。服务重启后正在运行的任务会中断，已保存的分块结果可再次补缺，队列本身不跨重启恢复。

可以通过 Cloudflare Tunnel 连接自有域名。以下仅为 HTTPS 隧道后的启动参数示例，不会创建隧道或修改 DNS：

```bash
python run_web.py --host 127.0.0.1 --allowed-host read.example.com --secure-cookie
```

如果还要保留局域网 HTTP 登录，可改用 `python run_web.py --https-host read.example.com`：该域名自动加入白名单并使用 Secure 登录 Cookie，本机和局域网地址继续使用原来的 Cookie 设置。

Windows 启动器可读取仓库外的 `%LOCALAPPDATA%\KanduWebRuntime\web-launch.json`，例如：

```json
{"httpsHosts": ["read.example.com"], "publicUrl": "https://read.example.com"}
```

修改配置后需重启 Web 进程。未配置此文件时仍使用原来的局域网模式。`tools/start_web.ps1 -NoBrowser` 仅启动后台 Web 服务，不打开浏览器或输出访问密码。

对于已配置的本机 Cloudflare Tunnel，把官方 `cloudflared.exe` 与 `config.yml` 放在 `%LOCALAPPDATA%\KanduWebRuntime\cloudflared\`，隧道凭据文件继续保存在仓库外。配置中只映射阅读域名到 `http://127.0.0.1:8766`，最后添加 `http_status:404` 兜底规则。运行 `powershell -NoProfile -ExecutionPolicy Bypass -File tools/start_public_web.ps1` 启动服务、隧道并打开网页；添加 `-NoBrowser` 可用于 Windows 用户登录启动项，凭据与日志均不应放入源码。此脚本不会创建 Cloudflare 账号资源或安装系统服务。

Web 登录密码、HttpOnly/SameSite 会话、CSRF、请求来源与访问主机名校验默认启用。浏览器使用纯文本节点显示用户和模型内容，不执行返回的 HTML。

需要在另一台 Windows 电脑继续使用同一域名时，参阅 [同域名主机迁移流程](docs/HOST_MIGRATION.md)：包含旧机停服与备份、全部阅读数据及原刊迁移、绝对路径修正、隧道凭据复用、新机验证、登录启动项和失败回退。当前需要手动切换，同一时刻只让一台主机提供服务。

## 测试与开发

```bash
python -m pip install -r requirements-dev.txt
python tests/run_web_tests.py
python tests/run_reading_stats_tests.py
python tests/run_privacy_tests.py
python -m playwright install chromium
python tests/run_web_browser_tests.py
python tools/check_public_files.py
```

也可以用 `KANDU_TEST_CHROME` 指定本机 Chrome 可执行文件。设置 `KANDU_TEST_EXTENSION` 为解压的刊读 Chrome 插件目录，再运行 `python tests/run_chrome_sync_tests.py`，可联调实际插件与临时 Web 服务的配对、上传和去重；未设置时跳过。测试使用临时目录和模拟模型，不读取个人书库或调用付费模型。Windows 辅助脚本之外的跨平台运行尚未实机验证。

## 代码结构与许可

```text
webapp/          Flask API、后台任务、网页界面
kandu/           阅读核心及内置词典资源
tests/           隔离的接口、隐私与浏览器测试
tools/           Windows 启动、安装、防火墙及源码检查工具
run_web.py       独立服务入口
```

核心源码提取自 `haithink/magread-desktop` 的 `288f2de`，保留版权归属；新仓库不复制旧仓库历史或个人运行数据。变更见 [CHANGELOG.md](CHANGELOG.md)。

采用 [AGPL-3.0-only](LICENSE)。第三方资源保留各自声明，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；其中原 CET-4 词表来源及精简词典的上游版本仍待核实，本次拆分保留这些已知说明。
