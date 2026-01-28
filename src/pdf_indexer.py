import os
import fitz  # PyMuPDF
import faiss
import numpy as np
from pathlib import Path
from typing import List, Dict, Any
from sentence_transformers import SentenceTransformer

class PDFIndexer:
    def __init__(self, data_dir: str = "data", index_path: str = "data/pdf_index.faiss"):
        """
        Инициализация индексатора PDF документов.
        
        Args:
            data_dir: Путь к директории с PDF файлами
            index_path: Путь к файлу индекса FAISS
        """
        self.data_dir = Path(data_dir)
        self.index_path = Path(index_path)
        self.model = SentenceTransformer('all-MiniLM-L6-v2')  # Легкая модель для эмбеддингов
        self.index = None
        self.doc_chunks = []  # Список чанков документов с метаданными
        
    def extract_text_from_pdf(self, pdf_path: str) -> str:
        """
        Извлекает текст из PDF файла.
        
        Args:
            pdf_path: Путь к PDF файлу
            
        Returns:
            Извлеченный текст
        """
        try:
            doc = fitz.open(pdf_path)
            text = ""
            for page in doc:
                text += page.get_text()
            return text
        except Exception as e:
            print(f"Ошибка при чтении PDF {pdf_path}: {e}")
            return ""
    
    def chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> List[Dict[str, Any]]:
        """
        Разбивает текст на чанки для индексации.
        
        Args:
            text: Текст для разбиения
            chunk_size: Размер чанка в символах
            overlap: Количество перекрывающихся символов между чанками
            
        Returns:
            Список чанков с текстом и метаданными
        """
        chunks = []
        start = 0
        
        while start < len(text):
            end = start + chunk_size
            if end >= len(text):
                chunk_text = text[start:]
                start = len(text)
            else:
                # Пытаемся разорвать на границе предложения
                chunk_end = text.rfind('. ', start, end)
                if chunk_end == -1:
                    chunk_end = end
                else:
                    chunk_end += 2  # Включаем точку и пробел
                
                chunk_text = text[start:chunk_end]
                start = chunk_end - overlap if chunk_end - overlap > start else start + chunk_size // 2
            
            if chunk_text.strip():
                chunks.append({
                    'text': chunk_text.strip(),
                    'start_pos': start,
                    'end_pos': len(text)
                })
        
        return chunks
    
    def create_embeddings(self, texts: List[str]) -> np.ndarray:
        """
        Создает эмбеддинги для текстов с помощью SentenceTransformer.
        
        Args:
            texts: Список текстов
            
        Returns:
            Массив эмбеддингов
        """
        embeddings = self.model.encode(texts, convert_to_numpy=True)
        return embeddings
    
    def build_index(self) -> None:
        """
        Строит индекс FAISS из всех PDF файлов в директории data.
        """
        pdf_files = list(self.data_dir.glob("*.pdf"))
        
        if not pdf_files:
            raise ValueError(f"Не найдено PDF файлов в директории {self.data_dir}")
        
        all_chunks = []
        
        for pdf_file in pdf_files:
            print(f"Обработка файла: {pdf_file.name}")
            
            # Извлекаем текст
            text = self.extract_text_from_pdf(str(pdf_file))
            if not text:
                continue
            
            # Разбиваем на чанки
            chunks = self.chunk_text(text)
            
            # Добавляем метаданные
            for chunk in chunks:
                chunk['source'] = str(pdf_file)
                chunk['source_name'] = pdf_file.name
                all_chunks.append(chunk)
        
        if not all_chunks:
            raise ValueError("Не удалось извлечь текст из PDF файлов")
        
        # Создаем эмбеддинги
        texts = [chunk['text'] for chunk in all_chunks]
        embeddings = self.create_embeddings(texts)
        
        # Нормализуем эмбеддинги для косинусного сходства
        faiss.normalize_L2(embeddings)
        
        # Создаем индекс FAISS
        dimension = embeddings.shape[1]
        self.index = faiss.IndexFlatIP(dimension)  # Inner product для косинусного сходства
        self.index.add(embeddings)
        
        # Сохраняем индекс и чанки
        faiss.write_index(self.index, str(self.index_path))
        
        # Сохраняем метаданные чанков
        import pickle
        with open(str(self.index_path) + "_chunks.pkl", 'wb') as f:
            pickle.dump(all_chunks, f)
        
        self.doc_chunks = all_chunks
        print(f"Индекс построен: {len(all_chunks)} чанков из {len(pdf_files)} PDF файлов")
    
    def load_index(self) -> None:
        """
        Загружает сохраненный индекс из файлов.
        """
        if not self.index_path.exists():
            raise FileNotFoundError(f"Файл индекса не найден: {self.index_path}")
        
        index_path_chunks = str(self.index_path) + "_chunks.pkl"
        if not Path(index_path_chunks).exists():
            raise FileNotFoundError(f"Файл метаданных чанков не найден: {index_path_chunks}")
        
        self.index = faiss.read_index(str(self.index_path))
        
        import pickle
        with open(index_path_chunks, 'rb') as f:
            self.doc_chunks = pickle.load(f)
        
        print(f"Индекс загружен: {len(self.doc_chunks)} чанков")
    
    def search(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """
        Выполняет поиск по индексу.
        
        Args:
            query: Поисковый запрос
            k: Количество возвращаемых результатов
            
        Returns:
            Список релевантных чанков с оценкой релевантности
        """
        if self.index is None:
            self.load_index()
        
        # Создаем эмбеддинг запроса
        query_embedding = self.model.encode([query], convert_to_numpy=True)
        faiss.normalize_L2(query_embedding)
        
        # Выполняем поиск
        similarities, indices = self.index.search(query_embedding, k)
        
        results = []
        for i, idx in enumerate(indices[0]):
            if idx != -1:  # Проверяем валидность индекса
                chunk = self.doc_chunks[idx].copy()
                chunk['similarity'] = float(similarities[0][i])
                results.append(chunk)
        
        return results