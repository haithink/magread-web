# 将源码仓库公开前的检查

源码公开与个人 Web 服务的开放权限分别管理。公开 GitHub 仓库不会主动上传仓库外的书库、模型配置或访问凭据，也不会修改服务器登录要求。

## 当前源码与资源

- 文档采用 `read.example.com` 示例域名，实际域名与隧道配置保存在仓库外。
- 精简词典、四级词表与基础补充表使用固定版本 ECDICT，保留上游 MIT 许可证；详见 [第三方说明](../THIRD_PARTY_NOTICES.md)。
- `kandu/assets/dictionary_sources.json` 记录固定提交、源文件及产物 SHA-256、导出规则。
- 下载该固定版本的 `ecdict.csv` 到仓库外，再运行 `python tools/build_dictionary_assets.py <ecdict.csv>` 可重建精简词典与四级词表。脚本校验输入，不联网，不修改用户数据。
- 运行 `python tests/run_dictionary_assets_tests.py` 和 `python tools/check_public_files.py`，验证资源完整性、临时数据目录内离线查词以及常见隐私文件模式。

## 历史记录与 GitHub 附件

当前文件清理不会删除旧提交。公开前需要检查所有计划公开的分支和标签中，是否仍有旧词表、真实域名、凭据或私人阅读内容。重写历史会改变提交 ID，应先将旧历史备份到仓库外，核对清理后的当前文件与原工作成果一致，再由仓库所有者确认是否强制更新远端；其他电脑之后需要重新克隆或按新历史同步。

本地 Git 扫描不包含 GitHub 上的 Actions 日志及构建附件、Release 附件、Issues、Pull Requests 或 Wiki；这些内容需另外检查。仓库切回私有不能撤回已经被别人复制的公开内容。

公开前确认：

1. Git 工作区与要公开的历史均经过检查，旧凭据如有泄漏应先撤销或更换。
2. GitHub 日志、附件和讨论中没有模型配置、迁移包、原刊或运行数据。
3. 保留根目录 AGPL 许可证、第三方 MIT 许可证及来源记录。
4. 在 GitHub 仓库设置中将可见性改为 Public；此操作不会更改当前部署配置。
