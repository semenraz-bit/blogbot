import os
import time
import json
import base64
import re
from pathlib import Path
import pandas as pd
import fitz  # PyMuPDF
from tqdm import tqdm
from ollama import Client

def pdf_to_base64_pages(pdf_path: Path):
    doc = fitz.open(pdf_path)
    pages_data = []
    for i, page in enumerate(doc):
        # 1. Обычная ориентация
        pix_0 = page.get_pixmap(matrix=fitz.Matrix(1, 1))
        b64_0 = base64.b64encode(pix_0.tobytes("jpeg")).decode("utf-8")
        
        # 2. Повернутая ориентация (для альбомых листов, отсканированных боком)
        page.set_rotation(270)
        pix_270 = page.get_pixmap(matrix=fitz.Matrix(1, 1))
        b64_270 = base64.b64encode(pix_270.tobytes("jpeg")).decode("utf-8")
        
        pages_data.append((i + 1, b64_0, b64_270))
    return pages_data

def extract_csv_from_image(client, model_name, b64_img, page_num, orientation):
    prompt = f"""
    Перед тобой страница {page_num} из строительного документа.
    Твоя задача — извлечь ВСЮ таблицу без исключений в сырой CSV формат.
    
    ИНСТРУКЦИЯ:
    1. В самой ПЕРВОЙ строке напиши строго в таком формате:
       CONTEXT: [Номер договора] | [Название Акта (например КС-2 №3, КС-2 №4, или Расчет удорожания к КС-2 №4)] | [Раздел]
       Ищи эти данные в заголовках таблиц или шапке документа. Это критически важно!
    2. Начиная со второй строки, переведи ВСЮ таблицу в CSV. Ничего не фильтруй!
    3. Разделитель ';'.
    
    Если таблица перевернута или нечитаема, верни пустую строку.
    Верни только сырой текст, без маркдауна.
    """
    try:
        response = client.chat(
            model=model_name,
            messages=[{
                'role': 'user',
                'content': prompt,
                'images': [b64_img]
            }]
        )
        return response['message']['content'].strip()
    except Exception as e:
        return f"ERROR: {e}"

def filter_rebar_lines(csv_text):
    if not csv_text or "ERROR:" in csv_text:
        return []
        
    lines = csv_text.split('\n')
    context = "CONTEXT: Неизвестно"
    filtered = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.startswith("CONTEXT:"):
            context = line
            continue
            
        lower_line = line.lower()
        if "арматур" in lower_line or "арматуp" in lower_line or "apмaтyp" in lower_line:
            filtered.append(f"{context} || {line}")
            
    return filtered

def step3_parse_lines_and_group(filtered_lines, pdf_filename):
    items = []
    
    for line in filtered_lines:
        if "||" not in line:
            continue
            
        context_str, csv_str = line.split("||", 1)
        context_str = context_str.strip()
        csv_str = csv_str.strip()
        
        # Определяем акт
        act_name = "Акт"
        if "КС-2 №4" in context_str or "КС2 №4" in context_str:
            act_name = "КС-2 №4"
        elif "КС-2 №3" in context_str or "КС2 №3" in context_str or "КС-3 №3" in context_str:
            act_name = "КС-2 №3"
        elif "КС-2 №12" in context_str or "КС2 №12" in context_str:
            act_name = "КС-2 №12"
            
        # Определяем удорожание
        is_increase = "удорожани" in context_str.lower()
        
        # Парсим CSV
        parts = [p.strip() for p in csv_str.split(";")]
        name = ""
        quantity = 0.0
        price_base = 0.0
        price_fact = 0.0
        
        for i, p in enumerate(parts):
            lower_p = p.lower()
            if "арматур" in lower_p or "арматуp" in lower_p or "apмaтyp" in lower_p:
                name = p
                # Ищем количество и цены в следующих ячейках
                for j in range(i+1, len(parts)):
                    cell = parts[j]
                    lower_cell = cell.lower()
                    if lower_cell in ('т', 'тн', 'шт', 'материал', 'субподрядчик', 'договорная цена'): 
                        continue
                        
                    try:
                        clean_cell = cell.replace(" ", "").replace(",", ".")
                        clean_cell = re.sub(r'[^\d.]', '', clean_cell)
                        if not clean_cell:
                            continue
                            
                        val = float(clean_cell)
                        if quantity == 0.0:
                            quantity = val
                        elif price_base == 0.0:
                            price_base = val
                        elif price_fact == 0.0:
                            price_fact = val
                            break
                    except:
                        pass
                break
                
        if quantity > 0:
            items.append({
                "Название файла": pdf_filename,
                "Акт КС-2": act_name,
                "Номер договора": "",
                "Раздел работ": "",
                "Наименование арматуры (из документа)": name,
                "Арматура (стандарт)": name,
                "Класс": "",
                "Диаметр, мм": "",
                "Количество, т": quantity,
                "Сметная цена без НДС, руб/т": price_base,
                "Фактическая цена без НДС, руб/т": price_fact if is_increase else price_base,
                "Удорожание": "Да" if is_increase else "Нет"
            })
            
    df = pd.DataFrame(items)
    if df.empty:
        return df
        
    df['Фактическая цена с НДС, руб/т'] = df['Фактическая цена без НДС, руб/т'] * 1.2
    df['Сметная стоимость без НДС, руб'] = df['Количество, т'] * df['Сметная цена без НДС, руб/т']
    df['Фактическая стоимость без НДС, руб'] = df['Количество, т'] * df['Фактическая цена без НДС, руб/т']
    df['Отклонение без НДС, руб'] = df['Фактическая стоимость без НДС, руб'] - df['Сметная стоимость без НДС, руб']
    
    df['Тип изменения'] = df['Удорожание'].apply(lambda x: "Удорожание цены" if x == "Да" else "Без изменения")
    df['Уверенность'] = "Высокая"
    
    group_cols = [
        'Название файла', 'Акт КС-2', 'Номер договора', 'Раздел работ', 
        'Наименование арматуры (из документа)', 'Арматура (стандарт)', 
        'Класс', 'Диаметр, мм', 'Сметная цена без НДС, руб/т', 
        'Фактическая цена без НДС, руб/т', 'Удорожание', 'Тип изменения', 'Уверенность'
    ]
    
    df[group_cols] = df[group_cols].fillna("")
    
    agg_dict = {
        'Количество, т': 'sum',
        'Фактическая цена с НДС, руб/т': 'first',
        'Сметная стоимость без НДС, руб': 'sum',
        'Фактическая стоимость без НДС, руб': 'sum',
        'Отклонение без НДС, руб': 'sum'
    }
    
    grouped_df = df.groupby(group_cols, as_index=False).agg(agg_dict)
    
    columns_order = [
        'Название файла', 'Акт КС-2', 'Номер договора', 'Раздел работ', 
        'Наименование арматуры (из документа)', 'Арматура (стандарт)', 
        'Класс', 'Диаметр, мм', 'Количество, т', 'Сметная цена без НДС, руб/т', 
        'Фактическая цена без НДС, руб/т', 'Фактическая цена с НДС, руб/т', 
        'Сметная стоимость без НДС, руб', 'Фактическая стоимость без НДС, руб', 
        'Отклонение без НДС, руб', 'Удорожание', 'Тип изменения', 'Уверенность'
    ]
    
    for col in columns_order:
        if col not in grouped_df.columns:
            grouped_df[col] = ""
            
    return grouped_df[columns_order]

