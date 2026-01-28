import fitz  # PyMuPDF
from pathlib import Path

def parse_bogachev(pdf_path):
    """
    Парсит PDF файл с материалами Богачева и возвращает текст.
    
    Args:
        pdf_path (str): Путь к PDF файлу
    
    Returns:
        str: Текст из первых 5000 символов документа
    """
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    return text[:5000]  # для тестов промптов

def get_document_text(filename="Богачев - периодизация подготовки_compressed.pdf"):
    """
    Получает текст из документа в папке data.
    
    Args:
        filename (str): Имя файла в папке data
    
    Returns:
        str: Текст из документа
    """
    data_dir = Path(__file__).parent.parent / "data"
    pdf_path = data_dir / filename
    
    if not pdf_path.exists():
        raise FileNotFoundError(f"Файл {pdf_path} не найден")
    
    return parse_bogachev(str(pdf_path))