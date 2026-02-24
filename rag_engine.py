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
_INDEX_ROOT = os.path.join(_DATA_DIR, "rag_indexes")


def configure(data_dir: str):
    global _DATA_DIR, _BASE_FOLDER, _INDEX_ROOT
    _DATA_DIR = os.path.abspath(data_dir)
    _BASE_FOLDER = os.path.join(_DATA_DIR, "StroyBot_Files")
    _INDEX_ROOT = os.path.join(_DATA_DIR, "rag_indexes")
    os.makedirs(_BASE_FOLDER, exist_ok=True)
    os.makedirs(_INDEX_ROOT, exist_ok=True)
    logger.info(f"RAG base folder: {_BASE_FOLDER}")


def _clean_name(name: str) -> str:
    return "".join([c if c.isalnum() or c in "._- " else "_" for c in name]).strip()


def _project_docs_path(project_name: str) -> str:
    return os.path.join(_BASE_FOLDER, _clean_name(project_name))


def _project_index_path(project_name: str) -> str:
    return os.path.join(_INDEX_ROOT, _clean_name(project_name))


def load_pdfs_from_folder(folder_path: str):
    """
    Рекурсивно читает все PDF внутри folder_path (включая подпапки систем и _PROJECT).
    """
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
            except Exception as e:
                logger.error(f"Ошибка чтения {file_path}: {e}")

    return docs


def build_index_for_project(project_name: str):
    docs_path = _project_docs_path(project_name)
    index_path = _project_index_path(project_name)

    logger.info(f"🔄 Индексация проекта: {project_name} ({docs_path})")

    raw_docs = load_pdfs_from_folder(docs_path)
    if not raw_docs:
        logger.warning(f"⚠️ В папке {docs_path} нет PDF файлов.")
        return None

    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=120)
    splits = splitter.split_documents(raw_docs)

    vectorstore = FAISS.from_documents(documents=splits, embedding=EMBEDDINGS)

    os.makedirs(index_path, exist_ok=True)
    vectorstore.save_local(index_path)

    VECTOR_STORES[project_name] = vectorstore
    logger.info(f"✅ Индекс сохранён: {index_path} (chunks: {len(splits)})")
    return vectorstore


def load_index_if_exists(project_name: str):
    index_path = _project_index_path(project_name)
    if not os.path.isdir(index_path):
        return None

    try:
        vs = FAISS.load_local(
            index_path,
            EMBEDDINGS,
            allow_dangerous_deserialization=True,
        )
        VECTOR_STORES[project_name] = vs
        logger.info(f"✅ Индекс загружен с диска: {index_path}")
        return vs
    except Exception as e:
        logger.error(f"Не удалось загрузить индекс {index_path}: {e}")
        return None


def get_relevant_context(project_name: str, query: str):
    if not project_name:
        return None

    if project_name not in VECTOR_STORES:
        if not load_index_if_exists(project_name):
            if not build_index_for_project(project_name):
                return None

    index = VECTOR_STORES[project_name]
    results = index.similarity_search(query, k=4)
    return "\n\n".join(
        [f"--- ИЗ ДОКУМЕНТА: {doc.metadata.get('source', 'unknown')} ---\n{doc.page_content}" for doc in results]
    )


