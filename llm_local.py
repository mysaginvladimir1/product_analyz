import json
import re
from datetime import datetime
import pandas as pd
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer



class LocalHFModel:
    def __init__(self, model_path, device="cpu", max_new_tokens=512):
        print(f"Загрузка модели из {model_path}...")
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True, use_fast=False )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
             # Попытка загрузить в half-точность (float16) для экономии памяти
        torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            dtype=torch_dtype,   
            low_cpu_mem_usage=True,
            trust_remote_code=True,
            device_map="auto" if torch.cuda.is_available() else None,
            attn_implementation='eager',   # отключаем flash-attention
        ).to(device if not torch.cuda.is_available() else "cuda")
        self.device = device if not torch.cuda.is_available() else "cuda"
        self.max_new_tokens = max_new_tokens
        self.model.eval()
        

    def _call_llm(self, prompt, temperature=0.2, max_new_tokens=None):
        if max_new_tokens is None:
            max_new_tokens = self.max_new_tokens
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                max_length=None,
                temperature=temperature if temperature > 0 else 1.0,
                do_sample=(temperature > 0),
                pad_token_id=self.tokenizer.eos_token_id
            )
        response = self.tokenizer.decode(outputs[0][inputs['input_ids'].shape[1]:], skip_special_tokens=True)
        return response.strip()

    def parse_user_query(self, user_query, available_shifts=None):
        today = datetime.now().date()
        prompt = f"""Ты помощник производственного аналитика. Извлеки параметры из запроса в формате JSON.
Дата сегодня: {today}
Доступные смены (пример): 2025-12-27_День, 2025-12-27_Ночь

Верни ТОЛЬКО JSON-объект. Поля:
- shift_list: список строк, каждая строка в формате "ГГГГ-ММ-ДД_День" или "ГГГГ-ММ-ДД_Ночь" (например, ["2025-12-27_День", "2025-12-27_Ночь"]).
- start_date: строка "ГГГГ-ММ-ДД" (если указан диапазон дат)
- end_date: строка "ГГГГ-ММ-ДД"
- start_shift_type: "День" или "Ночь" (если указано в начале)
- end_shift_type: "День" или "Ночь"
- machines: список строк (номера станков)
- operators: список строк (ФИО)
- metrics_type: "kim" | "downtime" | "operator" | "hourly" 
- aggregation: "detailed" | "period"



Примеры:

Запрос: "Комов Александр Викторович, Краснов Юрий Андреевич"
Ответ: {{"metrics_type": "operator", "aggregation": "period", "operators": ["Комов Александр Викторович, Краснов Юрий Андреевич]"}}

Запрос: "Работа оператора Комов Александр Викторович"
Ответ: {{"metrics_type": "operator", "aggregation": "period", "operators": ["Комов Александр Викторович]" }}

Запрос: "В какие смены работал оператор Краснов Юрий Андреевич"
Ответ: {{"metrics_type": "operator", "aggregation": "period", "operators": ["Краснов Юрий Андреевич]"}}

Запрос: "КИМ станка 3.1_BEIPING T6 за 27 декабря 2025"
Ответ: {{"start_date": "2025-12-27", "end_date": "2025-12-27", "machines": ["3.1_BEIPING T6"], "metrics_type": "kim", "aggregation": "period"}}

Запрос: "Простои в ночную смену 27 декабря"
Ответ: {{"shift_list": ["2025-12-27_Ночь"], "metrics_type": "downtime", "aggregation": "detailed"}}

Запрос: "Сводка по операторам за 28 декабря"
Ответ: {{"start_date": "2025-12-28", "end_date": "2025-12-28", "metrics_type": "operator", "aggregation": "period"}}

Запрос: "Топ-5 операторов по производительности за 27 декабря"
Ответ: {{"start_date": "2025-12-27", "end_date": "2025-12-27", "metrics_type": "operator", "aggregation": "period"}}



Запрос: {user_query}
Ответ:"""
        response = self._call_llm(prompt, temperature=0.0, max_new_tokens=800)

        # Извлечение JSON (первая открывающая скобка и сбалансированная закрывающая)
        start = response.find('{')
        if start == -1:
            return {"error": "JSON не найден", "raw_response": response[:200]}
        brace_count = 0
        end = -1
        for i, ch in enumerate(response[start:], start):
            if ch == '{':
                brace_count += 1
            elif ch == '}':
                brace_count -= 1
                if brace_count == 0:
                    end = i + 1
                    break
        if end == -1:
            return {"error": "Незакрытый JSON", "raw_response": response[:200]}
        json_str = response[start:end]

        try:
            params = json.loads(json_str)
        except json.JSONDecodeError as e:
            return {"error": f"Ошибка JSON: {e}", "raw_response": json_str}

        # Фильтрация: удаляем некорректный shift_list
        if 'shift_list' in params:
            sl = params['shift_list']
            if not isinstance(sl, list) or not all(isinstance(x, str) and '_' in x for x in sl):
                del params['shift_list']

        # Преобразование дат
        if 'start_date' in params and params['start_date']:
            params['start_date'] = datetime.strptime(params['start_date'], '%Y-%m-%d').date()
        if 'end_date' in params and params['end_date']:
            params['end_date'] = datetime.strptime(params['end_date'], '%Y-%m-%d').date()
        return params

    def generate_response(self, user_query, data_result, metrics_type):
        """
        Генерирует краткий ответ на основе данных, а не передаёт сырой DataFrame.
        """
       # Преобразуем данные в читаемый текст
        if isinstance(data_result, pd.DataFrame):
            if data_result.empty:
                data_text = "Данные отсутствуют."
            else:
                data_text = data_result.to_string(index=True)
        elif isinstance(data_result, pd.Series):
            if data_result.empty:
                data_text = "Данные отсутствуют."
            else:
                data_text = data_result.to_string()
        else:
            data_text = str(data_result)



        
        prompt = f"""Ты производственный аналитик. На основе предоставленных данных ответь на запрос пользователя. 
        Ответ должен быть кратким, содержать только факты и не содержать вопросов.
        Не выводи в ответ таблицу строки таблиц, которые получаешь.
        Если работаешь с метрикой "kim", укажи процент полезного использования оборудования (% полезной работы). 
        Если работаешь с метрикой "operator", то нужно рассказать, какой процент незарегистрированного простоя оборудования (%_неотмеченного_простоя) у оператора,
        сколько минут он был зарегистрирован (Всего_минут) и какое продуктивное время в минутах (Продуктивное_время_мин) и в процентах (%_продуктивного времени), и нужно вывести информацию, на каких станках работал оператор (Обслуживал_станки).
        Если пользователь спрашивает, в какие смены работал оператор, то выведи список смен (работал_в_смены), например 2025-12-28_Ночь, 2025-12-29_Ночь (если не просит, не выводи). 
        Если пользователь просит вывести топ-n операторов производительности, то нужно вывести два списка в порядке убывания значения: 1) список n операторов с наивысшим показателем "%_продуктивного времени", 2) список n операторов с наивысшим показателем Продуктивное_время_мин.  
        Если работаешь с метрикой "downtime" - то в столбце "вид простоя" указан вид простоя, а в стлбце "Доля_%" указан процент этого простоя, первая строка данных - общая сумма простоя оборудования; дай топ-5 простоев оборуодвания в формате ["вид простоя" - "минуты" или "Доля_%"], соотнеси вид простоя из первого столбца с данными о простоях из столбцов 2 и 3;
        Сумма состояний "станок включен" и "jog" составляет значение неотмеченного простоя оборудования.
        
        

Запрос пользователя: {user_query}
Тип метрики: {metrics_type}
Данные (первые строки):
{data_text}

Ответ:"""
        #print ('Полученный текст' + data_text)
        return self._call_llm(prompt, temperature=0.0, max_new_tokens=200)

    
    
    
