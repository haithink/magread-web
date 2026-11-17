# 同域名迁移到另一台 Windows 电脑

适用于已有刊读 Web 和本地管理的 Cloudflare Tunnel，需要将服务从旧电脑 A 切换到新电脑 B 的情况。以 `read.example.com` 为例；其他部署替换为自己的域名。

域名的 DNS 记录仍指向原隧道，新电脑使用同一隧道的凭据连接即可。Cloudflare 上已有的 HTTP 转 HTTPS 规则也继续保留，无须重新创建隧道、修改 DNS 或购买服务器。

当前采用手动切换：准备新机 → 旧机停止写入 → 备份并传输 → 新机修正路径 → 本机验证 → 启动隧道接管。切换期间会短暂无法访问。

## 先确认两件事

- 同一时刻只让一台电脑通过此隧道提供刊读服务。Cloudflare 支持同一隧道的多个连接器，但不会为刊读自动同步数据、会话中的后台任务或确定主备顺序；请求可能进入不同机器，造成记录分叉或任务查询失败。
- GitHub 只保存源码。`git clone` / `git pull` 不会带来书籍、摘要、生词本、阅读进度、模型密钥和隧道凭据，必须单独迁移。

## 1. 提前准备新电脑

安装 Git、Python 3.10 或更新版本，在选定目录下载源码：

```powershell
git clone git@github.com:haithink/magread-web.git
Set-Location .\magread-web
.\setup_web.bat
```

SSH 克隆要求新电脑已配置 GitHub SSH 访问；也可使用 GitHub 的 HTTPS 克隆地址。已有源码时在工作区干净的前提下运行 `git pull --ff-only`，确保两台使用同一版本。

从 Cloudflare 官方发布渠道下载适合新电脑架构的 `cloudflared`，将可执行文件命名为 `cloudflared.exe`，放到：

```text
%LOCALAPPDATA%\KanduWebRuntime\cloudflared\cloudflared.exe
```

先安装依赖即可，暂不运行 `start_web.bat` 或隧道。若新机已有测试数据，停服后将原数据目录完整另存为备份，再恢复旧机快照，避免混合两套书库。

不要直接复制旧机的 `.venv`：它包含旧机 Python 和目录信息，应在新机重新安装。

## 2. 旧机停止写入并禁止自动重连

先关闭手机、浏览器中的阅读页，等待摘要、生词、讨论和文件上传任务完成，再停止服务。当前后台任务不会跨进程迁移。

旧机当前的登录启动项为：

```text
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\KanduWeb.vbs
```

把此文件移到启动目录之外留作备份，或在 Windows 启动应用中禁用对应入口。若另外配置过计划任务或系统服务，也要停止并禁用该部署的自动启动，防止旧机重启后再次连接隧道。

可以用 PowerShell 查看候选进程和端口：

```powershell
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*run_web.py*' -or $_.Name -eq 'cloudflared.exe' } |
    Select-Object ProcessId, ParentProcessId, ExecutablePath, CommandLine

Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
```

核对命令行中的仓库路径、数据目录及隧道配置路径，仅结束本次部署的 `cloudflared` 和 `run_web.py` 进程。先断开隧道，再停止 Web 服务。Windows 虚拟环境可能有 Python 父子两个进程，均需核对；不要结束其他 Python 项目或其他隧道。

启动脚本关闭窗口不会停止后台服务。确认 8766 不再监听后再复制数据；已经安装成系统服务的部署还需确认服务已停止。

## 3. 完整复制数据和隧道配置

在旧机服务停止后制作完整快照，通过移动硬盘或可信局域网传输。路径中的 `%LOCALAPPDATA%` 和 `%USERPROFILE%` 均指各自电脑当前 Windows 用户的目录，不要照搬旧用户名。

