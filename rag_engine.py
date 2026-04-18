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
    Генератор документов: читает PDF рекурсивно и отдаёт Document по одной странице.
    Метаданные содержат source, page, total_pages — используются в RAG-контексте.
    """
    if not os.path.exists(folder_path):
        return

    for root, _, files in os.walk(folder_path):
        for filename in sorted(files):
            if not filename.lower().endswith(".pdf"):
                continue

            file_path = os.path.join(root, filename)
            rel_source = os.path.relpath(file_path, folder_path)

            try:
                with pdfplumber.open(file_path) as pdf:
                    total_pages = len(pdf.pages)
                    has_text = False

                    for page_num, page in enumerate(pdf.pages, start=1):
                        page_text = page.extract_text(layout=True)

                        try:
                            page.flush_cache()
                        except Exception:
                            pass

                        if not page_text or not page_text.strip():
                            continue

                        has_text = True
                        yield Document(
                            page_content=page_text.strip(),
                            metadata={
                                "source": rel_source,
                                "page": page_num,
                                "total_pages": total_pages,
                            },
                        )

                    if not has_text:
                        logger.warning("PDF без извлекаемого текста (возможно скан): %s", file_path)

            except Exception as e:
                logger.error("Ошибка чтения PDF %s: %s", file_path, e)


def build_index_for_project(
    project_name: str,
    chunk_size: int = 1000,   # увеличено с 600: лучше сохраняет контекст
    chunk_overlap: int = 150, # увеличено с 80: больше связность между чанками
    batch_size: int = 30,
):
    """
    Строит FAISS индекс батчами и сохраняет на диск:
      /var/data/rag_indexes/<project>/index.faiss + index.pkl
    Возвращает vectorstore или None.
    """
    docs_path = _project_docs_path(project_name)
    index_path = _project_index_path(project_name)

    logger.info("🔄 Индексация: %s (%s)", project_name, docs_path)

    if not os.path.exists(docs_path):
        logger.warning("⚠️ Папка не найдена: %s", docs_path)
        return None

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    vectorstore = None
    batch: list[Document] = []
    pdf_count = 0
    chunk_count = 0
    seen_sources: set[str] = set()

    for doc in iter_pdf_documents(docs_path):
        src = doc.metadata.get("source", "")
        if src not in seen_sources:
            seen_sources.add(src)
            pdf_count += 1

        splits = splitter.split_documents([doc])
        # Гарантируем, что source/page сохраняются в каждом чанке
        for s in splits:
            s.metadata.setdefault("source", src)
            batch.append(s)

            if len(batch) >= batch_size:
                if vectorstore is None:
                    vectorstore = FAISS.from_documents(documents=batch, embedding=EMBEDDINGS)
                else:
                    vectorstore.add_documents(documents=batch)
                chunk_count += len(batch)
                batch.clear()

    if batch:
        if vectorstore is None:
            vectorstore = FAISS.from_documents(documents=batch, embedding=EMBEDDINGS)
        else:
            vectorstore.add_documents(documents=batch)
        chunk_count += len(batch)
        batch.clear()

    if vectorstore is None:
        logger.warning("⚠️ Индекс не построен: PDF=%d, chunks=%d", pdf_count, chunk_count)
        return None

    os.makedirs(index_path, exist_ok=True)
    vectorstore.save_local(index_path)
    VECTOR_STORES[project_name] = vectorstore

    logger.info("✅ Индекс сохранён: %s (PDF: %d, chunks: %d)", index_path, pdf_count, chunk_count)
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


def get_relevant_context(project_name: str, query: str, k: int = 6, score_threshold: float = 0.35):
    """
    Усиленный RAG-поиск. Возвращает (context_str, source_files).

    Алгоритм:
      1. MMR (Maximal Marginal Relevance): выбирает k=6 разнообразных релевантных фрагментов
         из 20 кандидатов (lambda=0.7: 70% релевантность + 30% разнообразие).
      2. Fallback: similarity_search_with_relevance_scores + фильтр по порогу.

    Контекст содержит номер страницы и название документа.
    """
    if not project_name:
        return None, []

    if project_name not in VECTOR_STORES:
        if not load_index_if_exists(project_name):
            if not build_index_for_project(project_name):
                return None, []

    index = VECTOR_STORES[project_name]
    results: list[Document] = []

    # ── Шаг 1: MMR ──
    try:
        results = index.max_marginal_relevance_search(
            query,
            k=k,
            fetch_k=max(k * 4, 20),
            lambda_mult=0.7,
        )
    except Exception as exc:
        logger.warning("MMR недоступен (%s), fallback → similarity_search", exc)

    # ── Шаг 2: Fallback ──
    if not results:
        try:
            scored = index.similarity_search_with_relevance_scores(query, k=k + 4)
            results = [doc for doc, score in scored if score >= score_threshold][:k]
        except Exception:
            results = index.similarity_search(query, k=k)

    if not results:
        return None, []

    docs_path = _project_docs_path(project_name)
    seen_sources: list[str] = []
    source_files: list[str] = []
    context_parts: list[str] = []

    for doc in results:
        source = doc.metadata.get("source", "unknown")
        page = doc.metadata.get("page", "?")
        context_parts.append(
            f"——— [{source}] стр. {page} ———\n{doc.page_content}"
        )
        if source != "unknown" and source not in seen_sources:
            seen_sources.append(source)
            full_path = os.path.join(docs_path, source)
            if os.path.isfile(full_path):
                source_files.append(full_path)

    return "\n\n".join(context_parts), source_files