def process_pdf_and_save(client, model_name, pdf_path: Path, output_dir: Path):
    print(f"Анализ {pdf_path.name}...")
    pages_data = pdf_to_base64_pages(pdf_path)
    
    print(f"  Всего страниц: {len(pages_data)}. Этап 1: Двойное сканирование (0° и 270°)...")
    all_filtered_lines = []
    
    for page_num, b64_0, b64_270 in tqdm(pages_data, desc="Чтение страниц"):
        text_0 = extract_csv_from_image(client, model_name, b64_0, page_num, 0)
        lines_0 = filter_rebar_lines(text_0)
        
        text_270 = extract_csv_from_image(client, model_name, b64_270, page_num, 270)
        lines_270 = filter_rebar_lines(text_270)
        
        if lines_0:
            all_filtered_lines.extend(lines_0)
        elif lines_270:
            all_filtered_lines.extend(lines_270)
            
        time.sleep(0.5)
        
    print(f"\n  Этап 2: Фильтрация. Найдено {len(all_filtered_lines)} строк с арматурой.")
    
    with open("debug_filtered_lines_v7.txt", "w") as f:
        f.write("\n".join(all_filtered_lines))
        
    if not all_filtered_lines:
        print(f"  В документе {pdf_path.name} арматура не найдена.")
        return
        
    print("  Этап 3: Парсинг строк и Группировка (Только Python)...")
    final_df = step3_parse_lines_and_group(all_filtered_lines, pdf_path.name)
    
    if final_df.empty:
        print(f"  Не удалось сформировать финальные акты.")
        return

    acts = final_df['Акт КС-2'].unique()
    for act_name in acts:
        act_df = final_df[final_df['Акт КС-2'] == act_name]
        
        safe_act_name = "".join([c for c in act_name if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.', '№')]).strip()
        if not safe_act_name:
            safe_act_name = "Акт"
            
        safe_pdf_name = pdf_path.stem
        base_filename = f"списание_арматуры_{safe_pdf_name}_{safe_act_name}"
        excel_filename = f"{base_filename}.xlsx"
        excel_path = output_dir / excel_filename
        
        counter = 1
        while excel_path.exists():
            excel_filename = f"{base_filename}_часть{counter}.xlsx"
            excel_path = output_dir / excel_filename
            counter += 1
            
        act_df.to_excel(excel_path, index=False)
        print(f"  -> Создан файл: {excel_filename}")

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
    debug_dir = base_dir / "Отладка"
    output_dir = base_dir / "Экспорт_Отладка"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    pdf_files = list(debug_dir.glob("*.pdf"))
    
    if not pdf_files:
        print("PDF файлы не найдены в папке Отладка.")
        return
        
    for pdf_path in pdf_files:
        process_pdf_and_save(client, model_name, pdf_path, output_dir)

if __name__ == "__main__":
    main()