| 内容 | 旧机默认位置 | 新机处理 |
| --- | --- | --- |
| 全部应用数据 | `%LOCALAPPDATA%\KanduWeb` | 完整恢复到新机数据目录 |
| 外部原刊文件 | `books.json` 各条目的 `path` 指向的位置 | 复制所有 EPUB/PDF，保持内容一致 |
| 域名启动配置 | `%LOCALAPPDATA%\KanduWebRuntime\web-launch.json` | 复制并检查域名 |
| 隧道配置 | `%LOCALAPPDATA%\KanduWebRuntime\cloudflared\config.yml` | 复制并修正凭据路径 |
| 此隧道的凭据 JSON | `config.yml` 中 `credentials-file` 指向的文件，通常在 `%USERPROFILE%\.cloudflared\` | 私下复制到新机，再更新配置路径 |

`KanduWeb` 包含 `books.json`、`settings.json`、`web-auth.json`、`access-code.txt`、缓存、封面、生词本 SQLite 数据库、阅读进度、文章标记和讨论等；`reading-stats.json` 保存已读清单，`reading-activity.sqlite3` 保存有效阅读耗时及每日曲线数据。应复制整个目录，不要只选摘要或数据库；数据库存在配套的 journal/WAL 文件时也保留在完整快照中。

另外保留 `config-auth.json`（模型配置独立密码哈希）、`config-access.sqlite3`（临时解锁及验证限流）和 `upload-quota.sqlite3`（全站当日上传量及预留额度）。独立密码不保存在 `access-code.txt`；忘记时在服务器运行 `tools/set_config_password.py` 重新设置，旧解锁随即失效。每日 500 MB 额度按新机的本地日期计算，迁移时检查系统日期、时区正确。

从桌面版导入的书籍可能引用数据目录外的原刊。仅复制 `KanduWeb` 不保证包含全部书籍，需根据 `books.json` 检查。

如果使用了自定义 `KANDU_WEB_DATA_DIR` 或 `--data-dir`，以实际数据目录为准，并在新机配置相同用途的路径。新机启动器默认读取 `KANDU_WEB_DATA_DIR`，否则使用 `%LOCALAPPDATA%\KanduWeb`。

不用复制 `.venv`、日志、`web-server.pid` 或 `tunnel-launch.pid`，这些在新机重新生成。运行已有隧道使用该隧道的凭据 JSON 即可，不需要复制有更大管理权限的 `cert.pem` 或重新登录 Cloudflare。迁移不需要分享 GitHub SSH 私钥。

**上述快照包含真实模型 API Key、访问密码及隧道凭据，只能私下传输，不能加入 Git 仓库、GitHub Release 或公开附件。** 本文只记录操作，不包含凭据内容。

## 4. 修正新机的文件路径

如果新机盘符、Windows 用户名或书库目录不同，必须在启动前调整下列配置。修改前保留原始快照，JSON 文件使用 UTF-8 保存。

| 文件 | 需要检查的字段 |
| --- | --- |
| 数据目录中的 `books.json` | 每本书的 `path`，改为新机上对应原刊的实际绝对路径 |
| 数据目录中的 `settings.json` | 非空的 `booksDir`、`libraryDir`，指向新机正确目录 |
| `cloudflared\config.yml` | `credentials-file`，改为新机凭据 JSON 的绝对路径 |
| `web-launch.json` | 本机隧道使用 `cloudflareHosts` 和 `publicUrl`，保持原来的公网域名；旧的 `httpsHosts` 仅设置 HTTPS Cookie |
| Windows 登录启动项 | 更新为新机源码目录中的脚本路径 |

例如把书籍路径 `I:\KanduLibrary\sample.epub` 改为 `D:\KanduLibrary\sample.epub`，JSON 内写作 `"path": "D:\\KanduLibrary\\sample.epub"`。可在文本编辑器中批量替换确认过的书库路径前缀，但不要修改书籍 `id`、文章编号或缓存文件名。不要删掉旧条目后重新上传来代替路径修正，否则可能失去原记录的对应关系。

启动前可运行以下只读检查，确认所有原刊路径存在且文件大小与索引一致。默认数据目录以外的部署需先设置 `KANDU_WEB_DATA_DIR`：

```powershell
$dataDir = if ($env:KANDU_WEB_DATA_DIR) { $env:KANDU_WEB_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'KanduWeb' }
$books = Get-Content -LiteralPath (Join-Path $dataDir 'books.json') -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($book in $books) {
    if (-not (Test-Path -LiteralPath $book.path -PathType Leaf)) {
        Write-Output ('MISSING: ' + $book.fileName)
    } elseif ((Get-Item -LiteralPath $book.path).Length -ne $book.size) {
        Write-Output ('SIZE MISMATCH: ' + $book.fileName)
    }
}
```

没有输出表示这两项检查通过；不代表已经比较文件内容，仍应传输原文件而不是同名的其他版本。

## 5. 保留原隧道和域名配置

`web-launch.json` 示例：

```json
{"cloudflareHosts": ["read.example.com"], "publicUrl": "https://read.example.com"}
```

`config.yml` 示例（尖括号内容必须替换；UUID 和凭据沿用旧机，凭据存放路径按新机实际填写）：

```yaml
tunnel: <原隧道UUID>
credentials-file: 'D:\private\cloudflared\<原隧道UUID>.json'
protocol: http2
edge-ip-version: '4'
metrics: 127.0.0.1:49312
ingress:
  - hostname: read.example.com
    service: http://127.0.0.1:8766
  - service: http_status:404
