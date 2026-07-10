import os
import time
import json
import traceback
import base64
from datetime import datetime
from pathlib import Path
import pandas as pd
import fitz  # PyMuPDF

from ollama import Client

def pdf_to_base64_images(pdf_path: Path):
    doc = fitz.open(pdf_path)
    base64_images = []
    # Конвертируем каждую страницу в картинку
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(1, 1))  # Стандартное разрешение для скорости
        img_bytes = pix.tobytes("jpeg")
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        base64_images.append(b64)
    return base64_images

def process_pdf_and_save(client, model_name, pdf_path: Path, output_dir: Path):
    print(f"Анализ {pdf_path.name}...")
    try:
        base64_images = pdf_to_base64_images(pdf_path)
    except Exception as e:
        print(f"Ошибка при чтении PDF {pdf_path.name}: {e}")
        return
    
    prompt = """
    Перед тобой страницы сканированного документа, относящегося к строительству (возможно, внутри несколько актов КС-2, КС-3, расчеты удорожания/удешевления).
    Твоя задача — извлечь всю информацию об АРМАТУРЕ (арматурная сталь, каркасы, сетки и т.п.) и сгруппировать ее по Актам.
    
    Правила:
    1. Ищи базовые акты и расчеты удорожания. Если в PDF-документе содержится несколько разных актов (например, КС-2 №1, КС-2 №2 и т.д.), выдели каждый акт отдельно.
    2. Если к акту прилагается "расчет удорожания", то примени логику удорожания к позициям ИМЕННО ЭТОГО акта: 
       - используй измененную цену и объем из расчета;
       - если какой-то объем арматуры НЕ попал под удорожание, выведи его отдельной строкой с базовой ценой (из акта КС).
    3. Если удорожания к акту нет вообще, просто выводи арматуру с ценой из базового акта.
    4. Внутри ОДНОГО акта для каждой позиции арматуры с одинаковой ценой - складывай их объемы (тонны).
    5. Возвращай только арматуру. Игнорируй трубы, бетон, песок, работу механизмов и прочее.
    6. КОЛИЧЕСТВО ОБЯЗАТЕЛЬНО В ТОННАХ. Если указано в кг - раздели на 1000.
    
    Верни результат строго в формате JSON:
    {
      "acts": [
        {
          "act_name": "КС-2 №1 от 09.01.2023",
          "items": [
            {
              "contract_number": "Номер договора (если не найден, пустая строка)",
              "name": "Полное наименование арматуры",
              "quantity_tons": 10.5,
              "price": 50000.0
            }
          ]
        }
      ]
    }
    """
    try:
        response = client.chat(
            model=model_name,
            messages=[{
                'role': 'user',
                'content': prompt,
                'images': base64_images
            }],
            format='json'
        )
        
        result_json = response['message']['content']
        data = json.loads(result_json)
        
        acts = data.get('acts', [])
        
        if not acts:
            print(f"  В документе {pdf_path.name} акты с арматурой не найдены.")
            return

        for act in acts:
            act_name = act.get('act_name', 'Неизвестный_акт').strip()
            # Очищаем имя акта для использования в имени файла
            safe_act_name = "".join([c for c in act_name if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.', '№')]).strip()
            if not safe_act_name:
                safe_act_name = "Акт"
                
            items = act.get('items', [])
            if not items:
                continue
                
            # Добавляем название файла ко всем записями этого акта
            for item in items:
                item['file_name'] = pdf_path.name
                
            df = pd.DataFrame(items)
            
            # Переименовываем столбцы
            rename_map = {
                'file_name': 'Название файла',
                'contract_number': 'Номер договора',
                'name': 'Наименование арматуры',
                'quantity_tons': 'Количество (тонны)',
                'price': 'Цена за тонну'
            }
            # Заполняем недостающие
            for col in rename_map.keys():
                if col not in df.columns:
                    df[col] = ""
                    
            df = df.rename(columns=rename_map)
            
            df['Номер договора'] = df['Номер договора'].astype(str).fillna("")
            df['Количество (тонны)'] = pd.to_numeric(df['Количество (тонны)'], errors='coerce').fillna(0)
            df['Цена за тонну'] = pd.to_numeric(df['Цена за тонну'], errors='coerce').fillna(0)
            
            df = df[['Название файла', 'Номер договора', 'Наименование арматуры', 'Количество (тонны)', 'Цена за тонну']]
            
            # Группировка
            df_grouped = df.groupby(
                ['Название файла', 'Номер договора', 'Наименование арматуры', 'Цена за тонну'], 
                dropna=False,
                as_index=False
            )['Количество (тонны)'].sum()
            
            df_grouped = df_grouped[['Название файла', 'Номер договора', 'Наименование арматуры', 'Количество (тонны)', 'Цена за тонну']]
            
            # Имя итогового файла: Название_исходного_pdf + Название_акта
            safe_pdf_name = pdf_path.stem
            excel_filename = f"{safe_pdf_name} - {safe_act_name}.xlsx"
            excel_path = output_dir / excel_filename
            
            df_grouped.to_excel(excel_path, index=False)
            print(f"  -> Создан файл: {excel_filename}")
            
    except Exception as e:
        print(f"Ошибка при обработке {pdf_path.name}: {e}")
        traceback.print_exc()

def load_env():
    env_path = Path(__file__).parent / "ollama.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip()

def main():
    load_env()
    # Настройки Ollama Cloud
    api_key = os.getenv("OLLAMA_API_KEY")
    base_url = os.getenv("OLLAMA_BASE_URL", "https://ollama.com")
    model_name = os.getenv("OLLAMA_MODEL", "gemini-3-flash-preview")
    
    if not api_key:
        print("Ошибка: OLLAMA_API_KEY не задан в .env файле.")
        return

    # Извлекаем хост из base_url (если оканчивается на /v1, для ollama Client обрезаем)
    host = base_url
    if host.endswith("/v1"):
        host = host[:-3]

    client = Client(
        host=host,
        headers={'Authorization': 'Bearer ' + api_key}
    )
    
    base_dir = Path("/Users/andreybocharov/Documents/окр парсер 2")
    export_base_dir = base_dir / "Экспорт"
    
    target_folders = ["Ипподромская", "КС БГ", "Овражная"]
    
    for folder_name in target_folders:
        folder_path = base_dir / folder_name
        if not folder_path.exists() or not folder_path.is_dir():
            continue
            
        print(f"\n{'='*40}")
        print(f"Обработка папки: {folder_name}")
        print(f"{'='*40}")
        
        output_dir = export_base_dir / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        
        pdf_files = list(folder_path.glob("*.pdf"))
        
        if not pdf_files:
            print("PDF файлы не найдены.")
            continue
            
        for pdf_path in pdf_files:
            process_pdf_and_save(client, model_name, pdf_path, output_dir)
            time.sleep(1) # Небольшая пауза между файлами

if __name__ == "__main__":
    main()
