# 刊读 Web · Magread Web

在浏览器中阅读英文刊物，生成中文摘要、生词与短语解释，查询单词、解析句子，并围绕文章与 AI 讨论。电脑、Android 和 iPhone 浏览器共用服务器上的书库与学习记录。

当前版本 **v0.1.0**。阅读核心由 `haithink/magread-desktop` 提取，本仓库可以独立安装运行，不需要桌面程序、浏览器插件或前端构建工具。

## 功能

- EPUB/PDF 上传、封面与刊物分类、最近阅读及继续阅读。
- 文章摘要与搜索，未读、已读、不感兴趣标记。
- 正文阅读、字号调整、全文搜索、词数与阅读位置保存，工具栏实时显示百分比、进度条和段落位置。
- 全文/逐段生词与短语、补缺生成，区分个人收藏与模型词表。
- 查词自动收藏、个人生词本查询与编辑、句子讲解；展示最近收藏/查词来源及原文片段，点击文章可回到来源段落。
- 按文章保存多轮 AI 讨论，显示生成进度，支持停止讨论回答。
- 粘贴完整模型配置 JSON，选择默认模型与已掌握的词汇级别。

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

后续可以通过 Cloudflare Tunnel 连接自有域名。以下仅为 HTTPS 隧道后的启动参数示例，不会创建隧道或修改 DNS：

```bash
python run_web.py --host 127.0.0.1 --allowed-host read.example.com --secure-cookie
```

Web 登录密码、HttpOnly/SameSite 会话、CSRF、请求来源与访问主机名校验默认启用。浏览器使用纯文本节点显示用户和模型内容，不执行返回的 HTML。

## 测试与开发

```bash
python -m pip install -r requirements-dev.txt
python tests/run_web_tests.py
python tests/run_privacy_tests.py
python -m playwright install chromium
python tests/run_web_browser_tests.py
python tools/check_public_files.py
```

也可以用 `KANDU_TEST_CHROME` 指定本机 Chrome 可执行文件。测试使用临时目录和模拟模型，不读取个人书库或调用付费模型。Windows 辅助脚本之外的跨平台运行尚未实机验证。

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
