import os
import tempfile
import threading
import uuid

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from pypdf import PdfReader, PdfWriter
from docling.document_converter import DocumentConverter

app = FastAPI(title="Meishu Extractor", version="4.0")

# O conversor é carregado uma vez só, quando o serviço sobe.
converter = DocumentConverter()

API_KEY = os.environ.get("EXTRACTOR_API_KEY", "")

# Quantas páginas processar por vez.
PAGINAS_POR_LOTE = int(os.environ.get("PAGINAS_POR_LOTE", "30"))

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


def pagina_do_item(item, pagina_base: int) -> int:
    """Calcula a pagina absoluta do livro a partir da provenance do item
    (que so sabe a pagina DENTRO do lote atual) somada ao offset do lote."""
    try:
        prov = getattr(item, "prov", None)
        if prov:
            return pagina_base + (prov[0].page_no - 1)
    except Exception:
        pass
    return pagina_base


def exportar_estruturado(resultado, pagina_base: int):
    """
    Em vez de exportar Markdown (texto puro, onde titulo/nota/tabela viram
    so texto sem marcacao, obrigando o n8n a ADIVINHAR o que e cada coisa
    via regex), exportamos os itens do documento com o LABEL que o proprio
    modelo do Docling ja identificou: section_header, footnote, table,
    text, etc. Isso elimina a adivinhacao inteira do lado do n8n.
    """
    doc = resultado.document
    itens = []

    for item, _nivel in doc.iterate_items():
        tipo_python = type(item).__name__

        if tipo_python == "TableItem":
            try:
                df = item.export_to_dataframe(doc=doc)
                texto_tabela = df.to_markdown(index=False)
            except Exception:
                continue
            if texto_tabela and texto_tabela.strip():
                itens.append({
                    "texto": texto_tabela.strip(),
                    "tipo": "table",
                    "pagina": pagina_do_item(item, pagina_base),
                })
            continue

        texto = getattr(item, "text", None)
        if not texto or not texto.strip():
            continue

        label = getattr(item, "label", None)
        itens.append({
            "texto": texto.strip(),
            "tipo": str(label) if label else "text",
            "pagina": pagina_do_item(item, pagina_base),
        })

    return itens


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

        todos_itens = []
        pagina_inicial_lote = 1
        for indice, caminho_lote in enumerate(caminhos_lotes):
            trabalhos[job_id]["status"] = "processando"
            trabalhos[job_id]["lote_atual"] = indice + 1

            resultado = converter.convert(caminho_lote)
            todos_itens.extend(exportar_estruturado(resultado, pagina_inicial_lote))

            if extensao.lower() == ".pdf":
                pagina_inicial_lote += PAGINAS_POR_LOTE

            if caminho_lote != caminho_tmp:
                try:
                    os.unlink(caminho_lote)
                except OSError:
                    pass

        trabalhos[job_id] = {
            **trabalhos[job_id],
            "status": "concluido",
            "itens": todos_itens,
            "erro": None,
        }
    except Exception as erro:
        trabalhos[job_id] = {**trabalhos[job_id], "status": "erro", "itens": None, "erro": str(erro)}
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
        "itens": None,
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
