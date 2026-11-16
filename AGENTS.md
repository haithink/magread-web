# 仓库维护约定

- 本仓库是刊读 Web 版；版本号在 `webapp/__init__.py`，变更记入 `CHANGELOG.md`，开发中先写「未发布」。
- 所有运行数据必须放在仓库外。不得提交真实 API Key、模型配置、访问密码、用户书库、摘要、阅读记录、日志或迁移包。
- 测试使用临时目录和模拟模型，不读取个人书库或调用付费模型。
- 阅读核心来自 `magread-desktop`，保留 AGPL 许可证、来源说明及第三方声明；不要从桌面仓库复制运行数据或整个 Git 历史。
- Windows 启动日志与虚拟环境放在 `%LOCALAPPDATA%\KanduWebRuntime`，应用数据默认放在 `%LOCALAPPDATA%\KanduWeb`。
