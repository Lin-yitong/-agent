from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from app.services.input_processor import (
    BidNotFoundError,
    InputProcessingError,
    InputProcessor,
    UnsafeArchivePathError,
)


app = FastAPI(title="Bid Interpretation Input Service")
processor = InputProcessor(storage_root=Path("storage/uploads"))


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
          form { display: flex; gap: 12px; align-items: center; margin-bottom: 24px; }
          button { padding: 8px 14px; border: 1px solid #2563eb; background: #2563eb; color: white; border-radius: 6px; cursor: pointer; }
          input { border: 1px solid #d1d5db; padding: 8px; border-radius: 6px; }
          .meta { color: #4b5563; margin-bottom: 12px; }
          ul { list-style: none; padding-left: 20px; }
          li { margin: 5px 0; }
          .dir { font-weight: 650; }
          .file a { color: #2563eb; text-decoration: none; }
          .error { color: #b91c1c; }
        </style>
      </head>
      <body>
        <main>
          <h1>标书输入处理</h1>
          <form id="upload-form">
            <input id="file" name="file" type="file" required />
            <button type="submit">上传</button>
          </form>
          <div id="result"></div>
        </main>
        <script>
          const form = document.getElementById("upload-form");
          const result = document.getElementById("result");

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


@app.get("/api/interpretation/v1/bids/{bid_id}/files/{relative_path:path}/download")
def download_bid_file(bid_id: str, relative_path: str) -> FileResponse:
    try:
        file_path = processor.resolve_extracted_file(bid_id, relative_path)
    except (BidNotFoundError, UnsafeArchivePathError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return FileResponse(file_path, filename=file_path.name)

