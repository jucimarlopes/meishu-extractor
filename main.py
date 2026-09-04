import os
import tempfile
import threading
import uuid

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from pypdf import PdfReader, PdfWriter
from docling.document_converter import DocumentConverter

app = FastAPI(title="Meishu Extractor", version="3.0")

# O conversor é carregado uma vez só, quando o serviço sobe.
converter = DocumentConverter()

API_KEY = os.environ.get("EXTRACTOR_API_KEY", "")

# Quantas páginas processar por vez. Quanto menor, menos memória no pico —
# mas mais lento no total (mais idas e vindas). 30 é um ponto de partida
# razoável; se ainda estourar memória, é só baixar esse número.
PAGINAS_POR_LOTE = int(os.environ.get("PAGINAS_POR_LOTE", "30"))

# Armazém simples em memória dos trabalhos em andamento/concluídos.
trabalhos = {}


def verificar_chave(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Chave de API invalida ou ausente")


def dividir_pdf_em_lotes(caminho_pdf: str, paginas_por_lote: int):
    """
    Divide um PDF grande em vários PDFs menores (arquivos temporários),
    cada um com no máximo `paginas_por_lote` páginas. Devolve a lista de
    caminhos dos arquivos gerados, na ordem certa.
    """
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


def processar_em_segundo_plano(job_id: str, caminho_tmp: str, extensao: str):
    """Roda numa thread separada, pra nao segurar a resposta HTTP."""
    caminhos_lotes = []
    try:
        # Só PDF é dividido em lotes (DOCX/outros formatos, geralmente
        # menores, seguem direto para o Docling sem dividir).
        if extensao.lower() == ".pdf":
            trabalhos[job_id]["status"] = "dividindo_paginas"
            caminhos_lotes, total_paginas = dividir_pdf_em_lotes(caminho_tmp, PAGINAS_POR_LOTE)
            trabalhos[job_id]["total_paginas"] = total_paginas
            trabalhos[job_id]["total_lotes"] = len(caminhos_lotes)
        else:
            caminhos_lotes = [caminho_tmp]

        partes_markdown = []
        for indice, caminho_lote in enumerate(caminhos_lotes):
            trabalhos[job_id]["status"] = "processando"
            trabalhos[job_id]["lote_atual"] = indice + 1

            resultado = converter.convert(caminho_lote)
            partes_markdown.append(resultado.document.export_to_markdown())

            # Libera o arquivo temporário do lote assim que termina de usar.
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
    """
    Inicia a extracao em segundo plano e devolve NA HORA um job_id.
    PDFs grandes sao divididos em lotes de paginas e processados um
    lote por vez, para manter o uso de memoria estavel mesmo em livros
    muito grandes. Consulte o andamento em GET /extract/{job_id}.
    """
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
    """Consulta o andamento (ou resultado) de um trabalho iniciado em /extract."""
    verificar_chave(x_api_key)

    trabalho = trabalhos.get(job_id)
    if trabalho is None:
        raise HTTPException(status_code=404, detail="job_id nao encontrado")

    return {"job_id": job_id, **trabalho}
