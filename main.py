import os
import tempfile
import threading
import uuid

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from docling.document_converter import DocumentConverter

app = FastAPI(title="Meishu Extractor", version="2.0")

# O conversor é carregado uma vez só, quando o serviço sobe.
converter = DocumentConverter()

API_KEY = os.environ.get("EXTRACTOR_API_KEY", "")

# Armazém simples em memória dos trabalhos em andamento/concluídos.
# Como o serviço roda numa única instância, isso é suficiente pro caso
# de uso (o n8n só processa 1 documento por vez, graças à trava que já
# existe no workflow). Se o container reiniciar no meio de um trabalho,
# esse trabalho se perde — o n8n vai perceber pelo status "nao encontrado"
# e pode tentar de novo.
trabalhos = {}


def verificar_chave(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Chave de API invalida ou ausente")


def processar_em_segundo_plano(job_id: str, caminho_tmp: str):
    """Roda numa thread separada, pra nao segurar a resposta HTTP."""
    try:
        resultado = converter.convert(caminho_tmp)
        markdown = resultado.document.export_to_markdown()
        trabalhos[job_id] = {"status": "concluido", "markdown": markdown, "erro": None}
    except Exception as erro:
        trabalhos[job_id] = {"status": "erro", "markdown": None, "erro": str(erro)}
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
    Inicia a extracao em segundo plano e devolve NA HORA um job_id —
    nao espera o processamento terminar. Isso evita o timeout do proxy
    em documentos grandes (que podem levar minutos). Consulte o
    resultado depois em GET /extract/{job_id}.
    """
    verificar_chave(x_api_key)

    sufixo = os.path.splitext(file.filename or "")[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=sufixo) as tmp:
        conteudo = await file.read()
        tmp.write(conteudo)
        caminho_tmp = tmp.name

    job_id = str(uuid.uuid4())
    trabalhos[job_id] = {"status": "processando", "markdown": None, "erro": None}

    thread = threading.Thread(
        target=processar_em_segundo_plano, args=(job_id, caminho_tmp), daemon=True
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
