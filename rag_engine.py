import os
from dotenv import load_dotenv 
load_dotenv() 
import pdfplumber
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
import logging

# Настройка логгера
logger = logging.getLogger(__name__)

# Глобальное хранилище индексов: { "Название папки": FAISS_Index }
VECTOR_STORES = {}
EMBEDDINGS = OpenAIEmbeddings(model="text-embedding-3-small") # Дешевая и быстрая модель

def load_pdfs_from_folder(folder_path):
    """Читает все PDF в папке и возвращает список документов"""
    docs = []
    if not os.path.exists(folder_path):
        return []

    for filename in os.listdir(folder_path):
        if filename.lower().endswith('.pdf'):
            file_path = os.path.join(folder_path, filename)
            try:
                text = ""
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        # Извлекаем текст, стараясь сохранить структуру таблиц
                        page_text = page.extract_text(layout=True)
                        if page_text:
                            text += page_text + "\n"
                
                # Добавляем метаданные, чтобы бот знал, откуда инфа
                docs.append(Document(page_content=text, metadata={"source": filename}))
                logger.info(f"📄 Загружен файл: {filename}")
            except Exception as e:
                logger.error(f"Ошибка чтения {filename}: {e}")
    return docs

def build_index_for_project(project_name):
    """Создает поисковый индекс для конкретного объекта"""
    base_folder = "StroyBot_Files"
    # Очистка имени папки как в main.py
    project_clean = "".join([c if c.isalnum() or c in '._- ' else "_" for c in project_name]).strip()
    path = os.path.join(base_folder, project_clean)

    logger.info(f"🔄 Индексация проекта: {project_name} ({path})")
    
    raw_docs = load_pdfs_from_folder(path)
    if not raw_docs:
        logger.warning(f"⚠️ В папке {path} нет PDF файлов.")
        return None

    # Разбиваем текст на кусочки по 1000 символов, чтобы удобно скармливать ИИ
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    splits = text_splitter.split_documents(raw_docs)

    # Создаем векторную базу (это и есть "мозг" поиска)
    vectorstore = FAISS.from_documents(documents=splits, embedding=EMBEDDINGS)
    
    # Сохраняем в оперативную память
    VECTOR_STORES[project_name] = vectorstore
    logger.info(f"✅ Индекс для {project_name} готов! Загружено фрагментов: {len(splits)}")
    return vectorstore

def get_relevant_context(project_name, query):
    """Ищет информацию в документах по запросу"""
    # Если индекса нет - пробуем создать
    if project_name not in VECTOR_STORES:
        index = build_index_for_project(project_name)
        if not index:
            return None
    else:
        index = VECTOR_STORES[project_name]

    # Ищем 4 самых похожих куска текста
    results = index.similarity_search(query, k=4)
    
    # Собираем текст в одну строку
    context_text = "\n\n".join([f"--- ИЗ ДОКУМЕНТА: {doc.metadata['source']} ---\n{doc.page_content}" for doc in results])
    return context_text
