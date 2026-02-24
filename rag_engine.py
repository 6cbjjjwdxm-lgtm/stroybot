import os
import logging

import pdfplumber
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

logger = logging.getLogger(__name__)

VECTOR_STORES = {}
EMBEDDINGS = OpenAIEmbeddings(model="text-embedding-3-small")

_DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", "/var/data"))
_BASE_FOLDER = os.path.join(_DATA_DIR, "StroyBot_Files")


def configure(data_dir: str):
    global _DATA_DIR, _BASE_FOLDER
    _DATA_DIR = os.path.abspath(data_dir)
    _BASE_FOLDER = os.path.join(_DATA_DIR, "StroyBot_Files")
    os.makedirs(_BASE_FOLDER, exist_ok=True)
    logger.info(f"RAG base folder: {_BASE_FOLDER}")


def _clean_name(name: str) -> str:
    return "".join([c if c.isalnum() or c in "._- " else "_" for c in name]).strip()


def load_pdfs_from_folder(folder_path: str):
    docs = []
    if not os.path.exists(folder_path):
        return docs

    for root, _, files in os.walk(folder_path):
        for filename in files:
            if not filename.lower().endswith(".pdf"):
                continue

            file_path = os.path.join(root, filename)
            try:
                text_parts = []
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        page_text = page.extract_text(layout=True)
                        if page_text:
                            text_parts.append(page_text)

                text = "\n".join(text_parts).strip()
                if not text:
                    continue

                rel_source = os.path.relpath(file_path, folder_path)
                docs.append(Document(page_content=text, metadata={"source": rel_source}))
                logger.info(f"📄 Загружен файл: {rel_source}")
            except Exception as e:
                logger.error(f"Ошибка чтения {file_path}: {e}")

    return docs


def build_index_for_project(project_name: str):
    project_clean = _clean_name(project_name)
    path = os.path.join(_BASE_FOLDER, project_clean)

    logger.info(f"🔄 Индексация проекта: {project_name} ({path})")

    raw_docs = load_pdfs_from_folder(path)
    if not raw_docs:
        logger.warning(f"⚠️ В папке {path} нет PDF файлов.")
        return None

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    splits = text_splitter.split_documents(raw_docs)

    vectorstore = FAISS.from_documents(documents=splits, embedding=EMBEDDINGS)

    VECTOR_STORES[project_name] = vectorstore
    logger.info(f"✅ Индекс для {project_name} готов! Загружено фрагментов: {len(splits)}")
    return vectorstore


def get_relevant_context(project_name: str, query: str):
    if not project_name:
        return None

    if project_name not in VECTOR_STORES:
        index = build_index_for_project(project_name)
        if not index:
            return None
    else:
        index = VECTOR_STORES[project_name]

    results = index.similarity_search(query, k=4)
    context_text = "\n\n".join(
        [f"--- ИЗ ДОКУМЕНТА: {doc.metadata.get('source', 'unknown')} ---\n{doc.page_content}" for doc in results]
    )
    return context_text


