# Bid Interpretation Agent

标书解读 agent 的输入处理层，当前实现上传、递归解压、文件目录展示、独立的单文件切片预览，以及按 `bid_id` 对解包文件进行批量切片。

## Run

### 初始化环境

第一次拉代码或依赖变更后执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 本机启动

只在自己电脑上测试时使用：

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

打开 `http://127.0.0.1:8000/` 可以使用页面。

服务启动后，终端里会持续显示接口访问日志和报错日志。不要关闭这个终端窗口；需要停止服务时，在这个终端按 `Ctrl+C`。

页面包含两个功能区：

- 上传并解包：上传单文件或压缩包，展示解包后的目录树。
- 批量切片：上传并解包后，勾选目录树中的文件，点击“切片所选文件”异步切片。
- 单文件切片：单独上传 `.pdf/.doc/.docx/.wps/.xls/.xlsx`，展示可展开的切片结果。

### 局域网测试

如果需要在同一局域网内的其他设备（手机、平板等）上测试，启动时需要绑定 `0.0.0.0`：

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

然后通过本机局域网 IP 访问。查看本机 IP：

**macOS / Linux：**
```bash
ipconfig getifaddr en0    # macOS Wi-Fi
# 或
hostname -I               # Linux
```

**Windows：**
```bash
ipconfig
```

假设本机 IP 为 `192.168.1.100`，其他设备在浏览器打开 `http://192.168.1.100:8000/` 即可访问上传页面。

> **注意：** 确保防火墙允许 8000 端口的入站连接，且所有设备处于同一局域网内。

### 查看报错日志

自己启动服务后，所有关键报错都会打印在启动服务的终端里。例如：

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

如果上传 `.doc` 或 `.wps` 时转换失败，终端里重点看这几类日志：

- `DOC parse error at soffice_convert`：LibreOffice 转 `.docx` 失败，会带上失败代码位置、输入文件、临时 profile、stdout 和 stderr。
- `WPS parse error at soffice_convert`：LibreOffice 转 `.wps` 失败，会带上失败代码位置、输入文件、临时 profile、stdout 和 stderr。
- `DOC parse error at aspose_fallback`：Aspose 兜底解析失败，会带上失败代码位置和具体异常。
- `DOC parse error at doc_parse_failed`：LibreOffice 和 Aspose 都失败，接口准备返回 400。

常见处理方式：

- 如果提示找不到 `soffice`，说明 LibreOffice 命令行工具不可用，需要安装 LibreOffice 或确认环境变量。
- 如果看到 `libc++abi`、`Unspecified Application Error`、`NotConnectedException`，通常是 LibreOffice headless 环境/profile 异常；当前代码已使用独立临时 profile 和短文件名降低这类问题。
- 如果看到 `Aspose 评估版输出被截断`，说明已进入 Aspose 兜底，但未配置 license，输出不完整，当前接口会拒绝返回误导性的切片。

## API

- `POST /api/interpretation/v1/upload`
  - `multipart/form-data`，字段名：`file`
  - 后端生成 `bid_id`，同步完成上传和解压。

- `GET /api/interpretation/v1/bids/{bid_id}/tree`
  - 返回前端展示用目录树。

- `GET /api/interpretation/v1/bids/{bid_id}/files`
  - 返回后续切片流程使用的扁平文件列表。

- `GET /api/interpretation/v1/bids/{bid_id}/files/{relative_path:path}/download`
  - 下载或预览展开后的文件。

- `POST /api/interpretation/v1/chunk`
  - `multipart/form-data`，字段名：`file`
  - 独立上传单个文件并返回切片结果，不依赖 `bid_id`。
  - Word/PDF 优先按目录里的一级章切片；Excel 按工作表切片。
  - 每次切片会生成 `chunk_job_id`，并把结果保存到 `storage/chunks/`。

- `POST /api/interpretation/v1/bids/{bid_id}/chunk-jobs`
  - 对已解包的 `bid_id` 启动异步批量切片任务。
  - 只处理 `.pdf/.doc/.docx/.wps/.xls/.xlsx`。
  - 不传请求体时默认切片全部可切片文件。
  - 传 JSON 请求体时只切片选中的文件：

```json
{
  "relative_paths": [
    "第一章/招标公告.pdf",
    "第六章 技术标准和要求/技术规范书.docx"
  ]
}
```

  - 返回 `batch_chunk_job_id`、`status_url`、`result_url`。

- `GET /api/interpretation/v1/bids/{bid_id}/chunk-jobs/{batch_chunk_job_id}`
  - 查询批量切片任务状态和进度。

- `GET /api/interpretation/v1/bids/{bid_id}/chunk-jobs/{batch_chunk_job_id}/result`
  - 获取批量切片结果。
  - 任务未完成时返回明确错误。

## Supported Input

- 压缩包：`.zip`、`.rar`、`.7z`
- 普通文件：`.pdf`、`.xls`、`.xlsx`、`.doc`、`.docx`、`.wps`

## Chunking

单文件切片接口支持：

- `.pdf`：使用 `pypdf` 提取每页文本，尽量返回 `page_start/page_end`。
- `.docx`：使用 `python-docx` 提取段落文本；Word 表格如可读取，会按普通文本行追加。
- `.doc`：通过 LibreOffice `soffice` 转成 `.docx` 后解析；如果缺少 `soffice` 会返回明确错误。
- `.wps`：通过 LibreOffice `soffice` 转成 `.docx` 后解析；第一版不走 Aspose 兜底。
- `.xlsx`：使用 `openpyxl`，每个工作表一个切片。
- `.xls`：使用 `xlrd`，每个工作表一个切片。

PDF 优先使用文件内置大纲/书签定位一级章页码；如果没有可用大纲，再识别目录页中的 `第X章` 一级章标题并按目录章切正文。DOC/DOCX 使用目录段落识别。目录前内容会作为 `封面` chunk，目录本身不单独生成 chunk。默认 `max_chunk_chars` 为 `200000`，当前阶段基本不主动做二级拆分。

如果没有识别到目录章结构，会降级到标题正则切片并给出 warning。

每个 chunk 只输出普通文本，不包含表格增强结构。当前阶段先保证章节级切片稳定，表格解析后续作为独立能力重新设计。

切片结果会落盘，目录结构示例：

```text
storage/chunks/
└── {chunk_job_id}/
    └── {文件名}/
        ├── original/
        │   └── 招标文件.docx
        ├── chunks.json
        └── chunks/
            ├── chunk_001_第一章_招标公告.md
            └── chunk_002_第二章_投标人须知.md
```

- `original/` 保存本次上传的原始文件。
- `chunks.json` 保存完整接口返回结果。
- `chunks/` 下每个 `.md` 文件对应一个具体切片，方便人工打开查看。

批量切片结果会落盘到：

```text
storage/chunks/
└── batches/
    └── {batch_chunk_job_id}/
        ├── manifest.json
        └── files/
            ├── 001_招标公告/
            │   ├── chunks.json
            │   └── chunks/
            └── 002_报价表/
                ├── chunks.json
                └── chunks/
```

第一版批量切片采用“遇错中断”：某个文件失败后任务状态变为 `failed`，已成功文件的结果会保留在 `manifest.json` 和对应文件目录中。

`.rar` 需要系统安装 `unar`、`unrar` 或 `7z/7zz`。不建议使用 `bsdtar` 解 RAR5，可能出现目录不完整。

macOS 可以用 Homebrew 安装：

```bash
brew install unar
```

如果使用 conda，也可以安装 p7zip：

```bash
conda install -y -c conda-forge p7zip
```

## Test

```bash
.venv/bin/python -m pytest -q
```
