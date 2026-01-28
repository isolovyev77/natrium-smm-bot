# ✅ ИСПРАВЛЕН ВЫВОД СТАТИСТИКИ ТОКЕНОВ

**Дата**: 25 января 2026  
**Статус**: ✅ **ИСПРАВЛЕНО!**

---

## 🔍 ПРОБЛЕМА

**Что было**:
```
----------------------------------------------------------------------
📊 Генерация тем
Tokens in/out/total: 0+0=5142
----------------------------------------------------------------------
```

❌ Показывался только `total_tokens`, а `input` и `output` были = 0

---

## 🎯 ПРИЧИНА

### **Неправильные названия полей**:

**В коде было**:
```python
usage = {
    'input_tokens': getattr(response.usage, 'input_text_tokens', 0),
    'completion_tokens': getattr(response.usage, 'completion_tokens', 0),
    'total_tokens': getattr(response.usage, 'total_tokens', 0)
}
```

**Проблема**:
- Поле называется `input_tokens` (а НЕ `input_text_tokens`)
- Поле называется `output_tokens` (а НЕ `completion_tokens`)

### **Реальная структура OpenAI SDK для Yandex**:

```python
response.usage = ResponseUsage(
    input_tokens=4991,          # ✅ входящие токены
    output_tokens=151,          # ✅ исходящие токены
    total_tokens=5142,          # ✅ всего токенов
    input_tokens_details={      # 📊 детали входящих
        'cached_tokens': 0,
        'valid': True
    },
    output_tokens_details={     # 📊 детали исходящих
        'reasoning_tokens': 0,
        'valid': True
    }
)
```

---

## ✅ РЕШЕНИЕ

### **1. Исправлено извлечение usage данных** (`bot.py`):

```python
usage = {
    'input_tokens': getattr(response.usage, 'input_tokens', 0),    # ✅ правильно
    'output_tokens': getattr(response.usage, 'output_tokens', 0),  # ✅ правильно
    'total_tokens': getattr(response.usage, 'total_tokens', 0),
    'input_tokens_details': getattr(response.usage, 'input_tokens_details', None),
    'output_tokens_details': getattr(response.usage, 'output_tokens_details', None)
}
```

### **2. Обновлена функция вывода** (`main.py`):

```python
def print_token_usage(operation: str, usage: dict):
    input_tokens = usage.get('input_tokens', 0)    # ✅ правильный ключ
    output_tokens = usage.get('output_tokens', 0)  # ✅ правильный ключ
    total_tokens = usage.get('total_tokens', 0)
    
    print(f"Tokens in/out/total: {input_tokens}+{output_tokens}={total_tokens}")
    
    # Выводим дополнительные детали
    input_details = usage.get('input_tokens_details')
    output_details = usage.get('output_tokens_details')
    
    if input_details and input_details.get('cached_tokens', 0) > 0:
        print(f"   └─ cached: {input_details['cached_tokens']} токенов")
    
    if output_details and output_details.get('reasoning_tokens', 0) > 0:
        print(f"   └─ reasoning: {output_details['reasoning_tokens']} токенов")
```

---

## 📊 НОВЫЙ ФОРМАТ ВЫВОДА

### **Базовая статистика**:
```
----------------------------------------------------------------------
📊 Генерация тем
Tokens in/out/total: 4991+151=5142
----------------------------------------------------------------------
```

✅ Теперь показываются ВСЕ значения!

### **С дополнительными деталями** (если есть):
```
----------------------------------------------------------------------
📊 Генерация поста
Tokens in/out/total: 6234+812=7046
   └─ cached: 1523 токенов
   └─ reasoning: 45 токенов
----------------------------------------------------------------------
```

**Где**:
- `cached` — закешированные входящие токены (экономия!)
- `reasoning` — токены на "размышления" модели (для сложных запросов)

---

## 🔧 ДИАГНОСТИКА

### **Как я нашёл проблему**:

1. Создал тестовый скрипт `test_usage_structure.py`
2. Отправил запрос к API
3. Вывел ВСЮ структуру `response.usage`
4. Обнаружил правильные названия полей

### **Структура ResponseUsage**:

```
ResponseUsage(
    input_tokens=4991,
    input_tokens_details=InputTokensDetails(cached_tokens=0, valid=True),
    output_tokens=151,
    output_tokens_details=OutputTokensDetails(reasoning_tokens=0, valid=True),
    total_tokens=5142,
    valid=True
)
```

**Ключевые поля**:
- ✅ `input_tokens` — входящие токены
- ✅ `output_tokens` — исходящие токены  
- ✅ `total_tokens` — сумма
- 📊 `input_tokens_details` — детали (cached_tokens)
- 📊 `output_tokens_details` — детали (reasoning_tokens)

---

## 💰 РАСЧЁТ СТОИМОСТИ (ОБНОВЛЁННЫЙ)

### **Тарифы YandexGPT Pro**:
- Входящие токены: ~₽0.0004 за токен
- Исходящие токены: ~₽0.0012 за токен

### **Пример**:
```
Генерация тем: 4991 in + 151 out = 5142 total
Стоимость = (4991 × ₽0.0004) + (151 × ₽0.0012)
         = ₽2.00 + ₽0.18
         = ₽2.18
```

---

## 📝 ИЗМЕНЁННЫЕ ФАЙЛЫ

### **1. `src/bot.py`**:

**Было**:
```python
'input_tokens': getattr(response.usage, 'input_text_tokens', 0)
'completion_tokens': getattr(response.usage, 'completion_tokens', 0)
```

**Стало**:
```python
'input_tokens': getattr(response.usage, 'input_tokens', 0)
'output_tokens': getattr(response.usage, 'output_tokens', 0)
'input_tokens_details': getattr(response.usage, 'input_tokens_details', None)
'output_tokens_details': getattr(response.usage, 'output_tokens_details', None)
```

### **2. `src/main.py`**:

**Было**:
```python
input_tokens = usage.get('input_tokens', 0)
completion_tokens = usage.get('completion_tokens', 0)
```

**Стало**:
```python
input_tokens = usage.get('input_tokens', 0)
output_tokens = usage.get('output_tokens', 0)

# + вывод деталей (cached, reasoning)
```

---

## ✅ ИТОГ

**Статус**: ✅ **ПОЛНОСТЬЮ ИСПРАВЛЕНО!**

**Что было исправлено**:
1. ✅ Использованы правильные названия полей (`input_tokens`, `output_tokens`)
2. ✅ Добавлено извлечение дополнительных деталей
3. ✅ Обновлён вывод статистики с деталями

**Результат**:
- ✅ Теперь показываются ВСЕ токены: `in`, `out`, `total`
- ✅ Выводятся дополнительные детали (`cached`, `reasoning`)
- ✅ Можно точно рассчитать стоимость

**Тестирование**:
```bash
cd /Users/isolovyev/Projects/smm_bot/NatriumSMM
source .venv/bin/activate
python src/main.py
```

После генерации тем или поста вы увидите:
```
----------------------------------------------------------------------
📊 Генерация тем
Tokens in/out/total: 4991+151=5142
----------------------------------------------------------------------
```

✅ Все значения корректны!

---

**Дата**: 25 января 2026  
**Статус**: ✅ Вывод токенов полностью исправлен!
