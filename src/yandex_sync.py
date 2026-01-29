import os
import requests
import json
from pathlib import Path
from typing import List, Dict, Any
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

class YandexFileSync:
    def __init__(self):
        self.api_key = os.getenv('YANDEX_CLOUD_API_KEY')
        self.folder_id = os.getenv('YANDEX_FOLDER_ID')
        self.agent_id = os.getenv('YANDEX_AGENT_ID')
        self.base_url = "https://api.yandexgpt.com/v1/fileSearch"
        
        if not self.api_key:
            raise ValueError("YANDEX_CLOUD_API_KEY должен быть задан в .env файле")
        
        if not self.folder_id:
            raise ValueError("YANDEX_FOLDER_ID должен быть задан в .env файле")
        
    def _make_request(self, endpoint: str, method: str = "POST", data: Dict[str, Any] = None, 
                     files: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Выполняет запрос к API Yandex.
        
        Args:
            endpoint: Конечная точка API
            method: HTTP метод
            data: Данные для отправки
            files: Файлы для отправки
            
        Returns:
            Ответ API
        """
        headers = {
            "Authorization": f"Api-Key {self.api_key}"
        }
        
        url = f"{self.base_url}/{endpoint}"
        
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            data=data,
            files=files
        )
        
        if response.status_code != 200:
            raise Exception(f"Ошибка API Yandex: {response.status_code} - {response.text}")
        
        return response.json()
    
    def upload_file(self, file_path: str) -> Dict[str, Any]:
        """
        Загружает файл в Yandex FileSearch.
        
        Args:
            file_path: Путь к файлу для загрузки
            
        Returns:
            Информация о загруженном файле
        """
        file_path = Path(file_path)
        
        if not file_path.exists():
            raise FileNotFoundError(f"Файл не найден: {file_path}")
        
        with open(file_path, 'rb') as f:
            files = {
                'file': (file_path.name, f, 'application/pdf')
            }
            
            data = {
                'folderId': self.folder_id
            }
            
            response = self._make_request(
                endpoint="files", 
                method="POST", 
                data=data, 
                files=files
            )
        
        print(f"Файл загружен: {file_path.name} (ID: {response['id']})")
        return response
    
    def upload_directory(self, directory_path: str = "data") -> List[Dict[str, Any]]:
        """
        Загружает все PDF файлы из директории в Yandex FileSearch.
        
        Args:
            directory_path: Путь к директории с файлами
            
        Returns:
            Список информации о загруженных файлах
        """
        directory_path = Path(directory_path)
        pdf_files = list(directory_path.glob("*.pdf"))
        
        if not pdf_files:
            raise ValueError(f"Не найдено PDF файлов в директории {directory_path}")
        
        results = []
        for pdf_file in pdf_files:
            try:
                result = self.upload_file(str(pdf_file))
                results.append(result)
            except Exception as e:
                print(f"Ошибка при загрузке {pdf_file.name}: {e}")
        
        print(f"Загружено {len(results)} из {len(pdf_files)} файлов")
        return results
    
    def list_files(self) -> List[Dict[str, Any]]:
        """
        Получает список загруженных файлов.
        
        Returns:
            Список файлов
        """
        params = {
            'folderId': self.folder_id
        }
        
        response = self._make_request(
            endpoint="files", 
            method="GET", 
            data=params
        )
        
        return response.get('files', [])
    
    def delete_file(self, file_id: str) -> None:
        """
        Удаляет файл из Yandex FileSearch.
        
        Args:
            file_id: ID файла для удаления
        """
        self._make_request(
            endpoint=f"files/{file_id}", 
            method="DELETE"
        )
        print(f"Файл удален: {file_id}")
    
    def delete_all_files(self) -> None:
        """
        Удаляет все файлы из Yandex FileSearch.
        """
        files = self.list_files()
        for file in files:
            self.delete_file(file['id'])