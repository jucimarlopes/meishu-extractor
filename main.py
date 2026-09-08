import os
import tempfile
import threading
import uuid

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from pypdf import PdfReader, PdfWriter
from docling.document_converter import DocumentConverter

app = FastAPI(title="Meishu Extractor", version="3.1")

# O conversor é carregado uma vez só, quando o serviço sobe.
converter = DocumentConverter()

API_KEY = os.environ.get("EXTRACTOR_API_KEY", "")

# Quantas páginas processar por vez.
PAGINAS_POR_LOTE = int(os.environ.get("PAGINAS_POR_LOTE", "30"))

# Marcadores de página que o n8n sabe interpretar (opcional - se a versão
# do docling instalada não suportar page_break_placeholder, o serviço
# continua funcionando normalmente, só sem marcar a página real).
MARCADOR_PAGEBREAK = "\n\n<<<PAGEBREAK>>>\n\n"

trabalhos = {}


def verificar_chave(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Chave de API invalida ou ausente")


def dividir_pdf_em_lotes(caminho_pdf: str, paginas_por_lote: int):
    leitor = PdfReader(caminho_pdf)
    total_paginas = len(leitor.pages)
    caminhos_lotes = []

    for inicio in range(0, total_paginas, paginas_por_lote):
        escritor = PdfWriter()
        fim = min(inicio + paginas_por_lote, total_paginas)
        for i in range(inicio, fim):
            escritor.add_page(leitor.pages[i])

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_lote:
            escritor.write(tmp_lote)
            caminhos_lotes.append(tmp_lote.name)

    return caminhos_lotes, total_paginas


def exportar_markdown_com_pagina(resultado, pagina_inicial: int):
    """
    Exporta o markdown já com marcadores de página, quando a versão do
    docling instalada suporta isso. Se não suportar (TypeError), cai
    de volta pro comportamento simples de sempre - nunca quebra a
    extração por causa disso.
    """
    try:
        md = resultado.document.export_to_markdown(page_break_placeholder=MARCADOR_PAGEBREAK)
        return f"<<<PAGE:{pagina_inicial}>>>\n\n{md}"
    except TypeError:
        return resultado.document.export_to_markdown()


def processar_em_segundo_plano(job_id: str, caminho_tmp: str, extensao: str):
    caminhos_lotes = []
    try:
        if extensao.lower() == ".pdf":
            trabalhos[job_id]["status"] = "dividindo_paginas"
            caminhos_lotes, total_paginas = dividir_pdf_em_lotes(caminho_tmp, PAGINAS_POR_LOTE)
            trabalhos[job_id]["total_paginas"] = total_paginas
            trabalhos[job_id]["total_lotes"] = len(caminhos_lotes)
        else:
            caminhos_lotes = [caminho_tmp]

        partes_markdown = []
        pagina_inicial_lote = 1
        for indice, caminho_lote in enumerate(caminhos_lotes):
            trabalhos[job_id]["status"] = "processando"
            trabalhos[job_id]["lote_atual"] = indice + 1

            resultado = converter.convert(caminho_lote)
            partes_markdown.append(exportar_markdown_com_pagina(resultado, pagina_inicial_lote))

            if extensao.lower() == ".pdf":
                pagina_inicial_lote += PAGINAS_POR_LOTE

            if caminho_lote != caminho_tmp:
                try:
                    os.unlink(caminho_lote)
                except OSError:
                    pass

        markdown_final = "\n\n".join(partes_markdown)
        trabalhos[job_id] = {
            **trabalhos[job_id],
            "status": "concluido",
            "markdown": markdown_final,
            "erro": None,
        }
    except Exception as erro:
        trabalhos[job_id] = {**trabalhos[job_id], "status": "erro", "markdown": None, "erro": str(erro)}
        for caminho_lote in caminhos_lotes:
            if caminho_lote != caminho_tmp:
                try:
                    os.unlink(caminho_lote)
                except OSError:
                    pass
    finally:
        try:
            os.unlink(caminho_tmp)
        except OSError:
            pass


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/extract")
async def extract(file: UploadFile = File(...), x_api_key: str = Header(None)):
    verificar_chave(x_api_key)

    sufixo = os.path.splitext(file.filename or "")[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=sufixo) as tmp:
        conteudo = await file.read()
        tmp.write(conteudo)
        caminho_tmp = tmp.name

    job_id = str(uuid.uuid4())
    trabalhos[job_id] = {
        "status": "processando",
        "markdown": None,
        "erro": None,
        "total_paginas": None,
        "total_lotes": None,
        "lote_atual": None,
    }

    thread = threading.Thread(
        target=processar_em_segundo_plano, args=(job_id, caminho_tmp, sufixo), daemon=True
    )
    thread.start()

    return {"job_id": job_id, "status": "processando", "arquivo": file.filename}


@app.get("/extract/{job_id}")
def status_extract(job_id: str, x_api_key: str = Header(None)):
    verificar_chave(x_api_key)

    trabalho = trabalhos.get(job_id)
    if trabalho is None:
        raise HTTPException(status_code=404, detail="job_id nao encontrado")

    return {"job_id": job_id, **trabalho}
