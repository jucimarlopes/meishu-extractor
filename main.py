import os
import tempfile

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from docling.document_converter import DocumentConverter

app = FastAPI(title="Meishu Extractor", version="1.0")

# O conversor é carregado uma vez só, quando o servico sobe — nao a cada
# requisicao (senao ficaria lento, recarregando os modelos toda hora).
converter = DocumentConverter()

API_KEY = os.environ.get("EXTRACTOR_API_KEY", "")


@app.get("/health")
def health():
    """Endpoint simples pra confirmar que o servico esta de pe."""
    return {"status": "ok"}


@app.post("/extract")
async def extract(file: UploadFile = File(...), x_api_key: str = Header(None)):
    """
    Recebe um PDF/DOCX, devolve o conteudo em Markdown ja estruturado
    (titulos como #, paragrafos reais, tabelas, etc).
    """
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Chave de API invalida ou ausente")

    sufixo = os.path.splitext(file.filename or "")[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=sufixo) as tmp:
        conteudo = await file.read()
        tmp.write(conteudo)
        caminho_tmp = tmp.name

    try:
        resultado = converter.convert(caminho_tmp)
        markdown = resultado.document.export_to_markdown()
    except Exception as erro:
        raise HTTPException(status_code=500, detail=f"Falha ao converter: {erro}")
    finally:
        os.unlink(caminho_tmp)

    return {"markdown": markdown, "arquivo": file.filename}
