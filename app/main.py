import json
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from app.services.chunk_processor import CHUNKABLE_EXTENSIONS, ChunkProcessingError, ChunkProcessor, safe_path_name
from app.services.input_processor import (
    BidNotFoundError,
    InputProcessingError,
    InputProcessor,
    UnsafeArchivePathError,
)

app = FastAPI(title="Bid Interpretation Input Service")
processor = InputProcessor(storage_root=Path("storage/uploads"))
chunk_processor = ChunkProcessor()


class BatchChunkRequest(BaseModel):
    relative_paths: list[str] | None = None



@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
    <!doctype html>
    <html lang="zh-CN">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>标书输入处理</title>
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2937; }
          main { max-width: 920px; margin: 0 auto; }
          section { border-top: 1px solid #e5e7eb; padding: 24px 0; }
          section:first-child { border-top: 0; padding-top: 0; }
          form { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
          button { padding: 8px 14px; border: 1px solid #2563eb; background: #2563eb; color: white; border-radius: 6px; cursor: pointer; }
          input { border: 1px solid #d1d5db; padding: 8px; border-radius: 6px; }
          .meta { color: #4b5563; margin-bottom: 12px; }
          ul { list-style: none; padding-left: 20px; }
          li { margin: 5px 0; }
          .dir { font-weight: 650; }
          .file a { color: #2563eb; text-decoration: none; }
          .file label { display: inline-flex; align-items: center; gap: 6px; }
          .file input[type="checkbox"] { padding: 0; }
          .disabled { color: #9ca3af; }
          .selection-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 10px 0 14px; }
          .error { color: #b91c1c; }
          .warning { color: #92400e; background: #fffbeb; border: 1px solid #fde68a; padding: 8px 10px; border-radius: 6px; }
          details { border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px 12px; margin: 10px 0; background: #ffffff; }
          summary { cursor: pointer; font-weight: 650; }
          pre { white-space: pre-wrap; word-break: break-word; line-height: 1.55; color: #111827; background: #f9fafb; padding: 12px; border-radius: 6px; overflow: auto; }
          .secondary { border-color: #6b7280; background: #ffffff; color: #374151; }
          .file-group { border: 1px solid #d1d5db; border-radius: 6px; padding: 12px; margin: 14px 0; }
          h1 { margin-bottom: 8px; }
          h2 { margin: 0 0 14px; font-size: 20px; }
        </style>
      </head>
      <body>
        <main>
          <h1>标书输入处理</h1>
          <section>
            <h2>上传并解包</h2>
            <form id="upload-form">
              <input id="file" name="file" type="file" required />
              <button type="submit">上传</button>
            </form>
            <div id="result"></div>
          </section>
          <section>
            <h2>单文件切片</h2>
            <form id="chunk-form">
              <input id="chunk-file" name="file" type="file" accept=".pdf,.doc,.docx,.wps,.xls,.xlsx" required />
              <button type="submit">切片</button>
            </form>
            <div id="chunk-result"></div>
          </section>
        </main>
        <script>
          const form = document.getElementById("upload-form");
          const result = document.getElementById("result");
          const chunkForm = document.getElementById("chunk-form");
          const chunkResult = document.getElementById("chunk-result");
          let currentBidId = null;
          let currentBatchStatusUrl = null;
          let currentBatchResultUrl = null;
          let currentChunkablePaths = new Set();

          const chunkableExtensions = new Set([".pdf", ".doc", ".docx", ".wps", ".xls", ".xlsx"]);

          function renderNode(node) {
            if (node.type === "directory") {
              const children = (node.children || []).map(renderNode).join("");
              return `<li><span class="dir">${node.name}/</span><ul>${children}</ul></li>`;
            }
            const relativePath = escapeHtml(node.relative_path || "");
            const isChunkable = currentChunkablePaths.has(node.relative_path || "");
            const checkbox = isChunkable
              ? `<input class="chunk-file-checkbox" type="checkbox" data-relative-path="${relativePath}" checked />`
              : `<input type="checkbox" disabled />`;
            const disabledText = isChunkable ? "" : ` <span class="disabled">不可切片</span>`;
            return `
              <li class="file">
                <label>
                  ${checkbox}
                  <a href="${node.download_url}" target="_blank">${escapeHtml(node.name)}</a>
                </label>
                <span class="meta">${node.size} bytes</span>${disabledText}
              </li>
            `;
          }

          async function fetchFilesMap(filesUrl) {
            const response = await fetch(filesUrl);
            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || "获取文件列表失败");
            }
            return new Set(
              (payload.files || [])
                .filter((file) => chunkableExtensions.has(String(file.extension || "").toLowerCase()))
                .map((file) => file.relative_path)
            );
          }

          function updateSelectedCount() {
            const checkboxes = Array.from(document.querySelectorAll(".chunk-file-checkbox"));
            const selected = checkboxes.filter((item) => item.checked).length;
            const total = checkboxes.length;
            const label = document.getElementById("selected-file-count");
            const button = document.getElementById("batch-chunk-button");
            if (label) label.textContent = `已选择 ${selected} / ${total} 个文件`;
            if (button) button.disabled = selected === 0;
          }

          function selectedRelativePaths() {
            return Array.from(document.querySelectorAll(".chunk-file-checkbox"))
              .filter((item) => item.checked)
              .map((item) => item.dataset.relativePath);
          }

          form.addEventListener("submit", async (event) => {
            event.preventDefault();
            result.innerHTML = "<p>处理中...</p>";
            const data = new FormData(form);
            const response = await fetch("/api/interpretation/v1/upload", { method: "POST", body: data });
            const payload = await response.json();
            if (!response.ok) {
              result.innerHTML = `<p class="error">${payload.detail || "上传失败"}</p>`;
              return;
            }
            const treeResponse = await fetch(payload.tree_url);
            const tree = await treeResponse.json();
            try {
              currentChunkablePaths = await fetchFilesMap(payload.files_url);
            } catch (error) {
              result.innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
              return;
            }
            currentBidId = payload.bid_id;
            result.innerHTML = `
              <p class="meta">bid_id: ${payload.bid_id}</p>
              <div class="selection-bar">
                <button id="select-all-files" class="secondary" type="button">全选可切片文件</button>
                <button id="clear-selected-files" class="secondary" type="button">清空选择</button>
                <span id="selected-file-count" class="meta"></span>
                <button id="batch-chunk-button" class="secondary" type="button">切片所选文件</button>
              </div>
              <div id="batch-result"></div>
              <ul>${renderNode(tree.root)}</ul>
            `;
            updateSelectedCount();
          });

          function escapeHtml(value) {
            return String(value || "")
              .replaceAll("&", "&amp;")
              .replaceAll("<", "&lt;")
              .replaceAll(">", "&gt;")
              .replaceAll('"', "&quot;")
              .replaceAll("'", "&#039;");
          }

          function renderSource(source) {
            if (source.sheet_name) {
              return `sheet: ${escapeHtml(source.sheet_name)}`;
            }
            if (source.page_start && source.page_end) {
              return `页码: ${source.page_start}-${source.page_end}`;
            }
            return "来源: 文档";
          }

          function renderChunk(chunk, index) {
            const open = index === 0 ? " open" : "";
            const title = escapeHtml(chunk.title || chunk.chunk_id);
            const text = escapeHtml(chunk.text || "");
            const part = chunk.part_total > 1 ? ` · part ${chunk.part_index}/${chunk.part_total}` : "";
            return `
              <details${open}>
                <summary>${chunk.order}. ${title}</summary>
                <p class="meta">${chunk.chunk_id} · ${chunk.char_count} chars · ${renderSource(chunk.source || {})}${part}</p>
                <pre>${text}</pre>
              </details>
            `;
          }

          function renderBatchFile(file, index) {
            const chunks = (file.chunks || []).map(renderChunk).join("");
            return `
              <section class="file-group">
                <h3>${index + 1}. ${escapeHtml(file.relative_path)}</h3>
                <p class="meta">${escapeHtml(file.extension)} · ${file.chunk_count} chunks</p>
                ${chunks}
              </section>
            `;
          }

          async function pollBatchStatus(statusUrl, resultUrl) {
            const batchContainer = document.getElementById("batch-result");
            if (!batchContainer) return;

            const statusResponse = await fetch(statusUrl);
            const statusPayload = await statusResponse.json();
            if (!statusResponse.ok) {
              batchContainer.innerHTML = `<p class="error">${escapeHtml(statusPayload.detail || "获取批量切片状态失败")}</p>`;
              return;
            }

            if (statusPayload.status === "pending" || statusPayload.status === "running") {
              batchContainer.innerHTML = `
                <p class="meta">批量切片状态：${escapeHtml(statusPayload.status)}</p>
                <p class="meta">进度：${statusPayload.processed_files}/${statusPayload.total_files}</p>
                <p class="meta">当前文件：${escapeHtml(statusPayload.current_file || "-")}</p>
              `;
              window.setTimeout(() => pollBatchStatus(statusUrl, resultUrl), 1200);
              return;
            }

            const resultResponse = await fetch(resultUrl);
            const resultPayload = await resultResponse.json();
            if (!resultResponse.ok) {
              batchContainer.innerHTML = `<p class="error">${escapeHtml(resultPayload.detail || "获取批量切片结果失败")}</p>`;
              return;
            }

            const error = resultPayload.error
              ? `<p class="error">失败文件：${escapeHtml(resultPayload.error.relative_path)}；原因：${escapeHtml(resultPayload.error.message)}</p>`
              : "";
            const files = (resultPayload.files || []).map(renderBatchFile).join("");
            batchContainer.innerHTML = `
              <p class="meta">批量切片状态：${escapeHtml(resultPayload.status)}</p>
              <p class="meta">进度：${resultPayload.processed_files}/${resultPayload.total_files}</p>
              <p class="meta">batch_chunk_job_id: ${escapeHtml(resultPayload.batch_chunk_job_id)}</p>
              ${error}
              ${files}
            `;
          }

          result.addEventListener("click", async (event) => {
            if (event.target.id === "select-all-files") {
              document.querySelectorAll(".chunk-file-checkbox").forEach((item) => {
                item.checked = true;
              });
              updateSelectedCount();
              return;
            }
            if (event.target.id === "clear-selected-files") {
              document.querySelectorAll(".chunk-file-checkbox").forEach((item) => {
                item.checked = false;
              });
              updateSelectedCount();
              return;
            }
            if (event.target.id !== "batch-chunk-button") return;
            const batchContainer = document.getElementById("batch-result");
            const relativePaths = selectedRelativePaths();
            if (relativePaths.length === 0) {
              batchContainer.innerHTML = `<p class="error">请选择至少一个需要切片的文件</p>`;
              return;
            }
            batchContainer.innerHTML = "<p>正在启动批量切片任务...</p>";
            const response = await fetch(`/api/interpretation/v1/bids/${currentBidId}/chunk-jobs`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ relative_paths: relativePaths }),
            });
            const payload = await response.json();
            if (!response.ok) {
              batchContainer.innerHTML = `<p class="error">${escapeHtml(payload.detail || "启动批量切片失败")}</p>`;
              return;
            }
            currentBatchStatusUrl = payload.status_url;
            currentBatchResultUrl = payload.result_url;
            batchContainer.innerHTML = `<p class="meta">批量切片任务已启动：${escapeHtml(payload.batch_chunk_job_id)}</p>`;
            pollBatchStatus(currentBatchStatusUrl, currentBatchResultUrl);
          });

          result.addEventListener("change", (event) => {
            if (!event.target.classList.contains("chunk-file-checkbox")) return;
            updateSelectedCount();
          });

          chunkForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            chunkResult.innerHTML = "<p>切片中...</p>";
            const data = new FormData(chunkForm);
            const response = await fetch("/api/interpretation/v1/chunk", { method: "POST", body: data });
            const payload = await response.json();
            if (!response.ok) {
              chunkResult.innerHTML = `<p class="error">${escapeHtml(payload.detail || "切片失败")}</p>`;
              return;
            }
            const warnings = (payload.warnings || []).map((item) => `<p class="warning">${escapeHtml(item)}</p>`).join("");
            const chunks = (payload.chunks || []).map(renderChunk).join("");
            chunkResult.innerHTML = `
              <p class="meta">${escapeHtml(payload.filename)} · ${payload.extension} · ${payload.chunks.length} chunks</p>
              <p class="meta">chunk_job_id: ${escapeHtml(payload.chunk_job_id)}</p>
              <p class="meta">chunks_file: ${escapeHtml(payload.chunks_file)}</p>
              <p class="meta">chunks_dir: ${escapeHtml(payload.chunks_dir)}</p>
              ${warnings}
              ${chunks}
            `;
          });
        </script>
      </body>
    </html>
    """


@app.post("/api/interpretation/v1/upload")
async def upload_bid_file(file: UploadFile = File(...)) -> dict:
    try:
        result = await processor.process_upload(file)
    except UnsafeArchivePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InputProcessingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result


@app.get("/api/interpretation/v1/bids/{bid_id}/tree")
def get_bid_tree(bid_id: str) -> dict:
    try:
        return {"bid_id": bid_id, "root": processor.build_tree(bid_id)}
    except BidNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/interpretation/v1/bids/{bid_id}/files")
def get_bid_files(bid_id: str) -> dict:
    try:
        return {"bid_id": bid_id, "files": processor.list_files(bid_id)}
    except BidNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/interpretation/v1/chunk")
async def chunk_single_file(
    file: UploadFile = File(...),
    max_chunk_chars: int = Form(200000),
    include_text: bool = Form(True),
) -> dict:
    try:
        return await chunk_processor.process_upload(
            file,
            max_chunk_chars=max_chunk_chars,
            include_text=include_text,
        )
    except ChunkProcessingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/interpretation/v1/bids/{bid_id}/chunk-jobs")
async def start_bid_chunk_job(
    bid_id: str,
    background_tasks: BackgroundTasks,
    payload: BatchChunkRequest | None = Body(default=None),
) -> dict:
    try:
        files = select_batch_chunk_files(
            processor.list_files(bid_id),
            payload.relative_paths if payload else None,
        )
    except BidNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not files:
        raise HTTPException(status_code=400, detail="该 bid_id 下没有可切片文件")

    batch_chunk_job_id = str(uuid.uuid4())
    manifest = create_batch_manifest(
        bid_id=bid_id,
        batch_chunk_job_id=batch_chunk_job_id,
        total_files=len(files),
    )
    write_batch_manifest(manifest)
    background_tasks.add_task(
        run_batch_chunk_job,
        bid_id,
        batch_chunk_job_id,
        files,
        200000,
        True,
    )

    return {
        "bid_id": bid_id,
        "batch_chunk_job_id": batch_chunk_job_id,
        "status": "pending",
        "status_url": batch_status_url(bid_id, batch_chunk_job_id),
        "result_url": batch_result_url(bid_id, batch_chunk_job_id),
    }


def select_batch_chunk_files(all_files: list[dict], relative_paths: list[str] | None) -> list[dict]:
    files_by_path = {file_record["relative_path"]: file_record for file_record in all_files}
    if relative_paths is None:
        return [
            file_record
            for file_record in all_files
            if file_record["extension"] in CHUNKABLE_EXTENSIONS
        ]

    if not relative_paths:
        raise HTTPException(status_code=400, detail="请选择至少一个需要切片的文件")

    selected_files: list[dict] = []
    seen_paths: set[str] = set()
    for relative_path in relative_paths:
        if relative_path in seen_paths:
            continue
        seen_paths.add(relative_path)

        file_record = files_by_path.get(relative_path)
        if file_record is None:
            raise HTTPException(status_code=400, detail=f"选择的文件不存在：{relative_path}")
        if file_record["extension"] not in CHUNKABLE_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"选择的文件不支持切片：{relative_path}")
        selected_files.append(file_record)

    return selected_files


@app.get("/api/interpretation/v1/bids/{bid_id}/chunk-jobs/{batch_chunk_job_id}")
def get_bid_chunk_job_status(bid_id: str, batch_chunk_job_id: str) -> dict:
    manifest = load_batch_manifest(bid_id, batch_chunk_job_id)
    return {
        "bid_id": manifest["bid_id"],
        "batch_chunk_job_id": manifest["batch_chunk_job_id"],
        "status": manifest["status"],
        "total_files": manifest["total_files"],
        "processed_files": manifest["processed_files"],
        "current_file": manifest.get("current_file"),
        "error": manifest.get("error"),
    }


@app.get("/api/interpretation/v1/bids/{bid_id}/chunk-jobs/{batch_chunk_job_id}/result")
def get_bid_chunk_job_result(bid_id: str, batch_chunk_job_id: str) -> dict:
    manifest = load_batch_manifest(bid_id, batch_chunk_job_id)
    if manifest["status"] in {"pending", "running"}:
        raise HTTPException(status_code=400, detail="批量切片任务尚未完成")
    return manifest


@app.get("/api/interpretation/v1/bids/{bid_id}/files/{relative_path:path}/download")
def download_bid_file(bid_id: str, relative_path: str) -> FileResponse:
    try:
        file_path = processor.resolve_extracted_file(bid_id, relative_path)
    except (BidNotFoundError, UnsafeArchivePathError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(file_path, filename=file_path.name)


def create_batch_manifest(bid_id: str, batch_chunk_job_id: str, total_files: int) -> dict:
    return {
        "bid_id": bid_id,
        "batch_chunk_job_id": batch_chunk_job_id,
        "status": "pending",
        "total_files": total_files,
        "processed_files": 0,
        "current_file": None,
        "files": [],
        "error": None,
        "status_url": batch_status_url(bid_id, batch_chunk_job_id),
        "result_url": batch_result_url(bid_id, batch_chunk_job_id),
    }


def run_batch_chunk_job(
    bid_id: str,
    batch_chunk_job_id: str,
    files: list[dict],
    max_chunk_chars: int,
    include_text: bool,
) -> None:
    manifest = load_batch_manifest(bid_id, batch_chunk_job_id)
    manifest["status"] = "running"
    write_batch_manifest(manifest)

    for index, file_record in enumerate(files, start=1):
        relative_path = file_record["relative_path"]
        manifest["current_file"] = relative_path
        write_batch_manifest(manifest)

        try:
            file_path = processor.resolve_extracted_file(bid_id, relative_path)
            file_dir = batch_file_dir(batch_chunk_job_id, index, relative_path)
            result = chunk_processor.process_file_path(
                file_path=file_path,
                filename=file_path.name,
                file_dir=file_dir,
                chunk_job_id=batch_chunk_job_id,
                max_chunk_chars=max_chunk_chars,
                include_text=include_text,
            )
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["current_file"] = None
            manifest["error"] = {
                "relative_path": relative_path,
                "filename": file_record.get("name") or Path(relative_path).name,
                "message": str(exc),
            }
            write_batch_manifest(manifest)
            return

        manifest["files"].append(
            {
                "relative_path": relative_path,
                "filename": file_path.name,
                "extension": file_path.suffix.lower(),
                "status": "completed",
                "chunk_count": len(result["chunks"]),
                "chunks_file": result["chunks_file"],
                "chunks_dir": result["chunks_dir"],
                "chunks": result["chunks"],
                "warnings": result.get("warnings", []),
            }
        )
        manifest["processed_files"] = index
        write_batch_manifest(manifest)

    manifest["status"] = "completed"
    manifest["current_file"] = None
    manifest["error"] = None
    write_batch_manifest(manifest)


def batch_job_dir(batch_chunk_job_id: str) -> Path:
    return chunk_processor.storage_root / "batches" / batch_chunk_job_id


def batch_manifest_path(batch_chunk_job_id: str) -> Path:
    return batch_job_dir(batch_chunk_job_id) / "manifest.json"


def batch_file_dir(batch_chunk_job_id: str, index: int, relative_path: str) -> Path:
    name = safe_path_name(Path(relative_path).stem)
    return batch_job_dir(batch_chunk_job_id) / "files" / f"{index:03d}_{name}"


def write_batch_manifest(manifest: dict) -> None:
    path = batch_manifest_path(manifest["batch_chunk_job_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def load_batch_manifest(bid_id: str, batch_chunk_job_id: str) -> dict:
    path = batch_manifest_path(batch_chunk_job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="batch_chunk_job_id 不存在")

    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("bid_id") != bid_id:
        raise HTTPException(status_code=404, detail="batch_chunk_job_id 不存在")
    return manifest


def batch_status_url(bid_id: str, batch_chunk_job_id: str) -> str:
    return f"/api/interpretation/v1/bids/{bid_id}/chunk-jobs/{batch_chunk_job_id}"


def batch_result_url(bid_id: str, batch_chunk_job_id: str) -> str:
    return f"/api/interpretation/v1/bids/{bid_id}/chunk-jobs/{batch_chunk_job_id}/result"