```

如果示例中的端口被其他程序占用，先查明用途，再同步调整相关配置。不要通过 HTTP Host 改写绕过刊读的域名白名单；`cloudflareHosts` 会为该域名启用 Secure 会话 Cookie，同时保留本机和局域网 HTTP 登录，并仅信任本机回环连接提供的 Cloudflare 访客地址。迁移时可把旧配置中的该域名从 `httpsHosts` 移到 `cloudflareHosts`，重启后生效；其他代理不能直接使用此模式。

登录限流数据也保存在应用数据目录的 `login-rate.sqlite3`，随数据目录复制即可延续剩余等待时间。它不包含明文 IP 或密码；不要为了绕过登录限制而删除此文件。

在新机 PowerShell 中验证配置（此命令不启动连接）：

```powershell
$tunnelDir = Join-Path $env:LOCALAPPDATA 'KanduWebRuntime\cloudflared'
& (Join-Path $tunnelDir 'cloudflared.exe') tunnel --config (Join-Path $tunnelDir 'config.yml') ingress validate
```

## 6. 启动新机并确认接管

先在新机源码目录仅启动 Web 服务，不连接隧道：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\start_web.ps1 -NoBrowser
```

打开 `http://127.0.0.1:8766`，使用恢复的访问密码登录。确认书库、原刊、已有摘要、生词本及最近阅读记录正常，模型配置名称正确。不要发送测试生成请求来检查数据是否迁移；已有内容可直接验证。

确认旧机隧道已停止，再从新机源码目录启动公网服务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\start_public_web.ps1
```

通过 `https://read.example.com` 验证：

1. 未登录时仍显示密码登录界面，登录密码保持原值。
2. 书库、摘要、个人生词及已保存的文章讨论能读取，原刊能打开。
3. 阅读一小段后刷新，进度保存正常。
4. 使用手机流量访问同一域名，确认不依赖新机的局域网地址。
5. 若原来配置过 HTTP 转 HTTPS，访问 HTTP 地址仍应自动跳转。

必要时重新登录。首次接管可能需要等待连接建立；当前启动脚本提示后台进程已运行，不等于已经完成公网连通性验证。

日志位于 `%LOCALAPPDATA%\KanduWebRuntime\web-server-error.log` 和 `cloudflared\tunnel-error.log`。不要把包含个人信息的完整日志直接公开。

## 7. 新机设置登录后自动启动

验证接管正常后，在新机按 `Win+R` 输入 `shell:startup`，创建一个快捷方式，目标填写：

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "D:\apps\magread-web\tools\start_public_web.ps1" -NoBrowser
```

把示例源码目录替换为实际目录，快捷方式的“起始位置”设为新机源码目录。此入口在当前 Windows 用户登录后启动服务和隧道，不是无人登录时就启动的系统服务。避免同时保留另一个启动同一部署的入口。

新机需保持开机、联网并避免睡眠；旧机保持隧道启动项禁用。可安排一次新机重启并登录，验证公网访问恢复。

## 迁移失败与以后切回

- 新机未接管成功：先停止新机隧道和 Web 服务，再恢复旧机启动项并启动旧机，原 DNS 无须修改。保留新机现场和迁移前完整快照便于排查。
- 新机已产生新进度、生词或讨论后需要切回：新机已是最新数据源，须先停止写入、完整备份，再按同样流程从新机迁回旧机，并按旧机目录修正路径。直接启用旧机旧快照会丢失接管期间的新记录。
- 如果已在两台机器分别写入：先停止继续写入、分别备份，确定保留哪份数据；当前没有自动合并两份历史记录的功能，不能用两边互相覆盖文件来同步。

这是一份人工迁移流程，不会自动执行备份或主备切换。自动接管还需要独立的数据同步与主备协调设计。

## Cloudflare 官方参考

隧道副本与本地管理命令参考（核对日期：2026-11-17）：

```text
https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-availability/deploy-replicas/
https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/tunnel-useful-commands/
```
