# Bid Interpretation Agent

标书解读 agent 的输入处理层，当前第一版实现上传、递归解压和文件目录展示。

## Run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000/` 可以使用最小上传页面。

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

## Supported Input

- 压缩包：`.zip`、`.rar`、`.7z`
- 普通文件：`.pdf`、`.xls`、`.xlsx`、`.doc`、`.docx`

`.rar` 依赖 `rarfile`，运行环境还需要安装 `unar` 或 `unrar`。
如果系统里有 `bsdtar`，服务会优先用 `bsdtar` 解 `.rar`，兼容性通常更好。

macOS 可以用 Homebrew 安装：

```bash
brew install unar
```

## Test

```bash
.venv/bin/python -m pytest -q
```
