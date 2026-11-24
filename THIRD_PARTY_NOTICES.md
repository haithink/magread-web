# 第三方资源说明

本项目原创代码版权归 haithink 及贡献者所有，采用 AGPL-3.0-only。
第三方依赖和资源保留各自的版权及许可；根目录 LICENSE 不替代其上游声明。

## PyMuPDF

- 依赖：`requirements.txt` 中的 `pymupdf==1.24.14`。
- 项目：https://github.com/pymupdf/PyMuPDF/tree/1.24.14
- 上游提供 GNU AGPL 和商业许可两种授权方式；本项目使用其开源许可。
- 上游许可说明：https://github.com/pymupdf/PyMuPDF/tree/1.24.14#license-and-copyright
- 本项目采用 AGPL-3.0-only。分发包含此依赖的应用时，需要履行适用的 AGPL 义务，
  包括提供对应版本的完整源码、构建材料和许可证。

## ECDICT 精简词典

- 文件：`kandu/assets/ecdict_compact.tsv.zip`，包含 `ecdict_compact.tsv`。
- 来源：`skywind3000/ECDICT` 的 `ecdict.csv`，固定提交 `39cfb9672c0906b63569e0453c2e6abfb9ab182a`。
- 固定版本：https://github.com/skywind3000/ECDICT/tree/39cfb9672c0906b63569e0453c2e6abfb9ab182a
- 该版本的 MIT 许可证：https://github.com/skywind3000/ECDICT/blob/39cfb9672c0906b63569e0453c2e6abfb9ab182a/LICENSE
- 已随附上游许可证原文：[LICENSES/ECDICT-MIT.txt](LICENSES/ECDICT-MIT.txt)。
- 保留源词条的 `word`、`pos`、`phonetic`、`translation`、`exchange` 五个字段；词头去首尾空白、转小写，同词头保留最后一条值并按首次出现顺序导出 UTF-8 TSV，保持离线查词的大小写兼容。字段内真实制表符和换行替换为空格。
- 来源和产物 SHA-256、行数及处理规则见 `kandu/assets/dictionary_sources.json`；ZIP 时间戳固定，可重复构建。
- 复现：`python tools/build_dictionary_assets.py <固定版本的ecdict.csv>`。

## CET-4 词表

- 文件：`kandu/assets/cet4_words.txt`。
- 已替换旧版来源不明的内置词表，来源与上述 ECDICT 固定版本相同，采用该版本的 MIT 许可证。
- 只选 `tag` 字段包含独立 `cet4` 标签的词条，词头去首尾空白、转小写、去重、排序，保留该版本提供的音标。
- 这是 ECDICT 的考试分级，不宣称为官方四级考试完整大纲；来源、词数和产物 SHA-256 见 `kandu/assets/dictionary_sources.json`，与精简词典使用同一脚本复现。
- 只替换随源码分发的资源，不覆盖已安装用户在仓库外保存的词表；用户已有词表继续与基础补充表取并集。

## 基础至 CET-4 的已知词补充表

- 文件：`kandu/assets/cet4_known_supplement.txt`，与原词表取并集，不覆盖用户本地词表。
- 来源：ECDICT 的 `ecdict.csv`，提交 `39cfb9672c0906b63569e0453c2e6abfb9ab182a`。
- 上游固定版本：https://github.com/skywind3000/ECDICT/tree/39cfb9672c0906b63569e0453c2e6abfb9ab182a
- 选择规则：保留含 `zk`（中考）、`gk`（高考）或 `cet4` 标签的词，转小写、去重、排序。
  这些是 ECDICT 的分级标注，不宣称为官方完整考试大纲。
- 许可证：MIT，随附 `LICENSES/ECDICT-MIT.txt`；导出过程不改动该版权声明。
- 源文件 SHA-256、行数和导出词数见 `kandu/assets/cet4_known_supplement.json`。
- 复现：`python tools/build_cet4_supplement.py <固定版本的ecdict.csv>`。

## 其他依赖与打包组件

可选音频转写使用 faster-whisper（MIT，SYSTRAN）及 CTranslate2、PyAV、ONNX Runtime 等依赖，见 `requirements-transcription.txt`。语音模型通过 Hugging Face 下载至仓库外，按所选模型仓库的许可证使用，不随本项目分发模型权重。NVIDIA CUDA / cuDNN 是可选运行库，按 NVIDIA 的许可使用。

播客文字与音频来自相应节目官方来源，仅由用户运行时获取；其内容版权属于原权利人，不因本项目开源而转为 AGPL。机器转写、个人摘要和音频均不纳入源码仓库。

`requirements.txt` 中的 Flask、Waitress、Requests、BeautifulSoup、Pillow 等依赖，
以及 Python、SQLite 等组件，按各自的上游许可证使用。Web 版不需要桌面 GUI 或 PyInstaller。
分发应用时应保留所包含第三方组件要求的版权、许可证及声明。

以上说明记录项目实际采用的上游版本、许可证和转换规则，不为上游每个历史数据来源作额外授权承诺。旧 Git 提交仍可能包含已替换资源；公开整个仓库前也应检查历史，不能只检查当前文件。
