import os
import logging

import pdfplumber
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# Кэш индексов в памяти (ускоряет повторные запросы до рестарта)
VECTOR_STORES: dict[str, FAISS] = {}

# Embeddings (ключ берется из env OPENAI_API_KEY внутри langchain_openai)
EMBEDDINGS = OpenAIEmbeddings(model="text-embedding-3-small")

# Пути (на Render disk обычно /var/data)
_DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", "/var/data"))
_BASE_FOLDER = os.path.join(_DATA_DIR, "StroyBot_Files")
_INDEX_ROOT = os.path.join(_DATA_DIR, "rag_indexes")


def configure(data_dir: str):
    """Вызывай на старте приложения."""
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


def iter_pdf_documents(folder_path: str):
    """
    Генератор документов: читает PDF рекурсивно и отдаёт Document по одному файлу.
    Так мы не держим в RAM все тексты сразу.
    """
    if not os.path.exists(folder_path):
        return

    for root, _, files in os.walk(folder_path):
        for filename in files:
            if not filename.lower().endswith(".pdf"):
                continue

            file_path = os.path.join(root, filename)

            try:
                text_parts = []
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        # layout=True часто лучше для тех.доков, но дороже по CPU/RAM
                        page_text = page.extract_text(layout=True)
                        if page_text:
                            text_parts.append(page_text)

                        # освобождаем кэши pdfplumber, иначе на некоторых PDF память растет
                        try:
                            page.flush_cache()
                        except Exception:
                            pass
                        try:
                            # cache_clear есть не всегда, но часто помогает
                            page.get_text_layout.cache_clear()
                        except Exception:
                            pass

                text = "\n".join(text_parts).strip()
                if not text:
                    continue

                rel_source = os.path.relpath(file_path, folder_path)
                yield Document(page_content=text, metadata={"source": rel_source})

            except Exception as e:
                logger.error(f"Ошибка чтения PDF {file_path}: {e}")


def build_index_for_project(
    project_name: str,
    chunk_size: int = 600,
    chunk_overlap: int = 80,
    batch_size: int = 30,
):
    """
    Строит FAISS индекс батчами и сохраняет на диск:
      /var/data/rag_indexes/<project>/index.faiss + index.pkl
    Возвращает vectorstore или None.
    """
    docs_path = _project_docs_path(project_name)
    index_path = _project_index_path(project_name)

    logger.info(f"🔄 Индексация проекта: {project_name} ({docs_path})")

    if not os.path.exists(docs_path):
        logger.warning(f"⚠️ Папка проекта не найдена: {docs_path}")
        return None

    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    vectorstore = None
    batch: list[Document] = []
    pdf_count = 0
    chunk_count = 0

    for doc in iter_pdf_documents(docs_path):
        pdf_count += 1

        # Дробим по одному PDF -> чанки -> складываем в батч
        splits = splitter.split_documents([doc])
        for s in splits:
            batch.append(s)

            if len(batch) >= batch_size:
                if vectorstore is None:
                    vectorstore = FAISS.from_documents(documents=batch, embedding=EMBEDDINGS)
                else:
                    vectorstore.add_documents(documents=batch)  # добавление батчами [web:500]
                chunk_count += len(batch)
                batch.clear()

    # Догружаем остаток
    if batch:
        if vectorstore is None:
            vectorstore = FAISS.from_documents(documents=batch, embedding=EMBEDDINGS)
        else:
            vectorstore.add_documents(documents=batch)
        chunk_count += len(batch)
        batch.clear()

    if vectorstore is None:
        logger.warning(f"⚠️ Индекс не построен: PDF={pdf_count}, chunks={chunk_count}")
        return None

    os.makedirs(index_path, exist_ok=True)
    vectorstore.save_local(index_path)  # сохранение на диск [web:687]
    VECTOR_STORES[project_name] = vectorstore

    logger.info(f"✅ Индекс сохранён: {index_path} (PDF: {pdf_count}, chunks: {chunk_count})")
    return vectorstore


def load_index_if_exists(project_name: str):
    """Пробует загрузить сохранённый индекс с диска, если он есть."""
    index_path = _project_index_path(project_name)
    if not os.path.isdir(index_path):
        return None

    try:
        vs = FAISS.load_local(
            index_path,
            EMBEDDINGS,
            allow_dangerous_deserialization=True,  # load_local использует pickle [web:687]
        )
        VECTOR_STORES[project_name] = vs
        logger.info(f"✅ Индекс загружен с диска: {index_path}")
        return vs
    except Exception as e:
        logger.error(f"Не удалось загрузить индекс {index_path}: {e}")
        return None


def get_relevant_context(project_name: str, query: str, k: int = 3):
    """
    Возвращает склейку top-k чанков для промпта.
    Если индекс не загружен и нет сохранённого — строит.
    """
    if not project_name:
        return None

    if project_name not in VECTOR_STORES:
        if not load_index_if_exists(project_name):
            if not build_index_for_project(project_name):
                return None

    index = VECTOR_STORES[project_name]
    results = index.similarity_search(query, k=k)
    if not results:
        return None

    return "\n\n".join(
        [f"--- ИЗ ДОКУМЕНТА: {doc.metadata.get('source', 'unknown')} ---\n{doc.page_content}" for doc in results]
    )


