from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.middleware.cors import CORSMiddleware

from app.services.chunk_processor import ChunkProcessingError, ChunkProcessor
from app.services.input_processor import (
    BidNotFoundError,
    InputProcessingError,
    InputProcessor,
    UnsafeArchivePathError,
)

app = FastAPI(title="Bid Interpretation Input Service")
processor = InputProcessor(storage_root=Path("storage/uploads"))
chunk_processor = ChunkProcessor()



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
          .error { color: #b91c1c; }
          .warning { color: #92400e; background: #fffbeb; border: 1px solid #fde68a; padding: 8px 10px; border-radius: 6px; }
          details { border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px 12px; margin: 10px 0; background: #ffffff; }
          summary { cursor: pointer; font-weight: 650; }
          pre { white-space: pre-wrap; word-break: break-word; line-height: 1.55; color: #111827; background: #f9fafb; padding: 12px; border-radius: 6px; overflow: auto; }
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

          function renderNode(node) {
            if (node.type === "directory") {
              const children = (node.children || []).map(renderNode).join("");
              return `<li><span class="dir">${node.name}/</span><ul>${children}</ul></li>`;
            }
            return `<li class="file"><a href="${node.download_url}" target="_blank">${node.name}</a> <span class="meta">${node.size} bytes</span></li>`;
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
            result.innerHTML = `
              <p class="meta">bid_id: ${payload.bid_id}</p>
              <ul>${renderNode(tree.root)}</ul>
            `;
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


@app.get("/api/interpretation/v1/bids/{bid_id}/files/{relative_path:path}/download")
def download_bid_file(bid_id: str, relative_path: str) -> FileResponse:
    try:
        file_path = processor.resolve_extracted_file(bid_id, relative_path)
    except (BidNotFoundError, UnsafeArchivePathError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(file_path, filename=file_path.name)
